from flask import Blueprint, request, jsonify, session, g, current_app

api_bp = Blueprint('api', __name__)

# 不需要认证的公开路径（前缀匹配，使用绝对路径）
PUBLIC_PATHS = [
    '/api/v1/auth/login',          # 登录
    '/api/v1/auth/register',       # 注册
    '/api/v1/auth/woa-callback',   # WOA SSO 回调
    '/api/v1/system-changelog',    # 系统变更日志（公开）
    '/api/v1/test-reports/shared/',  # 测试报告分享外链匿名只读（MEMORY #134）
    '/api/v1/knowledge/shared/',    # 知识分享外链匿名只读与 Markdown 下载
    '/api/v1/collaboration-sessions/exchange',  # 有效协作链接签发/轮换短期 Token
    '/api/v1/collaboration-sessions/preview',  # 邀请持有者只读解析目标页面
    '/api/v1/chat-room-invites/exchange',  # 聊天室外部邀请兑换
    '/api/v1/chat-room-sessions/renew',     # 外部成员短期 Token 续期
]

# Token 验证缓存（避免每次请求遍历所有 claw）
# 只缓存 claw_id，不缓存 ORM 对象；gunicorn/gevent 下跨请求复用 ORM 对象
# 会在 Session 结束后变成 detached instance，后续访问字段触发 500。
_token_claw_cache = {}
_token_cache_ts = 0
_TOKEN_CACHE_TTL = 60  # 60秒缓存


def _verify_bearer_token(token):
    """验证 Bearer Token，返回对应的 OpenClawInstance 或 None"""
    import time
    global _token_claw_cache, _token_cache_ts

    # 快速路径：检查缓存
    now = time.time()
    if now - _token_cache_ts > _TOKEN_CACHE_TTL:
        _token_claw_cache.clear()
        _token_cache_ts = now

    from app.models import OpenClawInstance

    if token in _token_claw_cache:
        claw_id = _token_claw_cache[token]
        if not claw_id:
            return None
        return (OpenClawInstance.query
                .filter(OpenClawInstance.id == claw_id)
                .filter(OpenClawInstance.status != 'deleted')
                .first())

    # 遍历验证
    for claw in OpenClawInstance.query.filter(OpenClawInstance.status != 'deleted').all():
        if claw.verify_token(token):
            _token_claw_cache[token] = claw.id
            return claw

    _token_claw_cache[token] = None
    return None


@api_bp.before_request
def require_auth():
    """全局 API 认证：所有 /api/v1/ 请求必须携带 Bearer Token 或 Web session 登录"""
    # 测试客户端可能在外层复用 app context；显式清除请求级临时身份，避免上一
    # 请求的协作 Token 泄漏到后续 Web session。真实 WSGI 请求中同样是安全 no-op。
    for cache_key in ('_collaboration_session', '_chat_guest_session',
                      '_auth_claw', '_auth_user', '_auth_user_super'):
        g.pop(cache_key, None)
    path = request.path

    # OPTIONS 预检请求放行
    if request.method == 'OPTIONS':
        return None

    # 公开路径放行
    for pub in PUBLIC_PATHS:
        if path.startswith(pub):
            return None

    # Developer AI 临时协作 Token 使用独立前缀和路径白名单，不能被当作
    # OpenClaw Token 访问 Hub 其他 API。显式 Authorization 优先于 Web session，
    # 避免登录浏览器误把临时 Token 提升为用户权限。
    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer room_cs_'):
        if not current_app.config.get('CHAT_ROOM_ENABLED', False):
            return jsonify({'error': '聊天室能力未开启',
                            'code': 'CHAT_ROOM_DISABLED'}), 401
        from app.services.chat_rooms import (
            find_active_guest_session,
            guest_path_allowed,
        )
        if not guest_path_allowed(path, request.method):
            return jsonify({'error': '临时成员 Token 不允许访问该路径',
                            'code': 'CHAT_ROOM_PATH_DENIED'}), 403
        guest_session = find_active_guest_session(auth[7:])
        if not guest_session:
            return jsonify({'error': '临时成员 Token 无效、已过期或已撤销',
                            'code': 'CHAT_ROOM_TOKEN_INVALID'}), 401
        g._chat_guest_session = guest_session
        return None

    if auth.startswith('Bearer hub_cs_'):
        if not current_app.config.get('SHIFT_LEFT_ENABLED', False):
            return jsonify({'error': '临时协作能力未开启',
                            'code': 'SHIFT_LEFT_DISABLED'}), 401
        token = auth[7:]
        from app import db
        from app.models import CollaborationSession
        from app.services.shift_left import (
            collaboration_path_allowed,
            find_active_collaboration_session,
            now_cst_naive,
        )
        if not collaboration_path_allowed(path, request.method):
            return jsonify({'error': '临时协作 Token 不允许访问该路径',
                            'code': 'COLLABORATION_PATH_DENIED'}), 403
        collaboration = find_active_collaboration_session(token)
        if not collaboration:
            return jsonify({'error': '临时协作 Token 无效、已过期或已撤销',
                            'code': 'COLLABORATION_TOKEN_INVALID'}), 401
        now = now_cst_naive()
        updated = (CollaborationSession.query
                   .filter(
                       CollaborationSession.id == collaboration.id,
                       CollaborationSession.status == 'active',
                       CollaborationSession.call_count <
                       CollaborationSession.max_calls,
                   )
                   .update({
                       CollaborationSession.call_count:
                           CollaborationSession.call_count + 1,
                       CollaborationSession.last_activity_at: now,
                   }, synchronize_session=False))
        if not updated:
            db.session.rollback()
            return jsonify({'error': '临时协作调用预算已耗尽',
                            'code': 'COLLABORATION_BUDGET_EXHAUSTED'}), 429
        db.session.commit()
        g._collaboration_session = db.session.get(
            CollaborationSession, collaboration.id)
        return None

    # OpenClaw 注册 bootstrap 链接需要在 Agent 尚未接入前可访问；
    # bootstrap.sh / offline-install-bundle.sh 内部仍会校验 query token，
    # registration-skill 只用于发给目标 Agent。
    if path.startswith('/api/v1/openclaws/') and (
            path.endswith('/registration-skill')
            or path.endswith('/bootstrap.sh')
            or path.endswith('/offline-install-bundle.sh')):
        return None

    # Web session 登录
    uid = session.get('user_id')
    if uid:
        from app.models import User
        from app.services.project_access import (
            ensure_user_projects_initialized,
            is_project_required_path,
            user_has_project_access,
        )
        from app.services.review_visibility import is_project_exempt_review_path
        user = User.query.get(uid)
        if user:
            ensure_user_projects_initialized(user, session)
            # 完全公开（visibility=public_all）的评审要让无项目权限的用户也能看和评论，
            # 因此对少量评审读/回复接口豁免这道粗门禁。逐条可见性判定仍在 topics.py 执行，
            # 无项目用户依然拿不到 public / project / assigned 课题。
            if (not user_has_project_access(user)
                    and is_project_required_path(path)
                    and not is_project_exempt_review_path(path, request.method)):
                return jsonify({
                    'error': '请开通项目后再使用',
                    'code': 'PROJECT_REQUIRED',
                }), 403
        return None

    # Bearer Token 认证
    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        token = auth[7:]
        if token:
            claw = _verify_bearer_token(token)
            if claw:
                # 缓存到 flask.g，下游模块直接读取，无需重复验证
                g._auth_claw = claw
                return None
            return jsonify({'error': 'Token 无效，请检查 Authorization 头'}), 401

    # 既没 session 也没 Token
    return jsonify({'error': '未认证，请在 Header 中携带 Authorization: Bearer {TOKEN} 或先登录 Web'}), 401


from app.api import openclaws, skills, knowledge, dashboard, projects, agent_hub, rules, ai_generator, testcases, reports, audit, system, tapd, auth, memos_api, todos, packs, snapshots, registration, uploads, openspace, topics, testplans, engineering, requirements, test_accounts, review_comments, wecom, agent_deployments, agent_templates, shared_articles, test_reports, panorama, testcase_panorama_links, exams, secrets, workflows, workflow_missions, agent_artifacts, mission_stages, mission_handoffs, agent_eval, tasks_context, ops_verify, memories, shift_left, automation_candidates, automation_capabilities, testcase_promotions, resource_leases, entity_relations, analysis_rules, automation_closed_loop, agent_control, chat_rooms, worker_releases  # noqa: F401

# 注册 Agent Hub 通信中心蓝图
api_bp.register_blueprint(agent_hub.agent_hub_bp, url_prefix='/agent-hub')

# 注意：agent_client.agent_bp 不在此处注册，已在 app/__init__.py 中直接注册到 app（url_prefix='/api/openclaws'）
# 在此处注册会导致路径嵌套错误（/api/v1/api/openclaws/）和路由冲突
