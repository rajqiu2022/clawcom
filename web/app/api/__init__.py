from flask import Blueprint, request, jsonify, session, g

api_bp = Blueprint('api', __name__)

# 不需要认证的公开路径（前缀匹配，使用绝对路径）
PUBLIC_PATHS = [
    '/api/v1/auth/login',          # 登录
    '/api/v1/auth/register',       # 注册
    '/api/v1/auth/woa-callback',   # WOA SSO 回调
    '/api/v1/system-changelog',    # 系统变更日志（公开）
    '/api/v1/test-reports/shared/',  # 测试报告分享外链匿名只读（MEMORY #134）
]

# Token 验证缓存（避免每次请求遍历所有 claw）
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

    if token in _token_claw_cache:
        return _token_claw_cache[token]

    # 遍历验证
    from app.models import OpenClawInstance
    for claw in OpenClawInstance.query.filter(OpenClawInstance.status != 'deleted').all():
        if claw.verify_token(token):
            _token_claw_cache[token] = claw
            return claw

    _token_claw_cache[token] = None
    return None


@api_bp.before_request
def require_auth():
    """全局 API 认证：所有 /api/v1/ 请求必须携带 Bearer Token 或 Web session 登录"""
    path = request.path

    # OPTIONS 预检请求放行
    if request.method == 'OPTIONS':
        return None

    # 公开路径放行
    for pub in PUBLIC_PATHS:
        if path.startswith(pub):
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


from app.api import openclaws, skills, knowledge, dashboard, projects, agent_hub, rules, ai_generator, testcases, reports, audit, system, tapd, auth, memos_api, todos, packs, snapshots, registration, uploads, openspace, topics, testplans, engineering, requirements, test_accounts, review_comments, wecom, agent_deployments, agent_templates, shared_articles, test_reports, panorama, exams, secrets  # noqa: F401

# 注册 Agent Hub 通信中心蓝图
api_bp.register_blueprint(agent_hub.agent_hub_bp, url_prefix='/agent-hub')

# 注意：agent_client.agent_bp 不在此处注册，已在 app/__init__.py 中直接注册到 app（url_prefix='/api/openclaws'）
# 在此处注册会导致路径嵌套错误（/api/v1/api/openclaws/）和路由冲突
