---
name: agent-team-collaboration
description: 在 Hub Agent 团队中发现成员与角色，由测试经理持有效任期创建 Mission、冻结阶段计划、定向派发 Flow，并回读阶段、交接和证据。用于团队协作、经理调度、任务交接与团队队列查询；不用于修改团队配置或绕过 Flow 授权。
---

# Agent 团队协作与经理调度

版本：1.0.0。适用于 Hub 已启用 Agent Teams 的项目；不代表 Worker 已接完所有合同。

## 身份与边界

- 优先使用当前运行环境已授权的 `hub_api` 工具，路径前缀 `/api/v1`；由受控客户端注入自己的 Hub 身份。没有工具时由部署方配置客户端，不读取、索取、打印 Token，不用管理员身份代报或绕过本机出网限制。
- 从可信身份和 Hub 回读确认 project_id、claw_id、team_id；所有下例数字为演示 ID，必须替换，不照抄生产。
- 每个团队一名在任测试经理 `test_manager`（可有备用经理），多名代码分析员 `code_analyst`、测试执行员 `test_executor`。执行员细分为 `editor`、`mobile_package`、`client_performance`，以 API 当前返回为准。同项目可有多个团队，Claw 可加入多个团队。
- 团队角色独立于 Claw.role、Profile 和 AgentPost，不产生 Flow ACL、可信 Runtime 或执行容量。不能自行修改团队、白名单、系统开关或绑定其他 Worker。
- Skill 支持有界的按需调用；周期续租、可靠事件队列、重启恢复和异步 watcher 由 Worker 实现，不能用模型循环轮询替代。

## 1. 先读团队和当前工作

1. `GET /agent-teams?project_id={project_id}&limit=100&offset=0`，分页并确认自身成员关系；多团队时明确本次 team_id。
2. `GET /agent-teams/{team_id}` 与 `GET /agent-teams/roles?project_id={project_id}`，确认团队当前状态、角色、policy、经理 epoch。
3. `GET /agent-teams/{team_id}/missions?limit=20&offset=0` 查询已有目标和在途工作，避免重复创建。
4. `GET /agent-teams/{team_id}/members/activity` 查看自报工作；单成员详情用 `/agent-teams/{team_id}/members/{claw_id}/activity`。未上报或过期不当成空闲；团队内 idle 不代表跨团队有全局执行槽。
5. 核对 Flow Definition、启动变量、绑定执行员、ACL、运行能力和所需资源。Mission 派发仍要求目标 Worker 的可信 Runtime；不能用自报 working 伪造就绪。

## 2. 测试经理取得任期

仅团队配置的主/备经理按授权申请：

`POST /agent-teams/{team_id}/manager-lease`

```json
{"expected_epoch":0,"manager_session_id":"worker-start-session-uuid","ttl_seconds":120}
```

expected_epoch 必须来自最新回读。会话 ID 为本进程唯一身份；同进程续租保持同 ID，使用服务端当前 epoch。TTL 范围 30–300 秒。Worker 后台续租，无需调用模型；Skill 单次操作前检查任期，不能承诺离开会话后持续管理。

响应丢失先 GET Team，再用原 session 恢复确认；不得借用其他进程的 session。任期过期、配置变更或主备接管后旧 epoch 立即停止写入。备用经理只在允许接管时申请，不能双经理并发派发。

## 3. 创建 Mission、固定计划、派发

只在用户已授权的目标和范围内创建。先查已有 mission_key；网络结果不确定先回读，不生成新 key 重复创建。

`POST /workflow-missions`：

```json
{
  "team_id":401,"project_id":101,
  "objective":"验证指定修复构建","mission_key":"bug-X-build-Y-verification-1",
  "manager_epoch":1,"manager_session_id":"worker-start-session-uuid",
  "allowed_definition_ids":[301],"allowed_worker_claw_ids":[205],
  "context":{"baseline":{"fix_commit":"COMMIT","build_sha256":"DIGEST"}}
}
```

白名单必须是团队策略及实际权限允许的最小集合，不能自动加入所有成员或 Flow。

`POST /workflow-missions/{mission_id}/team-plan`：

```json
{
  "manager_epoch":1,"manager_session_id":"worker-start-session-uuid",
  "stages":[{
    "stage_key":"verify-mobile","role_key":"test_executor","specialty":"mobile_package",
    "assigned_claw_id":205,
    "input_snapshot":{"build_sha256":"DIGEST","acceptance_criteria":["修复用例及邻近回归均有证据"]}
  }]
}
```

成员需同时满足 Mission 快照和团队当前角色。计划相同内容可重放，不能原地换人或换基线；变化需要新 Mission。阶段列表不是自动依赖 DAG，前置验收和交接未完成不得提前派发。

`POST /workflow-missions/{mission_id}/dispatch`：

```json
{
  "manager_epoch":1,"manager_session_id":"worker-start-session-uuid",
  "stage_key":"verify-mobile","workflow_definition_id":301,
  "decision_key":"verify-mobile-once","start_vars":{}
}
```

start_vars 按实际 Definition 补齐，不照抄空对象。worker_claw_id 省略时从 Stage 解析，显式提供必须一致。相同 decision_key 重试回读原 Run；每阶段只能派发一次，不换 key 绕过。创建后回读 Run ID、Definition 快照和绑定执行员，向调用者返回。由已接入的 Worker watcher 继续跟踪；尚无 watcher 时明确需后续查询，不占模型槽轮询。

### 3.1 无需 Flow 的测试任务

测试计划里的独立任务不应为了派工强行包装成 Flow，也不能用 Todo 冒充执行任务。主测试经理
确认任务无 Flow 依赖后调用：

`POST /test-plans/{plan_id}/supervision/agent-tasks`

```json
{
  "command_key":"plan-{plan_id}-task-{task_id}-dispatch-v1",
  "test_task_id":228,
  "instruction":"执行任务并返回结构化结论、outputs 与 evidence",
  "retry_max":1
}
```

Hub 根据既有 Mission Stage 固定执行 Agent，创建带 claim/heartbeat/fencing/result 的普通
AgentTask；调用方不得覆盖执行者。该动作以团队主经理的持久任命为授权，不依赖当前计划监督
Turn 的短租约，因此其他 Stage 阻断时仍可派发无依赖任务。Todo 只作提醒，不能当领取或完成回执。

## 4. 阶段与交付证据

- 接手前 `GET /missions/{mission_id}/stages/{stage_key}/context`，另用 `GET /missions/{mission_id}/stages`、`GET /missions/{mission_id}/artifacts` 和 `GET /missions/{mission_id}/handoffs` 回读事实。
- Stage claim/transition 使用已安装的对应岗位 Skill 与当前合同。写入必须携带 expected_version、idempotency_key；transition 还需当前 fencing_token、reason_code、evidence_refs。旧 fencing、角色失效或版本冲突时停止并对账，不能盲改版本重试。
- Artifact 创建使用 Idempotency-Key，producer/hash 等由 Hub 生成；提交后管理员 Review 通过才可接受 Stage。不得自审、编造 evidence 或以成员动态 completed 代替业务验收。终态产物返工需新版本。
- Handoff 固定产物引用并经下游接受；需要交接的 Stage 在收到合法 Handoff 前不可 claim。不能把群消息中的“完成了”当交接证据。
- 尚未安装岗位 Skill 或执行器未实现合同，应报告具体能力缺口，不尝试猜测写请求结构。

## 5. 停止条件与结果

401/403、功能未启用、任期过期、白名单/能力不符：停止有副作用的调用并报告原因。版本冲突先读取真实状态，不扩大权限。

资源租约到期不是已停止证明。Team 资源以真实 workflow_run ID 为 owner；受保护资源须有停止回执才释放，quarantined 由授权管理员核实，不由 Agent 强行解锁。测试号沿用既有测试号租约接口。

每次输出 team/Mission/Stage/Run 标识、已验证事实、当前阻断、下一责任方；没有证据时标记未知。只有全部阶段及证据门禁通过才可申请完成 Mission。本 Skill 不启动未经授权的 Flow、不推群、不升级 Worker。
