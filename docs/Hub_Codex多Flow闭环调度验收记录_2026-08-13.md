# Hub × Codex 多 Flow 闭环调度验收记录

## 1. 验收结论

| 项目 | 结果 |
| --- | --- |
| 验收日期 | 2026-08-13 |
| 验收环境 | Hub `http://clawteam.woa.com:18800`，RacingGO 项目 `project_id=6` |
| 验收依据 | `Hub_Codex多Flow闭环调度改造需求_2026-08-12.md` |
| 总体结论 | **有条件不通过，暂不可启用候选自动晋级与闭环自动调度** |
| Flow #12 影响 | 本次验收未修改 Definition #12 和正式用例库 #33；Flow #12 固定调度可继续使用旧路径 |
| Flow #25 | Codex 定时任务已暂停，Hub 无正在执行的 Flow #25 Run |

当前候选幂等、乐观锁、状态机、资源租约、Run 幂等和关系追溯的基础接口已可用；但并发 promotion 出现“请求失败但 revision 被推进”的事务一致性问题，Flow #12 历史 Run 均未冻结用例库快照，回滚、Evidence Manifest、Finding 和 Capability Catalog 接口也未完整部署。因此只允许继续运行现有 Flow #12，不应开启候选自动晋级或把闭环控制器接到生产用例库。

## 2. 必须先修复的阻断问题

### P0-1 并发 promotion 存在失败副作用

在隔离用例库 #34 上让两个已合格候选基于同一 revision 并发发布：

- 一个请求返回 HTTP 500；另一个返回 HTTP 409。
- 没有任何请求返回成功。
- 用例库 revision 却从 1 推进到 2。
- case_count 仍为 1，promotion 记录仍只有原来的 1 条，候选也未进入 ACTIVE。
- revision 2 被记录为 `legacy_sync`，形成无法由成功请求解释的幽灵 revision。

这违反 AT-05 以及“testcase、snapshot、candidate state、relation、promotion 同事务”的要求。修复后必须保证并发时恰好一个提交成功，另一个稳定返回 409；500/409 均不得产生任何库 revision、快照、用例或候选状态副作用。

### P0-2 Flow #12 未冻结用例库快照

- 正式用例库 #33 当前有 22 条用例，但 revision 仍为 0。
- 自动化闭环页展示的最近 12 次 Flow #12 Run，revision 和 cases 全部为 `—`。
- API 返回这些 Run 的 `library_snapshot` 均为空。

AT-06 不通过。需要先把 #33 当前内容登记为初始 revision，并在 Flow #12 创建或开始读取用例时兼容地冻结快照；快照记录失败只能告警，不得反向阻断 Flow #12。

### P0-3 回滚接口未部署

- revision 历史可以查询。
- `POST /api/v1/testcase-libraries/{id}/rollback` 返回 404。
- snapshot 列表为空。

AT-12 无法执行。必须提供产生新审计 revision 的回滚能力，不能覆盖或删除历史。

## 3. 其他缺陷

### P1-1 分析闭环接口不完整

以下要求中的接口当前返回 404：

- `GET /api/v1/evidence-manifests`
- `GET /api/v1/findings`
- `GET /api/v1/automation-capabilities`

`GET /api/v1/workflow-runs/121/evidence-manifest` 返回 `SHIFT_LEFT_DISABLED`。因此 AT-10 证据不全语义、Finding 反馈和规则学习无法验收，闭环页的证据完整率也只能显示 `—`。

### P1-2 Capability Gap 回队标记未清理

Gap #1 resolve 后返回 `candidate_requeue_pending=true`。候选已从 `WAITING_CAPABILITY` 重新进入 `READY_FOR_CANARY`，最终也已退役，但 `candidate_requeue_pending=true` 查询仍返回该 Gap，闭环页仍显示“待回队 1”。回队成功或候选终止后应原子清理该标记。

### P1-3 终态 Run 元数据与本地资源回收不一致

Hub Run #121 已是 `blocked`，但 `finished_at` 仍为空。验收结束时旧的 Flow #12 Unity 进程和 3 个 MCP/uvx 进程仍存活，虽然本地 `flow12.active` 锁已不存在。终态写入、锁释放和进程清理需要统一，并保留可审计的清理结果。

### P1-4 隔离数据删除接口返回 500

隔离候选 #1~#4 已全部退役，但临时用例 #15053 和临时用例库 #34 无法删除：API 与前端删除均返回“服务器内部错误”。候选、Capability Gap 和关系也没有可用的验收清理接口。需要补充稳定错误码、request_id，以及管理员可用的隔离/清理机制。

## 4. 验收用例结果

| 用例 | 结果 | 说明 |
| --- | --- | --- |
| AT-01 创建 Run 幂等 | 通过 | 临时 Definition #28 使用相同 idempotency_key 创建两次，均返回 Run #122；临时 Run 和 Definition 已删除 |
| AT-02 候选状态并发保护 | 通过 | 同 version 并发 PATCH：一个成功，一个返回 409 `CANDIDATE_VERSION_CONFLICT` |
| AT-03 能力不足不能入正式库 | 通过 | 候选进入 WAITING_CAPABILITY，promotion 返回 409，目标库 revision 和 case_count 未变化 |
| AT-04 验证通过只发布一次 | 通过 | 隔离库同一 promotion 重放返回相同 promotion、case 和 revision，仅创建一次 |
| AT-05 两个发布者并发 | **失败/P0** | 500 + 409，但库 revision 仍推进，事务出现失败副作用 |
| AT-06 Flow12 快照稳定 | **失败/P0** | #33 revision 为 0，最近 12 次 Flow #12 Run 全部无 snapshot |
| AT-07 Flow12 不受外围闭环阻断 | 部分通过 | 验收未给 Flow #12 增加门禁；Run #121 的阻断原因为 Unity prefab YAML 解析错误，与闭环 API 无关 |
| AT-08 资源冲突 | 通过（隔离资源） | 第二持有者收到 409，响应含 holder、expires_at、retry_after_seconds；释放后可由另一持有者获取 |
| AT-09 租约超时回收 | 未完整验收 | 显式 release/reacquire 已通过，TTL 自动失效和过期审计尚未单独等待验证 |
| AT-10 证据不全语义 | **无法验收/P1** | Evidence Manifest 路由未部署，Run 路由返回 SHIFT_LEFT_DISABLED |
| AT-11 全链路追溯 | 部分通过 | relation batch-upsert 幂等、正反向查询和 trace 可用；Finding/Evidence 缺失导致完整链路未闭合 |
| AT-12 回滚 | **失败/P0** | rollback 路由 404 |

## 5. 已通过的基础能力

1. 候选 upsert 支持 idempotency_key/dedupe_key，重复提交返回同一候选。
2. 候选 PATCH 支持 expected_version，冲突稳定返回 409。
3. 服务端会拒绝 `READY_FOR_CANARY -> ACTIVE` 等非法跳转。
4. Capability Gap 能创建、关联需求并阻止候选写入正式库。
5. promotion 单请求重放幂等。
6. Workflow Run 创建、latest 查询与创建重放幂等可用。
7. entity relation 批量写入幂等，支持正向、反向与多跳 trace。
8. resource lease 冲突、释放和重新获取可用，错误信息包含等待所需字段。
9. Workflow Definition 空 steps 会返回明确字段校验错误。
10. 自动化闭环只读页面已部署，候选、Gap、用例库、Flow Run、证据、Finding 和租约区块可以正常加载。

## 6. 修复与复验顺序

1. 先修 promotion 事务边界和并发锁，增加“失败请求零副作用”的后端集成测试。
2. 初始化正式库 #33 revision，并补齐 Flow #12 Run 快照写入与回读。
3. 实现 rollback 并复验历史 revision、hash、case_count 与旧 Run 快照不变。
4. 部署 Evidence Manifest、Finding、automation-capabilities 的正式路由并启用 shift-left 数据模型。
5. 修复 Capability Gap 回队标记、Run `finished_at` 和资源回收。
6. 补充隔离数据的管理员清理接口后，重新执行 AT-01~AT-12。

复验全部通过前，Codex 控制器只做只读分析、候选设计和状态观察；不得自动 promotion 到正式库 #33。Flow #12 保持固定旧路径运行，不依赖外围闭环。
