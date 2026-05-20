## ClawTeam · OA/NGN 接入实战手册

> 沉淀自 2026-05-12 接入 `clawteam.woa.com` 全流程踩坑实录。
> 适用范围：所有 `*.woa.com` 内网站点（公司 NGN 网关已统一接管 SSO 的场景）。
> 关联 MEMORY 条目：#119、#120、#121、#122。

---

### 0. TL;DR（90 秒读完）

公司域名 `*.woa.com` 接入 SSO 的**正确路径只有一条**：

```
浏览器 → SLB/NGN 网关 → 完成 passport SSO → 注入 X-Tai-Identity 头 → 我们的 Flask
```

我们**不需要自己去解析 passport ticket**、**不需要调 RIO/SOAP 接口**、**不需要注册 OAuth appkey**。
NGN 网关在 SLB 那层全做掉了，我们只需要：

1. **登录**：在 Flask `before_request` 解 `X-Tai-Identity`（JWE，对称密钥就是太湖应用 Token），拿到 `LoginName/StaffId/Expiration`，直接登录到 session。
2. **登出**：跳到 `https://std.passport.woa.com/modules/passport/signout.ashx?url=<本站 /login?logout=1>`，让 passport 清全局 ticket；同时本地 `Set-Cookie ...=; Max-Age=0` 清 7+ 条 NGN 残留 cookie。
3. **联调**：通过太湖官方 `tai-skill` (OAuth2 PAR+PKCE) 完成 `site_check / site_status / app_list_by_owner` 等域名归属确认，**不要**走 `tai-skill` 之外的途径硬猜 appkey。

---

### 1. 走过的弯路（先记错误，避免重蹈覆辙）

#### 1.1 弯路一：调内部 Go 服务解 ticket
- 现象：Go 服务 `:8080/goApi/tofPassportDecryptTicketWithClientIp` 永远返回 `"非法Code，哈希值不匹配"` 或 `"code已过期"`。
- 根因：Go 服务 `tof4.auth.url` 指向的是 `devnet.rio.tencent.com`，passport 在 prod 签发的 ticket 跨环境验证哈希必然不过。
- 教训：**第三方 Go 中间件路径越长越脆**，链路上任一环境/IP 段不一致都会"哈希不匹配"，且错误信息毫无指向。

#### 1.2 弯路二：Hub 自己直连 RIO `AccessToken` 接口
- 实现：`requests.get('http://rio.tencent.com/ebus/tof4/api/v1/passport/AccessToken', params={'appkey':..., 'code': urlencoded(ticket)}, headers={'x-rio-paasid', 'x-rio-timestamp', 'x-rio-nonce', 'x-rio-signature'})`，signature = `SHA256(ts+token+nonce+ts).upper()`。
- 现象：RIO 链路通了（`TOF 500: illegal base64 data` 说明请求到位），但真 ticket 仍 `errcode=-9106 非法Code`。
- 根因：passport 把 ticket 绑死了**签发时的 `aud` 字段（客户端 IP）**，必须 IP 一致才能验证通过；Flask 部署在内网 SLB 后面，源 IP 永远是 SLB，跟用户真实 IP 对不上。
- 弯路修补：换 SOAP `DecryptTicketWithClientIP` 接口，从 ticket 里解出 `aud` 当 client IP 传……依然不通。
- 教训：**只要域名上了 NGN，我们后端就不该看到 ticket**。`appkey=ngn` 这个 query param 是关键线索——它表明 SSO 不是为我们签发的，是为 NGN 网关签发的。

#### 1.3 弯路三：抄别的系统的 appkey
- 试了 `claw_team`（这是 RIO paasid，不是 passport OAuth appkey）、抄 gametool 的 `zftzvzxnxrhdgffcgxhdgzvcmgcmfqcwl`（人家的 appkey）。
- 结果：passport 直接静默 302 回 `/login?sso_error=missing_ticket`——**根本没签发 ticket**。
- 教训：**OAuth appkey 必须是域名注册时申请的那个**，不能借用，跨应用借用 passport 一句不发地拒掉。

#### 1.4 弯路四：登出后跳 `https://passport.woa.com`
- 现象：用户报"退出对了，但重新点击登录提示 404 page not found"。
- 实测：`curl https://passport.woa.com/` 返回纯文本 `404 page not found`；`https://std.passport.woa.com/` 同样 404。
- 根因：两个 passport 域名只绑了具体 endpoint（`/modules/passport/signin.ashx` 等），**根路径没站点**。
- 教训：**所有"SSO 登出后回跳"的 URL，绝对不要写外部域的根路径**。安全做法：回跳本站登录页 `https://<本站>/login?logout=1`。

---

### 2. 最终方案：架构图

```
┌────────────────────────────────────────────────────────────────────┐
│ Browser (用户内网，Chrome/Edge 等)                                    │
└──────────────┬─────────────────────────────────────────────────────┘
               │ HTTPS → clawteam.woa.com
               ▼
┌────────────────────────────────────────────────────────────────────┐
│ SLB / NGN 网关 (公司基础设施层)                                       │
│  - 检查 cookie `x_host_key` / `bk_ticket`                           │
│  - 无 ticket → 302 std.passport.woa.com/signin.ashx?appkey=ngn      │
│    用户输 RTX 或公司域静默 SSO 完成认证                                │
│  - 有 ticket → 注入 HTTP 头：                                        │
│       X-Tai-Identity: <JWE Compact, alg=dir, enc=A256GCM>          │
│       X-Tai-Identity-Mode: 1                                       │
│       X-Real-Ip / X-Client-Ip / X-Sga-Mn 等                         │
└──────────────┬─────────────────────────────────────────────────────┘
               │ HTTP 内网（含 X-Tai-Identity）
               ▼
┌────────────────────────────────────────────────────────────────────┐
│ nginx (本机反代 → gunicorn:18800)                                    │
│  + ProxyFix(x_for=1,x_proto=1,x_host=1,x_prefix=1)                 │
└──────────────┬─────────────────────────────────────────────────────┘
               │
               ▼
┌────────────────────────────────────────────────────────────────────┐
│ Flask (web/app/views/__init__.py::check_login)                     │
│  ① _consume_tai_identity()  ← 第一优先级                            │
│     decode_x_tai_identity(header_value, TAI_APP_TOKEN)             │
│       jwcrypto: alg=dir + enc=A256GCM, key=32B token               │
│     User.query.filter_by(username=LoginName).first_or_create()     │
│     session['user_id'] = user.id; session.permanent = True         │
│  ② whitelist: /login /login/woa /logout /_ngn_probe (admin only)   │
│  ③ fallback: 旧 _consume_woa_ticket（NGN 接管后基本不触发）          │
└────────────────────────────────────────────────────────────────────┘
```

---

### 3. 实施步骤（下次新站点照搬）

#### Step 1：确认域名是否已接入 NGN（**最关键的一步**）

```bash
# 用浏览器 F12 → Network 看任一未登录请求的 Response Headers
# 接入 NGN 的标志：
#   - 302 跳到 std.passport.woa.com/...signin.ashx?appkey=ngn&...&url=...%2F_auth_login%2F...
#   - 登录后请求头里能看到 X-Tai-Identity / X-Tai-Identity-Mode

# 已接入 NGN：直接跳到 Step 3
# 未接入 NGN：联系平台 SRE 接入太湖网关（推荐路径），或继续自研 RIO（不推荐）
```

#### Step 2：通过 `tai-skill` 完成站点登记与归属确认

```powershell
# 本地操作（一次性，token 缓存 10080 min ≈ 7 天）
cd F:\Code\claw_team\.tai-skill
.\scripts\tai-auth.ps1   # 浏览器完成 OAuth2 PAR+PKCE 授权

# 查域名是否已接入太湖
.\scripts\download_skill.ps1 -SubSkill auth/tai-auth-access
# 用 site_check / site_status / app_list_by_owner 三个 MCP 工具
# 参数名是 domain= 不是 host=，别踩坑
```

> 关键陷阱：tai-skill 原始路径 `F:\文件下载\谷歌下载\tai-skill\` 含中文，PowerShell 会 `CommandNotFoundException`。**必须**先 `xcopy` 到 ASCII 路径（如 `F:\Code\claw_team\.tai-skill\`）再用。

#### Step 3：实现 `X-Tai-Identity` 解密模块

文件：`web/app/tai_identity.py`

```python
import os, json, base64
from datetime import datetime, timezone
from typing import Optional, Tuple
from jwcrypto import jwe, jwk

TAI_APP_TOKEN = os.getenv('TAI_APP_TOKEN', '')   # 太湖应用 Token，32 字符
TAI_CLOCK_SKEW_SECONDS = 180                      # 时钟偏差 3 分钟

class Identity:
    def __init__(self, login_name, staff_id, expiration):
        self.login_name = login_name
        self.staff_id = staff_id
        self.expiration = expiration

def decode_x_tai_identity(header_value: str,
                          token: Optional[str] = None) -> Tuple[bool, object]:
    token = (token or TAI_APP_TOKEN).strip()
    if not header_value or len(token) != 32:
        return False, {'error': 'invalid_input'}
    key_bytes = token.encode('utf-8')
    k_b64 = base64.urlsafe_b64encode(key_bytes).rstrip(b'=').decode('ascii')
    sym_key = jwk.JWK(kty='oct', k=k_b64)
    obj = jwe.JWE()
    obj.deserialize(header_value, key=sym_key)
    payload = json.loads(obj.payload.decode('utf-8'))
    # ... 校验 Expiration + clock skew，返回 Identity ...
```

依赖：`requirements.txt` 加 `jwcrypto>=1.5.1,<1.5.2`（兼容 Python 3.7）。

#### Step 4：改 `check_login` 钩子

```python
@views_bp.before_request
def check_login():
    # 第一优先级：NGN 注入的身份头
    if not session.get('user_id'):
        _consume_tai_identity()

    # 白名单（即使没登录也放行）
    if request.path in ('/login', '/login/woa', '/logout'):
        return None

    if request.path == '/_ngn_probe':   # 调试端点
        return _ngn_probe()

    # 兼容旧 SSO 流程（NGN 接管后基本不触发）
    if request.path == '/woa' or request.args.get('ticket', '').startswith('TOF4T'):
        return _consume_woa_ticket()

    uid = session.get('user_id')
    if not uid:
        return redirect('/login')
    # ... 用户存在性校验 ...
```

#### Step 5：实现登出（**两端协同**）

后端 `views/__init__.py`：

```python
@views_bp.route('/logout', methods=['GET', 'POST'])
def logout_page():
    session.clear()
    logout_url = os.getenv('WOA_LOGOUT_URL',
        'https://std.passport.woa.com/modules/passport/signout.ashx')
    # ⚠️ 不要写 'https://passport.woa.com'，根路径 404
    default_return = request.host_url.rstrip('/') + '/login?logout=1'
    return_url = os.getenv('WOA_LOGOUT_RETURN_URL', default_return)
    target = f'{logout_url}?url={urllib.parse.quote(return_url, safe="")}'

    resp = redirect(target)
    resp.set_cookie('openclaw_session', '', expires=0, path='/', samesite='Lax')
    for ck in ('x_host_key', 'x-host-key-ngn', 'x_host_key_access_https',
               'x-client-ssid', 'bk_ticket', 'bk_uid'):
        resp.set_cookie(ck, '', expires=0, path='/')
    return resp
```

前端 `base.html`：

```html
<a href="/logout" onclick="return doLogout(event)">登出</a>
<script>
function doLogout(ev) {
    // 必须页面级跳转（fetch 不行，浏览器不会 follow 跨域 302）
    if (ev) ev.preventDefault();
    window.location.href = '/logout';
    return false;
}
</script>
```

前端 `login.html` 显示登出提示：

```javascript
const qs = new URLSearchParams(window.location.search);
const box = document.getElementById('sso-error-msg');
if (qs.get('logout') === '1') {
    box.style.background = '#e6f7ff';
    box.style.color = '#0958d9';
    box.style.border = '1px solid #91caff';
    box.textContent = '已成功退出登录。如需重新进入，请点击下方"WOA 登录"。';
    box.style.display = '';
}
```

#### Step 6：环境变量与权限

```bash
# testserver
sudo tee -a /opt/openclaw-web/.env <<EOF
TAI_APP_TOKEN=<32 字符太湖应用 Token>
WOA_LOGOUT_URL=https://std.passport.woa.com/modules/passport/signout.ashx
# WOA_LOGOUT_RETURN_URL 默认值已可用，无需配置；
# 如需登出后跳企业内部门户可在这里覆盖
EOF
sudo chmod 600 /opt/openclaw-web/.env
sudo systemctl restart openclaw-web
```

---

### 4. 调试三板斧

#### 4.1 `/_ngn_probe` 端点
- **必须**带 `@views_bp.route` 装饰器，否则 Blueprint 的 `before_request` 不触发，直接 404。
- **必须**做 `super_admin` 鉴权，输出过滤掉 `Cookie` header（含 `bk_ticket / openclaw_session`，有泄漏风险）。
- 返回：所有上游 header + `X-Tai-Identity` 解密结果 + session 状态。

#### 4.2 access log 看 SSO 是否真的走通
- 进入 SSO 流程：`access.log` 里反复看到 `GET / HTTP/1.1 302 ... referer https://std.passport.woa.com/` → NGN 在反复重定向，**根本没认证成功**，去 F12 看请求头是否有 `X-Tai-Identity`。
- 票据签发流程异常：`GET /api/v1/auth/woa-callback?ticket=TOF4T... 401` → 解密失败，看 callback 实现里 RIO/SOAP 报错。
- 正常 NGN 流程：**用户根本不应该访问 `/login` 或 `/woa`**，所有路径都是 200。

#### 4.3 本地 curl 模拟 host
```bash
# dev 环境用 127.0.0.1 + Host header 模拟生产
curl -i -H 'Host: clawteam.woa.com' \
        -H 'X-Forwarded-Proto: https' \
        -H 'X-Forwarded-Host: clawteam.woa.com' \
     http://127.0.0.1:18800/logout
# 期望：302 Location: https://std.passport.woa.com/.../signout.ashx?url=https%3A%2F%2Fclawteam.woa.com%2Flogin%3Flogout%3D1
```

---

### 5. 部署/运维要点

| 项 | 内容 |
|---|---|
| Flask 加载路径 | `gunicorn` cwd 在 `/opt/openclaw-web`，加载的是 `app/`，**不是** `web/app/`（历史目录易误判） |
| 配置文件 | `/opt/openclaw-web/.env`（`python-dotenv` 自动加载），`chmod 600` |
| 反代中间件 | `ProxyFix(x_for=1, x_proto=1, x_host=1, x_prefix=1)`，识别 `X-Forwarded-Proto` 让 `request.host_url` 拼出 `https://` |
| Session | `PERMANENT_SESSION_LIFETIME=30 days`，`SESSION_COOKIE_NAME=openclaw_session`，`SAMESITE=Lax`，登录成功必须 `session.permanent=True` |
| NGN 残留 cookie | 登出时显式清 7 条：`x_host_key / x-host-key-ngn / x_host_key_access_https / x-client-ssid / bk_ticket / bk_uid / openclaw_session` |
| PowerShell 部署 | `&&` 在 PowerShell 5.1 不支持，多命令用 `;`；含 `$(...)` 的子 shell 远端最好写脚本 scp 后 `bash /tmp/xxx.sh` 执行 |

---

### 6. 一键回归测试脚本

```bash
# testserver 上执行
echo '--- /login 200 ---'
curl -sS -o /dev/null -w 'status=%{http_code}\n' http://127.0.0.1:18800/login

echo '--- /login?logout=1 200 + 含蓝色提示 ---'
curl -sS http://127.0.0.1:18800/login?logout=1 | grep -o '已成功退出'

echo '--- /login/woa 302 → passport signin ---'
curl -sS -o /dev/null -w 'status=%{http_code} loc=%{redirect_url}\n' \
     --max-redirs 0 http://127.0.0.1:18800/login/woa

echo '--- /logout 302 → passport signout + 7 条 Set-Cookie ---'
curl -sS -i -H 'Host: clawteam.woa.com' -H 'X-Forwarded-Proto: https' \
     http://127.0.0.1:18800/logout | grep -E '^(HTTP|Location|Set-Cookie):'
```

---

### 7. 经验沉淀清单（给未来的自己）

- 🔴 **最大教训**：调试 SSO 卡住超过 1 小时时，立刻去 F12 看请求头里有没有 `X-Tai-Identity` —— 有则一切自研 SSO 都是死循环。
- 🟠 域名根 URL（`xxx.woa.com/`）绝对不要假设它一定 200，先 `curl` 验证。
- 🟡 `appkey=ngn` 是 NGN 接管的明确信号，看到立刻停手不要再硬猜 appkey。
- 🟢 太湖 token = RIO paasToken = 太湖应用 Token，长度严格 32 字节，UTF-8 字符串直接当对称密钥用，不要 base64 解一遍。
- 🔵 Flask 的 `Blueprint.before_request` 只对**已注册路由**生效，要做"任意 path 都拦截"的 hook 必须搭配 `@route('/<path:_>')` 或者用 `app.before_request`。
- 🟣 浏览器跨域 302 `fetch` 不会 follow，登出/登录跳转都用 `window.location.href` 整页跳。

---

*作者：rajqiu* · *日期：2026-05-12* · *相关代码：`web/app/{views/__init__.py, tai_identity.py, api/auth.py}`、`web/templates/{login.html, base.html}`*
