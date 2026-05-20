"""用户认证与管理 API"""
import hashlib
import os
import random
import time
import requests
from flask import request, jsonify, session
from app import db
from app.models import User, OpenClawInstance, Project
from app.api import api_bp

VALID_ROLES = ('super_admin', 'admin', 'user', 'guest')

# WOA/OA SSO 配置 ——
#
# 票据签发入口： passport.woa.com/modules/passport/signin.ashx?oauth=true&appkey=...&url=...
# 票据解密入口： 智能网关 SGW + TOF4 RIO
#     http://api.sgw.woa.com/ebus/tof4/api/v1/passport/DecryptTicketWithClientIp
#
# 鉴权方式（公司太湖文档要求，所有调用必须签名，否则 SGW 直接拒"非法Code"）：
#   header['x-rio-paasid']    = WOA_PAASID
#   header['x-rio-nonce']     = nonce
#   header['x-rio-timestamp'] = timestamp
#   header['x-rio-signature'] = sha256(timestamp + paasToken + nonce + timestamp).upper()
#
# 选 SGW/RIO 而非老 SOAP 的原因：SGW 是公司主推接入点，鉴权一次签名解决，
# 不再受 passport 直连 IP 段限制。SOAP 仅在 SGW 不可达时作 fallback。

# SGW 智能网关接入点（OA / IDC 区轮询，testserver 9.134 段两者都可达）
# 可通过环境变量 WOA_SGW_BASES 覆盖（逗号分隔）。
_DEFAULT_SGW_BASES = (
    'http://api.sgw.woa.com',       # OA 区
    'http://api-idc.sgw.woa.com',   # IDC 区
)
_env_bases = (os.getenv('WOA_SGW_BASES') or '').strip()
SGW_BASES = tuple(b.strip().rstrip('/')
                  for b in _env_bases.split(',') if b.strip()) or _DEFAULT_SGW_BASES

# TOF4 解密 ticket 接口 path（在太湖订阅得到，无前导 server）
# AccessToken 接口：GET /ebus/tof4/api/v1/passport/AccessToken?appkey=...&code=<ticket>
# 已实测：claw_team 的订阅可用，签名头 + 此 path 能正常解析 ticket。
RIO_DECRYPT_PATH = os.getenv(
    'WOA_RIO_DECRYPT_PATH',
    '/ebus/tof4/api/v1/passport/AccessToken',
)

# 老 SOAP 兜底入口
PASSPORT_SOAP_URL = 'http://passport.oa.com/services/passportservice.asmx'
PASSPORT_SOAP_NS = 'http://indigo.oa.com/services/'

# 公司分配给 ClawTeam 项目的 paasid / token（在太湖申请获得）
WOA_PAASID = os.getenv('WOA_PAASID', 'claw_team')
WOA_TOKEN = os.getenv('WOA_TOKEN', 'your-woa-rio-token')


def _extract_ticket_aud(ticket):
    """从 TOF4T ticket 中提取 aud（passport 签发时绑定的客户端 IP）。

    ticket 形如 ``TOF4T<base64-json>``，payload 至少含 ``aud / iat``。
    解析失败一律返回空串，由 passport 自己用请求源 IP 兜底（也会失败，
    但至少不会因为我们传错 IP 把好 ticket 解坏）。
    """
    try:
        import base64
        import json as _json
        if not isinstance(ticket, str) or not ticket.startswith('TOF4T'):
            return ''
        payload_b64 = ticket[len('TOF4T'):]
        payload_b64 += '=' * (-len(payload_b64) % 4)
        raw = base64.urlsafe_b64decode(payload_b64.encode('ascii'))
        data = _json.loads(raw.decode('utf-8', 'replace'))
        return (data.get('aud') or data.get('Aud') or '').strip()
    except Exception:
        return ''


def _soap_decrypt_ticket(ticket, client_ip=''):
    """调用老 passport SOAP DecryptTicketWithClientIP 解密 ticket。

    Returns: (ok: bool, payload: dict)
        ok=True  时 payload = {username, nickname, deptname, raw_xml}
        ok=False 时 payload = {error: str, status: int, body: str}
    """
    import logging
    import re
    from xml.sax.saxutils import escape

    logger = logging.getLogger(__name__)

    ticket_escaped = escape(ticket or '')
    client_ip = client_ip or ''
    use_with_ip = bool(client_ip)
    action = (
        'DecryptTicketWithClientIP' if use_with_ip else 'DecryptTicket'
    )
    if use_with_ip:
        inner = (
            f'<DecryptTicketWithClientIP xmlns="{PASSPORT_SOAP_NS}">'
            f'<encryptedTicket>{ticket_escaped}</encryptedTicket>'
            f'<clientIP>{escape(client_ip)}</clientIP>'
            f'</DecryptTicketWithClientIP>'
        )
    else:
        inner = (
            f'<DecryptTicket xmlns="{PASSPORT_SOAP_NS}">'
            f'<encryptedTicket>{ticket_escaped}</encryptedTicket>'
            f'</DecryptTicket>'
        )
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        f'<soap:Body>{inner}</soap:Body>'
        '</soap:Envelope>'
    ).encode('utf-8')
    headers = {
        'Content-Type': 'text/xml; charset=utf-8',
        'SOAPAction': f'"{PASSPORT_SOAP_NS}{action}"',
    }
    try:
        resp = requests.post(PASSPORT_SOAP_URL, data=body, headers=headers,
                             timeout=10)
    except Exception as e:
        logger.error('[WOA-SSO] SOAP 请求异常: %s', e)
        return False, {'error': f'SOAP 请求异常: {e}', 'status': -1, 'body': ''}

    text = resp.text or ''
    logger.warning('[WOA-SSO] SOAP %s client_ip=%s status=%d body_head=%s',
                   action, client_ip, resp.status_code, text[:400])

    if resp.status_code != 200:
        # 提取 faultstring 让前端看得懂
        m = re.search(r'<faultstring[^>]*>([^<]+)</faultstring>', text)
        return False, {
            'error': f'SOAP HTTP {resp.status_code}: {m.group(1) if m else ""}',
            'status': resp.status_code,
            'body': text[:500],
        }

    def _x(tag):
        m = re.search(rf'<{tag}[^>]*>([^<]*)</{tag}>', text)
        return m.group(1).strip() if m else ''

    login = _x('LoginName')
    chinese = _x('ChineseName')
    dept = _x('DeptName')
    if not login:
        # passport 对无效 ticket 会返回空 <Response/>，没有 LoginName
        m = re.search(r'<faultstring[^>]*>([^<]+)</faultstring>', text)
        return False, {
            'error': m.group(1) if m else 'passport 未返回 LoginName（ticket 无效或已被消费）',
            'status': 200,
            'body': text[:500],
        }
    return True, {
        'username': login,
        'nickname': chinese or login,
        'deptname': dept or '',
        'raw_xml': text[:500],
    }


def _rio_signed_headers():
    """按公司太湖文档生成 SGW 签名头。

    签名串： timestamp + paasToken + nonce + timestamp
    哈希：   sha256 → hexdigest → upper
    """
    timestamp = str(int(time.time()))
    nonce = str(random.randint(100000, 999999))
    raw = timestamp + WOA_TOKEN + nonce + timestamp
    signature = hashlib.sha256(raw.encode('utf-8')).hexdigest().upper()
    return {
        'x-rio-paasid': WOA_PAASID,
        'x-rio-nonce': nonce,
        'x-rio-timestamp': timestamp,
        'x-rio-signature': signature,
    }


def _rio_decrypt_ticket(ticket):
    """SGW/RIO 解密 ticket（首选）。

    实测细节：
      * AccessToken 接口必须 GET，POST 一律 404 page not found
      * 必传 appkey + code，appkey 用 PAASID（==passport appkey==claw_team）
      * 签名头必须齐，缺失或顺序错会被 SGW 直接拒回 AGW.xxx

    Returns: (ok: bool, payload: dict)
    """
    import logging
    logger = logging.getLogger(__name__)

    params = {
        'appkey': WOA_PAASID,
        'code': ticket,
    }
    last_err = None
    for base in SGW_BASES:
        url = base + RIO_DECRYPT_PATH
        headers = _rio_signed_headers()
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=8)
        except Exception as e:
            last_err = f'{base} 请求异常: {e}'
            logger.warning('[WOA-SSO] RIO %s', last_err)
            continue
        body = (resp.text or '')[:600]
        logger.warning('[WOA-SSO] RIO GET %s status=%d body_head=%s',
                       url, resp.status_code, body)
        if resp.status_code != 200:
            last_err = f'{base} HTTP {resp.status_code}: {body[:200]}'
            continue
        try:
            data = resp.json()
        except Exception:
            last_err = f'{base} 返回非 JSON: {body[:200]}'
            continue
        # TOF4 标准约定：Ret==0 成功，否则看 ErrCode/ErrMsg
        ret = data.get('Ret', data.get('ret', 1))
        if ret != 0:
            last_err = (
                f'TOF4 Ret={ret} ErrCode={data.get("ErrCode")} '
                f'ErrMsg={data.get("ErrMsg")}'
            )
            # SGW 鉴权类错误（AGW.xxx）会出现在 ErrMsg 里
            return False, {'error': last_err, 'raw': data}
        d = data.get('Data') or data.get('data') or {}
        login = (d.get('LoginName') or d.get('loginName')
                 or d.get('login_name') or '').strip()
        if not login:
            return False, {'error': 'TOF4 未返回 LoginName', 'raw': data}
        return True, {
            'username': login,
            'nickname': (d.get('ChineseName') or d.get('chineseName')
                         or d.get('chinese_name') or login).strip(),
            'deptname': (d.get('DeptName') or d.get('deptName')
                         or d.get('dept_name') or '').strip(),
            'raw': data,
        }
    return False, {'error': last_err or '所有 SGW 接入点均不可达'}


def _decrypt_passport_ticket(ticket):
    """对外统一入口：解密 passport ticket → (ok, payload)。

    顺序：
      1) SGW/RIO 签名调用 DecryptTicketWithClientIp（公司主推，首选）
      2) 失败再回退到老 SOAP DecryptTicketWithClientIP
      3) SOAP 不带 clientIP 再兜底一次
    """
    import logging
    logger = logging.getLogger(__name__)

    ok, payload = _rio_decrypt_ticket(ticket)
    if ok:
        return True, payload
    rio_err = payload
    logger.warning('[WOA-SSO] RIO 解密失败，回退 SOAP: %s', rio_err.get('error'))

    aud_ip = _extract_ticket_aud(ticket)
    if aud_ip:
        ok2, payload2 = _soap_decrypt_ticket(ticket, client_ip=aud_ip)
        if ok2:
            return True, payload2

    ok3, payload3 = _soap_decrypt_ticket(ticket, client_ip='')
    if ok3:
        return True, payload3

    return False, {
        'error': rio_err.get('error') or 'ticket 解密失败',
        'rio': rio_err,
        'soap': payload3 if not aud_ip else payload2,
    }


@api_bp.route('/auth/register', methods=['POST'])
def register():
    """用户注册"""
    data = request.get_json()
    username = (data.get('username') or '').strip()
    password = (data.get('password') or '').strip()
    display_name = (data.get('display_name') or '').strip()

    if not username or not password:
        return jsonify({'error': '用户名和密码不能为空'}), 400
    if len(password) < 4:
        return jsonify({'error': '密码至少4位'}), 400

    if User.query.filter_by(username=username).first():
        return jsonify({'error': f'用户名 "{username}" 已存在'}), 409

    user = User(
        username=username,
        display_name=display_name or username,
        role='super_admin' if username == 'rajqiu' else 'user',
    )
    user.set_password(password)
    db.session.add(user)
    db.session.commit()

    session['user_id'] = user.id
    return jsonify({'message': '注册成功', 'user': user.to_dict()}), 201


@api_bp.route('/auth/login', methods=['POST'])
def login():
    """用户登录（用户名 + 密码）。

    成功后会自动设置 `session['bypass_ngn'] = True`，避免下次请求被 NGN
    `x-tai-identity` 头自动覆盖回 OA 身份——这是给 super_admin 测试别人
    账号验证逻辑留的"后门"机制。退出测试模式：访问 `/logout` 即可。
    """
    data = request.get_json()
    username = (data.get('username') or '').strip()
    password = (data.get('password') or '').strip()

    if not username or not password:
        return jsonify({'error': '用户名和密码不能为空'}), 400

    user = User.query.filter_by(username=username).first()
    if not user or not user.check_password(password):
        return jsonify({'error': '用户名或密码错误'}), 401

    session.permanent = True
    session['user_id'] = user.id
    session['bypass_ngn'] = True
    return jsonify({
        'message': '登录成功（已进入 NGN 旁路模式，登出后恢复 OA 自动登录）',
        'user': user.to_dict(),
        'bypass_ngn': True,
    })


@api_bp.route('/auth/impersonate', methods=['POST'])
def impersonate():
    """super_admin 专属：免密码切换到任意账号身份。

    用途：测试其他人账号的验证流程时（如权限检查、UI 显示等），不需要每个
    账号都拿到密码——super_admin 直接切过去即可。

    请求体：{"username": "target_login_name"}

    鉴权要求：当前 session 必须是 super_admin 用户。切换后会标记
    `bypass_ngn=True`，避免下次请求被 NGN 自动覆盖回 OA 身份。
    """
    actor_id = session.get('user_id')
    if not actor_id:
        return jsonify({'error': '未登录'}), 401
    actor = User.query.get(actor_id)
    if not actor or actor.role != 'super_admin':
        return jsonify({'error': '仅 super_admin 可切换身份'}), 403

    data = request.get_json() or {}
    target_username = (data.get('username') or '').strip()
    if not target_username:
        return jsonify({'error': '缺少 username 参数'}), 400

    target = User.query.filter_by(username=target_username).first()
    if not target:
        return jsonify({'error': f'用户 {target_username} 不存在'}), 404

    import logging
    logging.getLogger(__name__).warning(
        '[IMPERSONATE] %s (id=%d) 切换为 %s (id=%d)',
        actor.username, actor.id, target.username, target.id,
    )

    session.permanent = True
    session['user_id'] = target.id
    session['bypass_ngn'] = True
    session['impersonator_id'] = actor.id  # 留个尾巴方便审计/恢复
    return jsonify({
        'message': f'已切换为 {target.username}（NGN 旁路已开启）',
        'user': target.to_dict(),
        'impersonator': actor.username,
    })


@api_bp.route('/auth/woa-callback', methods=['GET', 'POST'])
def woa_callback():
    """WOA SSO 回调 —— Hub 直接解密 ticket，登录并跳转首页。

    流程：
    1. WOA 登录成功后重定向回本站，URL 带 ticket 参数（和 sessionKey）
    2. 通过 RIO + TOF4 passport AccessToken 解密 ticket
       （由 Hub 自己实现，不再依赖外部 Go 服务）
    3. 提取 LoginName / ChineseName / DeptName
    4. 查找或自动创建用户，写入 session
    5. 302 跳转首页

    任何失败一律 redirect 回 /login?sso_error=...，避免浏览器停留在
    JSON 错误页让用户以为「票据解析 OK 但没登录」。
    """
    import logging
    import urllib.parse
    from flask import redirect

    logger = logging.getLogger(__name__)

    def _fail_redirect(reason, status_code='sso_failed'):
        """统一失败处理：跳回登录页并把 reason 带回，便于排查。"""
        logger.error('[WOA-SSO] 登录失败: %s', reason)
        msg = urllib.parse.quote((reason or '')[:200])
        return redirect(f'/login?sso_error={status_code}&msg={msg}')

    # WOA passport 回调带的是 ticket 参数（不是 code）
    ticket = request.args.get('ticket') or request.form.get('ticket')
    if not ticket:
        return _fail_redirect('缺少 ticket 参数', 'missing_ticket')

    logger.warning('[WOA-SSO] 收到 ticket 长度=%d 前20字符=%s 来源=%s',
                   len(ticket), ticket[:20], request.headers.get('Referer', ''))

    ok, payload = _decrypt_passport_ticket(ticket)
    if not ok:
        return _fail_redirect(payload.get('error') or '解密失败', 'decrypt_failed')

    username = (payload.get('username') or '').strip()
    nickname = (payload.get('nickname') or username).strip()
    deptname = (payload.get('deptname') or '').strip()
    if not username:
        return _fail_redirect('passport 未返回用户名', 'no_username')

    # 查找或自动创建用户
    user = User.query.filter_by(username=username).first()
    if not user:
        user = User(
            username=username,
            display_name=nickname or username,
            role='super_admin' if username == 'rajqiu' else 'user',
        )
        # WOA 用户无需密码（设置随机密码防止密码登录）
        import secrets
        user.set_password(secrets.token_hex(16))
        db.session.add(user)
        db.session.commit()
        logger.warning('[WOA-SSO] 自动创建用户 %s (display=%s, dept=%s)',
                       username, nickname, deptname)
    else:
        if nickname and user.display_name != nickname:
            user.display_name = nickname
            db.session.commit()

    # 写入 session 并跳转首页
    session.permanent = True
    session['user_id'] = user.id
    logger.warning('[WOA-SSO] 用户 %s (id=%d) 登录成功，跳转首页', username, user.id)
    return redirect('/')


@api_bp.route('/auth/logout', methods=['POST'])
def logout():
    """登出（仅清 Flask session；要彻底清 OA SSO 状态请用 GET `/logout`）"""
    session.pop('user_id', None)
    session.pop('bypass_ngn', None)
    session.pop('impersonator_id', None)
    return jsonify({'message': '已登出'})


@api_bp.route('/auth/me', methods=['GET'])
def get_current_user():
    """获取当前登录用户"""
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': '未登录'}), 401
    user = User.query.get(user_id)
    if not user:
        session.pop('user_id', None)
        return jsonify({'error': '用户不存在'}), 401

    data = user.to_dict()
    # 暴露 NGN 旁路状态（前端可显示"测试模式中"提示）
    data['bypass_ngn'] = bool(session.get('bypass_ngn'))
    impersonator_id = session.get('impersonator_id')
    if impersonator_id:
        impersonator = User.query.get(impersonator_id)
        if impersonator:
            data['impersonator'] = impersonator.username

    # 解析负责项目名称（供前端项目筛选使用）
    from app.models import Project
    managed_ids = user.managed_projects or []
    managed_names = []
    if managed_ids:
        for pid in managed_ids:
            p = Project.query.get(pid)
            if p:
                managed_names.append(p.name)
    # 如果是普通用户且绑定了 claw，从 claw 推断项目
    if not managed_names and user.bound_claw_id:
        from app.models import OpenClawInstance
        claw = OpenClawInstance.query.get(user.bound_claw_id)
        if claw:
            pname = claw.project.name if claw.project else claw.project_name
            if pname:
                managed_names.append(pname)
    data['managed_project_names'] = managed_names

    # 权限信息
    role = user.role or 'user'
    is_sa = role == 'super_admin'
    is_admin = role == 'admin'
    is_user = role == 'user'

    data['permissions'] = {
        # 菜单可见性
        'nav_settings': is_sa,
        'nav_audit_logs': is_sa,
        'nav_agent_templates': is_sa,
        'nav_hub': True,
        'nav_review': True,
        'nav_users': True,
        # 功能权限
        'can_create_openclaw': is_sa or is_admin,
        'can_delete_openclaw': is_sa or is_admin,
        'can_assign_skills': is_sa or is_admin,
        'can_assign_rules': is_sa or is_admin,
        'can_review': is_sa or is_admin,
        'can_edit_knowledge': is_sa or is_admin or is_user,
        'can_edit_testcases': is_sa or is_admin or is_user,
        'can_see_all_projects': is_sa,
        'can_see_all_claws': is_sa or is_admin,  # admin 看到全部但只能操作本项目
        'can_see_admin_claw': is_sa,  # 超级管理员可见龙虾王
        'can_see_tokens': is_sa or is_admin,  # 成员只能看自己绑定的 token
    }

    return jsonify(data)


@api_bp.route('/auth/change-password', methods=['POST'])
def change_password():
    """修改自己的密码（临时功能，OA 接入后移除）"""
    uid = session.get('user_id')
    if not uid:
        return jsonify({'error': '未登录'}), 401
    user = User.query.get(uid)
    if not user:
        return jsonify({'error': '用户不存在'}), 401

    data = request.get_json()
    old_pwd = (data.get('old_password') or '').strip()
    new_pwd = (data.get('new_password') or '').strip()

    if not old_pwd or not new_pwd:
        return jsonify({'error': '新旧密码不能为空'}), 400
    if len(new_pwd) < 4:
        return jsonify({'error': '新密码至少4位'}), 400
    if not user.check_password(old_pwd):
        return jsonify({'error': '原密码错误'}), 400

    user.set_password(new_pwd)
    db.session.commit()
    return jsonify({'message': '密码修改成功'})


# ============== 权限辅助函数 ==============

def _get_operator():
    """获取当前操作者"""
    uid = session.get('user_id')
    if not uid:
        return None
    return User.query.get(uid)


def _require_super_admin():
    """检查当前用户是否为 super_admin。"""
    uid = session.get('user_id')
    if not uid:
        return None, (jsonify({'error': '未登录'}), 401)
    user = User.query.get(uid)
    if not user or user.role != 'super_admin':
        return None, (jsonify({'error': '需要超级管理员权限'}), 403)
    return user, None


def _require_admin_or_above():
    """检查当前用户是否为 admin 或 super_admin"""
    uid = session.get('user_id')
    if not uid:
        return None, (jsonify({'error': '未登录'}), 401)
    user = User.query.get(uid)
    if not user or user.role not in ('super_admin', 'admin'):
        return None, (jsonify({'error': '需要管理员权限'}), 403)
    return user, None


def _collect_user_project_ids(user):
    """收集用户关联的项目ID（统一 project_id 口径）。"""
    if not user:
        return set()
    project_ids = set()
    for pid in (user.managed_projects or []):
        try:
            project_ids.add(int(pid))
        except Exception:
            continue
    if user.bound_claw_id:
        claw = OpenClawInstance.query.get(user.bound_claw_id)
        if claw:
            if claw.project_id:
                project_ids.add(int(claw.project_id))
            elif claw.project_name:
                p = Project.query.filter_by(name=claw.project_name).first()
                if p:
                    project_ids.add(int(p.id))
    return project_ids


# ============== 用户管理 ==============

@api_bp.route('/users', methods=['GET'])
def list_users():
    """
    获取用户列表
    - super_admin: 看所有用户
    - admin: 只看与自己同项目的用户
    """
    operator = _get_operator()
    if not operator:
        return jsonify({'error': '未登录'}), 401

    users = User.query.order_by(User.created_at).all()

    if operator.role == 'super_admin':
        return jsonify([u.to_dict() for u in users])

    # 非超管：只看同项目用户（admin/user/guest）
    operator_project_ids = _collect_user_project_ids(operator)

    filtered = []
    for u in users:
        # 自身始终可见
        if u.id == operator.id:
            filtered.append(u)
            continue
        u_project_ids = _collect_user_project_ids(u)
        if operator_project_ids and (operator_project_ids & u_project_ids):
            filtered.append(u)

    return jsonify([u.to_dict() for u in filtered])


@api_bp.route('/users', methods=['POST'])
def create_user():
    """管理员添加用户，默认密码 123456"""
    operator, err = _require_admin_or_above()
    if err:
        return err

    data = request.get_json()
    username = (data.get('username') or '').strip()
    display_name = (data.get('display_name') or '').strip()
    role = data.get('role', 'user')
    managed_projects = data.get('managed_projects') or []

    if not username:
        return jsonify({'error': '用户名不能为空'}), 400

    if User.query.filter_by(username=username).first():
        return jsonify({'error': f'用户名 "{username}" 已存在'}), 409

    # admin 不能创建 super_admin / admin
    if operator.role == 'admin' and role in ('super_admin', 'admin'):
        return jsonify({'error': 'admin 只能创建成员或访客'}), 403

    if role not in VALID_ROLES:
        role = 'user'

    user = User(
        username=username,
        display_name=display_name or username,
        role=role,
        managed_projects=managed_projects,
    )
    user.set_password(data.get('password', '123456'))
    db.session.add(user)
    db.session.commit()

    return jsonify({'message': f'用户 {username} 已创建（默认密码：123456）', 'user': user.to_dict()}), 201


@api_bp.route('/users/<int:user_id>', methods=['PUT'])
def update_user(user_id):
    """
    更新用户信息
    - super_admin: 可改所有人的角色、项目
    - admin: 只能改 user/guest 角色的用户，不能提升为 admin/super_admin
    """
    operator, err = _require_admin_or_above()
    if err:
        return err

    target = User.query.get_or_404(user_id)
    data = request.get_json()

    # 不允许修改自己的角色
    if target.id == operator.id and 'role' in data:
        return jsonify({'error': '不能修改自己的角色'}), 400

    # admin 不能修改 super_admin 或其他 admin
    if operator.role == 'admin':
        if target.role in ('super_admin', 'admin'):
            return jsonify({'error': '无权修改管理员'}), 403
        op_projects = _collect_user_project_ids(operator)
        target_projects = _collect_user_project_ids(target)
        if target.id != operator.id and (not op_projects or not (op_projects & target_projects)):
            return jsonify({'error': '仅可管理本项目成员'}), 403
        # admin 只能设置 user 或 guest
        if 'role' in data and data['role'] not in ('user', 'guest'):
            return jsonify({'error': 'admin 只能设置成员或访客角色'}), 403

    if 'role' in data and data['role'] in VALID_ROLES:
        target.role = data['role']
    if 'managed_projects' in data:
        target.managed_projects = data['managed_projects']
    if 'display_name' in data:
        target.display_name = data['display_name']
    if 'bound_claw_id' in data:
        target.bound_claw_id = data['bound_claw_id']

    db.session.commit()
    return jsonify(target.to_dict())


@api_bp.route('/users/<int:user_id>/reset-password', methods=['POST'])
def reset_user_password(user_id):
    """管理员重置用户密码（临时功能）"""
    operator = _get_operator()
    if not operator:
        return jsonify({'error': '未登录'}), 401

    target = User.query.get_or_404(user_id)
    data = request.get_json()
    new_pwd = (data.get('new_password') or '').strip()

    if not new_pwd or len(new_pwd) < 4:
        return jsonify({'error': '新密码至少4位'}), 400

    # 普通成员仅可重置自己的密码
    if operator.role not in ('super_admin', 'admin') and target.id != operator.id:
        return jsonify({'error': '仅可重置自己的密码'}), 403
    # 项目管理员不能重置管理员密码（自身除外）
    if operator.role == 'admin' and target.id != operator.id and target.role in ('super_admin', 'admin'):
        return jsonify({'error': '无权重置管理员密码'}), 403
    # 项目管理员仅可重置本项目成员（自身除外）
    if operator.role == 'admin' and target.id != operator.id:
        op_projects = _collect_user_project_ids(operator)
        target_projects = _collect_user_project_ids(target)
        if not op_projects or not (op_projects & target_projects):
            return jsonify({'error': '仅可重置本项目成员密码'}), 403

    target.set_password(new_pwd)
    db.session.commit()
    return jsonify({'message': f'已重置 {target.username} 的密码'})


@api_bp.route('/users/<int:user_id>', methods=['DELETE'])
def delete_user(user_id):
    """删除用户（仅 super_admin）"""
    operator, err = _require_super_admin()
    if err:
        return err

    target = User.query.get_or_404(user_id)
    if target.id == operator.id:
        return jsonify({'error': '不能删除自己'}), 400

    db.session.delete(target)
    db.session.commit()
    return jsonify({'message': f'用户 {target.username} 已删除'})
