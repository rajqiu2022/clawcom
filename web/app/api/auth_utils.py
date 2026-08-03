"""
统一认证工具模块

将分散在 13+ 个文件中的认证函数收敛到一处：
- get_current_user()：Web session + Bearer Token → User 或 Proxy
- get_current_claw()：Bearer Token → OpenClawInstance
- require_claw_token：统一装饰器（用于 claw 自身调用的 API）
- is_admin_user()：判断请求方是否 admin/super_admin
- user_project_ids()：获取用户关联项目 ID 集合
- safe_commit()：统一 commit + rollback 包装

所有模块应从此处 import，不再自行定义。
"""

from functools import wraps
from flask import request, jsonify, session, g
from sqlalchemy.exc import SQLAlchemyError
from app import db


def _get_bearer_token():
    """从 Authorization header 提取 Bearer Token"""
    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        return auth[7:]
    return None


def _resolve_claw_from_token(token):
    """通过全局缓存验证 token，返回 OpenClawInstance 或 None
    复用 __init__.py 的 60s 缓存，避免重复遍历。
    """
    from app.api import _verify_bearer_token
    return _verify_bearer_token(token)


def get_current_claw():
    """从 Bearer Token 获取 OpenClawInstance，结果缓存到 flask.g"""
    if hasattr(g, '_auth_claw'):
        return g._auth_claw
    token = _get_bearer_token()
    if not token:
        g._auth_claw = None
        return None
    claw = _resolve_claw_from_token(token)
    g._auth_claw = claw
    return claw


def get_current_user():
    """获取当前用户（支持 Web session 和 OpenClaw Bearer Token）
    
    - Web session → 直接返回 User 对象
    - Bearer Token + admin claw → 返回 _ClawAdminProxy（role='admin'）
    - Bearer Token + 非 admin claw → 返回 owner User（附带 _claw_name/_claw_id）
    - 无凭证 → None
    
    结果缓存到 flask.g，同一请求不重复查询。
    """
    if hasattr(g, '_auth_user'):
        return g._auth_user

    from app.models import User, OpenClawInstance

    # 优先 Web session
    uid = session.get('user_id')
    if uid:
        user = User.query.get(uid)
        g._auth_user = user
        return user

    # Bearer Token
    claw = get_current_claw()
    if claw:
        if claw.role == 'admin':
            is_global_claw = claw.project_id is None

            class _ClawAdminProxy:
                role = 'admin'
                username = claw.name
                managed_projects = [] if is_global_claw else ([claw.project_id] if claw.project_id else [])
                bound_claw_id = claw.id
                is_global = is_global_claw
                _claw_id = claw.id
                _claw_name = claw.name
            g._auth_user = _ClawAdminProxy()
            return g._auth_user

        # 非 admin claw → 返回 owner User
        owner = User.query.filter_by(username=claw.owner).first()
        if owner:
            owner._claw_name = claw.name
            owner._claw_id = claw.id
            g._auth_user = owner
            return owner

    g._auth_user = None
    return None


def get_current_user_as_super_admin():
    """与 get_current_user 相同，但 admin claw 映射为 super_admin（知识库模块需要）"""
    if hasattr(g, '_auth_user_super'):
        return g._auth_user_super

    from app.models import User

    uid = session.get('user_id')
    if uid:
        user = User.query.get(uid)
        g._auth_user_super = user
        return user

    claw = get_current_claw()
    if claw:
        if claw.role == 'admin':
            class _AdminProxy:
                role = 'super_admin'
                username = claw.name
                managed_projects = []
                _claw_id = claw.id
                _claw_name = claw.name
            g._auth_user_super = _AdminProxy()
            return g._auth_user_super

        from app.models import User as UserModel
        owner = UserModel.query.filter_by(username=claw.owner).first()
        if owner:
            owner._claw_name = claw.name
            owner._claw_id = claw.id
            g._auth_user_super = owner
            return owner

    g._auth_user_super = None
    return None


def is_admin_user():
    """当前请求方是否 admin/super_admin（Web session 或 admin claw Token）"""
    uid = session.get('user_id')
    if uid:
        from app.models import User
        user = User.query.get(uid)
        if user and user.role in ('super_admin', 'admin'):
            return True

    claw = get_current_claw()
    if claw and claw.role == 'admin':
        return True
    return False


def user_project_ids(user):
    """获取用户关联的项目 ID 集合"""
    from app.models import OpenClawInstance, Project
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


def require_claw_token(f):
    """统一 OpenClaw API Token 认证装饰器

    用于 OpenClaw 自身调用的接口（heartbeat、report、config 等）。
    优先验证 URL 中 claw_id 对应的 Token；
    回退时验证 token 对应的 claw，但强制校验 claw_id 一致性（防越权）。
    """
    from app import db
    from app.models import OpenClawInstance

    @wraps(f)
    def decorated(claw_id, *args, **kwargs):
        claw = db.get_or_404(OpenClawInstance, claw_id)
        token = _get_bearer_token()
        if not token:
            return jsonify({'error': '缺少认证 Token'}), 401

        # 优先匹配 URL 中 claw_id 对应的 Token
        if claw.verify_token(token):
            return f(claw_id, claw=claw, *args, **kwargs)

        # 回退：通过缓存验证 token，但校验身份一致性
        verified_claw = _resolve_claw_from_token(token)
        if verified_claw:
            # 安全校验：token 对应的 claw 必须与 URL 中的 claw_id 一致
            # 否则可能存在越权（Token A 访问 claw B 的接口）
            if verified_claw.id == claw_id:
                return f(claw_id, claw=verified_claw, *args, **kwargs)
            # 日志记录越权尝试
            import logging
            logging.getLogger(__name__).warning(
                'Token claw_id=%s tried to access claw_id=%s, denied',
                verified_claw.id, claw_id
            )
        return jsonify({'error': 'Token 无效或不匹配'}), 403
    return decorated


def safe_commit(error_msg='操作失败'):
    """统一 commit + rollback + JSON 错误响应。
    
    用法：
        err = safe_commit('保存失败')
        if err:
            return err
    
    返回 None 表示成功，否则返回 (jsonify, status_code) 元组。
    """
    try:
        db.session.commit()
        return None
    except SQLAlchemyError as e:
        db.session.rollback()
        import logging
        logging.getLogger(__name__).exception('DB commit failed: %s', error_msg)
        return jsonify({'error': f'{error_msg}: {e}'}), 500
