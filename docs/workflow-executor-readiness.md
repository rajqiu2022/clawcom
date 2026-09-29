# Workflow 执行授权与 Worker 就绪

Flow 的编辑 ACL 和执行 ACL 是唯一的人工执行授权。编辑者也可启动 Run；执行者只能启动 Run，不能修改 Definition。Hub 下发给 Worker 的可启动 Flow 列表直接从实时 ACL（含团队授权）生成；历史 Sidecar 策略里的同名列表不再作为第二道白名单。给 Agent 授权时 Hub 仍更新配置版本，以唤醒 Worker 对账。

授权不等于物理机已经安装所需运行组件。含 `deepflow_runner` 节点的 Flow 在派发前仍需确认目标 Worker 的实际 `agent_direct` 运行模式、受管 Worker Release、本机 DeepFlow Release/Runner、健康探针及资源租约。运行时不兼容应报告为环境准备问题，不得描述为 ACL 或能力所有权问题；不能通过手工篡改 `runtime_mode`、移交别人的能力记录或跳过探针来假装就绪。

同一项目的同名自动化能力允许多个 Worker 分别发布。唯一身份是 `(project_id, capability_key, producer_claw_id)`；每台 Worker 只能更新自己的状态、版本、健康租约和验证收据。Hub 的候选资格可以汇总健康的提供者，具体 Run 必须使用目标 Worker 的能力与本机环境，不得用小牛的健康记录证明小窗就绪。

上线顺序：先执行 `ops/migrations/20260929_automation_capability_multi_producer.sql`，再部署 Hub API，最后部署包含按 `producer_claw_id` 对账的 Worker。对旧版 `legacy_split` Worker，需一次性安装兼容的 `agent_direct` Worker 和本机 DeepFlow Runner，并由 Worker 自行上报真实运行状态。小窗保留现有 CodeBuddy Provider；除非 Owner 另行决定，不将其擅自迁移到 Codex。其后赋予 Flow 权限不再要求能力移交或额外人工授权。

验收以小窗 #52 为例：Flow #12/#25 均可由 #52 发起；当 #52 被选为物理执行者时，Run 冻结绑定、AgentTask 目标、claim、heartbeat、Runner 收据均为 #52。另一个 Worker 的发布、降级或租约过期不能覆盖 #52 的能力记录。未安装本机 Runner 时只报告环境待就绪，不创建会误执行的 Run。
