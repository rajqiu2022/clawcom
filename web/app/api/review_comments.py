"""评审中心 - 评审记录 API（统一覆盖 Skill/Rule/Knowledge/TestCaseLibrary）

接口：
  POST /review-comments              纯评论（不变更状态）
  GET  /review-comments?resource_type=&resource_id=  某资源完整时间线
  GET  /review-comments/my-submissions  我提交的资源 + 最新评审摘要

权限：
  - GET 任何登录用户/OpenClaw 可读（应用层不做细粒度，由资源本身可见性控制）
  - POST 必须登录（user 或 OpenClaw token），author 从会话/token 反推，不信任 body
"""
from flask import request, jsonify, session
from app import db
from app.models import (ReviewComment, Skill, Rule, KnowledgeEntry,
                        TestCaseLibrary, User, OpenClawInstance)
from app.api import api_bp


def _get_caller():
    """返回 (author, author_type)；未登录返回 (None, None)。

    优先：Web session > Bearer Token。OpenClaw 用 claw_name 作 author。
    """
    uid = session.get('user_id')
    if uid:
        u = User.query.get(uid)
        if u:
            return (u.username, 'user')

    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        token = auth[7:]
        for claw in OpenClawInstance.query.filter(
                OpenClawInstance.status != 'deleted').all():
            if claw.verify_token(token):
                return (claw.name, 'openclaw')
    return (None, None)


def _resource_exists(resource_type, resource_id):
    if resource_type == 'skill':
        return Skill.query.get(resource_id) is not None
    if resource_type == 'rule':
        return Rule.query.get(resource_id) is not None
    if resource_type == 'knowledge':
        return KnowledgeEntry.query.get(resource_id) is not None
    if resource_type == 'testcase_library':
        return TestCaseLibrary.query.get(resource_id) is not None
    return False


def add_review_comment(resource_type, resource_id, action,
                       from_status='', to_status='', content='',
                       author='', author_type='system',
                       parent_review_id=None, commit=False):
    """供 review_skill/rule/knowledge/tcl 调用：写一条评审记录。

    commit=False 时不主动 commit，由调用方一起提交。
    """
    rc = ReviewComment(
        resource_type=resource_type,
        resource_id=resource_id,
        parent_review_id=parent_review_id,
        action=action,
        from_status=from_status or '',
        to_status=to_status or '',
        content=content or '',
        author=author or 'system',
        author_type=author_type or 'system',
    )
    db.session.add(rc)
    if commit:
        db.session.commit()
    return rc


# === 评审 payload 归一化（v2 统一字段约定）=========================
#
# 历史问题（见 MEMORY #130）：4 套 review 接口的字段名各不相同：
#   - /skills/<id>/review:           review_status + review_comment
#   - /rules/<id>/review:            review_status + review_comment
#   - /knowledge/<id>/review:        action       + notes
#   - /agent-templates/<id>/review:  status       + review_comment
# 龙虾王照着 knowledge-manager SKILL 文档调 skill 时传 action/comment
# 都被 .get('review_status', '') 接成空串 → 状态不变 / comment 丢失。
#
# 新规约：所有接口都接受 **三套字段** 任意一套，归一化为同一组 (action, comment)。
#
# action（推荐，v2）            == review_status（兼容）  == 内部 status
#   approve                        approved                approved
#   revise                         revise                  revise
#   reject                         rejected                rejected
#
# 评论字段优先级：comment > review_comment > notes
ACTION_FROM_INPUT = {
    'approve': 'approve',
    'approved': 'approve',
    'revise': 'revise',
    'reject': 'reject',
    'rejected': 'reject',
}

# 内部统一存到资源表 .review_status 字段的值
STATUS_FROM_ACTION = {
    'approve': 'approved',
    'revise':  'revise',
    'reject':  'rejected',
}


def parse_review_action(data, allowed_actions=('approve', 'revise', 'reject')):
    """统一解析评审 payload，归一化字段命名。

    Args:
        data: request body dict（可能为 None）
        allowed_actions: 该资源支持的 action 集合（如 knowledge 早期不支持 revise，
            可传 ('approve', 'reject') 强制拒绝；现已统一支持三种）

    Returns:
        (action, comment, error_msg)
        - action: 'approve' / 'revise' / 'reject' 之一；解析失败为 None
        - comment: str（已 strip）
        - error_msg: 失败原因；成功为 None

    Examples:
        >>> parse_review_action({'action': 'revise', 'comment': '请补充示例'})
        ('revise', '请补充示例', None)
        >>> parse_review_action({'review_status': 'approved', 'review_comment': 'ok'})
        ('approve', 'ok', None)
        >>> parse_review_action({'action': 'xxx'})
        (None, '', 'action 必须为 approve / revise / reject ...')
    """
    if not data:
        return None, '', 'request body 为空（必须 application/json + action 字段）'

    raw = (
        data.get('action')
        or data.get('review_status')
        or data.get('status')
        or ''
    )
    raw = str(raw).strip().lower()
    action = ACTION_FROM_INPUT.get(raw)

    if action not in allowed_actions:
        return None, '', (
            f'action 必须为 {" / ".join(allowed_actions)}'
            '（也可用兼容字段 review_status: approved/revise/rejected）'
        )

    comment = (
        data.get('comment')
        or data.get('review_comment')
        or data.get('notes')
        or ''
    )
    comment = str(comment).strip()
    return action, comment, None


@api_bp.route('/review-comments', methods=['POST'])
def create_review_comment():
    """添加纯评论（不变更状态）。

    body: { resource_type, resource_id, content }
    author / author_type 由后端从会话/token 反推（不信任 body）。
    """
    data = request.get_json() or {}
    rt = data.get('resource_type')
    rid = data.get('resource_id')
    content = (data.get('content') or '').strip()

    if rt not in ('skill', 'rule', 'knowledge', 'testcase_library'):
        return jsonify({'error': 'resource_type 必须为 skill/rule/knowledge/testcase_library'}), 400
    try:
        rid = int(rid)
    except (TypeError, ValueError):
        return jsonify({'error': 'resource_id 必须为整数'}), 400
    if not content:
        return jsonify({'error': 'content 不能为空'}), 400

    if not _resource_exists(rt, rid):
        return jsonify({'error': f'{rt} #{rid} 不存在'}), 404

    author, author_type = _get_caller()
    if not author:
        return jsonify({'error': '请先登录'}), 401

    rc = add_review_comment(
        rt, rid, action='comment',
        content=content, author=author, author_type=author_type,
        commit=True,
    )
    return jsonify(rc.to_dict()), 201


@api_bp.route('/review-comments', methods=['GET'])
def list_review_comments():
    """获取某资源的完整评审时间线。

    query: resource_type=skill, resource_id=N
    """
    rt = request.args.get('resource_type')
    rid = request.args.get('resource_id')
    if rt not in ('skill', 'rule', 'knowledge', 'testcase_library'):
        return jsonify({'error': 'resource_type 非法'}), 400
    try:
        rid = int(rid)
    except (TypeError, ValueError):
        return jsonify({'error': 'resource_id 必须为整数'}), 400

    rows = (ReviewComment.query
            .filter_by(resource_type=rt, resource_id=rid)
            .order_by(ReviewComment.created_at.asc(),
                      ReviewComment.id.asc())
            .all())
    return jsonify([r.to_dict() for r in rows])


@api_bp.route('/review-comments/my-submissions', methods=['GET'])
def my_submissions():
    """聚合"我作为 created_by 提交的"所有资源 + 各自最新一条 ReviewComment 摘要。

    query: status=pending|revise|approved|rejected（可选筛选）
    返回: [{resource_type, resource, latest_comment, comment_count}, ...]
    """
    author, author_type = _get_caller()
    if not author:
        return jsonify({'error': '请先登录'}), 401

    status_filter = request.args.get('status')

    items = []

    # Skill: created_by == author
    sk_q = Skill.query.filter(
        Skill.created_by == author,
        db.or_(Skill.is_deleted == False, Skill.is_deleted == None),  # noqa: E711, E712
    )
    if status_filter:
        sk_q = sk_q.filter(Skill.review_status == status_filter)
    for s in sk_q.all():
        items.append(_pack('skill', s.id, s.display_name or s.name,
                           s.review_status or 'approved'))

    # Rule
    ru_q = Rule.query.filter(
        Rule.created_by == author,
        db.or_(Rule.is_deleted == False, Rule.is_deleted == None),  # noqa: E711, E712
    )
    if status_filter:
        ru_q = ru_q.filter(Rule.review_status == status_filter)
    for r in ru_q.all():
        items.append(_pack('rule', r.id, r.display_name or r.name,
                           r.review_status or 'approved'))

    # Knowledge: source_openclaw 名 == author（OpenClaw 提交）
    # 或 approved_by == author（已通过的，目前没记 created_by 字段）
    # Knowledge 模型未来可以补 submitted_by，先用 source_openclaw_name 兜底
    if author_type == 'openclaw':
        claw = OpenClawInstance.query.filter_by(name=author).first()
        if claw:
            kn_q = KnowledgeEntry.query.filter_by(source_openclaw_id=claw.id)
            if status_filter:
                # knowledge.status 用 pending_review 而不是 pending
                ks = ('pending_review' if status_filter == 'pending'
                      else status_filter)
                kn_q = kn_q.filter(KnowledgeEntry.status == ks)
            for k in kn_q.all():
                items.append(_pack('knowledge', k.id, k.title, k.status))

    # TestCaseLibrary: created_by/owner_username == author（按现有字段）
    try:
        tcl_q = TestCaseLibrary.query.filter(
            TestCaseLibrary.owner_username == author,
        )
        if status_filter:
            tcl_q = tcl_q.filter(TestCaseLibrary.review_status == status_filter)
        for t in tcl_q.all():
            items.append(_pack('testcase_library', t.id, t.name,
                               t.review_status or 'draft'))
    except Exception:
        pass  # owner_username 字段可能不存在，忽略

    items.sort(key=lambda x: x['updated_at'] or '', reverse=True)
    return jsonify(items)


def _pack(resource_type, resource_id, name, status):
    """打包单条资源 + 评审记录摘要。"""
    last = (ReviewComment.query
            .filter_by(resource_type=resource_type, resource_id=resource_id)
            .order_by(ReviewComment.created_at.desc(),
                      ReviewComment.id.desc())
            .first())
    cnt = (ReviewComment.query
           .filter_by(resource_type=resource_type, resource_id=resource_id)
           .count())
    return {
        'resource_type': resource_type,
        'resource_id': resource_id,
        'name': name,
        'status': status,
        'comment_count': cnt,
        'latest_comment': last.to_dict() if last else None,
        'updated_at': (str(last.created_at) if last and last.created_at
                       else None),
    }
