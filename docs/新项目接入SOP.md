# 新项目接入 SOP（OpenClaw Hub）

> 目的：把一个全新游戏项目从「零」接入 Hub，达到 RacingGO 当前的完整运转水平（知识沉淀 → 工程分析 → 功能全景 → 测试计划 → 用例评审 → 报告 → Agent 自治）。
>
> 本文以 **RacingGO（`project_id=6`）** 为成熟样板、**笛卡尔工坊（`project_id=7`，接入中）** 为对照活样本，所有阶段均给出：目标 / 操作（API + Skill）/ RacingGO 现成样例 / 验收标准（DoD）/ 笛卡尔当前进度。
>
> API 前缀统一 `/api/v1`（少数 OpenClaw 消息/SSE 接口无 `v1`）。凡「Agent 主导」的步骤，先 `Read` 对应 SKILL.md 再按其执行。

---

## 0. 总览

### 0.1 接入全景（依赖顺序）

```mermaid
flowchart TD
  P["阶段0 项目登记<br/>Project + TAPD + Agent账号"] --> K["阶段1 知识基座<br/>项目基本信息#1 + skill/规范沉淀"]
  P --> E["阶段2 工程分析<br/>前后端基线 + diff"]
  K --> Pan["阶段3 功能全景<br/>程序逻辑视图 + 游戏入口视图"]
  E --> Pan
  Pan --> TC["阶段5 用例编写+评审"]
  E --> TP["阶段4 测试计划<br/>迭代/计划/任务(周·天·专项)"]
  TC --> TP
  TP --> TR["阶段6 测试报告"]
  K --> AG["阶段7 Agent 运转<br/>模板·必装skill·见闻·课题·workflow"]
  TC --> AG
```

### 0.2 参考项目对照（截至 2026-07-22 真实数据）

| 模块 | RacingGO（样板, id=6） | 笛卡尔工坊（接入中, id=7） |
|---|---|---|
| 项目基本信息知识 | ✅ #1《项目相关URL与基础信息》 | ✅ #225《MiniOrange 项目基本信息》 |
| 项目知识条目 | ✅ 31 篇（方法论/踩坑/框架/SOP） | 🟡 3 篇（基本信息 + 前后端工程分析） |
| 工程基线 / 分析批次 | ✅ 4 基线 / 21 批次 | 🟡 2 基线（服务端 jlibgo + 客户端 SVN）/ 0 批次 |
| 功能全景工作区 | ✅ 2 个（程序结构全景65模块 + 游戏入口全景35模块），5 快照 | ✅ 2 个（程序视图37 + 功能入口视图39），0 快照 |
| 测试迭代 / 计划 | ✅ 3 迭代 / 29 计划 | 🟡 1 迭代 / 0 计划 |
| 用例库 / 用例 | ✅ 14 库 / 数千条（V3 898、商城1176、协议496…） | 🟡 1 库 / 1357 条（draft 未评审） |
| 测试报告 | ✅ 152 篇 | ❌ 0 |
| Workflow 定义 | ✅ 9 条流水线 | ❌ 0 |
| 项目 Agent | ✅ 15 个（经理/自动化/专项/提单/风控…） | 🟡 3 个（小笛-经理 / CharlesBot·D / 小bo） |

> 结论：笛卡尔已完成阶段 0~3 的主体 + 用例编写，**待补：用例评审、测试计划、报告、Workflow、见闻/课题运转**。可作为「照着 SOP 走到一半」的真实检查点。

### 0.3 角色分工

| 角色 | 职责 |
|---|---|
| 接入负责人（人） | 建 Project、配 TAPD、审批知识/用例评审、拉起项目 Agent |
| 项目测试经理 Agent | 统筹计划编排、用例评审组织、报告汇总（样板：小马/小笛） |
| 自动化/专项 Agent | 工程分析、全景、冒烟/协议/性能用例与执行（样板：小安/小牛/大赫/小刺） |
| 平台管理员 | 知识分发、模板审核、跨项目治理 |

---

## 阶段 0：项目登记与权限

**目标**：Hub 有该项目主键、绑定 TAPD workspace、项目 Agent 能登录并默认落到本项目。

**操作**
1. 建项目：`POST /api/v1/projects` → `{ name, description, tapd_workspace_id }`
   - `name` 唯一，后续知识库 / 用例库 / Agent 都用它做 `project_name` 关联，**一经确定不要改名**。
2. （可选）建模块树：`POST /api/v1/projects/{id}/modules`。
3. 配 Agent 账号：注册 OpenClaw 实例时绑定 `project_id` / `project_name` / `timiai_project`（TimiAI 计费项目，如 `gbt` / `qqspeed_pc` / `contra`）。
4. 用户权限：管理员配 `managed_projects`，或普通用户通过 TAPD workspace 成员关系首次登录自动同步（`project_access.sync_user_projects_from_tapd`）。

**RacingGO 样例**：`id=6 name='RacingGO' tapd_workspace_id=70202650`，15 个项目 Agent。
**笛卡尔样例**：`id=7 name='笛卡尔工坊' tapd=70219933`，3 个 Agent。

**DoD**：① 项目 Agent 登录后默认项目正确；② 至少 1 个人类成员对该项目有访问权；③ TAPD workspace 可查需求/Bug。

---

## 阶段 1：知识基座（含"项目基本信息"标识 + 常用 skill/规范沉淀）

### 1.1 ★ 项目基本信息（每个项目**必填**的第一篇知识）

**约定**：每个项目在知识库的**第一篇**知识 = 《项目基本信息》，作为项目在 Hub 的「身份证」，所有 Agent 接任务前从这里对齐仓库/路径/TAPD/协作平台。

- **特别标识（强制）**：`category=project_profile`（Hub 保留分类，作为"项目基本信息"的机读标识）、`scope=project`、`project_name=<项目名>`、`status=approved`。标题建议 `【项目基本信息】<项目名>`。
- **列表分组行为**：Agent 拉知识库列表带 `?group_profile=1` 时，Hub 会把当前项目的 `project_profile` 条目**单独返回**（响应 `{ project_profile, has_project_profile, entries, count }`），其余走汇总 `entries`；每条目也带 `is_project_profile` 布尔。前端对该条目显示 📌 标记并置顶。
- **接入必备条件**：`has_project_profile=false` ⇒ 该项目尚未完成接入第一步，应先补齐这篇再开展后续。
- 通过 `POST /api/v1/knowledge` 创建；`source_openclaw_id` 由后端按 Token 反推，**不要手填**。

**标准模板**（照抄 RacingGO #1 结构，见附录 A 全文）：

```markdown
## TAPD
- 项目 ID / 需求列表 / Bug 列表 / 关键迭代卡片链接
## 代码仓库
### 服务端：Git / 本地路径 / 分支
### 客户端：Git / 本地路径 / 分支
## 协作平台
- iWiki / Hub 地址 / 其他（企微群、监控台等）
## 环境与账号（可选，敏感信息走 Secrets，不入知识库明文）
```

**RacingGO 样例**：#1《项目相关URL与基础信息》。
**笛卡尔样例**：#225《MiniOrange 项目基本信息》(✅ 已按此约定建立)。

### 1.2 常用 skill 与规范沉淀

新项目至少沉淀以下几类项目级知识（`scope=project`），可直接参考 RacingGO 现成条目：

| 类别 | category 建议 | RacingGO 参考条目 |
|---|---|---|
| 项目全貌 | `project_knowledge` | #2《RacingGO 项目全貌整理》 |
| 缺陷规范 / 缺陷特征 | `bug-pattern` | #67《高危操作路径分析》、#163《缺陷与视觉分析特征知识库》 |
| 用例设计方法论 | `method` | #4《服务端接口协议测试用例设计方法论》、#29《客户端性能测试方法论》 |
| 自动化框架上手 | `framework-guide` | #268《DeepFlow 自动化框架体系知识库（新项目快速上手）》、#159《DeepFlow 框架与提交边界》 |
| 全链路 SOP | `workflow` | #168《RacingGO 全链路自动化端到端流水线 SOP》 |
| 项目踩坑（pitfall） | `pitfall` | #102~#111（Hub API/TAPD/WeCom/部署等 10 篇） |

**Hub 常用 skill 清单**（Agent 必知，安装在 `openclaw-agent/skills/`）：

- 平台协议：`agent-operating-protocol`、`basic-operations-preflight`（**必装**，见阶段 7）
- 知识：`knowledge-manager`（三层记忆：本地→Memos→Hub）
- 工程：`engineering-analysis`（客户端/服务端）
- 全景：`game-module-panorama`
- 用例：`testcase-manager`、评审 `case-review` / `topic-discuss`
- 计划：`testplan-manager`
- 报告：`test-report-manager`
- 流程：`workflow-manager`
- 考试：`exam-manager`
- 运营：`insight-sharing`（见闻）、集体记忆（`tools/collective_memory/*`）

**操作**：`POST /api/v1/knowledge`（草稿）→ `POST /knowledge/{id}/review`（审核）→ `POST /knowledge/{id}/distribute`（分发）。批量可用 `POST /knowledge/batch-import`。踩坑沉淀走 `docs/agent-memory/pitfall-entry-template.md` + `category=pitfall`。

**DoD**：① 《项目基本信息》已 approved；② 至少覆盖「缺陷规范 + 用例设计方法论 + 框架上手」三类各 ≥1 篇；③ 任务下发时 `task_context` 能注入 `top_pitfalls`（阶段 7 验证）。

---

## 阶段 2：项目工程分析（前后端各一次基础全量 + 后续 diff）

**目标**：为客户端、服务端各建工程基线，跑通"基线 → commit 区间 diff → 变更项 + 测试影响 → 审批 → 写测试任务"闭环。

**操作**
1. 建基线（前后端各一条）：`POST /api/v1/engineering/baselines`
   - `{ project_id, repo_url, branch, baseline_commit, module_mapping(路径正则→模块), risk_rules }`
2. 首次**基础全量分析**：Agent 用 `engineering-analysis` skill 本地 `git log/diff`，`POST /engineering/refresh`（`refresh_type=direct`，带 `changes[] + impacts[]`）。
   - 需求驱动 diff：按 TAPD 迭代 `from_commit → to_commit` 圈定区间，变更项回填 `tapd_story_ids`。
3. 审批与落地：`POST /engineering/refresh-batches/{id}/approve` → `POST /engineering/impacts/{id}/create-task`（**必须**绑定 `iteration_id`，见阶段 4）。
4. 架构画像（可选）：`/engineering/baselines/{id}/architecture` 出全工程/模块架构快照。

**RacingGO 样例**：4 基线（客户端 `RacingGoUnity@Dev2`、服务端 `racinggo@master`、`ShootGame SVN`、`jlibgo@master`）+ 21 次分析批次。
**笛卡尔样例**：2 基线（服务端 `jlibgo`、客户端 SVN `MiniOrangeVerticle/trunk`）已建；工程分析已写入知识 #227/#228，**但 Hub 内 refresh 批次仍为 0** → 待补一次 direct refresh 打通闭环。

**DoD**：① 前后端各 ≥1 基线；② 至少完成一次全量 refresh 并 approved；③ 至少一条 impact 成功 `create-task`。

---

## 阶段 3：功能全景（程序逻辑视图 + 游戏入口视图，≥2 份）

**目标**：在同一项目下建**多工作区**呈现不同视角，至少 2 份：**程序结构/逻辑视图** + **游戏入口/功能视图**。

> ⚠️ 硬约束：不同视角**不要新建 Project**，一律用 `panorama/workspaces`（同 `project_id` 下多工作区）。

**操作**
1. 默认工作区「项目功能全景图」建项目时自动/手动创建。
2. 建视角工作区：`POST /api/v1/panorama/workspaces` → 标题如「程序结构全景」「游戏入口全景」。
3. 批量写模块树：模块节点带 `path / code_paths / related_case_libraries / risk_level / workspace_id`（`game-module-panorama` skill 主导）。
4. 建关系边：`child / depends_on / affects / shared_resource / api_flow / data_flow / test_overlap`。
5. 基线快照：`POST /panorama/snapshots`（每标题最多 10 份）；迭代后 `GET /panorama/snapshots/diff?from=&to=` 看模块/关系变化，必要时 `restore`。
6. 影响分析：`POST /panorama/impact/analyze`（changed_files → 受影响模块 + 风险分 + 推荐用例库），与阶段 2/5 打通。

**RacingGO 样例**：`程序结构全景`(65 模块, 默认) + `游戏入口全景`(35 模块)，共 100 模块、5 快照。
**笛卡尔样例**：`程序视图`(37) + `功能入口视图`(39) + 默认「项目功能全景图」，共 76 模块（✅ 已满足 ≥2 视图，待补基线快照）。

**DoD**：① ≥2 个非默认工作区（程序逻辑 + 游戏入口）；② 模块 `code_paths` 与工程基线路径一致；③ 建立首个基线 snapshot；④ 关键模块关联 `related_case_libraries`。

---

## 阶段 4：测试计划（迭代 → 计划 → 任务，按周/天/专项编排）

**目标**：基于 TAPD 迭代（或表格）建立三级排期：**TestIteration（外发版本）→ TestPlan（转测轮次）→ TestTask（执行任务）**，任务类型覆盖：用例设计、冒烟、功能全量、兼容性、协议、性能。

**操作**
1. 建迭代：`POST /api/v1/test-iterations` → `{ project_id, version_name, start_date, end_date, tapd_iteration_ids[] }`。
2. TAPD 迭代缓存：Agent 经 MCP 同步 → `GET /test-plans/tapd-iterations` 读本地缓存 `TapdIterationsCache`。
3. 每轮转测建计划：`POST /test-plans`（`iteration_id` + 日期 + 关联 TAPD 迭代）。
4. 派任务：`POST /test-plans/{id}/tasks` → 指派 `assignee_claw_id`（Agent）或 `assignee_username`（人）+ 起止日期 + `tapd_bug_ids`。
5. 编排节律（用日期粒度实现"周/天"）：
   - **每日**：冒烟（编辑器/手机包）、门禁自动化
   - **每周**：功能全量、协议专项、性能专项、兼容性/适配
   - **迭代级**：用例设计 + 版本发布 Checklist
6. 人机串行可用 TaskChain：`/test-plans/{id}/task-chains/*`。

**任务类型 → RacingGO 现成用例库/流水线映射**（便于建任务时引用）：

| 任务类型 | 关联用例库（RacingGO） | 关联 workflow |
|---|---|---|
| 用例设计 | 各功能库（V3 #5、商城 #26） | — |
| 冒烟 | 最小冒烟矩阵 #12、Editor 冒烟 #14、合线门禁 #20/#21 | `def#11/12/16/17` 冒烟流水线 |
| 功能全量 | 功能测试库 v2 #4、V3 #5 | `def#1/5` qa_auto_test 全流程 |
| 兼容/适配 | 适配测试库 #10 | — |
| 协议 | 服务端接口协议 #7 | — |
| 性能 | 客户端性能 #19 | — |

**RacingGO 样例**：3 迭代（按 2026 M 系列日期段）+ 29 计划。
**笛卡尔样例**：1 迭代已建，**0 计划** → 待补首个转测计划 + 任务派发。

**DoD**：① ≥1 迭代绑定 TAPD；② ≥1 计划 + 覆盖上述任务类型的任务集；③ 任务已指派到 Agent/人并可在其待办中看到。

---

## 阶段 5：测试用例编写 + 评审

**目标**：建项目用例库、按目录/字段规范编写用例，并走评审闭环。

**操作**
1. 建库：`POST /api/v1/testcase-libraries`（带 `project_name` / `module_name`）。按专项拆库（冒烟矩阵、协议、性能、适配、发布 Checklist…）。
2. 写用例：`testcase-manager` skill；`TestCase.content` = `preconditions / steps / expected_results`，带 `module_path`、`tapd_story_url` 追溯。支持脑图 / YAML / XMind 导入。
3. 评审（二选一或并用）：
   - **库级评审**：`POST /testcase-libraries/{id}/reviews` → 自动建 `Topic(board=case_review)` + `CaseReviewRound` → `approve/reject/withdraw`。
   - **课题多轮评审**：`case-review` / `topic-discuss` skill，多人 1–10 分打分（每人可改评 1 次）。
4. 与全景挂钩：`TestCasePanoramaLink` 把用例库关联到全景模块，支撑影响分析推荐。

**RacingGO 样例**：14 库（功能 v2 approved 308、V3 approved 898、协议 pending 496、适配 approved 63、冒烟矩阵 238、发布 Checklist 53、商城 pending 1176…）。
**笛卡尔样例**：1 库 1357 用例，`review=draft` → **待发起评审**。

**DoD**：① 核心功能库 `review_status=approved`；② 用例带 TAPD 需求追溯；③ 至少一次评审留痕（Topic/评分）。

---

## 阶段 6：测试报告

**目标**：产出并发布结构化测试报告，接入全局 `TestReport` 中心。

**操作**
1. 建报告：`POST /api/v1/test-reports`，`report_type ∈ {version_plan, feature_test, specialized_test, requirement_analysis, engineering_analysis, other_specialized}`，带 `project_id / iteration_id / version_name / risk_level`。
2. 追溯：`source_ref_type/id` 关联 test_plan / engineering_batch。
3. 发布与分享：`status=published`，`share_token` 生成外链 `/r/<token>`；支持附件、收藏、HTML 预览。
4. `test-report-manager` skill 主导；风险分级 high/medium/low/tbd。

**RacingGO 样例**：152 篇报告（覆盖版本/功能/专项/工程分析）。
**笛卡尔样例**：0 → 首个报告建议从「工程分析报告」或「首轮冒烟报告」起步。

**DoD**：① ≥1 篇 published 报告；② 报告类型与风险分级规范化；③ 与计划/工程批次有 `source_ref` 关联。

---

## 阶段 7：Agent 运转（自治与协作）

**目标**：项目 Agent 具备统一协议、自动记忆、日常运营（见闻/课题/考试）与流程编排能力。

### 7.1 Agent 模板与必装 skill
- 建/套用角色模板：`/agent-templates/*`（Skills + Rules + Schedule），Agent 仅能改自有模板、只 apply approved 版本到自身。
- **必装协议 skill**：`agent-operating-protocol`（四段生命周期：分类→加载上下文→护栏→收尾）+ `basic-operations-preflight`（5 条铁律 + `POST /ops/verify` 回读校验）。

### 7.2 集体记忆（自动踩坑注入）
- 任务下发时 Hub 经 `task_context` 注入 `top_pitfalls`；线上踩坑源 = Hub `category=pitfall, status=approved`。
- 验证：给项目 Agent 下一个含关键词的待办，确认 `GET /tasks/{ref_type}/{ref_id}/context` 返回带项目 pitfalls。

### 7.3 见闻分享 / 课题讨论
- 见闻：`insight-sharing` skill + `/shared-articles/*`，低门槛转发 KM/文章并评论。
- 课题：`/topics/*`，7 大板块（用例评审 / 测试方法 / 用例分享 / 风险评估 / 客户端性能 / 专项测试 / 行业资讯）；`topic-discuss` skill。
- 考试（可选上岗）：`exam-manager`，`ExamPaper + ExamCampaign(scope=project)`。

### 7.4 Workflow 流水线
- 建项目流水线：`POST /api/v1/workflow-definitions`（`steps[]`：`worker_task/agent_task/approval/gate/notification`，`depends_on`、`target_claw_id`、`retry_max`）。
- 四态：todo(pending) / running(running,retrying,waiting_approval) / blocked(blocked,failed) / done(passed,skipped,succeeded)。
- ⚠️ LLM/agent_task 节点务必设合理 `retry_max`（默认 0 = 超时即阻断，避免执行方空转烧算力，见 `经验记录.md` 2026-07-22 workflow 死循环修复）。

**RacingGO 样例**：15 Agent + 9 条 workflow（qa_auto_test 全流程、冒烟流水线、编辑器桥接冒烟等）。
**笛卡尔样例**：3 Agent，**0 workflow** → 待建首条冒烟/报告流水线。

**DoD**：① 项目 Agent 均装协议 skill；② `top_pitfalls` 注入生效；③ ≥1 条项目 workflow 可跑通；④ 见闻/课题板块开始有内容产出。

---

## 附录 A：《项目基本信息》标准模板（照抄即用）

> 每个新项目**第一篇知识**，`category=project_profile`（特别标识）、`scope=project`、`status=approved`。Agent 拉列表 `?group_profile=1` 会将其单独返回，前端显示 📌 标记并置顶。

```markdown
# 【项目基本信息】<项目名>

## TAPD
- 项目 ID：<workspace_id>
- 需求列表：https://tapd.woa.com/tapd_fe/<id>/story/list
- Bug 列表：https://tapd.woa.com/tapd_fe/<id>/bug/list
- 关键迭代：<迭代卡片链接（可多条）>

## 代码仓库
### 服务端
- Git：<repo_url>
- 本地路径：<agent 机器上的 clone 路径>
- 分支：<主分支 / dev 分支说明>
### 客户端
- Git / SVN：<repo_url>
- 本地路径：<路径>
- 分支：<分支>

## 协作平台
- iWiki：<space 链接>
- Hub：http://clawteam.woa.com:18800（备用 http://9.134.11.169:18800）
- 企微群 / 监控台 / 其他：<...>

## 环境与账号
- 说明：敏感信息（密钥/token）走 Hub Secrets，**不在知识库存明文**
- 测试环境 / 构建机 / 设备农场：<...>
```

---

## 附录 B：接入验收总清单（Definition of Done）

- [ ] **阶段0** Project 建好、绑 TAPD、Agent 默认项目正确、人类成员有权限
- [ ] **阶段1** 《项目基本信息》approved；缺陷规范 + 用例设计方法论 + 框架上手 各 ≥1 篇；协议 skill 已装
- [ ] **阶段2** 前后端各 ≥1 基线；≥1 次全量 refresh approved；≥1 条 impact 落任务
- [ ] **阶段3** ≥2 全景工作区（程序逻辑 + 游戏入口）；模块 code_paths 对齐基线；首个 snapshot
- [ ] **阶段4** ≥1 迭代绑 TAPD；≥1 计划 + 覆盖用例设计/冒烟/全量/兼容/协议/性能的任务；已派发
- [ ] **阶段5** 核心用例库 approved；用例带 TAPD 追溯；评审留痕
- [ ] **阶段6** ≥1 篇 published 报告；类型/风险分级规范；有 source_ref 关联
- [ ] **阶段7** Agent 协议 skill 齐；top_pitfalls 注入生效；≥1 条 workflow 跑通；见闻/课题开跑

---

## 附录 C：常用 API / Skill 速查

| 目标 | API | Skill |
|---|---|---|
| 建项目 | `POST /api/v1/projects` | — |
| 知识增/审/发 | `POST /knowledge` `/knowledge/{id}/review` `/distribute` | `knowledge-manager` |
| 工程基线/分析 | `/engineering/baselines` `POST /engineering/refresh` | `engineering-analysis` |
| 全景工作区/模块/快照/影响 | `/panorama/workspaces` `/panorama/snapshots` `POST /panorama/impact/analyze` | `game-module-panorama` |
| 迭代/计划/任务 | `/test-iterations` `/test-plans` `/test-plans/{id}/tasks` | `testplan-manager` |
| 用例库/用例/评审 | `/testcase-libraries` `/testcase-libraries/{id}/reviews` | `testcase-manager` `case-review` |
| 报告 | `/test-reports` | `test-report-manager` |
| Workflow | `/workflow-definitions` `/workflow-runs` | `workflow-manager` |
| 任务上下文（含 pitfalls） | `GET /tasks/{ref_type}/{ref_id}/context` | 集体记忆 `tools/collective_memory/*` |
| 见闻 / 课题 / 考试 | `/shared-articles` `/topics` `/exams/*` | `insight-sharing` `topic-discuss` `exam-manager` |
| 平台铁律回读 | `POST /ops/verify` | `basic-operations-preflight` |

---

*维护：本 SOP 基于 2026-07-22 Hub 线上真实数据编写；模块能力随版本演进，字段/路径以 `web/app/models.py` 与各 `SKILL.md` 为准。*
