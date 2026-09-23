# Agent 团队首批实现与接入合同

依据：知识库 #455 revision 1，以及用户确认的固定团队角色方案。2026-09-24 增加项目助理角色。
本批包含 Hub 后端基础与团队管理页面，不宣称已完成常驻自治、真实手机验收或生产部署。

## 一、角色边界

| 一级角色 | 标识 | 人数 | 二级角色 |
| --- | --- | --- | --- |
| 测试经理 | `test_manager` | 一名在任；可配一名备用 | 无 |
| 项目助理 | `project_assistant` | 多名 | 无 |
| 代码分析员 | `code_analyst` | 多名 | 无 |
| 测试执行员 | `test_executor` | 多名 | `editor` 编辑器、`mobile_package` 手机包、`client_performance` 客户端性能 |

项目助理协助测试经理收集版本/构建数据、整理计划、成员状态、阻断和待办；可接受经理派发并执行团队已授权 Flow，但不获得经理的调度、验收或团队配置权限。
一个执行员可具备多个二级角色；一个 Claw 可加入多个团队，亦可兼任不同团队岗位。
新角色仅保存在 `agent_teams/agent_team_members`，不修改 Claw.role、AgentPost、Profile、现有 Flow ACL。
经理在主备字段配置，不能通过 members 塞入第二个经理。主备只有一份有效 lease，按 Hub 时间判定。
角色与运行能力分离。2026-09-21 起，管理员勾选的 Flow 自动授予经理调度、项目助理/代码分析员/测试执行员执行权限，不授予编辑权限，也不伪造 Worker Runtime。详见 `agent_team_flow_permissions.md`。

## 二、本批已实现的行为

- Team 配置与乐观版本校验，项目管理员操作；新功能默认关闭并显式限定灰度项目。
- Mission 增量关联 Team，冻结角色/策略快照；普通 Mission 不查询新增 Team 表，保持既有路径。
- Team Mission 队列是现有 Mission 列表的投影，不再引入 GoalTodo 的第二套领取状态机。
- Manager lease 使用数据库行锁、epoch、会话 ID，30–300 秒 TTL（默认 120 秒）。续期不调用模型。
- 只有当前经理和当前会话可建 Mission、写计划、dispatch、完成/取消 Team Mission。过期任期不能复活；主备接管不重写已有 Run。
- 计划首次写入 Stage，计算内容 hash；相同内容幂等，不同内容不能原地覆盖。新基线需要新 Mission。
- dispatch 必须提供 stage_key，执行 Agent 从阶段绑定解析；仍检查可信 Runtime 和有效 Flow 权限（手工 ACL 或当前团队授权）。
- 角色绑定同时满足 Mission 快照和团队当前成员关系；移除的角色不能继续启动新 Run。
- Stage 与 Run 同事务关联，每阶段仅启动一次；相同 decision_key 重试回读同一个 Run。经理任期不进入决策 hash，接管后可回读原决策，但旧任期不能操作。
- Team 的 Handoff 接收按目标 Stage 指定 Agent + 团队角色判断，不使用 Claw.role/AgentPost。
- 完成 Mission 要求全部 Stage 完成；Stage 沿用现有 Artifact 验证与管理员 review 门禁，不放宽证据要求。
- 更新配置会撤销经理任期。暂停只禁止新计划/派发，不取消已有 Run；旧 Stage/Artifact 仍可回写。恢复后经理重新申请任期再完成 Mission。
- 团队不自行开启破坏性操作/外部通知。独立复核策略将在下一批明确授权和版本化。

首期不提供自动 failover 调度器：备用经理可在 lease 过期后申请接管，由 Worker 接入或管理员组织执行；Hub 不通过定时调用模型维持 lease。

## 三、API 示例（占位 ID，不能原样用于生产）

所有路径前缀 `/api/v1`，沿用 Hub 的登录/Agent Bearer 鉴权，不在本文保存任何凭据。

管理员 `POST /agent-teams`：

```json
{
  "project_id": 101,
  "name": "手游修复验证团队",
  "objective": "基于明确修复和构建基线完成回归",
  "primary_manager_claw_id": 201,
  "backup_manager_claw_id": 202,
  "members": [
    {"claw_id": 206, "role_key": "project_assistant"},
    {"claw_id": 203, "role_key": "code_analyst"},
    {"claw_id": 204, "role_key": "test_executor", "specialties": ["editor"]},
    {"claw_id": 205, "role_key": "test_executor", "specialties": ["mobile_package", "client_performance"]}
  ],
  "policy": {"allowed_definition_ids": [301], "max_child_runs": 5}
}
```

`GET /agent-teams?project_id=101` 列表；`GET /agent-teams/roles?project_id=101` 获取固定角色字典。
`PUT /agent-teams/{id}` 提交完整配置及 `expected_version`，项目不可改变。
配置只允许已实现字段，未知字段拒绝，不静默接受尚未生效的预算、资源或评审配置。

经理 `POST /agent-teams/{id}/manager-lease`：

```json
{"expected_epoch": 0, "manager_session_id": "worker-start-session-uuid", "ttl_seconds": 120}
```

首次成功返回 epoch=1；续期使用当前 epoch 和同一个进程会话 ID。Worker 每次启动应生成并持久化唯一会话身份，不与另一个进程共享；响应丢失先读 Team 当前 epoch，再用原会话 ID 续期。
主备切换或配置变更后，旧 session/epoch 失效。Hub 不回传 session ID，它不是 Hub API Token，也不能代替身份鉴权。

经理 `POST /workflow-missions`：

```json
{
  "team_id": 401,
  "project_id": 101,
  "objective": "验证修复 Bug X 的手机构建",
  "mission_key": "bug-X-build-Y-verification-1",
  "manager_epoch": 1,
  "manager_session_id": "worker-start-session-uuid",
  "allowed_definition_ids": [301],
  "allowed_worker_claw_ids": [205],
  "context": {"baseline": {"fix_commit": "COMMIT", "build_sha256": "DIGEST"}}
}
```

Flow 列表必须属于团队策略且创建者具备执行/编辑权限。Worker 白名单显式设置，不自动加入全部团队成员；白名单 Worker 必须已注册可信 Runtime。`mission_key` 沿用既有唯一约束：不确定创建结果时按 Team Mission 列表回读，勿另造 key 重复创建。

经理 `POST /workflow-missions/{id}/team-plan`：

```json
{
  "manager_epoch": 1,
  "manager_session_id": "worker-start-session-uuid",
  "stages": [{
    "stage_key": "verify-mobile",
    "role_key": "test_executor",
    "specialty": "mobile_package",
    "assigned_claw_id": 205,
    "input_snapshot": {"build_sha256": "DIGEST", "acceptance_criteria": ["修复用例与邻近回归均有证据"]}
  }]
}
```

经理 `POST /workflow-missions/{id}/dispatch`：

```json
{
  "manager_epoch": 1,
  "manager_session_id": "worker-start-session-uuid",
  "stage_key": "verify-mobile",
  "workflow_definition_id": 301,
  "decision_key": "verify-mobile-once",
  "start_vars": {}
}
```

省略 worker_claw_id 时从 Stage 解析；显式传入必须匹配。stage_key 纳入幂等 hash；不能换 decision_key 绕过阶段单次派发。
后续沿用 `/missions/{id}/stages/...`、Artifact、Handoff 合同。此时 Worker 仍需主动接 Stage/产物合同，本批不会伪造其已接入。
`GET /agent-teams/{id}/missions` 用于团队工作队列/看板，默认每页 20 项、最多 100 项，支持 `limit/offset`，返回总数和阶段摘要。

### 团队管理页面

- 入口：侧栏“Agent 团队”及自动化闭环页，地址 `/agent-teams?project_id=<项目ID>`。
- 同一项目支持创建多支团队；每支团队单独配置主经理、可选备用经理、多名分析员和多名执行员及其二级角色。
- 项目管理员可以创建、编辑、暂停、恢复和归档；普通项目成员只读。页面与后端双重校验，不通过隐藏按钮替代 API 权限。
- `GET /agent-teams/options?project_id=...` 仅返回同项目可用 Agent 的最小字段与当前调用者可见的 active Flow，不返回凭据或 Sidecar 配置；团队列表支持 `limit/offset` 分页。
- 展示团队策略、经理任期快照、Mission 队列和 Stage/Run 关联。任期快照不等同于实时在线状态；页面不轮询、不申请经理任期、不代创建 Run。
- 保存携带 `expected_version`；版本冲突保留表单并提示刷新，不静默覆盖。切换项目/团队时忽略旧请求结果。
- 管理员选定的 Flow 自动提供团队调度/执行授权；实际派发仍校验当前团队范围、阶段绑定和 Runtime。撤回团队授权不会改动独立的手工 ACL。
- 沿用 Hub 明暗主题，配置弹窗有独立滚动区域及窄屏布局；文本按纯文本安全转义。
- 本地浏览器验收使用 `python tests/preview_agent_teams.py`，仅监听 `127.0.0.1:18891`、内存数据库及模拟登录。该工具不可部署为服务；结束后停止本地进程即可。

## 四、资源到期与停止核实

受保护资源组到期后整组变为 `quarantined`，保留 `active_slot` 唯一占用位；不允许 renew 或普通 release。
保护条件：开启 `RESOURCE_LEASE_RECONCILIATION_ENABLED`，或以可信 Team Run 为 owner，或调用者主动要求 `metadata.requires_stop_confirmation=true`。
保护在 acquire 时固化到租约 metadata，关闭开关不撤销已发租约的停止确认要求；既有 quarantined 租约也不能被新 acquire 夺走。
Team Worker 必须以 `owner_type=workflow_run`、真实 Run ID 占用资源；本批未将任意自定义 owner 归并到 Team。

- active 受保护组正常释放：原 holder 或现有管理员提交 `stopped=true` 和 `stop_receipt`（整组停止回执/证据引用），审计记录 `stop_reported`。
- TTL 到期的隔离组：首期仅超级管理员人工核实后，`POST /resource-leases/{id}/reconcile`，提交同样字段；审计 `stop_reconciled` 后整组释放。
- Hub 不会把普通 Agent 的“我停了”当作可信的隔离恢复证明；Worker 的实例/epoch 绑定停止回执和自动核实留待后续接线。
- 等待响应 `waiting_stop_confirmation`、`retryable=false`、`retry_after_seconds=null`；应等待核实事件，不占模型槽循环重试。
- 旧普通 Flow 租约在全局保护开关关闭时仍保留旧 TTL 行为，不擅自全量改动生产任务。
- 测试号继续使用原测试号租约 API，本批未实现跨设备+测试号 API 的统一原子组租约。

## 五、迁移、灰度与回退

1. 先备份数据库及此次发布文件，保持所有新开关关闭。
2. 核实实际表结构；按依赖顺序执行 migration（`CREATE TABLE IF NOT EXISTS` 不负责修复已有旧表缺列）：
   - `20260804_workflow_operation_idempotency.sql`
   - `20260828_agent_artifacts.sql`
   - `20260828_mission_stages.sql`
   - `20260828_mission_handoffs.sql`
   - `20260920_agent_teams.sql`
3. Worker delegation 的上一版列迁移必须已完成。新表 JSON 字段以 LONGTEXT 兼容 MariaDB 10.1。
4. 发布 models、API 注册文件、Team/Mission/Stage/Handoff/Artifact API 及其服务依赖、资源租约 API/服务、config，以及 views、团队模板/CSS/JS、侧栏和自动化闭环入口。不得只更新 models 就宣称合同 API 已上线。
5. 先做只读路由/表结构检查，再开启 `AGENT_TEAM_CONTRACTS_ENABLED=1`、`AGENT_TEAMS_ENABLED=1`、`AGENT_TEAMS_PROJECT_IDS=<明确灰度项目ID>`；空项目列表不会启用任何团队。
6. 新开关只由部署配置读取，Agent 无法通过 API 开启。不开全局资源保护也会保护按 Run ID 占用的 Team 资源；全局开关应在旧 Worker 支持停止回执后另行灰度。
7. 运行测试和明确授权的 canary。未选定实际经理、执行员和 Flow 前，不自动创建生产团队/Run。
8. 回退先暂停团队、排空/核实在途任务与隔离资源，再关闭 Team 开关；保留新表和审计。不能直接退回会把隔离资源误判为空闲的旧租约代码。

部署工具：`python ops/deploy_agent_teams.py --key <既有SSH密钥路径>` 默认只预检；提交后加 `--apply` 才执行发布。工具保留线上独有配置和路由补丁，先在隔离目录做真实应用导入验证，备份文件及数据库结构，再进行增量迁移、页面/API 冒烟和文件切换。校验失败自动恢复本批文件，已创建的新表保留。不会写入功能开关、创建团队或启动 Run；项目灰度范围须另行确认。

## 六、后续批次（不在本批完成范围）

1. Worker 接 Team lease / Stage 合同、可靠 outbox、停止凭据；DeepFlow 提供构建含修复校验与真实设备证据；单团队人工触发完整验收。
2. 版本化独立/自动 review_policy，审查者与产出者隔离；本批仍沿用管理员 review。
3. 真实 Claw 并发槽和跨团队共享账号配额的原子预留/结算，不能用角色人数替代执行容量。
4. 多源事件收件箱、revision 去重、合并窗口、条件唤醒和依赖 DAG；本批阶段列表并不自动建立先后依赖。
5. 自动故障接管调度器、团队运行指标/事件看板、Skill/Profile 版本固定、持续运行验收。

上述能力必须分别验证后再称为 24/7 自动化闭环；本批仅建立可测试的身份、配置、计划与派发安全底座。

## 七、本地验证记录

- 后端合同及 UI 回归：102 passed，6 subtests passed；覆盖 Team/Mission/Stage/Handoff/资源隔离、旧 Mission 路径、Worker 身份及编辑 ACL。
- 11 个相关生产 Python 文件通过 Python 3.7 语法解析，团队 JavaScript 通过 `node --check`，`git diff --check` 通过。
- 本地 Chrome 内存数据库验证：同项目多团队、创建含两名不同细分执行员的团队、编辑版本冲突保留输入、标题纯文本转义、只读选项隐藏修改入口。
- 桌面与 420px 窄屏检查：窄屏默认收起侧栏、弹窗内部可滚动；文档宽度 414px / scrollWidth 414px，无横向溢出。页面沿用 Hub 明暗主题。
- 浏览器与预览进程已关闭；未创建生产 Run、未提交部署、未改动 Worker。真实 MariaDB 并发及 Worker canary 尚待灰度验收。
