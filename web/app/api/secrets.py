"""Skill 密钥保险箱 API

权限模型：
- 页面列表（GET /secrets）：只返回 owner 自己的密钥（自己user + 自己名下claw）
- 读取明文（GET /secrets/{key}）：
    - owner 自己的密钥：直接返回
    - 共享密钥（share_scope=project/public）：只能通过 Bearer Token + key 读取，
      页面列表不可见，不可修改/删除
- 修改/删除：仅 owner
- share_scope 三种：private（默认）/ project（同项目 claw 可读）/ public（所有 claw 可读）
"""
import re
from datetime import datetime
from flask import request, jsonify, session
from app import db
from app.models import ClawSecret, OpenClawInstance, User, Project
from app.api import api_bp
from app.api.auth_utils import get_current_user, get_current_claw


SECRET_PLACEHOLDER_PATTERN = re.compile(r'\$\{SECRET:([a-zA-Z0-9_\-]{1,120})\}')


def _resolve_writer_owner():
    """解析「写入/创建」的 owner。返回 (claw_id, user_id, display)"""
    claw = get_current_claw()
    if claw:
        return claw.id, None, f'claw:{claw.name}'
    uid = session.get('user_id')
    if uid:
        u = User.query.get(uid)
        if u:
            return None, u.id, f'user:{u.username}'
    return None, None, None


def _get_visible_claw_ids_for_user(user):
    """返回当前 Web 用户名下所有 OpenClaw 的 id 列表"""
    if not user:
        return []
    rows = OpenClawInstance.query.filter(
        OpenClawInstance.owner == user.username,
        OpenClawInstance.deleted_at.is_(None),
    ).all()
    return [c.id for c in rows]


def _apply_owner_filter(query):
    """只返回 owner 自己的密钥（用于页面列表、修改、删除）。

    - Bearer Token：本 claw 的
    - Web Session：自己 user 的 + 自己名下所有 claw 的
    """
    claw = get_current_claw()
    if claw:
        return query.filter(ClawSecret.owner_claw_id == claw.id)

    uid = session.get('user_id')
    if uid:
        user = User.query.get(uid)
        if user:
            claw_ids = _get_visible_claw_ids_for_user(user)
            conds = [ClawSecret.owner_user_id == user.id]
            if claw_ids:
                conds.append(ClawSecret.owner_claw_id.in_(claw_ids))
            return query.filter(db.or_(*conds))

    return query.filter(db.literal(False))


def _find_readable_secret(key, secret_id=None):
    """查找当前请求可读取的 secret（含共享逻辑）。

    查找优先级：
    1. owner 自己的（精确匹配）
    2. 共享的（仅 Bearer Token 场景）：
       - share_scope='public'：所有 claw 可读
       - share_scope='project'：同 project_id 的 claw 可读
    """
    q = ClawSecret.query.filter(ClawSecret.key == key)
    if secret_id is not None:
        q = q.filter(ClawSecret.id == secret_id)

    # 先找 owner 自己的
    own = _apply_owner_filter(q).first()
    if own:
        return own, True  # (secret, is_owner)

    # 共享逻辑：仅 Bearer Token（claw）可用
    claw = get_current_claw()
    if not claw:
        return None, False

    # public 共享
    pub = q.filter(ClawSecret.share_scope == 'public').first()
    if pub:
        return pub, False

    # project 共享：调用方 claw 的 project_id 匹配
    if claw.project_id:
        proj = q.filter(
            ClawSecret.share_scope == 'project',
            ClawSecret.share_project_id == claw.project_id,
        ).first()
        if proj:
            return proj, False

    return None, False


def _find_readable_secrets_by_keys(keys):
    """批量查找可读 secrets（含共享），返回 {key: secret}。

    同名 key 优先取 owner 自己的，其次共享的。
    """
    if not keys:
        return {}

    all_q = ClawSecret.query.filter(ClawSecret.key.in_(keys))

    # owner 自己的
    own_secrets = _apply_owner_filter(all_q).all()
    result = {s.key: s for s in own_secrets}

    missing_keys = [k for k in keys if k not in result]
    if not missing_keys:
        return result

    # 共享的（仅 Bearer Token）
    claw = get_current_claw()
    if not claw:
        return result

    shared_q = ClawSecret.query.filter(ClawSecret.key.in_(missing_keys))

    # public
    for s in shared_q.filter(ClawSecret.share_scope == 'public').all():
        if s.key not in result:
            result[s.key] = s

    missing_keys = [k for k in missing_keys if k not in result]
    if not missing_keys and claw.project_id:
        return result

    # project
    if claw.project_id and missing_keys:
        for s in shared_q.filter(
            ClawSecret.share_scope == 'project',
            ClawSecret.share_project_id == claw.project_id,
        ).all():
            if s.key not in result:
                result[s.key] = s

    return result


def _current_actor_display():
    if get_current_claw():
        return True
    if session.get('user_id'):
        return True
    return False


def _is_super_admin():
    uid = session.get('user_id')
    if not uid:
        return False
    u = User.query.get(uid)
    return u and u.role == 'super_admin'


# ==================== 列表 ====================

@api_bp.route('/secrets', methods=['GET'])
def list_secrets():
    """列出当前 owner 自己的密钥（不含被共享给自己的）。

    页面上只能看到自己的，共享的密钥不在列表里（防止被随意查看）。
    super_admin ?all=1 可查看所有元信息（不含明文）。
    """
    show_all = request.args.get('all') in ('1', 'true', 'yes')
    if show_all:
        if not _is_super_admin():
            return jsonify({'error': '仅超级管理员可查看所有密钥'}), 403
        items = ClawSecret.query.order_by(ClawSecret.updated_at.desc()).all()
    else:
        if not _current_actor_display():
            return jsonify({'error': '未认证'}), 401
        items = _apply_owner_filter(ClawSecret.query).order_by(
            ClawSecret.owner_claw_id.asc(), ClawSecret.key.asc()
        ).all()
    return jsonify({'items': [s.to_dict(include_value=False) for s in items], 'total': len(items)})


# ==================== 创建 / 更新 ====================

@api_bp.route('/secrets', methods=['POST'])
def upsert_secret():
    """创建或更新一条 secret。

    请求体：{
      "key": "tavily_api_key",
      "value": "tvly-xxx",
      "description": "...",
      "owner_claw_id": 4,            // 可选，Web 用户指定名下 claw
      "share_scope": "private",      // private / project / public
      "share_project_id": 1          // share_scope=project 时必填
    }
    """
    claw_id, user_id, display = _resolve_writer_owner()
    if not display:
        return jsonify({'error': '未认证'}), 401

    data = request.get_json(force=True) or {}
    key = (data.get('key') or '').strip()
    value = data.get('value')
    description = (data.get('description') or '').strip() or None
    target_claw_id = data.get('owner_claw_id')
    share_scope = (data.get('share_scope') or 'private').strip()
    share_project_id = data.get('share_project_id')

    if not key:
        return jsonify({'error': 'key 必填'}), 400
    if not re.match(r'^[a-zA-Z0-9_\-]{1,120}$', key):
        return jsonify({'error': 'key 仅允许字母/数字/下划线/横线，1-120字符'}), 400
    if share_scope not in ('private', 'project', 'public'):
        return jsonify({'error': 'share_scope 仅支持 private/project/public'}), 400
    if share_scope == 'project':
        if not share_project_id:
            return jsonify({'error': 'share_scope=project 时 share_project_id 必填'}), 400
        try:
            share_project_id = int(share_project_id)
        except (TypeError, ValueError):
            return jsonify({'error': 'share_project_id 非法'}), 400
    else:
        share_project_id = None

    # Web 用户可指定写入到自己名下的某个 claw
    if user_id and target_claw_id:
        try:
            target_claw_id = int(target_claw_id)
        except (TypeError, ValueError):
            return jsonify({'error': 'owner_claw_id 非法'}), 400
        user = User.query.get(user_id)
        owned = OpenClawInstance.query.filter_by(
            id=target_claw_id, owner=user.username
        ).first()
        if not owned:
            return jsonify({'error': '只能写入到自己名下的 claw'}), 403
        claw_id = target_claw_id
        user_id = None

    existing = ClawSecret.query.filter_by(
        owner_claw_id=claw_id, owner_user_id=user_id, key=key
    ).first()

    if existing:
        if value:
            existing.set_value(value)
        if description is not None:
            existing.description = description
        existing.share_scope = share_scope
        existing.share_project_id = share_project_id
        action = 'updated'
        secret = existing
    else:
        if not value:
            return jsonify({'error': 'value 必填'}), 400
        secret = ClawSecret(
            owner_claw_id=claw_id,
            owner_user_id=user_id,
            key=key,
            description=description,
            share_scope=share_scope,
            share_project_id=share_project_id,
        )
        secret.set_value(value)
        db.session.add(secret)
        action = 'created'

    db.session.commit()
    result = secret.to_dict(include_value=False)
    result['action'] = action
    return jsonify(result), (201 if action == 'created' else 200)


# ==================== 读取明文 ====================

@api_bp.route('/secrets/<key>', methods=['GET'])
def get_secret(key):
    """获取 secret 明文。

    owner 自己的直接返回；
    共享的（public/project）只能通过 Bearer Token 读取。
    """
    if not _current_actor_display():
        return jsonify({'error': '未认证'}), 401

    sid = request.args.get('id', type=int)
    secret, is_owner = _find_readable_secret(key, secret_id=sid)
    if not secret:
        return jsonify({'error': f'secret "{key}" 不存在或无权限'}), 404

    secret.last_used_at = datetime.now()
    secret.use_count = (secret.use_count or 0) + 1
    db.session.commit()

    return jsonify(secret.to_dict(include_value=True))


@api_bp.route('/secrets/<key>', methods=['DELETE'])
def delete_secret(key):
    """删除 secret（仅 owner 可删除）"""
    if not _current_actor_display():
        return jsonify({'error': '未认证'}), 401

    sid = request.args.get('id', type=int)
    q = ClawSecret.query.filter(ClawSecret.key == key)
    if sid:
        q = q.filter(ClawSecret.id == sid)
    secret = _apply_owner_filter(q).first()
    if not secret:
        return jsonify({'error': f'secret "{key}" 不存在或无权限（仅 owner 可删除）'}), 404

    db.session.delete(secret)
    db.session.commit()
    return jsonify({'ok': True, 'deleted_key': key, 'deleted_id': secret.id})


# ==================== 占位符批量解析 ====================

@api_bp.route('/secrets/resolve', methods=['POST'])
def resolve_secrets():
    """根据文本中的 ${SECRET:xxx} 占位符批量返回明文。

    自动查找 owner 自己的 + 被共享给自己的（public/project），优先 owner 自己的。
    """
    if not _current_actor_display():
        return jsonify({'error': '未认证'}), 401

    data = request.get_json(force=True) or {}
    text = data.get('text') or ''
    if not text:
        return jsonify({'resolved': '', 'placeholders': [], 'missing': []})

    placeholders = list(set(SECRET_PLACEHOLDER_PATTERN.findall(text)))
    if not placeholders:
        return jsonify({'resolved': text, 'placeholders': [], 'missing': []})

    secret_map = _find_readable_secrets_by_keys(placeholders)

    resolved = text
    missing = []
    used_secrets = []
    for ph in placeholders:
        s = secret_map.get(ph)
        if s:
            resolved = resolved.replace('${SECRET:' + ph + '}', s.get_value())
            used_secrets.append(s)
        else:
            missing.append(ph)

    if used_secrets:
        for s in used_secrets:
            s.last_used_at = datetime.now()
            s.use_count = (s.use_count or 0) + 1
        db.session.commit()

    return jsonify({
        'resolved': resolved,
        'placeholders': placeholders,
        'used': [s.key for s in used_secrets],
        'missing': missing,
    })
