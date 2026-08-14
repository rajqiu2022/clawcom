"""Server-side deployment secret lookup.

Deployment credentials live in the Hub secret vault and are owned by the
single global admin Claw.  Keeping this lookup server-side avoids exposing
shared deployment credentials through request payloads or ``system_config``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from app.models import ClawSecret, OpenClawInstance


logger = logging.getLogger(__name__)


def _select_secret_value(rows, ordered_keys: Iterable[str]) -> str:
    """Return the first non-empty secret using caller-provided key priority."""
    by_key = {}
    for row in rows:
        by_key.setdefault(row.key, row)

    for key in ordered_keys:
        row = by_key.get(key)
        if not row:
            continue
        try:
            value = (row.get_value() or '').strip()
        except Exception:
            logger.exception('failed to decrypt deployment secret %s', key)
            continue
        if value:
            return value
    return ''


def get_deployment_secret(keys: Iterable[str]) -> str:
    """Read a deployment secret from the global admin Claw's vault.

    ``keys`` is ordered so canonical names can precede legacy aliases.  Only
    active, project-less admin Claws qualify as system deployment owners.
    """
    ordered_keys = tuple(dict.fromkeys(str(key).strip() for key in keys if key))
    if not ordered_keys:
        return ''

    try:
        rows = (
            ClawSecret.query
            .join(OpenClawInstance,
                  OpenClawInstance.id == ClawSecret.owner_claw_id)
            .filter(
                OpenClawInstance.role == 'admin',
                OpenClawInstance.project_id.is_(None),
                OpenClawInstance.deleted_at.is_(None),
                ClawSecret.key.in_(ordered_keys),
            )
            .order_by(ClawSecret.updated_at.desc(), ClawSecret.id.desc())
            .all()
        )
    except Exception:
        logger.exception('failed to query deployment secrets: %s', ordered_keys)
        return ''

    return _select_secret_value(rows, ordered_keys)
