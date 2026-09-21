"""Agent Skill 下发清单、授权与文档包构建。"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote, urlencode


VALID_REF_TYPES = frozenset({'todo', 'agent_task', 'workflow_step'})
SOURCE_ORDER = ('assigned', 'profile', 'task_context')


class SkillDeliveryError(Exception):
    def __init__(self, code: str, message: str, status_code: int):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value or '')


def _normalize_path(filename: Any) -> str:
    raw = str(filename or '').replace('\\', '/')
    if raw.startswith('/') or ':' in raw.split('/', 1)[0]:
        raise ValueError(f'非法 Skill 文件路径：{filename}')
    path = PurePosixPath(raw)
    if not raw or raw in ('.', '..') or '..' in path.parts:
        raise ValueError(f'非法 Skill 文件路径：{filename}')
    return str(path)


def _file_descriptor(path: str, content: Any, updated_at: Any) -> dict[str, Any]:
    text = str(content or '')
    encoded = text.encode('utf-8')
    return {
        'path': path,
        'content': text,
        'size': len(encoded),
        'sha256': hashlib.sha256(encoded).hexdigest(),
        'updated_at': _iso(updated_at),
    }


def normalized_skill_files(skill) -> list[dict[str, Any]]:
    """返回路径排序、包含稳定摘要的 Skill 文件列表。"""
    rows = list(getattr(skill, 'file_entries', []) or [])
    by_path: dict[str, dict[str, Any]] = {}
    for row in rows:
        path = _normalize_path(getattr(row, 'filename', ''))
        if path in by_path:
            raise ValueError(f'Skill 文件路径规范化后重复：{path}')
        by_path[path] = _file_descriptor(
            path,
            getattr(row, 'content', ''),
            getattr(row, 'updated_at', None) or getattr(skill, 'updated_at', None),
        )

    template_content = getattr(skill, 'template_content', '') or ''
    if 'SKILL.md' not in by_path and template_content:
        by_path['SKILL.md'] = _file_descriptor(
            'SKILL.md',
            template_content,
            getattr(skill, 'updated_at', None),
        )
    return [by_path[path] for path in sorted(by_path)]


def skill_bundle_descriptor(skill) -> dict[str, Any]:
    """构建 Skill 文档包版本、文件摘要和 Bundle 摘要。"""
    files = normalized_skill_files(skill)
    hasher = hashlib.sha256()
    for item in files:
        path_bytes = item['path'].encode('utf-8')
        content_bytes = item['content'].encode('utf-8')
        hasher.update(len(path_bytes).to_bytes(8, 'big'))
        hasher.update(path_bytes)
        hasher.update(len(content_bytes).to_bytes(8, 'big'))
        hasher.update(content_bytes)

    timestamps = [
        item['updated_at'] for item in files if item.get('updated_at')
    ]
    skill_updated = _iso(getattr(skill, 'updated_at', None))
    if skill_updated:
        timestamps.append(skill_updated)
    return {
        'files': files,
        'sha256': hasher.hexdigest(),
        'content_version': max(timestamps) if timestamps else '',
    }


def _required_skill_name(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get('name') or value.get('skill') or value.get('key')
    return str(value or '').strip()


def _active_profile_skill_names(claw) -> list[str]:
    from app.models import AgentPostAssignment

    assignments = AgentPostAssignment.query.filter_by(
        claw_id=claw.id,
        status='active',
    ).all()
    valid = []
    for assignment in assignments:
        post = assignment.post
        profile = post.profile if post else None
        if not post or not profile:
            continue
        if (post.status or 'active') != 'active':
            continue
        if (profile.status or 'active') != 'active':
            continue
        valid.append(assignment)
    if not valid:
        return []
    active = sorted(
        valid,
        key=lambda item: (not bool(item.is_primary), item.id or 0),
    )[0]
    values = active.post.profile.required_skills_json or []
    return [
        name for name in (_required_skill_name(value) for value in values)
        if name
    ]


def resolve_task_context(ref_type: str, ref_id: int, claw) -> dict[str, Any]:
    if ref_type not in VALID_REF_TYPES:
        raise SkillDeliveryError(
            'invalid_ref_type',
            f'不支持的任务类型：{ref_type}',
            400,
        )
    from app.api.tasks_context import _resolve_task
    from app.services.task_context import build_task_context_payload

    resolved = _resolve_task(ref_type, ref_id)
    if resolved is None:
        raise SkillDeliveryError('task_not_found', '任务不存在', 404)
    title, description, task_claw, project, skill_policy = resolved
    if not task_claw or task_claw.id != claw.id:
        raise SkillDeliveryError(
            'task_forbidden',
            '任务不属于当前 Agent',
            403,
        )
    return build_task_context_payload(
        title or '',
        description,
        project=project,
        claw=task_claw,
        skill_policy=skill_policy,
    )


def _source_names(
    claw,
    ref_type=None,
    ref_id=None,
    *,
    task_context=None,
) -> dict[str, list[str]]:
    if (ref_type is None) != (ref_id is None):
        raise SkillDeliveryError(
            'invalid_task_ref',
            'ref_type 与 ref_id 必须同时提供',
            400,
        )

    assigned = []
    for installation in claw.skills.filter_by(enabled=True).all():
        if installation.skill:
            assigned.append(installation.skill.name)

    source_names = {
        'assigned': assigned,
        'profile': _active_profile_skill_names(claw),
        'task_context': [],
    }
    if ref_type is not None:
        if task_context is None:
            task_context = resolve_task_context(ref_type, int(ref_id), claw)
        source_names['task_context'] = [
            name
            for name in (
                _required_skill_name(value)
                for value in task_context.get('required_skills') or []
            )
            if name
        ]
    return source_names


def _skill_unavailable_reason(skill, claw) -> str | None:
    if bool(getattr(skill, 'is_deleted', False)):
        return 'deleted'
    if (getattr(skill, 'review_status', None) or 'approved') != 'approved':
        return 'not_approved'
    if (getattr(skill, 'visibility', None) or 'public') == 'private':
        if getattr(skill, 'owner_claw_id', None) != claw.id:
            return 'private_forbidden'
    project_ids = set()
    for value in getattr(skill, 'applicable_projects', None) or []:
        try:
            project_ids.add(int(value))
        except (TypeError, ValueError):
            continue
    if project_ids and getattr(claw, 'project_id', None) not in project_ids:
        return 'project_forbidden'
    return None


def _source_contract(
    claw,
    ref_type=None,
    ref_id=None,
) -> tuple[dict[str, list[str]], list[str]]:
    result: dict[str, list[str]] = {}
    task_context = None
    if ref_type is not None:
        task_context = resolve_task_context(ref_type, int(ref_id), claw)
    source_names = _source_names(
        claw,
        ref_type=ref_type,
        ref_id=ref_id,
        task_context=task_context,
    )
    for source in SOURCE_ORDER:
        for name in source_names[source]:
            result.setdefault(name, [])
            if source not in result[name]:
                result[name].append(source)
    blocking: list[str] = []
    if task_context is not None:
        blocking = [
            name
            for name in (
                _required_skill_name(value)
                for value in task_context.get('blocking_skills') or []
            )
            if name
        ]
    return result, list(dict.fromkeys(blocking))


def _task_query(ref_type, ref_id) -> str:
    if ref_type is None:
        return ''
    return '?' + urlencode({'ref_type': ref_type, 'ref_id': ref_id})


def build_skill_manifest(
    claw,
    ref_type=None,
    ref_id=None,
    base_path='/api/v1',
) -> dict[str, Any]:
    from app.models import Skill, OpenClawSkill
    from app.services.skill_installation import installation_view

    assignments = {a.skill_id: a for a in OpenClawSkill.query.filter_by(
        openclaw_id=claw.id, enabled=True).all()}

    sources_by_name, blocking_skills = _source_contract(
        claw,
        ref_type=ref_type,
        ref_id=ref_id,
    )
    blocking_set = set(blocking_skills)
    names = sorted(sources_by_name)
    found = {
        skill.name: skill
        for skill in (
            Skill.query.filter(Skill.name.in_(names)).all() if names else []
        )
    }
    skills = []
    missing = []
    query = _task_query(ref_type, ref_id)
    root = base_path.rstrip('/')
    for name in names:
        sources = sources_by_name[name]
        skill = found.get(name)
        if skill is None:
            missing.append({
                'name': name,
                'sources': sources,
                'reason': 'not_found',
                'blocking': name in blocking_set,
            })
            continue
        reason = _skill_unavailable_reason(skill, claw)
        if reason:
            missing.append({
                'name': name,
                'sources': sources,
                'reason': reason,
                'blocking': name in blocking_set,
            })
            continue

        descriptor = skill_bundle_descriptor(skill)
        file_root = (
            f'{root}/openclaws/{claw.id}/skills/{skill.id}/files'
        )
        files = []
        for item in descriptor['files']:
            files.append({
                'path': item['path'],
                'size': item['size'],
                'sha256': item['sha256'],
                'updated_at': item['updated_at'],
                'download_url': (
                    f"{file_root}/{quote(item['path'], safe='/')}{query}"
                ),
            })
        skills.append({
            'id': skill.id,
            'name': skill.name,
            'display_name': skill.display_name,
            'content_version': descriptor['content_version'],
            'sha256': descriptor['sha256'],
            'sources': sources,
            'blocking': name in blocking_set,
            'files': files,
            'installation': installation_view(assignments[skill.id], descriptor)
                if skill.id in assignments else None,
            'pack_url': (
                f'{root}/openclaws/{claw.id}/skills/{skill.id}/pack{query}'
            ),
        })

    return {
        'manifest_version': 1,
        'claw_id': claw.id,
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'task_ref': (
            {'ref_type': ref_type, 'ref_id': int(ref_id)}
            if ref_type is not None else None
        ),
        'skills': skills,
        'missing_skills': missing,
        'blocking_skills': blocking_skills,
    }


def get_authorized_skill_bundle(
    claw,
    skill_id: int,
    ref_type=None,
    ref_id=None,
) -> dict[str, Any]:
    from app import db
    from app.models import Skill

    manifest = build_skill_manifest(
        claw,
        ref_type=ref_type,
        ref_id=ref_id,
    )
    item = next(
        (value for value in manifest['skills'] if value['id'] == skill_id),
        None,
    )
    if item is None:
        raise SkillDeliveryError(
            'skill_forbidden',
            '当前 Agent 未获授权访问此 Skill',
            403,
        )
    skill = db.session.get(Skill, skill_id)
    return {
        'skill': skill,
        'manifest_item': item,
        **skill_bundle_descriptor(skill),
    }
