# Workflow Four-State Orchestrator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refocus Workflow as a Hub-driven long-process task splitter that advances one clear node at a time, with four user-facing states, node references, Hub message dispatch, and manual owner recovery.

**Architecture:** Keep the existing database technical statuses for compatibility, but add service helpers that expose four UI/API states: `todo`, `running`, `blocked`, and `done`. Extend workflow step definitions with `references` metadata, add a step status transition API for agents/owners, and update the Workflow page to show task context, references, and state controls. Existing AgentTask dispatch remains available as a notification/execution channel, but Hub no longer treats subprocess timeout as the product-level definition of task failure.

**Tech Stack:** Flask, SQLAlchemy, Jinja template JavaScript, existing `web/app/services/workflows.py` pure helpers, existing `tests/test_workflows_service.py` unittest tests.

---

### Task 1: Four-State Mapping Helpers

**Files:**
- Modify: `web/app/services/workflows.py`
- Test: `tests/test_workflows_service.py`

- [ ] **Step 1: Write failing tests for display state mapping**

Add tests to `WorkflowServiceTest`:

```python
    def test_workflow_step_display_state_maps_technical_statuses(self):
        cases = {
            'pending': 'todo',
            'running': 'running',
            'retrying': 'running',
            'waiting_approval': 'running',
            'blocked': 'blocked',
            'failed': 'blocked',
            'passed': 'done',
            'skipped': 'done',
            'succeeded': 'done',
        }
        for technical, display in cases.items():
            self.assertEqual(
                workflows.workflow_step_display_state(technical),
                display,
                technical,
            )

    def test_workflow_step_display_state_defaults_to_todo(self):
        self.assertEqual(workflows.workflow_step_display_state(''), 'todo')
        self.assertEqual(workflows.workflow_step_display_state(None), 'todo')
```

- [ ] **Step 2: Run test and verify it fails**

Run: `python -m unittest tests.test_workflows_service.WorkflowServiceTest.test_workflow_step_display_state_maps_technical_statuses -v`

Expected: FAIL with `AttributeError: module 'workflows' has no attribute 'workflow_step_display_state'`.

- [ ] **Step 3: Add the mapping helper**

In `web/app/services/workflows.py`, add:

```python
DISPLAY_STEP_STATES = {
    'todo': {'pending'},
    'running': {'running', 'retrying', 'waiting_approval'},
    'blocked': {'blocked', 'failed'},
    'done': {'passed', 'skipped', 'succeeded'},
}


def workflow_step_display_state(status):
    """Map internal workflow step status to the four user-facing states."""
    status = str(status or 'pending').strip() or 'pending'
    for display_state, statuses in DISPLAY_STEP_STATES.items():
        if status in statuses:
            return display_state
    return 'todo'
```

- [ ] **Step 4: Run tests and verify pass**

Run: `python -m unittest tests.test_workflows_service.WorkflowServiceTest.test_workflow_step_display_state_maps_technical_statuses tests.test_workflows_service.WorkflowServiceTest.test_workflow_step_display_state_defaults_to_todo -v`

Expected: PASS.

### Task 2: Normalize Node References

**Files:**
- Modify: `web/app/services/workflows.py`
- Test: `tests/test_workflows_service.py`

- [ ] **Step 1: Write failing test for references normalization**

Add:

```python
    def test_normalize_definition_preserves_step_references(self):
        definition = workflows.normalize_workflow_definition({
            'key': 'demo_refs',
            'name': 'Demo References',
            'steps': [{
                'id': 'review',
                'name': '审查报告',
                'type': 'agent_task',
                'references': [
                    {'type': 'knowledge', 'id': 12, 'title': '登录坑点'},
                    {'type': 'test_report', 'id': 34, 'title': '冒烟报告'},
                    {'type': 'skill', 'path': 'openclaw-agent/skills/hub-connect/SKILL.md'},
                    {'type': 'url', 'url': 'https://example.com/spec', 'title': '外部规范'},
                    'bad',
                ],
            }],
        })
        refs = definition['steps'][0]['references']
        self.assertEqual(len(refs), 4)
        self.assertEqual(refs[0]['type'], 'knowledge')
        self.assertEqual(refs[0]['id'], 12)
        self.assertEqual(refs[2]['path'], 'openclaw-agent/skills/hub-connect/SKILL.md')
```

- [ ] **Step 2: Run test and verify it fails**

Run: `python -m unittest tests.test_workflows_service.WorkflowServiceTest.test_normalize_definition_preserves_step_references -v`

Expected: FAIL because `references` is missing.

- [ ] **Step 3: Implement reference normalization**

Add helper:

```python
VALID_REFERENCE_TYPES = {
    'knowledge',
    'test_report',
    'topic',
    'testcase',
    'skill',
    'work_rule',
    'url',
}


def normalize_step_references(value):
    """Normalize optional context references attached to a workflow node."""
    refs = []
    for item in value or []:
        if not isinstance(item, dict):
            continue
        ref_type = str(item.get('type') or '').strip()
        if ref_type not in VALID_REFERENCE_TYPES:
            continue
        ref = {'type': ref_type}
        for key in ('id', 'title', 'url', 'path', 'summary'):
            if item.get(key) not in (None, ''):
                ref[key] = item.get(key)
        refs.append(ref)
    return refs
```

In `normalize_step()`, add:

```python
        'references': normalize_step_references(step.get('references')),
```

- [ ] **Step 4: Run targeted tests**

Run: `python -m unittest tests.test_workflows_service.WorkflowServiceTest.test_normalize_definition_preserves_step_references -v`

Expected: PASS.

### Task 3: Include References and Display State in Payloads

**Files:**
- Modify: `web/app/models.py`
- Modify: `web/app/services/workflows.py`
- Test: `tests/test_workflows_service.py`

- [ ] **Step 1: Write failing payload test**

Extend `test_build_agent_task_payload_contains_workflow_context` or add:

```python
    def test_build_agent_task_payload_contains_references_and_display_state(self):
        payload = workflows.build_workflow_agent_task_payload(
            run={'id': 8, 'run_name': 'Demo', 'context': {}},
            step={
                'step_id': 'review',
                'name': '审查报告',
                'status': 'running',
                'config': {
                    'prompt': '请审查报告',
                    'references': [{'type': 'knowledge', 'id': 12, 'title': '登录坑点'}],
                },
            },
            outputs_context={},
        )
        self.assertEqual(payload['display_state'], 'running')
        self.assertEqual(payload['references'][0]['type'], 'knowledge')
        self.assertIn('请审查报告', payload['prompt'])
```

- [ ] **Step 2: Run test and verify it fails**

Run: `python -m unittest tests.test_workflows_service.WorkflowServiceTest.test_build_agent_task_payload_contains_references_and_display_state -v`

Expected: FAIL because payload lacks `display_state` and/or `references`.

- [ ] **Step 3: Update payload builder**

In `build_workflow_agent_task_payload()`, include:

```python
    config = step.get('config') if isinstance(step.get('config'), dict) else {}
    references = config.get('references') if isinstance(config.get('references'), list) else []
    payload['display_state'] = workflow_step_display_state(step.get('status'))
    payload['references'] = references
```

If the function currently builds payload inline, preserve its existing keys and only add the new keys.

- [ ] **Step 4: Add display state to step dict**

In `WorkflowRunStep.to_dict()` in `web/app/models.py`, import or locally map with a small helper is not ideal because models should avoid service imports. Add a simple property by importing lazily inside `to_dict()`:

```python
            'display_state': _workflow_step_display_state_for_model(self.status),
```

Add a model-local helper near `WorkflowRunStep`:

```python
def _workflow_step_display_state_for_model(status):
    status = str(status or 'pending').strip() or 'pending'
    if status in ('running', 'retrying', 'waiting_approval'):
        return 'running'
    if status in ('blocked', 'failed'):
        return 'blocked'
    if status in ('passed', 'skipped', 'succeeded'):
        return 'done'
    return 'todo'
```

- [ ] **Step 5: Run tests**

Run: `python -m unittest tests.test_workflows_service -v`

Expected: PASS.

### Task 4: Manual Step Status Transition API

**Files:**
- Modify: `web/app/api/workflows.py`
- Test: add or extend an existing Flask workflow API test if available; otherwise create `tests/test_workflow_step_status_api.py`.

- [ ] **Step 1: Write API behavior test**

Create a test that starts a workflow with two nodes and asserts:

```python
def test_owner_can_mark_blocked_step_running_and_done(client, logged_in_user, workflow_definition):
    run = client.post('/api/v1/workflow-runs', json={'definition_id': workflow_definition.id}).get_json()
    run_id = run['id']

    blocked = client.post(f'/api/v1/workflow-runs/{run_id}/steps/a/status', json={
        'display_state': 'blocked',
        'summary': '等待环境恢复',
    })
    assert blocked.status_code == 200
    assert blocked.get_json()['steps'][0]['display_state'] == 'blocked'

    running = client.post(f'/api/v1/workflow-runs/{run_id}/steps/a/status', json={
        'display_state': 'running',
        'summary': '环境已恢复，继续执行',
    })
    assert running.status_code == 200
    assert running.get_json()['steps'][0]['display_state'] == 'running'

    done = client.post(f'/api/v1/workflow-runs/{run_id}/steps/a/status', json={
        'display_state': 'done',
        'summary': '节点完成',
    })
    assert done.status_code == 200
    assert done.get_json()['current_step_id'] == 'b'
```

Use repository fixtures if present. If there are no API fixtures, implement this as an app-context smoke test following existing tests in `tests/test_workflow_worker.py`.

- [ ] **Step 2: Run test and verify it fails**

Run: `python -m pytest tests/test_workflow_step_status_api.py -q`

Expected: FAIL with 404 for the new status endpoint.

- [ ] **Step 3: Implement endpoint**

Add route in `web/app/api/workflows.py`:

```python
@api_bp.route('/workflow-runs/<int:run_id>/steps/<step_id>/status', methods=['POST'])
def update_workflow_step_display_status(run_id, step_id):
    err = _require_actor()
    if err:
        return err
    run = WorkflowRun.query.get_or_404(run_id)
    if run.definition and not _definition_visible(run.definition):
        return jsonify({'error': 'workflow run 不存在'}), 404
    if not (_can_manage_definition(run.definition) if run.definition else is_admin_user()):
        claw = get_current_claw()
        step = WorkflowRunStep.query.filter_by(run_id=run_id, step_id=step_id).first_or_404()
        if not (claw and _step_belongs_to_claw(step, claw.id)):
            return jsonify({'error': '只有节点执行 Agent、owner 或管理员可以修改节点状态'}), 403
    step = WorkflowRunStep.query.filter_by(run_id=run_id, step_id=step_id).first_or_404()
    data = request.get_json() or {}
    display_state = str(data.get('display_state') or data.get('state') or '').strip()
    if display_state not in ('todo', 'running', 'blocked', 'done'):
        return jsonify({'error': 'display_state 必须是 todo/running/blocked/done'}), 400
    _apply_manual_step_display_status(run, step, display_state, data)
    _recompute_run_status(run, _actor_name())
    db.session.commit()
    return jsonify(_run_payload(run, with_steps=True))
```

Add helper:

```python
def _apply_manual_step_display_status(run, step, display_state, data):
    now = datetime.now()
    summary = data.get('summary') or ''
    if display_state == 'todo':
        step.status = 'pending'
        step.started_at = None
        step.finished_at = None
        step.blocker_json = {}
    elif display_state == 'running':
        step.status = 'running'
        step.started_at = step.started_at or now
        step.finished_at = None
        step.dispatched_at = step.dispatched_at or now
        step.blocker_json = {}
        step.health_status = 'stale'
        step.progress_at = now
        step.progress_by = _actor_name()
        step.progress_phase = 'manual_running'
        step.progress_message = summary or '已手动设为执行中'
    elif display_state == 'blocked':
        step.status = 'blocked'
        step.finished_at = now
        step.blocker_json = data.get('blocker') if isinstance(data.get('blocker'), dict) else {
            'type': 'manual_blocked',
            'message': summary or '节点被手动标记为阻断',
        }
    elif display_state == 'done':
        step.status = 'passed'
        step.finished_at = now
        step.blocker_json = {}
        step.progress_percent = 100
    step.summary = summary
    step.updated_by = _actor_name()
```

- [ ] **Step 4: Ensure running transition dispatches Hub message**

After setting `running`, call `_dispatch_step_message(step)` so the target Agent or owner gets a Hub message. Guard against duplicate messages by existing `_dispatch_agent_start_message()` duplicate check.

- [ ] **Step 5: Run tests**

Run:

```bash
python -m unittest tests.test_workflows_service -v
python -m pytest tests/test_workflow_step_status_api.py -q
```

Expected: PASS.

### Task 5: Notify Target Agent or Owner

**Files:**
- Modify: `web/app/api/workflows.py`
- Test: `tests/test_workflow_step_status_api.py` or a focused app-context test.

- [ ] **Step 1: Write failing test for owner fallback**

Test that a running node without `target_claw_id` creates a `ClawMessage` to the workflow owner or bound owner claw if available. Assertion should verify `msg_type='task_delegate'` and content includes run id, step id, and task summary.

- [ ] **Step 2: Implement owner fallback**

Refactor `_dispatch_step_message(step)`:

```python
def _step_message_target_claw_id(step):
    if step.target_claw_id:
        return step.target_claw_id
    definition = step.run.definition if step.run else None
    if definition and definition.owner_type == 'claw' and definition.owner_id:
        return definition.owner_id
    if definition and definition.owner_type == 'user' and definition.owner_id:
        user = User.query.get(definition.owner_id)
        if user and user.bound_claw_id:
            return user.bound_claw_id
    return None
```

Then use this target in `_dispatch_step_message()` and `_dispatch_agent_task()`. If no target claw exists, write `step.progress_message = '节点已进入执行中，但未找到可通知的 Agent/owner'`.

- [ ] **Step 3: Include references in the message content**

Append a compact reference section:

```python
refs = config.get('references') if isinstance(config.get('references'), list) else []
if refs:
    ref_lines = ['参考内容：'] + [
        '- {type}: {title}'.format(
            type=ref.get('type', '-'),
            title=ref.get('title') or ref.get('url') or ref.get('path') or ref.get('id') or '-',
        )
        for ref in refs[:8]
    ]
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_workflow_step_status_api.py -q`

Expected: PASS.

### Task 6: Frontend Four-State Display and Controls

**Files:**
- Modify: `web/templates/workflows.html`

- [ ] **Step 1: Update labels**

Change `statusLabel()` to prefer `display_state` where available:

```javascript
function displayStateLabel(s) {
    return ({todo:'待开始', running:'执行中', blocked:'阻断', done:'结束'})[s] || s || '待开始';
}
function stepDisplayState(step) {
    return step.display_state || ({
        pending:'todo',
        running:'running',
        retrying:'running',
        waiting_approval:'running',
        blocked:'blocked',
        failed:'blocked',
        passed:'done',
        skipped:'done',
        succeeded:'done',
    })[step.status || 'pending'] || 'todo';
}
```

Keep run-level labels as they are, but step cards should use `displayStateLabel(stepDisplayState(step))`.

- [ ] **Step 2: Add state action buttons**

In `renderRunStepInspector(step)`, add buttons:

```javascript
${renderStepStateActions(step)}
```

Implement:

```javascript
function renderStepStateActions(step) {
    const state = stepDisplayState(step);
    const buttons = [];
    if (state !== 'running') buttons.push(`<button class="btn btn-primary btn-sm" onclick="changeStepState(${currentRunData.id}, '${esc(step.step_id)}', 'running')">设为执行中</button>`);
    if (state !== 'blocked') buttons.push(`<button class="btn btn-ghost btn-sm" onclick="changeStepState(${currentRunData.id}, '${esc(step.step_id)}', 'blocked')">标记阻断</button>`);
    if (state !== 'done') buttons.push(`<button class="btn btn-ghost btn-sm" onclick="changeStepState(${currentRunData.id}, '${esc(step.step_id)}', 'done')">标记结束</button>`);
    return buttons.join('');
}
```

- [ ] **Step 3: Add API call**

```javascript
async function changeStepState(runId, stepId, displayState) {
    const label = displayStateLabel(displayState);
    const summary = await customPrompt({
        title: `将节点设为${label}`,
        msg: '请输入本次状态变更说明',
        placeholder: '例如：环境已恢复，继续执行',
        defaultValue: '',
    });
    if (summary === null) return;
    try {
        await api(`/workflow-runs/${runId}/steps/${encodeURIComponent(stepId)}/status`, {
            method: 'POST',
            body: JSON.stringify({display_state: displayState, summary})
        });
        await openRun(runId);
    } catch (e) {
        await customAlert('状态变更失败：' + e.message, 'error');
    }
}
```

If `customPrompt` does not exist, use the repository's modal helper pattern or `window.prompt()` as a minimal fallback.

- [ ] **Step 4: Show references**

In `renderRunStepInspector(step)`, read:

```javascript
const refs = (step.config && step.config.references) || [];
```

Render:

```javascript
function renderStepReferences(refs) {
    if (!refs || !refs.length) return '<div class="wf-meta">暂无参考内容</div>';
    return refs.map(ref => {
        const title = ref.title || ref.url || ref.path || ref.id || '-';
        const href = ref.url || '';
        const label = `${ref.type || 'ref'}：${title}`;
        return href
            ? `<a class="wf-artifact" href="${esc(href)}" target="_blank">${esc(label)}</a>`
            : `<span class="wf-artifact">${esc(label)}</span>`;
    }).join('');
}
```

- [ ] **Step 5: Smoke check page script**

Extract inline script and run `node --check` as done previously:

```bash
python -c "from pathlib import Path; import re; html=Path('web/templates/workflows.html').read_text(encoding='utf-8'); scripts='\n'.join(re.findall(r'<script>(.*?)</script>', html, re.S)); Path('_workflow_inline.js').write_text(scripts, encoding='utf-8')"
node --check _workflow_inline.js
```

Expected: no syntax errors. Delete `_workflow_inline.js` after check.

### Task 7: Stop Treating Agent Timeout as Product-Level Failure

**Files:**
- Modify: `web/app/api/agent_client.py`
- Modify: `web/app/api/workflows.py`
- Test: extend worker/agent task test.

- [ ] **Step 1: Write failing test**

Create a test where a `workflow_agent_task` fails with `subprocess timeout 330s`, and assert the step remains `running` with progress/blocker detail rather than becoming product-level `blocked`, unless the agent explicitly posts `status=blocked` to the result API.

- [ ] **Step 2: Adjust AgentTask failure handling**

Where sidecar task failures currently write a synthetic workflow result with `status='blocked'`, change timeout handling to:

```python
if task.task_type == 'workflow_agent_task' and 'timeout' in error_text.lower():
    step.progress_phase = 'agent_task_timeout'
    step.progress_message = 'AgentTask 执行通道超时，等待 Agent 或 owner 继续处理'
    step.logs_json = dict(step.logs_json or {}, agent_task_error=error_text)
    # Keep step.status running; do not force product blocked.
```

Keep explicit agent result `status='blocked'` behavior unchanged.

- [ ] **Step 3: Add owner/manual path expectation**

The UI status action from Task 6 becomes the supported way to mark blocked or resume after an infrastructure timeout.

- [ ] **Step 4: Run workflow tests**

Run:

```bash
python -m unittest tests.test_workflows_service -v
python -m pytest tests/test_workflow_worker.py -q
```

Expected: PASS.

### Task 8: Documentation and Experience Record

**Files:**
- Modify: `docs/经验记录.md`
- Optional Modify: `docs/Hub_Workflow自动化编排需求案.md` if the document exists and is still current.

- [ ] **Step 1: Add experience record**

Append:

```markdown
**Workflow 职责收敛为四态 Hub 编排器（2026-07-07）**：

- 背景：Workflow 不应把长流程复杂任务整体塞给单个 Agent，也不应由 Hub 的 subprocess timeout 决定业务失败；它的核心职责是拆解节点、附带上下文、通知目标 Agent/owner、等待节点明确回写。
- 修复：节点对外收敛为待开始/执行中/阻断/结束四态；保留内部技术状态兼容历史；节点支持 references 引用知识库、测试报告、课题、用例、Skill、规范等；页面支持 owner 手动改状态和阻断恢复。
- 经验：长流程自动化要把“执行任务”和“编排推进”分离。Hub 管状态和上下文，Agent 管单节点执行与监控；超时属于执行通道信号，不等同于业务阻断。
```

- [ ] **Step 2: Run verification**

Run:

```bash
python -m unittest tests.test_workflows_service -v
python -m pytest tests/test_workflow_worker.py -q
python -m py_compile web/app/services/workflows.py web/app/api/workflows.py web/app/api/agent_client.py web/app/models.py
```

Expected: all pass.

### Task 9: Deployment

**Files:**
- Create or update deployment script for this feature.

- [ ] **Step 1: Create deployment script**

Create `_deploy_workflow_four_state_orchestrator.py` based on existing paramiko deployment scripts. Upload:

```python
FILES = [
    ('web/app/models.py', 'app/models.py'),
    ('web/app/services/workflows.py', 'app/services/workflows.py'),
    ('web/app/api/workflows.py', 'app/api/workflows.py'),
    ('web/app/api/agent_client.py', 'app/api/agent_client.py'),
    ('web/templates/workflows.html', 'templates/workflows.html'),
]
```

- [ ] **Step 2: Compile remotely**

Run remote:

```bash
cd /opt/openclaw-web
./venv/bin/python -m py_compile app/models.py app/services/workflows.py app/api/workflows.py app/api/agent_client.py
```

- [ ] **Step 3: Restart and smoke test**

Run:

```bash
systemctl restart openclaw-web
sleep 3
systemctl is-active openclaw-web
```

Then use Flask test client to create a small workflow run, change a node through the four-state API, and verify it advances to the next node when marked `done`.

---

## Self-Review Notes

- Spec coverage: covers four-state display, Hub notification, owner fallback, node references, manual status changes, blocked-to-running recovery, and timeout demotion.
- Placeholder scan: no implementation placeholders remain; each task names files, test commands, and expected behavior.
- Scope check: this plan intentionally avoids a full visual workflow editor rewrite. It changes the runtime model and current detail page first.
- Risk: existing sidecars may still post `status=blocked`; that behavior remains respected because explicit agent blocker is different from infrastructure timeout.
