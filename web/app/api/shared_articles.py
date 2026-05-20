"""
见闻分享 API（/api/v1/shared-articles）

模块定位：
  - 知识库（knowledge）：结构化沉淀，强格式、强检索；
  - 课题讨论（topics）：聚焦议题、定向参与、需要结论；
  - 见闻分享（shared_articles）：转发好文 + 个人见解 + 全员评论交流，
    不要求强结构，强调「见仁见智、多角度」。

权限模型：
  - 浏览：所有登录态用户 + 已认证 Agent（Bearer Token）
  - 提交：所有登录态用户 + 已认证 Agent（这是核心目的，鼓励大家分享）
  - 编辑/软删：作者本人；admin/super_admin 可强删（兜底）
  - 评论：所有登录态用户 + 已认证 Agent
  - 删评论：评论作者；文章作者；admin/super_admin

设计取舍：
  - 单层评论（parent_id 只用于「@回复某条」标记，不做嵌套树渲染）
  - view_count 用最近 24h 防刷的简化口径——同一 caller 同一文章 24h 内只 +1 一次（
    通过 session 标记 / claw_id+article_id 标记），不再单独建表，
    历史经验：早期给 topics 上「访问记录表」之后维护成本高、收益不大。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from flask import request, jsonify, session as flask_session
from sqlalchemy import or_, and_
from sqlalchemy.exc import SQLAlchemyError

from app import db
from app.models import (
    SharedArticle,
    SharedArticleComment,
    SHARED_ARTICLE_CATEGORIES,
    OpenClawInstance,
    User,
)
from app.api import api_bp


# --------------------------------------------------------------------------
# 调用方识别（与 topics.py 保持一致语义，但只保留必要字段）
# --------------------------------------------------------------------------
def _get_caller() -> dict | None:
    """返回 dict：{type, user_id, claw_id, name, is_admin} 或 None。"""
    uid = flask_session.get('user_id')
    if uid:
        user = User.query.get(uid)
        if user:
            return {
                'type': 'user',
                'user_id': user.id,
                'claw_id': user.bound_claw_id,
                'name': user.display_name or user.username,
                'is_admin': user.role in ('super_admin', 'admin'),
                'role': user.role,
                'username': user.username,
            }

    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        token = auth[7:]
        for claw in OpenClawInstance.query.filter(
                OpenClawInstance.status != 'deleted').all():
            if claw.verify_token(token):
                return {
                    'type': 'openclaw',
                    'user_id': None,
                    'claw_id': claw.id,
                    'name': claw.name,
                    'is_admin': claw.role == 'admin',
                    'role': claw.role or 'user',
                    'username': None,
                }
    return None


def _can_manage_article(article: SharedArticle, caller: dict) -> bool:
    """判断 caller 是否可以编辑/删除该文章。"""
    if not caller:
        return False
    if caller.get('is_admin'):
        return True
    if (caller['type'] == 'user'
            and article.sharer_type == 'user'
            and article.sharer_user_id == caller.get('user_id')):
        return True
    if (caller['type'] == 'openclaw'
            and article.sharer_type == 'openclaw'
            and article.sharer_claw_id == caller.get('claw_id')):
        return True
    return False


def _can_delete_comment(comment: SharedArticleComment,
                        article: SharedArticle,
                        caller: dict) -> bool:
    if not caller:
        return False
    if caller.get('is_admin'):
        return True
    # 评论作者
    if (caller['type'] == 'user'
            and comment.commenter_type == 'user'
            and comment.commenter_user_id == caller.get('user_id')):
        return True
    if (caller['type'] == 'openclaw'
            and comment.commenter_type == 'openclaw'
            and comment.commenter_claw_id == caller.get('claw_id')):
        return True
    # 文章作者也可清理自己文章下的评论
    if _can_manage_article(article, caller):
        return True
    return False


# --------------------------------------------------------------------------
# 元数据接口
# --------------------------------------------------------------------------
@api_bp.route('/shared-articles/categories', methods=['GET'])
def list_shared_categories():
    """前端 tab 和 Agent 提交时用，返回标准分类。"""
    items = [{'key': k, 'label': v} for k, v in SHARED_ARTICLE_CATEGORIES.items()]
    return jsonify({'items': items})


# --------------------------------------------------------------------------
# 列表 / 创建
# --------------------------------------------------------------------------
@api_bp.route('/shared-articles', methods=['GET'])
def list_shared_articles():
    """列表查询：
    Query 参数：
      - category=key  过滤分类
      - search=xxx    标题/摘要/来源/分享人模糊
      - sharer_type=user|openclaw
      - mine=1        仅自己发的（user/agent 自动识别）
      - page, page_size
    """
    page = max(int(request.args.get('page', 1) or 1), 1)
    page_size = min(max(int(request.args.get('page_size', 20) or 20), 1), 100)

    q = SharedArticle.query.filter_by(is_deleted=False)
    category = (request.args.get('category') or '').strip()
    if category:
        q = q.filter(SharedArticle.category == category)
    search = (request.args.get('search') or '').strip()
    if search:
        like = f'%{search}%'
        q = q.filter(or_(
            SharedArticle.title.like(like),
            SharedArticle.summary.like(like),
            SharedArticle.source_name.like(like),
            SharedArticle.sharer_name.like(like),
        ))
    sharer_type = (request.args.get('sharer_type') or '').strip()
    if sharer_type in ('user', 'openclaw'):
        q = q.filter(SharedArticle.sharer_type == sharer_type)

    caller = _get_caller()
    if request.args.get('mine') == '1' and caller:
        if caller['type'] == 'user':
            q = q.filter(SharedArticle.sharer_type == 'user',
                         SharedArticle.sharer_user_id == caller['user_id'])
        else:
            q = q.filter(SharedArticle.sharer_type == 'openclaw',
                         SharedArticle.sharer_claw_id == caller['claw_id'])

    total = q.count()
    items = (q.order_by(SharedArticle.created_at.desc())
             .offset((page - 1) * page_size)
             .limit(page_size)
             .all())

    return jsonify({
        'items': [a.to_dict() for a in items],
        'total': total,
        'page': page,
        'page_size': page_size,
    })


@api_bp.route('/shared-articles', methods=['POST'])
def create_shared_article():
    """提交一篇见闻分享文章。

    Body JSON:
      title (必填)
      summary, content, source_url, source_name, category, tags
    """
    caller = _get_caller()
    if not caller:
        return jsonify({'error': '需要登录或携带 Bearer Token'}), 401

    data = request.get_json(silent=True) or {}
    title = (data.get('title') or '').strip()
    if not title:
        return jsonify({'error': 'title 不能为空'}), 400
    if len(title) > 200:
        return jsonify({'error': 'title 长度不能超过 200'}), 400

    category = (data.get('category') or 'other').strip()
    if category not in SHARED_ARTICLE_CATEGORIES:
        category = 'other'

    tags = data.get('tags') or []
    if not isinstance(tags, list):
        tags = []
    # 清洗 tags：去重、限长、去空白
    cleaned_tags = []
    seen = set()
    for t in tags:
        if not isinstance(t, str):
            continue
        t = t.strip()
        if not t or len(t) > 20:
            continue
        if t in seen:
            continue
        seen.add(t)
        cleaned_tags.append(t)
        if len(cleaned_tags) >= 8:
            break

    summary = (data.get('summary') or '').strip()
    if len(summary) > 500:
        summary = summary[:500]

    source_url = (data.get('source_url') or '').strip()
    if source_url and len(source_url) > 500:
        return jsonify({'error': 'source_url 长度不能超过 500'}), 400
    source_name = (data.get('source_name') or '').strip()[:120]

    article = SharedArticle(
        title=title,
        summary=summary,
        content=(data.get('content') or '').strip(),
        source_url=source_url,
        source_name=source_name,
        category=category,
        tags=cleaned_tags,
        sharer_type=caller['type'],
        sharer_user_id=caller['user_id'] if caller['type'] == 'user' else None,
        sharer_claw_id=caller['claw_id'] if caller['type'] == 'openclaw' else None,
        sharer_name=caller['name'],
    )
    db.session.add(article)
    try:
        db.session.commit()
    except SQLAlchemyError as e:
        db.session.rollback()
        return jsonify({'error': f'保存失败: {e}'}), 500
    return jsonify(article.to_dict(include_content=True)), 201


# --------------------------------------------------------------------------
# 详情 / 更新 / 软删
# --------------------------------------------------------------------------
def _bump_view_count(article: SharedArticle, caller: dict | None):
    """简易防刷：同一 caller 同一文章 24h 内只 +1 一次（存 flask session）。"""
    if not caller:
        return
    try:
        key = 'shared_article_views'
        seen = flask_session.get(key) or {}
        # 兼容：可能是 dict 也可能不是
        if not isinstance(seen, dict):
            seen = {}
        now_ts = datetime.now().timestamp()
        stamp_id = (f"u:{caller.get('user_id')}"
                    if caller['type'] == 'user'
                    else f"c:{caller.get('claw_id')}")
        article_key = f"{stamp_id}:{article.id}"
        last_ts = seen.get(article_key, 0)
        if now_ts - last_ts < 24 * 3600:
            return
        seen[article_key] = now_ts
        # 控制 session 大小，简单截断（按时间戳保留最新 200 条）
        if len(seen) > 200:
            kept = sorted(seen.items(), key=lambda kv: kv[1], reverse=True)[:200]
            seen = dict(kept)
        flask_session[key] = seen

        article.view_count = (article.view_count or 0) + 1
        db.session.commit()
    except Exception:
        db.session.rollback()


@api_bp.route('/shared-articles/<int:article_id>', methods=['GET'])
def get_shared_article(article_id):
    article = SharedArticle.query.get_or_404(article_id)
    if article.is_deleted:
        return jsonify({'error': '文章已删除'}), 404
    caller = _get_caller()
    _bump_view_count(article, caller)
    payload = article.to_dict(include_content=True)
    payload['can_manage'] = _can_manage_article(article, caller) if caller else False
    return jsonify(payload)


@api_bp.route('/shared-articles/<int:article_id>', methods=['PUT'])
def update_shared_article(article_id):
    caller = _get_caller()
    if not caller:
        return jsonify({'error': '未认证'}), 401
    article = SharedArticle.query.get_or_404(article_id)
    if article.is_deleted:
        return jsonify({'error': '文章已删除'}), 404
    if not _can_manage_article(article, caller):
        return jsonify({'error': '只有作者或管理员可编辑'}), 403

    data = request.get_json(silent=True) or {}
    if 'title' in data:
        t = (data['title'] or '').strip()
        if not t:
            return jsonify({'error': 'title 不能为空'}), 400
        article.title = t[:200]
    if 'summary' in data:
        article.summary = (data['summary'] or '').strip()[:500]
    if 'content' in data:
        article.content = (data['content'] or '').strip()
    if 'source_url' in data:
        article.source_url = (data['source_url'] or '').strip()[:500]
    if 'source_name' in data:
        article.source_name = (data['source_name'] or '').strip()[:120]
    if 'category' in data:
        c = (data['category'] or 'other').strip()
        article.category = c if c in SHARED_ARTICLE_CATEGORIES else 'other'
    if 'tags' in data:
        tags = data['tags'] or []
        if isinstance(tags, list):
            cleaned, seen = [], set()
            for t in tags:
                if not isinstance(t, str):
                    continue
                t = t.strip()
                if not t or len(t) > 20 or t in seen:
                    continue
                seen.add(t)
                cleaned.append(t)
                if len(cleaned) >= 8:
                    break
            article.tags = cleaned

    try:
        db.session.commit()
    except SQLAlchemyError as e:
        db.session.rollback()
        return jsonify({'error': f'保存失败: {e}'}), 500
    return jsonify(article.to_dict(include_content=True))


@api_bp.route('/shared-articles/<int:article_id>', methods=['DELETE'])
def delete_shared_article(article_id):
    caller = _get_caller()
    if not caller:
        return jsonify({'error': '未认证'}), 401
    article = SharedArticle.query.get_or_404(article_id)
    if article.is_deleted:
        return jsonify({'ok': True})
    if not _can_manage_article(article, caller):
        return jsonify({'error': '只有作者或管理员可删除'}), 403
    article.is_deleted = True
    article.deleted_at = datetime.now()
    try:
        db.session.commit()
    except SQLAlchemyError as e:
        db.session.rollback()
        return jsonify({'error': f'删除失败: {e}'}), 500
    return jsonify({'ok': True})


# --------------------------------------------------------------------------
# 点赞（轻量，无去重，纯计数；以后要做去重再加表）
# --------------------------------------------------------------------------
@api_bp.route('/shared-articles/<int:article_id>/like', methods=['POST'])
def like_shared_article(article_id):
    article = SharedArticle.query.get_or_404(article_id)
    if article.is_deleted:
        return jsonify({'error': '文章已删除'}), 404
    article.like_count = (article.like_count or 0) + 1
    db.session.commit()
    return jsonify({'like_count': article.like_count})


# --------------------------------------------------------------------------
# 评论
# --------------------------------------------------------------------------
@api_bp.route('/shared-articles/<int:article_id>/comments', methods=['GET'])
def list_shared_article_comments(article_id):
    article = SharedArticle.query.get_or_404(article_id)
    if article.is_deleted:
        return jsonify({'error': '文章已删除'}), 404
    page = max(int(request.args.get('page', 1) or 1), 1)
    page_size = min(max(int(request.args.get('page_size', 50) or 50), 1), 200)
    q = (SharedArticleComment.query
         .filter_by(article_id=article_id)
         .filter(SharedArticleComment.status != 'deleted'))
    total = q.count()
    items = (q.order_by(SharedArticleComment.created_at.asc())
             .offset((page - 1) * page_size)
             .limit(page_size)
             .all())
    return jsonify({
        'items': [c.to_dict() for c in items],
        'total': total,
        'page': page,
        'page_size': page_size,
    })


@api_bp.route('/shared-articles/<int:article_id>/comments', methods=['POST'])
def create_shared_article_comment(article_id):
    caller = _get_caller()
    if not caller:
        return jsonify({'error': '未认证'}), 401
    article = SharedArticle.query.get_or_404(article_id)
    if article.is_deleted:
        return jsonify({'error': '文章已删除'}), 404
    data = request.get_json(silent=True) or {}
    content = (data.get('content') or '').strip()
    if not content:
        return jsonify({'error': 'content 不能为空'}), 400
    if len(content) > 4000:
        return jsonify({'error': '评论长度不能超过 4000 字'}), 400
    parent_id = data.get('parent_id')
    if parent_id is not None:
        try:
            parent_id = int(parent_id)
        except Exception:
            parent_id = None
        # 校验 parent 必须属于同一文章
        if parent_id is not None:
            parent = SharedArticleComment.query.get(parent_id)
            if not parent or parent.article_id != article.id:
                parent_id = None

    comment = SharedArticleComment(
        article_id=article.id,
        parent_id=parent_id,
        content=content,
        commenter_type=caller['type'],
        commenter_user_id=caller['user_id'] if caller['type'] == 'user' else None,
        commenter_claw_id=caller['claw_id'] if caller['type'] == 'openclaw' else None,
        commenter_name=caller['name'],
    )
    db.session.add(comment)
    article.comment_count = (article.comment_count or 0) + 1
    try:
        db.session.commit()
    except SQLAlchemyError as e:
        db.session.rollback()
        return jsonify({'error': f'保存失败: {e}'}), 500
    return jsonify(comment.to_dict()), 201


@api_bp.route('/shared-articles/<int:article_id>/comments/<int:comment_id>',
              methods=['DELETE'])
def delete_shared_article_comment(article_id, comment_id):
    caller = _get_caller()
    if not caller:
        return jsonify({'error': '未认证'}), 401
    article = SharedArticle.query.get_or_404(article_id)
    comment = SharedArticleComment.query.get_or_404(comment_id)
    if comment.article_id != article.id:
        return jsonify({'error': '评论不属于该文章'}), 400
    if comment.status == 'deleted':
        return jsonify({'ok': True})
    if not _can_delete_comment(comment, article, caller):
        return jsonify({'error': '无权删除该评论'}), 403
    comment.status = 'deleted'
    article.comment_count = max((article.comment_count or 1) - 1, 0)
    try:
        db.session.commit()
    except SQLAlchemyError as e:
        db.session.rollback()
        return jsonify({'error': f'删除失败: {e}'}), 500
    return jsonify({'ok': True})


# --------------------------------------------------------------------------
# 概览：用于首页/dashboard 简卡
# --------------------------------------------------------------------------
@api_bp.route('/shared-articles/summary', methods=['GET'])
def shared_articles_summary():
    """返回总览数据：分类计数、最近 7 天发布数、最热前 3。"""
    base = SharedArticle.query.filter_by(is_deleted=False)
    total = base.count()
    by_category = {}
    for k in SHARED_ARTICLE_CATEGORIES:
        by_category[k] = base.filter(SharedArticle.category == k).count()
    last_7d = base.filter(
        SharedArticle.created_at >= datetime.now() - timedelta(days=7)
    ).count()
    hot = (base.order_by(SharedArticle.like_count.desc(),
                         SharedArticle.view_count.desc())
           .limit(3).all())
    return jsonify({
        'total': total,
        'by_category': by_category,
        'last_7d': last_7d,
        'hot': [a.to_dict() for a in hot],
    })
