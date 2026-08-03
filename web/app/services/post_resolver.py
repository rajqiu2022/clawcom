"""Agent post resolution helpers.

This module is intentionally pure: API code can pass rows already flattened
from SQLAlchemy, while tests can exercise the scheduling rules without a Hub app.
"""

from datetime import datetime, timedelta


ONLINE_STATUSES = {'online', 'working', 'busy', '学习', '工作'}
DEFAULT_ONLINE_WINDOW_SEC = 180


def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _is_recent(candidate, now, online_window_sec):
    last = candidate.get('last_activity')
    if not last:
        return False
    if isinstance(last, str):
        return True
    try:
        return now - last <= timedelta(seconds=online_window_sec)
    except Exception:
        return False


def _candidate_matches(candidate, post_key, project_id, now, online_window_sec, exclude_ids):
    if str(candidate.get('post_key') or '') != str(post_key or ''):
        return False
    if project_id is not None and candidate.get('project_id') not in (None, project_id):
        return False
    claw_id = _as_int(candidate.get('claw_id'))
    if claw_id in exclude_ids:
        return False
    if _as_int(candidate.get('profile_version')) < _as_int(candidate.get('required_profile_version'), 1):
        return False
    if not candidate.get('exam_passed', True):
        return False
    status = str(candidate.get('status') or '').strip()
    if status == 'deleted' or status == 'offline':
        return False
    if status not in ONLINE_STATUSES and not _is_recent(candidate, now, online_window_sec):
        return False
    return True


def resolve_post_candidate(candidates, *, post_key, project_id=None, now=None,
                           exclude_claw_ids=None, online_window_sec=DEFAULT_ONLINE_WINDOW_SEC):
    """Return the best claw for a post.

    Selection order is deliberately boring and explainable: valid candidates are
    sorted by active step count, then most recent heartbeat, then id.
    """
    now = now or datetime.utcnow()
    exclude_ids = {_as_int(v) for v in (exclude_claw_ids or set())}
    matched = [
        c for c in (candidates or [])
        if _candidate_matches(c, post_key, project_id, now, online_window_sec, exclude_ids)
    ]
    if not matched:
        return {'claw_id': None, 'reason': 'no_available_agent'}

    def score(c):
        last = c.get('last_activity')
        last_ts = last.timestamp() if hasattr(last, 'timestamp') else 0
        return (_as_int(c.get('active_steps')), -last_ts, _as_int(c.get('claw_id')))

    best = sorted(matched, key=score)[0]
    return {
        'claw_id': _as_int(best.get('claw_id')),
        'reason': 'matched',
        'candidate': best,
    }
