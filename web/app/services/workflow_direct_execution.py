"""Opt-in claim/fencing contract for Agent Direct Runner operations."""


DIRECT_EXECUTION_SCOPE = 'runner_operation'
DIRECT_EXECUTION_SCHEMA = 1
MIN_LEASE_SECONDS = 30
MAX_LEASE_SECONDS = 900
DEFAULT_LEASE_SECONDS = 180


def normalize_direct_execution_lease(value, *, path='direct_execution_lease'):
    if not isinstance(value, dict):
        raise ValueError(f'{path}: expected object')
    allowed = {'schema', 'required', 'scope', 'lease_seconds'}
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(
            f'{path}: unsupported fields: ' + ', '.join(unknown))
    try:
        schema = int(value.get('schema'))
    except (TypeError, ValueError) as exc:
        raise ValueError(f'{path}.schema: expected {DIRECT_EXECUTION_SCHEMA}') from exc
    if schema != DIRECT_EXECUTION_SCHEMA:
        raise ValueError(f'{path}.schema: expected {DIRECT_EXECUTION_SCHEMA}')
    if value.get('required') is not True:
        raise ValueError(f'{path}.required: expected true')
    if value.get('scope') != DIRECT_EXECUTION_SCOPE:
        raise ValueError(
            f'{path}.scope: expected {DIRECT_EXECUTION_SCOPE}')
    try:
        lease_seconds = int(
            value.get('lease_seconds', DEFAULT_LEASE_SECONDS))
    except (TypeError, ValueError) as exc:
        raise ValueError(f'{path}.lease_seconds: expected integer') from exc
    if not MIN_LEASE_SECONDS <= lease_seconds <= MAX_LEASE_SECONDS:
        raise ValueError(
            f'{path}.lease_seconds: expected '
            f'{MIN_LEASE_SECONDS}..{MAX_LEASE_SECONDS}')
    return {
        'schema': DIRECT_EXECUTION_SCHEMA,
        'required': True,
        'scope': DIRECT_EXECUTION_SCOPE,
        'lease_seconds': lease_seconds,
    }


def direct_execution_lease(step_type, step_config):
    if str(step_type or '') != 'agent_task' or not isinstance(step_config, dict):
        return None
    raw = step_config.get('direct_execution_lease')
    if raw is None:
        return None
    try:
        return normalize_direct_execution_lease(raw)
    except ValueError:
        # Persisted legacy/invalid definitions fail closed: they do not gain a
        # Direct Runner claim path merely by containing a similarly named key.
        return None


def workflow_step_claim_required(step_type, step_config):
    return bool(
        str(step_type or 'worker_task') == 'worker_task'
        or direct_execution_lease(step_type, step_config)
    )


def workflow_step_fencing_required(step_type, step_config):
    if direct_execution_lease(step_type, step_config):
        return True
    return bool(
        isinstance(step_config, dict)
        and step_config.get('require_fencing_token'))


def direct_execution_claim_allowed(step_type, step_config, claim_scope):
    policy = direct_execution_lease(step_type, step_config)
    return bool(
        policy and str(claim_scope or '').strip() == policy['scope'])
