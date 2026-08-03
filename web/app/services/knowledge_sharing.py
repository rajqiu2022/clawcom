"""Knowledge favorite, external sharing and Markdown export helpers."""

import re
from typing import Any, Iterable, Optional


_WINDOWS_FILENAME_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _value(value: Any) -> str:
    if value is None:
        return ''
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    return str(value)


def knowledge_favorite_owner(user=None, claw=None) -> dict:
    """Return the exclusive owner columns for one favorite."""
    if claw is not None and getattr(claw, 'id', None):
        return {'user_id': None, 'claw_id': int(claw.id)}
    if user is not None and getattr(user, 'id', None):
        return {'user_id': int(user.id), 'claw_id': None}
    raise ValueError('收藏操作需要登录用户或 Agent 身份')


def can_manage_knowledge_share(
        entry, user=None, claw=None,
        owned_claw_ids: Optional[Iterable[int]] = None) -> bool:
    """Check whether the current identity may manage an entry's share link."""
    if user is not None and getattr(user, 'role', '') in (
            'admin', 'super_admin'):
        return True
    created_by = str(getattr(entry, 'created_by', '') or '').strip()
    if user is not None and created_by:
        if str(getattr(user, 'username', '') or '').strip() == created_by:
            return True
    source_claw_id = getattr(entry, 'source_openclaw_id', None)
    if claw is not None:
        if source_claw_id and getattr(claw, 'id', None) == source_claw_id:
            return True
        if created_by and str(getattr(claw, 'name', '') or '').strip() == created_by:
            return True
    try:
        owner_ids = {int(item) for item in (owned_claw_ids or [])}
    except (TypeError, ValueError):
        owner_ids = set()
    return bool(source_claw_id and int(source_claw_id) in owner_ids)


def public_knowledge_payload(entry) -> dict:
    """Build an explicit anonymous-safe knowledge payload."""
    source_claw = getattr(entry, 'source_openclaw', None)
    return {
        'title': str(getattr(entry, 'title', '') or ''),
        'content': str(getattr(entry, 'content', '') or ''),
        'category': str(getattr(entry, 'category', '') or ''),
        'scope': str(getattr(entry, 'scope', '') or ''),
        'project_name': str(getattr(entry, 'project_name', '') or ''),
        'module_name': str(getattr(entry, 'module_name', '') or ''),
        'source_type': str(getattr(entry, 'source_type', '') or ''),
        'source_openclaw_name': (
            str(getattr(source_claw, 'name', '') or '') if source_claw else ''
        ),
        'created_by': str(getattr(entry, 'created_by', '') or ''),
        'created_at': _value(getattr(entry, 'created_at', None)),
        'updated_at': _value(getattr(entry, 'updated_at', None)),
    }


def knowledge_markdown_filename(title: str) -> str:
    """Return a Windows-compatible, bounded Markdown download filename."""
    cleaned = _WINDOWS_FILENAME_INVALID.sub('', str(title or ''))
    cleaned = re.sub(r'\s+', ' ', cleaned).strip().rstrip('. ')
    cleaned = cleaned[:120].rstrip('. ')
    return f'{cleaned or "knowledge"}.md'


def knowledge_markdown(entry) -> str:
    """Render one knowledge entry as portable Markdown."""
    payload = public_knowledge_payload(entry)
    metadata = [
        ('分类', payload['category']),
        ('范围', payload['scope']),
        ('项目', payload['project_name']),
        ('模块', payload['module_name']),
        ('来源', payload['source_openclaw_name'] or payload['source_type']),
        ('创建者', payload['created_by']),
        ('创建时间', payload['created_at']),
        ('更新时间', payload['updated_at']),
    ]
    lines = [f"# {payload['title'] or '未命名知识'}", '']
    lines.extend(
        f'- {label}：{value}'
        for label, value in metadata
        if value
    )
    body = payload['content']
    if body:
        lines.extend(['', '---', '', body])
    return '\n'.join(lines)
