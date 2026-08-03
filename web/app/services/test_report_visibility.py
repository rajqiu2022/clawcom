"""Visibility helpers for test reports."""


def _value(row, key, default=None):
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


def can_view_hidden_report(caller, report, owned_claw_ids=None):
    """Return whether caller may view report under the hidden-report rule.

    Non-hidden reports are not restricted here. Hidden reports are visible only
    to the report submitter or, for web users, agents owned by that user.
    """
    if not _value(report, 'is_hidden', False):
        return True
    if not caller:
        return False

    caller_type = caller.get('type')
    submitter_type = _value(report, 'submitter_type')
    submitter_user_id = _value(report, 'submitter_user_id')
    submitter_claw_id = _value(report, 'submitter_claw_id')
    owned_claw_ids = set(owned_claw_ids or set())

    if caller_type == 'user':
        if submitter_type == 'user' and submitter_user_id == caller.get('user_id'):
            return True
        if submitter_type == 'openclaw' and submitter_claw_id in owned_claw_ids:
            return True
        return False

    if caller_type == 'openclaw':
        return submitter_type == 'openclaw' and submitter_claw_id == caller.get('claw_id')

    return False


def should_include_in_report_list(caller, report, include_hidden=False, owned_claw_ids=None):
    """Return whether a report should appear in the test report list.

    Hidden workflow reports are workflow-scoped artifacts. They are intentionally
    kept out of the report center even when the user asks to show hidden reports.
    """
    is_hidden = bool(_value(report, 'is_hidden', False))
    report_type = _value(report, 'report_type', '')
    if is_hidden and report_type == 'workflow':
        return False
    if not is_hidden:
        return True
    if not include_hidden:
        return False
    return can_view_hidden_report(caller, report, owned_claw_ids)
