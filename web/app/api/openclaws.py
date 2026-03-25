from datetime import datetime, date, time
from functools import wraps
from flask import request, jsonify
from app import db
from app.models import (OpenClawInstance, DailyReport, Project,
                        generate_api_token, hash_token)
from app.api import api_bp


def require_claw_token(f):
    """OpenClaw API Token 认证装饰器
    
    用于 OpenClaw 自身调用的接口（heartbeat、report、config）。
    验证 Authorization: Bearer <token> 是否匹配该 OpenClaw 的 token。
    """
    @wraps(f)
    def decorated(claw_id, *args, **kwargs):
        claw = OpenClawInstance.query.get_or_404(claw_id)

        auth_header = request.headers.get('Authorization', '')
        if not auth_header.startswith('Bearer '):
            return jsonify({'error': '缺少认证 Token'}), 401

        token = auth_header[7:]  # 去掉 "Bearer "
        if not claw.verify_token(token):
            return jsonify({'error': 'Token 无效或不匹配'}), 403

        return f(claw_id, claw=claw, *args, **kwargs)
    return decorated


@api_bp.route('/openclaws', methods=['GET'])
def list_openclaws():
    """获取所有 OpenClaw 列表"""
    claws = OpenClawInstance.query.order_by(
        OpenClawInstance.created_at.desc()
    ).all()
    return jsonify([c.to_dict(brief=True) for c in claws])


@api_bp.route('/openclaws', methods=['POST'])
def create_openclaw():
    """注册新 OpenClaw，返回一次性 API Token"""
    data = request.get_json()
    if not data or not data.get('name') or not data.get('owner'):
        return jsonify({'error': '名称和所属用户为必填项'}), 400

    # 自动生成 claw_tag
    claw_tag = data.get('claw_tag', f"claw-{data['name']}")

    # 检查唯一性
    if OpenClawInstance.query.filter_by(claw_tag=claw_tag).first():
        return jsonify({'error': f'标签 {claw_tag} 已存在'}), 409

    # 生成 API Token
    raw_token = generate_api_token()
    token_hash = hash_token(raw_token)

    # 处理 project_id
    project_id = data.get('project_id')
    if project_id:
        project_id = int(project_id)

    claw = OpenClawInstance(
        name=data['name'],
        claw_tag=claw_tag,
        owner=data['owner'],
        role=data.get('role', 'test_member'),
        role_title=data.get('role_title'),
        responsibilities=data.get('responsibilities'),
        project_id=project_id,
        project_name=data.get('project_name'),
        module_name=data.get('module_name'),
        avatar=data.get('avatar'),
        soul_config=data.get('soul_config'),
        workflow_config=data.get('workflow_config'),
        report_schedule=data.get('report_schedule', '15:00,21:00'),
        web_system_url=data.get('web_system_url'),
        api_token_hash=token_hash,
    )
    db.session.add(claw)
    db.session.commit()

    # 返回包含明文 Token（仅此一次）
    result = claw.to_dict()
    result['api_token'] = raw_token
    return jsonify(result), 201


@api_bp.route('/openclaws/<int:claw_id>', methods=['GET'])
def get_openclaw(claw_id):
    """获取 OpenClaw 详情"""
    claw = OpenClawInstance.query.get_or_404(claw_id)
    return jsonify(claw.to_dict())


@api_bp.route('/openclaws/<int:claw_id>', methods=['PUT'])
def update_openclaw(claw_id):
    """更新 OpenClaw 信息"""
    claw = OpenClawInstance.query.get_or_404(claw_id)
    data = request.get_json()

    updatable_fields = [
        'name', 'role', 'role_title', 'responsibilities', 'project_name',
        'module_name', 'avatar', 'soul_config', 'workflow_config',
        'report_schedule', 'web_system_url'
    ]
    for field in updatable_fields:
        if field in data:
            setattr(claw, field, data[field])

    # 处理 project_id
    if 'project_id' in data:
        claw.project_id = int(data['project_id']) if data['project_id'] else None

    db.session.commit()
    return jsonify(claw.to_dict())


@api_bp.route('/openclaws/<int:claw_id>/regenerate-token', methods=['POST'])
def regenerate_token(claw_id):
    """重新生成 API Token（旧 Token 立即失效）"""
    claw = OpenClawInstance.query.get_or_404(claw_id)

    raw_token = generate_api_token()
    claw.api_token_hash = hash_token(raw_token)
    db.session.commit()

    return jsonify({
        'id': claw.id,
        'name': claw.name,
        'api_token': raw_token,
        'message': '新 Token 已生成，旧 Token 已失效。请立即复制保存。',
    })


@api_bp.route('/openclaws/<int:claw_id>', methods=['DELETE'])
def delete_openclaw(claw_id):
    """删除 OpenClaw"""
    claw = OpenClawInstance.query.get_or_404(claw_id)
    db.session.delete(claw)
    db.session.commit()
    return jsonify({'message': f'OpenClaw "{claw.name}" 已删除'})


@api_bp.route('/openclaws/<int:claw_id>/config', methods=['GET'])
@require_claw_token
def get_openclaw_config(claw_id, claw=None):
    """OpenClaw 拉取自己的配置（需 Token 认证）"""
    return jsonify({
        'id': claw.id,
        'name': claw.name,
        'claw_tag': claw.claw_tag,
        'project_name': claw.project.name if claw.project else claw.project_name,
        'module_name': claw.module_name,
        'report_schedule': claw.report_schedule,
        'web_system_url': claw.web_system_url,
        'skills': [s.skill.to_dict() for s in claw.skills if s.enabled],
    })


@api_bp.route('/openclaws/<int:claw_id>/heartbeat', methods=['POST'])
@require_claw_token
def heartbeat(claw_id, claw=None):
    """心跳上报（需 Token 认证）"""
    claw.status = 'online'
    claw.last_heartbeat = datetime.utcnow()
    db.session.commit()
    return jsonify({'status': 'ok'})


@api_bp.route('/openclaws/<int:claw_id>/report', methods=['POST'])
@require_claw_token
def submit_report(claw_id, claw=None):
    """工作日报上报（需 Token 认证）"""
    data = request.get_json()

    if not data:
        return jsonify({'error': '上报数据为空'}), 400

    # 解析日期和时间
    report_date = date.fromisoformat(data.get('report_date',
                                              date.today().isoformat()))
    report_time_str = data.get('report_time',
                               datetime.now().strftime('%H:%M'))
    hour, minute = map(int, report_time_str.split(':'))
    report_time = time(hour, minute)

    # 查找或创建日报（同一时间段去重）
    report = DailyReport.query.filter_by(
        openclaw_id=claw_id,
        report_date=report_date,
        report_time=report_time,
    ).first()

    if report:
        # 更新已有日报
        report.tasks_completed = data.get('tasks_completed')
        report.knowledge_recorded = data.get('knowledge_recorded')
        report.experience_shared = data.get('experience_shared')
        report.knowledge_learned = data.get('knowledge_learned')
        report.ai_summary = data.get('ai_summary')
    else:
        report = DailyReport(
            openclaw_id=claw_id,
            report_date=report_date,
            report_time=report_time,
            tasks_completed=data.get('tasks_completed'),
            knowledge_recorded=data.get('knowledge_recorded'),
            experience_shared=data.get('experience_shared'),
            knowledge_learned=data.get('knowledge_learned'),
            ai_summary=data.get('ai_summary'),
        )
        db.session.add(report)

    db.session.commit()
    return jsonify(report.to_dict()), 201


@api_bp.route('/openclaws/<int:claw_id>/reports', methods=['GET'])
def list_reports(claw_id):
    """获取日报历史"""
    claw = OpenClawInstance.query.get_or_404(claw_id)

    # 支持日期筛选
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')

    query = DailyReport.query.filter_by(openclaw_id=claw_id)
    if start_date:
        query = query.filter(DailyReport.report_date >= start_date)
    if end_date:
        query = query.filter(DailyReport.report_date <= end_date)

    reports = query.order_by(
        DailyReport.report_date.desc(),
        DailyReport.report_time.desc()
    ).limit(50).all()

    return jsonify([r.to_dict() for r in reports])


@api_bp.route('/openclaws/<int:claw_id>/report-schedule', methods=['PUT'])
def update_report_schedule(claw_id):
    """修改上报频率配置"""
    claw = OpenClawInstance.query.get_or_404(claw_id)
    data = request.get_json()

    if 'report_schedule' not in data:
        return jsonify({'error': 'report_schedule 为必填项'}), 400

    claw.report_schedule = data['report_schedule']
    db.session.commit()
    return jsonify({
        'id': claw.id,
        'name': claw.name,
        'report_schedule': claw.report_schedule,
    })
