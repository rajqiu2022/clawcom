"""Hub 能力索引（capability index）。

用途
    sidecar 在每次给 Agent LLM 构造 prompt 前（聊天回复 / 待办）自动注入这段
    精简索引，等价于"每次回复前先查一次 Rule #19"，但不依赖 Agent 自觉，也
    不增加网络往返（随 /sidecar-config 每 60s 刷新一起下发）。

单一事实来源
    本模块的 DIGEST 是 Rule #19「Hub 系统核心认知警醒规则」的精简速查版。
    Rule #19 正文第「零」节应与此保持一致；改这里就等于改注入内容。

设计约束（来自 docs/superpowers/specs/2026-07-09-agent-memory-routing-design.md）
    - 保持极简：只放"功能域 → 主 Skill"路由与必查顺序，完整 API 语义留在各
      专项 Skill，避免稀释注意力（#41 教训：AGENTS.md 越长越稀释）。
    - 对未升级的旧 sidecar 无破坏：多返回一个字段，旧代码忽略即可。
"""

HUB_CAPABILITY_VERSION = 4

HUB_CAPABILITY_DIGEST = """【Hub 能力索引 · 回复/执行前必读（Rule #19 速查）】
不确定某功能在 Hub 哪里，先按下表定位对应 Skill 再动手；禁止凭记忆猜接口/字段，枚举与 ID 一律查 API options。
- 任务/待办/消息/生命周期 → agent-operating-protocol、todo-manager、manager-hub、hub-sse-sidecar-v2
- 通用操作前置校验（5 铁律 / preflight Gate / 执行后 /ops/verify 落库校验）→ basic-operations-preflight
- 任务上下文 / 记忆路由（GET /api/v1/tasks/{ref_type}/{ref_id}/context）→ agent-operating-protocol
- 持久笔记（跨 session 保留，随本索引回注）：关键决策写 POST /api/v1/memos/upsert（tag=decision，scope_key=项目/主题）；当前任务上下文写 tag=taskctx、scope_key=todo-{id}/agent_task-{id}（同 scope_key 会滚动更新，处理完成后 Hub 自动归档）。回复前若「你的持久笔记索引」有相关项，先 GET /api/v1/memos/memo/{id} 取全文再作答，勿凭记忆臆测。
- 工作流四态编排（节点拆分 / 执行者变量 / 节点参考 / 阻断续跑）→ workflow-manager
- 主 Agent 自主调度 Mission：GET /api/v1/workflow-missions/{id}/definitions 查询项目内可用 Flow；POST /api/v1/workflow-missions/{id}/dispatch 只用于首次启动或切换到不同 Flow。Child Run blocked/failed 且修复后仍要执行同一 Flow 时，优先 POST /api/v1/workflow-runs/{run_id}/restart 原地完整重启，禁止重复 dispatch 制造新 Run；两类写入都需稳定幂等键，禁止传 executor/worker 覆盖字段。
- 接入注册 / Token / 拉标准包与增量同步 → hub-connect；Skill 市场增删改查 → skill-market-operations
- 知识库·踩坑沉淀 → knowledge-manager；课题讨论 → topic-discuss；见闻分享 → insight-sharing
- 测试报告（6 类 / 类别自定义 / 收藏 / 隐藏报告同项目按 ID 可见 / 附件 / 分享外链）→ test-report-manager
- 测试计划·迭代·任务 → testplan-manager；用例库 → testcase-manager；用例快照 → snapshot-manager；用例评审 → case-review
- 需求分析 → requirement-analysis；工程分析 → engineering-analysis；功能模块全景 → game-module-panorama
- 项目/模块 → project-manager；测试账号池 → test-account-manager；密钥箱 → secrets-vault；考试中心 → exam-manager；Agent 模板 → agent-template-manager；日报 → report-viewer
API 前缀：SSE/消息=/api/openclaws/；配置待办日报=/api/v1/openclaws/；公共资源=/api/v1/。
完整语义见对应 Skill 与 Rule #19（已固化到本地 AGENTS.md）。"""


def build_hub_capability_digest():
    """返回注入用的 Hub 能力索引摘要（纯字符串，无 DB 查询，热路径安全）。"""
    return HUB_CAPABILITY_DIGEST
