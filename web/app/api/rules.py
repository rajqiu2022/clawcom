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
    """获取当前用户（支持 Web session 和 OpenClaw Bearer Token）"""
    uid = session.get('user_id')
    if uid:
        return User.query.get(uid)

    # Bearer Token → 找到 claw 的 owner 用户
    from flask import request
    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        token = auth[7:]
        from app.models import OpenClawInstance
        for claw in OpenClawInstance.query.filter(OpenClawInstance.status != 'deleted').all():
            if claw.verify_token(token):
                # admin claw 继承 admin 权限（不等同 super_admin）
                if claw.role == 'admin':
                    class _ClawAdminProxy:
                        role = 'admin'
                        username = claw.name
                        managed_projects = [claw.project_id] if claw.project_id else []
                    return _ClawAdminProxy()
                # 非 admin 角色：返回 owner 用户（继承其权限），附带 claw_name 供 _can_edit 使用
                owner = User.query.filter_by(username=claw.owner).first()
                if owner:
                    owner._claw_name = claw.name
                    return owner
    return None


def _user_project_ids(user):
    ids = set()
    if not user:
        return ids
    for pid in (getattr(user, 'managed_projects', None) or []):
        try:
            ids.add(int(pid))
        except Exception:
            continue
    bound_claw_id = getattr(user, 'bound_claw_id', None)
    if bound_claw_id:
        claw = OpenClawInstance.query.get(bound_claw_id)
        if claw and claw.project_id:
            ids.add(int(claw.project_id))
    return ids


def _rule_project_ids(rule):
    ids = set()
    for pid in (getattr(rule, 'applicable_projects', None) or []):
        try:
            ids.add(int(pid))
        except Exception:
            continue
    return ids


def _can_edit(user, resource):
    """检查用户是否有权编辑资源（skill/rule）
    super_admin 和 admin: 可以编辑一切（同等权限）
    user: 只能编辑自己创建的（或通过 Token 认证的 OpenClaw 自己创建的）
    """
    if not user:
        return False
    if user.role == 'super_admin':
        return True
    created_by = getattr(resource, 'created_by', None) or ''
    if created_by == user.username:
        return True
    # Token 认证时，_claw_name 是 OpenClaw 实例名，created_by 可能存的是 claw 名
    claw_name = getattr(user, '_claw_name', None)
    if claw_name and created_by == claw_name:
        return True
    if user.role == 'admin':
        managed_projects = _user_project_ids(user)
        res_projects = _rule_project_ids(resource)
        if managed_projects and res_projects and (managed_projects & res_projects):
            return True
    return False


@api_bp.route('/rules', methods=['GET'])
def list_rules():
    """获取 Rules 列表（支持筛选；已软删除的默认隐藏）"""
    scope = request.args.get('scope')  # global/project/module
    category = request.args.get('category')  # standard/custom/ai_generated
    project_id = request.args.get('project_id', type=int)
    search = request.args.get('search')
    show_deleted = request.args.get('show_deleted', 'false').lower() == 'true'

    query = Rule.query

    # 默认隐藏软删除的 Rule（除非管理员显式查询）
    user = _get_current_user()
    if not show_deleted or not user or user.role not in ('super_admin', 'admin'):
        query = query.filter(Rule.is_deleted != True)

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

    # 如果指定了项目ID，过滤适用项目（注意 project_id=0 也合法，例如 RacingGO）
    if project_id is not None:
        rules = [r for r in rules if r.scope in ('global', 'project') and (
            r.scope == 'global' or
            (r.applicable_projects and project_id in r.applicable_projects)
        )]

    return jsonify([_rule_with_usage(r) for r in rules])


def _rule_with_usage(r):
    """Rule 字典 + 使用者信息（含 used_by_stale，用于'重新分配'UI）"""
    d = r.to_dict()
    rows = (db.session.query(OpenClawInstance.name,
                             OpenClawRule.applied_at)
            .join(OpenClawRule)
            .filter(OpenClawRule.rule_id == r.id,
                    OpenClawRule.enabled == True)
            .all())
    fresh, stale = [], []
    rule_updated = r.updated_at
    for name, ap in rows:
        # stale 判定：applied_at 为空（从未确认）或早于 rule.updated_at（rule 改过）
        if ap and rule_updated and ap >= rule_updated:
            fresh.append(name)
        else:
            stale.append(name)
    d['used_by'] = [row[0] for row in rows]
    d['used_by_fresh'] = fresh
    d['used_by_stale'] = stale
    d['install_count'] = len(d['used_by'])
    d['stale_count'] = len(stale)
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

    # 自动设置 created_by：优先用 _claw_name（Token认证），其次用 username
    # ⚠️ 不再默认 'system'，未认证时必须传 created_by 或带 Token
    user = _get_current_user()
    if user:
        created_by = getattr(user, '_claw_name', None) or user.username
    elif data.get('created_by'):
        created_by = data['created_by']
    else:
        return jsonify({'error': '未认证请求必须提供 created_by 字段，请在 Header 中携带 Authorization: Bearer {TOKEN}'}), 401

    # 判断提交者身份，决定审核状态（与 Skill 一致）
    if user and user.role == 'super_admin':
        review_status = 'approved'
    else:
        review_status = 'pending'

    rule = Rule(
        name=data['name'],
        display_name=data['display_name'],
        description=data.get('description', ''),
        category=data.get('category', 'custom'),
        scope=data.get('scope', 'global'),
        applicable_projects=data.get('applicable_projects'),
        applicable_modules=data.get('applicable_modules'),
        content_template=data.get('content_template', ''),
        created_by=created_by,
        review_status=review_status,
    )
    try:
        db.session.add(rule)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': f'创建 Rule 失败: {str(e)}'}), 500

    from app.api.skills import _notify_admin_claws, _create_review_todo_for_admin_claws
    if review_status == 'pending':
        notified_ids = _notify_admin_claws('Rule', '待审核', rule.display_name,
                            f'类型: {rule.category}, 作用域: {rule.scope}, 提交人: {rule.created_by}\n请审核后通过或拒绝。')
        _create_review_todo_for_admin_claws('Rule', rule.display_name, rule.created_by, rule.category, rule.scope)
    else:
        notified_ids = _notify_admin_claws('Rule', '新建', rule.display_name,
                            f'类型: {rule.category}, 作用域: {rule.scope}')
    db.session.commit()
    from app.api.agent_client import notify_claw
    for cid in notified_ids:
        notify_claw(cid)

    return jsonify(rule.to_dict()), 201


@api_bp.route('/rules/<int:rule_id>', methods=['GET'])
def get_rule(rule_id):
    """获取 Rule 详情"""
    rule = Rule.query.get_or_404(rule_id)
    return jsonify(rule.to_dict())


@api_bp.route('/rules/<int:rule_id>/review', methods=['POST'])
def review_rule(rule_id):
    """审核 Rule（通过/打回待修改/废弃）— admin/super_admin 可审核
    
    请求体：
    {
        "review_status": "approved" | "revise" | "rejected",
        "review_comment": "审核意见"
    }
    """
    from app.api.skills import _get_current_user, _notify_admin_claws, _notify_submitter_review_result
    user = _get_current_user()
    if not user or user.role not in ('super_admin', 'admin'):
        return jsonify({'error': '仅管理员可审核 Rule'}), 403

    rule = Rule.query.get_or_404(rule_id)
    data = request.get_json()
    status = (data or {}).get('review_status', '')
    comment = (data or {}).get('review_comment', '')
    
    if status not in ('approved', 'revise', 'rejected'):
        return jsonify({'error': 'review_status 必须为 approved、revise 或 rejected'}), 400

    # 校验状态流转合法性
    if rule.review_status == 'approved' and status != 'approved':
        return jsonify({'error': '已通过的 Rule 不能再打回或废弃'}), 400
    if rule.review_status == 'rejected':
        return jsonify({'error': '已废弃的 Rule 不能再审核'}), 400

    old_status = rule.review_status
    rule.review_status = status
    if comment:
        rule.review_comment = comment
    elif status in ('revise', 'rejected'):
        rule.review_comment = comment

    # 审核通过时：将镜像内容应用到原内容
    if status == 'approved' and rule.mirror_content:
        import json
        try:
            mirror_data = json.loads(rule.mirror_content)
        except (ValueError, TypeError):
            mirror_data = {}
        # 保存当前内容到历史记录（最多10条）
        from app.models import _now as _model_now
        history = rule.content_history or []
        history_entry = {
            'content_template': rule.content_template or '',
            'display_name': rule.display_name or '',
            'description': rule.description or '',
            'updated_by': rule.mirror_updated_by or 'system',
            'updated_at': str(rule.mirror_updated_at) if rule.mirror_updated_at else str(_model_now()),
            'summary': f'审核通过前自动备份（修改人: {rule.mirror_updated_by or "未知"}）',
        }
        history.insert(0, history_entry)
        rule.content_history = history[:10]

        # 应用镜像内容到原字段
        for field in ['display_name', 'description', 'category', 'scope',
                      'applicable_projects', 'applicable_modules', 'content_template']:
            if field in mirror_data:
                setattr(rule, field, mirror_data[field])

        # 审核通过 = 镜像合入主体，最后修改人取自镜像提交人
        if rule.mirror_updated_by:
            rule.last_modified_by = rule.mirror_updated_by
            try:
                from app.models import OpenClawInstance as _Inst
                inst = _Inst.query.filter_by(name=rule.mirror_updated_by).first()
                rule.last_modified_source = 'openclaw' if inst else 'web'
            except Exception:
                rule.last_modified_source = 'web'
            rule.last_modified_at = _model_now()

        # 清空镜像
        rule.mirror_content = None
        rule.mirror_updated_by = None
        rule.mirror_updated_at = None

    # 审核打回时：清空镜像（提交人需要重新修改）
    if status == 'revise':
        rule.mirror_content = None
        rule.mirror_updated_by = None
        rule.mirror_updated_at = None
    
    status_labels = {'approved': '通过', 'revise': '打回待修改', 'rejected': '废弃'}
    action = status_labels.get(status, status)

    # 审核完成后，自动关闭龙虾王的相关审核待办
    from app.models import ClawTodo, ClawTodoLog
    review_todos = ClawTodo.query.filter(
        ClawTodo.title.like(f'%审核 Rule「{rule.display_name}」%'),
        ClawTodo.task_category == 'review',
        ClawTodo.enabled == True,
    ).all()
    from datetime import date as _date
    _today = _date.today()
    for todo in review_todos:
        todo.enabled = False
        log = ClawTodoLog.query.filter_by(
            todo_id=todo.id, log_date=_today
        ).first()
        if log and log.status == 'pending':
            log.status = 'approved' if status == 'approved' else 'rejected'
            log.result_summary = f'Rule「{rule.display_name}」已{action}'
            from app.models import _now as _model_now
            log.completed_at = _model_now()

    db.session.commit()

    notified_ids = _notify_admin_claws('Rule', f'审核{action}', rule.display_name,
                        f'{old_status} → {status}, 操作人: {user.username}' + (f'\n审核意见: {comment}' if comment else ''))
    
    if status == 'revise':
        _notify_submitter_review_result(rule.created_by, 'Rule', rule.display_name, action, comment)
    
    db.session.commit()
    from app.api.agent_client import notify_claw
    for cid in notified_ids:
        notify_claw(cid)

    return jsonify({'message': f'Rule 已{action}', 'review_status': status, 'review_comment': comment})


@api_bp.route('/rules/<int:rule_id>', methods=['PUT'])
def update_rule(rule_id):
    """更新 Rule（非管理员修改时写入镜像内容，审核通过后替换）"""
    rule = Rule.query.get_or_404(rule_id)

    # 已软删除的 Rule 不可编辑
    if rule.is_deleted:
        return jsonify({'error': '此 Rule 已被删除，无法编辑'}), 403

    # 已废弃的 Rule 不可编辑
    if rule.review_status == 'rejected':
        return jsonify({'error': '已废弃的 Rule 不可编辑'}), 403

    user = _get_current_user()
    if not _can_edit(user, rule):
        return jsonify({'error': '无权修改此 Rule，只有超级管理员、管理员或提交人可编辑'}), 403

    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({'error': '请求体必须是 JSON 对象'}), 400
    old_review_status = rule.review_status
    is_super_admin = user and user.role == 'super_admin'

    # 内容类字段
    content_fields = [
        'display_name', 'description', 'category', 'scope',
        'applicable_projects', 'applicable_modules', 'content_template'
    ]
    # 非内容类字段
    meta_fields = ['is_standard']

    if is_super_admin:
        # 超级管理员直接修改原内容
        for field in content_fields:
            if field in data:
                setattr(rule, field, data[field])
        for field in meta_fields:
            if field in data:
                setattr(rule, field, data[field])
        from app.api.skills import _notify_admin_claws
        notified_ids = _notify_admin_claws('Rule', '更新', rule.display_name,
                            f'更新字段: {", ".join(data.keys())}')
    else:
        # 非超级管理员：内容字段写入镜像
        from app.models import _now
        modifier = (
            getattr(user, 'bound_claw_name', '')
            or getattr(user, '_claw_name', '')
            or getattr(user, 'display_name', '')
            or getattr(user, 'username', '')
            or 'unknown'
        )
        mirror_data = {}
        for field in content_fields:
            if field in data:
                mirror_data[field] = data[field]
        # 元数据字段直接修改
        for field in meta_fields:
            if field in data:
                setattr(rule, field, data[field])
        # 内容字段写入镜像
        if mirror_data:
            import json
            rule.mirror_content = json.dumps(mirror_data, ensure_ascii=False)
            rule.mirror_updated_by = modifier
            rule.mirror_updated_at = _now()

        # 非管理员编辑后，自动重置为待评审状态
        if old_review_status != 'pending':
            rule.review_status = 'pending'
            rule.review_comment = None
        from app.api.skills import _notify_admin_claws, _create_review_todo_for_admin_claws
        notified_ids = _notify_admin_claws('Rule', '待审核（修改后重新提交）', rule.display_name,
                            f'类型: {rule.category}, 作用域: {rule.scope}, 提交人: {modifier}\n请审核后通过或拒绝。')
        _create_review_todo_for_admin_claws('Rule', rule.display_name, modifier, rule.category, rule.scope)

    # review_status 只能通过专用审核接口修改
    if 'review_status' in data:
        return jsonify({'error': '请使用 POST /rules/<id>/review 接口修改审核状态'}), 403

    # 记录最后修改人 + 来源
    if data:
        from app.models import _now as _record_now
        claw_name = getattr(user, '_claw_name', None) if user else None
        if claw_name:
            rule.last_modified_by = claw_name
            rule.last_modified_source = 'openclaw'
        elif user:
            rule.last_modified_by = (
                getattr(user, 'bound_claw_name', '')
                or getattr(user, 'display_name', '')
                or getattr(user, 'username', '')
            )
            rule.last_modified_source = 'web'
        rule.last_modified_at = _record_now()

    db.session.commit()
    from app.api.agent_client import notify_claw
    for cid in notified_ids:
        notify_claw(cid)

    return jsonify(rule.to_dict())


@api_bp.route('/rules/<int:rule_id>/history', methods=['GET'])
def get_rule_history(rule_id):
    """获取 Rule 修改历史（最近10条）"""
    rule = Rule.query.get_or_404(rule_id)
    return jsonify({'history': rule.content_history or [], 'total': len(rule.content_history or [])})


@api_bp.route('/rules/<int:rule_id>/restore', methods=['POST'])
def restore_rule_history(rule_id):
    """从历史记录恢复 Rule 内容（仅 super_admin）

    请求体：{"index": 0}  — 0 表示最新一条历史
    """
    from app.api.skills import _get_current_user
    from app.models import _now
    user = _get_current_user()
    if not user or user.role != 'super_admin':
        return jsonify({'error': '只有超级管理员可以恢复历史版本'}), 403

    rule = Rule.query.get_or_404(rule_id)
    data = request.get_json()
    idx = (data or {}).get('index', -1)
    history = rule.content_history or []

    if idx < 0 or idx >= len(history):
        return jsonify({'error': f'无效的索引，历史记录共 {len(history)} 条'}), 400

    entry = history[idx]

    # 保存当前内容到历史（作为恢复前的备份）
    current_entry = {
        'content_template': rule.content_template or '',
        'display_name': rule.display_name or '',
        'description': rule.description or '',
        'updated_by': user.username,
        'updated_at': str(_now()),
        'summary': f'恢复前自动备份（恢复到索引 {idx}）',
    }
    history.insert(0, current_entry)

    # 恢复内容
    if entry.get('content_template'):
        rule.content_template = entry['content_template']
    if entry.get('display_name'):
        rule.display_name = entry['display_name']
    if entry.get('description'):
        rule.description = entry['description']

    rule.content_history = history[:10]
    db.session.commit()

    return jsonify({'message': f'已恢复到历史版本 #{idx}', 'rule': rule.to_dict()})


@api_bp.route('/rules/<int:rule_id>', methods=['DELETE'])
def delete_rule(rule_id):
    """软删除 Rule（隐藏，OpenClaw 搜索安装时不可见）

    权限规则：
    - super_admin：可删除任何 Rule
    - admin：只能删除自己项目创建的 Rule
    - user：只能删除自己创建的 Rule
    """
    rule = Rule.query.get_or_404(rule_id)

    user = _get_current_user()
    if not user:
        return jsonify({'error': '请先登录'}), 403

    # 已软删除的不能重复删除
    if rule.is_deleted:
        return jsonify({'error': '该 Rule 已被删除'}), 400

    # 权限检查
    can_delete = False
    if user.role == 'super_admin':
        can_delete = True
    elif user.role == 'admin':
        managed = _user_project_ids(user)
        scoped = _rule_project_ids(rule)
        can_delete = bool(managed and scoped and (managed & scoped))
    else:
        created_by = rule.created_by or ''
        if created_by == user.username:
            can_delete = True
        elif created_by == getattr(user, '_claw_name', None):
            can_delete = True

    if not can_delete:
        return jsonify({'error': '无权删除此 Rule，只能删除自己创建的'}), 403

    # 软删除：标记 is_deleted=True，禁用安装关联
    rule.is_deleted = True
    rule.deleted_at = datetime.now()

    # 禁用所有安装关联
    OpenClawRule.query.filter_by(rule_id=rule_id).update({'enabled': False})

    from app.api.skills import _notify_admin_claws
    notified_ids = _notify_admin_claws('Rule', '删除（隐藏）', rule.display_name,
                        f'由 {user.username} 软删除，OpenClaw 将无法搜索安装')
    db.session.commit()
    from app.api.agent_client import notify_claw
    for cid in notified_ids:
        notify_claw(cid)

    return jsonify({'message': f'Rule "{rule.name}" 已删除（隐藏），OpenClaw 将无法搜索安装'})


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

    # 增量安装 / 重新分配（已有且 rule 已修改 → 视为 reinstall）
    from app.models import ClawTodo, _now as _model_now
    installed_names = []
    reinstalled_names = []
    for rule_id in rule_ids:
        rule = Rule.query.get(rule_id)
        if not rule:
            continue
        # 已软删除的 Rule 不能安装
        if rule.is_deleted:
            return jsonify({'error': f'Rule "{rule.display_name}" 已被删除，无法安装'}), 403
        existing = OpenClawRule.query.filter_by(
            openclaw_id=claw_id, rule_id=rule_id
        ).first()
        is_reinstall = False
        reinstall_reason = ''  # 'content_updated' | 'forced' | ''
        if existing:
            existing.enabled = True
            # existing 命中 = 重装。区分两种 reason：
            #   content_updated: rule 本体改过
            #   forced: 用户手工再点一次（用于安装失败恢复 / 强制覆盖）
            is_reinstall = True
            if (existing.applied_at and rule.updated_at
                    and existing.applied_at < rule.updated_at):
                reinstall_reason = 'content_updated'
            else:
                reinstall_reason = 'forced'
            # 一律刷新 applied_at —— used_by_stale 立即清零
            existing.applied_at = _model_now()
            existing.applied = True
        else:
            db.session.add(OpenClawRule(
                openclaw_id=claw_id, rule_id=rule_id,
                enabled=True, applied=True, applied_at=_model_now()
            ))
        if is_reinstall:
            reinstalled_names.append(rule.display_name)
        installed_names.append(rule.display_name)

        # 下发安装/重装待办
        action_label = '重新安装' if is_reinstall else '安装'
        action_hint = '重新拉取并覆盖' if is_reinstall else '拉取并安装'
        if is_reinstall:
            if reinstall_reason == 'content_updated':
                reinstall_note = (
                    f'\n⚠️ 这是 **重新分配（内容已更新）**：本 Rule 在 Hub 端已修改（updated_at={rule.updated_at}），'
                    f'你之前的安装版本已过期，必须重新拉取并**全量覆盖** `~/.qclaw/rules/{rule.name}.md`。\n'
                )
            else:
                reinstall_note = (
                    f'\n🔁 这是 **强制重新分配（内容未变）**：通常用于安装失败恢复 / 文件被误删 / 强制覆盖场景。'
                    f'请重新拉取并**全量覆盖** `~/.qclaw/rules/{rule.name}.md`，不要假定本地已有的就是对的。\n'
                )
        else:
            reinstall_note = ''
        todo = ClawTodo(
            openclaw_id=claw_id,
            title=f'{action_label} Rule：{rule.display_name}',
            description=(
                f'Hub 已{action_label.replace("安装","分配")} Rule「{rule.display_name}」(id={rule.id})，请{action_hint}到本地。\n'
                f'{reinstall_note}\n'
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
            verification_target=f'{"reinstall" if is_reinstall else "install"}-rule:{rule.name}',
            enabled=True,
            created_by='hub',
        )
        db.session.add(todo)

    # 通知 OpenClaw 同步配置
    from app.models import ClawMessage
    if reinstalled_names:
        notice_head = f'[重新分配 Rules] 已重装 {len(reinstalled_names)} 条规则：{", ".join(reinstalled_names)}'
        if len(installed_names) > len(reinstalled_names):
            extra = [n for n in installed_names if n not in reinstalled_names]
            notice_head += f'；新装 {len(extra)} 条：{", ".join(extra)}'
        notice_head += '，请同步配置'
    else:
        notice_head = f'[安装 Rules] 已分配 {len(installed_names)} 条规则：{", ".join(installed_names)}，请同步配置'
    msg = ClawMessage(
        claw_id=claw_id, sender_name='Hub',
        content=notice_head,
        msg_type='sync_config', direction='to_claw', status='pending',
    )
    db.session.add(msg)
    db.session.commit()

    # 通知 SSE 长连接立即推送
    from app.api.agent_client import notify_claw
    notify_claw(claw_id)

    return jsonify({'message': f'Rules 已分配', 'rule_ids': rule_ids})


@api_bp.route('/openclaws/<int:claw_id>/rules/<int:rule_id>', methods=['DELETE'])
def uninstall_claw_rule(claw_id, rule_id):
    """移除 OpenClaw 的某条 Rule"""
    link = OpenClawRule.query.filter_by(
        openclaw_id=claw_id, rule_id=rule_id
    ).first_or_404()

    rule_name = link.rule.display_name if link.rule else str(rule_id)
    link.enabled = False
    db.session.commit()
    return jsonify({'message': f'已移除规范「{rule_name}」'})


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
            assoc.applied_at = datetime.now()

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
