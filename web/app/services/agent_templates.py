"""Pure helpers for Agent template permissions."""


ADMIN_USER_ROLES = {'super_admin', 'admin'}


def agent_template_actor_name(actor, actor_type, default='system'):
    """Return stable display name for template audit records."""
    if not actor:
        return default
    if actor_type == 'claw':
        return getattr(actor, 'name', None) or getattr(actor, 'safe_name', None) or default
    return (
        getattr(actor, 'username', None)
        or getattr(actor, 'display_name', None)
        or default
    )


def can_edit_agent_template(actor, actor_type, template_owner_claw_id):
    """Return whether actor can edit template content/files.

    Web admins can edit all templates. Bearer Agents can only edit templates
    they own; review/apply/delete stay on dedicated admin endpoints.
    """
    if not actor or not actor_type:
        return False
    if actor_type == 'user':
        return getattr(actor, 'role', '') in ADMIN_USER_ROLES
    if actor_type == 'claw':
        try:
            return template_owner_claw_id is not None and int(template_owner_claw_id) == int(getattr(actor, 'id', 0))
        except (TypeError, ValueError):
            return False
    return False


def can_apply_agent_template(actor, actor_type, template_owner_claw_id,
                             template_status, target_claw_id):
    """Return whether actor can apply a template.

    Web admins keep full control. Bearer Agents may self-serve only approved
    templates, and only to their own OpenClaw identity.
    """
    if not actor or not actor_type:
        return False
    if actor_type == 'user':
        return getattr(actor, 'role', '') in ADMIN_USER_ROLES
    if actor_type == 'claw':
        if template_status != 'approved':
            return False
        try:
            return int(target_claw_id) == int(getattr(actor, 'id', 0))
        except (TypeError, ValueError):
            return False
    return False
