# Agent 记忆分层与任务存取路由 P0 实施计划

> 设计文档：`docs/superpowers/specs/2026-07-09-agent-memory-routing-design.md`
> 本计划仅覆盖 P0 两项：**经验触发接线上知识库** + **统一任务上下文包接口**。P1/P2 另立计划。

**目标：** 让任务下发时自动注入的"公共经验"来自线上知识库（跨 Agent 共享），并提供一个 `todo / agent_task / workflow_step` 三类任务统一的上下文包接口，把 `required_skills / top_pitfalls / preflight_checklist / references` 喂到 Agent 嘴边。

**架构约束：**
- `tools/collective_memory/experience_trigger.py` 保持纯函数（不依赖 Flask），新增可测试的打分函数。
- 数据库检索放在 Hub 服务层 `web/app/services/experience_trigger_service.py`，对 `app.models` 做**受保护导入**，任何异常回退到本地 seed，保证无 app context 的纯单测仍可跑。
- 复用现有 `KnowledgeEntry`（`category='pitfall'`、`status='approved'`），不建新表。

**技术栈：** Flask、SQLAlchemy、Python `unittest`。已有测试：`tests/collective_memory/test_experience_trigger.py`、`tests/test_todo_experience_trigger.py`。

---

### Task 1: pitfall 通用打分纯函数

**Files:**
- Modify: `tools/collective_memory/experience_trigger.py`
- Test: `tests/collective_memory/test_experience_trigger.py`

- [ ] **Step 1: 写失败测试**

新增测试：给定一组通用条目（含 `title`/`content`/`keywords`）与命中词，返回按命中数排序、截断到 limit 的结果。

```python
def test_rank_pitfalls_by_terms_orders_by_hit_count():
    entries = [
        {'id': 1, 'title': 'TAPD 提单字段幻觉', 'content': 'baseline_find 手打错', 'keywords': ['TAPD', 'baseline_find']},
        {'id': 2, 'title': '无关条目', 'content': 'xxx', 'keywords': ['yyy']},
        {'id': 3, 'title': 'Hub 分页陷阱', 'content': 'per_page 不生效 limit', 'keywords': ['Hub', '分页']},
    ]
    ranked = experience_trigger.rank_pitfalls_by_terms(entries, ['TAPD', 'Hub'], limit=5)
    assert [e['id'] for e in ranked] == [1, 3]
```

- [ ] **Step 2: 运行确认失败**（`AttributeError: rank_pitfalls_by_terms`）

- [ ] **Step 3: 实现 `rank_pitfalls_by_terms`**

在 `experience_trigger.py` 增加纯函数：对每个 entry 统计 `terms` 在 `title+content+keywords` 中的命中数，命中>0 才保留，按 (命中数 desc, id asc) 排序，截断 limit。返回原 entry。

- [ ] **Step 4: 运行确认通过**

---

### Task 2: 经验触发优先查线上知识库

**Files:**
- Modify: `web/app/services/experience_trigger_service.py`
- Test: `tests/test_todo_experience_trigger.py`

- [ ] **Step 1: 写失败测试**

注入一个假的 `pitfall_source`（可调用，返回候选条目），断言 `build_task_operating_context` 优先用它、seed 作兜底：

```python
def test_build_context_prefers_injected_hub_source():
    fake = [{'id': 99, 'title': '线上专属坑', 'content': 'Hub 专属', 'keywords': ['Hub'], 'status': 'approved'}]
    ctx = service.build_task_operating_context(
        '调用 Hub API', '同步配置', project='RacingGO',
        pitfall_source=lambda project, module, terms, limit: fake,
    )
    assert '线上专属坑' in ctx['pitfall_notice']
    assert 99 in ctx['matched_pitfall_ids']

def test_build_context_falls_back_to_seed_when_hub_empty():
    ctx = service.build_task_operating_context(
        '调用 Hub API', '同步配置', project='RacingGO',
        pitfall_source=lambda project, module, terms, limit: [],
    )
    assert ctx['pitfall_notice']  # seed 兜底仍有内容
```

- [ ] **Step 2: 运行确认失败**（`build_task_operating_context` 不接受 `pitfall_source`）

- [ ] **Step 3: 实现**

- 增加 `_hub_pitfall_source(project, module, terms, limit)`：受保护导入 `app.models.KnowledgeEntry` + `app.db`，查 `category='pitfall'`、`status='approved'`、`project_name=project`（+ module 可选），用 `rank_pitfalls_by_terms` 打分，映射为 `{id, title(去[踩坑]前缀), solution(用 _extract_solution_from_content), status, module}`。任何异常返回 `[]`。
- `build_task_operating_context(..., pitfall_source=None)`：`source = pitfall_source or _hub_pitfall_source`；先 `matched = source(...)`；为空则回退 seed（`search_local_pitfalls`）。其余字段不变。
- `format_trigger_notice` 已能吃 `{title, solution, status}` 结构。

- [ ] **Step 4: 运行确认通过 + 回归旧测试**

`python -m unittest tests.test_todo_experience_trigger -v`

---

### Task 3: 统一任务上下文包服务

**Files:**
- Create: `web/app/services/task_context.py`
- Test: `tests/test_task_context.py`

- [ ] **Step 1: 写失败测试**

```python
def test_build_task_context_shape():
    ctx = task_context.build_task_context_payload(
        title='TAPD 提单', description='上传缺陷截图', project='RacingGO',
        pitfall_source=lambda *a, **k: [],
    )
    for key in ('project', 'required_skills', 'primary_skill',
                'top_pitfalls', 'preflight_checklist', 'references'):
        assert key in ctx
    assert isinstance(ctx['preflight_checklist'], list) and ctx['preflight_checklist']
    assert 'basic-operations-preflight' in ctx['required_skills']
```

- [ ] **Step 2: 运行确认失败**

- [ ] **Step 3: 实现 `build_task_context_payload`**

- 调用 `build_task_operating_context` 拿 `primary_skill / trigger_terms / matched_pitfall_ids / pitfall_notice`。
- `required_skills`：`[primary_skill, 'basic-operations-preflight']` 去重（通用壳恒在）。
- `top_pitfalls`：由 matched 结构裁剪（≤3 条，solution≤120 字）。
- `preflight_checklist`：固定 5 条通用铁律的自检项（身份/读写分离/回读/查证/配置）。
- `references`：把 matched pitfall 映射为 `{type:'knowledge', id, title}`。
- 全部字段给安全默认值。

- [ ] **Step 4: 运行确认通过**

---

### Task 4: 任务上下文包 API 端点

**Files:**
- Create: `web/app/api/tasks_context.py`
- Modify: `web/app/api/__init__.py`（注册蓝图/路由，按现有约定）
- Test: `tests/test_task_context_api.py`

- [ ] **Step 1: 写失败测试**（用 Flask test client + app context）

- 造一条 `ClawTodo`，`GET /api/v1/tasks/todo/<id>/context` 返回 200 且含 `required_skills / preflight_checklist`。
- `ref_type` 非法返回 400；`ref_id` 不存在返回 404。

- [ ] **Step 2: 运行确认失败**

- [ ] **Step 3: 实现端点**

- `GET /api/v1/tasks/<ref_type>/<int:ref_id>/context`，`ref_type ∈ {todo, agent_task, workflow_step}`。
- 按类型取标题/描述/所属 claw/project，调用 `build_task_context_payload`。
- 权限：沿用现有任务读取权限（登录用户或持 token 的目标 claw）。

- [ ] **Step 4: 运行确认通过**

---

### Task 5: ClawTodo 复用统一构建（消除重复）

**Files:**
- Modify: `web/app/models.py`（`ClawTodo.to_dict` 注入段改为调用 `build_task_context_payload`，保留旧字段兼容）
- Test: `tests/test_todo_experience_trigger.py`（补一条断言 to_dict 含 `required_skills`）

- [ ] **Step 1: 写失败测试** → **Step 2: 失败** → **Step 3: 实现** → **Step 4: 通过 + 全量回归**

保留现有 `pitfall_notice / primary_skill / trigger_terms / matched_pitfall_ids` 字段不删，新增 `required_skills / preflight_checklist / references`，确保旧 Agent 不破坏。

---

### Task 6: 文档与经验记录

- [ ] 更新 `openclaw-agent/skills/knowledge-manager/SKILL.md`：新增"任务上下文包"检索路径与 5 条通用铁律引用。
- [ ] `docs/经验记录.md` 追加本次改造的接口用法与部署注意。

---

### Task 7: 部署

- [ ] 复用 `_deploy_models_only.py` 模式扩展为多文件上传脚本，上传：`experience_trigger.py`、`experience_trigger_service.py`、`task_context.py`、`tasks_context.py`、`models.py`、`api/__init__.py`。
- [ ] 远端编译 + smoke：`GET /api/v1/tasks/todo/<id>/context` 返回 200，含线上知识库命中的 pitfall。
- [ ] 验证：在知识库新建一条 `category=pitfall` 条目 → 命中关键词的新任务 context 能带出该条（不依赖 seed）。
