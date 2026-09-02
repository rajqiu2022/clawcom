import os
import uuid
from datetime import datetime
from urllib.parse import quote
from flask import (Response, request, jsonify, current_app,
                   send_from_directory)
from app import db
from app.models import (KnowledgeEntry, KnowledgeDistribution,
                        KnowledgeFavorite, OpenClawInstance, Topic)
from app.api import api_bp
from app.services.knowledge_sharing import (
    can_manage_knowledge_share,
    knowledge_favorite_owner,
    knowledge_markdown,
    knowledge_markdown_filename,
    public_knowledge_payload,
)


def _get_current_user():
    """获取当前用户（支持 Web session 和 OpenClaw Bearer Token）
    注意：knowledge 模块中 admin claw 映射为 super_admin
    """
    from app.api.auth_utils import get_current_user_as_super_admin
    return get_current_user_as_super_admin()


def _get_current_openclaw():
    """从 Bearer Token 反推当前 OpenClaw 实例（不要信任 body 里的 source_openclaw_id）。

    返回 OpenClawInstance 或 None。Web session 用户调用时返回 None。
    """
    from app.api.auth_utils import get_current_claw
    return get_current_claw()


def _favorite_owner():
    return knowledge_favorite_owner(
        _get_current_user(),
        _get_current_openclaw(),
    )


def _favorite_filter(owner):
    if owner['claw_id']:
        return KnowledgeFavorite.claw_id == owner['claw_id']
    return KnowledgeFavorite.user_id == owner['user_id']


def _favorite_knowledge_ids(owner, entry_ids=None):
    query = KnowledgeFavorite.query.filter(_favorite_filter(owner))
    if entry_ids is not None:
        ids = [int(item) for item in entry_ids]
        if not ids:
            return set()
        query = query.filter(KnowledgeFavorite.knowledge_id.in_(ids))
    return {row.knowledge_id for row in query.all()}


def _entry_payload(entry, favorite_ids=None):
    payload = entry.to_dict()
    if favorite_ids is None:
        try:
            favorite_ids = _favorite_knowledge_ids(
                _favorite_owner(), [entry.id])
        except ValueError:
            favorite_ids = set()
    payload['is_favorite'] = entry.id in favorite_ids
    payload['can_manage_share'] = _may_manage_share(entry)
    if payload['can_manage_share'] and entry.is_shared and entry.share_token:
        payload['share_token'] = entry.share_token
        payload['share_url'] = f'/k/{entry.share_token}'
    return payload


def _journal_entry_visible(entry):
    if getattr(entry, 'entry_type', 'article') != 'test_journal':
        return True
    from app.api.knowledge_notebooks import _actor, _can_access_project
    project_id = entry.project_id or (
        entry.notebook.project_id if entry.notebook else None)
    return _can_access_project(_actor(), project_id)


def _owned_claw_ids(user, claw):
    if claw is not None and getattr(claw, 'id', None):
        return {int(claw.id)}
    username = str(getattr(user, 'username', '') or '').strip()
    if not username:
        return set()
    rows = (OpenClawInstance.query
            .filter(OpenClawInstance.owner == username,
                    OpenClawInstance.status != 'deleted')
            .with_entities(OpenClawInstance.id)
            .all())
    return {int(row[0]) for row in rows}


def _may_manage_share(entry):
    user = _get_current_user()
    claw = _get_current_openclaw()
    return can_manage_knowledge_share(
        entry,
        user=user,
        claw=claw,
        owned_claw_ids=_owned_claw_ids(user, claw),
    )


def _shared_entry(token):
    return KnowledgeEntry.query.filter_by(
        share_token=token,
        is_shared=True,
    ).first()


def _markdown_response(entry):
    filename = knowledge_markdown_filename(entry.title)
    response = Response(
        knowledge_markdown(entry),
        content_type='text/markdown; charset=utf-8',
    )
    response.headers['Content-Disposition'] = (
        'attachment; filename="knowledge.md"; '
        f"filename*=UTF-8''{quote(filename)}"
    )
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


# 「项目基本信息」特别标识：每个项目接入 Hub 的必备身份卡（TAPD/前后端仓库/分支/协作平台）。
# 用保留 category 作为标识，无需 schema 迁移；拉列表 group_profile=1 时单独返回。
PROJECT_PROFILE_CATEGORY = 'project_profile'


UPLOAD_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                          'static', 'uploads')


@api_bp.route('/upload/image', methods=['POST'])
def upload_image():
    """上传图片，返回 Markdown 可用的 URL"""
    if 'image' not in request.files:
        return jsonify({'error': '未选择文件'}), 400

    f = request.files['image']
    if not f.filename:
        return jsonify({'error': '文件名为空'}), 400

    # 校验文件类型
    allowed = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.svg'}
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in allowed:
        return jsonify({'error': f'不支持的格式: {ext}'}), 400

    # 限制 10MB
    f.seek(0, 2)
    size = f.tell()
    f.seek(0)
    if size > 10 * 1024 * 1024:
        return jsonify({'error': '图片不能超过 10MB'}), 400

    os.makedirs(UPLOAD_DIR, exist_ok=True)

    # 生成唯一文件名
    filename = f'{uuid.uuid4().hex[:12]}{ext}'
    filepath = os.path.join(UPLOAD_DIR, filename)
    f.save(filepath)

    url = f'/static/uploads/{filename}'
    return jsonify({
        'url': url,
        'filename': filename,
        # Vditor 需要的格式
        'data': {'url': url},
    })


@api_bp.route('/knowledge', methods=['GET'])
def list_knowledge():
    """查询知识（支持 scope/category/project/source_type 筛选）"""
    # 版本纪要在独立 Wiki 工作台展示，避免混入普通知识卡片和审核流程。
    query = KnowledgeEntry.query.filter(
        db.or_(KnowledgeEntry.entry_type == 'article',
               KnowledgeEntry.entry_type.is_(None)))

    scope = request.args.get('scope')
    category = request.args.get('category')
    project = request.args.get('project')
    module = request.args.get('module')
    status = request.args.get('status')
    source_type = request.args.get('source_type')
    favorite_only = request.args.get('favorite') in (
        '1', 'true', 'True', 'yes')

    try:
        owner = _favorite_owner()
    except ValueError:
        owner = None

    if favorite_only:
        if owner is None:
            return jsonify({'error': '收藏筛选需要登录身份'}), 401
        query = query.join(
            KnowledgeFavorite,
            KnowledgeFavorite.knowledge_id == KnowledgeEntry.id,
        ).filter(_favorite_filter(owner))

    if scope:
        query = query.filter(KnowledgeEntry.scope == scope)
    if category:
        query = query.filter(KnowledgeEntry.category == category)
    if project:
        query = query.filter(KnowledgeEntry.project_name == project)
    if module:
        query = query.filter(KnowledgeEntry.module_name == module)
    if status:
        query = query.filter(KnowledgeEntry.status == status)
    if source_type:
        query = query.filter(KnowledgeEntry.source_type == source_type)

    # 全文搜索
    search = request.args.get('search')
    if search:
        search_term = f'%{search}%'
        query = query.filter(
            db.or_(
                KnowledgeEntry.title.like(search_term),
                KnowledgeEntry.content.like(search_term),
            )
        )

    entries = query.order_by(
        KnowledgeEntry.created_at.desc()
    ).limit(200).all()
    favorite_ids = (
        _favorite_knowledge_ids(owner, [entry.id for entry in entries])
        if owner else set()
    )

    # 「项目基本信息」特别标识（category=project_profile）：
    # 当 group_profile=1（Agent 拉知识库列表推荐带上）时，把当前项目的基本信息条目
    # 单独返回，其余走汇总列表。缺失该条目 = 该项目尚未完成接入的必备第一步。
    group_profile = request.args.get('group_profile') in ('1', 'true', 'True', 'yes')
    if group_profile:
        target_project = project
        if not target_project:
            caller_claw = _get_current_openclaw()
            if caller_claw and getattr(caller_claw, 'project_name', None):
                target_project = caller_claw.project_name
        profile = None
        if target_project:
            profile = KnowledgeEntry.query.filter(
                KnowledgeEntry.category == PROJECT_PROFILE_CATEGORY,
                KnowledgeEntry.project_name == target_project,
                KnowledgeEntry.status == 'approved',
            ).order_by(KnowledgeEntry.updated_at.desc()).first()
        if profile and favorite_only and profile.id not in favorite_ids:
            profile = None
        profile_id = profile.id if profile else None
        others = [
            _entry_payload(e, favorite_ids)
            for e in entries if e.id != profile_id
        ]
        return jsonify({
            'project_profile': (
                _entry_payload(profile, favorite_ids) if profile else None
            ),
            'has_project_profile': profile is not None,
            'target_project': target_project,
            'entries': others,
            'count': len(others),
        })

    return jsonify([_entry_payload(e, favorite_ids) for e in entries])


@api_bp.route('/knowledge', methods=['POST'])
def create_knowledge():
    """创建知识条目"""
    data = request.get_json()
    if not data or not data.get('title') or not data.get('content'):
        return jsonify({'error': '标题和内容为必填项'}), 400

    # 从 Bearer Token 反推 OpenClaw，强制覆盖 source_openclaw_id / source_type
    # 防止 Agent 因不知道自己 ID 而 hardcode 错的数字（历史曾因此出现"未知 Agent"）
    caller_claw = _get_current_openclaw()
    if caller_claw:
        source_openclaw_id = caller_claw.id
        source_type = 'openclaw'
    else:
        source_openclaw_id = data.get('source_openclaw_id')
        source_type = data.get('source_type', 'manual')

    user = _get_current_user()
    # 超级管理员创建直接 approved，其他人 pending_review
    if user and user.role == 'super_admin':
        default_status = 'approved'
    else:
        default_status = 'pending_review'

    entry = KnowledgeEntry(
        memos_id=data.get('memos_id'),
        title=data['title'],
        content=data['content'],
        category=data.get('category', 'general'),
        scope=data.get('scope', 'global'),
        project_name=data.get('project_name'),
        module_name=data.get('module_name'),
        source_openclaw_id=source_openclaw_id,
        source_type=source_type,
        status=data.get('status', default_status),
        created_by=(getattr(user, 'username', None) or (caller_claw.name if caller_claw else 'system')),
    )
    db.session.add(entry)

    # 通知 admin 角色 OpenClaw（龙虾王）有新知识库待审核
    created_by = getattr(user, 'username', None) or (caller_claw.name if caller_claw else '未知')
    if entry.status == 'pending_review':
        from app.api.skills import _notify_admin_claws, _create_review_todo_for_admin_claws
        notified_ids = _notify_admin_claws('知识库', '待审核', entry.title,
                            f'分类: {entry.category}, 作用域: {entry.scope}, 提交人: {created_by}\n请前往知识库市场审核后通过或拒绝。')
        _create_review_todo_for_admin_claws('知识库', entry.title, created_by,
                            entry.category, entry.scope)
    else:
        notified_ids = []

    db.session.commit()

    # SSE 即时推送
    if notified_ids:
        from app.api.agent_client import notify_claw
        for cid in notified_ids:
            notify_claw(cid)

    return jsonify(entry.to_dict()), 201


@api_bp.route('/knowledge/batch-import', methods=['POST'])
def batch_import_knowledge():
    """批量导入知识条目（来自 OpenSpace 或 Agent 上报）"""
    data = request.get_json()
    entries = data.get('entries', [])
    source_type = data.get('source_type', 'openspace')

    if not entries:
        return jsonify({'error': 'entries 数组不能为空'}), 400

    if source_type not in ('openclaw', 'openspace'):
        return jsonify({'error': 'source_type 必须是 openclaw 或 openspace'}), 400

    # 同 create_knowledge：Bearer Token → 强制覆盖 source_openclaw_id / source_type
    caller_claw = _get_current_openclaw()

    created = 0
    for e_data in entries:
        if not e_data.get('title') or not e_data.get('content'):
            continue

        if caller_claw:
            entry_source_id = caller_claw.id
            entry_source_type = 'openclaw'
        else:
            entry_source_id = e_data.get('source_openclaw_id')
            entry_source_type = source_type

        entry = KnowledgeEntry(
            memos_id=e_data.get('memos_id'),
            title=e_data['title'],
            content=e_data['content'],
            category=e_data.get('category', 'general'),
            scope=e_data.get('scope', 'global'),
            project_name=e_data.get('project_name'),
            module_name=e_data.get('module_name'),
            source_openclaw_id=entry_source_id,
            source_type=entry_source_type,
            status=e_data.get('status', 'pending_review'),
            created_by=(getattr(user, 'username', None) or (caller_claw.name if caller_claw else 'system')),
        )
        db.session.add(entry)
        created += 1

    # 通知 admin 角色 OpenClaw（龙虾王）有批量导入的知识待审核
    user = _get_current_user()
    batch_submitter = getattr(user, 'username', None) or (caller_claw.name if caller_claw else '未知')
    if created > 0:
        from app.api.skills import _notify_admin_claws, _create_review_todo_for_admin_claws
        notified_ids = _notify_admin_claws('知识库', f'批量导入 {created} 条待审核', f'来自 {batch_submitter}',
                            f'提交人: {batch_submitter}\n请前往知识库市场审核后通过或拒绝。')
        _create_review_todo_for_admin_claws('知识库', f'批量导入 {created} 条',
                            batch_submitter, 'batch', 'global')
    else:
        notified_ids = []

    db.session.commit()

    # SSE 即时推送
    if notified_ids:
        from app.api.agent_client import notify_claw
        for cid in notified_ids:
            notify_claw(cid)

    return jsonify({
        'message': f'导入 {created} 条知识（待审核）',
        'created': created,
    })


@api_bp.route('/knowledge/<int:entry_id>', methods=['GET'])
def get_knowledge(entry_id):
    """获取单条知识详情"""
    entry = db.get_or_404(KnowledgeEntry, entry_id)
    if not _journal_entry_visible(entry):
        return jsonify({'error': '知识条目不存在'}), 404
    return jsonify(_entry_payload(entry))


@api_bp.route('/knowledge/shared/<token>', methods=['GET'])
def get_shared_knowledge(token):
    """匿名读取显式分享的知识条目。"""
    entry = _shared_entry(token)
    if entry is None:
        return jsonify({'error': '分享链接不存在或已失效'}), 404
    return jsonify(public_knowledge_payload(entry))


@api_bp.route('/knowledge/shared/<token>/export.md', methods=['GET'])
def export_shared_knowledge(token):
    """匿名下载显式分享知识的 Markdown 文件。"""
    entry = _shared_entry(token)
    if entry is None:
        return jsonify({'error': '分享链接不存在或已失效'}), 404
    return _markdown_response(entry)


@api_bp.route('/knowledge/<int:entry_id>/export.md', methods=['GET'])
def export_knowledge(entry_id):
    """下载单条知识的 Markdown 文件。"""
    entry = db.get_or_404(KnowledgeEntry, entry_id)
    if not _journal_entry_visible(entry):
        return jsonify({'error': '知识条目不存在'}), 404
    return _markdown_response(entry)


@api_bp.route('/knowledge/<int:entry_id>/favorite', methods=['POST'])
def favorite_knowledge(entry_id):
    """收藏知识；重复调用保持幂等。"""
    entry = db.get_or_404(KnowledgeEntry, entry_id)
    if not _journal_entry_visible(entry):
        return jsonify({'error': '知识条目不存在'}), 404
    try:
        owner = _favorite_owner()
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 401
    favorite = KnowledgeFavorite.query.filter_by(
        knowledge_id=entry_id,
        **owner,
    ).first()
    if favorite is not None:
        return jsonify({
            'message': '已收藏',
            'knowledge_id': entry_id,
            'is_favorite': True,
        })
    db.session.add(KnowledgeFavorite(
        knowledge_id=entry_id,
        **owner,
    ))
    db.session.commit()
    return jsonify({
        'message': '收藏成功',
        'knowledge_id': entry_id,
        'is_favorite': True,
    }), 201


@api_bp.route('/knowledge/<int:entry_id>/favorite', methods=['DELETE'])
def unfavorite_knowledge(entry_id):
    """取消当前用户或 Agent 对知识的收藏。"""
    db.get_or_404(KnowledgeEntry, entry_id)
    try:
        owner = _favorite_owner()
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 401
    KnowledgeFavorite.query.filter_by(
        knowledge_id=entry_id,
        **owner,
    ).delete(synchronize_session=False)
    db.session.commit()
    return jsonify({
        'message': '已取消收藏',
        'knowledge_id': entry_id,
        'is_favorite': False,
    })


@api_bp.route('/knowledge/<int:entry_id>/share', methods=['POST'])
def share_knowledge(entry_id):
    """启用或刷新匿名分享链接。"""
    entry = db.get_or_404(KnowledgeEntry, entry_id)
    if entry.entry_type == 'test_journal':
        return jsonify({
            'error': '版本测试纪要当前仅允许项目内协作，不生成匿名外链',
            'code': 'JOURNAL_EXTERNAL_SHARE_DISABLED',
        }), 409
    if not _may_manage_share(entry):
        return jsonify({'error': '仅创建者、Agent owner 或管理员可管理分享'}), 403
    refresh = request.args.get('refresh') in ('1', 'true', 'True', 'yes')
    token = entry.enable_share(refresh=refresh)
    db.session.commit()
    return jsonify({
        'message': '分享链接已刷新' if refresh else '分享链接已启用',
        'is_shared': True,
        'share_token': token,
        'share_url': f'/k/{token}',
        'export_url': f'/api/v1/knowledge/shared/{token}/export.md',
    })


@api_bp.route('/knowledge/<int:entry_id>/share', methods=['DELETE'])
def unshare_knowledge(entry_id):
    """撤销匿名分享并使旧 token 立即失效。"""
    entry = db.get_or_404(KnowledgeEntry, entry_id)
    if not _may_manage_share(entry):
        return jsonify({'error': '仅创建者、Agent owner 或管理员可管理分享'}), 403
    entry.revoke_share()
    db.session.commit()
    return jsonify({
        'message': '分享链接已撤销',
        'is_shared': False,
    })


@api_bp.route('/knowledge/<int:entry_id>', methods=['PUT'])
def update_knowledge(entry_id):
    """更新知识"""
    entry = KnowledgeEntry.query.get_or_404(entry_id)
    if not _journal_entry_visible(entry):
        return jsonify({'error': '知识条目不存在'}), 404
    if entry.entry_type == 'test_journal':
        return jsonify({
            'error': '版本纪要必须通过 revisions API 保存，不能原地覆盖',
            'code': 'VERSIONED_KNOWLEDGE_REQUIRES_REVISION',
        }), 409
    data = request.get_json()

    for field in ['title', 'content', 'category', 'scope',
                  'project_name', 'module_name', 'status']:
        if field in data:
            setattr(entry, field, data[field])

    db.session.commit()
    return jsonify(entry.to_dict())


@api_bp.route('/knowledge/pending', methods=['GET'])
def pending_reviews():
    """获取待审核列表"""
    entries = KnowledgeEntry.query.filter_by(
        status='pending_review'
    ).order_by(KnowledgeEntry.created_at.desc()).all()
    return jsonify([e.to_dict() for e in entries])


@api_bp.route('/knowledge/<int:entry_id>/review', methods=['POST'])
def review_knowledge(entry_id):
    """审核知识（通过/打回整改/废弃）— 仅超级管理员（龙虾王 Token 认证映射为 super_admin）

    请求体（v2 统一字段约定，详见 review_comments.parse_review_action）：
    {
        "action":  "approve" | "revise" | "reject",     // 推荐
        "comment": "审核意见（revise/reject 时必填）"
    }
    兼容旧字段：review_status (approved/revise/rejected) + review_comment 或 notes。

    状态流转：
    - pending_review → approved (action=approve)
    - pending_review → revise   (action=revise，必填 comment) ← v2 新增"打回整改"
    - pending_review → rejected (action=reject，必填 comment)
    - revise         → pending_review（提交人 PUT 内容更新自动重置 + 写 submit 记录）
    """
    from app.api.review_comments import (
        add_review_comment, parse_review_action, STATUS_FROM_ACTION,
    )

    user = _get_current_user()
    if not user or user.role != 'super_admin':
        return jsonify({'error': '仅超级管理员可审核知识'}), 403

    entry = KnowledgeEntry.query.get_or_404(entry_id)
    data = request.get_json()
    action, comment, err = parse_review_action(data)
    if err:
        return jsonify({'error': err}), 400
    if action in ('revise', 'reject') and not comment:
        return jsonify({'error': f'{action} 操作必须填写 comment（审核意见）'}), 400

    old_status = entry.status or ''
    entry.reviewer_notes = comment  # 历史字段保留写入
    entry.approved_by = user.username

    # knowledge 状态值历史是 approved/rejected/revise；rev=knowledge 维持原命名
    if action == 'approve':
        entry.status = 'approved'
        entry.approved_at = datetime.now()
    elif action == 'revise':
        entry.status = 'revise'
    else:
        entry.status = 'rejected'
    new_status = entry.status

    # 通知 admin OpenClaw 审核结果 + 通知提交人
    from app.api.skills import _notify_admin_claws, _notify_submitter_review_result
    action_labels = {'approve': '审核通过', 'revise': '打回整改', 'reject': '审核拒绝'}
    action_label = action_labels.get(action, action)
    reviewer_name = user.username if user else 'admin'
    notified_ids = _notify_admin_claws('知识库', action_label, entry.title,
                        f'操作人: {reviewer_name}' + (f'\n审核意见: {comment}' if comment else ''))
    if action in ('revise', 'reject') and entry.source_openclaw_id:
        from app.models import OpenClawInstance
        source_claw = OpenClawInstance.query.get(entry.source_openclaw_id)
        if source_claw:
            _notify_submitter_review_result(source_claw.name, '知识库', entry.title,
                                           action_label, comment)

    # 自动关闭 admin OpenClaw 上的审核待办
    from app.models import ClawTodo, ClawTodoLog, OpenClawInstance
    admin_claws = OpenClawInstance.query.filter(
        OpenClawInstance.role == 'admin',
        OpenClawInstance.status != 'deleted',
    ).all()
    for ac in admin_claws:
        review_todos = ClawTodo.query.filter(
            ClawTodo.openclaw_id == ac.id,
            ClawTodo.task_category == 'review',
            ClawTodo.title.like(f'%{entry.title}%'),
            ClawTodo.enabled == True,
        ).all()
        for rt in review_todos:
            rt.enabled = False
            log = ClawTodoLog(
                todo_id=rt.id,
                openclaw_id=ac.id,
                log_date=datetime.now().date(),
                completed_at=datetime.now(),
                status='approved',
                result_summary=f'知识库「{entry.title}」已{action_label}',
            )
            db.session.add(log)

    # 写一条评审时间线记录（修复 #131）
    add_review_comment(
        resource_type='knowledge',
        resource_id=entry.id,
        action=action,
        from_status=old_status,
        to_status=new_status,
        content=comment,
        author=user.username,
        author_type='user',
        commit=False,
    )

    db.session.commit()

    # SSE 即时推送
    if notified_ids:
        from app.api.agent_client import notify_claw
        for cid in notified_ids:
            notify_claw(cid)

    payload = entry.to_dict()
    payload.update({
        'action': action,
        'review_status': new_status,
        'review_comment': comment,
        'comment': comment,
    })
    return jsonify(payload)


@api_bp.route('/knowledge/<int:entry_id>/distribute', methods=['POST'])
def distribute_knowledge(entry_id):
    """共享知识到指定范围"""
    entry = KnowledgeEntry.query.get_or_404(entry_id)

    if entry.status != 'approved':
        return jsonify({'error': '只有已审核通过的知识才能共享'}), 400

    data = request.get_json()
    dist = KnowledgeDistribution(
        knowledge_id=entry_id,
        target_scope=data.get('target_scope', 'all'),
        target_project=data.get('target_project'),
        target_module=data.get('target_module'),
        distributed_by=data.get('distributed_by', 'admin'),
    )
    db.session.add(dist)
    db.session.commit()
    return jsonify({'message': '知识已共享', 'distribution_id': dist.id}), 201


@api_bp.route('/knowledge/<int:entry_id>', methods=['DELETE'])
def delete_knowledge(entry_id):
    """删除知识条目"""
    entry = KnowledgeEntry.query.get_or_404(entry_id)
    if entry.entry_type == 'test_journal':
        return jsonify({
            'error': '版本纪要不能物理删除，请通过归档保留历史',
            'code': 'VERSIONED_KNOWLEDGE_DELETE_FORBIDDEN',
        }), 409

    # Clear foreign key references in topics
    Topic.query.filter_by(review_knowledge_id=entry_id).update(
        {'review_knowledge_id': None})
    # Delete associated distribution records
    KnowledgeDistribution.query.filter_by(knowledge_id=entry_id).delete()
    KnowledgeFavorite.query.filter_by(knowledge_id=entry_id).delete()
    db.session.delete(entry)
    db.session.commit()
    return jsonify({'message': '已删除'})
