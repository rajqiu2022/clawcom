"""
课题讨论 API
支持发帖、回复、管理、频率限制、通知
"""
from datetime import datetime, date, timedelta
from flask import request, jsonify, session as flask_session
from sqlalchemy import func
from sqlalchemy.sql.expression import text
from app import db
from app.models import (Topic, TopicReply, TopicGrant, TOPIC_BOARDS,
                        TOPIC_VISIBILITIES, ClawMessage,
                        OpenClawInstance, User, Project,
                        CaseReviewRound, CaseReviewComment, CaseReviewNodeMark,
                        TestCase, TestCaseLibrary, TestCaseLibraryReview, _now)
from app.api import api_bp
from app.api.audit import log_action
from app.services.review_visibility import (
    caller_has_any_project,
    can_comment_topic,
    can_mark_review_nodes,
    can_view_topic,
    granted_topic_ids,
    normalize_visibility,
    visible_topic_filter,
)


def _sync_library_review_status(topic, new_lib_status):
    """将课题评审结果同步回用例库的 review_status。

    同步规则：
    - 课题 round approve → library approved
    - 课题 round reject  → library rejected
    - 课题 round pending (verdict 回退) → library pending_review
    - 课题 review_status closed → library approved (如果最终轮通过) / rejected (如果最终轮打回)
    - 课题 review_status reviewing (重开) → library pending_review

    参数 new_lib_status: draft / pending_review / approved / rejected
    """
    if not topic.review_library_id:
        return
    library = TestCaseLibrary.query.get(topic.review_library_id)
    if not library:
        return

    library.review_status = new_lib_status
    library.review_status_at = _now()

    # 同步关联的 TestCaseLibraryReview 记录状态
    review = TestCaseLibraryReview.query.filter_by(
        related_topic_id=topic.id
    ).order_by(TestCaseLibraryReview.id.desc()).first()
    if review and review.status == 'submitted':
        if new_lib_status == 'approved':
            review.status = 'approved'
            review.decided_at = _now()
        elif new_lib_status == 'rejected':
            review.status = 'rejected'
            review.decided_at = _now()


def _get_sys_config(key, default=''):
    try:
        row = db.session.execute(
            text("SELECT value FROM system_config WHERE config_key = :k"),
            {'k': key}
        ).fetchone()
        return row[0] if row else default
    except Exception:
        return default


def _get_caller_info():
    """识别调用者：Web session 或 OpenClaw Token"""
    uid = flask_session.get('user_id')
    if uid:
        user = User.query.get(uid)
        if user:
            project_ids = set()
            for pid in (user.managed_projects or []):
                try:
                    project_ids.add(int(pid))
                except Exception:
                    continue
            if user.bound_claw_id:
                claw = OpenClawInstance.query.get(user.bound_claw_id)
                if claw and claw.project_id:
                    project_ids.add(int(claw.project_id))
            return {
                'username': user.display_name or user.username,
                'user_id': user.id,
                'claw_id': user.bound_claw_id,
                'role': user.role,
                'is_admin': user.role in ('super_admin', 'admin'),
                'project_ids': list(project_ids),
            }

    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        token = auth[7:]
        for claw in OpenClawInstance.query.filter(OpenClawInstance.status != 'deleted').all():
            if claw.verify_token(token):
                pname = claw.project.name if claw.project else claw.project_name
                return {
                    'username': claw.name,
                    'user_id': None,
                    'claw_id': claw.id,
                    'role': 'admin' if claw.role == 'admin' else 'user',
                    'is_admin': claw.role == 'admin',
                    'project_ids': [claw.project_id] if claw.project_id else [],
                    'project_name': pname,
                }
    return None


def _topic_project_id(topic):
    pname = (topic.project_name or '').strip()
    if not pname:
        return None
    p = Project.query.filter_by(name=pname).first()
    return p.id if p else None


def _is_topic_granted(caller, topic):
    """调用者是否命中该课题的定向授权（assigned 可见性用）。"""
    if not caller or not topic:
        return False
    return int(topic.id) in granted_topic_ids(caller)


def _ensure_topic_viewable(caller, topic):
    """统一的查看鉴权，返回 error response 或 None。"""
    if can_view_topic(
            caller, topic,
            topic_project_id=_topic_project_id(topic),
            granted=_is_topic_granted(caller, topic),
            caller_has_project_access=caller_has_any_project(caller)):
        return None
    if not caller:
        return jsonify({'error': '该课题需登录查看'}), 401
    return jsonify({'error': '无权查看该课题'}), 403


def _ensure_topic_commentable(caller, topic):
    """统一的参与鉴权，返回 error response 或 None。"""
    if can_comment_topic(
            caller, topic,
            topic_project_id=_topic_project_id(topic),
            granted=_is_topic_granted(caller, topic),
            caller_has_project_access=caller_has_any_project(caller)):
        return None
    return jsonify({'error': '无权参与该课题讨论'}), 403


def _sync_topic_grants(topic, grants, operator):
    """按入参重建课题的定向授权列表。

    grants 形如 [{"type": "user", "id": 12}, {"type": "claw", "id": 11}]。
    返回 (成功条数, 跳过的非法项列表)。
    """
    if grants is None:
        return 0, []

    TopicGrant.query.filter_by(topic_id=topic.id).delete(synchronize_session=False)

    added, skipped = 0, []
    seen = set()
    for item in (grants or []):
        if not isinstance(item, dict):
            skipped.append(item)
            continue
        gtype = str(item.get('type') or '').strip()
        try:
            target_id = int(item.get('id'))
        except (TypeError, ValueError):
            skipped.append(item)
            continue

        if gtype == 'user':
            target = User.query.get(target_id)
            if not target:
                skipped.append(item)
                continue
            name = target.display_name or target.username
            grant = TopicGrant(topic_id=topic.id, grant_type='user',
                               target_user_id=target_id, target_name=name,
                               granted_by=operator)
        elif gtype == 'claw':
            target = OpenClawInstance.query.get(target_id)
            if not target or target.status == 'deleted':
                skipped.append(item)
                continue
            grant = TopicGrant(topic_id=topic.id, grant_type='claw',
                               target_claw_id=target_id, target_name=target.name,
                               granted_by=operator)
        else:
            skipped.append(item)
            continue

        key = (gtype, target_id)
        if key in seen:
            continue
        seen.add(key)
        db.session.add(grant)
        added += 1

    return added, skipped


def _is_topic_author(caller, topic):
    return ((caller.get('claw_id') and caller['claw_id'] == topic.author_claw_id)
            or (caller.get('user_id') and caller['user_id'] == topic.author_user_id))


def _notify_topic_participants(topic, event_type, detail, exclude_claw_id=None):
    """通知课题参与者，返回需要 SSE 通知的 claw_id 列表"""
    participant_ids = set()
    if topic.author_claw_id:
        participant_ids.add(topic.author_claw_id)
    for reply in topic.replies.filter(TopicReply.status != 'deleted').all():
        if reply.author_claw_id:
            participant_ids.add(reply.author_claw_id)
    if exclude_claw_id:
        participant_ids.discard(exclude_claw_id)

    for cid in participant_ids:
        msg = ClawMessage(
            claw_id=cid,
            sender_name='课题讨论',
            content='[%s] 课题「%s」%s\n#topic_id=%d' % (
                event_type, topic.title[:30], detail, topic.id),
            msg_type='topic_notify',
            direction='to_claw',
            status='pending',
        )
        db.session.add(msg)

    return list(participant_ids)


# ============== API 端点 ==============

@api_bp.route('/topics/boards', methods=['GET'])
def list_boards():
    """获取所有板块（返回数组格式供前端使用）"""
    return jsonify([{'key': k, 'label': v} for k, v in TOPIC_BOARDS.items()])


@api_bp.route('/topics', methods=['GET'])
def list_topics():
    """课题列表（支持板块/状态筛选、分页、visibility 权限过滤）"""
    board = request.args.get('board')
    status = request.args.get('status', 'open')
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    search = request.args.get('search')

    caller = _get_caller_info()

    query = Topic.query
    if board:
        query = query.filter(Topic.board == board)
    if status and status != 'all':
        query = query.filter(Topic.status == status)
    else:
        query = query.filter(Topic.status != 'deleted')
    if search:
        query = query.filter(Topic.title.like(f'%{search}%'))

    # visibility 四档过滤（public_all / public / project / assigned），
    # 列表与板块计数共用同一条件，避免两处逻辑漂移
    visibility_cond = visible_topic_filter(
        caller,
        caller_has_project_access=caller_has_any_project(caller),
    )
    query = query.filter(visibility_cond)

    total = query.count()
    topics = query.order_by(
        func.coalesce(Topic.last_reply_at, Topic.created_at).desc(),
        Topic.created_at.desc()
    ).offset((page - 1) * per_page).limit(per_page).all()

    # 统计各板块课题数（基于 status 过滤，不受 board 过滤影响）
    count_query = Topic.query
    if status and status != 'all':
        count_query = count_query.filter(Topic.status == status)
    else:
        count_query = count_query.filter(Topic.status != 'deleted')
    count_query = count_query.filter(visibility_cond)

    board_rows = count_query.with_entities(
        Topic.board, func.count(Topic.id)
    ).group_by(Topic.board).all()
    board_counts = {row[0]: row[1] for row in board_rows}
    board_counts['_all'] = sum(board_counts.values())

    return jsonify({
        'items': [t.to_dict() for t in topics],
        'total': total,
        'page': page,
        'per_page': per_page,
        'boards': TOPIC_BOARDS,
        'visibilities': TOPIC_VISIBILITIES,
        'board_counts': board_counts,
    })


@api_bp.route('/topics/visibilities', methods=['GET'])
def list_visibilities():
    """课题可见性四档枚举（发起弹窗与筛选器用）"""
    return jsonify([{'key': k, 'label': v} for k, v in TOPIC_VISIBILITIES.items()])


@api_bp.route('/topics', methods=['POST'])
def create_topic():
    """发起课题"""
    caller = _get_caller_info()
    if not caller:
        return jsonify({'error': '未登录'}), 401

    data = request.get_json()
    if not data or not data.get('title') or not data.get('content'):
        return jsonify({'error': '标题和内容为必填项'}), 400

    board = data.get('board', 'test_methods')
    if board not in TOPIC_BOARDS:
        return jsonify({'error': '无效的板块'}), 400

    # 频率限制（非管理员）
    if not caller['is_admin']:
        daily_limit = int(_get_sys_config('topic_daily_limit', '1'))
        today = date.today()
        today_count = Topic.query.filter(
            Topic.author_name == caller['username'],
            func.date(Topic.created_at) == today,
            Topic.status != 'deleted',
        ).count()
        if today_count >= daily_limit:
            return jsonify({'error': f'每天最多发起 {daily_limit} 个课题'}), 429

    caller_project_ids = caller.get('project_ids') or []
    explicit_project_id = data.get('project_id')
    topic_project_name = data.get('project_name')
    if explicit_project_id:
        p = Project.query.get(explicit_project_id)
        if p:
            topic_project_name = p.name
    elif caller_project_ids:
        p = Project.query.get(caller_project_ids[0])
        if p:
            topic_project_name = p.name

    topic = Topic(
        title=data['title'],
        content=data['content'],
        board=board,
        author_claw_id=caller.get('claw_id'),
        author_user_id=caller.get('user_id'),
        author_name=caller['username'],
        project_name=topic_project_name,
        visibility=normalize_visibility(data.get('visibility')),
    )

    grants = data.get('grants')
    if topic.visibility == 'assigned' and not grants:
        return jsonify({'error': '指定范围的课题必须至少指定一个用户或 Agent'}), 400

    # 用例评审关联字段
    if board == 'case_review':
        topic.review_library_id = data.get('review_library_id')
        topic.review_module_paths = data.get('review_module_paths')
        topic.review_knowledge_id = data.get('review_knowledge_id')
        topic.review_case_ids = data.get('review_case_ids')
        if not topic.review_library_id:
            return jsonify({'error': '用例评审必须关联用例库'}), 400

        # 用例库 share-aware 权限校验：
        # 让被共享出去的人也能发起 case_review topic（与"用例库邀请评审"打通）
        from app.api.testcases import (
            _ensure_library_access as _tcl_access,
        )
        library = TestCaseLibrary.query.get(topic.review_library_id)
        if not library:
            return jsonify({'error': '关联的用例库不存在'}), 404
        _, lib_err = _tcl_access(library, write=False)
        if lib_err:
            return jsonify({'error': '无权对该用例库发起评审课题：请联系作者开放共享'}), 403

        # 防重复：同一用例库不允许同时存在多个 reviewing 状态的评审课题
        existing_topic = Topic.query.filter_by(
            board='case_review',
            review_library_id=topic.review_library_id,
            review_status='reviewing'
        ).first()
        if existing_topic:
            return jsonify({
                'error': '该用例库已有进行中的评审课题（#%d），请在已有课题中继续' % existing_topic.id,
                'existing_topic_id': existing_topic.id
            }), 409
    db.session.add(topic)
    db.session.flush()

    skipped = []
    if topic.visibility == 'assigned':
        added, skipped = _sync_topic_grants(topic, grants, caller['username'])
        if not added:
            db.session.rollback()
            return jsonify({'error': '指定的用户或 Agent 均无效，请重新选择'}), 400

    db.session.commit()

    log_action('create', 'topic', topic.id, topic.title,
               operator=caller['username'],
               detail='发起课题「%s」板块: %s 范围: %s' % (
                   topic.title, TOPIC_BOARDS.get(board, board),
                   TOPIC_VISIBILITIES.get(topic.visibility, topic.visibility)))

    result = topic.to_dict()
    if skipped:
        result['skipped_grants'] = skipped
    return jsonify(result), 201


@api_bp.route('/topics/<int:topic_id>', methods=['GET'])
def get_topic(topic_id):
    """获取课题详情（含回复）"""
    topic = Topic.query.get_or_404(topic_id)
    if topic.status == 'deleted':
        return jsonify({'error': '课题已删除'}), 404

    # visibility 四档权限检查
    caller = _get_caller_info()
    err = _ensure_topic_viewable(caller, topic)
    if err:
        return err

    data = topic.to_dict(with_replies=True)
    if topic.visibility == 'assigned':
        data['grants'] = [g.to_dict() for g in
                          TopicGrant.query.filter_by(topic_id=topic.id).all()]
    return jsonify(data)


@api_bp.route('/topics/<int:topic_id>/grants', methods=['GET'])
def list_topic_grants(topic_id):
    """查看课题的定向授权列表"""
    topic = Topic.query.get_or_404(topic_id)
    caller = _get_caller_info()
    err = _ensure_topic_viewable(caller, topic)
    if err:
        return err
    grants = TopicGrant.query.filter_by(topic_id=topic.id).all()
    return jsonify({'items': [g.to_dict() for g in grants], 'total': len(grants)})


@api_bp.route('/topics/<int:topic_id>/grants', methods=['PUT'])
def replace_topic_grants(topic_id):
    """整体替换课题的定向授权列表 — 发起人或管理员"""
    topic = Topic.query.get_or_404(topic_id)
    caller = _get_caller_info()
    if not caller:
        return jsonify({'error': '未登录'}), 401
    if not caller['is_admin'] and not _is_topic_author(caller, topic):
        return jsonify({'error': '只有发起人或管理员可以调整授权'}), 403

    data = request.get_json() or {}
    grants = data.get('grants')
    if not isinstance(grants, list):
        return jsonify({'error': 'grants 必须是数组'}), 400
    if topic.visibility == 'assigned' and not grants:
        return jsonify({'error': '指定范围的课题不能清空授权，请先切换可见范围'}), 400

    added, skipped = _sync_topic_grants(topic, grants, caller['username'])
    if topic.visibility == 'assigned' and not added:
        db.session.rollback()
        return jsonify({'error': '指定的用户或 Agent 均无效，请重新选择'}), 400
    db.session.commit()

    log_action('update', 'topic', topic.id, topic.title,
               operator=caller['username'],
               detail='更新课题授权：%d 条生效，%d 条无效' % (added, len(skipped)))

    return jsonify({
        'message': '授权已更新',
        'total': added,
        'skipped_grants': skipped,
        'items': [g.to_dict() for g in
                  TopicGrant.query.filter_by(topic_id=topic.id).all()],
    })


@api_bp.route('/topics/<int:topic_id>/visibility', methods=['POST'])
def update_topic_visibility(topic_id):
    """调整课题可见范围 — 发起人或管理员

    切到 assigned 时必须同时给出 grants，否则会造成除发起人外无人可见。
    """
    topic = Topic.query.get_or_404(topic_id)
    caller = _get_caller_info()
    if not caller:
        return jsonify({'error': '未登录'}), 401
    if not caller['is_admin'] and not _is_topic_author(caller, topic):
        return jsonify({'error': '只有发起人或管理员可以调整可见范围'}), 403

    data = request.get_json() or {}
    raw = str(data.get('visibility') or '').strip()
    if raw not in TOPIC_VISIBILITIES:
        return jsonify({
            'error': '无效的可见范围，可选：%s' % '/'.join(TOPIC_VISIBILITIES.keys()),
        }), 400

    old = topic.visibility
    topic.visibility = raw

    if raw == 'assigned':
        grants = data.get('grants')
        existing = TopicGrant.query.filter_by(topic_id=topic.id).count()
        if grants:
            added, _ = _sync_topic_grants(topic, grants, caller['username'])
            if not added:
                db.session.rollback()
                return jsonify({'error': '指定的用户或 Agent 均无效，请重新选择'}), 400
        elif not existing:
            db.session.rollback()
            return jsonify({'error': '切换到指定范围时必须至少指定一个用户或 Agent'}), 400

    db.session.commit()

    log_action('update', 'topic', topic.id, topic.title,
               operator=caller['username'],
               detail='课题可见范围 %s → %s' % (
                   TOPIC_VISIBILITIES.get(old, old),
                   TOPIC_VISIBILITIES.get(raw, raw)))

    return jsonify(topic.to_dict())


def _review_scope_of(topic):
    """课题的评审范围：返回 (library, root_module_path, error_response)。

    范围锁定在课题自己的 `review_module_paths`，越界内容不返回，
    避免有人借"完全公开评审"读到整个用例库。
    """
    if topic.board != 'case_review':
        return None, None, (jsonify({'error': '该课题不是用例评审课题'}), 400)
    if not topic.review_library_id:
        return None, None, (jsonify({'error': '该评审课题未关联用例库'}), 400)
    library = TestCaseLibrary.query.get(topic.review_library_id)
    if not library:
        return None, None, (jsonify({'error': '关联的用例库已不存在'}), 404)

    paths = topic.review_module_paths or []
    root = str(paths[0] or '').strip().strip('/') if paths else ''
    return library, root, None


def _in_review_scope(module_path, root_path, scoped):
    """目录/用例路径是否落在评审范围内。scoped=False 表示整库评审。"""
    module_path = (module_path or '').strip().strip('/')
    if not scoped:
        return True
    if not root_path:
        # 范围锁定为"(未分类)"根目录
        return module_path == ''
    return module_path == root_path or module_path.startswith(root_path + '/')


def _load_topic_marks(topic_id):
    """返回 {节点 id: mark} 与 {节点 id: 记录} 两份映射。"""
    rows = CaseReviewNodeMark.query.filter_by(topic_id=topic_id).all()
    return ({r.node_id: r.mark for r in rows}, {r.node_id: r for r in rows})


def _parse_node_id(node_id):
    """`case:123` → ('case', '123')；`mod:登录模块` → ('module', '登录模块')。"""
    node_id = str(node_id or '').strip()
    if node_id.startswith('case:'):
        return 'case', node_id[5:]
    if node_id.startswith('mod:'):
        return 'module', node_id[4:]
    return None, None


@api_bp.route('/topics/<int:topic_id>/review-mindmap', methods=['GET'])
def get_review_mindmap(topic_id):
    """评审课题的目录脑图（含评审标记）。

    鉴权走**课题可见性**而不是用例库权限：完全公开评审的外部评审人没有
    用例库权限，若走 `_ensure_library_access` 必然 403。
    """
    from app.services.case_mindmap import (DEFAULT_MAX_LEAVES,
                                           build_directory_mindmap)

    topic = Topic.query.get_or_404(topic_id)
    if topic.status == 'deleted':
        return jsonify({'error': '课题已删除'}), 404

    caller = _get_caller_info()
    err = _ensure_topic_viewable(caller, topic)
    if err:
        return err

    library, root, scope_err = _review_scope_of(topic)
    if scope_err:
        return scope_err

    scoped = bool(topic.review_module_paths)
    try:
        max_nodes = int(request.args.get('max_nodes', DEFAULT_MAX_LEAVES))
    except (TypeError, ValueError):
        return jsonify({'error': 'max_nodes 必须是整数'}), 400
    max_nodes = max(50, min(max_nodes, 5000))

    # module_path：只取评审范围内某个子目录的子树，供前端展开被截断的目录时就地补齐。
    # 缺省 None 表示整个评审范围；显式传空串表示"未分类"那一支。
    sub_path = request.args.get('module_path')
    if sub_path is not None:
        sub_path = sub_path.strip().strip('/')
        scope_root = root if scoped else ''
        in_scope = (not scoped) or (
            sub_path == scope_root
            or (bool(scope_root) and sub_path.startswith(scope_root + '/'))
            or (not scope_root and sub_path == '')
        )
        if not in_scope:
            return jsonify({'error': 'module_path 超出该评审范围'}), 400

    effective_root = sub_path if sub_path is not None else (root if scoped else '')
    # 整库评审 + 未传 module_path 时不加目录过滤，避免把"未分类"之外的用例漏掉
    filter_by_path = scoped or sub_path is not None

    query = TestCase.query.filter_by(library_id=library.id)
    if filter_by_path:
        if effective_root:
            query = query.filter(db.or_(
                TestCase.module_path == effective_root,
                TestCase.module_path.like(effective_root + '/%'),
            ))
        else:
            # 范围限定但路径为空 = "未分类"那一支，不能退化成整库
            query = query.filter(db.or_(
                TestCase.module_path == '',
                TestCase.module_path.is_(None),
            ))
    cases = query.all()

    marks, _rows = _load_topic_marks(topic.id)
    if effective_root:
        root_text = effective_root.split('/')[-1]
    elif sub_path is not None or scoped:
        root_text = '(未分类)'
    else:
        root_text = library.name or '用例库#%d' % library.id

    mindmap = build_directory_mindmap(
        root_text, cases, root_module_path=effective_root,
        marks=marks, max_nodes=max_nodes)
    mindmap.update({
        'topic_id': topic.id,
        'library_id': library.id,
        'library_name': library.name,
        'scope_type': 'module' if scoped else 'library',
        'scope_module_path': root if scoped else '',
        'subtree_module_path': sub_path,
        'can_mark': can_mark_review_nodes(
            caller, topic,
            topic_project_id=_topic_project_id(topic),
            granted=_is_topic_granted(caller, topic),
            caller_has_project_access=caller_has_any_project(caller)),
    })
    return jsonify(mindmap)


@api_bp.route('/topics/<int:topic_id>/review-cases/<int:case_id>', methods=['GET'])
def get_review_case(topic_id, case_id):
    """评审范围内单条用例的详情，供脑图就地展开前提/步骤/预期。

    鉴权同样走**课题可见性**：完全公开评审的外部评审人没有用例库权限，
    却必须能看到用例步骤，否则根本没法评审。
    """
    topic = Topic.query.get_or_404(topic_id)
    if topic.status == 'deleted':
        return jsonify({'error': '课题已删除'}), 404

    caller = _get_caller_info()
    err = _ensure_topic_viewable(caller, topic)
    if err:
        return err

    library, root, scope_err = _review_scope_of(topic)
    if scope_err:
        return scope_err

    case = TestCase.query.filter_by(id=case_id, library_id=library.id).first()
    if not case:
        return jsonify({'error': '用例不存在'}), 404

    if topic.review_module_paths:
        path = case.module_path or ''
        in_scope = (path == root or path.startswith(root + '/')) if root else (path == '')
        if not in_scope:
            return jsonify({'error': '该用例不在评审范围内'}), 403

    return jsonify({
        'id': case.id,
        'case_id': case.case_id,
        'title': case.title,
        'priority': case.priority,
        'module_path': case.module_path or '',
        'content': case.content or {},
    })


@api_bp.route('/topics/<int:topic_id>/review-marks', methods=['GET'])
def list_review_marks(topic_id):
    """列出该评审的全部节点标记"""
    topic = Topic.query.get_or_404(topic_id)
    caller = _get_caller_info()
    err = _ensure_topic_viewable(caller, topic)
    if err:
        return err

    from app.services.case_mindmap import MARKS
    rows = CaseReviewNodeMark.query.filter_by(topic_id=topic.id).all()
    return jsonify({
        'items': [r.to_dict() for r in rows],
        'total': len(rows),
        'mark_legend': {k: dict(v) for k, v in MARKS.items()},
    })


@api_bp.route('/topics/<int:topic_id>/review-marks', methods=['PUT'])
def update_review_marks(topic_id):
    """批量打/清除评审节点标记（幂等）。

    请求体：{"marks": [{"node_id": "case:123", "mark": "question", "note": "可选"}]}
    mark 传空值表示清除该节点标记。

    标记只写评审镜像层，**不回写用例库**。
    """
    from app.services.case_mindmap import normalize_mark

    topic = Topic.query.get_or_404(topic_id)
    if topic.status == 'deleted':
        return jsonify({'error': '课题已删除'}), 404

    caller = _get_caller_info()
    if not caller:
        return jsonify({'error': '未登录'}), 401
    if not can_mark_review_nodes(
            caller, topic,
            topic_project_id=_topic_project_id(topic),
            granted=_is_topic_granted(caller, topic),
            caller_has_project_access=caller_has_any_project(caller)):
        return jsonify({'error': '无权在该评审上打标记'}), 403

    library, root, scope_err = _review_scope_of(topic)
    if scope_err:
        return scope_err
    scoped = bool(topic.review_module_paths)

    data = request.get_json() or {}
    items = data.get('marks')
    if not isinstance(items, list):
        return jsonify({'error': 'marks 必须是数组'}), 400
    if len(items) > 500:
        return jsonify({'error': '单次最多提交 500 个标记'}), 400

    # 允许清除他人标记的人：评审发起人 / 管理员
    can_clear_others = caller['is_admin'] or _is_topic_author(caller, topic)
    operator = caller['username']

    existing = {r.node_id: r for r in
                CaseReviewNodeMark.query.filter_by(topic_id=topic.id).all()}
    case_cache = {}
    applied, cleared, skipped = 0, 0, []

    for item in items:
        if not isinstance(item, dict):
            skipped.append({'item': item, 'reason': 'NOT_OBJECT'})
            continue
        node_id = str(item.get('node_id') or '').strip()
        node_type, node_key = _parse_node_id(node_id)
        if not node_type:
            skipped.append({'node_id': node_id, 'reason': 'BAD_NODE_ID'})
            continue

        # 范围校验：越界节点一律拒绝，防止借公开评审标记范围外的用例
        if node_type == 'case':
            if not node_key.isdigit():
                skipped.append({'node_id': node_id, 'reason': 'BAD_CASE_ID'})
                continue
            case = case_cache.get(node_key)
            if case is None:
                case = TestCase.query.get(int(node_key))
                case_cache[node_key] = case
            if not case or case.library_id != library.id:
                skipped.append({'node_id': node_id, 'reason': 'CASE_NOT_IN_LIBRARY'})
                continue
            if not _in_review_scope(case.module_path, root, scoped):
                skipped.append({'node_id': node_id, 'reason': 'OUT_OF_SCOPE'})
                continue
        else:
            node_key = node_key.strip().strip('/')
            if not _in_review_scope(node_key, root, scoped):
                skipped.append({'node_id': node_id, 'reason': 'OUT_OF_SCOPE'})
                continue

        mark = normalize_mark(item.get('mark'))
        row = existing.get(node_id)

        if mark is None:
            if row is None:
                continue
            if row.marked_by and row.marked_by != operator and not can_clear_others:
                skipped.append({'node_id': node_id, 'reason': 'NOT_YOUR_MARK'})
                continue
            db.session.delete(row)
            existing.pop(node_id, None)
            cleared += 1
            continue

        note = str(item.get('note') or '').strip()[:500]
        if row is None:
            row = CaseReviewNodeMark(
                topic_id=topic.id, node_type=node_type, node_key=node_key,
                mark=mark, note=note, marked_by=operator)
            db.session.add(row)
            existing[node_id] = row
        else:
            if row.marked_by and row.marked_by != operator and not can_clear_others:
                skipped.append({'node_id': node_id, 'reason': 'NOT_YOUR_MARK'})
                continue
            row.mark = mark
            row.note = note
            row.marked_by = operator
        applied += 1

    db.session.commit()

    rows = CaseReviewNodeMark.query.filter_by(topic_id=topic.id).all()
    return jsonify({
        'message': '标记已更新',
        'applied': applied,
        'cleared': cleared,
        'skipped': skipped,
        'items': [r.to_dict() for r in rows],
    })


@api_bp.route('/topics/<int:topic_id>', methods=['DELETE'])
def delete_topic(topic_id):
    """删除课题"""
    topic = Topic.query.get_or_404(topic_id)
    caller = _get_caller_info()
    if not caller:
        return jsonify({'error': '未登录'}), 401

    # 超管可删任何课题；项目管理员可删本项目或自己的；成员仅删自己的
    if caller.get('role') != 'super_admin':
        is_own = (caller.get('claw_id') and caller['claw_id'] == topic.author_claw_id) or \
                 (caller.get('user_id') and caller['user_id'] == topic.author_user_id)
        topic_pid = _topic_project_id(topic)
        in_project = topic_pid and topic_pid in set(caller.get('project_ids') or [])
        if caller.get('role') == 'admin':
            if not (is_own or in_project):
                return jsonify({'error': '仅可删除自己或本项目课题'}), 403
        elif not is_own:
            return jsonify({'error': '只能删除自己的课题'}), 403

    topic.status = 'deleted'
    notified = _notify_topic_participants(topic, '删除', '已被删除', exclude_claw_id=caller.get('claw_id'))
    db.session.commit()
    from app.api.agent_client import notify_claw
    for cid in notified:
        notify_claw(cid)

    log_action('delete', 'topic', topic.id, topic.title,
               operator=caller['username'],
               detail='删除课题「%s」' % topic.title)

    return jsonify({'message': '课题已删除'})


@api_bp.route('/topics/<int:topic_id>/close', methods=['POST'])
def close_topic(topic_id):
    """关闭课题讨论 — 管理员或作者"""
    topic = Topic.query.get_or_404(topic_id)
    caller = _get_caller_info()
    if not caller:
        return jsonify({'error': '未登录'}), 401
    is_author = (caller.get('claw_id') and caller['claw_id'] == topic.author_claw_id) or \
                (caller.get('user_id') and caller['user_id'] == topic.author_user_id)
    if not caller['is_admin'] and not is_author:
        return jsonify({'error': '只有管理员或作者可以关闭课题'}), 403

    topic.status = 'closed'
    detail_msg = '已被作者关闭评审' if is_author else '已被管理员关闭讨论'
    notified = _notify_topic_participants(topic, '关闭', detail_msg, exclude_claw_id=caller.get('claw_id'))
    db.session.commit()
    from app.api.agent_client import notify_claw
    for cid in notified:
        notify_claw(cid)

    log_action('close', 'topic', topic.id, topic.title,
               operator=caller['username'],
               detail='关闭课题「%s」讨论' % topic.title)

    return jsonify({'message': '课题已关闭'})


@api_bp.route('/topics/<int:topic_id>/reopen', methods=['POST'])
def reopen_topic(topic_id):
    """重新开放课题 — 管理员或作者"""
    topic = Topic.query.get_or_404(topic_id)
    caller = _get_caller_info()
    if not caller:
        return jsonify({'error': '未登录'}), 401
    is_author = (caller.get('claw_id') and caller['claw_id'] == topic.author_claw_id) or \
                (caller.get('user_id') and caller['user_id'] == topic.author_user_id)
    if not caller['is_admin'] and not is_author:
        return jsonify({'error': '只有管理员或作者可以重新开放课题'}), 403

    topic.status = 'open'
    db.session.commit()
    return jsonify({'message': '课题已重新开放'})


@api_bp.route('/topics/<int:topic_id>/replies', methods=['POST'])
def reply_topic(topic_id):
    """回复课题"""
    topic = Topic.query.get_or_404(topic_id)
    if topic.status != 'open':
        return jsonify({'error': '课题已关闭，无法回复'}), 400

    caller = _get_caller_info()
    if not caller:
        return jsonify({'error': '未登录'}), 401

    # visibility 四档参与权限检查
    err = _ensure_topic_commentable(caller, topic)
    if err:
        return err

    data = request.get_json()
    if not data or not data.get('content'):
        return jsonify({'error': '回复内容不能为空'}), 400

    # 回复间隔限制（非管理员）
    if not caller['is_admin']:
        interval = int(_get_sys_config('topic_reply_interval_minutes', '10'))
        cutoff = datetime.now() - timedelta(minutes=interval)
        recent = TopicReply.query.filter(
            TopicReply.author_name == caller['username'],
            TopicReply.created_at > cutoff,
        ).first()
        if recent:
            return jsonify({'error': f'回复间隔至少 {interval} 分钟'}), 429

        # 每日回复次数限制
        reply_daily_limit = int(_get_sys_config('topic_reply_daily_limit', '5'))
        today = date.today()
        today_reply_count = TopicReply.query.filter(
            TopicReply.author_name == caller['username'],
            func.date(TopicReply.created_at) == today,
            TopicReply.status != 'deleted',
        ).count()
        if today_reply_count >= reply_daily_limit:
            return jsonify({'error': f'每天最多参与 {reply_daily_limit} 次课题讨论'}), 429

    reply = TopicReply(
        topic_id=topic_id,
        content=data['content'],
        author_claw_id=caller.get('claw_id'),
        author_user_id=caller.get('user_id'),
        author_name=caller['username'],
        reply_to_id=data.get('reply_to_id'),
    )
    db.session.add(reply)
    topic.reply_count = (topic.reply_count or 0) + 1
    topic.last_reply_at = datetime.now()

    notified = _notify_topic_participants(topic, '回复', '%s 发表了回复' % caller['username'],
                               exclude_claw_id=caller.get('claw_id'))
    db.session.commit()
    from app.api.agent_client import notify_claw
    for cid in notified:
        notify_claw(cid)

    log_action('reply', 'topic', topic.id, topic.title,
               operator=caller['username'],
               detail='回复课题「%s」' % topic.title)

    return jsonify(reply.to_dict()), 201


@api_bp.route('/topics/<int:topic_id>/replies/<int:reply_id>', methods=['DELETE'])
def delete_reply(topic_id, reply_id):
    """删除回复 — 管理员或回复人"""
    reply = TopicReply.query.filter_by(id=reply_id, topic_id=topic_id).first_or_404()
    caller = _get_caller_info()
    if not caller:
        return jsonify({'error': '未登录'}), 401

    if not caller['is_admin']:
        is_own = (caller.get('claw_id') and caller['claw_id'] == reply.author_claw_id) or \
                 (caller.get('user_id') and caller['user_id'] == reply.author_user_id)
        if not is_own:
            return jsonify({'error': '只能删除自己的回复'}), 403

    reply.status = 'deleted'
    topic = Topic.query.get(topic_id)
    if topic:
        topic.reply_count = max(0, (topic.reply_count or 1) - 1)
    db.session.commit()

    return jsonify({'message': '回复已删除'})


# ============== 用例评审轮次 API ==============

@api_bp.route('/topics/<int:topic_id>/review-rounds', methods=['GET'])
def list_review_rounds(topic_id):
    """获取评审课题的所有轮次"""
    topic = Topic.query.get_or_404(topic_id)
    if topic.board != 'case_review':
        return jsonify({'error': '非用例评审课题'}), 400
    rounds = CaseReviewRound.query.filter_by(topic_id=topic_id)\
        .filter((CaseReviewRound.is_deleted == False) | (CaseReviewRound.is_deleted == None))\
        .order_by(CaseReviewRound.round_number).all()
    return jsonify([rd.to_dict() for rd in rounds])


@api_bp.route('/topics/<int:topic_id>/review-rounds', methods=['POST'])
def submit_review_round(topic_id):
    """提交新一轮评审（发起者操作）

    请求体：
    {
        "description": "评审介绍（Markdown）",
        "case_content": "用例内容（YAML 格式字符串）"
    }
    """
    topic = Topic.query.get_or_404(topic_id)
    if topic.board != 'case_review':
        return jsonify({'error': '非用例评审课题'}), 400

    caller = _get_caller_info()
    if not caller:
        return jsonify({'error': '未登录'}), 401

    # 只有发起者或管理员可以提交新轮次
    is_author = (caller.get('claw_id') and caller['claw_id'] == topic.author_claw_id) or \
                (caller.get('user_id') and caller['user_id'] == topic.author_user_id)
    if not is_author and not caller['is_admin']:
        return jsonify({'error': '只有发起者或管理员可以提交评审'}), 403

    # 总状态已关闭不能再提交
    if topic.review_status == 'closed':
        return jsonify({'error': '评审已关闭，无法提交新轮次'}), 400

    data = request.get_json()
    if not data:
        return jsonify({'error': '请求体为空'}), 400
    if not data.get('case_content', '').strip():
        return jsonify({'error': '用例内容不能为空'}), 400

    # 关闭所有之前的 pending 轮次（每次只有一轮开放）
    pending_rounds = CaseReviewRound.query.filter_by(
        topic_id=topic_id, status='pending').all()
    for pr in pending_rounds:
        pr.status = 'rejected'

    # 计算轮次号
    max_round = db.session.query(func.max(CaseReviewRound.round_number))\
        .filter_by(topic_id=topic_id).scalar() or 0
    new_round = max_round + 1

    rd = CaseReviewRound(
        topic_id=topic_id,
        round_number=new_round,
        description=data.get('description', ''),
        case_content=data['case_content'],
        status='pending',
        submitted_by=caller['username'],
    )
    db.session.add(rd)

    # 确保总状态为 reviewing
    if topic.review_status != 'reviewing':
        topic.review_status = 'reviewing'

    # 提交新轮次 → 用例库保持待评审状态
    _sync_library_review_status(topic, 'pending_review')

    # 通知参与者
    detail = '%s 提交了第 %d 轮评审' % (caller['username'], new_round)
    notified = _notify_topic_participants(topic, '评审提交', detail,
                                          exclude_claw_id=caller.get('claw_id'))
    db.session.commit()
    from app.api.agent_client import notify_claw
    for cid in notified:
        notify_claw(cid)

    log_action('submit_review', 'topic', topic.id, topic.title,
               operator=caller['username'],
               detail='提交第 %d 轮用例评审' % new_round)

    return jsonify(rd.to_dict()), 201


@api_bp.route('/topics/<int:topic_id>/review-rounds/<int:round_id>',
              methods=['DELETE'])
def delete_review_round(topic_id, round_id):
    """删除一轮评审

    - pending/rejected 状态可删除
    - 有评审意见：软删除（标记 is_deleted，数据保留）
    - 无评审意见：物理删除
    - 删除后自动重排剩余轮次序号
    - 只有发起者或管理员可以操作
    """
    topic = Topic.query.get_or_404(topic_id)
    if topic.board != 'case_review':
        return jsonify({'error': '非用例评审课题'}), 400

    caller = _get_caller_info()
    if not caller:
        return jsonify({'error': '未登录'}), 401

    is_author = (caller.get('claw_id') and caller['claw_id'] == topic.author_claw_id) or \
                (caller.get('user_id') and caller['user_id'] == topic.author_user_id)
    if not is_author and not caller['is_admin']:
        return jsonify({'error': '只有发起者或管理员可以删除轮次'}), 403

    rd = CaseReviewRound.query.filter_by(
        id=round_id, topic_id=topic_id).first()
    if not rd:
        return jsonify({'error': '轮次不存在'}), 404

    if rd.status not in ('pending', 'rejected'):
        return jsonify({'error': '只能删除 pending 或 rejected 状态的轮次（当前: %s）' % rd.status}), 400

    deleted_round_number = rd.round_number
    comment_count = CaseReviewComment.query.filter_by(round_id=rd.id).count()

    if comment_count > 0:
        # 有评审意见：软删除
        from datetime import datetime
        rd.is_deleted = True
        rd.deleted_at = datetime.now()
        action_detail = '软删除第 %d 轮评审（保留 %d 条评审意见）' % (deleted_round_number, comment_count)
    else:
        # 无评审意见：物理删除
        db.session.delete(rd)
        action_detail = '删除第 %d 轮评审' % deleted_round_number

    db.session.flush()

    # 重排剩余未删除轮次的序号
    remaining_rounds = CaseReviewRound.query.filter_by(
        topic_id=topic_id
    ).filter(
        (CaseReviewRound.is_deleted == False) | (CaseReviewRound.is_deleted == None)
    ).order_by(CaseReviewRound.submitted_at, CaseReviewRound.id).all()

    for idx, r in enumerate(remaining_rounds, 1):
        if r.round_number != idx:
            r.round_number = idx

    db.session.commit()

    log_action('delete_review_round', 'topic', topic.id, topic.title,
               operator=caller['username'],
               detail=action_detail)

    return jsonify({'message': action_detail}), 200


def _do_add_review_comment(topic, rd, override_data=None, caller=None):
    """提交评审意见的核心逻辑（内部函数）

    override_data: 外部已构造好的请求数据（用于已关闭轮次的总结场景）
    caller: 外部已获取的调用者信息（避免重复获取）
    """
    if not caller:
        caller = _get_caller_info()
        if not caller:
            return jsonify({'error': '未登录'}), 401

    # 已关闭的评审：仅允许发起者/管理员提交总结（由调用方控制）
    if not override_data:
        if topic.review_status == 'closed':
            return jsonify({'error': '评审已关闭，无法提交评审意见'}), 400
        # 该轮已结束（approved/rejected）也不能再提交（普通场景）
        if rd.status in ('approved', 'rejected'):
            return jsonify({'error': '该轮评审已结束（%s），无法再提交意见' % rd.status}), 400

    data = override_data or request.get_json()
    if not data or not data.get('content', '').strip():
        return jsonify({'error': '评审意见不能为空'}), 400

    verdict = data.get('verdict', 'comment')
    if verdict not in ('comment', 'approve', 'reject', 'summary'):
        verdict = 'comment'

    # 评分（可选，1-10）
    score = data.get('score')
    if score is not None:
        try:
            score = int(score)
            if score < 1 or score > 10:
                return jsonify({'error': '评分范围为 1-10 分'}), 400
        except (ValueError, TypeError):
            return jsonify({'error': '评分必须是整数'}), 400

    comment = CaseReviewComment(
        round_id=rd.id,
        author_name=caller['username'],
        author_claw_id=caller.get('claw_id'),
        author_user_id=caller.get('user_id'),
        content=data['content'],
        verdict=verdict,
        score=score,
    )
    db.session.add(comment)

    # 如果是 approve 或 reject，更新轮次状态（summary 不影响状态）
    if verdict == 'approve':
        rd.status = 'approved'
        _sync_library_review_status(topic, 'approved')
    elif verdict == 'reject':
        rd.status = 'rejected'
        _sync_library_review_status(topic, 'rejected')

    # 通知
    verdict_labels = {'comment': '评论', 'approve': '通过', 'reject': '打回', 'summary': '评审总结'}
    detail = '%s 对第 %d 轮评审提交了 %s' % (
        caller['username'], rd.round_number, verdict_labels.get(verdict, '评论'))
    notified = _notify_topic_participants(topic, '评审意见', detail,
                                          exclude_claw_id=caller.get('claw_id'))
    db.session.commit()
    from app.api.agent_client import notify_claw
    for cid in notified:
        notify_claw(cid)

    log_action('review_comment', 'topic', topic.id, topic.title,
               operator=caller['username'],
               detail='对第 %d 轮评审提交 %s（轮次 %d）' % (
                   rd.round_number, verdict_labels.get(verdict, '评论'), rd.round_number))

    return jsonify(comment.to_dict()), 201


@api_bp.route('/topics/<int:topic_id>/review-comments', methods=['POST'])
def add_review_comment_auto(topic_id):
    """简化接口：自动提交到当前开放的评审轮次（不需要指定 round_id）

    请求体与 add_review_comment 相同：
    {
        "content": "评审意见（Markdown）",
        "verdict": "comment|approve|reject",
        "score": 8   // 可选
    }
    """
    topic = Topic.query.get_or_404(topic_id)
    if topic.board != 'case_review':
        return jsonify({'error': '非用例评审课题'}), 400

    # 查找当前唯一的 pending 轮次
    rd = CaseReviewRound.query.filter_by(
        topic_id=topic_id, status='pending'
    ).order_by(CaseReviewRound.round_number.desc()).first()
    if not rd:
        return jsonify({'error': '当前没有开放的评审轮次，所有轮次已结束'}), 400

    return _do_add_review_comment(topic, rd)


@api_bp.route('/topics/<int:topic_id>/review-rounds/<int:round_id>/comments', methods=['POST'])
def add_review_comment(topic_id, round_id):
    """对某轮评审提交评审意见

    请求体：
    {
        "content": "评审意见（Markdown）",
        "verdict": "comment|approve|reject"  # 可选，默认 comment
    }
    """
    topic = Topic.query.get_or_404(topic_id)
    if topic.board != 'case_review':
        return jsonify({'error': '非用例评审课题'}), 400

    rd = CaseReviewRound.query.filter_by(id=round_id, topic_id=topic_id).first_or_404()

    # 轮次已关闭时，仅发起者/管理员可以提交评审总结（verdict=summary）
    if rd.status in ('approved', 'rejected'):
        caller = _get_caller_info()
        if not caller:
            return jsonify({'error': '未登录'}), 401
        is_author = (caller.get('claw_id') and caller['claw_id'] == topic.author_claw_id) or \
                    (caller.get('user_id') and caller['user_id'] == topic.author_user_id)
        if not is_author and not caller['is_admin']:
            return jsonify({'error': '该轮评审已结束，只有发起者或管理员可以提交评审总结'}), 403
        # 强制 verdict 为 summary
        data = request.get_json() or {}
        data['verdict'] = 'summary'
        return _do_add_review_comment(topic, rd, override_data=data, caller=caller)

    return _do_add_review_comment(topic, rd)


@api_bp.route('/topics/<int:topic_id>/review-status', methods=['POST'])
def update_review_status(topic_id):
    """更新评审总状态（关闭/重开评审）

    请求体：{"status": "closed" | "reviewing"}
    """
    topic = Topic.query.get_or_404(topic_id)
    if topic.board != 'case_review':
        return jsonify({'error': '非用例评审课题'}), 400

    caller = _get_caller_info()
    if not caller:
        return jsonify({'error': '未登录'}), 401

    is_author = (caller.get('claw_id') and caller['claw_id'] == topic.author_claw_id) or \
                (caller.get('user_id') and caller['user_id'] == topic.author_user_id)
    if not is_author and not caller['is_admin']:
        return jsonify({'error': '只有发起者或管理员可以更改评审状态'}), 403

    data = request.get_json()
    new_status = data.get('status') if data else None
    if new_status not in ('closed', 'reviewing'):
        return jsonify({'error': '状态值无效，可选 closed / reviewing'}), 400

    topic.review_status = new_status

    # 同步用例库状态
    if new_status == 'closed':
        # 关闭评审：根据最新轮次结果决定用例库状态
        latest_round = CaseReviewRound.query.filter_by(
            topic_id=topic.id
        ).filter(
            (CaseReviewRound.is_deleted == False) | (CaseReviewRound.is_deleted == None)
        ).order_by(CaseReviewRound.round_number.desc()).first()
        if latest_round and latest_round.status == 'approved':
            _sync_library_review_status(topic, 'approved')
        elif latest_round and latest_round.status == 'rejected':
            _sync_library_review_status(topic, 'rejected')
        else:
            # 没有明确结论就关闭，按草稿处理
            _sync_library_review_status(topic, 'draft')
    elif new_status == 'reviewing':
        # 重开评审
        _sync_library_review_status(topic, 'pending_review')

    status_label = '关闭' if new_status == 'closed' else '重新开放'
    notified = _notify_topic_participants(
        topic, '评审状态', '%s %s了评审' % (caller['username'], status_label),
        exclude_claw_id=caller.get('claw_id'))
    db.session.commit()
    from app.api.agent_client import notify_claw
    for cid in notified:
        notify_claw(cid)

    return jsonify({'message': '评审已%s' % status_label, 'review_status': new_status})


@api_bp.route('/topics/<int:topic_id>/review-summary', methods=['POST'])
def submit_review_summary(topic_id):
    """提交评审总结 — 评审关闭后，发起者或管理员可填写总结。

    请求体：{"summary": "评审总结内容（Markdown）"}
    """
    topic = Topic.query.get_or_404(topic_id)
    if topic.board != 'case_review':
        return jsonify({'error': '非用例评审课题'}), 400

    caller = _get_caller_info()
    if not caller:
        return jsonify({'error': '未登录'}), 401

    is_author = (caller.get('claw_id') and caller['claw_id'] == topic.author_claw_id) or \
                (caller.get('user_id') and caller['user_id'] == topic.author_user_id)
    if not is_author and not caller['is_admin']:
        return jsonify({'error': '只有发起者或管理员可以填写评审总结'}), 403

    if topic.review_status != 'closed':
        return jsonify({'error': '评审尚未关闭，请先关闭评审再填写总结'}), 400

    data = request.get_json()
    summary = (data.get('summary') or '').strip() if data else ''
    if not summary:
        return jsonify({'error': '总结内容不能为空'}), 400

    topic.review_summary = summary
    topic.review_summary_by = caller['username']
    topic.review_summary_at = datetime.now()
    db.session.commit()

    log_action('review_summary', 'topic', topic.id, topic.title,
               operator=caller['username'],
               detail='提交评审总结 (%d 字)' % len(summary))

    return jsonify({
        'message': '评审总结已保存',
        'review_summary': summary,
        'review_summary_by': caller['username'],
        'review_summary_at': str(topic.review_summary_at),
    })


@api_bp.route('/topics/<int:topic_id>/review-rounds/<int:round_id>/comments/<int:comment_id>', methods=['PUT'])
def edit_review_comment(topic_id, round_id, comment_id):
    """修改评审意见（每人仅1次修改机会）

    请求体：
    {
        "content": "新的评审意见",
        "verdict": "comment|approve|reject",  # 可选
        "score": 8  # 可选，1-10
    }
    """
    topic = Topic.query.get_or_404(topic_id)
    if topic.board != 'case_review':
        return jsonify({'error': '非用例评审课题'}), 400

    rd = CaseReviewRound.query.filter_by(id=round_id, topic_id=topic_id).first_or_404()
    comment = CaseReviewComment.query.filter_by(id=comment_id, round_id=round_id).first_or_404()

    caller = _get_caller_info()
    if not caller:
        return jsonify({'error': '未登录'}), 401

    # 已关闭不能修改
    if topic.review_status == 'closed':
        return jsonify({'error': '评审已关闭，无法修改评审意见'}), 400

    # 只有本人可以修改自己的评审
    is_comment_author = (
        (caller.get('claw_id') and caller['claw_id'] == comment.author_claw_id) or
        (caller.get('user_id') and caller['user_id'] == comment.author_user_id)
    )
    if not is_comment_author:
        return jsonify({'error': '只能修改自己的评审意见'}), 403

    # 检查是否已修改过（每人仅1次机会）
    if comment.is_edited:
        return jsonify({'error': '每条评审意见仅有1次修改机会，已使用'}), 400

    data = request.get_json()
    if not data:
        return jsonify({'error': '请求体为空'}), 400

    # 更新内容
    if data.get('content', '').strip():
        comment.content = data['content'].strip()

    # 更新 verdict
    new_verdict = data.get('verdict')
    if new_verdict and new_verdict in ('comment', 'approve', 'reject'):
        old_verdict = comment.verdict
        comment.verdict = new_verdict
        # 如果 verdict 变化，可能影响轮次状态
        if new_verdict != old_verdict:
            if new_verdict == 'approve':
                rd.status = 'approved'
                _sync_library_review_status(topic, 'approved')
            elif new_verdict == 'reject':
                rd.status = 'rejected'
                _sync_library_review_status(topic, 'rejected')
            elif old_verdict in ('approve', 'reject') and new_verdict == 'comment':
                # 从结论改回评论，轮次恢复 pending
                rd.status = 'pending'
                _sync_library_review_status(topic, 'pending_review')

    # 更新评分
    new_score = data.get('score')
    if new_score is not None:
        try:
            new_score = int(new_score)
            if new_score < 1 or new_score > 10:
                return jsonify({'error': '评分范围为 1-10 分'}), 400
            comment.score = new_score
        except (ValueError, TypeError):
            return jsonify({'error': '评分必须是整数'}), 400
    elif 'score' in data and data['score'] is None:
        comment.score = None

    comment.is_edited = True
    db.session.commit()

    log_action('edit_review_comment', 'topic', topic.id, topic.title,
               operator=caller['username'],
               detail='修改了第 %d 轮评审意见 #%d' % (rd.round_number, comment.id))

    return jsonify(comment.to_dict())
