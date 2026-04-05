"""
Rules 工作规范 API
支持 CRUD 和 AI 生成
"""

import json
import secrets
from datetime import datetime
from flask import request, jsonify, session
from app import db
from app.models import Rule, OpenClawRule, OpenClawInstance, User
from app.api import api_bp


def _get_current_user():
    """获取当前登录用户"""
    uid = session.get('user_id')
    if not uid:
        return None
    return User.query.get(uid)


def _can_edit(user, resource):
    """检查用户是否有权编辑资源"""
    if not user:
        return False
    if user.role == 'super_admin':
        return True
    created_by = getattr(resource, 'created_by', None) or ''
    if created_by == user.username:
        return True
    if user.role == 'admin':
        managed = user.managed_projects or []
        if not managed:
            return False
        res_projects = getattr(resource, 'applicable_projects', None) or []
        if not res_projects:
            return False
        if set(res_projects) & set(managed):
            return True
    return False


@api_bp.route('/rules', methods=['GET'])
def list_rules():
    """获取 Rules 列表（支持筛选）"""
    scope = request.args.get('scope')  # global/project/module
    category = request.args.get('category')  # standard/custom/ai_generated
    project_id = request.args.get('project_id', type=int)
    search = request.args.get('search')

    query = Rule.query

    # By default, hide admin-scoped rules unless explicitly requested
    include_admin = request.args.get('include_admin', 'false').lower() == 'true'
    if scope:
        query = query.filter_by(scope=scope)
    elif not include_admin:
        query = query.filter(Rule.scope != 'admin')
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

    return jsonify([_rule_with_usage(r) for r in rules])


def _rule_with_usage(r):
    """Rule 字典 + 使用者信息"""
    d = r.to_dict()
    users = (db.session.query(OpenClawInstance.name)
             .join(OpenClawRule)
             .filter(OpenClawRule.rule_id == r.id,
                     OpenClawRule.enabled == True)
             .all())
    d['used_by'] = [u[0] for u in users]
    d['install_count'] = len(d['used_by'])
    return d


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
        created_by=data.get('created_by', 'system'),
    )
    try:
        db.session.add(rule)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': f'创建 Rule 失败: {str(e)}'}), 500

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

    user = _get_current_user()
    if not _can_edit(user, rule):
        return jsonify({'error': '无权修改此 Rule，只有超级管理员、管理员或提交人可编辑'}), 403

    data = request.get_json()

    updatable_fields = [
        'display_name', 'description', 'category', 'scope',
        'applicable_projects', 'applicable_modules', 'content_template'
    ]
    for field in updatable_fields:
        if field in data:
            setattr(rule, field, data[field])

    if 'is_standard' in data:
        rule.is_standard = bool(data['is_standard'])

    # review_status 只有 admin 以上可改
    if 'review_status' in data and data['review_status'] in ('approved', 'pending', 'rejected'):
        if user and user.role in ('super_admin', 'admin'):
            rule.review_status = data['review_status']
        else:
            return jsonify({'error': '只有管理员可以修改审核状态'}), 403

    db.session.commit()
    return jsonify(rule.to_dict())


@api_bp.route('/rules/<int:rule_id>', methods=['DELETE'])
def delete_rule(rule_id):
    """删除 Rule（管理员权限）

    删除后，所有安装过此 Rule 的 OpenClaw 的关联记录会被级联删除，
    OpenClaw 自行在下次同步时感知到 Rule 已不存在。
    """
    rule = Rule.query.get_or_404(rule_id)

    user = _get_current_user()
    if not user or user.role not in ('super_admin', 'admin'):
        return jsonify({'error': '只有管理员可以删除 Rule'}), 403

    # 级联删除所有安装关联（OpenClawRule）
    OpenClawRule.query.filter_by(rule_id=rule_id).delete()

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
    为 OpenClaw 安装 Rules（增量添加，不会删除已有的）

    请求体：
    {
        "rule_ids": [1, 2, 3]  // 要安装的 rule_id 列表
    }
    """
    claw = OpenClawInstance.query.get_or_404(claw_id)
    data = request.get_json()

    if 'rule_ids' not in data:
        return jsonify({'error': 'rule_ids 为必填项'}), 400

    rule_ids = data['rule_ids']

    # Validate: admin-scoped rules can only be installed by their owner
    for rule_id in rule_ids:
        rule = Rule.query.get(rule_id)
        if rule and rule.scope == 'admin' and rule.owner_claw_id and rule.owner_claw_id != claw_id:
            return jsonify({'error': f'Rule "{rule.display_name}" 为管理员专属，不能安装到其他 OpenClaw'}), 403

    # 增量安装（已有的跳过）
    from app.models import ClawTodo
    installed_names = []
    for rule_id in rule_ids:
        rule = Rule.query.get(rule_id)
        if not rule:
            continue
        existing = OpenClawRule.query.filter_by(
            openclaw_id=claw_id, rule_id=rule_id
        ).first()
        if existing:
            existing.enabled = True
        else:
            db.session.add(OpenClawRule(
                openclaw_id=claw_id, rule_id=rule_id, enabled=True
            ))
        installed_names.append(rule.display_name)

        # 下发安装待办
        todo = ClawTodo(
            openclaw_id=claw_id,
            title=f'安装 Rule：{rule.display_name}',
            description=(
                f'Hub 已分配 Rule「{rule.display_name}」(id={rule.id})，请拉取并安装到本地。\n\n'
                f'执行步骤：\n'
                f'1. GET /api/v1/openclaws/{claw_id}/assigned-rules 获取最新 Rules 列表\n'
                f'2. 找到 name="{rule.name}" 的 Rule，获取 content_template\n'
                f'3. 写入 ~/.qclaw/rules/{rule.name}.md\n'
                f'4. 完成后上报 POST /todos/{{todo_id}}/complete'
            ),
            schedule_type='once',
            urgency_level='interrupt',
            priority='P0',
            task_category='routine',
            verification_target=f'install-rule:{rule.name}',
            enabled=True,
            created_by='hub',
        )
        db.session.add(todo)

    # 通知 OpenClaw 同步配置
    from app.models import ClawMessage
    msg = ClawMessage(
        claw_id=claw_id, sender_name='Hub',
        content=f'[安装 Rules] 已分配 {len(installed_names)} 条规则：{", ".join(installed_names)}，请同步配置',
        msg_type='sync_config', direction='to_claw', status='pending',
    )
    db.session.add(msg)
    db.session.commit()

    return jsonify({'message': f'Rules 已分配', 'rule_ids': rule_ids})


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
