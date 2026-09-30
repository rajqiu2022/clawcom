"""Authenticated cross-project references, separate from anonymous share links."""
from flask import current_app, g
from app.models import SharedResourcePolicy


def enabled():
    value = current_app.config.get('CODE_ANALYSIS_ENABLED', False)
    return value is True or str(value).lower() in ('1', 'true', 'yes', 'on')


def policy_for(kind, resource_id):
    if not enabled():
        return None
    # One query per request, not one query for every market card.
    if not hasattr(g, '_resource_share_policies'):
        g._resource_share_policies = {
            (row.resource_kind, row.resource_id): row
            for row in SharedResourcePolicy.query.all()}
    return g._resource_share_policies.get((kind, int(resource_id)))


def invalidate():
    g.pop('_resource_share_policies', None)


def is_global_admin(actor):
    if not actor:
        return False
    if actor['type'] == 'claw':
        return actor['claw'].role == 'admin' and actor['claw'].project_id is None
    if actor['type'] != 'user':
        return False
    from app.api.knowledge_notebooks import _actor_project_ids
    role = actor['user'].role
    return role == 'super_admin' or (role == 'admin' and not _actor_project_ids(actor))


def can_write(policy, actor):
    if not actor:
        return False
    if actor['type'] == 'user' and actor['user'].role == 'guest':
        return False
    if is_global_admin(actor):
        return True
    from app.api.knowledge_notebooks import _can_access_project
    return bool(policy.owner_project_id and
                _can_access_project(actor, policy.owner_project_id))


def can_read(policy, actor):
    if not actor:
        return False
    if can_write(policy, actor):
        return True
    if policy.scope == 'all':
        return actor['type'] in ('user', 'claw')
    if policy.scope == 'selected':
        from app.api.knowledge_notebooks import _actor_project_ids
        return bool(_actor_project_ids(actor) & set(policy.project_ids_json or []))
    return False


def resource_readable(kind, row, actor):
    policy = policy_for(kind, row.id)
    if policy:
        if not can_read(policy, actor):
            return False
        # Sharing a package does not publish a draft/private/off-shelf Skill.
        if kind == 'skill' and not can_write(policy, actor):
            return (not row.is_deleted and row.visibility != 'private'
                    and row.scope != 'admin' and row.review_status == 'approved')
        if kind == 'knowledge' and not can_write(policy, actor):
            return row.status == 'approved' and not row.archived_at
        return True
    if kind == 'knowledge':
        from app.api.knowledge_notebooks import _can_access_project
        return bool(actor and (row.project_id is None or
                               _can_access_project(actor, row.project_id)))
    if row.is_deleted or row.scope == 'admin' or row.review_status != 'approved':
        return False
    if row.visibility == 'private':
        return actor and actor['type'] == 'claw' and row.owner_claw_id == actor['id']
    from app.api.knowledge_notebooks import _actor_project_ids
    projects = {int(p) for p in (row.applicable_projects or []) if str(p).isdigit()}
    return bool(actor and (row.scope == 'global' or not projects or
                          is_global_admin(actor) or projects & _actor_project_ids(actor)))


def policy_payload(policy):
    return {'scope': policy.scope, 'project_ids': policy.project_ids_json or [],
            'owner_project_id': policy.owner_project_id,
            'revision': policy.revision}
