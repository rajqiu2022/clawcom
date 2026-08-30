"""Authoritative Agent todo scheduling in China Standard Time.

Database timestamps are stored as naive CST values for legacy compatibility.
This module keeps that convention internally while emitting explicit +08:00
timestamps at API boundaries.
"""

import calendar
from datetime import date, datetime, time, timedelta, timezone


TODO_TIMEZONE = 'Asia/Shanghai'
CST = timezone(timedelta(hours=8))
TERMINAL_TODO_STATUSES = frozenset({
    'submitted', 'completed', 'approved', 'skipped', 'retry_failed',
})


def cst_now_naive():
    return datetime.now(CST).replace(tzinfo=None)


def cst_iso(value=None):
    current = value or cst_now_naive()
    if current.tzinfo is None:
        current = current.replace(tzinfo=CST)
    else:
        current = current.astimezone(CST)
    return current.isoformat(timespec='seconds')


def _schedule_time(value):
    raw = str(value or '').strip()
    if not raw:
        return time(0, 0)
    try:
        parsed = datetime.strptime(raw, '%H:%M').time()
        return parsed.replace(second=0, microsecond=0)
    except (TypeError, ValueError):
        # 历史脏配置不能让正常待办永久阻断；按当天 00:00 保守放行。
        return time(0, 0)


def _month_due_date(year, month, day):
    maximum = calendar.monthrange(year, month)[1]
    return date(year, month, min(max(int(day or 1), 1), maximum))


def _next_month(year, month):
    return (year + 1, 1) if month == 12 else (year, month + 1)


def _due_datetime(todo, now):
    """Return (due_at, cycle_matches_today) for the active/next cycle."""
    today = now.date()
    scheduled_time = _schedule_time(getattr(todo, 'schedule_time', None))
    schedule_type = str(getattr(todo, 'schedule_type', None) or 'daily').lower()

    if schedule_type == 'once':
        created_at = getattr(todo, 'created_at', None) or now
        if getattr(todo, 'schedule_time', None):
            due_at = datetime.combine(created_at.date(), scheduled_time)
        else:
            due_at = created_at.replace(microsecond=0)
        return due_at, due_at.date() <= today

    if schedule_type == 'weekly':
        target = int(getattr(todo, 'schedule_day', None) or today.isoweekday())
        target = min(max(target, 1), 7)
        delta = (target - today.isoweekday()) % 7
        due_date = today + timedelta(days=delta)
        return datetime.combine(due_date, scheduled_time), delta == 0

    if schedule_type == 'monthly':
        target_day = int(getattr(todo, 'schedule_day', None) or today.day)
        this_due = _month_due_date(today.year, today.month, target_day)
        if today <= this_due:
            due_date = this_due
        else:
            year, month = _next_month(today.year, today.month)
            due_date = _month_due_date(year, month, target_day)
        return datetime.combine(due_date, scheduled_time), due_date == today

    # daily 以及未知历史值均按每天执行，避免旧任务被永久丢弃。
    return datetime.combine(today, scheduled_time), True


def todo_schedule_state(todo, today_log=None, now=None):
    """Calculate whether one todo may execute now, using Asia/Shanghai."""
    current = now or cst_now_naive()
    if current.tzinfo is not None:
        current = current.astimezone(CST).replace(tzinfo=None)
    due_at, cycle_matches_today = _due_datetime(todo, current)
    enabled = bool(getattr(todo, 'enabled', True))
    log_status = str(getattr(today_log, 'status', '') or '')
    terminal = log_status in TERMINAL_TODO_STATUSES
    is_due = bool(
        enabled and not terminal and cycle_matches_today and current >= due_at)

    if not enabled:
        today_status = 'disabled'
    elif log_status:
        today_status = log_status
    elif is_due:
        today_status = 'pending'
    else:
        today_status = 'scheduled'

    return {
        'today_status': today_status,
        'is_due': is_due,
        'is_overdue': bool(is_due and current > due_at),
        'scheduled_for_today': bool(due_at.date() == current.date()),
        'due_at': cst_iso(due_at),
        'next_due_at': cst_iso(due_at),
        'timezone': TODO_TIMEZONE,
    }
