"""Team plan authoring and schedule views; not a second execution scheduler."""
from datetime import date, timedelta
from sqlalchemy import and_, or_, func
from app import db
from app.models import (TestPlan, TestPlanReport, TestReport, TestTask,
                        TestTaskOccurrence,
                        TestTaskReport, TestIteration, OpenClawInstance, _now)
from app.services.agent_teams import TeamError, load_team, integer
from app.services.test_task_references import serialize_task_references


def access(team, write=False):
    from app.api.mission_stages import _actor, _can_access_project, _can_administer
    actor = _actor()
    if not actor:
        raise TeamError('AUTH_REQUIRED', '请先认证', 401)
    if not _can_access_project(actor, team.project_id):
        raise TeamError('TEAM_ACCESS_DENIED', '无权访问团队项目', 403)
    manager = (actor['type'] == 'user' and _can_administer(actor, team.project_id)) or (
        actor['type'] == 'claw' and actor['claw'].project_id == team.project_id
        and team.has_manager_authority(actor['id']))
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
    if not old_team_id and team.status != 'active':
        raise TeamError('TEAM_PLAN_INACTIVE', '仅启用中的团队可创建或关联计划', 409)
    access(team, write=True)
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
    from app.models_plan_supervision import PlanSupervisor
    supervisors = {row.plan_id: row for row in PlanSupervisor.query.filter(
        PlanSupervisor.plan_id.in_(ids)).all()} if ids else {}
    period_counts = dict(tasks.filter(TestTask.plan_id.in_(ids)).with_entities(
        TestTask.plan_id, func.count(TestTask.id)).group_by(TestTask.plan_id).all()) if ids else {}
    unscheduled = dict(db.session.query(TestTask.plan_id, func.count(TestTask.id)).filter(
        TestTask.plan_id.in_(ids), TestTask.start_date.is_(None), TestTask.end_date.is_(None)).group_by(
            TestTask.plan_id).all()) if ids else {}
    legacy_report_counts = dict(db.session.query(
        TestPlanReport.plan_id, func.count(TestPlanReport.id)
    ).filter(TestPlanReport.plan_id.in_(ids)).group_by(
        TestPlanReport.plan_id).all()) if ids else {}
    global_report_counts = dict(db.session.query(
        TestReport.source_ref_id, func.count(TestReport.id)
    ).filter(TestReport.source_ref_type == 'test_plan',
             TestReport.source_ref_id.in_(ids),
             TestReport.is_hidden.is_(False),
             TestReport.is_deleted.is_(False)).group_by(
        TestReport.source_ref_id).all()) if ids else {}
    linked_plan_report_counts = dict(db.session.query(
        TestPlanReport.plan_id, func.count(TestPlanReport.id)
    ).join(TestReport, TestPlanReport.linked_test_report_id == TestReport.id).filter(
        TestPlanReport.plan_id.in_(ids),
        TestReport.source_ref_type == 'test_plan',
        TestReport.source_ref_id == TestPlanReport.plan_id,
        TestReport.is_hidden.is_(False),
        TestReport.is_deleted.is_(False)).group_by(
        TestPlanReport.plan_id).all()) if ids else {}
    grouped = db.session.query(TestTask.plan_id, TestTask.status, func.count(TestTask.id)).filter(
        TestTask.plan_id.in_(ids)).group_by(TestTask.plan_id, TestTask.status).all() if ids else []
    stats = {}
    for pid, status, count in grouped:
        stats.setdefault(pid, {})[status] = count
    previews, preview_task_ids = {}, []
    for plan in rows:
        selected = tasks.filter(TestTask.plan_id == plan.id)
        # Return every task in the selected date window.  The team page keeps
        # the first five visible and expands the rest on demand; truncating the
        # API here made the remaining tasks impossible to inspect in-place.
        # This still serializes task summaries only, never task cases.
        preview = selected.outerjoin(OpenClawInstance, TestTask.assignee_claw_id == OpenClawInstance.id).with_entities(
            TestTask, OpenClawInstance.name).order_by(TestTask.end_date.is_(None), TestTask.end_date,
                                                   TestTask.priority, TestTask.id).all()
        previews[plan.id] = preview
        preview_task_ids.extend(t.id for t, _ in preview)
    legacy_task_report_counts = dict(db.session.query(
        TestTaskReport.task_id, func.count(TestTaskReport.id)
    ).filter(TestTaskReport.task_id.in_(preview_task_ids)).group_by(
        TestTaskReport.task_id).all()) if preview_task_ids else {}
    global_task_report_counts = dict(db.session.query(
        TestReport.source_ref_id, func.count(TestReport.id)
    ).filter(TestReport.source_ref_type == 'test_task',
             TestReport.source_ref_id.in_(preview_task_ids),
             TestReport.is_hidden.is_(False),
             TestReport.is_deleted.is_(False)).group_by(
        TestReport.source_ref_id).all()) if preview_task_ids else {}
    linked_task_report_counts = dict(db.session.query(
        TestTaskReport.task_id, func.count(TestTaskReport.id)
    ).join(TestReport, TestTaskReport.linked_test_report_id == TestReport.id).filter(
        TestTaskReport.task_id.in_(preview_task_ids),
        TestReport.source_ref_type == 'test_task',
        TestReport.source_ref_id == TestTaskReport.task_id,
        TestReport.is_hidden.is_(False),
        TestReport.is_deleted.is_(False)).group_by(
        TestTaskReport.task_id).all()) if preview_task_ids else {}
    occurrence_rows = (TestTaskOccurrence.query.filter(
        TestTaskOccurrence.test_task_id.in_(preview_task_ids),
        TestTaskOccurrence.occurrence_date == anchor,
    ).order_by(TestTaskOccurrence.id.desc()).all()) if preview_task_ids else []
    current_occurrences = {}
    current_occurrence_rows = {}
    for occurrence in occurrence_rows:
        if occurrence.test_task_id not in current_occurrences:
            current_occurrences[occurrence.test_task_id] = occurrence.to_dict()
            current_occurrence_rows[occurrence.test_task_id] = occurrence
    items = []
    for plan in rows:
        all_counts = stats.get(plan.id, {})
        plan_total = sum(all_counts.values())
        preview = previews.get(plan.id, [])
        supervisor = supervisors.get(plan.id)
        supervisor_data = supervisor.to_dict() if supervisor else None
        legacy_report_count = legacy_report_counts.get(plan.id, 0)
        # Old plans may still carry their only report in TestPlan.report_content.
        # Treat that as one report without mutating data during a read-only overview.
        if not legacy_report_count and plan.report_content and plan.report_content.strip():
            legacy_report_count = 1
        report_count = (legacy_report_count + global_report_counts.get(plan.id, 0)
                        - linked_plan_report_counts.get(plan.id, 0))
        items.append({'id': plan.id, 'name': plan.name, 'status': plan.status,
            'team_id': team.id, 'start_date': str(plan.start_date), 'end_date': str(plan.end_date),
            'url': '/testplans?plan_id=%s' % plan.id,
            'report_count': report_count,
            'total_tasks': plan_total, 'completed_tasks': all_counts.get('completed', 0),
            'progress': round(all_counts.get('completed', 0) / plan_total * 100) if plan_total else 0,
            'period_tasks': period_counts.get(plan.id, 0),
            'unscheduled_tasks': unscheduled.get(plan.id, 0),
            'supervision': ({
                'status': supervisor_data['status'],
                'orchestrator_claw_id': supervisor_data['orchestrator_claw_id'],
                'mission_id': supervisor_data['mission_id'],
                'next_check_at': supervisor_data['next_check_at'],
                'manager_lease_active': bool(
                    (supervisor_data.get('manager_lease') or {}).get('active')),
                'manager_authority_active': bool(
                    (supervisor_data.get('manager_lease') or {}).get('active')),
                'lease_expires_at': (
                    (supervisor_data.get('manager_lease') or {}).get('expires_at')),
                'can_resume': bool(
                    manage and supervisor_data['status'] in (
                        'blocked', 'blocked_owner_gate', 'stopped')),
                'resume_api': (
                    '/test-plans/%s/supervision/resume' % plan.id),
            } if supervisor_data else None),
            'tasks': [{'id': t.id, 'name': t.name,
                       'description': t.description or '',
                       'references': serialize_task_references(
                           t, current_occurrence_rows.get(t.id)),
                       'status': ((current_occurrences.get(t.id) or {}).get('status')
                                  if t.schedule_enabled else t.status),
                       'template_status': t.status,
                       'schedule_enabled': bool(t.schedule_enabled),
                       'recurrence_type': t.recurrence_type or 'once',
                       'occurrence': current_occurrences.get(t.id),
                       'priority': t.priority,
                       'progress': (
                           100 if t.schedule_enabled and
                           (current_occurrences.get(t.id) or {}).get('status')
                           in ('completed', 'skipped') else
                           1 if t.schedule_enabled and
                           (current_occurrences.get(t.id) or {}).get('status')
                           == 'running' else
                           0 if t.schedule_enabled else
                           max(0, min(100, t.progress or 0))),
                       'assignee': name or t.assignee_username or '未指派',
                       'report_count': (legacy_task_report_counts.get(t.id, 0)
                                        + global_task_report_counts.get(t.id, 0)
                                        - linked_task_report_counts.get(t.id, 0)),
                       'start_date': str(t.start_date) if t.start_date else None,
                       'end_date': str(t.end_date) if t.end_date else None,
                       'overdue': bool(t.end_date and t.end_date < _now().date() and t.status not in ('completed', 'skipped'))}
                      for t, name in preview]})
    return {'team_id': team.id, 'can_manage': manage, 'period': period, 'date': str(anchor),
            'start_date': str(start) if period != 'all' else None,
            'end_date': str(end) if period != 'all' else None, 'timezone': 'Asia/Shanghai',
            'items': items, 'total': total, 'limit': limit, 'offset': offset, 'summary': summary}
