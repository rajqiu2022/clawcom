# Agent 记忆分层与任务存取路由 P1 实施计划

> 设计文档：`docs/superpowers/specs/2026-07-09-agent-memory-routing-design.md`
> 本计划覆盖 P1 两项：**执行后校验 `/ops/verify`** + **通用壳 Skill `basic-operations-preflight`**。

**目标：** 提供一个"从 DB 重读真实落库状态"的校验原语，堵住"自以为成功"与僵尸待办；把 5 条通用铁律沉淀成可复用 Skill。

**核心约束（来自 #41 提单小助手）：** verify 必须**从数据库重读** `actual`，严禁回显请求体，否则是二次幻觉。

**架构约束：**
- 纯比较逻辑 `compare_expected_actual` 放服务层，可脱离 Flask 单测。
- DB 重读 `reread_resource` 对 `app.models` 受保护导入。
- 新表 `claw_ops_verifications` 用幂等 `CREATE TABLE IF NOT EXISTS` 建（部署脚本直连 DB 建 + 写进 `__init__.py` 源头）；不整文件覆盖远端 `__init__.py`。

---

### Task 1: 纯比较函数 compare_expected_actual

**Files:** `web/app/services/ops_verification.py`（新）、`tests/test_ops_verification.py`（新）

- [ ] 写失败测试：`{status:'done'}` vs actual `{status:'done'}` → verified True，空 mismatches；不一致 → verified False + mismatches 含该键；actual 缺键按 None 处理。
- [ ] 实现 `compare_expected_actual(expected, actual)`：逐键 `str()` 归一比较，返回 `{'verified': bool, 'mismatches': [{'field','expected','actual'}]}`。

### Task 2: DB 重读 + 校验记录服务

**Files:** `web/app/services/ops_verification.py`、`web/app/models.py`（新增 `ClawOpsVerification`）

- [ ] `RESOURCE_MODELS` 注册：`todo→ClawTodo, knowledge→KnowledgeEntry, test_report→TestReport, workflow_step→WorkflowRunStep, agent_task→AgentTask`。
- [ ] `reread_resource(rtype, rid, keys)`：受保护导入，`Model.query.get(rid)`，不存在返回 None，否则 `{k: getattr(row,k,None)}`（JSON 安全）。
- [ ] `ClawOpsVerification` 模型：id/claw_id/token/resource_type/resource_id/expected(Text)/verified(Bool)/actual(Text)/mismatches(Text)/created_at/verified_at。

### Task 3: API 端点 POST /api/v1/ops/verify

**Files:** `web/app/api/ops_verify.py`（新）、`web/app/api/__init__.py`（原地补丁追加导入）

- [ ] body `{resource_type, resource_id, expected:{...}}`；非法类型 400；资源不存在 404。
- [ ] 从 DB 重读 → 比较 → 落一条 `ClawOpsVerification` 记录（claw 来自 `g._auth_claw`）→ 返回 `{verified, actual, mismatches, verification_id}`。

### Task 4: 通用壳 Skill basic-operations-preflight

**Files:** `openclaw-agent/skills/basic-operations-preflight/SKILL.md`（新）

- [ ] 5 条通用铁律 + 入口硬约束（未读不得写）+ 提交前 gate + `/ops/verify` 用法示例。保持极简（避免"装了不更新"）。

### Task 5: 文档与经验记录

- [ ] `docs/经验记录.md` 追加 P1 接口用法与建表注意。

### Task 6: 部署与线上 smoke

- [ ] 部署脚本：直连 DB 幂等建 `claw_ops_verifications`；上传 `models.py`、`ops_verification.py`、`ops_verify.py`；原地补丁 `api/__init__.py`；编译 + 重启。
- [ ] smoke：对已知 todo 用 verify，故意给错 expected → verified=false + mismatches；给对 → verified=true；校验记录落库。
