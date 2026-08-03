# 基础操作前置校验 (basic-operations-preflight)

## 简介

本 Skill 是**所有 Agent 的通用壳**——把课题 #41 实证有效的 5 条通用铁律 + "执行前拦截 / 执行后校验"落成一张随时可勾的清单。它**不含任何项目专用内容**（TAPD 字段、用例库 ID 等留在各自项目 Skill），刻意保持极简，避免"装了不更新"。

> 设计依据：`docs/superpowers/specs/2026-07-09-agent-memory-routing-design.md`
> 定位：`agent-operating-protocol` 定义"任务生命周期"，本 Skill 定义"每次写操作前后必过的硬约束"。

**触发词**：写操作、提交、创建、更新、删除、提单、发报告、改用例、preflight、操作校验、自检、铁律

**必装**：所有接入 Hub 的 Agent 默认安装；任何 create/update/delete 类操作前**第一个加载本 Skill**。

---

## 〇、任务到达：先取上下文包 `task_context`

任何任务（Todo / AgentTask / workflow 节点）**动手前第一步**：拿到 Hub 推送的 `task_context`。

- 已内嵌：sidecar v2.1+ 会把它拼进 prompt（Todo 用 `GET /tasks/todo/{id}/context`；workflow 在 `payload.task_context`）。
- 若手上没有，主动拉：`GET /api/v1/tasks/{ref_type}/{ref_id}/context`（`ref_type ∈ todo|agent_task|workflow_step`）。

从 `task_context` 里取三样东西，直接驱动后续 Gate：

| 字段 | 用途 |
|------|------|
| `required_skills` | **先加载这些 Skill**（含本 Skill），再动手 |
| `preflight_checklist` | 即第三节 Gate 要逐条勾的铁律来源（始终最新） |
| `top_pitfalls` | 本任务高相关历史坑，调 API 前对照，避免重复踩 |

> 拿不到 `task_context` 不阻塞执行，但必须回退到本 Skill 内置的 5 条铁律（下节）。

---

## 一、5 条通用铁律（写操作前必过）

| # | 铁律 | 拦什么 |
|---|------|--------|
| 1 | **身份校验**：确认当前 `claw_id` 与 token 前缀一致 | Token 互换 / 身份混淆 |
| 2 | **读写分离**：验证只用 GET，禁止用 POST/PATCH/DELETE 做验证 | 验证复用写命令 → 制造重复数据 |
| 3 | **提交后回读**：写操作后用独立 GET 确认真实落库（不信返回体） | 自以为成功 / 僵尸待办 |
| 4 | **查证再填**：枚举/ID 字段必须从 API options 取，禁止凭记忆手打 | 字段幻觉 / 参数值猜测 |
| 5 | **配置校验**：配置变更后比对 hash | 配置漂移 |

---

## 二、入口硬约束

> **未读完本 SKILL.md，不得调用任何 create/update/delete 写接口。**

这条等价于提单小助手实证有效的"铁律 #0"，把"跳过 Skill 直接执行"从源头堵住。

---

## 三、提交前 Gate（正文里逐条复述才允许提交）

执行写操作前，在你的输出正文里逐条打勾：

```
□ 铁律1 身份校验：claw_id=___ token 前缀=___ 一致
□ 铁律2 读写分离：本次验证仅用 GET
□ 铁律4 查证再填：枚举/ID 字段来源=API options（非记忆）
→ 通过后方可提交写操作
```

提交**之后**立即执行铁律 3 的回读校验（见第四节）。

---

## 四、执行后校验：/ops/verify（从 DB 重读，不信返回体）

任何写操作完成后，用 Hub 的校验原语确认真实落库——**Hub 从数据库重读**，而非回显你的请求体（堵二次幻觉）：

```bash
POST /api/v1/ops/verify
{
  "resource_type": "todo",          # todo|knowledge|test_report|workflow_step|agent_task
  "resource_id": 724,
  "expected": {"status": "completed"}
}
```

返回：

```json
{
  "verified": true,
  "actual": {"status": "completed"},
  "mismatches": [],
  "verification_id": 12
}
```

- `verified=false` + `mismatches` 非空 → 你的写操作**没有真正生效**（典型：僵尸待办 completion 记录写了但 status 没更新）。此时不得对用户/owner 声称"已完成"，需排查后重试。
- `expected` 只放你能确认的关键字段（如 `status`、标题、关键外键），不要放易变字段。

---

一句话：**先取上下文包（task_context）→ 读完再动手（入口硬约束）→ 逐条勾选再提交（Gate）→ 提交后从 DB 回读确认（/ops/verify）。** 四步缺一不可。
