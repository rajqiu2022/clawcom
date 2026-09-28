# Agent 直接协作模式：Hub 是团队事实账本

本模式适用于 Agent Team，先在 RacingGO Team #1 验证。Owner 或测试经理给出明确目标后，成员可在自身项目权限和设备能力范围内直接分析、执行与求助。TestTask、AgentTask、Mission Stage 和计划监督器用于排期、自动唤醒、租约及留痕；**普通工作不以先创建这些实例为前提**。需要执行正式 Workflow 时仍创建 Run，并遵守该 Run 的执行租约与结果合同。

Hub 负责保存团队成员、共享资源目录、消息、状态、任务、Run、报告和证据。它校验身份、项目权限、同一物理资源的互斥租约、写入幂等和审计；不替 Agent 判定业务原因，也不把一个计划或旧 Run 的阻断扩散到无依赖的工作。成员可用 `POST /api/v1/agent-teams/{team_id}/members/{claw_id}/activity` 在没有任务实例时上报 `working` 或 `blocked`，须提供具体 `summary`；已有自报任务则先结束该任务，避免状态串台。正式 Flow 的进度仍以 Run claim/heartbeat 为准。

测试经理应根据 Owner 目标及版本、需求、Bug、构建、设备和成员状态自主安排工作。向成员发出明确任务后，回读成员响应和实际执行证据；只创建任务或口头答应均不算开工。对于执行 Agent 的阻断，先让其在自身权限内检查和恢复；经理只处理跨成员、跨资源或授权范围外的问题。计划监督器只是一个观察与定时唤醒来源，`blocked_owner_gate` 仅影响对应计划的排期执行，不禁止成员继续其他授权工作。

Worker 接线要求：

1. 注入并执行 Hub `agent_teams[].collaboration_model` 与 `system_context.policy.team_collaboration_mode`。不要再把 TestTask、AgentTask、Mission Stage、Supervisor 有效租约解释为普通工作的通用门票。
2. Owner/经理明确指派的普通工作可直接在 Agent 会话中执行；需要可视化状态时先上报无任务实例的 `working`，随后定期更新，完成后上报 `idle`。Hub 断连时继续已授权的本地只读调查，保留待同步证据；不得声称 Hub 写入或正式 Run 已成功。
3. 经理自主循环须回读版本/任务/成员变化，选择行动并验证回执。不能因为当前没有 `ready_stages` 就只返回 `wait`；独立 Stage 和无计划任务仍可继续。对于未交付的工作，不得把“我已安排”当成“成员已执行”。
4. 对 Run 恢复调用 Hub recover API 获取实际准许动作。不要自行增加“必须找到历史签名 shutdown receipt”等全局门槛。旧执行缺回执时做当前状态对账，明确标记为**事后观察**，不能伪造历史回执。
5. 不得自动丢弃他人未提交改动、提交/推送 RacingGoUnity 工程、伪造设备/Unity/报告证据，或绕过同一资源的互斥租约。共享工作区 dirty 时优先使用独立工作区，若当前 Flow 仍固定路径则把它识别为 DeepFlow 接线缺口，不重复原地重试。

验收至少覆盖：Owner 直接请小魏做设备探测而无 TestTask；小魏在 Hub 留下真实工作状态、证据和结果；小策自动发现并持续跟进；一个 blocked Run 不影响小马代码分析和小安复核；需要 Flow 的工作产生真实 Run/claim/heartbeat；共享 dev2 有未提交改动时证据得到保留，Agent 不执行清理或无意义重复恢复。

本合同只改变团队协作默认行为。已有 Workflow 的冻结定义、实际资源锁和外部通知授权仍按各自合同执行；需要调整 DeepFlow 的固定工作区绑定时，发布新的不可变 Release 后再切换线上 Flow。
