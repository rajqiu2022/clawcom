# Test Plan 计划级守护器 v1

依据协作纪要 #430 Revision 3（2026-09-21）。本次是 Hub 合同实现，不表示 Plan #50 已激活或 Worker 候选已部署。

## 边界

- 计划显式绑定一个同项目的 orchestrator；不写死小策、项目或 Flow ID。
- `start` 在同一个事务中将 draft 转为 active，创建唯一 `plan-supervisor:{plan_id}` 监督工作项及启动回执。此时不自动创建 Run，也不凭任务 assigned 状态推断已开始执行。
- 工作项与长期 Agent Goal 试点独立，不依赖 `/agent-goals` 是否开放。最多关联一个 Mission，通过其 dispatch 关联 Run。
- Hub 程序负责持久化、定时、租约和事件；Agent 只在被唤醒后作有限决策。Hub 没有任何模型调用。
- Plan/Mission 关联不是 Flow 授权，原有 ACL、Worker 绑定、Mission 预算和 Team 经理任期检查继续生效。普通非计划 Mission 保持原行为。
- 计划内新派工应通过关联 Mission。未绑定的历史 Run、普通聊天自由执行和其他既有入口不会凭名称自动归入计划，也不能因此宣称已受监督。

## 启用与发布

1. 备份并执行 `ops/migrations/20260921_plan_supervision.sql`，再发布 Hub。
2. 显式设置 `PLAN_SUPERVISION_ENABLED=1` 和 `PLAN_SUPERVISION_TEAM_IDS=1` 可只开放团队 #1；默认关闭、空名单拒绝启动。`*` 仅用于明确需要全局/独立计划的环境，不用于本次生产灰度。
3. 保持既有 `TIMEOUT_WATCHER_ENABLED=1`。watchdog 约每 60 秒检查 DB，不运行模型；作为到点计时和漏事件/断线补偿。
4. 开放团队能力不等于激活计划。Worker 实现 claim/heartbeat/decision 后，在独立测试计划进行并发、重启、SSE 丢失与跨周期验收，再由 Owner 决定是否激活 Plan #50、小策 #54。

团队 GET/list 和 Sidecar `agent_teams[].plan_supervision` 下发能力与合同入口。限定范围内的 Plan 持久绑定 `team_id`，只能由同项目的当前主经理监督；关联 Mission 也必须属于该团队。团队暂停、换经理或移出启用名单后旧监督租约不能再写入。现有 Flow 权限不变。

本迁移只建三张表，不修改 Plan #50，不批准 Release，不部署 Worker，不启动任何业务 Flow。回滚应用保留表与审计。功能关闭后，已有标记为计划管理的 Mission 禁止新 dispatch；在途 Run 不取消。

## API

根路径：`/api/v1/test-plans/{plan_id}/supervision`。以下均使用既有受控 Hub 身份，不在请求文本中放真实凭据。

| 接口 | 调用者 | 用途 |
| --- | --- | --- |
| GET 根路径 | 同项目成员/管理员 | 回读监督状态、关联 Run 和事件分页 |
| POST `/start` | 计划管理者 | 显式绑定 Agent，原子启动工作项 |
| POST `/claim` | 绑定 Agent | 领取一次唤醒对应的唯一监督 Turn |
| POST `/heartbeat` | 持有效租约的绑定 Agent | 续租，不产生模型调用或 Owner 通知 |
| POST `/decision` | 持有效租约的绑定 Agent | 确认游标、安排下次检查或人工阻断 |
| POST `/bind-mission` | 持有效租约的绑定 Agent | 接管自身、同项目的既有 Mission |
| POST `/stop`、`/resume` | 计划管理者 | 停止或恢复监督，不取消 Run；Agent stop 需有效租约，resume 须登录的人类管理者确认 |
| GET `/receipts/{receipt_id}` | 同项目成员/管理员 | 独立回读持久调度回执 |

开始示例（ID 必须来自实际计划配置）：

```json
{"command_key":"plan-start-1","team_id":1,"orchestrator_claw_id":54}
```

`start` 返回 `receipt_id`、`scheduled`、`supervision.work_item_id` 等信息。未来开始日期的计划先保存 next_check_at，到当天才产生唤醒。每个计划只有一个监督工作项，重复原请求返回同一回执；更换 command_key 或绑定身份不能悄悄新建第二个工作项。

## 唤醒、租约与游标

唤醒使用持久 `ClawMessage(msg_type=plan_supervision)` outbox，content 为 `hub.plan_supervision.v1` JSON。SSE 仅提醒拉取，丢失 SSE 不丢工作项。

Worker 处理这类消息时先 GET 根路径核实自身身份、现有 Mission/Run，再 claim。不可让普通聊天 done 表示计划已安排。

```json
{"command_key":"wake-123-claim","worker_id":"instance-54","wake_message_id":123}
```

返回 `fencing_token`、`lease_cursor`、`lease_expires_at`。租约 180 秒；Worker 程序建议每 60 秒续租，单 Turn 最长 10 分钟。重复 claim 原请求返回同一历史回执及当前 `lease_valid`，不能把过期回执当作新授权。另一个进程/worker_id 不能并行 claim。

事件页默认从 acknowledged_cursor 开始，最多 200 条。沿 `next_cursor` 调用 `GET ?after=...` 直到 `has_more=false`。leased 状态的事件窗口固定在领取时的 lease_cursor；本轮期间新事件留给下一轮，不被本次 decision 吞掉。

事件顺序由锁定监督行后的 sequence 决定，而非数据库自增 ID 大小，因此较小 ID 的迟提交事务不会被游标跳过。

## Mission 接线

创建新 Mission 仍调用既有 `POST /workflow-missions`，额外提供：

```json
{
  "test_plan_id":50,
  "team_id":1,
  "plan_supervision":{"worker_id":"instance-54","fencing_token":1}
}
```

其余项目、主 Agent、objective、预算、Flow/Worker 白名单字段沿用原合同。创建与计划关联在同一事务提交；计划已经有 Mission 时返回 `PLAN_MISSION_ALREADY_BOUND`，Worker 必须回读复用，不另建平行链。

关联 Mission 的每次 `/dispatch` 也必须提供 `plan_supervision`，并沿用稳定 decision_key/idempotency_key。旧 fence、过期 lease、计划未开始/已到期/停止均拒绝新派工。租约不扩大执行权限。`/bind-mission` 额外带 command_key、mission_id、worker_id、fencing_token；只能绑定相同主 Agent 和项目，且 Mission 不得已属于其他计划。

Agent 对关联 Mission 的 complete/cancel，以及对已监督 Plan 的修改，同样检查当前监督租约，防止旧 Turn 在失去租约后结束新链路。登录的计划管理者保留人工停止/收口能力。

## “明早继续”必须取得回执

```json
{
  "command_key":"wake-123-decision",
  "worker_id":"instance-54",
  "fencing_token":1,
  "cursor":8,
  "outcome":"wait",
  "summary":"当前 Run 继续执行，明早复核；异常事件可提前唤醒",
  "next_check_at":"2026-09-22T09:00:00+08:00",
  "resume_condition":"timer_or_event"
}
```

next_check_at 必须带时区、在未来且早于计划结束日次日零点（北京时间）。只有成功 response 且独立 GET 回读 receipt_id 后，才可说“已安排”。回执描述历史接受结果；后来 stop/到期会取消该安排，恢复时必须读取当前状态。

成功 decision 原子确认本轮游标、保存最近决策和 next_check_at、关闭唤醒消息并释放租约。有未消费的关键事件时可以提前再次唤醒；普通进度合并并限频 60 秒。`outcome=blocked` 表示需人工决策，不附 next_check_at，等待管理者 resume。

同 action + command_key 的同内容请求返回原回执；不同内容拒绝。普通 `messages/{id}/done` 在没有 decision 回执时返回 `PLAN_DECISION_RECEIPT_REQUIRED`，避免聊天承诺假成功。

## 事件与补漏

- TestTask 状态/分配/进度、Mission 创建/状态、Run 创建/节点/终态及 Step 实质进度变化，在源事务中写持久事件。
- 正常 API 事务提交后立即合并事件并创建唯一唤醒；事务回滚不投递。
- 当前归属执行者的 heartbeat/progress 生命周期冲突、heartbeat claim 拒绝会记录异常事件。无权限调用者不能借拒绝请求制造监督唤醒。
- watchdog 回读关联任务和 Run，补计时、缺失源事件、SSE 丢失；初版按 10 分钟实质进度未变、3 分钟无 heartbeat 产生诊断事件，不自动终止 Run或判业务失败。
- 重复相同业务进度但更新时间/heartbeat 不能不断刷新实质进度基线。watchdog 只产生状态变化或到期事件，未变化不调用模型。
- 监督租约过期或唤醒长期未被领取最多恢复三次，随后转 blocked，防止无限占用模型。Owner 可在监督 API 回读阻断并决定 resume。
- 计划到期自动清除 next_check_at、使监督租约失效、停止唤醒与关联 Mission 新派工，保留在途 Run 和结果。计划到期不等于业务已完成。

## Worker 待实现 / 验收边界

本次未改 Worker。Worker 须识别 plan_supervision 控制消息、持久化 wake/lease/receipt、恢复时回读、在 decision 后立即释放模型槽，并在 Hub fencing 拒绝时停止旧监督写入。不得用模型循环轮询模拟 watchdog。

普通心跳、无变化检查不发送 Owner 通知；计划监督消息不进入旧的“消息卡住五分钟”泛化告警。首版不新增 Owner 主动推送链路，阻断/决策通过回执和监督 API 回读；每日/最终收口通知与报告仍须后续按受控通知合同接线，不能宣称已全链路验收。

本地专项测试覆盖：重复启动/唯一工作项、唯一租约与 fencing、未来开始/定时唤醒、重启回读、事件在途不丢、游标分页、聊天假完成拒绝、Mission 真派工与幂等、三次过期停止、计划到期和权限边界。生产 MariaDB 多进程竞争、真实 Worker 重启及跨日 Todo 候选仍需独立 canary。
