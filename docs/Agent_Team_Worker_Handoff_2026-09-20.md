## 2026-09-20：团队 Skill 发布与 Worker 可靠上报接线

### Hub 已提供与发布范围

Hub 当前发布版本 `97ea71a`，提供团队角色、经理任期、Mission 计划/派发、成员卡片及动态 API。此次仅发布 Skill 与登记 Worker 开发任务，不代表 Worker 接线已完成；不自动安装 Skill、不修改 Worker、不启动 Run。

{{PUBLISHED_SKILLS}}

身份：一支团队一名在任测试经理（可有备用），多名代码分析员、测试执行员；执行员细分 editor/mobile_package/client_performance。团队角色不修改 Claw.role/Profile，不授予 Flow 权限，不代替可信 Runtime 或执行槽。成员自报适用于全部 Provider；正式 Mission dispatch 仍检查可信 Runtime 和实际 ACL。

### Worker P0：公共层可靠成员动态上报

1. 在跨平台公共执行生命周期层接入，不在 Codex/Hermes/CodeBuddy Prompt 分别拼逻辑；Linux、Windows 使用同一合同。普通 AgentTask、Flow 节点、代码分析、Bug 回归仅上报本机实际负责的任务。
2. 沿用本机受控 Hub 客户端注入自己的 Claw 身份，分页发现所属团队；显式 task→team 绑定，不把同一任务自动广播到所有团队，不硬编码项目/Claw/Flow ID。未配置团队时保持旧功能。
3. GET `/api/v1/agent-teams/{team_id}/members/{claw_id}/activity` 取得 member.version、当前任务及历史；POST 同路径。事件字段为 event_id、expected_version、state、summary、task。任务含 task_key/title/task_type/reference/status/progress_percent/progress_message。
4. 任务开始/阻塞/恢复/结束立即上报，公共后台计时器建议每 60 秒提交一次真实存活观察，不调用模型。state=idle/working/blocked；任务终态 completed/failed/cancelled 搭配 idle。一个成员在一团队只能有一当前任务；并发任务必须在调度边界串行或明确选取代表任务，不能互相覆盖。
5. 持久 outbox：发送前保存 event_id、完整原始请求体、expected_version、task_key、执行实例身份；按 (team_id, claw_id) 单写队列提交。网络超时/断连/5xx 有界指数退避加抖动，重发原请求，不改事件 ID；新观察使用新事件。离线时限制队列增长，不能丢失开始/终态顺序；若合并尚未发送的进度，须保证版本与幂等一致，不能合并已发送的不确定事件。
6. 相同事件重放只返回原回执、不会刷新时间；旧回执不可回退本地版本。TEAM_ACTIVITY_VERSION_CONFLICT 回读并对账所有权，不能换版本硬重放旧状态；TEAM_ACTIVITY_EVENT_CONFLICT 停止并留诊断。401/403、成员移除或项目不符停止重试并提示，不换身份或扩权。
7. Worker 重启先恢复 outbox，再结合 Hub 正式任务状态、本地进程与执行账本对账；确认任务存在才续报，否则有依据地报 failed/cancelled。未取证不得直接报 idle/completed。重复进程应通过本机锁/实例身份防止双写；此状态接口的 expected_version 不是业务 claim fencing。
8. 只报短摘要及证据引用，不报认证信息、完整日志、原始 Prompt。未知百分比用 null；immutable title/type/reference 不变；重跑用新 task_key。event_id/task_key ≤96，规范化 JSON ≤12000 字节。
9. 自报失败不得改变正式业务终态、跳过业务 heartbeat、释放资源或伪造任务完成；卡片“空闲”不作为跨团队容量证明。执行器死亡不得由孤立定时器继续报告 working 存活。
10. 暴露脱敏诊断：待发送数量、最后成功时间/版本、上报延迟、冲突与停止原因。能通过配置关闭上报并保留待对账记录，回退不影响既有任务路径。

### Worker P1：经理任期、阶段和异步监管

- manager-lease 独立后台续期，ttl 30–300 秒，建议 120 秒；每次启动唯一 manager_session_id，持久 epoch，过期/配置变化立即停止经理写操作。主备不能同任；是否自动接管需另行授权配置，不由 Skill 自行决定。
- 经理创建 Mission→team-plan→dispatch，持久 mission_key/decision_key/stage_key，响应不确定先回读，禁止换 key 创建重复 Run。创建后登记 watcher 并立即释放模型槽，不用企微/对话会话长轮询。
- 按 Stage context 接 claim/version/fencing、Artifact/Review/Handoff；未接入则如实阻断。成员动态终态不能代替 Stage 完成或独立验收。资源停止凭据按原合同处理，不能把 TTL 到期当作已停止。
- P0 卡片自报不依赖 P1 全部完成；P1 属后续明确范围，不宣称当前系统已有全自动依赖 DAG、全局容量预留或 24/7 自治。

### 验收矩阵与交付

自动化测试至少覆盖：首次上报；working→blocked→working→completed；failed/cancelled；未知百分比；当前任务切换限制；同事件重试不重复历史且不刷新活跃；乱序旧回执不回退版本；版本冲突不覆盖新执行；断网与 5xx 后恢复；重启恢复 outbox；正式任务已终态时不复活；成员移除/401/403 停止；两个进程竞争；多团队隔离；终态不可改；敏感内容不进入报告；任务进程退出不持续伪报存活。

Windows/Linux 公共测试通过，并覆盖 Codex/Hermes/CodeBuddy 公共生命周期适配；某 Provider 未做实机验证须明确标为未验证。经授权的 canary 先用无副作用普通任务，确认卡片、进度、历史、180 秒过期提示及恢复；本次发布不自行启动生产验收。

交付 Worker commit/Release、开关与回退方式、专项测试结果、各平台实测证据及尚未接入项，再按授权逐台升级。Hub 无需为各 Provider 分别增加团队动态接口。
