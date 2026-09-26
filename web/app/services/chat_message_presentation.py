"""Pure presentation helpers for chat messages.

This module intentionally has no Flask or database imports so the parsing rule
can be reused and tested without booting the Hub application.
"""

import json


_MESSAGE_TEXT_KEYS = ('answer', 'reply', 'message', 'content', 'summary', 'text')


def message_presentation(content):
    """Build a UI projection without changing the auditable raw message.

    Agent adapters may return a short progress prefix followed by a JSON object.
    Keeping ``content`` untouched is important for Agent/API consumers, while the
    browser needs the actual answer and structured receipts separated. Only a
    JSON object that consumes the complete suffix is accepted, so ordinary text
    containing braces cannot be accidentally hidden.
    """
    raw = str(content or '').strip()
    fallback = {'parsed_json': False, 'text': raw, 'fields': {}}
    if not raw:
        return fallback

    decoder = json.JSONDecoder()
    starts = [0]
    starts.extend(index for index, char in enumerate(raw) if char == '{' and index)
    for start in starts:
        candidate = raw[start:].lstrip()
        try:
            value, end = decoder.raw_decode(candidate)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if candidate[end:].strip() or not isinstance(value, dict):
            continue
        text_key = next((key for key in _MESSAGE_TEXT_KEYS
                         if isinstance(value.get(key), str)
                         and value.get(key).strip()), None)
        if text_key is None:
            continue
        fields = {key: item for key, item in value.items()
                  if key != text_key and item not in (None, '', [], {})}
        return {
            'parsed_json': True,
            'text': value[text_key].strip(),
            'text_key': text_key,
            'fields': fields,
            'source_prefix_omitted': bool(raw[:start].strip()),
        }
    return fallback
