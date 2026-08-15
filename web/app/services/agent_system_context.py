"""Build the non-secret Hub context delivered to an Agent sidecar.

The response is deliberately additive: old sidecars can keep consuming
``active_agent_profile`` while newer providers receive the same profile plus
Hub identity and enabled Rules as invocation-scoped trusted context.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable


SYSTEM_CONTEXT_VERSION = 1


def _as_text(value: Any) -> str:
    return str(value or '').strip()


def _timestamp(value: Any) -> str | None:
    return str(value) if value else None


def _profile_dict(profile: Any) -> dict[str, Any]:
    serializer = getattr(profile, 'to_dict', None)
    if callable(serializer):
        value = serializer()
        return dict(value) if isinstance(value, dict) else {}
    return {}


def build_profile_context(assignments: Iterable[Any]) -> tuple[list[dict], dict | None, list[str]]:
    """Return compatible profile fields without making a role mandatory."""

    profiles = []
    warnings = []
    ordered = sorted(
        list(assignments or []),
        key=lambda item: (
            not bool(getattr(item, 'is_primary', False)),
            int(getattr(item, 'id', 0) or 0),
        ),
    )
    for assignment in ordered:
        if _as_text(getattr(assignment, 'status', 'active')).lower() != 'active':
            continue
        post = getattr(assignment, 'post', None)
        profile = getattr(post, 'profile', None) if post is not None else None
        if post is None or profile is None:
            continue
        if _as_text(getattr(post, 'status', 'active')).lower() != 'active':
            continue
        if _as_text(getattr(profile, 'status', 'active')).lower() != 'active':
            continue
        item = {
            'post_key': _as_text(getattr(post, 'post_key', '')),
            'post_name': _as_text(getattr(post, 'name', '')),
            'profile_version': int(getattr(assignment, 'profile_version', 1) or 1),
            'required_profile_version': int(
                getattr(post, 'required_profile_version', 1) or 1),
            'is_primary': bool(getattr(assignment, 'is_primary', False)),
            'profile': _profile_dict(profile),
        }
        profiles.append(item)
        if item['profile_version'] < item['required_profile_version']:
            warnings.append('PROFILE_VERSION_BEHIND')

    active_profile = profiles[0] if profiles else None
    if active_profile is None:
        warnings.append('PROFILE_UNASSIGNED')
    return profiles, active_profile, sorted(set(warnings))


def build_rule_context(rule_links: Iterable[Any]) -> list[dict]:
    """Return enabled, reviewed Rules; ``applied`` is informative, not a gate.

    Older agents may not maintain the historical ``applied`` flag consistently.
    Sidecar context delivery itself is a valid application channel, so requiring
    that flag here would incorrectly hide otherwise enabled Rules.
    """

    rules = []
    for link in list(rule_links or []):
        if not bool(getattr(link, 'enabled', False)):
            continue
        rule = getattr(link, 'rule', None)
        if rule is None or bool(getattr(rule, 'is_deleted', False)):
            continue
        review_status = _as_text(getattr(rule, 'review_status', 'approved')).lower()
        if review_status not in ('', 'approved'):
            continue
        rules.append({
            'id': getattr(rule, 'id', None),
            'name': _as_text(getattr(rule, 'name', '')),
            'display_name': _as_text(getattr(rule, 'display_name', '')),
            'description': _as_text(getattr(rule, 'description', '')),
            'category': _as_text(getattr(rule, 'category', '')),
            'scope': _as_text(getattr(rule, 'scope', '')),
            'content': _as_text(getattr(rule, 'content_template', '')),
            'applied': bool(getattr(link, 'applied', False)),
            'updated_at': _timestamp(getattr(rule, 'updated_at', None)),
        })
    return sorted(rules, key=lambda item: (int(item.get('id') or 0), item['name']))


def build_agent_system_context(
    claw: Any,
    runtime_agent_type: str,
    assignments: Iterable[Any],
    rule_links: Iterable[Any],
    *,
    warnings: Iterable[str] = (),
) -> dict[str, Any]:
    profiles, active_profile, profile_warnings = build_profile_context(assignments)
    rules = build_rule_context(rule_links)
    context_warnings = sorted(set(profile_warnings).union(
        _as_text(item) for item in warnings if _as_text(item)))
    system_context = {
        'schema_version': SYSTEM_CONTEXT_VERSION,
        'identity': {
            'claw_id': getattr(claw, 'id', None),
            'claw_name': _as_text(getattr(claw, 'name', '')),
            'provider': _as_text(runtime_agent_type),
        },
        'profile': active_profile,
        'rules': rules,
        'warnings': context_warnings,
        'policy': {
            'profile_required_for_execution': False,
            'missing_profile_behavior': 'continue_with_identity_and_rules',
        },
    }
    canonical = json.dumps(
        system_context,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    return {
        'agent_profiles': profiles,
        'active_agent_profile': active_profile,
        'rules': rules,
        'system_context': system_context,
        'system_context_version': SYSTEM_CONTEXT_VERSION,
        'system_context_digest': hashlib.sha256(canonical).hexdigest(),
        'context_warnings': context_warnings,
    }
