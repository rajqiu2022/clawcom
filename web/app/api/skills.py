from flask import request, jsonify
from datetime import datetime
from app import db
from app.models import Skill, OpenClawSkill, OpenClawInstance, Rule
from app.api import api_bp


@api_bp.route('/skills', methods=['GET'])
def list_skills():
    """获取 Skills 列表"""
    category_filter = request.args.get('category')
    query = Skill.query
    if category_filter:
        query = query.filter(Skill.category == category_filter)
    skills = query.order_by(Skill.category, Skill.name).all()
    result = []
    for s in skills:
        d = s.to_dict()
        # 查出正在使用的 OpenClaw 名单
        users = (db.session.query(OpenClawInstance.name)
                 .join(OpenClawSkill)
                 .filter(OpenClawSkill.skill_id == s.id,
                         OpenClawSkill.enabled == True)
                 .all())
        d['used_by'] = [u[0] for u in users]
        d['used_by_count'] = len(d['used_by'])
        result.append(d)
    return jsonify(result)


@api_bp.route('/skills', methods=['POST'])
def create_skill():
    """创建新 Skill"""
    data = request.get_json()
    if not data or not data.get('name') or not data.get('display_name'):
        return jsonify({'error': '标识名和显示名称为必填项'}), 400

    if Skill.query.filter_by(name=data['name']).first():
        return jsonify({'error': f'Skill "{data["name"]}" 已存在'}), 409

    skill = Skill(
        name=data['name'],
        display_name=data['display_name'],
        description=data.get('description'),
        category=data.get('category', 'custom'),
        trigger_phrase=data.get('trigger_phrase'),
        template_content=data.get('template_content'),
        scope=data.get('scope', 'global'),
        applicable_projects=data.get('applicable_projects'),
        applicable_modules=data.get('applicable_modules'),
        created_by=data.get('created_by', 'system'),
    )

    # 进化技能额外字段
    if data.get('category') == 'evolved':
        skill.evolve_source = data.get('evolve_source')
        skill.success_rate = data.get('success_rate', 0)
        skill.total_runs = data.get('total_runs', 0)
        skill.error_count = data.get('error_count', 0)
        skill.last_evolved_at = datetime.utcnow()
        skill.evolve_history = [{
            'action': 'created',
            'timestamp': datetime.utcnow().isoformat(),
            'detail': f'由 OpenSpace 从 {data.get("evolve_source", "未知")} 进化而来',
        }]

    db.session.add(skill)
    db.session.commit()
    return jsonify(skill.to_dict()), 201


@api_bp.route('/skills/batch-evolved', methods=['POST'])
def batch_import_evolved():
    """批量导入 OpenSpace 自动进化的技能"""
    data = request.get_json()
    skills = data.get('skills', [])
    source = data.get('source', 'OpenSpace')

    if not skills:
        return jsonify({'error': 'skills 数组不能为空'}), 400

    created = 0
    updated = 0

    for s_data in skills:
        if not s_data.get('name') or not s_data.get('display_name'):
            continue

        existing = Skill.query.filter_by(name=s_data['name']).first()
        if existing:
            # 更新已有技能
            if s_data.get('description'):
                existing.description = s_data['description']
            if s_data.get('template_content'):
                existing.template_content = s_data['template_content']
            if s_data.get('trigger_phrase'):
                existing.trigger_phrase = s_data['trigger_phrase']
            existing.success_rate = s_data.get('success_rate', existing.success_rate)
            existing.total_runs = s_data.get('total_runs', existing.total_runs)
            existing.error_count = s_data.get('error_count', existing.error_count)
            existing.last_evolved_at = datetime.utcnow()
            history = existing.evolve_history or []
            history.append({
                'action': 'evolved',
                'timestamp': datetime.utcnow().isoformat(),
                'detail': f'OpenSpace 自动进化',
            })
            existing.evolve_history = history[-20:]  # 保留最近20条
            updated += 1
        else:
            # 创建新技能
            skill = Skill(
                name=s_data['name'],
                display_name=s_data['display_name'],
                description=s_data.get('description'),
                category='evolved',
                trigger_phrase=s_data.get('trigger_phrase'),
                template_content=s_data.get('template_content'),
                scope=s_data.get('scope', 'global'),
                evolve_source=source,
                success_rate=s_data.get('success_rate', 0),
                total_runs=s_data.get('total_runs', 0),
                error_count=s_data.get('error_count', 0),
                last_evolved_at=datetime.utcnow(),
                evolve_history=[{
                    'action': 'created',
                    'timestamp': datetime.utcnow().isoformat(),
                    'detail': f'由 OpenSpace 从 {source} 进化而来',
                }],
            )
            db.session.add(skill)
            created += 1

    db.session.commit()

    return jsonify({
        'message': f'导入完成：新建 {created} 个，更新 {updated} 个',
        'created': created,
        'updated': updated,
    })


@api_bp.route('/skills/<int:skill_id>', methods=['PUT'])
def update_skill(skill_id):
    """更新 Skill"""
    skill = Skill.query.get_or_404(skill_id)
    data = request.get_json()

    for field in ['display_name', 'description', 'trigger_phrase',
                  'template_content', 'category', 'scope',
                  'applicable_projects', 'applicable_modules']:
        if field in data:
            setattr(skill, field, data[field])

    db.session.commit()
    return jsonify(skill.to_dict())


@api_bp.route('/openclaws/<int:claw_id>/skills', methods=['POST'])
def install_skill(claw_id):
    """为 OpenClaw 安装 Skill"""
    claw = OpenClawInstance.query.get_or_404(claw_id)
    data = request.get_json()
    skill_id = data.get('skill_id')

    if not skill_id:
        return jsonify({'error': 'skill_id 为必填项'}), 400

    skill = Skill.query.get_or_404(skill_id)

    # 检查是否已安装
    existing = OpenClawSkill.query.filter_by(
        openclaw_id=claw_id, skill_id=skill_id
    ).first()

    if existing:
        existing.enabled = True
    else:
        existing = OpenClawSkill(
            openclaw_id=claw_id, skill_id=skill_id, enabled=True
        )
        db.session.add(existing)

    db.session.commit()
    return jsonify({'message': f'已为 {claw.name} 安装 {skill.display_name}'}), 201


@api_bp.route('/openclaws/<int:claw_id>/skills/<int:skill_id>',
              methods=['DELETE'])
def uninstall_skill(claw_id, skill_id):
    """卸载 Skill"""
    link = OpenClawSkill.query.filter_by(
        openclaw_id=claw_id, skill_id=skill_id
    ).first_or_404()

    link.enabled = False
    db.session.commit()
    return jsonify({'message': '已卸载'})


# ==================== 标准化 Skills 管理 ====================

@api_bp.route('/skills/standard', methods=['GET'])
def list_standard_skills():
    """获取所有标准化 Skills"""
    skills = Skill.query.filter_by(is_standard=True).order_by(Skill.name).all()
    return jsonify([s.to_dict() for s in skills])


@api_bp.route('/skills/standard', methods=['PUT'])
def batch_update_standard_skills():
    """
    批量更新 Skills 的标准化标记

    请求体：
    {
        "skill_ids": [1, 2, 3],  // 要标记为标准化的 skill_ids
        "standard": true         // true=标记为标准化，false=取消标准化
    }
    """
    data = request.get_json()
    if not data or 'skill_ids' not in data:
        return jsonify({'error': 'skill_ids 为必填项'}), 400

    skill_ids = data['skill_ids']
    is_standard = data.get('standard', True)

    updated = 0
    for skill_id in skill_ids:
        skill = Skill.query.get(skill_id)
        if skill:
            skill.is_standard = is_standard
            updated += 1

    db.session.commit()

    action = "已标记为标准化" if is_standard else "已取消标准化"
    return jsonify({
        'message': f'{updated} 个 Skills {action}',
        'updated': updated,
    })


@api_bp.route('/skills/registration-skill', methods=['GET'])
def get_registration_skill():
    """
    获取注册 Skill（注册时使用的标准化 Skill）
    注册 Skill 包含：
    1. 注册到 Hub 的 API 调用
    2. 需要安装的标准化 Rules 列表
    3. 需要安装的标准化 Skills 列表
    """
    # 查找注册 Skill（名称为 registration-skill）
    skill = Skill.query.filter_by(name='registration-skill').first()

    if not skill:
        return jsonify({'error': '注册 Skill 未找到'}), 404

    # 获取标准化 Rules
    standard_rules = Rule.query.filter_by(is_standard=True).all()

    # 获取标准化 Skills（排除注册 Skill 本身）
    standard_skills = Skill.query.filter(
        Skill.is_standard == True,
        Skill.name != 'registration-skill'
    ).all()

    result = skill.to_dict()
    result['standard_rules'] = [r.to_dict() for r in standard_rules]
    result['standard_skills'] = [s.to_dict() for s in standard_skills]
    result['standard_rule_ids'] = [r.id for r in standard_rules]
    result['standard_skill_ids'] = [s.id for s in standard_skills]

    return jsonify(result)


@api_bp.route('/skills/registration-skill', methods=['POST'])
def create_registration_skill():
    """
    创建/更新注册 Skill 模板

    请求体：
    {
        "display_name": "注册技能",
        "description": "标准化注册流程，包含 Hub 注册、Rules/Skills 安装",
        "template_content": "...",  // 注册流程的具体执行内容
    }
    """
    data = request.get_json()

    # 查找或创建注册 Skill
    skill = Skill.query.filter_by(name='registration-skill').first()

    if skill:
        # 更新已有
        for field in ['display_name', 'description', 'template_content']:
            if field in data:
                setattr(skill, field, data[field])
    else:
        # 创建新的
        skill = Skill(
            name='registration-skill',
            display_name=data.get('display_name', '注册技能'),
            description=data.get('description', '标准化注册流程'),
            template_content=data.get('template_content', ''),
            category='standard',
            is_standard=True,
        )
        db.session.add(skill)

    db.session.commit()
    return jsonify(skill.to_dict()), 201
