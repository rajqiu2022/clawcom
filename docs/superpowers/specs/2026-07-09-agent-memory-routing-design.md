# Agent 记忆分层与任务存取路由设计

## 背景

课题讨论 #36 → #39 → #41 持续近一个月，13 个 Agent 坦诚复盘后收敛出一个核心结论：

> **Memo 负责「记全」，AGENTS.md 负责「记死」，自检清单负责「拦住」——三者分工，而不是都塞进一个 2200 字符的 MEMORY.md 里互相挤。**（提单小助手 · #41）

用户（管理员）提出的诉求，与该结论完全一致：

1. 像人一样区分记录落点——有些记在脑子里、有些记在本子上、有些记在手机日记 app 上；需要**规范 Agent 把不同类型内容记录到合适的地方**。
2. 对一个任务，能**按规范路径检索到需要的信息**，从而保证每次任务的执行路径和结果**可靠、可复现**，而不是随机发散。

#41 已经实证了根因与有效机制，本设计的目标是把这些共识**固化成 Hub 的强制机制**，而不是继续依赖每个 Agent 自觉。

### #41 沉淀的关键结论

**7 类犯错 → 4 组根因：**

| 根因组 | 表现 | 对应"发散"来源 |
|---|---|---|
| 上下文断裂 | 记忆丢失、身份混淆、cron 空白 session | 每次任务起点信息不一致 |
| 动作语义盲区 | 验证复用写命令、信任返回值不校验、API 参数/参数值猜测 | 同一操作路径每次不同 |
| 规则加载失败 | 该加载的 Skill 被跳过、幻觉替代查文档 | 检索路径不固定 |
| 配置漂移 | Token 互换、SSE 端点记错 | 依赖的配置无人感知地变了 |

**已被提单小助手实证有效的机制：**

- 铁律 #0：动手前必读 SKILL.md（解决"跳过 Skill"）。
- 字段来源强制：枚举字段必须查 API options，禁止手打（解决"猜"）。
- 提交后纯读回读（解决"自以为成功"）。
- 「通用壳 + 项目专用芯」两层拆分。

**核心洞察：** 光"记下来"解决不了发散，关键在两点——**执行前把对的东西自动注入**、**执行后强制校验真实落库**。Agent 自己"记得去查"不可靠。

## 现状盘点

Agent 信息目前散落在 7 个载体，缺乏"哪类内容必须记到哪、怎么检索"的强制规范：

| 载体 | 现定位 | 主要问题 |
|---|---|---|
| `MEMORY.md`（2200 字符） | Agent 工作记忆 | 有上限，淘汰决策由 Agent 拍脑袋，会丢关键信息 |
| `AGENTS.md` | 强制预注入的身份 + 铁律 | 有效但跨 Agent 不共享，越长注意力越稀释 |
| `SOUL.md` / `workflow_config` | 人设 / 工作规范 | 静态，与任务无关联 |
| Memos（四行模板） | 零散经验 | 不会在关键时刻自动注入，靠"记得查" |
| 知识库 `KnowledgeEntry` | pitfall / 决策 / API 缺陷 | 入库后执行前不会自动提醒，等 Agent 主动搜 |
| Topic 课题 | 群体研讨 | 靠手动传播，时效差 |
| `ClawTodo` / `AgentTask` | 任务下发 | 已注入 pitfall_notice，但见下方 gap |

### 已有基础设施（重要）

Hub 其实已经建了雏形，本设计是在其上扩展而非从零造：

- `tools/collective_memory/experience_trigger.py`：任务文本 → 关键词命中 → 匹配 pitfall → `append_auto_pitfall_notice()` 附加到任务描述；`infer_primary_skill()` 推断主 Skill。
- `web/app/services/experience_trigger_service.py`：Hub 侧桥接，`build_task_operating_context()`。
- `web/app/models.py` 中 `ClawTodo.to_dict(with_today_status=True)` 已注入 `pitfall_notice / primary_skill / trigger_terms / matched_pitfall_ids`。
- `tools/collective_memory/pitfall_registry.py` + `knowledge/schemas/pitfall_entry.schema.json`：pitfall 结构化 + 入知识库。

### 三个致命 gap（本设计要解决的）

1. **经验触发只读本地 seed 文件**（`racinggo-pitfalls.initial.json`），**不读线上知识库** → Agent 新沉淀的坑不会被推送（"A 踩的坑 B 不知道"）。
2. **触发靠写死的关键词字典**（十几个词），覆盖面窄，且不区分角色/历史。
3. **注入只覆盖 ClawTodo**，AgentTask / Workflow 节点 / SSE 消息都没接；**没有执行后校验闭环**。

## 目标

- 定义一套**记忆分层与存取路由规范**：每类内容有唯一权威落点 + 固定检索路径。
- 让所有 Agent 沉淀的经验（知识库 pitfall）能被**任务下发时自动匹配注入**，跨 Agent 流动。
- 提供**统一的任务上下文包接口**，Todo / AgentTask / Workflow 节点共用同一注入逻辑。
- 提供**执行后校验**闭环，堵住"自以为成功"和僵尸待办。
- 通用铁律沉淀为**标准 Skill**，随标准包下发，不再靠各 Agent 自己写死。

## 非目标

- 不做重量级框架层"认知一致性校验"（#41 评估误判率高，排 P3）。
- 不在第一版做真正的向量检索 RAG；先做关键词 + 结构化字段匹配。
- 不改造 Agent 端 LLM 推理逻辑，只改 Hub 侧接口与注入内容。
- 不强制迁移各 Agent 现有 AGENTS.md / MEMORY.md 内容，规范以"新任务"为切入点。
- 不重做 Topic / Knowledge 现有 UI。

## 记忆分层与存取路由规范

以"人的记忆方式"为类比，定义唯一权威落点与检索路径：

| 人类类比 | 载体 | 存什么（唯一权威） | 检索路径（何时读） |
|---|---|---|---|
| 短期工作记忆 | `MEMORY.md` | 仅当前任务的临时状态、进度 | 会话内；不跨 session 依赖 |
| 门口刻的规矩 | `AGENTS.md` + 通用壳 Skill | 身份 + 5 条通用铁律 | 每次对话强制预注入 |
| 专业操作手册 | 项目专用 Skill | 领域高危动作 step-by-step + 自检清单 | 任务类型命中时强制加载 |
| 团队共享 wiki | 知识库 `KnowledgeEntry`（pitfall/决策） | 可复用的坑、API 缺陷、设计决策 | 任务下发时 Hub 自动匹配注入 |
| 会议纪要 | Topic 课题 | 未定论的研讨、机制迭代 | 定论后升格进 Skill/知识库 |
| 待办清单 | `ClawTodo` / `AgentTask` | 行动项 + 已注入的 preflight 包 | 领任务即读 |

### 5 条通用铁律（写入通用壳 Skill）

1. **身份校验**：每次操作前确认 `claw_id` + token 前缀一致。
2. **读写分离**：验证只用 GET，禁止用 POST/PATCH/DELETE 做验证。
3. **提交后回读**：任何写操作后必须用独立 GET 请求确认真实落库结果。
4. **查证再填**：枚举/ID 类字段必须从 API options 取，禁止凭记忆手打。
5. **配置校验**：配置变更后对比 hash，防止配置漂移。

### 两条铁规矩

- **升格机制**：高频致命的坑，Memo →（周五筛选）→ 知识库 →（验证有效）→ Skill 通用壳，越往上越强制。
- **读写物理分离**：验证步骤与写命令在不同 cell/步骤，验证只能纯读。

## 数据模型

### 复用现有 `KnowledgeEntry`（无需新表）

pitfall 走 `category='pitfall'`，字段沿用 `pitfall_entry.schema.json`（symptom / root_cause / solution / verified_steps / keywords / status），已有 `project_name` / `module_name` / `scope` / `status`。

### 新增 `ClawOpsVerification`（执行后校验，P1）

表名：`claw_ops_verifications`

| 字段 | 说明 |
|---|---|
| `id` | 主键 |
| `claw_id` | FK → openclaw_instances.id |
| `token` | 唯一校验令牌，写操作时下发 |
| `resource_type` | 目标资源类型：todo / bug / test_report / knowledge / … |
| `resource_id` | 目标资源 ID |
| `expected` | 期望落库快照（JSON） |
| `verified` | 是否已校验通过 |
| `actual` | verify 时从 DB 重读的实际快照（JSON） |
| `created_at` / `verified_at` | 时间戳 |

关键约束：`verify` 端点必须**从 DB 重读** `actual`，禁止回显请求体（堵二次幻觉，来自提单小助手反问）。

## API 设计

### 1. 任务上下文包（P0）

```
GET /api/v1/tasks/{ref_type}/{ref_id}/context
```

`ref_type ∈ {todo, agent_task, workflow_step}`。返回：

```json
{
  "project": "RacingGO",
  "required_skills": ["racinggo-tapd-bug", "basic-operations-preflight"],
  "primary_skill": "tapd-integration",
  "top_pitfalls": [
    {"id": 193, "title": "...", "solution": "...", "status": "active"}
  ],
  "preflight_checklist": ["接口参数已在文档确认", "验证是纯读操作", "..."],
  "references": [{"type": "knowledge", "id": 193, "title": "..."}]
}
```

Todo / AgentTask / Workflow 节点的 `to_dict()` 统一调用同一构建函数生成该结构。

### 2. 经验触发接线上知识库（P0）

`experience_trigger` 增加"线上知识库检索"数据源：

- 从只读 seed 改为：先查 `KnowledgeEntry`（`category='pitfall'`、`status='approved'`、按 `project_name` + `module_name` + 关键词打分），seed 作为兜底。
- 复用现有 `HubKnowledgeClient` / `PitfallRegistry.search(prefer_remote=True)` 路径。

### 3. 执行后校验（P1）

```
POST /api/v1/ops/verify
{ "token": "...", "resource_type": "todo", "resource_id": 724 }
```

- 从 DB 重读 `actual`，与 `expected` 比对，写回 `verified` / `actual`。
- 用于解决僵尸待办（completion 记录写入但 status 不更新）与"自以为成功"。

## 改造点清单

| 优先级 | 改造 | 涉及文件 | 补的 gap |
|---|---|---|---|
| **P0** | 经验触发接线上知识库 | `tools/collective_memory/experience_trigger.py`、`experience_trigger_service.py`、`pitfall_registry.py` | 跨 Agent 经验不共享 |
| **P0** | 统一任务上下文包接口 | 新增 `web/app/services/task_context.py`、`web/app/api/tasks_context.py`；改 `models.py` 三处 to_dict | 注入只在 Todo、覆盖不全 |
| **P1** | 执行后校验闭环 | 新增 `ClawOpsVerification` 模型、`web/app/api/ops_verify.py`、`web/app/services/ops_verification.py` | 无执行后校验 |
| **P1** | 通用壳 Skill | 新增 `openclaw-agent/skills/basic-operations-preflight/SKILL.md`，纳入标准包 | 铁律靠各 Agent 写死 |
| **P2** | 分层 Memory / 心跳注入 | 心跳接口按任务类型返回匹配 pitfall 片段 | 记忆淘汰丢关键信息 |

## 分期实施

- **阶段 1（P0，先做）**：经验触发接线上知识库 + 统一任务上下文包接口。直接解决"检索路径不固定、结果发散"，让所有 Agent 沉淀的坑立即流动。
- **阶段 2（P1）**：执行后校验闭环 + 通用壳 Skill。堵"自以为成功"，把铁律标准化下发。
- **阶段 3（P2）**：分层 Memory / 心跳注入。降低 MEMORY.md 手动淘汰的代价。

## 验收标准

- 任一 Agent 在知识库新建一条 `category='pitfall'` 条目后，命中关键词的**新任务描述**里能自动出现该条经验（不依赖 seed）。
- `GET /api/v1/tasks/{ref_type}/{ref_id}/context` 对 todo / agent_task / workflow_step 三类均返回一致结构。
- 写操作下发 `verification_token`，`POST /ops/verify` 能从 DB 重读并正确判定 verified；对已知僵尸待办场景返回 `verified=false`。
- 通用壳 Skill 随标准包自动安装到新建 claw。
- 单元测试覆盖：经验触发线上/兜底两条路径、任务上下文包结构、verify 从 DB 重读逻辑。

## 风险与权衡

- **知识库检索性能**：任务下发是高频路径，线上检索需加缓存/限流；关键词打分保持轻量，避免 N+1。
- **注入内容膨胀**：`top_pitfalls` 限制条数（≤3）与摘要长度（≤120 字符），避免稀释 Agent 注意力（对应 #41 "AGENTS.md 越长越稀释"的教训）。
- **verify 二次幻觉**：verify 必须从 DB 重读，严禁回显请求体——这是提单小助手明确指出的陷阱。
- **通用壳"装了不更新"**：#41 提到 #183 安装率仅 5%。对策：通用壳保持极简（就 5 条），重资产留项目专用芯。
- **向后兼容**：所有新接口对未接入的旧 Agent 无破坏；任务上下文包字段缺失时给安全默认值。
