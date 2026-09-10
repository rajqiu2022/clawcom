"""Build the non-secret Hub context delivered to an Agent sidecar.

The response is deliberately additive: old sidecars can keep consuming
``active_agent_profile`` while newer providers receive the same profile plus
Hub identity and enabled Rules as invocation-scoped trusted context.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable


SYSTEM_CONTEXT_VERSION = 2
MAX_SYSTEM_CONTEXT_BYTES = 60 * 1024
_RULE_BUDGET_WARNING = 'RULE_CONTENT_OMITTED_FOR_CONTEXT_BUDGET'
_SYSTEM_CONTEXT_POLICY_FIELDS = {
    'allowed_workflow_create_definition_ids',
    'codex_orchestrator',
    'workflow_start_bindings',
    'deepflow_release_required_definition_ids',
    'remote_source_id',
    'remote_source_ids',
}
_CODEX_ORCHESTRATOR_FIELDS = {
    'enabled', 'session_key', 'resume_on', 'allowed_next_flows',
    'transitions', 'max_retries', 'review_success',
}
_WORKFLOW_TERMINAL_STATUSES = {
    'blocked', 'cancelled', 'canceled', 'completed', 'done', 'failed',
    'passed', 'skipped', 'succeeded', 'success', 'timeout', 'timed_out',
}
_SESSION_KEY_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9:._-]{0,127}')
_REMOTE_SOURCE_ID_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}')
_CODEX_HUB_API_DISCOVERY_RULE = {
    'id': 0,
    'name': 'codex_claw_hub_delayed_tool_discovery',
    'display_name': 'Codex Hub API 延迟工具发现',
    'description': 'Hub 为 Codex 回合注入的临时兼容指令。',
    'category': 'runtime_compatibility',
    'scope': 'invocation',
    'content': (
        '本回合已注入 claw_hub MCP。需要查询或操作 Hub 时，必须先在 '
        'ALL_TOOLS 中查找 mcp__claw_hub__hub_api，并通过 '
        'tools.mcp__claw_hub__hub_api 调用。未执行工具发现前，不得声称 '
        'Hub API 工具不可用。返回值需从 result.content[0].text 解析 JSON，'
        '实际 Hub 响应位于 body 字段。'
    ),
    'applied': True,
    'updated_at': None,
    'runtime_generated': True,
}


def _codex_workflow_create_rule(definition_ids: Iterable[int]) -> dict[str, Any] | None:
    """Describe the exact proxy-safe collection-create contract to Codex."""

    allowed = sorted({
        int(item) for item in definition_ids
        if type(item) is int and item > 0
    })
    if not allowed:
        return None
    return {
        'id': -1,
        'name': 'codex_workflow_run_create_contract',
        'display_name': 'Codex Workflow Run 创建契约',
        'description': 'Hub 为 Codex 回合生成的最小权限启动契约。',
        'category': 'runtime_compatibility',
        'scope': 'invocation',
        'content': (
            '创建 Workflow Run 时，只能调用 mcp__claw_hub__hub_api，使用 '
            'method=POST、path=/api/v1/workflow-runs。body 只传 '
            '{"definition_id": <获授权的整数ID>}；不要传 workflow_key、hub_url、'
            'start_vars、variables、context、executor、target、selected 或调度覆盖字段。'
            f'当前获授权的 definition_id 为 {allowed}。必须传唯一 operation_id，并传 '
            'verify={"expected":{"definition_id":<同一个整数ID>},'
            '"resource_id_field":"id",'
            '"read_path":"/api/v1/workflow-runs/{resource_id}"}。'
            '工具返回非 2xx 时，应读取 result.content[0].text 中 body.error 的具体错误，'
            '不得改猜其他路径或参数契约。'
        ),
        'applied': True,
        'updated_at': None,
        'runtime_generated': True,
    }


def _codex_workflow_mission_rule(missions: Iterable[Any]) -> dict[str, Any] | None:
    summaries = []
    for item in list(missions or []):
        if isinstance(item, dict):
            mission_id = item.get('id')
            mission_key = _as_text(item.get('mission_key'))
            project_id = item.get('project_id')
            definitions_api = _as_text(item.get('definitions_api'))
            dispatch_api = _as_text(item.get('dispatch_api'))
        else:
            mission_id = getattr(item, 'id', None)
            mission_key = _as_text(getattr(item, 'mission_key', ''))
            project_id = getattr(item, 'project_id', None)
            definitions_api = (
                f'/api/v1/workflow-missions/{mission_id}/definitions')
            dispatch_api = (
                f'/api/v1/workflow-missions/{mission_id}/dispatch')
        if type(mission_id) is not int or mission_id <= 0:
            continue
        summaries.append({
            'id': mission_id,
            'mission_key': mission_key,
            'project_id': project_id,
            'definitions_api': definitions_api,
            'dispatch_api': dispatch_api,
        })
    if not summaries:
        return None
    return {
        'id': -2,
        'name': 'codex_workflow_mission_autonomy',
        'display_name': 'Codex Mission 自主调度契约',
        'description': 'Hub 为主 Agent 注入的一次授权、自主决策调度契约。',
        'category': 'runtime_compatibility',
        'scope': 'invocation',
        'content': (
            '你是以下 Workflow Mission 的主 Agent，下一步选择由你自主决策：'
            f'{json.dumps(summaries, ensure_ascii=False, separators=(",", ":"))}。'
            '先 GET definitions_api 获取当前项目全部可调度 active Flow，再按你的判断 '
            'POST dispatch_api，body 最少只需 workflow_definition_id；可选 start_vars、'
            'context、reason、decision_key。Hub 不要求逐节点人工 Gate 或额外结果合同，'
            '通过 hub_api 发起写操作时只需提供稳定 operation_id，不需要 verify；'
            '只校验主 Agent 身份、项目范围、幂等和 Child Run 预算。不得传 executor、'
            'target、selected 或 worker 覆盖字段。一个 Child Run 完成后读取其结果：若 '
            'blocked/failed 的原因已经修复且仍需执行同一 Flow，优先 POST 该 Run '
            '返回的 restart_api 原地完整重启，并携带稳定 Idempotency-Key。若 Hub 返回 '
            'STALE_DEFINITION_SNAPSHOT，说明不可变 Run 快照已落后；此时必须用同一 '
            'Definition 创建一个受预算和幂等保护的替代 Run，不能继续 restart 旧 Run。'
            '只有首次启动、快照过期替代或决定切换到不同 Flow 时才调用 dispatch。'
            '随后再自主决定继续、换 Flow 或结束 Mission。'
        ),
        'applied': True,
        'updated_at': None,
        'runtime_generated': True,
    }

def allowed_workflow_create_definition_ids(
    claw: Any,
    definitions: Iterable[Any],
) -> list[int]:
    """Return active definitions this authenticated Claw may execute.

    This mirrors the Workflow execution ACL without broadening it.  The list is
    consumed by the Worker as an invocation-scoped collection-write grant; Hub
    still performs its normal visibility and execute checks on the POST itself.
    """

    claw_id = getattr(claw, 'id', None)
    claw_name = _as_text(getattr(claw, 'name', ''))
    is_admin = _as_text(getattr(claw, 'role', '')).lower() == 'admin'
    allowed = set()
    for definition in list(definitions or []):
        definition_id = getattr(definition, 'id', None)
        if type(definition_id) is not int or definition_id <= 0:
            continue
        if _as_text(getattr(definition, 'status', 'active')).lower() != 'active':
            continue
        owner_type = _as_text(getattr(definition, 'owner_type', ''))
        owner_id = getattr(definition, 'owner_id', None)
        legacy_owner = bool(
            not owner_type and
            not owner_id and
            claw_name and
            _as_text(getattr(definition, 'created_by', '')) == claw_name
        )
        acl = getattr(definition, 'executor_acl_json', None) or {}
        editor_acl = getattr(definition, 'editor_acl_json', None) or {}
        acl_claw_ids = set()
        for item in acl.get('claw_ids', []) if isinstance(acl, dict) else []:
            try:
                acl_claw_ids.add(int(item))
            except (TypeError, ValueError):
                continue
        for item in (editor_acl.get('claw_ids', [])
                     if isinstance(editor_acl, dict) else []):
            try:
                acl_claw_ids.add(int(item))
            except (TypeError, ValueError):
                continue
        try:
            same_owner = owner_type == 'claw' and int(owner_id) == int(claw_id)
        except (TypeError, ValueError):
            same_owner = False
        try:
            explicitly_allowed = int(claw_id) in acl_claw_ids
        except (TypeError, ValueError):
            explicitly_allowed = False
        all_allowed = bool(acl.get('all')) if isinstance(acl, dict) else False
        if (is_admin or legacy_owner or same_owner or all_allowed or
                explicitly_allowed):
            allowed.add(definition_id)
    return sorted(allowed)


def _as_text(value: Any) -> str:
    return str(value or '').strip()


def _timestamp(value: Any) -> str | None:
    return str(value) if value else None


def _json_bytes(value: Any) -> bytes:
    """Serialize exactly as the Worker-facing size check is expected to."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
    ).encode('utf-8')


def _definition_ids(value: Any, *, field: str) -> list[int]:
    if not isinstance(value, list):
        raise ValueError(f'{field} must be a list')
    if any(type(item) is not int or item <= 0 for item in value):
        raise ValueError(f'{field} must contain positive integers only')
    return sorted(set(value))


def validate_system_context_policy(value: Any) -> dict[str, Any]:
    """Canonicalize the persisted non-secret policy or reject it entirely."""
    if not isinstance(value, dict):
        raise ValueError('system context policy must be an object')
    if set(value) - _SYSTEM_CONTEXT_POLICY_FIELDS:
        raise ValueError('system context policy contains unsupported fields')
    if 'allowed_workflow_create_definition_ids' not in value:
        raise ValueError('allowed_workflow_create_definition_ids is required')
    allowed = _definition_ids(
        value.get('allowed_workflow_create_definition_ids'),
        field='allowed_workflow_create_definition_ids',
    )
    result: dict[str, Any] = {
        'allowed_workflow_create_definition_ids': allowed,
    }
    remote_source_ids = []
    if 'remote_source_id' in value:
        singular = value.get('remote_source_id')
        if (not isinstance(singular, str)
                or _REMOTE_SOURCE_ID_RE.fullmatch(singular) is None):
            raise ValueError('remote_source_id is invalid')
        remote_source_ids.append(singular)
    if 'remote_source_ids' in value:
        plural = value.get('remote_source_ids')
        if (not isinstance(plural, list)
                or not 1 <= len(plural) <= 16
                or any(not isinstance(item, str)
                       or _REMOTE_SOURCE_ID_RE.fullmatch(item) is None
                       for item in plural)):
            raise ValueError('remote_source_ids is invalid')
        remote_source_ids.extend(plural)
    if remote_source_ids:
        result['remote_source_ids'] = sorted(set(remote_source_ids))
    if 'workflow_start_bindings' in value:
        bindings = value.get('workflow_start_bindings')
        if not isinstance(bindings, dict):
            raise ValueError('workflow_start_bindings must be an object')
        normalized_bindings = {}
        for raw_definition_id, raw_binding in bindings.items():
            if not str(raw_definition_id).isdigit() or int(raw_definition_id) <= 0:
                raise ValueError('workflow_start_bindings contains invalid definition id')
            if (not isinstance(raw_binding, dict)
                    or set(raw_binding) - {'executor_claw_ids', 'start_vars'}):
                raise ValueError('workflow_start_bindings contains invalid binding')
            executor_ids = _definition_ids(
                raw_binding.get('executor_claw_ids') or [],
                field='workflow_start_bindings.executor_claw_ids')
            start_vars = raw_binding.get('start_vars') or {}
            if (not executor_ids or not isinstance(start_vars, dict)
                    or any(not isinstance(name, str)
                           or re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', name) is None
                           or re.search(r'(?i)(?:token|secret|password|private_key|api_key)', name)
                           or isinstance(item, (dict, list))
                           or isinstance(item, str)
                           and len(item.encode('utf-8', 'replace')) > 1024
                           for name, item in start_vars.items())):
                raise ValueError('workflow_start_bindings contains invalid binding values')
            normalized_bindings[str(int(raw_definition_id))] = {
                'executor_claw_ids': executor_ids,
                'start_vars': dict(start_vars),
            }
        result['workflow_start_bindings'] = normalized_bindings
    if 'deepflow_release_required_definition_ids' in value:
        result['deepflow_release_required_definition_ids'] = _definition_ids(
            value.get('deepflow_release_required_definition_ids'),
            field='deepflow_release_required_definition_ids')
    if 'codex_orchestrator' not in value:
        return result
    raw = value.get('codex_orchestrator')
    if not isinstance(raw, dict):
        raise ValueError('codex_orchestrator must be an object')
    if set(raw) - _CODEX_ORCHESTRATOR_FIELDS:
        raise ValueError('codex_orchestrator contains unsupported fields')
    if raw.get('enabled') is not True:
        raise ValueError('codex_orchestrator.enabled must be true')
    session_key = _as_text(raw.get('session_key'))
    if not _SESSION_KEY_RE.fullmatch(session_key):
        raise ValueError('codex_orchestrator.session_key is invalid')
    resume_on = raw.get('resume_on')
    if not isinstance(resume_on, list) or not resume_on:
        raise ValueError('codex_orchestrator.resume_on must be a non-empty list')
    normalized_resume_on = []
    for item in resume_on:
        if (not isinstance(item, str) or
                item.lower() not in _WORKFLOW_TERMINAL_STATUSES):
            raise ValueError('codex_orchestrator.resume_on contains invalid status')
        normalized_resume_on.append(item.lower())
    allowed_next = _definition_ids(
        raw.get('allowed_next_flows'),
        field='codex_orchestrator.allowed_next_flows',
    )
    if not allowed_next:
        raise ValueError('codex_orchestrator.allowed_next_flows must not be empty')
    if not set(allowed_next).issubset(allowed):
        raise ValueError('allowed_next_flows must be covered by workflow create grants')
    raw_transitions = raw.get('transitions') or {}
    if not isinstance(raw_transitions, dict):
        raise ValueError('codex_orchestrator.transitions must be an object')
    transitions = {}
    for raw_source, raw_targets in raw_transitions.items():
        if not str(raw_source).isdigit() or int(raw_source) <= 0:
            raise ValueError('codex_orchestrator.transitions contains invalid source')
        targets = _definition_ids(
            raw_targets,
            field='codex_orchestrator.transitions targets')
        if not set(targets).issubset(allowed_next):
            raise ValueError('transition targets must be covered by allowed_next_flows')
        transitions[str(int(raw_source))] = targets
    max_retries = raw.get('max_retries')
    if type(max_retries) is not int or not 0 <= max_retries <= 5:
        raise ValueError('codex_orchestrator.max_retries must be between 0 and 5')
    review_success = raw.get('review_success', True)
    if not isinstance(review_success, bool):
        raise ValueError('codex_orchestrator.review_success must be boolean')
    result['codex_orchestrator'] = {
        'enabled': True,
        'session_key': session_key,
        'resume_on': sorted(set(normalized_resume_on)),
        'allowed_next_flows': allowed_next,
        'transitions': transitions,
        'max_retries': max_retries,
        'review_success': review_success,
    }
    return result


def _resolve_workflow_policy(
    runtime_agent_type: str,
    workflow_create_definition_ids: Iterable[int] | None,
    configured_policy: Any,
) -> tuple[dict[str, Any], list[str]]:
    acl_ids = None
    if workflow_create_definition_ids is not None:
        acl_ids = sorted({
            int(item) for item in workflow_create_definition_ids
            if type(item) is int and item > 0
        })
    if _as_text(runtime_agent_type).lower() != 'codex' or configured_policy is None:
        if acl_ids is None:
            return {}, []
        return {'allowed_workflow_create_definition_ids': acl_ids}, []
    try:
        configured = validate_system_context_policy(configured_policy)
    except ValueError:
        return {'allowed_workflow_create_definition_ids': []}, [
            'CODEX_ORCHESTRATOR_POLICY_INVALID',
        ]
    if acl_ids is None:
        return {'allowed_workflow_create_definition_ids': []}, [
            'WORKFLOW_CREATE_GRANTS_UNAVAILABLE',
        ]
    effective_ids = sorted(set(acl_ids).intersection(
        configured['allowed_workflow_create_definition_ids']))
    result: dict[str, Any] = {
        'allowed_workflow_create_definition_ids': effective_ids,
    }
    if configured.get('remote_source_ids'):
        result['remote_source_ids'] = list(
            configured['remote_source_ids'])
    bindings = configured.get('workflow_start_bindings') or {}
    effective_bindings = {
        key: dict(binding)
        for key, binding in bindings.items()
        if int(key) in effective_ids
    }
    if effective_bindings:
        result['workflow_start_bindings'] = effective_bindings
    required_releases = sorted(set(
        configured.get('deepflow_release_required_definition_ids') or []
    ).intersection(effective_ids))
    if required_releases:
        result['deepflow_release_required_definition_ids'] = required_releases
    warnings = []
    orchestrator = configured.get('codex_orchestrator')
    if orchestrator:
        next_flows = sorted(set(orchestrator['allowed_next_flows']).intersection(
            effective_ids))
        if next_flows != orchestrator['allowed_next_flows']:
            warnings.append('CODEX_ORCHESTRATOR_FLOWS_FILTERED')
        if next_flows:
            transitions = {
                source: sorted(set(targets).intersection(next_flows))
                for source, targets in orchestrator.get('transitions', {}).items()
            }
            transitions = {
                source: targets for source, targets in transitions.items()
                if targets
            }
            result['codex_orchestrator'] = dict(
                orchestrator,
                allowed_next_flows=next_flows,
                transitions=transitions,
            )
        else:
            warnings.append('CODEX_ORCHESTRATOR_DISABLED_NO_ALLOWED_FLOW')
    return result, warnings


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


def _fit_system_context(
    system_context: dict[str, Any],
    *,
    max_bytes: int = MAX_SYSTEM_CONTEXT_BYTES,
) -> tuple[dict[str, Any], list[int], int]:
    """Keep trusted context below the Worker limit without blocking execution.

    Full Rules remain available in the legacy top-level ``rules`` field. Only
    the invocation-scoped trusted context is compacted, and only when needed.
    Largest rule bodies are omitted first so ordinary configurations remain
    byte-for-byte unchanged.
    """

    fitted = dict(system_context)
    fitted['rules'] = [dict(item) for item in system_context.get('rules') or []]
    size_bytes = len(_json_bytes(fitted))
    if size_bytes <= max_bytes:
        return fitted, [], size_bytes

    fitted['warnings'] = sorted(set(fitted.get('warnings') or []).union({
        _RULE_BUDGET_WARNING,
    }))
    candidates = sorted(
        enumerate(fitted['rules']),
        key=lambda pair: (
            -len(_as_text(pair[1].get('content')).encode('utf-8')),
            int(pair[1].get('id') or 0),
        ),
    )
    omitted_rule_ids = []
    for _, rule in candidates:
        content = _as_text(rule.get('content'))
        if not content:
            continue
        content_bytes = content.encode('utf-8')
        rule['content'] = ''
        rule['content_omitted'] = True
        rule['content_bytes'] = len(content_bytes)
        rule['content_sha256'] = hashlib.sha256(content_bytes).hexdigest()
        rule['omission_reason'] = 'system_context_size_budget'
        omitted_rule_ids.append(int(rule.get('id') or 0))
        size_bytes = len(_json_bytes(fitted))
        if size_bytes <= max_bytes:
            break

    return fitted, omitted_rule_ids, size_bytes


def build_agent_system_context(
    claw: Any,
    runtime_agent_type: str,
    assignments: Iterable[Any],
    rule_links: Iterable[Any],
    *,
    warnings: Iterable[str] = (),
    workflow_create_definition_ids: Iterable[int] | None = None,
    configured_policy: Any = None,
    workflow_missions: Iterable[Any] = (),
) -> dict[str, Any]:
    profiles, active_profile, profile_warnings = build_profile_context(assignments)
    rules = build_rule_context(rule_links)
    workflow_policy, policy_warnings = _resolve_workflow_policy(
        runtime_agent_type, workflow_create_definition_ids, configured_policy)
    trusted_rules = [dict(item) for item in rules]
    if _as_text(runtime_agent_type).lower() == 'codex':
        trusted_rules.append(dict(_CODEX_HUB_API_DISCOVERY_RULE))
        create_rule = _codex_workflow_create_rule(
            workflow_policy.get('allowed_workflow_create_definition_ids') or [])
        if create_rule:
            trusted_rules.append(create_rule)
        mission_rule = _codex_workflow_mission_rule(workflow_missions)
        if mission_rule:
            trusted_rules.append(mission_rule)
    context_warnings = sorted(
        set(profile_warnings)
        .union(_as_text(item) for item in warnings if _as_text(item))
        .union(policy_warnings)
    )
    policy = {
        'profile_required_for_execution': False,
        'missing_profile_behavior': 'continue_with_identity_and_rules',
    }
    policy.update(workflow_policy)
    mission_summaries = []
    for mission in list(workflow_missions or []):
        mission_id = (
            mission.get('id') if isinstance(mission, dict)
            else getattr(mission, 'id', None))
        if type(mission_id) is int and mission_id > 0:
            mission_summaries.append({
                'id': mission_id,
                'control_mode': 'agent_autonomous',
            })
    if mission_summaries:
        policy['active_workflow_missions'] = mission_summaries
    system_context = {
        'schema_version': SYSTEM_CONTEXT_VERSION,
        'identity': {
            'claw_id': getattr(claw, 'id', None),
            'claw_name': _as_text(getattr(claw, 'name', '')),
            'provider': _as_text(runtime_agent_type),
        },
        'profile': active_profile,
        'rules': trusted_rules,
        'warnings': context_warnings,
        'policy': policy,
    }
    system_context, omitted_rule_ids, context_size_bytes = _fit_system_context(
        system_context)
    context_warnings = list(system_context['warnings'])
    canonical = _json_bytes(system_context)
    return {
        'agent_profiles': profiles,
        'active_agent_profile': active_profile,
        'rules': rules,
        'system_context': system_context,
        'system_context_version': SYSTEM_CONTEXT_VERSION,
        'system_context_digest': hashlib.sha256(canonical).hexdigest(),
        'context_warnings': context_warnings,
        'system_context_size_bytes': context_size_bytes,
        'system_context_max_bytes': MAX_SYSTEM_CONTEXT_BYTES,
        'system_context_omitted_rule_ids': omitted_rule_ids,
    }
