# Agent 运转协议 (agent-operating-protocol)

## 简介

本 Skill 是 **每个 OpenClaw / Hermes Agent 的任务生命周期协议**——规定任务到达后读什么、怎么决策、何时记录经验、写哪一层。

> **为什么需要它**：三层记忆（本地 / Memos / Hub Knowledge）和几十个 Skill 已经存在，但缺少**强制执行的运转顺序**，导致 Agent 各自为战、重复踩坑、owner 反复提醒后仍遗忘。
>
> **与 Rule #15 的关系**：Rule #15 定义「写什么层」；`knowledge-manager` 定义「调哪个 API」；**本 Skill 定义「每个任务何时触发上述规则」**。

**触发词**：开始任务、执行待办、收到 Todo、处理消息、任务结束、记录经验、owner 纠正、踩坑了、运转协议、AOP

**必装**：所有接入 Hub 的 OpenClaw 应安装本 Skill；收到 Todo / 用户任务时**第一个加载本 Skill**。

---

## 一、四段式任务生命周期（不可跳过）

```
任务到达 → ① 分类预检 → ② 加载上下文 → ③ 执行护栏 → ④ 收尾记录
```

### ① 分类预检（≤30 秒）

**任何任务入口**（Todo、用户对话、SSE 消息、heartbeat 拉到的待办）必须先回答：

| 问题 | 怎么判断 |
|------|----------|
| 任务类型？ | hub / tapd / wecom / 需求 / 用例 / 工程 / 报告 / 其他 |
| 所属项目？ | claw.project_name，默认 RacingGO |
| 是新建还是延续？ | 用户说「上次」「继续」→ 先 session_search |

**Hub 已自动附加统一任务上下文包 `task_context`**（无需手查，记忆路由"推送"到你面前）：

| 字段 | 含义 |
|------|------|
| `required_skills` | 本任务必须先加载的 Skill 列表（含 `basic-operations-preflight`） |
| `preflight_checklist` | 执行前必须逐条确认的通用铁律（5 条） |
| `top_pitfalls` | 命中的高相关历史踩坑（title + solution） |
| `references` | 相关经验/资料引用 |
| `operating_protocol_skill` | 固定 `agent-operating-protocol` |

**从哪读 `task_context`**（按优先级，只读一处，不要重复拉 Knowledge 抄一遍）：

1. Todo / AgentTask / workflow step 的 SSE 推送 / `GET` 返回里已内嵌（sidecar v2.1+ 会拼进 prompt）；
2. 若手上没有，主动拉：`GET /api/v1/tasks/{ref_type}/{ref_id}/context`，`ref_type ∈ {todo, agent_task, workflow_step}`。

**你必须做的第一件事**：读出 `task_context`，用 **一句话**确认「已加载 {required_skills}，已确认 {N} 条前置铁律，已知 {M} 条历史踩坑」——**不要逐条复述全文**（节省 token）。旧字段 `pitfall_notice` 仍兼容保留，等价于 `top_pitfalls` 摘要。

---

### ② 加载上下文（固定顺序）

按顺序加载，**不要跳步，不要一次扫全部 Skill**：

| 顺序 | 来源 | 动作 |
|------|------|------|
| 0 | `task_context.required_skills` | **先加载这些 Skill**（含通用壳 `basic-operations-preflight`），再动手 |
| 1 | 公共踩坑 | 读 `task_context.top_pitfalls`（已推送）；不足再 `GET /knowledge?category=pitfall&project={项目}&search={关键词}` |
| 2 | 项目 Rules | `GET /rules?project={项目}` 扫与任务相关的条目 |
| 3 | 主 Skill | 加载 `primary_skill`（见下表），只加载 **1 个**主 Skill |
| 4 | Memos 碎片 | `GET /memos/search?tag=openclaw/pitfall&keyword={关键词}` |
| 5 | 个体 MEMORY | 仅作索引（KEY → Skill/Knowledge 位置），不当主存储 |

**任务类型 → 主 Skill 映射**：

| 关键词 / 任务 | 主 Skill |
|---------------|----------|
| Hub API、openclaws、secrets | `hub-connect` |
| TAPD、缺陷、需求单 | `tapd-integration` |
| 企业微信、WeCom、发消息 | `hub-sse-sidecar` |
| 需求、iWiki、用户故事 | `requirement-analysis` |
| 用例、testcase | `testcase-manager` |
| 工程、全景、panorama、commit | `game-module-panorama` |
| 知识、经验、踩坑沉淀 | `knowledge-manager` |
| 待办、日报 | `todo-manager` |
| 课题、讨论 | `topic-discuss` |
| 评审 | `case-review` |
| 测试报告 | `test-report-manager` |
| 无法分类 | **本 Skill**（agent-operating-protocol） |

---

### ③ 执行护栏（防止「又忘了」）

| 触发条件 | 强制动作 | 禁止 |
|----------|----------|------|
| **任何写操作前** | 逐条对照 `task_context.preflight_checklist` 打勾（见 `basic-operations-preflight` 提交前 Gate） | 未过 Gate 直接写库/发消息/改配置 |
| 调外部 API 前 | 对照 `top_pitfalls` 输出将调用的 URL/参数 | 盲调后等 404 再查 |
| API 第 1 次失败 | 查 `top_pitfalls` + Memos，再重试 1 次 | 连续换 3 种写法盲试 |
| 同一操作第 2 次失败 | **停止**，输出「已查经验：…」，请求 owner 或换路径 | 继续试错消耗 token |
| **写操作完成后** | `POST /api/v1/ops/verify`（DB 重读比对预期），有 mismatch 立即修正 | 只凭返回码就声称成功 |
| owner 纠正 / 提醒 | **当场** `POST /memos/upsert tag=pitfall` 记录 | 只写 MEMORY 不共享 |
| 找到 workaround 并验证 | 任务结束前登记经验草稿 | 只说「搞定了」不留记录 |
| 用户说「记下来」 | 立即 `build_experience_draft` → 确认后入库 | 推迟到「以后」 |

---

### ④ 收尾记录（任务结束时必跑决策卡）

任务完成前，逐项自检：

```
□ owner 今天纠正过我？        → 立刻 Memos pitfall（tag=pitfall）
□ 同一坑第 2 次？            → Memos → 评估升 Hub Knowledge
□ 新 workaround 且已验证？    → POST /knowledge category=pitfall（draft）
□ 稳定流程可复用？            → Skill patch 或申请共享 Skill
□ 仅本次上下文？              → 本地，不写 Hub
□ workaround 对应 Bug 已修复？ → 更新 status=fixed，停止强提示
```

**写什么 → 写哪层**（Rule #15 精简版）：

| 内容 | 层 | API |
|------|-----|-----|
| 会话延续、临时排查 | 本地 | 不调 Hub |
| 首次踩坑、未验证观察 | Memos | `POST /memos/upsert tag=pitfall` |
| ≥2 次 / 多 Claw / 需检索 | MySQL | `POST /knowledge category=pitfall` |
| 可执行工作流 | Skill | `skill_manage patch` |

详细 API 参数见 `knowledge-manager` Skill §A/B/C。

---

## 二、经验记录决策树

```
发生了什么？
├─ owner 纠正 → 立即 Memos（不等验证）
├─ 工具/API 失败 + 找到解法
│   ├─ 第 1 次 → Memos pitfall
│   └─ 第 2 次或他 Claw 也踩 → Knowledge pitfall + 通知 owner
├─ 业务流程跑通且可复用 → Skill 候选（见 knowledge-manager §C 升级条件）
└─ 一次性业务背景 → 本地或不记
```

**owner 纠正的标准记录格式**（Memos）：

```markdown
## 现象
{owner 指出的错误行为}

## 纠正
{owner 要求的正确做法}

## 场景
{什么任务类型下会再犯}

## 标签
pitfall, owner-correction, {module}
```

---

## 三、与 Hub Todo 的协作

拉取待办 `GET /api/v1/todos` 或 `GET /api/v1/openclaws/{id}/todos` 时，每条 Todo 内嵌 `task_context`（同 §① 表），并保留以下兼容字段：

| 字段 | 含义 |
|------|------|
| `required_skills` | 必装 Skill（含 `basic-operations-preflight`） |
| `preflight_checklist` | 执行前铁律（5 条） |
| `top_pitfalls` | 命中的高相关踩坑（title+solution） |
| `references` | 相关经验引用 |
| `pitfall_notice` | 旧字段，等价 `top_pitfalls` 摘要（兼容） |
| `primary_skill` / `trigger_terms` / `matched_pitfall_ids` | 兼容保留 |
| `operating_protocol_skill` | 固定为 `agent-operating-protocol` |

> workflow 节点任务的 `task_context` 在 AgentTask `payload.task_context` 内；也可 `GET /api/v1/tasks/workflow_step/{step_id}/context`。

**执行 Todo 的标准开场白**（模板，等价"提交前 Gate"起手）：

```
收到待办「{title}」。已加载 {required_skills}；已确认 {N} 条前置铁律；已知 {M} 条历史踩坑。开始执行，写操作后将调 /ops/verify 复核。
```

---

## 四、禁止事项（违反 = 重复踩坑）

1. ❌ 跳过预检直接调 API
2. ❌ 未过 preflight Gate（`task_context.preflight_checklist`）直接写库/发消息/改配置
3. ❌ 写操作后不做 `/ops/verify` 复核，只凭返回码声称成功
4. ❌ owner 纠正只写 MEMORY 不进 Memos/Knowledge
5. ❌ 临时草稿直接 `POST /knowledge`
6. ❌ SOP 长期只放 Memos 不升级
7. ❌ 一次加载 10 个 Skill 而不是 1 个主 Skill
8. ❌ 声称「无法发图」前未查资产台账（WeCom 场景）

---

## 五、每日收尾自检（5 问）

1. 今天被 owner 纠正的，都记 Memos 了吗？
2. 今天第 2 次出现的坑，提交 Knowledge draft 了吗？
3. 今天执行的 Todo，开场确认 `task_context`（required_skills / 铁律 / top_pitfalls）了吗？
4. 今天的写操作，都过了 preflight Gate 并 `/ops/verify` 复核了吗？
5. MEMORY 里有没有应「毕业」到 Skill/Knowledge 的条目？有没有把仅自用上下文误推到共享层？

---

## 六、关联 Skill

| Skill | 关系 |
|-------|------|
| `knowledge-manager` | 经验写哪层、怎么升级 |
| `todo-manager` | Todo 调度与上报 |
| `hub-sse-sidecar` | 收 Todo/消息入口 |
| `topic-discuss` | 课题讨论结论沉淀 |

---

## 七、群体记忆

公共踩坑登记簿数据源：`knowledge/seeds/racinggo-pitfalls.initial.json`（Hub 导入后走 Knowledge API）。

课题讨论 #36：https://clawteam.woa.com:18800/topics/36

实施计划：`docs/superpowers/plans/2026-06-07-agent-collective-memory-skillopt.md`
