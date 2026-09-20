---
name: agent-team-member-activity
description: 向 Hub Agent 团队报告自己的空闲、工作、阻塞状态和当前任务进度，回读历史任务；处理事件幂等、版本冲突及重启对账。用于成员卡片状态更新，不代替 Workflow 结果、AgentTask heartbeat 或执行授权。
---

# Agent 团队成员动态上报

版本：1.0.0。适用于能调用 Hub 受控 HTTP 工具的 Agent，不限 Codex/Hermes/CodeBuddy，不要求 has_worker_runtime。

## 准备

使用已授权的 `hub_api` 或部署方配置的受控客户端，所有路径前缀 `/api/v1`。认证由运行时注入当前 Agent 身份；不读取、索取、打印 Token，不借管理员账号上报，不绕过宿主网络策略。

从可信配置确定 claw_id/project_id，分页 `GET /agent-teams?project_id={project_id}&limit=100&offset=0` 确認自身所属团队。多团队必须显式关联任务所属 team_id，不能向所有团队广播同一任务。只有当前团队成员可报自己的状态，用户身份（含超级管理员）不能代报。

先读：

- `GET /agent-teams/{team_id}/members/activity`：成员卡片及 report_contract。
- `GET /agent-teams/{team_id}/members/{claw_id}/activity?limit=20&offset=0`：member.version、current_task、history 和 recent_reports。历史分页最大 100。

首次版本为 0；已有任务先核对，不直接覆盖成 idle。

## 上报请求

`POST /agent-teams/{team_id}/members/{claw_id}/activity`

以下是结构示例，标识和进度必须用本次真实事实，禁止原样抄用：

```json
{
  "event_id":"session-a:run-741:1",
  "expected_version":0,
  "state":"working",
  "summary":"正在执行功能发现",
  "task":{
    "task_key":"flow-36:run-741:attempt-1",
    "title":"执行 Flow #36 功能发现",
    "task_type":"flow",
    "reference":"Flow #36 / Run #741",
    "status":"working",
    "progress_percent":null,
    "progress_message":"已同步源码，正在分析登录模块"
  }
}
```

- state：idle / working / blocked。working、blocked 必须附 task，task.status 与 state 相同。
- task.status：working / blocked / completed / failed / cancelled。结束时 state=idle，并附同一任务完整终态对象；不能只报 idle 隐去未完成任务。
- task_type：flow / bug_regression / code_analysis / other。回归 Bug 可用 reference="Bug #123"，不要伪造不存在的 Flow/Run。
- 单次执行 task_key 固定，一个成员在一个团队最多一个当前任务；换任务先明确结束旧任务。结束任务不可再改，新尝试使用新 key。
- title/task_type/reference 创建后不可更改；进度细节放 progress_message。progress_percent 为 0–100 整数或 null，未知用 null，不能按时间推测或为成功强填 100。
- event_id/task_key 为 1–96 位 ASCII 字母、数字或 `._:-`；title 1–240 字符，reference ≤240，summary ≤500，progress_message ≤4000，规范化 JSON ≤12000 字节。
- 不上传凭据、原始完整日志、Prompt 或敏感业务原文。用简短事实和受控证据标识；reference 只是纯文本。

任务开始、阻塞、恢复、结束时立即上报。成功后保存回执版本，后续真实观察用新 event_id 和最新版本。

无当前任务的空闲报告示例：

```json
{"event_id":"session-a:idle:1","expected_version":3,"state":"idle","summary":"等待分配"}
```

## 幂等与恢复规则

1. 网络结果不确定：原 event_id、原请求体完整重试，不新建任务。Hub 返回 replayed=true 时是原回执，不刷新最后活跃时间；旧回执版本不得降低本地已知版本。
2. TEAM_ACTIVITY_EVENT_CONFLICT：同 ID 内容不一致，停止并检查持久化记录；不改内容强行覆盖。
3. TEAM_ACTIVITY_VERSION_CONFLICT：GET 最新状态并确认本进程是否仍负责该任务，不能只把 expected_version 改大后重放旧状态。
4. 重启先对账 Hub 当前任务和本地实际执行/正式任务状态。确认仍在执行才续报；证实中断后显式报 failed/cancelled。事实不足时保留旧状态并说明待对账，不编造完成或空闲。
5. 401/403 或成员移除：停止重试并报告身份/成员问题；不切换其他身份。团队暂停时可以报告已在途任务，但不能因此派发新任务。

## 可靠性与显示边界

建议 60 秒一次新的存活观察，180 秒无有效新报告标为过期，首次接入前为“尚未上报”。相同事件重放不算新观察。

Skill 只能按需上报；周期计时、任务生命周期自动挂钩、持久 outbox、有界退避重试和重启对账需要 Worker 实现，不能用模型每分钟轮询。若已有 Worker 自动上报，Skill 先回读并走同一受控上报队列，不开第二写入者竞争版本。

团队状态是观察信息：不代替 Worker SSE/heartbeat、AgentTask claim/fencing、Flow 终态、证据审核或资源租约。idle 不代表全局有执行容量，completed 也不等于业务验收通过。

上报后 GET 回读，向用户说明 team_id/claw_id、state、task_key、version、最后上报时间；未成功持久化不要宣称页面已更新。
