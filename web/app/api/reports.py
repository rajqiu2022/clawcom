"""
日报管理中心 API
提供全局日报汇总视图、时间线、统计
"""
from datetime import date, datetime, timedelta
from flask import request, jsonify
from sqlalchemy import func, desc
from app import db
from app.models import DailyReport, OpenClawInstance
from app.api import api_bp


@api_bp.route('/reports', methods=['GET'])
def list_all_reports():
    """全局日报列表（支持多维筛选）"""
    query = DailyReport.query

    # 筛选: openclaw_id
    claw_id = request.args.get('openclaw_id', type=int)
    if claw_id:
        query = query.filter(DailyReport.openclaw_id == claw_id)

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
        result.append(d)

    return jsonify(result)


@api_bp.route('/reports/stats', methods=['GET'])
def report_stats():
    """日报统计概览"""
    today = date.today()
    yesterday = today - timedelta(days=1)
    week_ago = today - timedelta(days=7)

    # 今日日报数
    today_count = DailyReport.query.filter_by(report_date=today).count()

    # 今日已汇报 OpenClaw 数
    today_reported_claws = db.session.query(
        func.count(func.distinct(DailyReport.openclaw_id))
    ).filter(DailyReport.report_date == today).scalar() or 0

    # 全部 OpenClaw 数
    total_claws = OpenClawInstance.query.count()

    # 今日任务总数（兼容字符串和列表类型）
    today_reports = DailyReport.query.filter_by(report_date=today).all()
    def count_items(field_value):
        if isinstance(field_value, list):
            return len(field_value)
        elif isinstance(field_value, str) and field_value.strip():
            # 字符串按换行分割计算行数
            return len([l for l in field_value.split('\n') if l.strip()])
        return 0
    today_tasks = sum(count_items(r.tasks_completed) for r in today_reports)

    # 今日知识总数
    today_knowledge = sum(count_items(r.knowledge_recorded) for r in today_reports)

    # 近 7 天趋势
    trend_data = (
        db.session.query(
            DailyReport.report_date,
            func.count().label('report_count'),
        )
        .filter(DailyReport.report_date >= week_ago)
        .group_by(DailyReport.report_date)
        .order_by(DailyReport.report_date)
        .all()
    )

    # 各 OpenClaw 今日汇报情况
    claws = OpenClawInstance.query.order_by(OpenClawInstance.name).all()
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
                'count': t.report_count,
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

    reports = DailyReport.query.filter_by(
        report_date=target_date
    ).order_by(DailyReport.report_time.desc()).all()

    timeline = []
    for r in reports:
        claw = OpenClawInstance.query.get(r.openclaw_id)
        timeline.append({
            'id': r.id,
            'openclaw_name': claw.name if claw else '未知',
            'openclaw_avatar': claw.avatar if claw else None,
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
