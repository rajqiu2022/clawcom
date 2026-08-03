"""Feature flags and sidecar config for shared memory."""

import json

from app import db


FLAG_DEFAULTS = {
    "memory_api_enabled": "0",
    "memory_sync_advertise_enabled": "0",
    "memory_sync_enabled": "0",
    "memory_pull_enabled": "0",
    "memory_push_enabled": "0",
    "memory_dual_read_enabled": "0",
    "memo_decision_migration_enabled": "0",
    "memo_decision_read_enabled": "1",
    "memory_admin_ui_enabled": "0",
    "memory_sync_claw_allowlist": "[]",
}


def _system_config_values(keys):
    try:
        from app.models import SystemConfig

        rows = SystemConfig.query.filter(SystemConfig.config_key.in_(keys)).all()
        return {row.config_key: row.value for row in rows}
    except Exception:
        # Tests that load this module without the full models table still get
        # conservative defaults.
        db.session.rollback()
        return {}


def flag_enabled(key):
    value = (_system_config_values([key]).get(key) or FLAG_DEFAULTS.get(key) or "0")
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _claw_allowed(claw_id):
    raw = _system_config_values(["memory_sync_claw_allowlist"]).get(
        "memory_sync_claw_allowlist",
        FLAG_DEFAULTS["memory_sync_claw_allowlist"],
    )
    try:
        values = json.loads(raw or "[]")
    except ValueError:
        values = []
    return int(claw_id) in {int(value) for value in values if str(value).isdigit()}


def memory_api_enabled():
    return flag_enabled("memory_api_enabled")


def memory_push_enabled():
    return flag_enabled("memory_push_enabled")


def memory_pull_enabled():
    return flag_enabled("memory_pull_enabled")


def build_memory_sync_config(claw):
    if not (
        flag_enabled("memory_sync_advertise_enabled")
        and flag_enabled("memory_sync_enabled")
        and _claw_allowed(getattr(claw, "id", 0))
    ):
        return None
    claw_id = int(claw.id)
    return {
        "enabled": True,
        "pull_enabled": memory_pull_enabled(),
        "push_enabled": memory_push_enabled(),
        "protocol_version": 1,
        "delta_path": f"/api/v1/openclaws/{claw_id}/memories/delta",
        "mutations_path": f"/api/v1/openclaws/{claw_id}/memories/mutations",
        "poll_interval_seconds": 30,
        "poll_jitter_seconds": 5,
        "batch_max_items": 100,
        "batch_max_bytes": 262144,
        "sync_scopes": ["owner", "project"],
        "conversation_mode": "hub_opaque_only",
    }


def shared_memory_capability():
    return {
        "version": 1,
        "hub_canonical": True,
        "supports_delta": True,
        "supports_mutations": True,
        "supports_tombstone": True,
    }
