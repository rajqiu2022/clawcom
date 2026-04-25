from datetime import date, datetime, timedelta
from flask import jsonify, session as flask_session
from sqlalchemy import func
from app import db
from app.models import (OpenClawInstance, DailyReport, KnowledgeEntry,
                        Skill, Rule, OpenClawSkill, Project, Agent, Message, User,
                        ClawTodo, ClawTodoLog)
from app.api import api_bp
from app.api.agent_client import _cleanup_stale_connections


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
    """Dashboard 统计数据"""
    # 清理超时的 SSE 连接
    _cleanup_stale_connections()

    today = date.today()

    visible_claws = _get_visible_claws()
    visible_ids = {c.id for c in visible_claws}

    # 在线 OpenClaw 数量（工作/学习/摸鱼 都算在线，休息和 offline 算离线）
    online_count = sum(1 for c in visible_claws if c.status in ('工作', '学习', '摸鱼', 'online'))
    total_claws = len(visible_claws)

    # 今日任务统计（仅可见 claw）
    if visible_ids:
        today_reports = DailyReport.query.filter(
            DailyReport.report_date == today,
            DailyReport.openclaw_id.in_(visible_ids)
        ).all()
    else:
        today_reports = []
    def _count_tasks(tasks_json):
        """安全计算完成任务数量，兼容数组/字符串/None"""
        if not tasks_json:
            return 0
        if isinstance(tasks_json, list):
            return len(tasks_json)
        if isinstance(tasks_json, str):
            return 1 if tasks_json.strip() else 0
        return 1
    today_tasks = sum(
        _count_tasks(r.tasks_completed) for r in today_reports
    )

    # 今日日报汇报率（仅可见 claw）
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

    # 待审核数（知识库 + Skill + Rule）
    pending_count = KnowledgeEntry.query.filter_by(
        status='pending_review'
    ).count()
    pending_count += Skill.query.filter_by(
        review_status='pending', is_deleted=False
    ).count()
    pending_count += Rule.query.filter_by(
        review_status='pending', is_deleted=False
    ).count()

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

    # 通信中心统计（在线状态以 SSE 为准，super_admin/admin 可见 admin 角色）
    try:
        _uid = flask_session.get('user_id')
        _user = User.query.get(_uid) if _uid else None
        _urole = _user.role if _user else 'guest'
        agent_query = Agent.query
        if _urole != 'super_admin':
            agent_query = agent_query.filter(Agent.role != 'admin')
        agents = agent_query.all()
        online_statuses = {}
        for claw in OpenClawInstance.query.filter(OpenClawInstance.status != 'deleted').all():
            online_statuses[claw.name] = claw.status in ('工作', '学习', '摸鱼', 'online')
        online_agents = sum(1 for a in agents
                           if online_statuses.get(a.name, a.status == 'online'))
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

    # OpenClaw 概览（仅可见 claw）
    # 先计算每个 claw 的待办数（过滤闹钟类 interrupt）
    claw_todo_counts = {}
    if visible_ids:
        all_enabled_todos = ClawTodo.query.filter(
            ClawTodo.openclaw_id.in_(visible_ids),
            ClawTodo.enabled == True
        ).all()
        for todo in all_enabled_todos:
            st = (todo.schedule_time or '').strip()
            if todo.urgency_level == 'interrupt' and st in ('15:00', '15:30', '21:00'):
                continue
            cid = todo.openclaw_id
            today_log = ClawTodoLog.query.filter_by(
                todo_id=todo.id, log_date=today
            ).first()
            if not today_log or today_log.status == 'pending':
                claw_todo_counts[cid] = claw_todo_counts.get(cid, 0) + 1

    claw_summaries = []
    for c in visible_claws:
        today_report = DailyReport.query.filter_by(
            openclaw_id=c.id, report_date=today
        ).order_by(DailyReport.report_time.desc()).first()

        claw_summaries.append({
            'id': c.id,
            'name': c.name,
            'role_title': c.role_title,
            'status': c.status,
            'last_activity': (str(c.last_activity)
                              if c.last_activity else None),
            'today_tasks': claw_todo_counts.get(c.id, 0),
            'today_completed': (_count_tasks(today_report.tasks_completed)
                                if today_report else 0),
            'today_summary': (today_report.ai_summary
                            if today_report else None),
            'avatar': c.avatar,
            'reported_today': today_report is not None,
            'project_name': c.project.name if c.project else (c.project_name or ''),
        })

    # ============== 最近完成的任务（近3天日报） ==============
    three_days_ago = today - timedelta(days=3)
    recent_reports = DailyReport.query.filter(
        DailyReport.report_date >= three_days_ago,
        DailyReport.openclaw_id.in_((visible_ids) if visible_ids else [0])
    ).order_by(DailyReport.report_date.desc(), DailyReport.report_time.desc()).all()

    recent_tasks = []
    for r in recent_reports[:50]:  # 最多取 50 条
        claw_name_map = {c.id: c.name for c in visible_claws}
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
    upcoming_todos = []       # 今日待完成
    submitted_todos = []      # 今日已提交（待审核）
    if visible_ids:
        todos_q = ClawTodo.query.filter(
            ClawTodo.openclaw_id.in_(visible_ids)
        ).order_by(ClawTodo.created_at.desc()).all()
        claw_name_map = {c.id: c.name for c in visible_claws}
        for todo in todos_q:
            # 过滤 15:00 闹钟类待办（定时中断的提醒任务不显示）
            st = (todo.schedule_time or '').strip()
            if todo.urgency_level == 'interrupt' and st in ('15:00', '15:30', '21:00'):
                continue
            today_log = ClawTodoLog.query.filter_by(
                todo_id=todo.id, log_date=today
            ).first()
            if not today_log:
                latest_log = (ClawTodoLog.query
                              .filter_by(todo_id=todo.id)
                              .order_by(ClawTodoLog.log_date.desc(), ClawTodoLog.created_at.desc())
                              .first())
                if latest_log and latest_log.status == 'submitted':
                    today_log = latest_log
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
                'today_status': today_log.status if today_log else 'pending',
                'today_completed_at': str(today_log.completed_at) if today_log and today_log.completed_at else None,
                'result_summary': today_log.result_summary if today_log else None,
                'created_at': str(todo.created_at) if todo.created_at else None,
            }
            if (not today_log or today_log.status == 'pending') and todo.enabled:
                upcoming_todos.append(todo_base)
            elif today_log and today_log.status == 'submitted':
                submitted_todos.append(todo_base)
            # approved/completed 以及 enabled=False 且无 today_log 的情况不在这两个列表中
    upcoming_todos = upcoming_todos[:30]
    submitted_todos = submitted_todos[:30]

    # ============== 近3天审核通过记录 ==============
    recent_approved = []
    if visible_ids:
        since = today - timedelta(days=2)  # 含今天共3天
        approved_logs = ClawTodoLog.query.filter(
            ClawTodoLog.openclaw_id.in_(visible_ids),
            ClawTodoLog.log_date >= since,
            ClawTodoLog.status.in_(['approved', 'completed'])
        ).order_by(ClawTodoLog.completed_at.desc()).limit(30).all()
        for log in approved_logs:
            todo = ClawTodo.query.get(log.todo_id)
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
