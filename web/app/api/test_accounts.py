"""测试账号管理 API（仅后端 + Skill 暴露给 OpenClaw 使用，不进 hub Web 页面）。

路由前缀：/api/v1/test-accounts

权限模型：
    - 列表 / 单查 / acquire / release / mark-abnormal / usage-history：
        任意通过 `before_request` 全局认证（Web session 或 Bearer Token）的调用方均可。
    - create / update / delete：
        仅 super_admin 用户  或  admin 角色 OpenClaw（龙虾王）token。
        其他角色一律 403。
"""
from datetime import datetime

from flask import request, jsonify, session as flask_session
from sqlalchemy import or_

from app import db
from app.models import (
    OpenClawInstance,
    TestAccount,
    TestAccountUsageLog,
    User,
)
from app.api import api_bp


# ---------------------------------------------------------------------------
# 鉴权辅助
# ---------------------------------------------------------------------------

def _get_session_user():
    uid = flask_session.get('user_id')
    if not uid:
        return None
    return User.query.get(uid)


def _get_token_claw():
    auth = request.headers.get('Authorization', '')
    if not auth.startswith('Bearer '):
        return None
    token = auth[7:]
    if not token:
        return None
    for claw in OpenClawInstance.query.filter(
            OpenClawInstance.status != 'deleted').all():
        if claw.verify_token(token):
            return claw
    return None


def _resolve_actor():
    """返回 (actor_type, actor_name, actor_claw_id, actor_user_id, is_admin)。

    is_admin: 是否拥有 create/delete 权限（super_admin 或 admin claw）。
    """
    user = _get_session_user()
    if user:
        actor_name = user.display_name or user.username
        is_admin = user.role == 'super_admin'
        return ('user', actor_name, None, user.id, is_admin)

    claw = _get_token_claw()
    if claw:
        is_admin = claw.role == 'admin'
        return ('claw', claw.name, claw.id, None, is_admin)

    return (None, '', None, None, False)


def _require_actor():
    actor_type, actor_name, claw_id, user_id, is_admin = _resolve_actor()
    if actor_type is None:
        # before_request 已挡过，但兜底返回 401
        return None, jsonify({'error': '未认证'}), 401
    return (actor_type, actor_name, claw_id, user_id, is_admin), None, None


def _require_admin():
    info, err, code = _require_actor()
    if err is not None:
        return None, err, code
    actor_type, actor_name, claw_id, user_id, is_admin = info
    if not is_admin:
        return None, jsonify({
            'error': '仅超级管理员或龙虾王（admin claw）可执行此操作',
        }), 403
    return info, None, None


def _add_log(account, action, actor_type, actor_name,
             claw_id, user_id, purpose='', extra=None):
    log = TestAccountUsageLog(
        account_id=account.id,
        action=action,
        actor_type=actor_type or 'claw',
        actor_name=actor_name or '',
        actor_claw_id=claw_id,
        actor_user_id=user_id,
        purpose=purpose or '',
        extra=extra or {},
    )
    db.session.add(log)
    return log


def _account_or_404(account_id, allow_deleted=False):
    q = TestAccount.query.filter_by(id=account_id)
    if not allow_deleted:
        q = q.filter_by(is_deleted=False)
    return q.first()


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

@api_bp.route('/test-accounts', methods=['GET'])
def list_test_accounts():
    """列出测试账号。

    查询参数（均可选）：
      platform=qq|wechat|other
      status=idle|in_use|abnormal
      keyword=xxx                  account / notes / holder_name 模糊匹配
      include_deleted=true         默认 false
      include_password=true        默认 false（即使是 admin 也需显式声明）
      limit=50, offset=0
    """
    info, err, code = _require_actor()
    if err is not None:
        return err, code
    actor_type, actor_name, claw_id, user_id, is_admin = info

    platform = request.args.get('platform', '').strip().lower() or None
    status = request.args.get('status', '').strip().lower() or None
    keyword = request.args.get('keyword', '').strip() or None
    include_deleted = request.args.get(
        'include_deleted', 'false').lower() == 'true'
    include_password = (
        request.args.get('include_password', 'false').lower() == 'true'
        and is_admin
    )
    try:
        limit = max(1, min(int(request.args.get('limit', 100)), 500))
    except Exception:
        limit = 100
    try:
        offset = max(0, int(request.args.get('offset', 0)))
    except Exception:
        offset = 0

    q = TestAccount.query
    if not include_deleted:
        q = q.filter_by(is_deleted=False)
    if platform:
        q = q.filter_by(platform=platform)
    if status:
        q = q.filter_by(status=status)
    if keyword:
        like = f'%{keyword}%'
        q = q.filter(or_(TestAccount.account.like(like),
                         TestAccount.notes.like(like),
                         TestAccount.holder_name.like(like),
                         TestAccount.current_purpose.like(like)))
    total = q.count()
    rows = (q.order_by(TestAccount.platform.asc(),
                       TestAccount.id.asc())
            .limit(limit).offset(offset).all())
    return jsonify({
        'total': total,
        'limit': limit,
        'offset': offset,
        'items': [r.to_dict(include_password=include_password) for r in rows],
    })


@api_bp.route('/test-accounts/<int:account_id>', methods=['GET'])
def get_test_account(account_id):
    info, err, code = _require_actor()
    if err is not None:
        return err, code
    _, _, _, _, is_admin = info

    include_password = (
        request.args.get('include_password', 'false').lower() == 'true'
        and is_admin
    )
    account = _account_or_404(account_id, allow_deleted=is_admin)
    if not account:
        return jsonify({'error': '账号不存在'}), 404
    return jsonify(account.to_dict(include_password=include_password))


# ---------------------------------------------------------------------------
# 领用 / 释放 / 异常
# ---------------------------------------------------------------------------

@api_bp.route('/test-accounts/<int:account_id>/acquire', methods=['POST'])
def acquire_test_account(account_id):
    """领用一个空闲账号，返回密码。

    请求体：
      { "purpose": "登录验证", "force": false }

    purpose: 必填，至少 2 个字符；用作 current_purpose 与日志 purpose。
    force=true: 即使当前 in_use 也强制抢占（仅 admin 可用）。
    """
    info, err, code = _require_actor()
    if err is not None:
        return err, code
    actor_type, actor_name, claw_id, user_id, is_admin = info

    data = request.get_json(silent=True) or {}
    purpose = (data.get('purpose') or '').strip()
    force = bool(data.get('force')) and is_admin

    if len(purpose) < 2:
        return jsonify({'error': 'purpose（使用途径/备注）必填且至少 2 个字符'}), 400

    account = _account_or_404(account_id)
    if not account:
        return jsonify({'error': '账号不存在'}), 404

    if account.status == 'in_use' and not force:
        return jsonify({
            'error': '账号正在被使用',
            'current_user': account.holder_name or '',
            'current_purpose': account.current_purpose or '',
            'last_login_at': str(account.last_login_at) if account.last_login_at else None,
            'hint': '可换一个空闲账号；admin 可加 force=true 强制抢占',
        }), 409

    if account.status == 'abnormal':
        return jsonify({
            'error': '账号当前为异常状态，请先恢复后再领用',
            'hint': 'POST /api/v1/test-accounts/{id}/recover 由 admin 处理',
        }), 409

    now = datetime.now()
    prev_user = account.holder_name or ''
    account.status = 'in_use'
    account.holder_name = actor_name
    account.current_claw_id = claw_id
    account.current_purpose = purpose
    account.last_login_at = now

    extra = {}
    if force and prev_user:
        extra['preempted_from'] = prev_user
    _add_log(account, 'acquire', actor_type, actor_name,
             claw_id, user_id, purpose=purpose, extra=extra)
    db.session.commit()

    payload = account.to_dict(include_password=True)
    payload['acquired_at'] = str(now)
    return jsonify(payload)


@api_bp.route('/test-accounts/<int:account_id>/release', methods=['POST'])
def release_test_account(account_id):
    """释放账号。

    请求体（可选）：
      { "summary": "登录用例已完成" }

    任何已认证调用方都可释放（设计上面向自助归还）。
    如果释放者既不是当前占用者也不是 admin，会被拒绝以避免误踢。
    """
    info, err, code = _require_actor()
    if err is not None:
        return err, code
    actor_type, actor_name, claw_id, user_id, is_admin = info

    data = request.get_json(silent=True) or {}
    summary = (data.get('summary') or '').strip()

    account = _account_or_404(account_id)
    if not account:
        return jsonify({'error': '账号不存在'}), 404

    if account.status == 'idle':
        return jsonify({
            'message': '账号本就是空闲状态，无需释放',
            'account': account.to_dict(),
        })

    is_self = (
        (claw_id and account.current_claw_id == claw_id)
        or (actor_name and account.holder_name == actor_name)
    )
    if not (is_self or is_admin):
        return jsonify({
            'error': '只能释放自己当前占用的账号；如需强制释放请联系超级管理员或龙虾王',
            'current_user': account.holder_name or '',
        }), 403

    extra = {'summary': summary} if summary else {}
    if not is_self and is_admin:
        extra['released_by_admin'] = actor_name

    account.status = 'idle'
    account.holder_name = ''
    account.current_claw_id = None
    account.current_purpose = ''
    _add_log(account, 'release', actor_type, actor_name,
             claw_id, user_id, purpose=summary, extra=extra)
    db.session.commit()
    return jsonify({
        'message': '已释放',
        'account': account.to_dict(),
    })


@api_bp.route('/test-accounts/<int:account_id>/mark-abnormal', methods=['POST'])
def mark_test_account_abnormal(account_id):
    """把账号标记为异常（如登录失败、被风控）。

    请求体：
      { "reason": "登录返回需要扫码", "release": true }

    release=true 时同步把 holder_name 清空（默认 true）。
    """
    info, err, code = _require_actor()
    if err is not None:
        return err, code
    actor_type, actor_name, claw_id, user_id, is_admin = info

    data = request.get_json(silent=True) or {}
    reason = (data.get('reason') or '').strip()
    do_release = data.get('release', True)

    if len(reason) < 2:
        return jsonify({'error': 'reason 必填且至少 2 个字符'}), 400

    account = _account_or_404(account_id)
    if not account:
        return jsonify({'error': '账号不存在'}), 404

    account.status = 'abnormal'
    if do_release:
        account.holder_name = ''
        account.current_claw_id = None
        account.current_purpose = ''

    _add_log(account, 'mark_abnormal', actor_type, actor_name,
             claw_id, user_id, purpose=reason,
             extra={'reason': reason, 'released': bool(do_release)})
    db.session.commit()
    return jsonify({
        'message': '已标记为异常',
        'account': account.to_dict(),
    })


@api_bp.route('/test-accounts/<int:account_id>/recover', methods=['POST'])
def recover_test_account(account_id):
    """把异常账号恢复为 idle（仅 super_admin / admin claw）。"""
    info, err, code = _require_admin()
    if err is not None:
        return err, code
    actor_type, actor_name, claw_id, user_id, _ = info

    data = request.get_json(silent=True) or {}
    note = (data.get('note') or '').strip()

    account = _account_or_404(account_id)
    if not account:
        return jsonify({'error': '账号不存在'}), 404

    account.status = 'idle'
    account.holder_name = ''
    account.current_claw_id = None
    account.current_purpose = ''
    _add_log(account, 'recover', actor_type, actor_name,
             claw_id, user_id, purpose=note,
             extra={'note': note} if note else {})
    db.session.commit()
    return jsonify({'message': '已恢复为空闲', 'account': account.to_dict()})


# ---------------------------------------------------------------------------
# 创建 / 更新 / 删除（仅 super_admin / admin claw）
# ---------------------------------------------------------------------------

@api_bp.route('/test-accounts', methods=['POST'])
def create_test_account():
    """创建测试账号。仅 super_admin / admin claw 可调。

    请求体：
      {
        "platform": "qq",                  # qq / wechat / other
        "account": "1234567",
        "password": "xxxx",
        "notes": "广州号-小天专用"          # 可选
      }
    """
    info, err, code = _require_admin()
    if err is not None:
        return err, code
    actor_type, actor_name, claw_id, user_id, _ = info

    data = request.get_json(silent=True) or {}
    platform = (data.get('platform') or '').strip().lower()
    account_str = (data.get('account') or '').strip()
    password = data.get('password') or ''
    notes = (data.get('notes') or '').strip()

    if platform not in ('qq', 'wechat', 'other'):
        return jsonify({'error': 'platform 必须是 qq / wechat / other'}), 400
    if not account_str:
        return jsonify({'error': 'account 必填'}), 400
    if not password:
        return jsonify({'error': 'password 必填'}), 400

    existing = TestAccount.query.filter_by(
        platform=platform, account=account_str, is_deleted=False).first()
    if existing:
        return jsonify({
            'error': '该平台下已存在同名账号',
            'existing_id': existing.id,
        }), 409

    account = TestAccount(
        platform=platform,
        account=account_str,
        password=password,
        status='idle',
        notes=notes,
        created_by=actor_name,
    )
    db.session.add(account)
    db.session.flush()
    _add_log(account, 'create', actor_type, actor_name,
             claw_id, user_id, purpose='', extra={'platform': platform})
    db.session.commit()
    return jsonify(account.to_dict(include_password=False)), 201


@api_bp.route('/test-accounts/<int:account_id>', methods=['PUT', 'PATCH'])
def update_test_account(account_id):
    """更新账号字段（password/notes/platform/account）。仅 super_admin / admin claw。

    请求体：
      { "password": "...", "notes": "...", "platform": "qq", "account": "..." }
    """
    info, err, code = _require_admin()
    if err is not None:
        return err, code
    actor_type, actor_name, claw_id, user_id, _ = info

    account = _account_or_404(account_id, allow_deleted=True)
    if not account:
        return jsonify({'error': '账号不存在'}), 404

    data = request.get_json(silent=True) or {}
    changed = {}

    if 'password' in data and data['password']:
        account.password = data['password']
        changed['password'] = '***changed***'
    if 'notes' in data:
        account.notes = (data.get('notes') or '').strip()
        changed['notes'] = account.notes
    if 'platform' in data and data['platform']:
        new_platform = data['platform'].strip().lower()
        if new_platform not in ('qq', 'wechat', 'other'):
            return jsonify({'error': 'platform 必须是 qq / wechat / other'}), 400
        account.platform = new_platform
        changed['platform'] = new_platform
    if 'account' in data and data['account']:
        new_account = data['account'].strip()
        # 唯一性
        dup = TestAccount.query.filter(
            TestAccount.platform == account.platform,
            TestAccount.account == new_account,
            TestAccount.id != account.id,
            TestAccount.is_deleted == False,  # noqa: E712
        ).first()
        if dup:
            return jsonify({'error': '该平台下已存在同名账号'}), 409
        account.account = new_account
        changed['account'] = new_account

    if not changed:
        return jsonify({'message': '无字段变更', 'account': account.to_dict()})

    _add_log(account, 'create', actor_type, actor_name,
             claw_id, user_id, purpose='update',
             extra={'changed_fields': list(changed.keys())})
    db.session.commit()
    return jsonify({'message': '已更新', 'changed': changed,
                    'account': account.to_dict()})


@api_bp.route('/test-accounts/<int:account_id>', methods=['DELETE'])
def delete_test_account(account_id):
    """软删除账号。仅 super_admin / admin claw。"""
    info, err, code = _require_admin()
    if err is not None:
        return err, code
    actor_type, actor_name, claw_id, user_id, _ = info

    account = _account_or_404(account_id)
    if not account:
        return jsonify({'error': '账号不存在'}), 404

    account.is_deleted = True
    account.deleted_at = datetime.now()
    account.deleted_by = actor_name
    if account.status == 'in_use':
        # 占用中的账号被删 → 同时记录释放
        _add_log(account, 'release', actor_type, actor_name,
                 claw_id, user_id, purpose='deleted_while_in_use',
                 extra={'previous_user': account.holder_name or ''})
        account.status = 'idle'
        account.holder_name = ''
        account.current_claw_id = None
        account.current_purpose = ''
    _add_log(account, 'delete', actor_type, actor_name,
             claw_id, user_id, purpose='', extra={})
    db.session.commit()
    return jsonify({'message': '已删除', 'account_id': account_id})


# ---------------------------------------------------------------------------
# 使用历史（最近 10 次）
# ---------------------------------------------------------------------------

@api_bp.route('/test-accounts/<int:account_id>/usage-history', methods=['GET'])
def get_test_account_usage_history(account_id):
    """获取账号最近的使用流水。

    查询参数（可选）：
      limit=10        默认 10，最大 100
      action=acquire|release|mark_abnormal|recover|create|delete
    """
    info, err, code = _require_actor()
    if err is not None:
        return err, code

    try:
        limit = max(1, min(int(request.args.get('limit', 10)), 100))
    except Exception:
        limit = 10
    action = request.args.get('action', '').strip().lower() or None

    account = _account_or_404(account_id, allow_deleted=True)
    if not account:
        return jsonify({'error': '账号不存在'}), 404

    q = TestAccountUsageLog.query.filter_by(account_id=account_id)
    if action:
        q = q.filter_by(action=action)
    rows = (q.order_by(TestAccountUsageLog.created_at.desc(),
                       TestAccountUsageLog.id.desc())
            .limit(limit).all())
    return jsonify({
        'account_id': account_id,
        'platform': account.platform,
        'account': account.account,
        'count': len(rows),
        'items': [r.to_dict() for r in rows],
    })
