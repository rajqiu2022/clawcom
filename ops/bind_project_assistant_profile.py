"""Idempotently bind the versioned project-assistant Profile to one Claw.

This operator also disables enabled Skill assignments that the same Claw
cannot download (deleted, unapproved, private-owned by another Claw, or scoped
to another project). It never completes todos or starts Workflow Runs.
"""

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
    AgentPost,
    AgentPostAssignment,
    AgentProfile,
    ClawMessage,
    ClawSidecarConfig,
    OpenClawInstance,
    OpenClawSkill,
)
from app.services.skill_delivery import (  # noqa: E402
    skill_assignment_unavailable_reason,
)


PROFILE_KEY = 'project_assistant'


def _definition():
    path = ROOT / 'openclaw-agent' / 'profiles' / 'game-test-team-v1.json'
    document = json.loads(path.read_text(encoding='utf-8'))
    if document.get('schema') != 1:
        raise RuntimeError('unsupported profile document schema')
    matches = [
        item for item in document.get('profiles') or []
        if item.get('profile_key') == PROFILE_KEY
    ]
    if len(matches) != 1:
        raise RuntimeError('project assistant profile definition is missing')
    return int(document['version']), matches[0]


def apply(claw_id, project_id, actor):
    version, item = _definition()
    claw = db.session.get(OpenClawInstance, claw_id)
    if claw is None or getattr(claw, 'deleted_at', None) is not None:
        raise RuntimeError('target Claw does not exist or is deleted')
    if int(claw.project_id or 0) != int(project_id):
        raise RuntimeError('target Claw project does not match')

    changed = False
    profile = AgentProfile.query.filter_by(profile_key=PROFILE_KEY).first()
    if profile is None:
        profile = AgentProfile(profile_key=PROFILE_KEY, created_by=actor)
        db.session.add(profile)
        changed = True
    elif int(profile.version or 1) > version:
        raise RuntimeError('refusing to downgrade existing Profile')
    profile_values = {
        'name': item['name'],
        'description': item['description'],
        'system_prompt': item['system_prompt'],
        'workflow_config': item['workflow_config'],
        'required_skills_json': item['required_skills'],
        'contract_json': item['contract'],
        'version': version,
        'status': 'active',
    }
    for field, value in profile_values.items():
        if getattr(profile, field) != value:
            setattr(profile, field, value)
            changed = True
    db.session.flush()

    post_data = item['post']
    post = AgentPost.query.filter_by(
        project_id=project_id, post_key=PROFILE_KEY).first()
    if post is None:
        post = AgentPost(
            project_id=project_id, post_key=PROFILE_KEY, created_by=actor)
        db.session.add(post)
        changed = True
    post_values = {
        'name': post_data['name'],
        'description': item['description'],
        'profile_id': profile.id,
        'required_profile_version': version,
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
        'profile_version': version,
        'is_primary': True,
        'status': 'active',
        'assigned_by': actor,
    }
    for field, value in assignment_values.items():
        if getattr(assignment, field) != value:
            setattr(assignment, field, value)
            changed = True

    disabled_skills = []
    for link in OpenClawSkill.query.filter_by(
            openclaw_id=claw_id, enabled=True).all():
        reason = skill_assignment_unavailable_reason(link.skill, claw)
        if not reason:
            continue
        link.enabled = False
        disabled_skills.append({
            'skill_id': link.skill_id,
            'name': link.skill.name if link.skill else '',
            'reason': reason,
        })
        changed = True

    config = db.session.get(ClawSidecarConfig, claw_id)
    if config is None:
        raise RuntimeError('target Claw has no Sidecar config')
    if changed:
        config.config_version = int(config.config_version or 0) + 1
        config.updated_by = actor + ':project-assistant-profile'
        db.session.add(ClawMessage(
            claw_id=claw_id,
            sender_name='Hub',
            content=(
                'Hub 已绑定版本化项目助理 Profile，并清理当前实例不可下载的 '
                'Skill 关联；请从鉴权接口重新同步配置与 Skill Manifest。'
            ),
            msg_type='sync_config',
            direction='to_claw',
            status='pending',
        ))
    db.session.commit()
    return {
        'claw_id': claw.id,
        'profile_id': profile.id,
        'profile_key': profile.profile_key,
        'profile_version': profile.version,
        'post_id': post.id,
        'assignment_id': assignment.id,
        'is_primary': bool(assignment.is_primary),
        'disabled_skills': disabled_skills,
        'config_version': config.config_version,
        'changed': changed,
        'sync_config_enqueued': changed,
        'todos_completed': 0,
        'workflow_runs_started': 0,
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
            ensure_ascii=False,
            sort_keys=True,
        ))


if __name__ == '__main__':
    main()
