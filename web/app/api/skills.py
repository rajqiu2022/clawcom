from flask import request, jsonify, session
from datetime import datetime
from app import db
from app.models import Skill, OpenClawSkill, OpenClawInstance, Rule, User


def _get_current_user():
    """获取当前用户（支持 Web session 和 OpenClaw Bearer Token）"""
    uid = session.get('user_id')
    if uid:
        return User.query.get(uid)

    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        token = auth[7:]
        for claw in OpenClawInstance.query.filter(OpenClawInstance.status != 'deleted').all():
            if claw.verify_token(token):
                if claw.role == 'admin':
                    class _AdminProxy:
                        role = 'super_admin'
                        username = claw.name
                        managed_projects = []
                    return _AdminProxy()
                # 非 admin 角色：返回 owner 用户（继承其权限），附带 claw_name 供 _can_edit 使用
                owner = User.query.filter_by(username=claw.owner).first()
                if owner:
                    owner._claw_name = claw.name
                    return owner
    return None


def _can_edit(user, resource):
    """检查用户是否有权编辑资源（skill/rule）
    super_admin 和 admin: 可以编辑一切（同等权限）
    user: 只能编辑自己创建的（或通过 Token 认证的 OpenClaw 自己创建的）
    """
    if not user:
        return False
    if user.role in ('super_admin', 'admin'):
        return True
    created_by = getattr(resource, 'created_by', None) or ''
    if created_by == user.username:
        return True
    # Token 认证时，_claw_name 是 OpenClaw 实例名，created_by 可能存的是 claw 名
    claw_name = getattr(user, '_claw_name', None)
    if claw_name and created_by == claw_name:
        return True
    # admin 可编辑自己管理项目下的资源
    if user.role == 'admin':
        managed_projects = getattr(user, 'managed_projects', None) or []
        res_projects = getattr(resource, 'applicable_projects', None) or []
        if managed_projects and res_projects and any(p in managed_projects for p in res_projects):
            return True
    return False
from app.api import api_bp


@api_bp.route('/skills', methods=['GET'])
def list_skills():
    """获取 Skills 列表（scope=admin 的仅管理员可见；非管理员只能看已审核通过的；已软删除的默认隐藏）

    支持搜索参数：
    - search: 标题关键词搜索
    - semantic_search: 语义搜索（标题无匹配时用 LLM 按描述匹配）
    """
    category_filter = request.args.get('category')
    review_filter = request.args.get('review_status')  # 可选过滤：pending/approved/rejected
    show_deleted = request.args.get('show_deleted', 'false').lower() == 'true'  # 管理员可查看已删除
    search_keyword = request.args.get('search', '').strip()
    semantic_search = request.args.get('semantic_search', '').strip()
    query = Skill.query
    if category_filter:
        query = query.filter(Skill.category == category_filter)

    # 默认隐藏软删除的 Skill（除非管理员显式查询）
    user = _get_current_user()
    if not show_deleted or not user or user.role not in ('super_admin', 'admin'):
        query = query.filter(Skill.is_deleted != True)

    # scope=admin 的 Skill 仅超级管理员和管理员可见
    if not user or user.role not in ('super_admin', 'admin'):
        query = query.filter(Skill.scope != 'admin')
        # 非管理员只能看到已审核通过的（自己的 pending 也可以看到）
        if review_filter:
            query = query.filter(Skill.review_status == review_filter)
        else:
            if user:
                claw_name = getattr(user, '_claw_name', None)
                own_filters = [Skill.created_by == user.username]
                if claw_name:
                    own_filters.append(Skill.created_by == claw_name)
                query = query.filter(
                    db.or_(
                        Skill.review_status == 'approved',
                        *own_filters
                    )
                )
            else:
                query = query.filter(Skill.review_status == 'approved')
    else:
        # 管理员可按审核状态过滤
        if review_filter:
            query = query.filter(Skill.review_status == review_filter)

    # 标题搜索（display_name 或 name 模糊匹配）
    if search_keyword:
        query = query.filter(
            db.or_(
                Skill.display_name.like(f'%{search_keyword}%'),
                Skill.name.like(f'%{search_keyword}%')
            )
        )

    skills = query.order_by(Skill.created_at.desc()).all()

    # 语义搜索：标题无匹配时，用 LLM 根据描述匹配
    if semantic_search and not skills and not search_keyword:
        skills = _semantic_search_skills(semantic_search, user, review_filter, show_deleted)

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


def _semantic_search_skills(keyword, user=None, review_filter=None, show_deleted=False):
    """用 LLM 根据关键词语义匹配 Skill 描述"""
    # 先获取所有候选 Skill（非删除、权限过滤后的）
    query = Skill.query.filter(Skill.is_deleted != True)
    if not user or user.role != 'super_admin':
        query = query.filter(Skill.scope != 'admin')
        query = query.filter(Skill.review_status == 'approved')
    if review_filter:
        query = query.filter(Skill.review_status == review_filter)

    all_skills = query.all()
    if not all_skills:
        return []

    # 构建 Skill 摘要列表
    skill_summaries = []
    for s in all_skills:
        desc = (s.description or '')[:200]
        skill_summaries.append({
            'id': s.id,
            'name': s.name,
            'display_name': s.display_name,
            'description': desc,
        })

    # 调用 LLM 匹配
    try:
        from app.api.system import _call_llm
        from sqlalchemy import text
        import json

        rows = dict(db.session.execute(
            text("SELECT config_key, value FROM system_config")
        ).fetchall())
        provider = rows.get('llm_provider', 'doubao')
        model = rows.get('llm_model', 'doubao-pro-32k')
        api_base = rows.get('llm_api_base', 'https://ark.cn-beijing.volces.com/api/v3')
        api_key = rows.get('llm_api_key', '')
        if not api_key:
            return []

        prompt = f"""你是一个语义匹配专家。用户正在搜索 Skill，请根据搜索关键词，从以下 Skill 列表中找出语义相关的 Skill。

搜索关键词：{keyword}

Skill 列表：
{json.dumps(skill_summaries, ensure_ascii=False, indent=2)}

请返回匹配的 Skill ID 列表，格式为 JSON 数组，如 [1, 3, 5]。
只返回 ID，不需要解释。如果没有匹配的，返回空数组 []。
直接输出 JSON 数组："""

        result = _call_llm(prompt, provider, model, api_base, api_key)
        # 解析结果
        cleaned = result.strip()
        if cleaned.startswith('```'):
            cleaned = cleaned.split('\n', 1)[1] if '\n' in cleaned else cleaned[3:]
        if cleaned.endswith('```'):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

        matched_ids = json.loads(cleaned)
        if not isinstance(matched_ids, list):
            return []

        # 按匹配顺序返回
        id_set = set(matched_ids)
        matched_skills = []
        for mid in matched_ids:
            s = next((s for s in all_skills if s.id == mid), None)
            if s:
                matched_skills.append(s)
        return matched_skills
    except Exception:
        return []


@api_bp.route('/skills/<int:skill_id>', methods=['GET'])
def get_skill(skill_id):
    """获取指定 Skill 详情"""
    skill = Skill.query.get_or_404(skill_id)
    return jsonify(skill.to_dict())


@api_bp.route('/skills', methods=['POST'])
def create_skill():
    """创建新 Skill

    龙虾王（admin角色）或超级管理员创建的 Skill 直接 approved；
    其他 OpenClaw 提交的 Skill 默认 pending，需审核后才进入市场。
    """
    data = request.get_json()
    if not data or not data.get('name') or not data.get('display_name'):
        return jsonify({'error': '标识名和显示名称为必填项'}), 400

    if Skill.query.filter_by(name=data['name']).first():
        return jsonify({'error': f'Skill "{data["name"]}" 已存在'}), 409

    # 判断提交者身份，决定审核状态
    user = _get_current_user()
    if user and user.role == 'super_admin':
        review_status = 'approved'
    else:
        review_status = 'pending'

    # 自动设置 created_by：优先用 _claw_name（Token认证），其次用 username
    # ⚠️ 不再默认 'system'，未认证时必须传 created_by 或带 Token
    if user:
        created_by = getattr(user, '_claw_name', None) or user.username
    elif data.get('created_by'):
        created_by = data['created_by']
    else:
        return jsonify({'error': '未认证请求必须提供 created_by 字段，请在 Header 中携带 Authorization: Bearer {TOKEN}'}), 401

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
        created_by=created_by,
        is_standard=bool(data.get('is_standard', False)),
        review_status=review_status,
    )

    # 进化技能额外字段
    if data.get('category') == 'evolved':
        skill.evolve_source = data.get('evolve_source')
        skill.success_rate = data.get('success_rate', 0)
        skill.total_runs = data.get('total_runs', 0)
        skill.error_count = data.get('error_count', 0)
        skill.last_evolved_at = datetime.now()
        skill.evolve_history = [{
            'action': 'created',
            'timestamp': datetime.now().isoformat(),
            'detail': f'由 OpenSpace 从 {data.get("evolve_source", "未知")} 进化而来',
        }]

    db.session.add(skill)
    db.session.commit()

    if review_status == 'pending':
        notified_ids = _notify_admin_claws('Skill', '待审核', skill.display_name,
                            f'类型: {skill.category}, 作用域: {skill.scope}, 提交人: {skill.created_by}\n请审核后通过或拒绝。')
        _create_review_todo_for_admin_claws('Skill', skill.display_name, skill.created_by, skill.category, skill.scope)
    else:
        notified_ids = _notify_admin_claws('Skill', '新建', skill.display_name,
                            f'类型: {skill.category}, 作用域: {skill.scope}')
    db.session.commit()
    from app.api.agent_client import notify_claw
    for cid in notified_ids:
        notify_claw(cid)

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
            existing.last_evolved_at = datetime.now()
            history = existing.evolve_history or []
            history.append({
                'action': 'evolved',
                'timestamp': datetime.now().isoformat(),
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
                last_evolved_at=datetime.now(),
                evolve_history=[{
                    'action': 'created',
                    'timestamp': datetime.now().isoformat(),
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


@api_bp.route('/skills/<int:skill_id>/review', methods=['POST'])
def review_skill(skill_id):
    """审核 Skill（通过/打回待修改/废弃）— 仅 super_admin（龙虾王 Token 认证映射为 super_admin）
    
    请求体：
    {
        "review_status": "approved" | "revise" | "rejected",
        "review_comment": "审核意见（打回/废弃时填写）"
    }
    
    状态流转：
    - pending → approved（通过，可供搜索使用）
    - pending → revise（打回待修改，提交人需修改后重新提交）
    - pending → rejected（废弃，不再显示）
    - revise → approved（待修改后重新提交，通过审核）
    - revise → rejected（废弃）
    """
    user = _get_current_user()
    if not user or user.role != 'super_admin':
        return jsonify({'error': '只有超级管理员可以审核 Skill'}), 403

    skill = Skill.query.get_or_404(skill_id)
    data = request.get_json()
    status = (data or {}).get('review_status', '')
    comment = (data or {}).get('review_comment', '')
    
    if status not in ('approved', 'revise', 'rejected'):
        return jsonify({'error': 'review_status 必须为 approved、revise 或 rejected'}), 400

    # 校验状态流转合法性
    if skill.review_status == 'approved' and status != 'approved':
        return jsonify({'error': '已通过的 Skill 不能再打回或废弃，如需修改请联系提交人'}), 400
    if skill.review_status == 'rejected':
        return jsonify({'error': '已废弃的 Skill 不能再审核'}), 400

    old_status = skill.review_status
    skill.review_status = status
    if comment:
        skill.review_comment = comment
    elif status in ('revise', 'rejected'):
        skill.review_comment = comment  # 可以为空但记录
    
    status_labels = {'approved': '通过', 'revise': '打回待修改', 'rejected': '废弃'}
    action = status_labels.get(status, status)

    # 审核完成后，自动关闭龙虾王的相关审核待办
    from app.models import ClawTodo, ClawTodoLog
    review_todos = ClawTodo.query.filter(
        ClawTodo.title.like(f'%审核 Skill「{skill.display_name}」%'),
        ClawTodo.task_category == 'review',
        ClawTodo.enabled == True,
    ).all()
    from datetime import date as _date
    _today = _date.today()
    for todo in review_todos:
        todo.enabled = False  # 关闭待办
        log = ClawTodoLog.query.filter_by(
            todo_id=todo.id, log_date=_today
        ).first()
        if log and log.status == 'pending':
            log.status = 'approved' if status == 'approved' else 'rejected'
            log.result_summary = f'Skill「{skill.display_name}」已{action}'
            from app.models import _now as _model_now
            log.completed_at = _model_now()

    db.session.commit()

    notified_ids = _notify_admin_claws('Skill', f'审核{action}', skill.display_name,
                        f'{old_status} → {status}, 操作人: {user.username}' + (f'\n审核意见: {comment}' if comment else ''))
    
    # 如果打回待修改，通知提交人
    if status == 'revise':
        _notify_submitter_review_result(skill.created_by, 'Skill', skill.display_name, action, comment)
    
    db.session.commit()
    from app.api.agent_client import notify_claw
    for cid in notified_ids:
        notify_claw(cid)

    return jsonify({'message': f'Skill 已{action}', 'review_status': status, 'review_comment': comment})


@api_bp.route('/skills/<int:skill_id>/rating', methods=['POST'])
def update_skill_rating(skill_id):
    """修改 Skill 星级评分 — 仅超级管理员可修改

    请求体：{"rating": 4.5}  范围 1.0~5.0，步进 0.5
    """
    user = _get_current_user()
    if not user or user.role not in ('super_admin', 'admin'):
        return jsonify({'error': '只有管理员可以修改星级评分'}), 403

    skill = Skill.query.get_or_404(skill_id)
    data = request.get_json()
    if not data or 'rating' not in data:
        return jsonify({'error': 'rating 为必填项'}), 400

    rating = data['rating']
    # 验证范围和步进
    try:
        rating = float(rating)
    except (TypeError, ValueError):
        return jsonify({'error': 'rating 必须为数字'}), 400

    if rating < 1.0 or rating > 5.0:
        return jsonify({'error': 'rating 范围为 1.0~5.0'}), 400
    # 步进 0.5
    rating = round(rating * 2) / 2

    skill.rating = rating
    db.session.commit()
    return jsonify({'message': '评分已更新', 'rating': rating})


@api_bp.route('/skills/<int:skill_id>', methods=['PUT', 'PATCH'])
def update_skill(skill_id):
    """更新 Skill（非管理员修改后自动重置为待评审状态，通知龙虾王审核）"""
    skill = Skill.query.get_or_404(skill_id)

    # 已软删除的 Skill 不可编辑
    if skill.is_deleted:
        return jsonify({'error': '此 Skill 已被删除，无法编辑'}), 403

    # 已废弃的 Skill 不可编辑
    if skill.review_status == 'rejected':
        return jsonify({'error': '已废弃的 Skill 不可编辑'}), 403

    user = _get_current_user()
    if not _can_edit(user, skill):
        import logging
        logging.warning(f'[SKILL AUTH] 403 denied: user={user}, user.role={getattr(user,"role",None)}, user.username={getattr(user,"username",None)}, skill={skill_id}, created_by={skill.created_by}')
        return jsonify({'error': '无权修改此 Skill，只有超级管理员、管理员或提交人可编辑'}), 403

    data = request.get_json()
    old_review_status = skill.review_status

    for field in ['display_name', 'description', 'trigger_phrase',
                  'template_content', 'category', 'scope',
                  'applicable_projects', 'applicable_modules']:
        if field in data:
            setattr(skill, field, data[field])

    if 'is_standard' in data:
        skill.is_standard = bool(data['is_standard'])

    # review_status 只有 admin 以上可改（且只能通过专用审核接口修改）
    if 'review_status' in data:
        return jsonify({'error': '请使用 POST /skills/<id>/review 接口修改审核状态'}), 403

    # 非管理员编辑 Skill 后，自动重置为待评审状态，通知龙虾王审核
    # 管理员编辑不重置状态（管理员直接审核通过）
    if user and user.role != 'super_admin' and old_review_status != 'pending':
        skill.review_status = 'pending'
        skill.review_comment = None  # 重新提交时清除之前的审核意见
        # 通知龙虾王有待审核的 Skill
        notified_ids = _notify_admin_claws('Skill', '待审核（修改后重新提交）', skill.display_name,
                            f'类型: {skill.category}, 作用域: {skill.scope}, 提交人: {skill.created_by}\n请审核后通过或拒绝。')
        _create_review_todo_for_admin_claws('Skill', skill.display_name, skill.created_by, skill.category, skill.scope)
    else:
        notified_ids = _notify_admin_claws('Skill', '更新', skill.display_name,
                            f'更新字段: {", ".join(data.keys())}')

    db.session.commit()
    from app.api.agent_client import notify_claw
    for cid in notified_ids:
        notify_claw(cid)

    return jsonify(skill.to_dict())


@api_bp.route('/skills/<int:skill_id>', methods=['DELETE'])
def delete_skill(skill_id):
    """软删除 Skill（隐藏，OpenClaw 搜索安装时不可见）

    权限规则：
    - super_admin：可删除任何 Skill
    - admin：只能删除自己项目创建的 Skill
    - user：只能删除自己创建的 Skill
    """
    skill = Skill.query.get_or_404(skill_id)

    user = _get_current_user()
    if not user:
        return jsonify({'error': '请先登录'}), 403

    # 已软删除的不能重复删除
    if skill.is_deleted:
        return jsonify({'error': '该 Skill 已被删除'}), 400

    # 权限检查
    can_delete = False
    if user.role in ('super_admin', 'admin'):
        can_delete = True
    else:
        # 普通用户只能删除自己创建的
        created_by = skill.created_by or ''
        if created_by == user.username:
            can_delete = True
        elif created_by == getattr(user, '_claw_name', None):
            can_delete = True

    if not can_delete:
        return jsonify({'error': '无权删除此 Skill，只能删除自己创建的'}), 403

    # 软删除：标记 is_deleted=True，禁用安装关联
    skill.is_deleted = True
    skill.deleted_at = datetime.now()

    # 禁用所有安装关联（OpenClaw 不再能看到此 Skill）
    OpenClawSkill.query.filter_by(skill_id=skill_id).update({'enabled': False})

    notified_ids = _notify_admin_claws('Skill', '删除（隐藏）', skill.display_name,
                        f'由 {user.username} 软删除，OpenClaw 将无法搜索安装')
    db.session.commit()
    from app.api.agent_client import notify_claw
    for cid in notified_ids:
        notify_claw(cid)

    return jsonify({'message': f'Skill "{skill.name}" 已删除（隐藏），OpenClaw 将无法搜索安装'})


def _notify_claw_sync(claw_id, action, detail):
    """给 OpenClaw 发送配置同步通知，并立即触发 SSE 推送"""
    from app.models import ClawMessage
    from app.api.agent_client import notify_claw
    msg = ClawMessage(
        claw_id=claw_id,
        sender_name='Hub',
        content=f'[{action}] {detail}，请同步配置',
        msg_type='sync_config',
        direction='to_claw',
        status='pending',
    )
    db.session.add(msg)
    # 注意：notify_claw 需要在 db.session.commit() 之后调用才能确保数据可见
    # 这里只记录 claw_id，调用方在 commit 后负责调用 notify_claw
    return claw_id


def _notify_admin_claws(resource_type, action, resource_name, detail=None):
    """通知所有 admin 角色的 OpenClaw（龙虾王）资源变更

    龙虾王收到后自行判断是否需要下发给其他 claw。
    返回需要 SSE 通知的 claw_id 列表。
    """
    from app.models import ClawMessage
    admin_claws = OpenClawInstance.query.filter(
        OpenClawInstance.role == 'admin',
        OpenClawInstance.status != 'deleted',
    ).all()

    content = f'[{resource_type}变更] {action}「{resource_name}」'
    if detail:
        content += f'\n{detail}'
    content += '\n请评估是否需要下发给相关 OpenClaw。'

    notified_ids = []
    for claw in admin_claws:
        msg = ClawMessage(
            claw_id=claw.id,
            sender_name='Hub',
            content=content,
            msg_type='sync_config',
            direction='to_claw',
            status='pending',
        )
        db.session.add(msg)
        notified_ids.append(claw.id)

    return notified_ids


def _notify_submitter_review_result(created_by, resource_type, resource_name, action, comment=None):
    """通知提交人审核结果（打回待修改/废弃时发送消息给提交人对应的 OpenClaw）"""
    from app.models import ClawMessage
    # 根据 created_by 查找对应的 OpenClaw
    claw = OpenClawInstance.query.filter(
        OpenClawInstance.name == created_by,
        OpenClawInstance.status != 'deleted',
    ).first()
    if not claw:
        return
    content = f'[{resource_type}审核结果] 「{resource_name}」{action}'
    if comment:
        content += f'\n审核意见: {comment}'
    if action == '打回待修改':
        content += '\n请修改后重新提交，修改后将自动重新进入待评审状态。'
    msg = ClawMessage(
        claw_id=claw.id,
        sender_name='Hub',
        content=content,
        msg_type='sync_config',
        direction='to_claw',
        status='pending',
    )
    db.session.add(msg)


def _create_review_todo_for_admin_claws(resource_type, resource_name, created_by, category, scope):
    """给所有 admin 角色的 OpenClaw 创建审核待办任务"""
    from app.models import ClawTodo
    admin_claws = OpenClawInstance.query.filter(
        OpenClawInstance.role == 'admin',
        OpenClawInstance.status != 'deleted',
    ).all()
    for claw in admin_claws:
        review_todo = ClawTodo(
            openclaw_id=claw.id,
            title=f'审核 {resource_type}「{resource_name}」',
            description=f'提交人: {created_by}\n类型: {category}, 作用域: {scope}\n请前往 {resource_type} 市场审核后通过或拒绝。',
            schedule_type='once',
            urgency_level='flexible',
            priority='P1',
            task_category='review',
            enabled=True,
            created_by='Hub',
        )
        db.session.add(review_todo)


@api_bp.route('/openclaws/<int:claw_id>/skills', methods=['POST'])
def install_skill(claw_id):
    """为 OpenClaw 安装 Skill（仅已审核通过的 Skill 可安装）

    1. 写入 OpenClawSkill 关联记录
    2. 下发待办任务（interrupt 级别），OpenClaw 心跳时感知并拉取安装
    """
    claw = OpenClawInstance.query.get_or_404(claw_id)
    data = request.get_json()
    skill_id = data.get('skill_id')

    if not skill_id:
        return jsonify({'error': 'skill_id 为必填项'}), 400

    skill = Skill.query.get_or_404(skill_id)

    # 已软删除的 Skill 不能安装
    if skill.is_deleted:
        return jsonify({'error': '此 Skill 已被删除，无法安装'}), 403

    # 只有已审核通过的 skill 才能安装（管理员跳过此限制）
    user = _get_current_user()
    if skill.review_status != 'approved':
        if not user or user.role not in ('super_admin', 'admin'):
            return jsonify({'error': '此 Skill 尚未通过审核，无法安装'}), 403

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

    # 检测 skill 是否带一键安装脚本（install.sh / setup.sh）
    # —— 主要给 sidecar / 守护进程类 skill 用，普通文档 skill 不会有
    has_install_sh = False
    install_sh_name = None
    if skill.files:
        for f in skill.files:
            if isinstance(f, dict):
                fname = f.get('name', '')
                if fname.endswith('install.sh') or fname.endswith('setup.sh'):
                    has_install_sh = True
                    install_sh_name = fname
                    break

    # 拼描述：含 install.sh 的 skill 优先给一键命令
    # 优先用环境变量 OPENCLAW_PUBLIC_URL；没配则用固定外网地址（不能用 request.host_url，
    # 因为 curl 从内网 localhost 调时会拿到 localhost:8088，发给 claw 没法用）
    import os as _os
    hub_url_hint = (
        _os.environ.get('OPENCLAW_PUBLIC_URL', '').rstrip('/')
        or 'http://9.134.11.169:8088'
    )
    desc_lines = [
        f'Hub 已分配 Skill「{skill.display_name}」(id={skill.id})，请拉取并安装到本地。',
        '',
    ]
    if has_install_sh:
        desc_lines.extend([
            f'⚡ 本 skill 含一键安装脚本 `{install_sh_name}`，推荐路径（一行搞定）：',
            '',
            '```bash',
            f'curl -fsSL {hub_url_hint}/static/skills/{skill.name}/{install_sh_name} \\',
            f'  | CLAW_ID={claw_id} API_TOKEN=<你的 Hub API token> bash',
            '```',
            '',
            '或者照标准流程：把 skill 拉到 ~/.qclaw/skills/ 后再执行：',
            '',
            '```bash',
            f'CLAW_ID={claw_id} API_TOKEN=<你的 token> \\',
            f'  bash ~/.qclaw/skills/{skill.name}/{install_sh_name}',
            '```',
            '',
            '标准拉取流程：',
        ])
    else:
        desc_lines.append('执行步骤：')

    desc_lines.extend([
        f'1. GET /api/v1/openclaws/{claw_id}/assigned-skills 获取最新 Skills 列表',
        f'2. 找到 name="{skill.name}" 的 Skill',
        f'3. GET /api/v1/skills/{skill.id}/files 获取文档包文件清单',
        f'4. 逐个拉取文件写入 ~/.qclaw/skills/{skill.name}/',
        f'5. 完成后上报 POST /todos/{{todo_id}}/complete',
    ])

    from app.models import ClawTodo
    todo = ClawTodo(
        openclaw_id=claw_id,
        title=f'安装 Skill：{skill.display_name}',
        description='\n'.join(desc_lines),
        schedule_type='once',
        urgency_level='interrupt',
        priority='P0',
        task_category='routine',
        verification_target=f'install-skill:{skill.name}',
        enabled=True,
        created_by='hub',
    )
    db.session.add(todo)

    _notify_claw_sync(claw_id, '分配 Skill', skill.display_name)
    db.session.commit()
    from app.api.agent_client import notify_claw
    notify_claw(claw_id)
    return jsonify({'message': f'已为 {claw.name} 分配 {skill.display_name}'}), 201


@api_bp.route('/openclaws/<int:claw_id>/skills/<int:skill_id>',
              methods=['DELETE'])
def uninstall_skill(claw_id, skill_id):
    """移除 Skill"""
    link = OpenClawSkill.query.filter_by(
        openclaw_id=claw_id, skill_id=skill_id
    ).first_or_404()

    skill_name = link.skill.display_name if link.skill else str(skill_id)
    skill_ident = link.skill.name if link.skill else str(skill_id)
    link.enabled = False

    # 下发卸载待办
    from app.models import ClawTodo
    todo = ClawTodo(
        openclaw_id=claw_id,
        title=f'卸载 Skill：{skill_name}',
        description=f'Hub 已移除 Skill「{skill_name}」，请从本地删除 ~/.qclaw/skills/{skill_ident}/ 目录。',
        schedule_type='once',
        urgency_level='flexible',
        priority='P1',
        task_category='routine',
        verification_target=f'uninstall-skill:{skill_ident}',
        enabled=True,
        created_by='hub',
    )
    db.session.add(todo)

    _notify_claw_sync(claw_id, '移除 Skill', skill_name)
    db.session.commit()
    from app.api.agent_client import notify_claw
    notify_claw(claw_id)
    return jsonify({'message': '已移除'})


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


# ==================== Skill/Rule 原始内容链接 ====================

from flask import Response

@api_bp.route('/skills/<int:skill_id>/raw', methods=['GET'])
def get_skill_raw(skill_id):
    """获取 Skill 原始内容（Markdown），可通过链接直接拉取

    用法：curl http://hub:8088/api/v1/skills/3/raw
    不需要登录，公开访问（方便 OpenClaw 拉取）
    """
    skill = Skill.query.get_or_404(skill_id)
    content = skill.template_content or ''
    md = f"# {skill.display_name}\n\n"
    if skill.description:
        md += f"> {skill.description}\n\n"
    if skill.trigger_phrase:
        md += f"**触发短语**：{skill.trigger_phrase}\n\n---\n\n"
    md += content
    return Response(md, mimetype='text/markdown; charset=utf-8',
                    headers={'Content-Disposition': f'inline; filename="skill-{skill.id}.md"'})


@api_bp.route('/rules/<int:rule_id>/raw', methods=['GET'])
def get_rule_raw(rule_id):
    """获取 Rule 原始内容（Markdown）"""
    from app.models import Rule
    rule = Rule.query.get_or_404(rule_id)
    content = rule.content_template or ''
    md = f"# {rule.display_name}\n\n"
    if rule.description:
        md += f"> {rule.description}\n\n---\n\n"
    md += content
    return Response(md, mimetype='text/markdown; charset=utf-8',
                    headers={'Content-Disposition': f'inline; filename="rule-{rule.id}.md"'})


# ==================== Skill 文档包 API（MySQL 存储） ====================

from app.models import SkillFile
import os

HUB_STORE_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'hub-store')


def _sync_to_disk(skill, filename, content):
    """同步写磁盘缓存（可选）"""
    try:
        pack_dir = os.path.join(HUB_STORE_ROOT, 'skills', skill.name)
        os.makedirs(pack_dir, exist_ok=True)
        with open(os.path.join(pack_dir, filename), 'w', encoding='utf-8') as f:
            f.write(content or '')
    except Exception:
        pass


def _remove_from_disk(skill, filename):
    """从磁盘缓存删除"""
    try:
        fpath = os.path.join(HUB_STORE_ROOT, 'skills', skill.name, filename)
        if os.path.isfile(fpath):
            os.remove(fpath)
    except Exception:
        pass


@api_bp.route('/skills/<int:skill_id>/files', methods=['GET'])
def list_skill_files(skill_id):
    """列出 Skill 文档包所有文件"""
    skill = Skill.query.get_or_404(skill_id)
    files = SkillFile.query.filter_by(skill_id=skill_id).order_by(SkillFile.filename).all()
    return jsonify({
        'skill_id': skill_id,
        'skill_name': skill.name,
        'files': [f.to_dict() for f in files],
    })


@api_bp.route('/skills/<int:skill_id>/files/<path:filename>', methods=['GET'])
def get_skill_file(skill_id, filename):
    """获取文档包某个文件内容"""
    Skill.query.get_or_404(skill_id)
    sf = SkillFile.query.filter_by(skill_id=skill_id, filename=filename).first()
    if not sf:
        return jsonify({'error': f'文件 {filename} 不存在'}), 404

    fmt = request.args.get('format', 'raw')
    if fmt == 'json':
        return jsonify(sf.to_dict())

    return Response(sf.content or '', mimetype='text/markdown; charset=utf-8',
                    headers={'Content-Disposition': f'inline; filename="{filename}"'})


@api_bp.route('/skills/<int:skill_id>/files/<path:filename>', methods=['PUT'])
def save_skill_file(skill_id, filename):
    """保存/更新文档包文件（存 MySQL + 同步磁盘缓存）"""
    skill = Skill.query.get_or_404(skill_id)
    user = _get_current_user()
    if not _can_edit(user, skill):
        return jsonify({'error': '无权修改此 Skill 的文件'}), 403
    data = request.get_json()
    content = data.get('content', '')
    description = data.get('description')

    sf = SkillFile.query.filter_by(skill_id=skill_id, filename=filename).first()
    if sf:
        sf.content = content
        if description is not None:
            sf.description = description
    else:
        sf = SkillFile(skill_id=skill_id, filename=filename, content=content,
                       description=description,
                       file_type='markdown' if filename.endswith('.md') else 'text')
        db.session.add(sf)

    # SKILL.md 同步到 template_content
    if filename == 'SKILL.md':
        skill.template_content = content
        skill.pack_path = f'hub-store/skills/{skill.name}'

    # 更新文件清单
    db.session.flush()
    all_files = SkillFile.query.filter_by(skill_id=skill_id).all()
    skill.files = [{'name': f.filename, 'size': len(f.content or '')} for f in all_files]
    db.session.commit()

    # 同步磁盘缓存
    _sync_to_disk(skill, filename, content)

    return jsonify({'message': f'{filename} 已保存', 'file': sf.to_dict()})


@api_bp.route('/skills/<int:skill_id>/files/<path:filename>', methods=['DELETE'])
def delete_skill_file(skill_id, filename):
    """删除文档包文件"""
    skill = Skill.query.get_or_404(skill_id)
    user = _get_current_user()
    if not _can_edit(user, skill):
        return jsonify({'error': '无权删除此 Skill 的文件'}), 403

    if filename == 'SKILL.md':
        return jsonify({'error': '不能删除主文件 SKILL.md'}), 400

    sf = SkillFile.query.filter_by(skill_id=skill_id, filename=filename).first()
    if sf:
        db.session.delete(sf)
        # 更新清单
        remaining = SkillFile.query.filter_by(skill_id=skill_id).filter(
            SkillFile.filename != filename).all()
        skill.files = [{'name': f.filename, 'size': len(f.content or '')} for f in remaining]
        db.session.commit()
        _remove_from_disk(skill, filename)

    return jsonify({'message': f'{filename} 已删除'})


@api_bp.route('/skills/<int:skill_id>/pack', methods=['GET'])
def download_skill_pack(skill_id):
    """打包下载整个文档包（ZIP，从 MySQL 读取）"""
    import zipfile, io
    skill = Skill.query.get_or_404(skill_id)
    files = SkillFile.query.filter_by(skill_id=skill_id).all()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.writestr(f'{skill.name}/{f.filename}', f.content or '')

    buf.seek(0)
    return Response(buf.getvalue(),
                    mimetype='application/zip',
                    headers={'Content-Disposition': f'attachment; filename="{skill.name}.zip"'})
