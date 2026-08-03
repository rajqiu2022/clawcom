# Agent Collective Memory and SkillOpt Implementation Plan

> **同步说明：** 本文档源自 `hermes agent/docs/superpowers/plans/2026-06-07-agent-collective-memory-skillopt.md`，在 claw_team 仓库跟踪 Hub 侧落地进度。课题讨论见 [Topic #36](https://clawteam.woa.com:18800/topics/36)。
>
> **claw_team P0 进度（2026-06-07）：**
> - [x] `knowledge/schemas/pitfall_entry.schema.json`
> - [x] `docs/agent-memory/pitfall-entry-template.md`
> - [x] `knowledge/seeds/racinggo-pitfalls.initial.json`（10 条）
> - [x] `tools/collective_memory/knowledge_client.py`
> - [x] `tools/collective_memory/pitfall_registry.py`
> - [x] `tools/collective_memory/experience_trigger.py`
> - [x] `tools/collective_memory/experience_draft.py`
> - [x] `tests/collective_memory/test_pitfall_registry.py`
> - [x] `tests/collective_memory/test_experience_trigger.py`
> - [x] `openclaw-agent/skills/agent-operating-protocol/SKILL.md`
> - [x] Todo API 接入 `pitfall_notice` + description 自动附加
> - [x] `web/app/services/experience_trigger_service.py`
> - [ ] Hub 线上导入种子数据（需部署后执行）
> - [ ] Skill 同步到 Hub 市场（agent-operating-protocol）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立一套“群体 Agent 经验共享 -> 任务前主动触发 -> 高频经验升级 Skill -> SkillOpt 验证优化”的闭环，减少多个 Claw/Agent 重复踩坑。

**Architecture:** P0 先用现有 Hub Knowledge API 建公共“踩坑登记簿”和任务前检索机制，立即解决群体遗忘。P1 将高频稳定经验沉淀为共享 Skill。P2 接入 SkillOpt，把历史任务样本转成可评分 benchmark，自动优化并产出 `best_skill.md`，再进入 Skill 市场。

**Tech Stack:** Hub Knowledge API、现有 Agent/Claw 任务入口、Skills Markdown、TAPD/Hub/WeCom 工具链、SkillOpt Python 包、JSON/YAML 数据集。

---

## 1. 背景和核心判断

当前问题不是单个 Agent 的上下文记忆不足，而是“个体记忆没有公共化”。每个 Claw 都有自己的 `MEMORY.md`、会话记录和 Skills，但彼此隔离，导致同一类工具链坑被多个 Agent 反复踩。

Letta 的价值是“长期记忆和有状态 Agent 底座”，适合后续统一 Agent 平台时参考。SkillOpt 的价值更贴近当前目标：把自然语言经验 Skill 当成可训练文本，通过任务轨迹、反思、补丁、验证集评分来优化，最终输出可部署的 `best_skill.md`。

本方案的落地顺序：

1. P0：踩坑登记簿 + 任务前主动检索。
2. P1：Skill 共享市场 + 高频经验升级规则。
3. P2：SkillOpt 训练验证闭环。

---

## 2. 文件结构建议

如果当前仓库已有同类目录，保持同名接口并放入对应模块；如果没有，按以下结构创建。

```text
docs/
  agent-memory/
    pitfall-entry-template.md
    skill-upgrade-policy.md
    skillopt-pilot-guide.md

knowledge/
  schemas/
    pitfall_entry.schema.json
    skill_entry.schema.json
    task_experience.schema.json
    eval_case.schema.json
  seeds/
    racinggo-pitfalls.initial.json

skills/
  shared/
    hub-api-skill.md
    tapd-upload-skill.md
    wecom-media-skill.md
    racinggo-requirement-skill.md

tools/
  collective_memory/
    knowledge_client.py
    pitfall_registry.py
    experience_trigger.py
    experience_draft.py
    skill_market.py
    skillopt_exporter.py

tests/
  collective_memory/
    test_pitfall_registry.py
    test_experience_trigger.py
    test_skill_market.py
    test_skillopt_exporter.py
```

职责划分：

- `pitfall_registry.py`：封装踩坑登记簿的创建、查询、更新、状态变更。
- `experience_trigger.py`：在任务开始前按关键词、工具名、项目名检索经验并生成提示。
- `experience_draft.py`：在 Todo/任务结束时生成经验草稿，降低登记成本。
- `skill_market.py`：管理共享 Skill 的发布、版本、适用范围和来源经验。
- `skillopt_exporter.py`：把历史经验和任务结果导出为 SkillOpt 可训练数据集。

---

## 3. 数据结构

### 3.1 踩坑条目 `PitfallEntry`

```json
{
  "id": "pitfall-wecom-image-send-001",
  "title": "WeCom 不能直接复用图片链接发送图片",
  "project": "RacingGO",
  "module": "wecom",
  "type": "pitfall",
  "symptom": "Agent 汇总报告时只发文字，图片变成链接，声称无法发图片。",
  "root_cause": "图片没有作为可追踪资产记录，缺少 local_path、media_id 或 msg_id，汇总时只能依赖聊天上下文回忆。",
  "solution": "每次生成或发送图片后写入资产台账；汇总时通过 asset_id 查询并重新上传或复用企业微信 media_id。",
  "verified_steps": [
    "生成图片后记录 local_path、public_url、wecom_media_id、wecom_msg_id。",
    "汇总报告时先查任务资产台账。",
    "企业微信接口不可复用旧 media_id 时，从 local_path 重新上传图片。"
  ],
  "related_tools": ["wecom", "image_generation", "artifact_registry"],
  "keywords": ["企业微信", "WeCom", "图片", "media_id", "汇总报告", "发不了图片"],
  "status": "active",
  "owner": "小峰",
  "created_at": "2026-06-07",
  "updated_at": "2026-06-07",
  "evidence": {
    "discussion_url": "https://clawteam.woa.com:18800/topics/36",
    "source": "agent discussion"
  }
}
```

### 3.2 Skill 条目 `SkillEntry`

```json
{
  "id": "skill-wecom-media",
  "name": "WeCom Media Sending Skill",
  "path": "skills/shared/wecom-media-skill.md",
  "version": "0.1.0",
  "status": "active",
  "scope": {
    "projects": ["RacingGO"],
    "modules": ["wecom"],
    "tools": ["enterprise_wechat", "image_generation"]
  },
  "source_pitfalls": ["pitfall-wecom-image-send-001"],
  "activation_keywords": ["企业微信", "WeCom", "发图片", "media_id", "素材上传"],
  "last_validated_at": "2026-06-07",
  "validation": {
    "method": "manual",
    "pass_count": 3,
    "fail_count": 0
  }
}
```

### 3.3 SkillOpt 训练样本 `EvalCase`

```json
{
  "id": "wecom-image-report-001",
  "task_type": "wecom_media_report",
  "input": "用户要求把本次报告和前面生成过的龙虾图片一起通过企业微信发送。",
  "context": {
    "assets": [
      {
        "asset_id": "asset-lobster-v3",
        "asset_type": "image",
        "local_path": "/data/artifacts/lobster-v3.png",
        "wecom_media_id": "MEDIA_ID_EXAMPLE",
        "status": "sent"
      }
    ]
  },
  "expected_behavior": [
    "先查询任务资产台账。",
    "确认图片 asset_id 和可发送来源。",
    "通过企业微信图片接口发送图片附件。",
    "发送文字报告。"
  ],
  "failure_patterns": [
    "只发送文字。",
    "只发送图片链接。",
    "未查询资产台账就声称无法发图片。"
  ],
  "score_rule": {
    "hard": "完成图片和文字发送得 1，否则 0。",
    "soft": "查询资产台账、上传图片、发送报告三个步骤各占 0.33。"
  }
}
```

---

## 4. P0 实施：踩坑登记簿和主动触发

### Task 1: 建立踩坑登记簿模板和 Schema

**Files:**
- Create: `knowledge/schemas/pitfall_entry.schema.json`
- Create: `docs/agent-memory/pitfall-entry-template.md`
- Create: `knowledge/seeds/racinggo-pitfalls.initial.json`

- [x] **Step 1–4:** schema、模板、10 条种子、单元测试校验

### Task 2: 封装 Hub Knowledge API 客户端

**Files:**
- Create: `tools/collective_memory/knowledge_client.py`
- Test: `tests/collective_memory/test_pitfall_registry.py`

- [x] **Step 1–4:** KnowledgeClient 接口 + HubKnowledgeClient + mock 测试

### Task 3: 任务开始前主动检索

**Files:**
- Create: `tools/collective_memory/experience_trigger.py`
- Test: `tests/collective_memory/test_experience_trigger.py`

- [x] **Step 1–4:** 关键词抽取、格式化提示、WeCom 场景测试

### Task 4: 任务结束经验草稿

**Files:**
- Create: `tools/collective_memory/experience_draft.py`

- [x] **Step 1:** `build_experience_draft()` 已实现
- [ ] **Step 2:** 接入 Todo 完成事件（待 Hub API 集成）

---

## 5. P1 实施：Skill 共享市场

（待 P0 部署验证后推进，详见原文 Task 5–7）

---

## 6–12. 运行机制、验收指标、风险、执行顺序

见原文档各节。核心闭环：

```text
事实登记 -> 任务触发 -> Skill 沉淀 -> 验证优化 -> 版本发布
```

Letta 对应“长期状态和记忆底座”，SkillOpt 对应“Skill 优化器”。当前最优先的是公共踩坑登记簿和触发机制。
