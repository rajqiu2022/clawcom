"""Resolve shared-memory scopes from trusted Hub-side Claw relationships."""

from app import db
from app.memory_models import MemoryScopeRef, new_uuid


class MemoryScopeError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _get_or_create(scope, owner_username=None, project_id=None):
    query = MemoryScopeRef.query.filter_by(scope=scope)
    if scope == "owner":
        query = query.filter_by(owner_username=owner_username)
    else:
        query = query.filter_by(project_id=project_id)
    row = query.first()
    if row:
        return row.id
    row = MemoryScopeRef(
        id=new_uuid(),
        scope=scope,
        owner_username=owner_username,
        project_id=project_id,
    )
    db.session.add(row)
    db.session.flush()
    return row.id


def resolve_scope_ref(claw, scope):
    """Return an opaque scope ref derived only from server-side Claw state."""
    if scope == "owner":
        owner = (getattr(claw, "owner", "") or "").strip()
        if not owner:
            raise MemoryScopeError("OWNER_NOT_FOUND", "Claw owner is missing")
        return _get_or_create("owner", owner_username=owner)
    if scope == "project":
        project_id = getattr(claw, "project_id", None)
        if not project_id:
            raise MemoryScopeError(
                "AMBIGUOUS_PROJECT_SCOPE",
                "Claw has no deterministic project scope",
            )
        return _get_or_create("project", project_id=int(project_id))
    raise MemoryScopeError("SCOPE_NOT_SYNCABLE", "scope is not syncable")


def authorized_scope_refs(claw):
    refs = [("owner", resolve_scope_ref(claw, "owner"))]
    try:
        refs.append(("project", resolve_scope_ref(claw, "project")))
    except MemoryScopeError:
        pass
    return refs
