"""Idempotently bind the read-only source analyst Profile to one Claw."""

from __future__ import print_function

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / 'web'
if str(WEB) not in sys.path:
    sys.path.insert(0, str(WEB))

from app import create_app, db  # noqa: E402
from app.models import (  # noqa: E402
    AgentPost, AgentPostAssignment, AgentProfile, AuditLog, ClawMessage,
    ClawSidecarConfig, OpenClawInstance,
)


PROFILE_KEY = 'readonly_source_code_analyst'
PROFILE_VERSION = 1


def _profile_values():
    return {
        'name': 'RacingGO 只读源码分析专员',
        'description': '读取项目已授权源码，完成变更、风险和需求关联分析；不修改源码或共享环境。',
        'system_prompt': (
            '你是 RacingGO 只读源码分析专员。只可使用 Hub 下发的受控远程源码能力读取已授权仓库；'
            '不得提交、推送、合并、部署、修改分支、工作区或凭据。结论必须区分代码事实、推断与未知，'
            '引用仓库、revision、文件路径和行号；证据不足时明确标记，不得猜测。'
        ),
        'workflow_config': (
            '1. 先确认任务范围、仓库与基线；2. 通过受控只读接口检索和读取；'
            '3. 输出变更摘要、影响链、风险、测试建议和证据引用；'
            '4. 需要写操作时只提交建议或 Owner Gate，不自行变更。'
        ),
        'required_skills_json': [
            'hub-connect', 'engineering-analysis',
            'agent-team-member-activity',
        ],
        'contract_json': {
            'role_key': 'code_analyst',
            'source_access': 'read_only',
            'external_mutations_allowed': False,
            'required_outputs': [
                'summary', 'risk_items', 'test_recommendations',
                'source_evidence',
            ],
            'evidence_fields': [
                'source_id', 'revision', 'path', 'line_or_symbol',
            ],
        },
        'version': PROFILE_VERSION,
        'status': 'active',
    }


def apply(claw_id, project_id, actor):
    claw = db.session.get(OpenClawInstance, claw_id)
    if claw is None or getattr(claw, 'deleted_at', None) is not None:
        raise RuntimeError('target Claw does not exist or is deleted')
    if int(claw.project_id or 0) != int(project_id):
        raise RuntimeError('target Claw project does not match')
    config = db.session.get(ClawSidecarConfig, claw_id)
    if config is None:
        raise RuntimeError('target Claw has no Sidecar config')
    policy = dict(config.system_context_policy_json or {})
    source_ids = policy.get('remote_source_ids')
    if not isinstance(source_ids, list) or not source_ids:
        raise RuntimeError('target Claw has no Hub-managed remote source scope')

    changed = False
    profile = AgentProfile.query.filter_by(profile_key=PROFILE_KEY).first()
    if profile is None:
        profile = AgentProfile(profile_key=PROFILE_KEY, created_by=actor)
        db.session.add(profile)
        changed = True
    elif int(profile.version or 1) > PROFILE_VERSION:
        raise RuntimeError('refusing to downgrade existing Profile')
    for field, value in _profile_values().items():
        if getattr(profile, field) != value:
            setattr(profile, field, value)
            changed = True
    db.session.flush()

    post = AgentPost.query.filter_by(
        project_id=project_id, post_key=PROFILE_KEY).first()
    if post is None:
        post = AgentPost(
            project_id=project_id, post_key=PROFILE_KEY, created_by=actor)
        db.session.add(post)
        changed = True
    post_values = {
        'name': '只读源码分析专员',
        'description': profile.description,
        'profile_id': profile.id,
        'required_profile_version': PROFILE_VERSION,
        'status': 'active',
    }
    for field, value in post_values.items():
        if getattr(post, field) != value:
            setattr(post, field, value)
            changed = True
    db.session.flush()

    for other in AgentPostAssignment.query.filter_by(
            claw_id=claw_id, status='active').all():
        desired = other.post_id == post.id
        if bool(other.is_primary) != desired:
            other.is_primary = desired
            changed = True
    assignment = AgentPostAssignment.query.filter_by(
        post_id=post.id, claw_id=claw_id).first()
    if assignment is None:
        assignment = AgentPostAssignment(
            post_id=post.id, claw_id=claw_id, assigned_by=actor)
        db.session.add(assignment)
        changed = True
    assignment_values = {
        'profile_version': PROFILE_VERSION,
        'is_primary': True,
        'status': 'active',
        'assigned_by': actor,
    }
    for field, value in assignment_values.items():
        if getattr(assignment, field) != value:
            setattr(assignment, field, value)
            changed = True

    if changed:
        config.config_version = int(config.config_version or 0) + 1
        config.updated_by = actor + ':readonly-source-profile'
        db.session.add(ClawMessage(
            claw_id=claw_id, sender_name='Hub', msg_type='sync_config',
            direction='to_claw', status='pending',
            content=('Hub 已绑定只读源码分析 Profile；请重新同步配置。源码范围沿用 '
                     'Hub 受控 remote_source_ids，不授予仓库写权限。')))
        db.session.add(AuditLog(
            action='bind', resource_type='agent_profile',
            resource_id=profile.id, resource_name=PROFILE_KEY,
            operator=actor, ip_address='', detail=json.dumps({
                'claw_id': claw_id, 'project_id': project_id,
                'source_ids': source_ids, 'access': 'read_only',
            }, ensure_ascii=False, sort_keys=True)))
    db.session.commit()
    return {
        'claw_id': claw_id,
        'profile_id': profile.id,
        'profile_key': PROFILE_KEY,
        'profile_version': profile.version,
        'post_id': post.id,
        'assignment_id': assignment.id,
        'remote_source_ids': source_ids,
        'config_version': config.config_version,
        'changed': changed,
        'sync_config_enqueued': changed,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--claw-id', type=int, required=True)
    parser.add_argument('--project-id', type=int, required=True)
    parser.add_argument('--actor', default='codex-deploy')
    args = parser.parse_args()
    app = create_app()
    with app.app_context():
        print(json.dumps(
            apply(args.claw_id, args.project_id, args.actor),
            ensure_ascii=False, sort_keys=True))


if __name__ == '__main__':
    main()
