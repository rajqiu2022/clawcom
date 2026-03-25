from flask import request, jsonify
from app import db
from app.models import Skill, OpenClawSkill, OpenClawInstance
from app.api import api_bp


@api_bp.route('/skills', methods=['GET'])
def list_skills():
    """获取 Skills 列表"""
    skills = Skill.query.order_by(Skill.category, Skill.name).all()
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
    )
    db.session.add(skill)
    db.session.commit()
    return jsonify(skill.to_dict()), 201


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
