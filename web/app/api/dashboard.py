from datetime import date, datetime, timedelta
from flask import jsonify, session as flask_session
from sqlalchemy import func
from app import db
from app.models import (OpenClawInstance, DailyReport, KnowledgeEntry,
                        Skill, Rule, OpenClawSkill, Project, Agent, Message, User,
                        ClawTodo, ClawTodoLog)
from app.api import api_bp
from app.api.agent_client import _cleanup_stale_connections
from app.services.todo_schedule import cst_now_naive, todo_schedule_state


def _infer_task_category(task_text):
    """根据任务文本推断分类"""
    t = (task_text or '').lower()
    if any(k in t for k in ['学习', 'learn', '课程', 'course', '阅读', 'read', '课题']):
        return 'learning'
    if any(k in t for k in ['讨论', 'discuss', '会议', 'meeting', 'review']):
        return 'discussion'
    if any(k in t for k in ['测试', 'test', '用例', 'case', 'bug']):
        return 'testing'
    return 'task'




def _get_visible_claws():
    """根据当前用户角色返回可见的 OpenClaw 列表（排除已删除）"""
    all_claws = OpenClawInstance.query.filter(
        OpenClawInstance.status != 'deleted'
    ).order_by(OpenClawInstance.created_at).all()

    uid = flask_session.get('user_id')
    user = User.query.get(uid) if uid else None
    user_role = user.role if user else 'guest'

    # 仅 super_admin 可见 admin 角色（龙虾王）
    visible = all_claws if user_role == 'super_admin' else [c for c in all_claws if c.role != 'admin']

    # 非超管仅看自己项目
    if user_role != 'super_admin' and user:
        project_ids = set()
        for pid in (user.managed_projects or []):
            try:
                project_ids.add(int(pid))
            except Exception:
                continue
        if user.bound_claw_id:
            b = OpenClawInstance.query.get(user.bound_claw_id)
            if b:
                if b.project_id:
                    project_ids.add(int(b.project_id))
                elif b.project_name:
                    p = Project.query.filter_by(name=b.project_name).first()
                    if p:
                        project_ids.add(int(p.id))
        project_names = {p.name for p in Project.query.filter(Project.id.in_(list(project_ids))).all()} if project_ids else set()
        if project_ids or project_names:
            visible = [c for c in visible if (c.project_id in project_ids) or (c.project_name in project_names)]
        elif user.bound_claw_id:
            visible = [c for c in visible if c.id == user.bound_claw_id]
        else:
            visible = []
    return visible


@api_bp.route('/dashboard/stats', methods=['GET'])
def dashboard_stats():
    """Dashboard 统计数据（已优化：消除 N+1 查询）"""
    # 清理超时的 SSE 连接
    _cleanup_stale_connections()

    today = cst_now_naive().date()

    visible_claws = _get_visible_claws()
    visible_ids = {c.id for c in visible_claws}

    # 在线 OpenClaw 数量
    online_count = sum(1 for c in visible_claws if c.status in ('工作', '学习', '摸鱼', 'online'))
    total_claws = len(visible_claws)

    # ===== 批量预加载：今日日报 =====
    if visible_ids:
        today_reports = DailyReport.query.filter(
            DailyReport.report_date == today,
            DailyReport.openclaw_id.in_(visible_ids)
        ).all()
    else:
        today_reports = []

    # 按 openclaw_id 索引（取最新一条）
    today_report_map = {}
    for r in today_reports:
        existing = today_report_map.get(r.openclaw_id)
        if not existing or (r.report_time and (not existing.report_time or r.report_time > existing.report_time)):
            today_report_map[r.openclaw_id] = r

    def _count_tasks(tasks_json):
        if not tasks_json:
            return 0
        if isinstance(tasks_json, list):
            return len(tasks_json)
        if isinstance(tasks_json, str):
            return 1 if tasks_json.strip() else 0
        return 1

    today_tasks = sum(_count_tasks(r.tasks_completed) for r in today_reports)

    # 今日日报汇报率
    if visible_ids:
        today_reported = db.session.query(
            func.count(func.distinct(DailyReport.openclaw_id))
        ).filter(
            DailyReport.report_date == today,
            DailyReport.openclaw_id.in_(visible_ids)
        ).scalar() or 0
    else:
        today_reported = 0

    # 知识库总条目
    total_knowledge = KnowledgeEntry.query.count()

    # 待审核数
    pending_count = KnowledgeEntry.query.filter_by(status='pending_review').count()
    pending_count += Skill.query.filter_by(review_status='pending', is_deleted=False).count()
    pending_count += Rule.query.filter_by(review_status='pending', is_deleted=False).count()

    # 今日新增知识
    today_knowledge = KnowledgeEntry.query.filter(
        func.date(KnowledgeEntry.created_at) == today
    ).count()

    # 技能总数
    total_skills = Skill.query.count()
    evolved_skills = Skill.query.filter_by(category='evolved').count()

    # TAPD 绑定项目数
    tapd_projects = Project.query.filter(
        Project.tapd_workspace_id.isnot(None),
        Project.tapd_workspace_id != ''
    ).count()

    # 通信中心统计
    try:
        uid = flask_session.get('user_id')
        _user = User.query.get(uid) if uid else None
        _urole = _user.role if _user else 'guest'
        agent_query = Agent.query
        if _urole != 'super_admin':
            agent_query = agent_query.filter(Agent.role != 'admin')
        agents = agent_query.all()
        # 复用已加载的 visible_claws 而非重新查询
        online_statuses = {c.name: c.status in ('工作', '学习', '摸鱼', 'online') for c in visible_claws}
        online_agents = sum(1 for a in agents if online_statuses.get(a.name, a.status == 'online'))
        total_agents = len(agents)
        unread_messages = Message.query.filter_by(status='unread').count()
    except Exception:
        total_agents = 0
        online_agents = 0
        unread_messages = 0

    # 近30天知识增长趋势
    thirty_days_ago = today - timedelta(days=30)
    trend_data = (
        db.session.query(
            func.date(KnowledgeEntry.created_at).label('date'),
            func.count().label('count')
        )
        .filter(KnowledgeEntry.created_at >= thirty_days_ago)
        .group_by(func.date(KnowledgeEntry.created_at))
        .order_by(func.date(KnowledgeEntry.created_at))
        .all()
    )

    # 今日各标签新增
    category_stats = (
        db.session.query(
            KnowledgeEntry.category,
            func.count().label('count')
        )
        .filter(func.date(KnowledgeEntry.created_at) == today)
        .group_by(KnowledgeEntry.category)
        .all()
    )

    # ===== 批量预加载：所有启用待办的今日 log =====
    if visible_ids:
        all_enabled_todos = ClawTodo.query.filter(
            ClawTodo.openclaw_id.in_(visible_ids),
            ClawTodo.enabled == True
        ).all()
        enabled_todo_ids = [t.id for t in all_enabled_todos]
    else:
        all_enabled_todos = []
        enabled_todo_ids = []

    # 一次性批量查询所有今日 log（消除 N+1）
    today_logs_map = {}  # todo_id → ClawTodoLog
    if enabled_todo_ids:
        today_logs = ClawTodoLog.query.filter(
            ClawTodoLog.todo_id.in_(enabled_todo_ids),
            ClawTodoLog.log_date == today
        ).all()
        for log in today_logs:
            existing = today_logs_map.get(log.todo_id)
            if not existing or (log.created_at and (not existing.created_at or log.created_at > existing.created_at)):
                today_logs_map[log.todo_id] = log

    # 计算每个 claw 的待办数
    claw_todo_counts = {}
    for todo in all_enabled_todos:
        today_log = today_logs_map.get(todo.id)
        state = todo_schedule_state(todo, today_log=today_log)
        if ((state['is_due'] or state['scheduled_for_today'])
                and (not today_log or today_log.status == 'pending')):
            cid = todo.openclaw_id
            claw_todo_counts[cid] = claw_todo_counts.get(cid, 0) + 1

    # OpenClaw 概览（复用 today_report_map，不再逐个查 DailyReport）
    claw_summaries = []
    for c in visible_claws:
        today_report = today_report_map.get(c.id)
        claw_summaries.append({
            'id': c.id,
            'name': c.name,
            'role_title': c.role_title,
            'status': c.status,
            'last_activity': (str(c.last_activity) if c.last_activity else None),
            'today_tasks': claw_todo_counts.get(c.id, 0),
            'today_completed': (_count_tasks(today_report.tasks_completed) if today_report else 0),
            'today_summary': (today_report.ai_summary if today_report else None),
            'avatar': c.avatar,
            'reported_today': today_report is not None,
            'project_name': c.project.name if c.project else (c.project_name or ''),
        })

    # ============== 最近完成的任务（近3天日报） ==============
    three_days_ago = today - timedelta(days=3)
    recent_reports = DailyReport.query.filter(
        DailyReport.report_date >= three_days_ago,
        DailyReport.openclaw_id.in_(visible_ids if visible_ids else [0])
    ).order_by(DailyReport.report_date.desc(), DailyReport.report_time.desc()).all()

    claw_name_map = {c.id: c.name for c in visible_claws}  # 移到循环外
    recent_tasks = []
    for r in recent_reports[:50]:
        tasks = r.tasks_completed
        if not tasks:
            continue
        task_list = tasks if isinstance(tasks, list) else [str(tasks)]
        for t in task_list:
            if isinstance(t, dict):
                task_text = t.get('task', '') or t.get('name', '') or str(t)
            elif isinstance(t, str):
                task_text = t.strip()
            else:
                task_text = str(t)
            if not task_text:
                continue
            claw_name = claw_name_map.get(r.openclaw_id, '未知')
            recent_tasks.append({
                'task': task_text,
                'openclaw_name': claw_name,
                'openclaw_id': r.openclaw_id,
                'date': str(r.report_date),
                'category': _infer_task_category(task_text),
            })

    # ============== 今日待办（三阶段：待完成/已提交/已审核） ==============
    upcoming_todos = []
    submitted_todos = []
    if visible_ids:
        todos_q = ClawTodo.query.filter(
            ClawTodo.openclaw_id.in_(visible_ids)
        ).order_by(ClawTodo.created_at.desc()).all()

        # 批量查询所有待办的今日 log + 最近 log（消除 N+1）
        all_todo_ids = [t.id for t in todos_q]
        all_today_logs_map = {}
        all_latest_logs_map = {}
        if all_todo_ids:
            # 今日 logs
            all_today_logs = ClawTodoLog.query.filter(
                ClawTodoLog.todo_id.in_(all_todo_ids),
                ClawTodoLog.log_date == today
            ).all()
            for log in all_today_logs:
                existing = all_today_logs_map.get(log.todo_id)
                if not existing or (log.created_at and (not existing.created_at or log.created_at > existing.created_at)):
                    all_today_logs_map[log.todo_id] = log

            # 对于没有今日 log 的待办，批量查最新 submitted log
            missing_today_ids = [tid for tid in all_todo_ids if tid not in all_today_logs_map]
            if missing_today_ids:
                # 取每个 todo 最新的一条 log
                latest_logs = ClawTodoLog.query.filter(
                    ClawTodoLog.todo_id.in_(missing_today_ids)
                ).order_by(ClawTodoLog.log_date.desc(), ClawTodoLog.created_at.desc()).all()
                for log in latest_logs:
                    if log.todo_id not in all_latest_logs_map:
                        all_latest_logs_map[log.todo_id] = log

        for todo in todos_q:
            today_log = all_today_logs_map.get(todo.id)
            if not today_log:
                latest_log = all_latest_logs_map.get(todo.id)
                if latest_log and latest_log.status == 'submitted':
                    today_log = latest_log

            state = todo_schedule_state(todo, today_log=today_log)
            todo_base = {
                'id': todo.id,
                'title': todo.title,
                'description': todo.description or '',
                'openclaw_name': claw_name_map.get(todo.openclaw_id, '未知'),
                'openclaw_id': todo.openclaw_id,
                'schedule_type': todo.schedule_type,
                'schedule_time': todo.schedule_time or '',
                'urgency_level': todo.urgency_level or 'flexible',
                'priority': todo.priority or 'P1',
                'task_category': todo.task_category or 'routine',
                'today_status': state['today_status'],
                'today_completed_at': str(today_log.completed_at) if today_log and today_log.completed_at else None,
                'result_summary': today_log.result_summary if today_log else None,
                'created_at': str(todo.created_at) if todo.created_at else None,
                'is_due': state['is_due'],
                'due_at': state['due_at'],
                'timezone': state['timezone'],
            }
            if (not today_log or today_log.status == 'pending') and todo.enabled and (
                    state['is_due'] or state['scheduled_for_today']):
                upcoming_todos.append(todo_base)
            elif today_log and today_log.status == 'submitted':
                submitted_todos.append(todo_base)
    upcoming_todos = upcoming_todos[:30]
    submitted_todos = submitted_todos[:30]

    # ============== 近3天审核通过记录 ==============
    recent_approved = []
    if visible_ids:
        since = today - timedelta(days=2)
        approved_logs = ClawTodoLog.query.filter(
            ClawTodoLog.openclaw_id.in_(visible_ids),
            ClawTodoLog.log_date >= since,
            ClawTodoLog.status.in_(['approved', 'completed'])
        ).order_by(ClawTodoLog.completed_at.desc()).limit(30).all()

        # 批量预加载 todo（消除 N+1）
        approved_todo_ids = list({log.todo_id for log in approved_logs})
        approved_todos_map = {}
        if approved_todo_ids:
            approved_todos = ClawTodo.query.filter(ClawTodo.id.in_(approved_todo_ids)).all()
            approved_todos_map = {t.id: t for t in approved_todos}

        for log in approved_logs:
            todo = approved_todos_map.get(log.todo_id)
            recent_approved.append({
                'id': todo.id if todo else log.todo_id,
                'title': todo.title if todo else '(已删除)',
                'openclaw_name': claw_name_map.get(log.openclaw_id, '未知'),
                'openclaw_id': log.openclaw_id,
                'log_date': str(log.log_date),
                'status': log.status,
                'completed_at': str(log.completed_at) if log.completed_at else None,
                'result_summary': log.result_summary,
            })

    return jsonify({
        'online_count': online_count,
        'total_claws': total_claws,
        'today_tasks': today_tasks,
        'today_reported': today_reported,
        'total_knowledge': total_knowledge,
        'pending_review': pending_count,
        'today_knowledge': today_knowledge,
        'total_skills': total_skills,
        'evolved_skills': evolved_skills,
        'tapd_projects': tapd_projects,
        'total_agents': total_agents,
        'online_agents': online_agents,
        'unread_messages': unread_messages,
        'knowledge_trend': [
            {'date': str(t.date), 'count': t.count} for t in trend_data
        ],
        'category_stats': [
            {'category': c.category, 'count': c.count}
            for c in category_stats
        ],
        'claws': claw_summaries,
        'recent_tasks': recent_tasks,
        'upcoming_todos': upcoming_todos,
        'submitted_todos': submitted_todos,
        'recent_approved': recent_approved,
    })
