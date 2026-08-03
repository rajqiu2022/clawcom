# Agent Skill 下发端点契约 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为定制 Agent 提供经过 Claw Token 鉴权、支持 assigned/Profile/task_context 三种授权来源、带版本与 SHA256 的 Skill Manifest 和下载 API。

**Architecture:** 新增独立 `skill_delivery` 服务负责来源合并、任务归属、可见性、文档包规范化和哈希；API 层只做参数解析、Token 鉴权及 HTTP 文件/ZIP 响应。保留旧公开 Skill 下载接口兼容存量 Agent，新 Loader 只使用 Claw-scoped 端点。

**Tech Stack:** Python 3、Flask、Flask-SQLAlchemy、SQLite 测试库、`hashlib`、`zipfile`、`unittest`。

## Global Constraints

- Profile 和 task_context required Skill 可自动授权，无需写入 `openclaw_skills`。
- 动态任务必须属于 URL 中的 `claw_id`。
- 自动授权仅允许已审核、未删除、项目匹配、私有归属匹配的 Skill。
- 不新增数据库字段或依赖。
- Manifest 不返回文件正文。
- 旧 `/skills/{id}/raw|files|pack` 接口保持兼容。
- 所有生产代码必须由先失败的自动化测试驱动。
- 未明确要求时不创建 Git commit。

---

## File Map

- Create `web/app/services/skill_delivery.py`：纯服务，负责 bundle、哈希、来源解析、授权判断和 Manifest 构建。
- Modify `web/app/api/openclaws.py`：增加 Manifest、Agent 单文件下载和 Agent ZIP 下载路由。
- Create `tests/test_skill_delivery.py`：纯服务测试，覆盖 bundle/hash/来源/授权。
- Create `tests/test_skill_delivery_api.py`：真实 Flask + SQLite HTTP 契约测试。
- Modify `docs/自研定制Agent接入Hub改造说明.md`：写入最终接口、响应和 Loader 行为。
- Modify `docs/经验记录.md`：记录动态授权、任务归属和稳定哈希经验。

### Task 1: Skill 文档包规范化与稳定哈希

**Files:**
- Create: `web/app/services/skill_delivery.py`
- Create: `tests/test_skill_delivery.py`

**Interfaces:**
- Produces: `normalized_skill_files(skill) -> list[dict]`
- Produces: `skill_bundle_descriptor(skill) -> dict`
- 文件项结构：`path/content/size/sha256/updated_at`
- Bundle 结构：`files/sha256/content_version`

- [ ] **Step 1: 写旧 Skill 合成 `SKILL.md` 的失败测试**

```python
def test_template_content_becomes_skill_md_when_file_row_missing(self):
    skill = _Skill(template_content='# Demo', updated_at=_dt(1), file_entries=[])
    files = delivery.normalized_skill_files(skill)
    self.assertEqual([f['path'] for f in files], ['SKILL.md'])
    self.assertEqual(files[0]['content'], '# Demo')
```

- [ ] **Step 2: 运行测试并确认因模块/函数缺失而失败**

Run:

```powershell
python -m unittest tests.test_skill_delivery.SkillBundleTest.test_template_content_becomes_skill_md_when_file_row_missing -v
```

Expected: FAIL/ERROR，明确指出 `skill_delivery.py` 或 `normalized_skill_files` 尚不存在。

- [ ] **Step 3: 实现文件规范化**

规则：

```python
def normalized_skill_files(skill):
    rows = list(getattr(skill, 'file_entries', []) or [])
    by_path = {
        str(row.filename).replace('\\', '/').lstrip('/'): {
            'path': normalized_path,
            'content': row.content or '',
            'updated_at': _iso(row.updated_at or skill.updated_at),
        }
        for row in rows
    }
    if 'SKILL.md' not in by_path and (skill.template_content or ''):
        by_path['SKILL.md'] = {
            'path': 'SKILL.md',
            'content': skill.template_content,
            'updated_at': _iso(skill.updated_at),
        }
    return [_with_size_and_sha256(by_path[path]) for path in sorted(by_path)]
```

拒绝空路径、`.`、`..` 和含 `../` 的文件名，避免 Loader 解压路径穿越。

- [ ] **Step 4: 写稳定 SHA256 的失败测试**

覆盖：

- 文件插入顺序不同，Bundle SHA256 相同。
- 修改内容，文件 SHA256 和 Bundle SHA256 同时变化。
- 路径和内容边界无拼接歧义。

Bundle 哈希输入采用：

```python
hasher.update(len(path_bytes).to_bytes(8, 'big'))
hasher.update(path_bytes)
hasher.update(len(content_bytes).to_bytes(8, 'big'))
hasher.update(content_bytes)
```

- [ ] **Step 5: 实现 `skill_bundle_descriptor` 并运行纯服务测试**

Run:

```powershell
python -m unittest tests.test_skill_delivery.SkillBundleTest -v
```

Expected: 所有 Bundle 测试 PASS。

### Task 2: 授权来源、任务归属与 Manifest 服务

**Files:**
- Modify: `web/app/services/skill_delivery.py`
- Modify: `tests/test_skill_delivery.py`

**Interfaces:**
- Produces: `resolve_task_context(ref_type, ref_id, claw) -> dict | None`
- Produces: `build_skill_manifest(claw, ref_type=None, ref_id=None, base_path='/api/v1') -> dict`
- Produces: `get_authorized_skill_bundle(claw, skill_id, ref_type=None, ref_id=None) -> dict`
- Raises: `SkillDeliveryError(code, message, status_code)`

- [ ] **Step 1: 写 assigned/Profile 来源合并的失败测试**

期望：

```python
self.assertEqual(item['sources'], ['assigned', 'profile'])
self.assertEqual(manifest['missing_skills'], [])
```

Profile 来源读取当前 Agent 的 active `AgentPostAssignment`：

- `assignment.status == 'active'`
- `assignment.post.status == 'active'`
- `assignment.post.profile.status == 'active'`
- `assignment.post.profile.required_skills_json`

如果有 `is_primary=True` 的 assignment，只使用主 Profile；否则按 assignment ID 最小的一条作为 active Profile，与 sidecar-config 当前选择语义保持一致。

- [ ] **Step 2: 写 task_context 自动授权与跨 Agent 拒绝测试**

覆盖三个模型：

```text
todo.openclaw_id
agent_task.claw_id
workflow_step.target_claw_id
```

服务端使用 `tasks_context._resolve_task(ref_type, ref_id)` 获取 title、description、claw、project，再调用 `build_task_context_payload`。若解析出的任务 Agent ID 与当前 Claw ID 不一致，抛出：

```python
SkillDeliveryError('task_forbidden', '任务不属于当前 Agent', 403)
```

- [ ] **Step 3: 实现来源解析和参数校验**

规则：

- `ref_type/ref_id` 必须同时为空或同时非空。
- `ref_type` 仅允许 `todo/agent_task/workflow_step`。
- 任务不存在返回 404。
- 来源顺序固定为 `assigned/profile/task_context`。
- 名称去重后批量查询 `Skill.name.in_(names)`，避免逐个查询。

- [ ] **Step 4: 写不可用 Skill 的失败测试**

分别断言：

```text
not_found
not_approved
deleted
project_forbidden
private_forbidden
```

规则：

- 显式 assigned Skill 同样不能绕过 deleted/review 状态。
- `applicable_projects` 为空表示全局。
- 非空时必须包含 `claw.project_id`；任务项目只用于 task context 推导，不替代 Claw 项目授权。
- `visibility=private` 时必须满足 `owner_claw_id == claw.id`。

- [ ] **Step 5: 实现 Manifest**

每个可用 Skill 返回：

```python
{
    'id': skill.id,
    'name': skill.name,
    'display_name': skill.display_name,
    'content_version': descriptor['content_version'],
    'sha256': descriptor['sha256'],
    'sources': sources,
    'files': [
        {
            'path': file['path'],
            'size': file['size'],
            'sha256': file['sha256'],
            'updated_at': file['updated_at'],
            'download_url': scoped_file_url,
        }
    ],
    'pack_url': scoped_pack_url,
}
```

URL 对任务级请求保留 `ref_type/ref_id` query string。

- [ ] **Step 6: 运行服务测试**

Run:

```powershell
python -m unittest tests.test_skill_delivery -v
```

Expected: Bundle、来源、任务归属和权限测试全部 PASS。

### Task 3: Manifest 与受控下载 HTTP API

**Files:**
- Modify: `web/app/api/openclaws.py`
- Create: `tests/test_skill_delivery_api.py`

**Interfaces:**
- `GET /api/v1/openclaws/<claw_id>/skill-manifest`
- `GET /api/v1/openclaws/<claw_id>/skills/<skill_id>/files/<path:filename>`
- `GET /api/v1/openclaws/<claw_id>/skills/<skill_id>/pack`

- [ ] **Step 1: 建立 Flask/SQLite HTTP 测试夹具**

沿用 `tests/test_review_mindmap_api.py` 的扩展 stub、`Flask(__name__)`、`db.init_app`、`api_bp` 和内存 SQLite 模式。Seed：

- 两个 Project。
- 两个 OpenClawInstance，分别生成 Token。
- assigned/Profile/task Skill。
- 一个属于 Agent A 的 AgentTask。

- [ ] **Step 2: 写 Manifest 鉴权与响应失败测试**

覆盖：

- 无 Token 返回 401。
- Agent B Token 访问 Agent A URL 返回 403。
- Agent A 返回 200。
- `manifest_version == 1`。
- 响应不包含文件 `content`。
- `ref_type` 缺少 `ref_id` 返回 400。
- Agent B 的任务引用返回 403。

- [ ] **Step 3: 增加 Manifest 路由**

路由使用现有：

```python
@api_bp.route('/openclaws/<int:claw_id>/skill-manifest', methods=['GET'])
@require_claw_token
def get_skill_manifest(claw_id, claw=None):
    ...
```

API 层捕获 `SkillDeliveryError` 并返回：

```json
{"error": "<message>", "code": "<stable_code>"}
```

- [ ] **Step 4: 写单文件、ZIP 和 ETag 失败测试**

覆盖：

- 已授权 `SKILL.md` 返回 UTF-8 正文。
- 未授权 Skill 返回 403。
- ZIP 内路径统一为 `<skill.name>/<relative path>`。
- 文件与 ZIP 都返回 `ETag`、`X-Skill-Content-Version`。
- `If-None-Match` 命中返回 304。
- `../` 等非法文件路径无法下载。

- [ ] **Step 5: 实现 Agent-scoped 下载路由**

使用 `get_authorized_skill_bundle` 每次重新计算授权。不要信任 Manifest URL 本身。

Headers：

```python
{
    'ETag': f'"{sha256}"',
    'X-Skill-Content-Version': content_version,
    'Cache-Control': 'private, max-age=60',
}
```

文件 mimetype：

- `.md` → `text/markdown; charset=utf-8`
- `.json` → `application/json; charset=utf-8`
- 其他 → `text/plain; charset=utf-8`

- [ ] **Step 6: 运行 HTTP 契约测试**

Run:

```powershell
python -m unittest tests.test_skill_delivery_api -v
```

Expected: 所有鉴权、Manifest、下载和缓存测试 PASS。

### Task 4: 回归、契约文档与经验记录

**Files:**
- Modify: `docs/自研定制Agent接入Hub改造说明.md`
- Modify: `docs/经验记录.md`
- Test: `tests/test_skill_delivery.py`
- Test: `tests/test_skill_delivery_api.py`
- Test: `tests/test_skill_payload.py`
- Test: `tests/test_skill_visibility.py`
- Test: `tests/test_task_context.py`

- [ ] **Step 1: 运行 Skill 和任务上下文回归**

Run:

```powershell
python -m unittest tests.test_skill_delivery tests.test_skill_delivery_api tests.test_skill_payload tests.test_skill_visibility tests.test_task_context -v
```

Expected: 0 failures、0 errors。

- [ ] **Step 2: 更新 Agent 接入文档**

在 `docs/自研定制Agent接入Hub改造说明.md` 写入：

- Manifest URL、认证、query 参数。
- 完整响应示例。
- 自动授权和 `missing_skills` 语义。
- Agent-scoped 文件/ZIP 下载。
- ETag/SHA256 缓存流程。
- Loader 的安全解压、原子替换、blocked 行为。
- 明确旧公开接口不是新 Loader 契约。

- [ ] **Step 3: 更新经验记录**

记录：

- required Skill 名称不能由客户端自由提交，必须由 Hub Profile/task context 推导。
- Manifest 与下载必须重复鉴权，不能把 URL 当授权凭证。
- 旧 Skill 缺少 SkillFile 时必须合成 `SKILL.md`。
- Bundle 哈希必须编码路径/内容长度，避免拼接歧义。
- Manifest 不应返回正文。

- [ ] **Step 4: 检查文档和代码诊断**

使用 IDE lint 检查：

```text
web/app/services/skill_delivery.py
web/app/api/openclaws.py
tests/test_skill_delivery.py
tests/test_skill_delivery_api.py
docs/自研定制Agent接入Hub改造说明.md
docs/经验记录.md
```

Expected: 没有本次引入的诊断。

- [ ] **Step 5: 最终完整验证**

Run:

```powershell
python -m unittest tests.test_skill_delivery tests.test_skill_delivery_api tests.test_skill_payload tests.test_skill_visibility tests.test_task_context tests.test_workflows_service tests.test_workflow_worker -v
```

Expected: 0 failures、0 errors；若存在预先已有失败，必须逐条确认与本次改动无关并明确报告，不能声称全部通过。
