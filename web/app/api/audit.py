"""
审计日志 API
提供操作日志查询、CSV 导出
"""
import csv
import io
from datetime import date, timedelta
from flask import request, jsonify, Response
from sqlalchemy import desc
from app import db
from app.models import AuditLog
from app.api import api_bp


def log_action(action, resource_type, resource_id=None,
               resource_name=None, operator='system', detail=None):
    """记录审计日志（供其他模块调用）"""
    try:
        ip = request.remote_addr if request else None
    except RuntimeError:
        ip = None

    log = AuditLog(
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        resource_name=resource_name,
        operator=operator,
        ip_address=ip,
        detail=detail,
    )
    db.session.add(log)
    db.session.commit()
    return log


@api_bp.route('/audit-logs', methods=['GET'])
def list_audit_logs():
    """查询审计日志（支持多维筛选）"""
    query = AuditLog.query

    action = request.args.get('action')
    resource_type = request.args.get('resource_type')
    operator = request.args.get('operator')
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    search = request.args.get('search')

    if action:
        query = query.filter(AuditLog.action == action)
    if resource_type:
        query = query.filter(AuditLog.resource_type == resource_type)
    if operator:
        query = query.filter(AuditLog.operator.like(f'%{operator}%'))
    if start_date:
        query = query.filter(AuditLog.created_at >= start_date)
    if end_date:
        query = query.filter(AuditLog.created_at <= end_date + ' 23:59:59')
    if search:
        search_term = f'%{search}%'
        query = query.filter(
            db.or_(
                AuditLog.resource_name.like(search_term),
                AuditLog.detail.like(search_term),
                AuditLog.operator.like(search_term),
            )
        )

    # 默认最近 30 天
    if not start_date and not end_date:
        thirty_days_ago = date.today() - timedelta(days=30)
        query = query.filter(AuditLog.created_at >= thirty_days_ago)

    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)
    per_page = min(per_page, 200)

    total = query.count()
    logs = query.order_by(desc(AuditLog.created_at)).offset(
        (page - 1) * per_page
    ).limit(per_page).all()

    return jsonify({
        'logs': [l.to_dict() for l in logs],
        'total': total,
        'page': page,
        'per_page': per_page,
        'pages': (total + per_page - 1) // per_page,
    })


@api_bp.route('/audit-logs/export', methods=['GET'])
def export_audit_logs():
    """导出审计日志为 CSV"""
    query = AuditLog.query

    action = request.args.get('action')
    resource_type = request.args.get('resource_type')
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')

    if action:
        query = query.filter(AuditLog.action == action)
    if resource_type:
        query = query.filter(AuditLog.resource_type == resource_type)
    if start_date:
        query = query.filter(AuditLog.created_at >= start_date)
    if end_date:
        query = query.filter(AuditLog.created_at <= end_date + ' 23:59:59')

    if not start_date and not end_date:
        thirty_days_ago = date.today() - timedelta(days=30)
        query = query.filter(AuditLog.created_at >= thirty_days_ago)

    logs = query.order_by(desc(AuditLog.created_at)).limit(5000).all()

    output = io.StringIO()
    output.write('\ufeff')  # BOM for Excel
    writer = csv.writer(output)
    writer.writerow(['时间', '操作', '资源类型', '资源ID', '资源名称',
                     '操作者', 'IP地址', '详情'])

    for l in logs:
        writer.writerow([
            str(l.created_at) if l.created_at else '',
            l.action, l.resource_type, l.resource_id or '',
            l.resource_name or '', l.operator or '',
            l.ip_address or '', l.detail or '',
        ])

    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={
            'Content-Disposition': f'attachment; filename=audit_logs_{date.today()}.csv'
        }
    )
