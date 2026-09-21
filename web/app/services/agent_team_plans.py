"""Team plan authoring and schedule views; not a second execution scheduler."""
from datetime import date, timedelta
from sqlalchemy import and_, or_, func
from app import db
from app.models import TestPlan, TestTask, TestIteration, OpenClawInstance, _now
from app.services.agent_teams import TeamError, load_team, integer


def access(team, write=False):
    from app.api.mission_stages import _actor, _can_access_project, _can_administer
    actor = _actor()
    if not actor:
        raise TeamError('AUTH_REQUIRED', '请先认证', 401)
    if not _can_access_project(actor, team.project_id):
        raise TeamError('TEAM_ACCESS_DENIED', '无权访问团队项目', 403)
    manager = (actor['type'] == 'user' and _can_administer(actor, team.project_id)) or (
        actor['type'] == 'claw' and actor['claw'].project_id == team.project_id and (
            actor['id'] == team.primary_manager_claw_id or (
                actor['id'] == team.backup_manager_claw_id == team.active_manager_claw_id
                and team.manager_lease_expires_at and team.manager_lease_expires_at > _now())))
    if write and not manager:
        raise TeamError('TEAM_PLAN_MANAGER_REQUIRED', '仅团队测试经理或项目管理员可管理团队计划', 403)
    return actor, bool(manager)


def bind(data, plan=None):
    team_id = data.get('team_id', plan.team_id if plan else None)
    old_team_id = plan.team_id if plan else None
    if old_team_id:
        access(load_team(old_team_id, lock=True), write=True)
    if team_id is None:
        if old_team_id:
            raise TeamError('TEAM_PLAN_REBIND_FORBIDDEN', '团队计划不能通过编辑解除归属', 409)
        return None
    integer(team_id, 'team_id')
    if old_team_id and team_id != old_team_id:
        raise TeamError('TEAM_PLAN_REBIND_FORBIDDEN', '计划已归属其他团队', 409)
    team = load_team(team_id, lock=True)
    access(team, write=True)
    if not old_team_id and team.status != 'active':
        raise TeamError('TEAM_PLAN_INACTIVE', '仅启用中的团队可创建或关联计划', 409)
    project_id = data.get('project_id', plan.project_id if plan else team.project_id)
    if (type(project_id) is not int or project_id != team.project_id
            or (plan and plan.project_id != team.project_id)):
        raise TeamError('TEAM_PLAN_SCOPE_MISMATCH', '测试计划与团队必须属于同一项目', 400)
    iteration_id = data.get('iteration_id', plan.iteration_id if plan else None)
    if iteration_id is not None:
        iteration = db.session.get(TestIteration, iteration_id) if type(iteration_id) is int else None
        if not iteration or iteration.project_id != team.project_id:
            raise TeamError('TEAM_PLAN_SCOPE_MISMATCH', '迭代必须属于团队项目', 400)
    if plan:
        from app.models_plan_supervision import PlanSupervisor
        sup = db.session.get(PlanSupervisor, plan.id)
        if sup and sup.team_id != team.id:
            raise TeamError('TEAM_PLAN_SUPERVISION_CONFLICT', '计划的监督团队不一致，不能更改归属', 409)
    data['team_id'], data['project_id'] = team.id, team.project_id
    return team


def validate_dates(data, plan=None):
    for key in ('start_date', 'end_date'):
        raw = data.get(key, getattr(plan, key, None))
        try:
            data[key] = str(raw if isinstance(raw, date) else date.fromisoformat(raw))
        except (ValueError, TypeError):
            raise TeamError('TEAM_PLAN_DATE_INVALID', '计划日期须为 YYYY-MM-DD', 400)
    if data['end_date'] < data['start_date']:
        raise TeamError('TEAM_PLAN_DATE_INVALID', '结束日期不能早于开始日期', 400)
    if 'name' in data and (not isinstance(data['name'], str) or not data['name'].strip() or len(data['name']) > 200):
        raise TeamError('TEAM_PLAN_INVALID', '计划名称须为 1–200 字符', 400)
    if data.get('status', 'draft') not in ('draft', 'active', 'completed', 'archived'):
        raise TeamError('TEAM_PLAN_INVALID', '无效的计划状态', 400)


def date_window(period, raw_date):
    if period not in ('day', 'week', 'all'):
        raise TeamError('TEAM_PLAN_PERIOD_INVALID', 'period 须为 day/week/all', 400)
    try:
        anchor = date.fromisoformat(raw_date) if raw_date else _now().date()
        start = anchor - timedelta(days=anchor.weekday()) if period == 'week' else anchor
        end = start + timedelta(days=6) if period == 'week' else start
    except (ValueError, TypeError, OverflowError):
        raise TeamError('TEAM_PLAN_DATE_INVALID', 'date 须为有效 YYYY-MM-DD', 400)
    return anchor, start, end


def task_window(start, end):
    # One missing boundary acts as a one-day task; completely undated is separate.
    return and_(func.coalesce(TestTask.start_date, TestTask.end_date) <= end,
                func.coalesce(TestTask.end_date, TestTask.start_date) >= start)


def overview(team, period, raw_date, limit, offset):
    _, manage = access(team)
    anchor, start, end = date_window(period, raw_date)
    plans = TestPlan.query.filter_by(team_id=team.id, project_id=team.project_id)
    tasks = TestTask.query.join(TestPlan).filter(TestPlan.team_id == team.id,
                                               TestPlan.project_id == team.project_id)
    if period != 'all':
        plans = plans.filter(TestPlan.status != 'archived', or_(
            and_(TestPlan.start_date <= end, TestPlan.end_date >= start),
            TestPlan.tasks.any(task_window(start, end))))
        tasks = tasks.filter(TestPlan.status != 'archived', task_window(start, end))
    total = plans.count()
    counts = dict(tasks.with_entities(TestTask.status, func.count(TestTask.id)).group_by(TestTask.status).all())
    overdue = and_(TestTask.end_date < _now().date(), TestTask.status.notin_(['completed', 'skipped']))
    summary = {'total': sum(counts.values()), 'completed': counts.get('completed', 0),
               'in_progress': counts.get('in_progress', 0), 'blocked': counts.get('blocked', 0),
               'pending': counts.get('pending', 0) + counts.get('assigned', 0),
               'skipped': counts.get('skipped', 0), 'overdue': tasks.filter(overdue).count()}
    rows = plans.order_by(TestPlan.start_date.desc(), TestPlan.id.desc()).offset(offset).limit(limit).all()
    ids = [p.id for p in rows]
    period_counts = dict(tasks.filter(TestTask.plan_id.in_(ids)).with_entities(
        TestTask.plan_id, func.count(TestTask.id)).group_by(TestTask.plan_id).all()) if ids else {}
    unscheduled = dict(db.session.query(TestTask.plan_id, func.count(TestTask.id)).filter(
        TestTask.plan_id.in_(ids), TestTask.start_date.is_(None), TestTask.end_date.is_(None)).group_by(
            TestTask.plan_id).all()) if ids else {}
    grouped = db.session.query(TestTask.plan_id, TestTask.status, func.count(TestTask.id)).filter(
        TestTask.plan_id.in_(ids)).group_by(TestTask.plan_id, TestTask.status).all() if ids else []
    stats = {}
    for pid, status, count in grouped:
        stats.setdefault(pid, {})[status] = count
    items = []
    for plan in rows:
        all_counts = stats.get(plan.id, {})
        plan_total = sum(all_counts.values())
        selected = tasks.filter(TestTask.plan_id == plan.id)
        # Bounded preview queries (6 plans per page), no large task/case serialization.
        preview = selected.outerjoin(OpenClawInstance, TestTask.assignee_claw_id == OpenClawInstance.id).with_entities(
            TestTask, OpenClawInstance.name).order_by(TestTask.end_date.is_(None), TestTask.end_date,
                                                   TestTask.priority, TestTask.id).limit(5).all()
        items.append({'id': plan.id, 'name': plan.name, 'status': plan.status,
            'team_id': team.id, 'start_date': str(plan.start_date), 'end_date': str(plan.end_date),
            'url': '/testplans?plan_id=%s' % plan.id,
            'total_tasks': plan_total, 'completed_tasks': all_counts.get('completed', 0),
            'progress': round(all_counts.get('completed', 0) / plan_total * 100) if plan_total else 0,
            'period_tasks': period_counts.get(plan.id, 0),
            'unscheduled_tasks': unscheduled.get(plan.id, 0),
            'tasks': [{'id': t.id, 'name': t.name, 'status': t.status, 'priority': t.priority,
                       'progress': max(0, min(100, t.progress or 0)),
                       'assignee': name or t.assignee_username or '未指派',
                       'start_date': str(t.start_date) if t.start_date else None,
                       'end_date': str(t.end_date) if t.end_date else None,
                       'overdue': bool(t.end_date and t.end_date < _now().date() and t.status not in ('completed', 'skipped'))}
                      for t, name in preview]})
    return {'team_id': team.id, 'can_manage': manage, 'period': period, 'date': str(anchor),
            'start_date': str(start) if period != 'all' else None,
            'end_date': str(end) if period != 'all' else None, 'timezone': 'Asia/Shanghai',
            'items': items, 'total': total, 'limit': limit, 'offset': offset, 'summary': summary}
