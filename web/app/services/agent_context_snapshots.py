"""Immutable identity/role contracts attached to Workflow Runs.

Hub is the authority for these documents.  They contain no credentials and
must never be rebuilt for an existing Run: configuration changes only affect
the next Run.
"""

from __future__ import annotations

import hashlib
import json
from uuid import uuid4

from app import db
from app.models import (
    AgentContextSnapshot,
    AgentPostAssignment,
    AgentTeam,
    ClawSidecarConfig,
    OpenClawInstance,
    OpenClawRule,
    OpenClawSkill,
)
from app.services.worker_runtime import runtime_summary


SCHEMA = 'hub.agent_context_snapshot@1'

TEST_MANAGER_CONTRACT = {
    'role_key': 'test_manager',
    'display_name': '测试经理',
    'purpose': '负责团队整体测试管理、受控调度和结果验收，不是默认执行者。',
    'responsibilities': [
        '理解测试目标，创建计划并把工作拆分为可验收任务',
        '优先调度团队内已有项目助理、代码分析员和测试执行员，不凭名称猜测身份',
        '跟踪任务进度、阻断和恢复，必要时重新派发但不得绕过 Hub 团队角色、阶段绑定与权限',
        '检查执行证据、分析结论和报告完整性，不合格结果必须退回',
        '汇总团队结果，给出结论并完成测试闭环',
    ],
    'forbidden': [
        '在已有合格执行员时把自己当作默认执行员',
        '替执行员伪造 Unity、设备、性能或客户端执行证据',
        '同时充当同一交付物的执行者与独立评审者',
        '仅凭聊天回复把任务判定为完成',
    ],
    'dispatch_policy': {
        'requires_team_manager_assignment': True,
        'allowed_targets': ['project_assistant', 'code_analyst', 'test_executor'],
        'result_check_required': True,
    },
}

ROLE_CONTRACTS = {
    'test_manager': TEST_MANAGER_CONTRACT,
    'project_assistant': {
        'role_key': 'project_assistant', 'display_name': '项目助理',
        'purpose': '辅助测试经理收集版本与构建数据、整理团队运行信息并跟进协作事项。',
        'responsibilities': [
            '收集并核对版本号、分支、提交、构建包、环境和发布时间等版本数据',
            '整理测试计划、成员状态、任务进展、阻断和待办，及时向测试经理反馈缺口',
            '维护可追溯的数据来源和引用，不把聊天描述冒充为执行证据',
            '按测试经理派发的任务执行团队已授权 Flow，并提交结构化结果',
        ],
        'forbidden': [
            '代替测试经理行使团队调度、结果验收或成员权限配置权',
            '擅自改变团队编制、Flow 授权、监督状态或测试结论',
            '伪造版本、构建、进度或执行证据',
            '读取、转发或落盘未授权的凭据和密钥',
        ],
    },
    'code_analyst': {
        'role_key': 'code_analyst', 'display_name': '代码分析员',
        'purpose': '负责需求和代码分析、风险识别及执行建议，不产生虚假执行证据。',
    },
    'test_executor': {
        'role_key': 'test_executor', 'display_name': '测试执行员',
        'purpose': '按已分配平台和专项执行测试，提交可追溯证据与结构化结果。',
    },
}


class ContextSnapshotError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), default=str).encode('utf-8')


def _sha(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _resource_version(updated_at, digest):
    timestamp = updated_at.isoformat() if updated_at else ''
    return '%s:%s' % (timestamp, digest[:12])


def _profile_snapshots(claw_id):
    result = []
    rows = AgentPostAssignment.query.filter_by(
        claw_id=claw_id, status='active').order_by(
            AgentPostAssignment.is_primary.desc(),
            AgentPostAssignment.id.asc()).all()
    for row in rows:
        post = row.post
        profile = post.profile if post else None
        if not post or not profile or post.status != 'active' or profile.status != 'active':
            continue
        content = {
            'system_prompt': profile.system_prompt or '',
            'workflow_config': profile.workflow_config or '',
            'contract': profile.contract_json or {},
            'required_skills': profile.required_skills_json or [],
        }
        result.append({
            'post_id': post.id,
            'post_key': post.post_key,
            'post_name': post.name,
            'profile_id': profile.id,
            'profile_key': profile.profile_key,
            'profile_version': int(profile.version or 1),
            'assigned_profile_version': int(row.profile_version or 1),
            'required_profile_version': int(post.required_profile_version or 1),
            'is_primary': bool(row.is_primary),
            'content_sha256': _sha(content),
            'content': content,
        })
    return result


def _rule_snapshots(claw_id):
    result = []
    rows = OpenClawRule.query.filter_by(
        openclaw_id=claw_id, enabled=True).order_by(
            OpenClawRule.rule_id.asc()).all()
    for row in rows:
        rule = row.rule
        if (not rule or rule.is_deleted
                or str(rule.review_status or 'approved') != 'approved'):
            continue
        content = rule.content_template or ''
        digest = hashlib.sha256(content.encode('utf-8')).hexdigest()
        result.append({
            'rule_id': rule.id,
            'name': rule.name,
            'display_name': rule.display_name,
            'version': _resource_version(rule.updated_at, digest),
            'sha256': digest,
            'content': content,
        })
    return result


def _skill_snapshots(claw_id):
    result = []
    rows = OpenClawSkill.query.filter_by(
        openclaw_id=claw_id, enabled=True).order_by(
            OpenClawSkill.skill_id.asc()).all()
    for row in rows:
        skill = row.skill
        if (not skill or skill.is_deleted
                or str(skill.review_status or 'approved') != 'approved'):
            continue
        files = [{
            'filename': item.filename,
            'sha256': hashlib.sha256(
                (item.content or '').encode('utf-8')).hexdigest(),
        } for item in sorted(
            skill.file_entries.all(), key=lambda value: value.filename or '')]
        manifest = {
            'template_content_sha256': hashlib.sha256(
                (skill.template_content or '').encode('utf-8')).hexdigest(),
            'files': files,
        }
        digest = _sha(manifest)
        result.append({
            'skill_id': skill.id,
            'name': skill.name,
            'display_name': skill.display_name,
            'version': _resource_version(skill.updated_at, digest),
            'sha256': digest,
            'installation_generation': row.installation_generation or '',
            'installation_status': row.installation_status or '',
            'manifest': manifest,
        })
    return result


def _agent_snapshot(claw_id, role_key, specialties=None):
    claw = db.session.get(OpenClawInstance, claw_id)
    if not claw or claw.status == 'deleted':
        raise ContextSnapshotError(
            'POLICY_CONTEXT_INCOMPLETE', '上下文中的 Agent 不存在或已删除')
    config = db.session.get(ClawSidecarConfig, claw_id)
    runtime = runtime_summary(
        config.runtime_config_json or {}, config.config_owner or 'hub') if config else {}
    role_contract = ROLE_CONTRACTS.get(role_key, {'role_key': role_key})
    return {
        'claw_id': claw.id,
        'claw_name': claw.name,
        'role_key': role_key,
        'role_contract_version': 1,
        'role_contract_sha256': _sha(role_contract),
        'role_contract': role_contract,
        'specialties': sorted(set(specialties or [])),
        'config_version': int(config.config_version or 1) if config else None,
        'runtime': runtime,
        'profiles': _profile_snapshots(claw.id),
        'rules': _rule_snapshots(claw.id),
        'skills': _skill_snapshots(claw.id),
    }


def resolve_team_for_run(project_id, definition_id, manager_claw_id):
    """Return one unambiguous active team selected by its manager."""
    if not project_id or not definition_id or not manager_claw_id:
        return None
    candidates = AgentTeam.query.filter(
        AgentTeam.project_id == project_id,
        AgentTeam.status == 'active',
        db.or_(
            AgentTeam.primary_manager_claw_id == manager_claw_id,
            AgentTeam.backup_manager_claw_id == manager_claw_id,
        )).all()
    matches = [row for row in candidates if definition_id in {
        int(item) for item in (row.policy_json or {}).get(
            'allowed_definition_ids', []) if str(item).isdigit()}]
    return matches[0] if len(matches) == 1 else None


def _assignment_ids(run, manager_claw_id=None):
    context = run.context_json if isinstance(run.context_json, dict) else {}
    assignment = context.get('assignment_snapshot')
    assignment = assignment if isinstance(assignment, dict) else {}
    workflow_start = context.get('workflow_start')
    workflow_start = workflow_start if isinstance(workflow_start, dict) else {}
    mission = context.get('mission')
    mission = mission if isinstance(mission, dict) else {}

    def integer(*values):
        for value in values:
            try:
                value = int(value)
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
        return None

    return {
        'manager_claw_id': integer(
            manager_claw_id, mission.get('dispatch_manager_claw_id'),
            mission.get('main_claw_id')),
        'executor_claw_id': integer(
            assignment.get('executor_claw_id'),
            workflow_start.get('executor_claw_id'),
            workflow_start.get('worker_claw_id'), mission.get('worker_claw_id')),
        'reviewer_claw_id': integer(
            assignment.get('reviewer_claw_id'),
            workflow_start.get('reviewer_claw_id')),
    }


def freeze_run_context(run, *, team=None, manager_claw_id=None):
    """Freeze one immutable context and attach its id/digest to ``run``."""
    if run.context_snapshot_id:
        return AgentContextSnapshot.query.filter_by(
            snapshot_id=run.context_snapshot_id).first()
    assignment = _assignment_ids(run, manager_claw_id)
    manager_id = assignment['manager_claw_id']
    executor_id = assignment['executor_claw_id']
    reviewer_id = assignment['reviewer_claw_id']
    if (executor_id and reviewer_id and executor_id == reviewer_id
            and run.trigger_source != 'workflow_external_review'):
        raise ContextSnapshotError(
            'POLICY_ROLE_OVERLAP', '同一 Run 的执行者与独立评审者必须不同')
    if team and manager_id and executor_id and manager_id == executor_id:
        raise ContextSnapshotError(
            'POLICY_ROLE_OVERLAP', '测试经理负责调度和结果检查，不能作为默认执行者')

    roster = []
    role_by_claw = {}
    if team:
        role_by_claw[team.primary_manager_claw_id] = ('test_manager', [])
        if team.backup_manager_claw_id:
            role_by_claw[team.backup_manager_claw_id] = ('test_manager', [])
        for member in sorted(team.members, key=lambda item: item.id or 0):
            role_by_claw[member.claw_id] = (
                member.role_key, list(member.specialties_json or []))
        roster = [{
            'claw_id': claw_id,
            'role_key': value[0],
            'specialties': value[1],
        } for claw_id, value in sorted(role_by_claw.items())]

    participant_ids = set(value for value in assignment.values() if value)
    if team:
        participant_ids.update(role_by_claw)
    participants = []
    for claw_id in sorted(participant_ids):
        role_key, specialties = role_by_claw.get(claw_id, ('workflow_actor', []))
        participants.append(_agent_snapshot(claw_id, role_key, specialties))

    document = {
        'schema': SCHEMA,
        'workflow_run_id': run.id,
        'workflow_definition_id': run.definition_id,
        'project_id': run.project_id,
        'team': ({
            'team_id': team.id,
            'name': team.name,
            'version': int(team.version or 1),
            'objective': team.objective,
            'primary_manager_claw_id': team.primary_manager_claw_id,
            'backup_manager_claw_id': team.backup_manager_claw_id,
            'active_manager_claw_id': (
                team.primary_manager_claw_id
                if team.status == 'active' else None),
            'manager_epoch': int(team.manager_epoch or 0),
            'manager_authority_mode': 'team_role_assignment',
            'roster': roster,
        } if team else None),
        'assignment': assignment,
        'participants': participants,
        'policy': {
            'hub_is_identity_authority': True,
            'manager_is_default_executor': False,
            'executor_reviewer_must_differ': True,
            'result_check_required': bool(team),
        },
    }
    digest = _sha(document)
    snapshot = AgentContextSnapshot(
        snapshot_id='ctx_' + uuid4().hex,
        schema_version=1,
        workflow_run_id=run.id,
        project_id=run.project_id,
        team_id=team.id if team else None,
        context_sha256=digest,
        context_json=document,
    )
    db.session.add(snapshot)
    run.context_snapshot_id = snapshot.snapshot_id
    run.context_snapshot_sha256 = digest
    context = dict(run.context_json or {})
    context['agent_context_snapshot'] = {
        'schema': SCHEMA,
        'snapshot_id': snapshot.snapshot_id,
        'sha256': digest,
        'team_id': team.id if team else None,
    }
    run.context_json = context
    return snapshot


def inherit_run_context(task, run):
    """Copy immutable identity reference to an AgentTask."""
    if not run or not run.context_snapshot_id:
        return task
    task.context_snapshot_id = run.context_snapshot_id
    task.context_snapshot_sha256 = run.context_snapshot_sha256 or ''
    return task
