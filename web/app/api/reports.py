"""
日报管理中心 API
提供全局日报汇总视图、时间线、统计
"""
from datetime import date, datetime, timedelta
from flask import request, jsonify, session as flask_session
from sqlalchemy import func, desc, or_
from app import db
from app.models import DailyReport, OpenClawInstance, Project, User
from app.api import api_bp


def _project_filter(project_name):
    """构造项目过滤条件，同时匹配 project_name 字段和 project 关联表"""
    return or_(
        OpenClawInstance.project_name == project_name,
        OpenClawInstance.project_id.in_(
            db.session.query(Project.id).filter(Project.name == project_name)
        )
    )


def _collect_user_project_ids(user):
    ids = set()
    if not user:
        return ids
    for pid in (user.managed_projects or []):
        try:
            ids.add(int(pid))
        except Exception:
            continue
    if user.bound_claw_id:
        claw = OpenClawInstance.query.get(user.bound_claw_id)
        if claw:
            if claw.project_id:
                ids.add(int(claw.project_id))
            elif claw.project_name:
                p = Project.query.filter_by(name=claw.project_name).first()
                if p:
                    ids.add(int(p.id))
    return ids


def _visible_claw_ids():
    uid = flask_session.get('user_id')
    user = User.query.get(uid) if uid else None
    query = OpenClawInstance.query.filter(OpenClawInstance.status != 'deleted')
    if not user:
        return []
    if user.role != 'super_admin':
        query = query.filter(OpenClawInstance.role != 'admin')
        project_ids = list(_collect_user_project_ids(user))
        if project_ids:
            project_names = [p.name for p in Project.query.filter(Project.id.in_(project_ids)).all()]
            query = query.filter(
                or_(
                    OpenClawInstance.project_id.in_(project_ids),
                    OpenClawInstance.project_name.in_(project_names) if project_names else db.text('1=0')
                )
            )
        elif user.bound_claw_id:
            query = query.filter(OpenClawInstance.id == int(user.bound_claw_id))
        else:
            query = query.filter(OpenClawInstance.id == -1)
    return [c.id for c in query.all()]


@api_bp.route('/reports', methods=['GET'])
def list_all_reports():
    """全局日报列表（支持多维筛选）"""
    visible_ids = _visible_claw_ids()
    query = DailyReport.query.filter(DailyReport.openclaw_id.in_(visible_ids or [0]))

    # 筛选: openclaw_id
    claw_id = request.args.get('openclaw_id', type=int)
    if claw_id:
        query = query.filter(DailyReport.openclaw_id == claw_id)

    # 筛选: 项目
    project = request.args.get('project')
    if project:
        query = query.join(OpenClawInstance).filter(
            _project_filter(project)
        )

    # 筛选: 日期范围
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    if start_date:
        query = query.filter(DailyReport.report_date >= start_date)
    if end_date:
        query = query.filter(DailyReport.report_date <= end_date)

    # 默认查最近 7 天
    if not start_date and not end_date:
        week_ago = date.today() - timedelta(days=7)
        query = query.filter(DailyReport.report_date >= week_ago)

    reports = query.order_by(
        DailyReport.report_date.desc(),
        DailyReport.report_time.desc()
    ).limit(200).all()

    result = []
    for r in reports:
        d = r.to_dict()
        # 附上 OpenClaw 名称和头像
        claw = OpenClawInstance.query.get(r.openclaw_id)
        d['openclaw_name'] = claw.name if claw else '未知'
        d['openclaw_avatar'] = claw.avatar if claw else None
        d['openclaw_role_title'] = claw.role_title if claw else None
        d['openclaw_project_name'] = claw.project.name if (claw and claw.project) else (claw.project_name or '') if claw else ''
        result.append(d)

    return jsonify(result)


@api_bp.route('/reports/stats', methods=['GET'])
def report_stats():
    """日报统计概览"""
    today = date.today()
    yesterday = today - timedelta(days=1)
    week_ago = today - timedelta(days=7)

    # 项目过滤
    project = request.args.get('project')

    visible_ids = _visible_claw_ids()
    # 基础查询
    base_query = DailyReport.query.filter(DailyReport.openclaw_id.in_(visible_ids or [0]))
    if project:
        base_query = base_query.join(OpenClawInstance).filter(
            _project_filter(project)
        )

    # 今日日报数
    today_reports_q = base_query.filter(DailyReport.report_date == today)
    today_count = today_reports_q.count()

    # 今日已汇报 OpenClaw 数
    today_reported_claws = db.session.query(
        func.count(func.distinct(DailyReport.openclaw_id))
    ).filter(DailyReport.report_date == today)
    if project:
        today_reported_claws = today_reported_claws.join(OpenClawInstance).filter(
            _project_filter(project)
        )
    today_reported_claws = today_reported_claws.scalar() or 0

    # 全部 OpenClaw 数（排除已删除）
    claws_query = OpenClawInstance.query.filter(
        OpenClawInstance.status != 'deleted',
        OpenClawInstance.id.in_(visible_ids or [0])
    )
    if project:
        claws_query = claws_query.filter(_project_filter(project))
    total_claws = claws_query.count()

    # 今日任务总数（兼容字符串和列表类型）
    today_reports = today_reports_q.all()
    def count_items(field_value):
        if isinstance(field_value, list):
            return len(field_value)
        elif isinstance(field_value, str) and field_value.strip():
            # 字符串可能是双重转义的换行符 \\n，先替换为真实换行符再分割
            normalized = field_value.replace('\\n', '\n').replace('\\\\n', '\n')
            return len([l for l in normalized.split('\n') if l.strip()])
        return 0
    today_tasks = sum(count_items(r.tasks_completed) for r in today_reports)

    # 今日知识总数
    today_knowledge = sum(count_items(r.knowledge_recorded) for r in today_reports)

    # 近 7 天趋势
    trend_query = db.session.query(
        DailyReport.report_date,
        func.count().label('count'),
    ).filter(DailyReport.report_date >= week_ago)
    if project:
        trend_query = trend_query.join(OpenClawInstance).filter(
            _project_filter(project)
        )
    trend_data = (
        trend_query
        .group_by(DailyReport.report_date)
        .order_by(DailyReport.report_date)
        .all()
    )

    # 各 OpenClaw 今日汇报情况
    claws = claws_query.order_by(OpenClawInstance.name).all()
    claw_status = []
    for c in claws:
        latest = DailyReport.query.filter_by(
            openclaw_id=c.id, report_date=today
        ).order_by(DailyReport.report_time.desc()).first()

        claw_status.append({
            'id': c.id,
            'name': c.name,
            'avatar': c.avatar,
            'role_title': c.role_title,
            'project_name': c.project.name if c.project else (c.project_name or ''),
            'reported': latest is not None,
            'report_time': latest.report_time.strftime('%H:%M') if latest else None,
            'task_count': count_items(latest.tasks_completed) if latest else 0,
            'summary': latest.ai_summary if latest else None,
        })

    return jsonify({
        'today_count': today_count,
        'today_reported_claws': today_reported_claws,
        'total_claws': total_claws,
        'today_tasks': today_tasks,
        'today_knowledge': today_knowledge,
        'trend': [
            {
                'date': str(t.report_date),
                'count': t.count,
            }
            for t in trend_data
        ],
        'claw_status': claw_status,
    })


@api_bp.route('/reports/timeline', methods=['GET'])
def report_timeline():
    """时间线视图 - 按日期分组，最新在前"""
    target_date = request.args.get('date', date.today().isoformat())
    target_date = date.fromisoformat(target_date)
    project = request.args.get('project')

    visible_ids = _visible_claw_ids()
    query = DailyReport.query.filter_by(report_date=target_date).filter(
        DailyReport.openclaw_id.in_(visible_ids or [0])
    )
    if project:
        query = query.join(OpenClawInstance).filter(
            _project_filter(project)
        )

    reports = query.order_by(DailyReport.report_time.desc()).all()

    timeline = []
    for r in reports:
        claw = OpenClawInstance.query.get(r.openclaw_id)
        timeline.append({
            'id': r.id,
            'openclaw_name': claw.name if claw else '未知',
            'openclaw_avatar': claw.avatar if claw else None,
            'openclaw_project_name': claw.project.name if (claw and claw.project) else (claw.project_name or '') if claw else '',
            'report_time': r.report_time.strftime('%H:%M') if r.report_time else None,
            'tasks_completed': r.tasks_completed or [],
            'knowledge_recorded': r.knowledge_recorded or [],
            'experience_shared': r.experience_shared or [],
            'ai_summary': r.ai_summary,
        })

    return jsonify({
        'date': target_date.isoformat(),
        'timeline': timeline,
    })
