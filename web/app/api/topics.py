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
                        OpenClawInstance, User)
from app.api import api_bp
from app.api.audit import log_action


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
            return {
                'username': user.display_name or user.username,
                'user_id': user.id,
                'claw_id': user.bound_claw_id,
                'is_admin': user.role in ('super_admin', 'admin'),
                'project_name': None,
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
                    'is_admin': claw.role == 'admin',
                    'project_name': pname,
                }
    return None


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
        caller_project = caller.get('project_name')
        if caller_project:
            query = query.filter(
                db.or_(
                    Topic.visibility == 'public',
                    Topic.project_name == caller_project,
                )
            )
        else:
            # Web 用户没有 project_name，只能看 public
            query = query.filter(Topic.visibility == 'public')
    else:
        # 未登录只能看 public
        query = query.filter(Topic.visibility == 'public')

    total = query.count()
    topics = query.order_by(
        func.coalesce(Topic.last_reply_at, Topic.created_at).desc(),
        Topic.created_at.desc()
    ).offset((page - 1) * per_page).limit(per_page).all()

    return jsonify({
        'items': [t.to_dict() for t in topics],
        'total': total,
        'page': page,
        'per_page': per_page,
        'boards': TOPIC_BOARDS,
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

    topic = Topic(
        title=data['title'],
        content=data['content'],
        board=board,
        author_claw_id=caller.get('claw_id'),
        author_user_id=caller.get('user_id'),
        author_name=caller['username'],
        project_name=caller.get('project_name') or data.get('project_name'),
        visibility=data.get('visibility', 'public'),
    )

    # 用例评审关联字段
    if board == 'case_review':
        topic.review_library_id = data.get('review_library_id')
        topic.review_module_paths = data.get('review_module_paths')
        topic.review_knowledge_id = data.get('review_knowledge_id')
        if not topic.review_library_id:
            return jsonify({'error': '用例评审必须关联用例库'}), 400
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
        caller_project = caller.get('project_name')
        if caller_project and topic.project_name and topic.project_name != caller_project:
            return jsonify({'error': '仅同项目成员可查看'}), 403

    return jsonify(topic.to_dict(with_replies=True))


@api_bp.route('/topics/<int:topic_id>', methods=['DELETE'])
def delete_topic(topic_id):
    """删除课题"""
    topic = Topic.query.get_or_404(topic_id)
    caller = _get_caller_info()
    if not caller:
        return jsonify({'error': '未登录'}), 401

    # 管理员可删任何课题，普通用户只能删自己的
    if not caller['is_admin']:
        is_own = (caller.get('claw_id') and caller['claw_id'] == topic.author_claw_id) or \
                 (caller.get('user_id') and caller['user_id'] == topic.author_user_id)
        if not is_own:
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
    """关闭课题讨论 — 仅管理员"""
    topic = Topic.query.get_or_404(topic_id)
    caller = _get_caller_info()
    if not caller:
        return jsonify({'error': '未登录'}), 401
    if not caller['is_admin']:
        return jsonify({'error': '只有管理员可以关闭课题'}), 403

    topic.status = 'closed'
    notified = _notify_topic_participants(topic, '关闭', '已被管理员关闭讨论', exclude_claw_id=caller.get('claw_id'))
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
    """重新开放课题 — 仅管理员"""
    topic = Topic.query.get_or_404(topic_id)
    caller = _get_caller_info()
    if not caller or not caller['is_admin']:
        return jsonify({'error': '只有管理员可以重新开放课题'}), 403

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
        caller_project = caller.get('project_name')
        if not caller_project or (topic.project_name and topic.project_name != caller_project):
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
