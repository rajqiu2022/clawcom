"""
Rules 工作规范 API
支持 CRUD 和 AI 生成
"""

import json
import secrets
from datetime import datetime
from flask import request, jsonify
from app import db
from app.models import Rule, OpenClawRule, OpenClawInstance
from app.api import api_bp


@api_bp.route('/rules', methods=['GET'])
def list_rules():
    """获取 Rules 列表（支持筛选）"""
    scope = request.args.get('scope')  # global/project/module
    category = request.args.get('category')  # standard/custom/ai_generated
    project_id = request.args.get('project_id', type=int)
    search = request.args.get('search')

    query = Rule.query

    if scope:
        query = query.filter_by(scope=scope)
    if category:
        query = query.filter_by(category=category)
    if search:
        query = query.filter(
            db.or_(
                Rule.name.ilike(f'%{search}%'),
                Rule.display_name.ilike(f'%{search}%'),
                Rule.description.ilike(f'%{search}%')
            )
        )

    rules = query.order_by(Rule.created_at.desc()).all()

    # 如果指定了项目ID，过滤适用项目
    if project_id:
        rules = [r for r in rules if r.scope in ('global', 'project') and (
            r.scope == 'global' or
            (r.applicable_projects and project_id in r.applicable_projects)
        )]

    return jsonify([r.to_dict() for r in rules])


@api_bp.route('/rules', methods=['POST'])
def create_rule():
    """创建 Rule"""
    data = request.get_json()

    if not data or not data.get('name') or not data.get('display_name'):
        return jsonify({'error': 'name 和 display_name 为必填项'}), 400

    # 检查唯一性
    if Rule.query.filter_by(name=data['name']).first():
        return jsonify({'error': f'Rule {data["name"]} 已存在'}), 409

    rule = Rule(
        name=data['name'],
        display_name=data['display_name'],
        description=data.get('description', ''),
        category=data.get('category', 'custom'),
        scope=data.get('scope', 'global'),
        applicable_projects=data.get('applicable_projects'),
        applicable_modules=data.get('applicable_modules'),
        content_template=data.get('content_template', ''),
    )
    db.session.add(rule)
    db.session.commit()

    return jsonify(rule.to_dict()), 201


@api_bp.route('/rules/<int:rule_id>', methods=['GET'])
def get_rule(rule_id):
    """获取 Rule 详情"""
    rule = Rule.query.get_or_404(rule_id)
    return jsonify(rule.to_dict())


@api_bp.route('/rules/<int:rule_id>', methods=['PUT'])
def update_rule(rule_id):
    """更新 Rule"""
    rule = Rule.query.get_or_404(rule_id)
    data = request.get_json()

    updatable_fields = [
        'display_name', 'description', 'category', 'scope',
        'applicable_projects', 'applicable_modules', 'content_template'
    ]
    for field in updatable_fields:
        if field in data:
            setattr(rule, field, data[field])

    db.session.commit()
    return jsonify(rule.to_dict())


@api_bp.route('/rules/<int:rule_id>', methods=['DELETE'])
def delete_rule(rule_id):
    """删除 Rule"""
    rule = Rule.query.get_or_404(rule_id)
    db.session.delete(rule)
    db.session.commit()
    return jsonify({'message': f'Rule "{rule.name}" 已删除'})


# ==================== OpenClaw 关联 Rules ====================

@api_bp.route('/openclaws/<int:claw_id>/rules', methods=['GET'])
def get_claw_rules(claw_id):
    """获取 OpenClaw 关联的 Rules"""
    claw = OpenClawInstance.query.get_or_404(claw_id)

    associations = OpenClawRule.query.filter_by(
        openclaw_id=claw_id
    ).all()

    return jsonify([{
        'rule': association.rule.to_dict(),
        'enabled': association.enabled,
        'applied': association.applied,
        'applied_at': str(association.applied_at) if association.applied_at else None,
    } for association in associations])


@api_bp.route('/openclaws/<int:claw_id>/rules', methods=['POST'])
def update_claw_rules(claw_id):
    """
    更新 OpenClaw 关联的 Rules（勾选/取消勾选）

    请求体：
    {
        "rule_ids": [1, 2, 3]  // 要关联的 rule_id 列表
    }
    """
    claw = OpenClawInstance.query.get_or_404(claw_id)
    data = request.get_json()

    if 'rule_ids' not in data:
        return jsonify({'error': 'rule_ids 为必填项'}), 400

    rule_ids = data['rule_ids']

    # 删除旧的关联
    OpenClawRule.query.filter_by(openclaw_id=claw_id).delete()

    # 创建新的关联
    for rule_id in rule_ids:
        rule = Rule.query.get(rule_id)
        if rule:
            assoc = OpenClawRule(
                openclaw_id=claw_id,
                rule_id=rule_id,
                enabled=True
            )
            db.session.add(assoc)

    db.session.commit()

    return jsonify({'message': 'Rules 关联已更新', 'rule_ids': rule_ids})


@api_bp.route('/openclaws/<int:claw_id>/rules/preview', methods=['POST'])
def preview_rules_merge(claw_id):
    """
    预览 Rules 合并结果（生成应用后的配置内容）
    用户确认后会调用 apply 接口

    返回：合并后的配置内容（用于用户确认）
    """
    claw = OpenClawInstance.query.get_or_404(claw_id)
    data = request.get_json()

    rule_ids = data.get('rule_ids', [])

    if not rule_ids:
        return jsonify({'error': 'rule_ids 不能为空'}), 400

    # 获取选中的 rules
    rules = Rule.query.filter(Rule.id.in_(rule_ids)).all()

    # 合并 content_template
    merged_content = []
    for rule in rules:
        if rule.content_template:
            merged_content.append(f"# {rule.display_name}\n\n{rule.content_template}")

    preview = "\n\n---\n\n".join(merged_content)

    return jsonify({
        'preview': preview,
        'rule_count': len(rules),
        'rules': [r.to_dict() for r in rules],
    })


@api_bp.route('/openclaws/<int:claw_id>/rules/apply', methods=['POST'])
def apply_rules_to_claw(claw_id):
    """
    应用 Rules 到 OpenClaw（需要用户确认）

    会更新 OpenClaw 的 soul_config 或 workflow_config
    """
    claw = OpenClawInstance.query.get_or_404(claw_id)
    data = request.get_json()

    rule_ids = data.get('rule_ids', [])
    target_config = data.get('target_config', 'soul_config')  # soul_config / workflow_config

    if not rule_ids:
        return jsonify({'error': 'rule_ids 不能为空'}), 400

    # 获取选中的 rules
    rules = Rule.query.filter(Rule.id.in_(rule_ids)).all()

    # 合并 content_template
    merged_content = []
    for rule in rules:
        if rule.content_template:
            merged_content.append(f"# {rule.display_name}\n\n{rule.content_template}")

    new_content = "\n\n---\n\n".join(merged_content)

    # 更新 OpenClaw 配置
    if target_config == 'soul_config':
        claw.soul_config = new_content
    elif target_config == 'workflow_config':
        claw.workflow_config = new_content
    else:
        return jsonify({'error': f'无效的 target_config: {target_config}'}), 400

    # 标记已应用
    for rule_id in rule_ids:
        assoc = OpenClawRule.query.filter_by(
            openclaw_id=claw_id,
            rule_id=rule_id
        ).first()
        if assoc:
            assoc.applied = True
            assoc.applied_at = datetime.utcnow()

    db.session.commit()

    return jsonify({
        'message': 'Rules 已应用到 OpenClaw',
        'target_config': target_config,
        'applied_rules': len(rules),
    })


# ==================== 标准化 Rules 管理 ====================

@api_bp.route('/rules/standard', methods=['GET'])
def list_standard_rules():
    """获取所有标准化 Rules"""
    rules = Rule.query.filter_by(is_standard=True).order_by(Rule.name).all()
    return jsonify([r.to_dict() for r in rules])


@api_bp.route('/rules/standard', methods=['PUT'])
def batch_update_standard_rules():
    """
    批量更新 Rules 的标准化标记

    请求体：
    {
        "rule_ids": [1, 2, 3],  // 要标记为标准化的 rule_ids
        "standard": true         // true=标记为标准化，false=取消标准化
    }
    """
    data = request.get_json()
    if not data or 'rule_ids' not in data:
        return jsonify({'error': 'rule_ids 为必填项'}), 400

    rule_ids = data['rule_ids']
    is_standard = data.get('standard', True)

    updated = 0
    for rule_id in rule_ids:
        rule = Rule.query.get(rule_id)
        if rule:
            rule.is_standard = is_standard
            updated += 1

    db.session.commit()

    action = "已标记为标准化" if is_standard else "已取消标准化"
    return jsonify({
        'message': f'{updated} 个 Rules {action}',
        'updated': updated,
    })
