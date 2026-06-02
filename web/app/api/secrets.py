"""Skill 密钥保险箱 API

让 Agent 把外部 API 凭据（Tavily Key、企微 Webhook、第三方 Token 等）
加密存到 Hub，Skill 内容里只写占位符 ${SECRET:key_name}，
运行时由 Agent 自己拿 Bearer Token 调 /secrets/{key} 取明文。

权限：
- 每个 claw / user 只能看到/读写自己的 secret（owner 隔离）
- super_admin 可看到所有 key 名（不返回明文），用于审计
"""
import re
from datetime import datetime
from flask import request, jsonify, session
from app import db
from app.models import ClawSecret, OpenClawInstance, User
from app.api import api_bp
from app.api.auth_utils import get_current_user, get_current_claw


SECRET_PLACEHOLDER_PATTERN = re.compile(r'\$\{SECRET:([a-zA-Z0-9_\-]{1,120})\}')


def _resolve_owner():
    """解析当前请求的 owner（claw 或 user）。

    返回 (owner_claw_id, owner_user_id, display)
    优先 Bearer Token 的 claw（agent 调），否则 Web session user。
    """
    claw = get_current_claw()
    if claw:
        return claw.id, None, f'claw:{claw.name}'
    uid = session.get('user_id')
    if uid:
        u = User.query.get(uid)
        if u:
            return None, u.id, f'user:{u.username}'
    return None, None, None


def _owner_filter(query):
    """根据当前请求的 owner 过滤 ClawSecret 查询"""
    claw_id, user_id, _ = _resolve_owner()
    if claw_id is not None:
        return query.filter_by(owner_claw_id=claw_id)
    if user_id is not None:
        return query.filter_by(owner_user_id=user_id)
    # 未认证
    return query.filter(db.literal(False))


def _is_super_admin():
    uid = session.get('user_id')
    if not uid:
        return False
    u = User.query.get(uid)
    return u and u.role == 'super_admin'


# ==================== 列表 ====================

@api_bp.route('/secrets', methods=['GET'])
def list_secrets():
    """列出当前 owner 的所有 secret（不返回明文）。

    super_admin 通过 ?all=1 可查看所有人的 key 元信息（仍不返回明文）。
    """
    show_all = request.args.get('all') in ('1', 'true', 'yes')
    if show_all:
        if not _is_super_admin():
            return jsonify({'error': '仅超级管理员可查看所有密钥'}), 403
        items = ClawSecret.query.order_by(ClawSecret.updated_at.desc()).all()
    else:
        claw_id, user_id, display = _resolve_owner()
        if not display:
            return jsonify({'error': '未认证'}), 401
        items = _owner_filter(ClawSecret.query).order_by(ClawSecret.key).all()
    return jsonify({'items': [s.to_dict(include_value=False) for s in items], 'total': len(items)})


# ==================== 创建 / 更新 ====================

@api_bp.route('/secrets', methods=['POST'])
def upsert_secret():
    """创建或更新一条 secret。

    请求体：{ "key": "tavily_api_key", "value": "tvly-xxx", "description": "..." }
    如果同 owner+key 已存在则更新 value/description；否则创建。
    """
    claw_id, user_id, display = _resolve_owner()
    if not display:
        return jsonify({'error': '未认证'}), 401

    data = request.get_json(force=True) or {}
    key = (data.get('key') or '').strip()
    value = data.get('value')
    description = (data.get('description') or '').strip() or None

    if not key:
        return jsonify({'error': 'key 必填'}), 400
    if not re.match(r'^[a-zA-Z0-9_\-]{1,120}$', key):
        return jsonify({'error': 'key 仅允许字母/数字/下划线/横线，1-120字符'}), 400

    existing = ClawSecret.query.filter_by(
        owner_claw_id=claw_id, owner_user_id=user_id, key=key
    ).first()

    if existing:
        # 编辑模式：value 可选（不传则保持原值，仅更新 description）
        if value:
            existing.set_value(value)
        if description is not None:
            existing.description = description
        action = 'updated'
        secret = existing
    else:
        # 创建模式：value 必填
        if not value:
            return jsonify({'error': 'value 必填'}), 400
        secret = ClawSecret(
            owner_claw_id=claw_id,
            owner_user_id=user_id,
            key=key,
            description=description,
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
    """获取 secret 明文（仅 owner 可调）"""
    claw_id, user_id, display = _resolve_owner()
    if not display:
        return jsonify({'error': '未认证'}), 401

    secret = ClawSecret.query.filter_by(
        owner_claw_id=claw_id, owner_user_id=user_id, key=key
    ).first()
    if not secret:
        return jsonify({'error': f'secret "{key}" 不存在'}), 404

    # 记录使用情况
    secret.last_used_at = datetime.now()
    secret.use_count = (secret.use_count or 0) + 1
    db.session.commit()

    return jsonify(secret.to_dict(include_value=True))


@api_bp.route('/secrets/<key>', methods=['DELETE'])
def delete_secret(key):
    claw_id, user_id, display = _resolve_owner()
    if not display:
        return jsonify({'error': '未认证'}), 401

    secret = ClawSecret.query.filter_by(
        owner_claw_id=claw_id, owner_user_id=user_id, key=key
    ).first()
    if not secret:
        return jsonify({'error': f'secret "{key}" 不存在'}), 404

    db.session.delete(secret)
    db.session.commit()
    return jsonify({'ok': True, 'deleted_key': key})


# ==================== 占位符批量解析 ====================

@api_bp.route('/secrets/resolve', methods=['POST'])
def resolve_secrets():
    """根据文本中的 ${SECRET:xxx} 占位符批量返回明文。

    请求体：{ "text": "Token: ${SECRET:tavily_api_key}\nWebhook: ${SECRET:wecom_xx}" }
    返回：{
      "resolved": "Token: tvly-xxx\nWebhook: https://...",
      "placeholders": ["tavily_api_key", "wecom_xx"],
      "missing": []
    }

    Agent 用 Bearer Token 调用，自动按 owner 解析。仅替换 owner 自己的 key，
    不存在的 key 保留原占位符并写到 missing 列表。
    """
    claw_id, user_id, display = _resolve_owner()
    if not display:
        return jsonify({'error': '未认证'}), 401

    data = request.get_json(force=True) or {}
    text = data.get('text') or ''
    if not text:
        return jsonify({'resolved': '', 'placeholders': [], 'missing': []})

    placeholders = list(set(SECRET_PLACEHOLDER_PATTERN.findall(text)))
    if not placeholders:
        return jsonify({'resolved': text, 'placeholders': [], 'missing': []})

    secrets = ClawSecret.query.filter(
        ClawSecret.key.in_(placeholders),
        ClawSecret.owner_claw_id == claw_id,
        ClawSecret.owner_user_id == user_id,
    ).all()
    secret_map = {s.key: s for s in secrets}

    resolved = text
    missing = []
    used_keys = []
    for ph in placeholders:
        s = secret_map.get(ph)
        if s:
            resolved = resolved.replace('${SECRET:' + ph + '}', s.get_value())
            used_keys.append(ph)
        else:
            missing.append(ph)

    # 记录使用情况
    if used_keys:
        for s in secrets:
            if s.key in used_keys:
                s.last_used_at = datetime.now()
                s.use_count = (s.use_count or 0) + 1
        db.session.commit()

    return jsonify({
        'resolved': resolved,
        'placeholders': placeholders,
        'used': used_keys,
        'missing': missing,
    })
