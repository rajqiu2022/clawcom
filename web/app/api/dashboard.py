from datetime import date, datetime, timedelta
from flask import jsonify
from sqlalchemy import func
from app import db
from app.models import (OpenClawInstance, DailyReport, KnowledgeEntry,
                        Skill, OpenClawSkill, Project, Agent, Message)
from app.api import api_bp


@api_bp.route('/dashboard/stats', methods=['GET'])
def dashboard_stats():
    """Dashboard 统计数据"""
    today = date.today()

    # 在线 OpenClaw 数量（5分钟内有心跳）
    threshold = datetime.utcnow() - timedelta(minutes=5)
    online_count = OpenClawInstance.query.filter(
        OpenClawInstance.last_heartbeat >= threshold
    ).count()

    total_claws = OpenClawInstance.query.count()

    # 今日任务统计
    today_reports = DailyReport.query.filter_by(
        report_date=today
    ).all()
    today_tasks = sum(
        len(r.tasks_completed or []) for r in today_reports
    )

    # 今日日报汇报率
    today_reported = db.session.query(
        func.count(func.distinct(DailyReport.openclaw_id))
    ).filter(DailyReport.report_date == today).scalar() or 0

    # 知识库总条目
    total_knowledge = KnowledgeEntry.query.count()

    # 待审核数
    pending_count = KnowledgeEntry.query.filter_by(
        status='pending_review'
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

    # 通信中心统计
    try:
        total_agents = Agent.query.count()
        online_agents = Agent.query.filter_by(status='online').count()
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

    # OpenClaw 概览（含今日摘要）
    claws = OpenClawInstance.query.order_by(
        OpenClawInstance.created_at
    ).all()
    claw_summaries = []
    for c in claws:
        today_report = DailyReport.query.filter_by(
            openclaw_id=c.id, report_date=today
        ).order_by(DailyReport.report_time.desc()).first()

        claw_summaries.append({
            'id': c.id,
            'name': c.name,
            'role_title': c.role_title,
            'status': c.status,
            'last_heartbeat': (str(c.last_heartbeat)
                              if c.last_heartbeat else None),
            'today_tasks': (len(today_report.tasks_completed or [])
                          if today_report else 0),
            'today_summary': (today_report.ai_summary
                            if today_report else None),
            'avatar': c.avatar,
            'reported_today': today_report is not None,
            'project_name': c.project.name if c.project else (c.project_name or ''),
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
    })
