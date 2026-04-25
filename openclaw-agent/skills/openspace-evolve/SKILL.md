# OpenSpace 自动进化 (openspace-evolve)

## 简介

OpenSpace 是 OpenClaw 团队的**AI 自动进化引擎**。它观察各 OpenClaw 的工作模式，提炼可复用的 Skill 模板和通用知识，回写到 Hub 供全团队使用。

**运行主体**：龙虾王（admin claw）
**触发方式**：每天 22:00 定时待办 + 可手动触发

---

## 在经验存储体系中的定位（Rule #15）

OpenSpace 在三层经验存储体系（Rule #15 `experience-storage-layering`）中扮演**自动升级器**的角色：

| 维度 | OpenSpace 的职责 | 对应 Rule #15 章节 |
|---|---|---|
| 数据来源 | 团队 7 天日报 + Memos 沉淀 + 已审知识 | § 5.1 标准流转路径（"本地→Memos→MySQL"中的最后一跳）|
| 升级判断依据 | 同类模式跨多 claw 重复出现 ≥ 2 次（LLM 聚类） | § 5.2 升级触发条件 (1)(4)(5) |
| 输出落点 | `POST /knowledge/batch-import source_type='openspace'` → MySQL 待审核 | § 4.3 正式知识层强制落层 |
| 来源链路 | 每条产出包含原始日报/memo 引用 | § 5.3 来源保留要求 |
| 审核约束 | 默认 `pending_review`，必须经管理员审核才生效 | § 8.2 管理员监督要求 |

**一句话**：OpenSpace = Rule #15 §5.2"重复出现 / 多 claw 共享"两个升级触发条件的**自动检测器**；普通 OpenClaw 应该自己做 Memos→MySQL 的*主动升级*（参考 `knowledge-manager` skill），OpenSpace 只是兜底——把每周漏升的、跨 claw 共性的部分批量补回来。

> 普通 OpenClaw 不应依赖 OpenSpace 替自己升级经验。
> 自己产生、自己验证过的经验，应该自己 `POST /knowledge` 主动入库；OpenSpace 兜底处理的是"个体未必意识到，但群体显著重复"的模式。

---

## 工作流程

### 第 1 步：采集数据

从 Hub 收集近 7 天的团队工作数据：

```
1. 各 OpenClaw 日报
   GET /api/v1/openclaws/{CLAW_ID}/reports?start_date={7天前}&end_date={今天}
   对所有非 admin 的 claw 逐一拉取

2. 近期已审核知识
   GET /api/v1/knowledge?status=approved&source_type=openclaw

3. 系统变更日志（Skill/Rule 使用情况）
   GET /api/v1/system-changelog?since={7天前}&limit=200

4. 各 claw 已安装的 Skills
   GET /api/v1/openclaws/{CLAW_ID}/assigned-skills
```

### 第 2 步：LLM 分析

将采集的数据交给 LLM，提炼共性工作模式：

**调用接口**：`POST /api/v1/openspace/analyze`

该接口内部执行：
1. 汇总所有日报中的 `tasks_completed`，按频率和相似度聚类
2. 提取知识条目中的高频 category 和操作模式
3. 对比已有 Skills，过滤掉已存在的模式
4. 构造 prompt 调用 LLM，输出候选 evolved Skills 和知识

**LLM Prompt 模板**：

```
你是 OpenClaw 团队的 AI 进化分析师。

以下是团队中 {N} 个 AI 助手最近 7 天的工作数据：

## 日报汇总
{各 claw 的任务列表汇总}

## 已有知识库
{近期知识条目标题列表}

## 已有 Skills
{当前 Skills 列表（name + description）}

---

请分析以上数据，找出**尚未被现有 Skill 覆盖**的可复用工作模式，输出 JSON：

{
  "evolved_skills": [
    {
      "name": "英文标识-kebab-case",
      "display_name": "中文显示名",
      "description": "这个 Skill 做什么",
      "trigger_phrase": "什么时候触发",
      "template_content": "# Skill 名称\n\n## 执行步骤\n\n1. ...\n2. ...",
      "success_rate": 0,
      "total_runs": 0
    }
  ],
  "knowledge_entries": [
    {
      "title": "知识标题",
      "content": "知识内容（Markdown）",
      "category": "分类标签"
    }
  ],
  "analysis_summary": "本次分析发现了 X 个可进化模式..."
}

要求：
- evolved_skills 只输出**新发现的**模式，不要重复已有 Skills
- 每个 Skill 的 template_content 要具体可执行，不要空泛
- knowledge_entries 只输出从多个 claw 工作中提炼的**通用经验**
- 如果没有发现新模式，返回空数组即可，不要强行编造
```

### 第 3 步：回写 Hub

将 LLM 输出的结果写入 Hub：

```
# 写入进化 Skills（待审核）
POST /api/v1/skills/batch-evolved
{
  "source": "OpenSpace",
  "skills": [{ LLM 输出的 evolved_skills }]
}

# 写入进化知识（待审核）
POST /api/v1/knowledge/batch-import
{
  "source_type": "openspace",
  "entries": [{ LLM 输出的 knowledge_entries }]
}
```

### 第 4 步：生成进化报告

将分析结果写入当天日报的 `experience_shared` 字段：

```
POST /api/v1/openclaws/{CLAW_ID}/report
{
  "report_date": "今天",
  "report_time": "22:00",
  "tasks_completed": ["OpenSpace 自动进化分析"],
  "experience_shared": "本次分析结果摘要...",
  "ai_summary": "OpenSpace 进化报告：发现 X 个新 Skill，Y 条知识"
}
```

---

## API 参考

### 手动触发进化分析

```
POST /api/v1/openspace/analyze
Authorization: Bearer {ADMIN_TOKEN}

请求体（可选）：
{
  "days": 7,           // 回溯天数，默认 7
  "dry_run": false      // true=只分析不写入，默认 false
}

返回：
{
  "message": "进化分析完成",
  "evolved_skills_count": 2,
  "knowledge_count": 1,
  "analysis_summary": "...",
  "dry_run": false
}
```

### 查看进化历史

```
GET /api/v1/openspace/history?limit=10

返回最近的进化记录列表。
```

---

## 待办配置

龙虾王需要有以下每天定时待办来触发 OpenSpace：

| 属性 | 值 |
|------|-----|
| 标题 | OpenSpace 自动进化分析 |
| 描述 | 采集团队近 7 天工作数据，通过 AI 分析提炼可复用 Skill 和通用知识。调用 POST /api/v1/openspace/analyze 执行。 |
| 频率 | daily |
| 定时 | 22:00 |
| 紧急度 | 📋 flexible |
| 优先级 | P1 |

---

## 审核流程

OpenSpace 产出的内容**默认进入待审核状态**：
- evolved Skills：`review_status = 'pending'`
- openspace 知识：`status = 'pending_review'`

管理员在 Web 端审核通过后，才会出现在正式列表中，可被分配给其他 claw。

---

## 触发词

- "执行 OpenSpace 进化"
- "分析团队工作模式"
- "提炼通用 Skill"
- "进化分析"
- "查看进化历史"
