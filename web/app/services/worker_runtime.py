"""Canonical Hub contract for Claw Worker runtime identity.

Runtime identity is intentionally separate from the provider.  A Claw Worker
can host either Hermes or Codex, while legacy sidecars may still use the same
provider names without being a Claw Worker deployment.
"""

import re


RUNTIME_SCHEMA_VERSION = 1
RUNTIME_KINDS = {'claw_worker', 'legacy_sidecar', 'hermes_agent'}
WORKER_PROVIDERS = {'hermes', 'codex'}
RUNTIME_MODES = {
    'legacy_split',
    'agent_host_v3',
    'codex_host_v4_canary',
    'agent_direct',
}
CONFIG_OWNERS = {'hub', 'worker'}
AUTH_MODES = {'', 'subscription', 'timiai_bridge'}
RUNTIME_SOURCES = {'worker', 'operator', 'deployment'}

_RUNTIME_FIELDS = {
    'schema', 'kind', 'provider', 'runtime_mode', 'platform',
    'provider_version', 'auth_mode', 'llm_provider', 'llm_model',
    'timiai_project', 'release_id', 'source_commit', 'artifact_sha256',
    'config_digest', 'source',
}
_HEX_64 = re.compile(r'^[0-9a-f]{64}$')
_HEX_COMMIT = re.compile(r'^[0-9a-f]{7,40}$')


def normalize_config_owner(value):
    owner = str(value or 'hub').strip().lower()
    if owner not in CONFIG_OWNERS:
        raise ValueError('config_owner 仅支持 hub / worker')
    return owner


def _text(value, field, limit):
    if value is None:
        return ''
    if not isinstance(value, (str, int, float)):
        raise ValueError(f'{field} 必须是字符串')
    result = str(value).strip()
    if len(result) > limit:
        raise ValueError(f'{field} 长度不能超过 {limit}')
    return result


def _platform(value):
    raw = _text(value, 'platform', 40).lower()
    if not raw:
        return 'unknown'
    if raw.startswith('win'):
        return 'windows'
    if raw.startswith('linux'):
        return 'linux'
    if raw.startswith('macos') or raw == 'darwin':
        return 'macos'
    if raw == 'unknown':
        return raw
    raise ValueError('platform 仅支持 linux / windows / macos / unknown')


def _auth_mode(value):
    raw = _text(value, 'auth_mode', 40).lower()
    aliases = {
        'chatgpt_subscription': 'subscription',
        'timiai': 'timiai_bridge',
    }
    raw = aliases.get(raw, raw)
    if raw not in AUTH_MODES:
        raise ValueError('auth_mode 仅支持 subscription / timiai_bridge')
    return raw


def validate_worker_runtime(value):
    """Validate and canonicalize a secret-free worker runtime document."""
    if not isinstance(value, dict):
        raise ValueError('runtime 必须是对象')
    unknown = set(value) - _RUNTIME_FIELDS
    if unknown:
        raise ValueError('runtime 包含不支持字段: ' + ', '.join(sorted(unknown)))

    try:
        schema = int(value.get('schema', RUNTIME_SCHEMA_VERSION))
    except (TypeError, ValueError):
        raise ValueError('runtime.schema 必须是整数')
    if schema != RUNTIME_SCHEMA_VERSION:
        raise ValueError(f'runtime.schema 仅支持 {RUNTIME_SCHEMA_VERSION}')

    kind = _text(value.get('kind'), 'kind', 40).lower()
    if kind not in RUNTIME_KINDS:
        raise ValueError('kind 仅支持 claw_worker / legacy_sidecar / hermes_agent')

    provider = _text(value.get('provider'), 'provider', 20).lower()
    if kind == 'claw_worker' and provider not in WORKER_PROVIDERS:
        raise ValueError('claw_worker provider 仅支持 hermes / codex')
    if kind != 'claw_worker' and provider not in {'openclaw', 'hermes', 'codex', 'custom'}:
        raise ValueError('provider 不受支持')

    runtime_mode = _text(value.get('runtime_mode'), 'runtime_mode', 40).lower()
    if runtime_mode and runtime_mode not in RUNTIME_MODES:
        raise ValueError('runtime_mode 不受支持')

    auth_mode = _auth_mode(value.get('auth_mode'))
    if provider == 'codex' and not auth_mode:
        auth_mode = 'subscription'
    if provider != 'codex' and auth_mode:
        raise ValueError('只有 codex provider 可设置 auth_mode')

    source = _text(value.get('source') or 'operator', 'source', 20).lower()
    if source not in RUNTIME_SOURCES:
        raise ValueError('source 仅支持 worker / operator / deployment')

    source_commit = _text(value.get('source_commit'), 'source_commit', 40).lower()
    if source_commit and not _HEX_COMMIT.fullmatch(source_commit):
        raise ValueError('source_commit 必须是 7-40 位十六进制 Git commit')
    artifact_sha = _text(
        value.get('artifact_sha256'), 'artifact_sha256', 64).lower()
    if artifact_sha and not _HEX_64.fullmatch(artifact_sha):
        raise ValueError('artifact_sha256 必须是 64 位十六进制摘要')
    config_digest = _text(
        value.get('config_digest'), 'config_digest', 64).lower()
    if config_digest and not _HEX_64.fullmatch(config_digest):
        raise ValueError('config_digest 必须是 64 位十六进制摘要')

    return {
        'schema': schema,
        'kind': kind,
        'provider': provider,
        'runtime_mode': runtime_mode,
        'platform': _platform(value.get('platform')),
        'provider_version': _text(
            value.get('provider_version'), 'provider_version', 80),
        'auth_mode': auth_mode,
        'llm_provider': _text(
            value.get('llm_provider'), 'llm_provider', 50).lower(),
        'llm_model': _text(value.get('llm_model'), 'llm_model', 120),
        'timiai_project': _text(
            value.get('timiai_project'), 'timiai_project', 50),
        'release_id': _text(value.get('release_id'), 'release_id', 100),
        'source_commit': source_commit,
        'artifact_sha256': artifact_sha,
        'config_digest': config_digest,
        'source': source,
    }


def runtime_from_query(args, existing=None):
    """Build a runtime report from optional sidecar query fields.

    Returns ``None`` unless ``runtime_kind`` is explicitly supplied or an
    existing Claw Worker record is being refreshed.  This avoids incorrectly
    classifying old Hermes sidecars as Claw Worker runtimes.
    """
    existing = existing if isinstance(existing, dict) else {}
    report_keys = (
        'runtime_kind', 'runtime_provider', 'runtime_agent_type',
        'runtime_mode', 'runtime_platform', 'provider_version', 'auth_mode',
        'runtime_llm_provider', 'runtime_llm_model',
        'runtime_timiai_project', 'worker_release_id',
        'worker_source_commit', 'worker_artifact_sha256',
        'runtime_config_digest',
    )
    if not any(args.get(key) not in (None, '') for key in report_keys):
        return None
    kind = (args.get('runtime_kind') or '').strip().lower()
    if not kind and existing.get('kind') != 'claw_worker':
        return None
    kind = kind or existing.get('kind')
    provider = (
        args.get('runtime_provider')
        or args.get('runtime_agent_type')
        or args.get('agent_type')
        or existing.get('provider')
        or ''
    )
    mapping = {
        'schema': RUNTIME_SCHEMA_VERSION,
        'kind': kind,
        'provider': provider,
        'runtime_mode': args.get('runtime_mode'),
        'platform': args.get('runtime_platform'),
        'provider_version': args.get('provider_version'),
        'auth_mode': args.get('auth_mode'),
        'llm_provider': args.get('runtime_llm_provider'),
        'llm_model': args.get('runtime_llm_model'),
        'timiai_project': args.get('runtime_timiai_project'),
        'release_id': args.get('worker_release_id'),
        'source_commit': args.get('worker_source_commit'),
        'artifact_sha256': args.get('worker_artifact_sha256'),
        'config_digest': args.get('runtime_config_digest'),
        'source': 'worker',
    }
    merged = dict(existing)
    for key, item in mapping.items():
        if item not in (None, '') or key in ('schema', 'kind', 'provider', 'source'):
            merged[key] = item
    return validate_worker_runtime(merged)


def runtime_summary(runtime, config_owner='hub'):
    """Return stable API/UI fields for a stored runtime document."""
    try:
        canonical = validate_worker_runtime(runtime)
    except ValueError:
        canonical = None
    try:
        owner = normalize_config_owner(config_owner)
    except ValueError:
        owner = 'hub'
    return {
        'has_worker_runtime': bool(
            canonical and canonical['kind'] == 'claw_worker'),
        'runtime_kind': canonical['kind'] if canonical else '',
        'runtime_provider': canonical['provider'] if canonical else '',
        'runtime_config_owner': owner,
        'worker_runtime': canonical,
    }
