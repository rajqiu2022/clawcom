"""Project scoping helpers for test reports."""


def resolve_report_project_id(caller, requested_project_id):
    """Resolve project_id for a new report.

    OpenClaw token calls are scoped to the OpenClaw's bound project. This
    prevents an Agent from accidentally filing reports under a stale UI/default
    project id supplied by the caller payload.
    """
    if caller and caller.get('type') == 'openclaw' and caller.get('project_id'):
        return int(caller['project_id'])
    return requested_project_id


def resolve_report_is_hidden(caller, status: str, payload: dict) -> bool:
    """Resolve is_hidden for a new report.

    Agent draft reports default to hidden so automated smoke/draft outputs do not
    clutter the Web list until explicitly published or unhidden.
    """
    if 'is_hidden' in payload:
        return bool(payload['is_hidden'])
    if (payload.get('report_type') or '').strip() == 'workflow':
        return True
    if caller and caller.get('type') == 'openclaw' and status == 'draft':
        return True
    return False
