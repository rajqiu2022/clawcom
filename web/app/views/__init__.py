from flask import Blueprint, current_app, render_template, session, redirect, request, jsonify
from app import db

views_bp = Blueprint('views', __name__)


def _consume_woa_ticket():
    """解密 ticket 并把对应用户登录到 session。

    成功 → 302 到首页 `/`
    失败 → 302 到 `/login?sso_error=...`
    """
    import logging
    import secrets
    import urllib.parse
    from app.api.auth import _decrypt_passport_ticket
    from app.models import User

    logger = logging.getLogger(__name__)

    ticket = request.args.get('ticket') or request.form.get('ticket') or ''
    if not ticket:
        return redirect('/login?sso_error=missing_ticket')

    logger.warning('[WOA-SSO] /woa ticket 长度=%d 前20=%s 来源=%s',
                   len(ticket), ticket[:20],
                   request.headers.get('Referer', ''))

    ok, payload = _decrypt_passport_ticket(ticket)
    if not ok:
        msg = urllib.parse.quote((payload.get('error') or '')[:200])
        return redirect(f'/login?sso_error=decrypt_failed&msg={msg}')

    username = (payload.get('username') or '').strip()
    nickname = (payload.get('nickname') or username).strip()
    if not username:
        return redirect('/login?sso_error=no_username')

    user = User.query.filter_by(username=username).first()
    if not user:
        user = User(
            username=username,
            display_name=nickname or username,
            role='super_admin' if username == 'rajqiu' else 'user',
        )
        user.set_password(secrets.token_hex(16))
        db.session.add(user)
        db.session.commit()
        logger.warning('[WOA-SSO] 自动创建用户 %s (display=%s)', username, nickname)
    elif nickname and user.display_name != nickname:
        user.display_name = nickname
        db.session.commit()

    session.permanent = True
    session['user_id'] = user.id
    logger.warning('[WOA-SSO] 用户 %s (id=%d) SSO 登录成功，跳转首页',
                   username, user.id)
    return redirect('/')


# 允许的 WOA SSO 回调路径（passport 会把 ticket 拼回这里）
# 同时兼容 `/` 兜底（passport 偶尔会丢 url 参数 fallback 回站点根）
_SSO_CALLBACK_PATHS = ('/woa', '/login/woa-callback', '/')


@views_bp.route('/_ngn_probe', methods=['GET'])
def _ngn_probe():
    """NGN 网关注入头排查 endpoint（仅 super_admin 可见）。

    用于确认 NGN 在 SLB 层注入了哪些 HTTP header（重点是 `X-Tai-Identity`），
    以及 `tai_identity` 模块解密结果。线上调试需求消失后可整体下线。

    安全考虑：上游 NGN 会原样转发浏览器请求里的所有 Cookie，包含 bk_ticket /
    openclaw_session 等敏感凭证，因此本 endpoint 必须限制访问者身份，
    避免任何人凭 URL 即可拉到他人 session/凭据。
    """
    import logging
    from app.models import User

    logger = logging.getLogger(__name__)

    uid = session.get('user_id')
    user = User.query.get(uid) if uid else None
    if not user or user.role != 'super_admin':
        return jsonify({'error': 'forbidden'}), 403

    headers = {k: v for k, v in request.headers.items()}
    cookies = {k: v for k, v in request.cookies.items()}
    env_keys_of_interest = (
        'REMOTE_ADDR', 'REMOTE_USER', 'HTTP_X_FORWARDED_FOR',
        'HTTP_X_REAL_IP', 'HTTP_HOST',
    )
    env_view = {
        k: request.environ.get(k) for k in env_keys_of_interest
        if request.environ.get(k) is not None
    }
    # 仅在 super_admin 调用时打日志，仍隐去 Cookie 行避免落盘泄漏
    _hdr_for_log = {k: v for k, v in headers.items() if k.lower() != 'cookie'}
    logger.warning('[NGN-PROBE] caller=%s path=%s headers(no-cookie)=%s env=%s',
                   user.username, request.path, _hdr_for_log, env_view)

    tai_preview = None
    tai_header = headers.get('x-tai-identity') or headers.get('X-Tai-Identity')
    if tai_header:
        try:
            from app.tai_identity import decode_x_tai_identity
            ok, payload = decode_x_tai_identity(tai_header)
            if ok:
                tai_preview = payload.to_dict()
            else:
                tai_preview = {'error': payload.get('error')}
        except Exception as e:
            tai_preview = {'error': f'{e.__class__.__name__}: {e}'}

    # 对外输出去掉 Cookie 头（仍含其它头便于排查 NGN 注入字段）
    safe_headers = {k: v for k, v in headers.items() if k.lower() != 'cookie'}

    return jsonify({
        'path': request.path,
        'method': request.method,
        'remote_addr': request.remote_addr,
        'host': request.host,
        'headers': safe_headers,
        'cookie_keys': sorted(cookies.keys()),
        'env': env_view,
        'x_tai_identity_present': bool(tai_header),
        'x_tai_identity_decoded': tai_preview,
    })


def _consume_tai_identity():
    """从 `x-tai-identity` 请求头解密并登录。

    `clawteam.woa.com` 已接入太湖 NGN 网关，所有请求经过 SLB 时已完成 OA
    统一登录，NGN 把身份以 JWE Compact 形式塞进 `x-tai-identity` 头。我们
    解密这个头取出 LoginName，按 LoginName 查找/创建用户并写入 session。

    "NGN 旁路"机制：如果 session 里有 `bypass_ngn=True`（由用户主动
    `/api/v1/auth/login` 表单登录、`/login?bypass_ngn=1` 入口或
    `/api/v1/auth/impersonate` 切换身份触发），本函数直接返回 False，
    让 NGN header **不覆盖** session 里已经设好的 user_id。这是给
    super_admin 测试别人账号验证流程留的"后门"——NGN 接管后默认所有请求
    都被自动登录为 OA 用户（rajqiu），无 bypass 标记就没法切到别人账号测。

    Returns:
        bool: 解密成功并完成登录返回 True；该请求没带 header 或解密失败返
        回 False（不阻断请求，让调用方继续走原 check_login 流程）。
    """
    import logging
    import secrets
    from app.tai_identity import decode_x_tai_identity
    from app.models import User

    logger = logging.getLogger(__name__)

    # NGN 旁路：用户主动切换了身份就别再被 NGN 覆盖回去
    if session.get('bypass_ngn'):
        return False

    header_value = request.headers.get('x-tai-identity') \
        or request.headers.get('X-Tai-Identity')
    if not header_value:
        return False

    ok, payload = decode_x_tai_identity(header_value)
    if not ok:
        logger.warning('[TAI] x-tai-identity 解密失败: %s',
                       (payload or {}).get('error'))
        return False

    login_name = (payload.login_name or '').strip()
    if not login_name:
        return False

    user = User.query.filter_by(username=login_name).first()
    if not user:
        user = User(
            username=login_name,
            display_name=login_name,
            role='super_admin' if login_name == 'rajqiu' else 'user',
        )
        user.set_password(secrets.token_hex(16))
        db.session.add(user)
        db.session.commit()
        logger.warning('[TAI] 自动创建用户 %s (staff_id=%s)',
                       login_name, payload.staff_id)

    # 更新最后登录时间（每次 TAI 认证通过都更新）
    from datetime import datetime
    user.last_login_at = datetime.now()
    db.session.commit()

    session.permanent = True
    session['user_id'] = user.id
    return True


@views_bp.before_request
def check_login():
    """全局登录检查（排除 /login、/login/woa、/woa），并自动识别 NGN 身份。"""
    # 0) NGN 旁路开关：?bypass_ngn=1 显式切到"用账号密码登录"模式
    #    用途：super_admin 想测试其他人账号的验证逻辑。访问 `/login?bypass_ngn=1`
    #    会清掉当前 session（含 NGN 自动登录的 user_id）并打上 bypass 标记，
    #    后续请求 `_consume_tai_identity()` 会跳过，用户能正常用 username/pwd
    #    登录到别的账号。退出测试模式：访问 `/logout`（标记会一并被清掉）。
    if request.args.get('bypass_ngn') == '1' and request.path == '/login':
        session.clear()
        session['bypass_ngn'] = True
        session.permanent = True

    # 1) 太湖 NGN 网关注入的 x-tai-identity（首选 SSO 入口，必须最先执行）
    #    `clawteam.woa.com` 已接入太湖，浏览器到我们 Flask 时身份已认证完成，
    #    NGN 在 header 里塞了 JWE。只要解密成功就直接登录，不再走旧 ticket 流程。
    #    若 session.bypass_ngn 已置位，内部会直接返回不覆盖 user_id。
    if not session.get('user_id'):
        _consume_tai_identity()  # 内部把 user_id 写进 session

    # 2) 兼容旧 SSO 入口：专用 /woa 回调路径（NGN 接管后基本不会触发）
    if request.path == '/woa':
        return _consume_woa_ticket()

    # 3) 兼容旧 SSO 入口：任何路径上看到 `?ticket=TOF4T...`
    ticket_qs = request.args.get('ticket') or ''
    if ticket_qs.startswith('TOF4T'):
        return _consume_woa_ticket()

    # 4) 登录页、登出页、SSO 入口不需要登录态
    #    `/logout` 必须放行：万一 NGN 头被剥离或 session 已损坏时，仍然要
    #    能把浏览器送到 passport signout 清掉 OA 状态，不能 redirect 回 login。
    if request.path in ('/login', '/login/woa', '/logout'):
        return None

    # 5) 测试报告分享外链（MEMORY #134）：匿名只读，不能要求登录
    #    /r/<token> 或 /test-reports/share/<token> 都放行
    if (request.path.startswith('/r/')
            or request.path.startswith('/test-reports/share/')
            or request.path == '/developer-ai/collaborate'
            or request.path == '/developer-ai/case-review'
            or request.path == '/chat/join'):
        return None

    # 知识库匿名分享页：只读正文，并支持通过公开 API 下载 Markdown。
    if request.path.startswith('/k/') or request.path.startswith('/knowledge/share/'):
        return None

    uid = session.get('user_id')
    if not uid:
        return redirect('/login')
    from app.models import User
    user = User.query.get(uid)
    if not user:
        session.pop('user_id', None)
        return redirect('/login')
    return None


@views_bp.route('/woa', methods=['GET', 'POST'])
def woa_callback_page():
    """WOA/OA SSO 回调页。

    跳转登录时带的回调 URL 就是它（`https://clawteam.woa.com/woa`）。
    passport 在用户认证完成后把 ticket 拼回 `/woa?ticket=...`，我们
    解密 ticket 拿到 LoginName/ChineseName/DeptName，写入 session，跳
    转首页；任何失败一律退回 `/login?sso_error=...`，前端登录页会显示
    具体原因，不会出现"浏览器卡在 JSON 错误"的情况。
    """
    return _consume_woa_ticket()


def _require_role(*roles):
    """检查当前用户角色"""
    uid = session.get('user_id')
    if not uid:
        return redirect('/login')
    from app.models import User
    user = User.query.get(uid)
    if not user or user.role not in roles:
        return redirect('/')
    return None


@views_bp.route('/login')
def login_page():
    """登录页"""
    return render_template('login.html')


@views_bp.route('/logout', methods=['GET', 'POST'])
def logout_page():
    """全站登出 —— 同步清掉 OA passport 鉴权状态。

    `clawteam.woa.com` 接入了 NGN 网关，所有请求会被 SLB 自动用
    `X-Tai-Identity` 头识别身份。仅 `session.clear()` 不足以登出：浏览器
    再次访问任何路径时，NGN 仍持有 passport ticket，`check_login()` 会
    立即又把用户认证回来——表现为"点了登出还是登录态"。

    解决：让浏览器**离开本域名**跳到 passport 全局 signout，由 passport
    清掉 OA ticket；passport 那边失效后，NGN 的 `x_host_key` 会被强制
    重做 SSO。本地再 `Set-Cookie ...=; Max-Age=0` 清掉 Flask session 与
    NGN 注入的会话 cookie，万一 passport signout 不识别也能切断本域残留。

    支持 env 覆盖：
      * `WOA_LOGOUT_URL`        默认 `https://std.passport.woa.com/modules/passport/signout.ashx`
      * `WOA_LOGOUT_RETURN_URL` 默认本站 `/login?logout=1`
        注意：**不能写 `https://passport.woa.com` 作为 return URL**，
        该域名根路径直接返回纯文本 `404 page not found`，
        signout 完成后浏览器会停留在 404 页，体验非常差。
        改为回跳到本站 `/login?logout=1`：
          - 如 OA ticket 已失效 → NGN 拉起 SSO 让用户重新输密码；
          - 如公司域全局 SSO 仍生效 → 静默登回（SSO 设计如此，无法绕过）；
          - 至少不会出现 "404 page not found"。
    """
    import os
    import urllib.parse

    session.clear()

    logout_url = (os.getenv('WOA_LOGOUT_URL')
                  or 'https://std.passport.woa.com/modules/passport/signout.ashx').strip()
    # 默认回跳本站登录页（带 logout=1 让前端显示"已退出"提示）。
    # 用 request.host_url 兼容测试/生产环境主机名差异。
    default_return = request.host_url.rstrip('/') + '/login?logout=1'
    return_url = (os.getenv('WOA_LOGOUT_RETURN_URL') or default_return).strip()
    target = f'{logout_url}?url={urllib.parse.quote(return_url, safe="")}'

    resp = redirect(target)
    # 显式清除 Flask session cookie
    resp.set_cookie('openclaw_session', '', expires=0, path='/',
                    samesite='Lax')
    # 顺手清 NGN 在 clawteam.woa.com 域名下种的 cookie，
    # 避免 passport signout 失败时仍有本域 NGN 残留会话
    for ck in ('x_host_key', 'x-host-key-ngn',
               'x_host_key_access_https', 'x-client-ssid',
               'bk_ticket', 'bk_uid'):
        resp.set_cookie(ck, '', expires=0, path='/')
    return resp


@views_bp.route('/login/woa')
def login_woa():
    """WOA/OA SSO 登录跳转 —— 重定向到 passport 认证页。

    与公司 gametool 完全同款：
        passport.woa.com/modules/passport/signin.ashx
            ?oauth=true
            &appkey=<RIO paasid>
            &url=<callback>

    注意 appkey 不是 RIO token，是 RIO paasid（公司服务标识）。
    回调到 `/woa`，由 `_consume_woa_ticket()` 解密 ticket 并登录。
    """
    import urllib.parse

    callback_url = 'https://clawteam.woa.com/woa'
    # claw_team 项目专属 paasid（== passport appkey）
    appkey = 'claw_team'
    encoded_url = urllib.parse.quote(callback_url, safe='')
    sso_url = (
        f'https://passport.woa.com/modules/passport/signin.ashx'
        f'?oauth=true&appkey={appkey}&url={encoded_url}'
    )
    return redirect(sso_url)


@views_bp.route('/')
def dashboard():
    return render_template('dashboard.html')


@views_bp.route('/openclaws')
def openclaws_list():
    return render_template('openclaws.html')


@views_bp.route('/openclaws/<int:claw_id>')
def openclaw_detail(claw_id):
    return render_template('openclaw_detail.html', claw_id=claw_id)


@views_bp.route('/skills')
def skills_market():
    return render_template('skills.html')


@views_bp.route('/knowledge')
def knowledge_base():
    return render_template('knowledge.html')


@views_bp.route('/knowledge/wiki/<int:page_id>')
def knowledge_wiki_page(page_id):
    """Stable authenticated deep link; page ACL is enforced by the API."""
    return render_template('knowledge.html')


@views_bp.route('/k/<token>')
@views_bp.route('/knowledge/share/<token>')
def knowledge_share_page(token):
    """知识库匿名只读分享页。"""
    return render_template('knowledge_share.html', share_token=token)


@views_bp.route('/settings')
def settings():
    r = _require_role('super_admin')
    if r: return r
    return render_template('settings.html')


@views_bp.route('/reports')
def reports():
    return render_template('reports.html')


@views_bp.route('/tapd')
def tapd():
    return render_template('tapd.html')


@views_bp.route('/hub')
def hub():
    return render_template(
        'hub.html',
        chat_room_enabled=bool(current_app.config.get('CHAT_ROOM_ENABLED', False)),
    )


@views_bp.route('/chat/join')
def chat_room_join():
    """外部临时成员入口；邀请码只从 URL fragment 由浏览器兑换。"""
    if not current_app.config.get('CHAT_ROOM_ENABLED', False):
        return render_template('chat_room_join.html', chat_room_enabled=False), 404
    return render_template('chat_room_join.html', chat_room_enabled=True)


@views_bp.route('/agent-templates')
def agent_templates():
    r = _require_role('super_admin')
    if r:
        return r
    return render_template('agent_templates.html')


@views_bp.route('/rules')
def rules():
    return render_template('rules.html')


@views_bp.route('/testcases')
@views_bp.route('/testcases/<int:lib_id>')
def testcases(lib_id=None):
    return render_template('testcases.html', initial_lib_id=lib_id)


@views_bp.route('/review')
def review_center():
    return render_template('review.html')


@views_bp.route('/topics')
def topics_page():
    return render_template('topics.html')


@views_bp.route('/panorama')
def panorama_page():
    return render_template('panorama.html')


@views_bp.route('/exams')
def exams_page():
    return render_template('exams.html')


@views_bp.route('/secrets')
def secrets_page():
    return render_template('secrets.html')


@views_bp.route('/exams/papers/<int:paper_id>/edit')
def exam_paper_edit_page(paper_id):
    return render_template('exam_paper_edit.html', paper_id=paper_id)


@views_bp.route('/exams/sessions/<int:session_id>')
def exam_session_page(session_id):
    return render_template('exam_session.html', session_id=session_id)


@views_bp.route('/topics/<int:topic_id>')
def topic_detail(topic_id):
    return render_template(
        'topic_detail.html', topic_id=topic_id,
        shift_left_enabled=_shift_left_enabled())


@views_bp.route('/shared-articles')
@views_bp.route('/shared-articles/<int:article_id>')
def shared_articles_page(article_id=None):
    """见闻分享：全员可见的轻量文章分享 + 评论。"""
    return render_template('shared_articles.html', initial_article_id=article_id)


# --------------------------------------------------------------------------
# 全局测试报告中心（MEMORY #134）
# --------------------------------------------------------------------------
@views_bp.route('/test-reports')
@views_bp.route('/test-reports/<int:report_id>')
def test_reports_page(report_id=None):
    """测试报告列表 + 详情 modal（登录态）。点详情走 ?report_id=xx。"""
    return render_template(
        'test_reports.html', initial_report_id=report_id,
        shift_left_enabled=_shift_left_enabled())


@views_bp.route('/workflows')
def workflows_page():
    return render_template(
        'workflows.html', shift_left_enabled=_shift_left_enabled())


def _shift_left_enabled():
    value = current_app.config.get('SHIFT_LEFT_ENABLED', False)
    if isinstance(value, str):
        return value.lower() in ('1', 'true', 'yes', 'on')
    return bool(value)


@views_bp.route('/developer-ai/collaborate')
def developer_ai_collaborate_page():
    """Public handoff page; the one-time invitation stays in URL fragment."""
    return render_template('developer_ai_collaborate.html')


@views_bp.route('/developer-ai/case-review')
def developer_ai_case_review_page():
    """Public shell; review data still requires a scoped temporary token."""
    return render_template('developer_ai_case_review.html')


# 匿名外链页：/r/<token>（短路径，便于分享）；/test-reports/share/<token>（备用）
@views_bp.route('/r/<token>')
@views_bp.route('/test-reports/share/<token>')
def test_report_share_page(token):
    """分享外链匿名只读页，不要求登录态（已在 check_login 放行）。"""
    return render_template('test_report_share.html', share_token=token)


@views_bp.route('/audit-logs')
def audit_logs():
    r = _require_role('super_admin')
    if r: return r
    return render_template('audit_logs.html')


@views_bp.route('/users')
def users_management():
    return render_template('users.html')


@views_bp.route('/office')
def pixel_office():
    """像素办公室 — 可视化 OpenClaw 状态"""
    return render_template('office.html')


@views_bp.route('/testplans')
def testplans():
    """测试计划排期"""
    return render_template('testplans.html')


@views_bp.route('/automation-closed-loop')
def automation_closed_loop():
    """项目级自动化闭环只读驾驶舱（P0-J）。"""
    return render_template('automation_closed_loop.html')


@views_bp.route('/agent-eval')
def agent_eval_page():
    """Agent岗位评测、双评校准与运行回读。"""
    value = current_app.config.get('AGENT_TEAM_CONTRACTS_ENABLED', False)
    enabled = (
        value.lower() in ('1', 'true', 'yes', 'on')
        if isinstance(value, str) else bool(value))
    return render_template(
        'agent_eval.html',
        agent_team_contracts_enabled=enabled)


@views_bp.route('/agent-teams')
def agent_teams_page():
    """Project teams: each team owns its manager and multi-agent roster."""
    return render_template('agent_teams.html')


@views_bp.route('/engineering')
@views_bp.route('/engineering/baselines/<int:baseline_id>')
@views_bp.route('/engineering/refresh/<int:batch_id>')
@views_bp.route('/engineering/architecture/<int:snap_id>')
def engineering(baseline_id=None, batch_id=None, snap_id=None):
    """工程分析中心：基线管理 + 增量刷新批次 + 架构快照 + 影响项闭环。

    Deep link 入口：
      /engineering/baselines/{id}     基线详情
      /engineering/refresh/{id}       刷新批次详情
      /engineering/architecture/{id}  架构快照详情（用于跨 OpenClaw 引用）
    """
    return render_template(
        'engineering.html',
        initial_baseline_id=baseline_id,
        initial_batch_id=batch_id,
        initial_arch_snap_id=snap_id,
    )


@views_bp.route('/requirements')
@views_bp.route('/requirements/iterations/<int:iteration_id>')
@views_bp.route('/requirements/items/<int:item_id>')
def requirements(iteration_id=None, item_id=None):
    """需求分析中心：迭代需求快照 + 变更跟踪 + 用例/工程关联"""
    return render_template(
        'requirements.html',
        initial_iteration_id=iteration_id,
        initial_item_id=item_id,
    )
