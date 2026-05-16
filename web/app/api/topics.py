"""
课题讨论 API
支持发帖、回复、管理、频率限制、通知
"""
from datetime import datetime, date, timedelta
from flask import request, jsonify, session as flask_session
from sqlalchemy import func
from sqlalchemy.sql.expression import text
from app import db
from app.models import (Topic, TopicReply, TOPIC_BOARDS, ClawMessage,
                        OpenClawInstance, User, Project,
                        CaseReviewRound, CaseReviewComment,
                        TestCaseLibrary, TestCaseLibraryReview, _now)
from app.api import api_bp
from app.api.audit import log_action


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

    # visibility 过滤：project 类型只对同项目用户可见
    if caller:
        caller_projects = set(caller.get('project_ids') or [])
        if caller_projects:
            project_names = [p.name for p in Project.query.filter(Project.id.in_(list(caller_projects))).all()]
            query = query.filter(
                db.or_(
                    Topic.visibility == 'public',
                    Topic.project_name.in_(project_names),
                )
            )
        else:
            query = query.filter(Topic.visibility == 'public')
    else:
        # 未登录只能看 public
        query = query.filter(Topic.visibility == 'public')

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
    # visibility 过滤同上
    if caller:
        caller_projects = set(caller.get('project_ids') or [])
        if caller_projects:
            project_names = [p.name for p in Project.query.filter(
                Project.id.in_(list(caller_projects))).all()]
            count_query = count_query.filter(
                db.or_(
                    Topic.visibility == 'public',
                    Topic.project_name.in_(project_names),
                )
            )
        else:
            count_query = count_query.filter(Topic.visibility == 'public')
    else:
        count_query = count_query.filter(Topic.visibility == 'public')

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
        'board_counts': board_counts,
    })


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
        visibility=data.get('visibility', 'public'),
    )

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
    db.session.commit()

    log_action('create', 'topic', topic.id, topic.title,
               operator=caller['username'],
               detail='发起课题「%s」板块: %s' % (topic.title, TOPIC_BOARDS.get(board, board)))

    return jsonify(topic.to_dict()), 201


@api_bp.route('/topics/<int:topic_id>', methods=['GET'])
def get_topic(topic_id):
    """获取课题详情（含回复）"""
    topic = Topic.query.get_or_404(topic_id)
    if topic.status == 'deleted':
        return jsonify({'error': '课题已删除'}), 404

    # visibility 权限检查
    caller = _get_caller_info()
    if topic.visibility == 'project':
        if not caller:
            return jsonify({'error': '该项目课题需登录查看'}), 401
        caller_projects = set(caller.get('project_ids') or [])
        topic_pid = _topic_project_id(topic)
        if topic_pid and topic_pid not in caller_projects:
            return jsonify({'error': '仅同项目成员可查看'}), 403

    return jsonify(topic.to_dict(with_replies=True))


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

    # visibility 权限检查：project 类型只能同项目回复
    if topic.visibility == 'project':
        caller_projects = set(caller.get('project_ids') or [])
        topic_pid = _topic_project_id(topic)
        if topic_pid and topic_pid not in caller_projects:
            return jsonify({'error': '仅同项目成员可参与讨论'}), 403

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
