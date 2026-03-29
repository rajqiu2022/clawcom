import uuid
import logging
from datetime import datetime, date, time
from functools import wraps
from flask import request, jsonify
from app import db
from app.models import (OpenClawInstance, DailyReport, Project, Rule,
                        OpenClawRule, OpenClawSkill, Skill,
                        generate_api_token, hash_token, _simple_encrypt)
from app.api import api_bp

logger = logging.getLogger(__name__)


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
    """注册新 OpenClaw，返回 API Token"""
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
    from app.models import _simple_encrypt
    token_encrypted = _simple_encrypt(raw_token)

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
        api_token_plain=token_encrypted,
    )
    db.session.add(claw)
    db.session.commit()

    # 自动安装标准化的 Skills
    standard_skills = Skill.query.filter_by(is_standard=True).all()
    for skill in standard_skills:
        if skill.name != 'registration-skill':  # 注册 Skill 不自动安装
            assoc = OpenClawSkill(
                openclaw_id=claw.id,
                skill_id=skill.id,
                enabled=True
            )
            db.session.add(assoc)

    # 自动安装标准化的 Rules
    standard_rules = Rule.query.filter_by(is_standard=True).all()
    for rule in standard_rules:
        assoc = OpenClawRule(
            openclaw_id=claw.id,
            rule_id=rule.id,
            enabled=True
        )
        db.session.add(assoc)

    db.session.commit()

    # 返回 Token 预览（不返回完整明文）
    result = claw.to_dict()
    result['api_token_preview'] = claw.get_token_preview()
    result['has_token'] = True
    result['auto_installed'] = {
        'skills': [s.name for s in standard_skills if s.name != 'registration-skill'],
        'rules': [r.name for r in standard_rules],
    }
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
    claw.api_token_plain = _simple_encrypt(raw_token)
    db.session.commit()

    return jsonify({
        'id': claw.id,
        'name': claw.name,
        'api_token': raw_token,
        'api_token_preview': claw.get_token_preview(),
        'has_token': True,
        'message': '新 Token 已生成，旧 Token 已失效。',
    })


@api_bp.route('/openclaws/<int:claw_id>/token', methods=['GET'])
def get_claw_token(claw_id):
    """
    获取 OpenClaw 的完整 Token（用于弹窗显示和复制）
    注意：需要管理员权限或验证操作者身份
    """
    claw = OpenClawInstance.query.get_or_404(claw_id)

    return jsonify({
        'id': claw.id,
        'name': claw.name,
        'api_token': claw.get_token_plain(),
        'api_token_preview': claw.get_token_preview(),
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
    # 获取已安装的 Skills
    installed_skills = [s.skill.to_dict() for s in claw.skills if s.enabled]

    # 获取已安装的 Rules
    installed_rules = []
    for r in OpenClawRule.query.filter_by(openclaw_id=claw_id, enabled=True).all():
        installed_rules.append({
            'id': r.rule.id,
            'name': r.rule.name,
            'display_name': r.rule.display_name,
            'description': r.rule.description,
            'content_template': r.rule.content_template,
        })

    return jsonify({
        'id': claw.id,
        'name': claw.name,
        'claw_tag': claw.claw_tag,
        'project_name': claw.project.name if claw.project else claw.project_name,
        'module_name': claw.module_name,
        'report_schedule': claw.report_schedule,
        'web_system_url': claw.web_system_url,
        'skills': installed_skills,
        'rules': installed_rules,
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


@api_bp.route('/openclaws/<int:claw_id>/assigned-skills', methods=['GET'])
@require_claw_token
def get_assigned_skills(claw_id, claw=None):
    """OpenClaw 获取分配给自己的 Skills（需 Token 认证）"""
    skills = []
    for s in claw.skills:
        if s.enabled:
            skills.append({
                'id': s.skill.id,
                'name': s.skill.name,
                'display_name': s.skill.display_name,
                'description': s.skill.description,
                'template_content': s.skill.template_content,
                'trigger_phrase': s.skill.trigger_phrase,
            })
    return jsonify({'skills': skills})


@api_bp.route('/openclaws/<int:claw_id>/assigned-rules', methods=['GET'])
@require_claw_token
def get_assigned_rules(claw_id, claw=None):
    """OpenClaw 获取分配给自己的 Rules（需 Token 认证）"""
    rules = []
    for r in OpenClawRule.query.filter_by(openclaw_id=claw_id, enabled=True).all():
        rules.append({
            'id': r.rule.id,
            'name': r.rule.name,
            'display_name': r.rule.display_name,
            'description': r.rule.description,
            'content_template': r.rule.content_template,
        })
    return jsonify({'rules': rules})


@api_bp.route('/openclaws/<int:claw_id>/dispatch', methods=['POST'])
@require_claw_token
def dispatch_task_to_claw(claw_id, claw=None):
    """
    向 OpenClaw 下发任务（通过 WebSocket 推送）

    请求体：
    {
        "task_type": "sync_skills" | "sync_rules" | "get_status" | ...",
        "payload": {...}  // 任务参数
    }
    """
    data = request.get_json()
    if not data or not data.get('task_type'):
        return jsonify({'error': 'task_type 为必填项'}), 400

    task_id = f"task_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}"
    task_type = data.get('task_type')
    payload = data.get('payload', {})

    # 构造任务数据
    task_data = {
        'task_id': task_id,
        'task_type': task_type,
        'payload': payload,
    }

    # 通过 WebSocket 推送给 OpenClaw
    from app.api.gateway_ws import push_event, conn_manager

    # 找到该 claw 对应的 session
    session_id = None
    for sid, info in conn_manager.list_connections().items():
        if info.get('claw_id') == claw_id:
            session_id = sid
            break

    if session_id:
        push_event(session_id, 'task', task_data)
        logger.info(f"任务已推送给 OpenClaw {claw_id}: {task_type}")
        return jsonify({
            'message': '任务已推送',
            'task_id': task_id,
            'task_type': task_type,
            'status': 'pushed',
        })
    else:
        # OpenClaw 不在线，存入待处理队列（简化处理，后续可扩展）
        logger.warning(f"OpenClaw {claw_id} 不在线，任务已忽略")
        return jsonify({
            'message': 'OpenClaw 不在线，任务推送失败',
            'task_id': task_id,
            'task_type': task_type,
            'status': 'offline',
        }), 503


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
