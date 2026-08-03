def normalize_skill_visibility(data):
    """规范化 Skill 可见性字段，兼容历史/误用入参。

    私有类型不是 scope；scope=private 仅作为兼容别名处理，并从后续
    scope 更新中移除，避免写入非法 scope 值。
    """
    payload = dict(data or {})
    visibility = None

    raw_visibility = payload.pop('visibility', None)
    if isinstance(raw_visibility, str):
        raw = raw_visibility.strip().lower()
        if raw in ('private', 'public'):
            visibility = raw

    if 'is_public' in payload:
        visibility = 'public' if bool(payload.pop('is_public')) else 'private'

    if str(payload.get('scope') or '').strip().lower() == 'private':
        visibility = 'private'
        payload.pop('scope', None)

    return {
        'data': payload,
        'visibility': visibility,
    }
