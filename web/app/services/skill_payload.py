def normalize_skill_payload(data):
    """规范化 Skill API 入参。

    Hub 的权威正文字段是 template_content。兼容 Agent 常用的
    content/body 别名，但不把别名继续传给后续字段更新逻辑。
    """
    if data is None:
        payload = {}
    elif not isinstance(data, dict):
        return data
    else:
        payload = dict(data)
    if not payload.get('template_content'):
        for alias in ('content', 'body'):
            if payload.get(alias):
                payload['template_content'] = payload[alias]
                break
    payload.pop('content', None)
    payload.pop('body', None)
    return payload
