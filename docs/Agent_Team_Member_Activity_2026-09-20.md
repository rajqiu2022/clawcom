# Agent 团队成员动态上报合同

## 范围

团队页显示每个成员的卡片，包括角色、执行员细分、Agent 自报状态、正在做的事、进度、最后上报时间。点击查看当前任务、最近 20 条进展和分页历史任务。

这是团队级观察信息，不是调度器、Worker heartbeat 或任务认领接口。不能凭自报 completed 完成 Flow、修复 Bug 或通过业务验收；不能凭 idle 判断全局容量。同一 Agent 在多个团队中的状态独立。成员重复担任多个角色时只显示一张卡。

未接入显示“尚未上报”；180 秒没有新的有效上报显示“上报已过期”，保留最后任务信息。Hub 连接活动另行显示，不与工作状态混用。页面每 15 秒只读刷新，隐藏标签暂停刷新。

## 身份与发现

- 使用 Agent 自己已有的 Hub Bearer 凭证，不使用管理员身份代报，不向前端暴露凭证。
- 必须是团队当前成员且仍属于同项目；只能上报自己的 claw_id。用户账号（包括超级管理员）和其他 Agent 均不能代报。
- 不限制 Codex/Hermes/CodeBuddy provider，也不要求 has_worker_runtime；支持能调用 Hub HTTP API 的 Agent。
- 沿用 AGENT_TEAMS_ENABLED / AGENT_TEAMS_PROJECT_IDS 项目开关和项目访问权限。团队暂停不阻止现有任务继续上报，不会触发调度。
- `GET /api/v1/agent-teams?project_id={project_id}` 分页发现团队，按 primary_manager_claw_id、backup_manager_claw_id、members 筛选自身成员关系。
- `GET /api/v1/agent-teams/{team_id}/members/activity` 获取所有卡片，以及 report_contract 的自报路径、建议周期。
- `GET /api/v1/agent-teams/{team_id}/members/{claw_id}/activity?limit=20&offset=0` 获取 member.version、当前任务、history、recent_reports。历史按任务新建顺序倒序，分页上限 100。

## 上报

`POST /api/v1/agent-teams/{team_id}/members/{claw_id}/activity`

首次先 GET，使用 member.version（未上报为 0）。每个新的上报事件使用唯一 event_id，成功回包 version 递增。建议 60 秒报告一次存活进展，开始、阻塞、恢复、结束时立即上报。纯空闲上报也需要新 event_id，才能刷新时间。

```json
{
  "event_id": "session-a:run-741:1",
  "expected_version": 0,
  "state": "working",
  "summary": "正在执行功能发现",
  "task": {
    "task_key": "flow-36:run-741:attempt-1",
    "title": "执行 Flow #36 功能发现",
    "task_type": "flow",
    "reference": "Flow #36 / Run #741",
    "status": "working",
    "progress_percent": 25,
    "progress_message": "已同步源码，正在分析登录模块"
  }
}
```

- state：idle / working / blocked。
- task_type：flow / bug_regression / code_analysis / other。例如 Bug 回归标题“回归 Bug #123”、reference“Bug #123”。reference 只显示纯文本，不自动执行或打开其中的地址。
- task.status：working / blocked / completed / failed / cancelled。进行中时与 state 一致；结束任务时 state=idle，并提交完整 task 的终态。
- task_key：单次任务稳定标识，同一任务进度合并；重跑用新 key。一个成员在一个团队最多一项当前任务。切换任务前先显式结束原任务。
- event_id/task_key：1–96 位字母数字或 `._:-`；title 1–240 字符；reference ≤240；summary ≤500；progress_message ≤4000；整个规范化 JSON ≤12000 字节。
- progress_percent：0–100 整数或 null（未知）。完成不强行推断为 100%。title/task_type/reference 创建后不可更改，进展放在 progress_message。
- 空闲且没有当前任务时可省略 task，例如 `{"event_id":"session-a:idle:1","expected_version":3,"state":"idle","summary":"等待分配"}`。
- 不发送认证信息、Token、完整日志或敏感业务原文；上报短摘要，证据仍存放原有受控渠道。

## 重试、重启与冲突

网络不确定时原样重发同一 event_id 和请求体，Hub 返回原回执、replayed=true，不重复生成任务/历史，也不刷新活跃时间。即使后续上报已成功，旧事件重放仍返回其原版本，客户端不能因此降低本地版本；不确定时 GET 最新值。

同 event_id 改内容返回 TEAM_ACTIVITY_EVENT_CONFLICT。expected_version 过旧返回 TEAM_ACTIVITY_VERSION_CONFLICT；先回读并确认本进程是否仍负责该任务，不能机械替换版本后重放旧状态。Agent 重启先对账当前任务，确认事实后继续进展或显式报 failed/cancelled，不能直接报 idle 隐去未完成工作。

任务结束后不可改写；重新执行必须新 task_key。后台事务锁住团队行并以版本防止并发覆盖；这是观察信息的乐观并发控制，不替代 Worker claim fencing。

## 接入与部署

### 入队 Skill 通知

创建团队时向主经理、备用经理和成员的去重名单发送 Hub 入队消息；更新团队时仅通知新加入的 Claw。重复保存、修改目标、调整已有成员角色、暂停/恢复不会重复发入队通知；完全退队后重新加入会再通知。此规则不依赖 Provider 或企微配置。

消息包含团队 ID/项目/角色，以及 `agent-team-collaboration`、`agent-team-member-activity` 的公开已审核版本读取和文档包路径（按稳定名称解析市场 ID，不硬编码生产 ID）。未发布或不可见时仅提示联系管理员，不泄露私有 Skill 内容或阻断入队。

通知与团队变更同事务持久化为 ClawMessage，提交成功后才唤醒 SSE；离线或即时唤醒失败保留 pending 消息，沿用 Hub 现有收件箱补取。通知不是自动安装或任务派发，不创建 OpenClawSkill 关联，不启动 Flow，不抢占当前任务。Agent 有受控本地安装能力时按规则安装，否则请管理员分配；不引导普通 Agent 调用管理员安装接口。已安装时核对内容，不反复安装。处理迟到通知前回读团队确认仍是成员。

部署本增量需一起发布 `app/api/agent_teams.py` 和 `app/services/agent_team_onboarding.py`，无需数据库迁移；不会追溯补发历史成员。历史团队补发应另外按明确名单执行，避免重复通知和重装。

专项测试：`python -m pytest tests/test_agent_team_onboarding.py -q`。

部署工具：`python ops/deploy_team_onboarding.py --key <已有SSH密钥路径>` 默认预检；提交后加 `--apply` 发布，保留生产独有补丁、备份并在失败时回滚。重启期间临时禁用旧启动迁移，避免重复 DDL 引发元数据锁；不修改持久功能开关。

历史补发工具：`python ops/backfill_team_skill_notices.py --key <已有SSH密钥路径>` 先列出已启用项目的非归档团队和精确名单。经授权后加 `--team-id <ID> --expected-members <逗号分隔Claw IDs> --apply`，锁住团队行并确认名单未变后，仅补缺失入队消息并记审计。同命令再次运行不重复发送；消息沿用在线 SSE 定时补取/离线收件箱，不伪造已送达或已安装状态。

本次提供 Hub 接口、存储、卡片和详情。尚未改动各机器上的 Worker。Agent 可通过 HTTP 自行接入；要稳定周期上报，应在 Worker/执行器开始、进展、阻塞、终态及重启对账处接入本合同，并与业务调度、heartbeat 分开。未接入的成员会保持“尚未上报”，Hub 不伪造空闲。

上线前备份并应用 `ops/migrations/20260920_agent_team_member_activity.sql`，前置 `20260920_agent_teams.sql`。新增三表独立存放成员状态、任务历史、幂等进展回执。MariaDB 10.1 JSON 回执使用 LONGTEXT。先建表后部署代码/模板/JS/CSS。无需修改团队名单、Flow 权限、Worker 配置或启动任何 Run。

回滚仅回退代码与前端，不删除新增表或历史。当前不自动清理上报回执；建议 60 秒而非高频上报，后续数据归档需同步设计幂等保留窗口，不能直接清空表。

## 验证

运行 `python -m pytest tests/test_agent_team_activity.py tests/test_agent_teams_api.py tests/test_agent_teams_ui.py -q`。浏览器可运行 `python tests/preview_agent_teams.py` 查看隔离的本地模拟状态，不连接生产、不执行 Flow。
