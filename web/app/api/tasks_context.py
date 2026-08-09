"""任务上下文包 API：todo / agent_task / workflow_step 三类统一注入结构。

GET /api/v1/tasks/<ref_type>/<int:ref_id>/context
设计文档：docs/superpowers/specs/2026-07-09-agent-memory-routing-design.md
"""
from __future__ import annotations

from flask import jsonify

from app import db
from app.api import api_bp
from app.services.task_context import build_task_context_payload

VALID_REF_TYPES = {'todo', 'agent_task', 'workflow_step'}


def _resolve_task(ref_type: str, ref_id: int):
    """返回 (title, description, claw, project, skill_policy) 或 None。"""
    from app.models import ClawTodo, AgentTask, WorkflowRunStep

    if ref_type == 'todo':
        todo = db.session.get(ClawTodo, ref_id)
        if not todo:
            return None
        claw = getattr(todo, 'openclaw', None)
        project = getattr(claw, 'project_name', None)
        return todo.title, todo.description, claw, project, {}

    if ref_type == 'agent_task':
        task = db.session.get(AgentTask, ref_id)
        if not task:
            return None
        payload = {}
        if task.payload:
            import json
            try:
                payload = json.loads(task.payload) or {}
            except Exception:
                payload = {}
        title = (payload.get('title') or payload.get('name')
                 or task.command or task.task_type or '')
        description = (payload.get('description') or payload.get('prompt')
                       or task.target_path or '')
        claw = getattr(task, 'claw', None)
        project = getattr(claw, 'project_name', None)
        policy = payload.get('skill_policy')
        if not isinstance(policy, dict):
            embedded = payload.get('task_context')
            policy = embedded if isinstance(embedded, dict) else {}
        return title, description, claw, project, policy

    if ref_type == 'workflow_step':
        step = db.session.get(WorkflowRunStep, ref_id)
        if not step:
            return None
        config = step.step_config_json or {}
        title = step.name or ''
        description = (config.get('prompt') or config.get('description') or '')
        claw = getattr(step, 'target_claw', None)
        project = None
        run = getattr(step, 'run', None)
        definition = getattr(run, 'definition', None) if run else None
        if definition is not None:
            project = getattr(definition, 'project_name', None)
        if not project:
            project = getattr(claw, 'project_name', None)
        policy = config.get('skill_policy')
        if not isinstance(policy, dict):
            embedded = config.get('task_context')
            policy = embedded if isinstance(embedded, dict) else {}
        return title, description, claw, project, policy

    return None


@api_bp.route('/tasks/<ref_type>/<int:ref_id>/context', methods=['GET'])
def get_task_context(ref_type, ref_id):
    if ref_type not in VALID_REF_TYPES:
        return jsonify({
            'error': f'不支持的任务类型：{ref_type}',
            'valid_types': sorted(VALID_REF_TYPES),
        }), 400

    resolved = _resolve_task(ref_type, ref_id)
    if resolved is None:
        return jsonify({'error': '任务不存在'}), 404

    title, description, claw, project, skill_policy = resolved
    payload = build_task_context_payload(
        title or '', description, project=project, claw=claw,
        skill_policy=skill_policy,
    )
    payload['ref_type'] = ref_type
    payload['ref_id'] = ref_id
    return jsonify(payload)
