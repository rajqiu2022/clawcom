#!/usr/bin/env python3
"""hub-sse-sidecar v2 — 事件驱动 + 显式状态机 + 无伪造兜底

设计目标
    1. 通信链路稳定：SSE 长连接 + 自动重连，不轮询不耗 token
    2. 状态显式上报：processing → done/failed，让 Hub 能区分"在干"和"卡死"
    3. 配置中心化：所有运行时参数从 Hub 拉，本地只存 CLAW_ID / TOKEN / HUB_URL
    4. 无伪造兜底：sidecar 不再"代替 LLM 完成 todo"或"1.5 秒自动 mark read"，
       所有兜底由 Hub 的 timeout_watcher 承担（超时告警 owner）

vs v1（hub_worker.py）
    - 移除 inbox 文件中转 → 直接 SSE 收 message → 起线程调 LLM
    - 移除 1.5s 自动 mark read → 只有 LLM 真返回才 mark done
    - 移除 force_complete todo → todo 由 LLM 自己处理 + Hub 5min 兜底告警
    - 移除本地 sidecar.env → 启动时从 Hub 拉配置（首次部署后免运维）

依赖
    Python 3.6+，无第三方包（标准库 urllib / threading / subprocess / json / signal）

运行
    必需环境变量：
        HUB_URL=http://clawteam.woa.com:18800
        CLAW_ID=5
        CLAW_TOKEN=xxxxx
    可选：
        TIMEOUT_AGENT_BOOTSTRAP=10  - 首次拉配置失败的重试间隔
        SSE_RECONNECT_SEC=5         - SSE 断后重连间隔
        TODO_LOOP_SEC=120           - todo 兜底拉取间隔（Hub 也通过 SSE 推，这里只是兜底）

退出码
    0 - 正常退出
    2 - 启动配置加载失败（HUB_URL/CLAW_ID/CLAW_TOKEN 不全 或 Hub 不可达）
"""

import json
import os
import queue
import signal
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

SIDECAR_VERSION = '2.7.0'

HUB_URL = os.getenv('HUB_URL', '').rstrip('/')
CLAW_ID = os.getenv('CLAW_ID', '').strip()
CLAW_TOKEN = os.getenv('CLAW_TOKEN', '').strip()

SSE_RECONNECT_SEC = int(os.getenv('SSE_RECONNECT_SEC', '5'))
TODO_LOOP_SEC = int(os.getenv('TODO_LOOP_SEC', '120'))
CONFIG_REFRESH_SEC = int(os.getenv('CONFIG_REFRESH_SEC', '60'))
TODO_RETRY_SEC = int(os.getenv('TODO_RETRY_SEC', '3600'))
TODO_SUCCESS_COOLDOWN_SEC = int(os.getenv('TODO_SUCCESS_COOLDOWN_SEC', '300'))
WORKFLOW_HEARTBEAT_SEC = int(os.getenv('WORKFLOW_HEARTBEAT_SEC', '30'))
TODO_WORKER_ENABLED = os.getenv('TODO_WORKER_ENABLED', 'true').lower() not in (
    '0', 'false', 'no', 'off'
)

_stop_event = threading.Event()
_config = {}
_config_lock = threading.Lock()
# 已派发处理的 msg_id，避免 SSE 偶尔重推时重复执行 LLM
_inflight_msgs = set()
_inflight_lock = threading.Lock()
_todo_queue = queue.Queue()
_todo_lock = threading.Lock()
_queued_todos = set()
_todo_cooldown_until = {}
_codex_provider = None
_codex_provider_lock = threading.Lock()
_execution_slots = threading.BoundedSemaphore(
    max(1, int(os.getenv('MAX_CONCURRENT_AGENT_TASKS', '2')))
)
_wecom_channel = None


def log(msg):
    print(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] {msg}', flush=True)


def http(method, path, body=None, timeout=30, query=None):
    """HTTP 调用 Hub，统一带 Bearer Token。

    Returns: (status_code: int, body: dict)  status=0 表示连接异常
    """
    url = f"{HUB_URL}{path}"
    if query:
        url += ('&' if '?' in url else '?') + urllib.parse.urlencode(query)
    data = json.dumps(body).encode('utf-8') if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header('Authorization', f'Bearer {CLAW_TOKEN}')
    if data is not None:
        req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode('utf-8') or '{}'
            return r.status, json.loads(raw) if raw.strip().startswith(('{', '[')) else {}
    except urllib.error.HTTPError as e:
        try:
            body = json.loads((e.read() or b'').decode('utf-8') or '{}')
        except Exception:
            body = {}
        return e.code, body
    except Exception as e:
        return 0, {'error': f'{type(e).__name__}: {e}'}


# ===================== Hub 能力索引（Rule #19 速查注入） =====================

def capability_digest_lines():
    """把 Hub 下发的能力索引摘要渲染成 prompt 行（每次回复/执行前必读）。

    digest 随 /sidecar-config 每 60s 刷新一起下发；Hub 未提供时返回空，不影响旧行为。
    """
    digest = get_cfg('hub_capability_digest', '') or ''
    digest = digest.strip()
    if not digest:
        return []
    return [
        '=== Hub 能力索引（Rule #19 速查，动手/回复前必读）===',
        digest,
        '=== 能力索引结束 ===',
    ]


def memo_index_lines():
    """把 Hub 下发的笔记索引渲染成 prompt 行（三层记忆·记全层）。

    memo_index 随 /sidecar-config 下发，含当前任务上下文(taskctx)与常驻决策(decision)。
    只给"索引"（标题+摘要+id），需要全文时用 GET {HUB}/api/v1/memos/memo/{id} 取。
    跨 session 保留：上下文压缩 / 每日会话重置后，仍会随下一次注入回到 prompt。
    """
    items = get_cfg('memo_index', []) or []
    if not isinstance(items, list) or not items:
        return []
    taskctx = [it for it in items if it.get('kind') == 'taskctx']
    decision = [it for it in items if it.get('kind') == 'decision']

    def _fmt(it):
        title = (it.get('title') or '').strip()
        summary = (it.get('summary') or '').strip()
        mid = it.get('id') or ''
        tail = f'：{summary}' if summary and summary != title else ''
        return f'- (id={mid}) {title}{tail}'

    lines = [
        f'=== 你的持久笔记索引（跨 session 保留；取全文：GET {HUB_URL}/api/v1/memos/memo/{{id}}）===',
    ]
    if taskctx:
        lines.append('【当前任务上下文】')
        lines.extend(_fmt(it) for it in taskctx)
    if decision:
        lines.append('【关键决策/配置】')
        lines.extend(_fmt(it) for it in decision)
    lines.append('=== 笔记索引结束（相关项请先取全文再作答，勿凭记忆臆测）===')
    return lines


def agent_profile_lines():
    """Render Hub-managed agent profile into every LLM prompt."""
    item = get_cfg('active_agent_profile') or {}
    if not isinstance(item, dict):
        return []
    profile = item.get('profile') or {}
    if not isinstance(profile, dict):
        return []
    lines = [
        '=== Hub Agent 岗位说明书（必须遵守）===',
        f"- 工位：{item.get('post_name') or item.get('post_key') or ''}",
        f"- Profile：{profile.get('name') or profile.get('profile_key') or ''} v{profile.get('version') or 1}",
    ]
    system_prompt = (profile.get('system_prompt') or '').strip()
    workflow_config = (profile.get('workflow_config') or '').strip()
    required_skills = profile.get('required_skills') or []
    if required_skills:
        lines.append(
            '- 建议 Skills（可用时优先加载）：'
            + ', '.join(str(x) for x in required_skills)
        )
    blocking_skills = profile.get('blocking_skills') or []
    if blocking_skills:
        lines.append(
            '- 阻断型 Skills（缺失则停止）：'
            + ', '.join(str(x) for x in blocking_skills)
        )
    if system_prompt:
        lines.extend(['', '岗位职责：', system_prompt])
    if workflow_config:
        lines.extend(['', '工作规范：', workflow_config])
    lines.append('=== 岗位说明书结束 ===')
    return lines


# ===================== 任务上下文（记忆路由注入） =====================

def fetch_task_context(ref_type, ref_id):
    """拉取统一任务上下文包（required_skills / preflight / top_pitfalls / references）。

    ref_type ∈ {'todo', 'agent_task', 'workflow_step'}。失败返回 {}。
    """
    if not ref_id:
        return {}
    try:
        code, body = http('GET', f'/api/v1/tasks/{ref_type}/{ref_id}/context')
        if code == 200 and isinstance(body, dict):
            return body
        log(f'[ctx] {ref_type}/{ref_id} code={code}')
    except Exception as e:
        log(f'[ctx] 拉取 {ref_type}/{ref_id} 异常: {e}')
    return {}


def format_task_context(tc):
    """把上下文包渲染成给 LLM 的 prompt 行（执行前必读）。"""
    if not isinstance(tc, dict):
        return []
    lines = []
    skills = tc.get('required_skills') or []
    if skills:
        lines.append(
            '建议 Skill（可用时优先加载）：'
            + '、'.join(str(s) for s in skills)
        )
    blocking = tc.get('blocking_skills') or []
    if blocking:
        lines.append(
            '阻断型 Skill（缺失则停止）：'
            + '、'.join(str(s) for s in blocking)
        )
    if skills or blocking:
        lines.append(
            'Skill 可用性规则：未加载的可选 Skill 不得使用或声称已使用；'
            '记录降级后继续。只有上述阻断型 Skill 缺失时才停止任务。'
        )
    op = tc.get('operating_protocol_skill')
    if op:
        lines.append(f'执行协议 Skill：{op}（可用时遵循其生命周期）')
    checklist = tc.get('preflight_checklist') or []
    if checklist:
        lines.append('执行前铁律（逐条确认后再动手）：')
        for i, item in enumerate(checklist, 1):
            lines.append(f'  {i}. {item}')
    pitfalls = tc.get('top_pitfalls') or []
    if pitfalls:
        lines.append('高相关历史踩坑（务必规避）：')
        for p in pitfalls:
            if isinstance(p, dict):
                lines.append(f"  - {p.get('title', '')}：{p.get('solution', '')}")
    refs = tc.get('references') or []
    if refs:
        lines.append('相关经验参考：')
        for r in refs:
            if isinstance(r, dict):
                t = r.get('title') or r.get('name') or ''
                u = r.get('url') or r.get('link') or ''
                lines.append(f'  - {t} {u}'.rstrip())
            else:
                lines.append(f'  - {r}')
    return lines


# ===================== 配置 =====================

def fetch_config():
    """从 Hub 拉 sidecar 配置 + 心跳。"""
    query = {'sidecar_version': SIDECAR_VERSION}
    local_agent_type = os.getenv('AGENT_TYPE', '').strip().lower()
    if local_agent_type:
        query['runtime_agent_type'] = local_agent_type
    code, body = http('GET', f'/api/openclaws/{CLAW_ID}/sidecar-config', query=query)
    if code != 200:
        log(f'[config] 拉取失败 code={code} body={body}')
        return None
    return body


def get_cfg(key, default=None):
    with _config_lock:
        return _config.get(key, default)


def set_cfg(new_cfg):
    global _config
    with _config_lock:
        old_version = _config.get('config_version', 0)
        _config = new_cfg
        new_version = new_cfg.get('config_version', 0)
        if new_version != old_version:
            log(f'[config] 已加载 v{new_version}: agent_type={new_cfg.get("agent_type")} '
                f'bin={new_cfg.get("openclaw_bin", "(none)")} '
                f'agent={new_cfg.get("agent_name")} timeout={new_cfg.get("agent_timeout")}s '
                f'wecom_enabled={new_cfg.get("wecom_enabled")} '
                f'owner_wecom_userid={new_cfg.get("owner_wecom_userid", "(none)")!r}')


_llm_apply_lock = threading.Lock()


def _apply_env_lines(env_text, updates):
    """把 updates(dict) 覆盖写进 .env 文本，缺失的 key 追加到末尾。"""
    seen = set()
    out = []
    for ln in env_text.splitlines():
        key = ln.split('=', 1)[0].strip() if ('=' in ln and not ln.lstrip().startswith('#')) else None
        if key in updates:
            out.append(f'{key}={updates[key]}')
            seen.add(key)
        else:
            out.append(ln)
    for k, v in updates.items():
        if k not in seen:
            out.append(f'{k}={v}')
    return '\n'.join(out) + '\n'


def apply_llm_config(cfg):
    """把 Hub 下发的 llm_apply 落到本机 config.yaml + .env，并重启 gateway。

    幂等：与现有配置一致时不写盘、不重启。任何异常都吞掉，绝不影响 sidecar 主流程。
    仅当 Hub 下发 llm_apply（新版 Hub）时才生效；老 Hub 无此字段则跳过。
    """
    la = cfg.get('llm_apply') if isinstance(cfg, dict) else None
    if not isinstance(la, dict):
        return
    provider = (la.get('provider') or '').strip()
    config_model = (la.get('config_model') or '').strip()
    if not provider or not config_model:
        return
    hermes_home = (get_cfg('hermes_home') or os.getenv('HERMES_HOME', '')).strip()
    if not hermes_home:
        return
    cfg_path = os.path.join(hermes_home, 'config.yaml')
    env_path = os.path.join(hermes_home, '.env')
    if not os.path.isfile(cfg_path):
        return

    with _llm_apply_lock:
        try:
            content = open(cfg_path, encoding='utf-8').read()
        except Exception as e:
            log(f'[llm] 读 config.yaml 失败: {e}')
            return

        api_mode = (la.get('api_mode') or 'chat_completions').strip()
        base_url = (la.get('base_url') or '').strip()
        api_key_env = (la.get('api_key_env')
                       or ('TIMIAI_API_KEY' if provider == 'timiai' else 'VENUS_API_KEY')).strip()
        try:
            context_length = int(la.get('context_length') or 128000)
        except Exception:
            context_length = 128000

        block = (
            "model:\n"
            f"  default: {config_model}\n"
            f"  provider: {provider}\n"
            f"  api_mode: {api_mode}\n"
            "providers:\n"
            f"  {provider}:\n"
            "    type: openai_compatible\n"
            f"    base_url: {base_url}\n"
            "    api_key: ${" + api_key_env + "}\n"
            f"    default_model: {config_model}\n"
            f"    api_mode: {api_mode}\n"
            f"    context_length: {context_length}\n"
        )

        mi = content.find('model:')
        marker = '\nfallback_providers:'
        idx = content.find(marker)
        if mi < 0 or idx < 0 or idx <= mi:
            log('[llm] config.yaml 结构不识别（缺 model:/fallback_providers:），跳过 llm 应用')
            return
        new_content = content[:mi] + block + content[idx + 1:]

        changed = False
        if new_content != content:
            try:
                if not os.path.isfile(cfg_path + '.bak_preLLMapply'):
                    shutil.copy2(cfg_path, cfg_path + '.bak_preLLMapply')
                with open(cfg_path, 'w', encoding='utf-8') as f:
                    f.write(new_content)
                changed = True
                log(f'[llm] config.yaml 已更新 -> provider={provider} model={config_model} ctx={context_length}')
            except Exception as e:
                log(f'[llm] 写 config.yaml 失败: {e}')
                return

        try:
            env_text = open(env_path, encoding='utf-8').read() if os.path.isfile(env_path) else ''
        except Exception:
            env_text = ''
        updates = {
            'HERMES_LLM_PROVIDER': provider,
            'HERMES_LLM_MODEL': (la.get('model') or config_model),
        }
        if provider == 'timiai' and la.get('timiai_project'):
            updates['HERMES_TIMIAI_PROJECT'] = la['timiai_project']
        if la.get('api_key'):
            updates[api_key_env] = la['api_key']
        new_env = _apply_env_lines(env_text, updates)
        if new_env != env_text:
            try:
                if os.path.isfile(env_path) and not os.path.isfile(env_path + '.bak_preLLMapply'):
                    shutil.copy2(env_path, env_path + '.bak_preLLMapply')
                with open(env_path, 'w', encoding='utf-8') as f:
                    f.write(new_env)
                changed = True
                log('[llm] .env 已更新（HERMES_LLM_PROVIDER/MODEL' +
                    ('/TIMIAI_PROJECT' if provider == 'timiai' else '') + '/API_KEY）')
            except Exception as e:
                log(f'[llm] 写 .env 失败: {e}')

        if changed:
            svc = (la.get('gateway_service') or f'hermes-gateway-claw-{CLAW_ID}.service').strip()
            try:
                r = subprocess.run(
                    ['systemctl', 'restart', svc],
                    capture_output=True, text=True, timeout=90,
                    encoding='utf-8', errors='replace',
                )
                log(f'[llm] 重启 gateway {svc} rc={r.returncode} '
                    f'{(r.stderr or "").strip()[:120]}')
            except Exception as e:
                log(f'[llm] 重启 gateway {svc} 失败: {e}')


def config_refresh_loop():
    while not _stop_event.is_set():
        try:
            new_cfg = fetch_config()
            if new_cfg:
                set_cfg(new_cfg)
                try:
                    apply_llm_config(new_cfg)
                except Exception as e:
                    log(f'[llm] 应用异常: {e}')
        except Exception as e:
            log(f'[config] 异常: {e}')
        _stop_event.wait(CONFIG_REFRESH_SEC)


# ===================== 调用 Agent 执行器 =====================

def _which_first(names):
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    return ''


def _build_agent_command(prompt, timeout, wecom_enabled):
    """Build argv/cwd for supported non-interactive agent executors."""
    agent_type = (get_cfg('agent_type', 'openclaw') or 'openclaw').strip().lower()
    bin_path = (get_cfg('openclaw_bin') or '').strip()
    agent_name = get_cfg('agent_name', 'main')
    hermes_home = (get_cfg('hermes_home') or os.getenv('HERMES_HOME', '')).strip()
    hermes_cwd = (
        os.getenv('HERMES_CWD', '').strip()
        or os.getenv('HERMES_INSTALL_DIR', '').strip()
        or hermes_home
    )

    if agent_type == 'openclaw':
        exe = bin_path or 'openclaw'
        cmd = [exe, 'agent', '--message', prompt,
               '--agent', agent_name, '--timeout', str(timeout)]
        if wecom_enabled:
            cmd += ['--channel', 'wecom']
        return cmd, None, agent_type

    if agent_type == 'hermes':
        # Preferred for explicit wrappers: install_v2.sh may create a wrapper
        # that accepts --message/--timeout. Native Hermes CLI uses chat -q.
        exe = bin_path or os.getenv('HERMES_BIN', '').strip()
        if exe:
            base = os.path.basename(exe).lower()
            if base in ('hermes', 'hermes.exe') or base in ('python', 'python.exe', 'python3'):
                return [exe, 'chat', '-q', prompt, '-Q', '--max-turns', str(timeout)], None, agent_type
            if base.startswith('hermes-agent'):
                return [exe, '--query', prompt, '--max_turns', str(timeout)], None, agent_type
            return [exe, '--message', prompt, '--timeout', str(timeout)], None, agent_type
        exe = _which_first(['hermes', 'hermes-agent'])
        if exe:
            base = os.path.basename(exe).lower()
            if base.startswith('hermes-agent'):
                return [exe, '--query', prompt, '--max_turns', str(timeout)], None, agent_type
            return [exe, 'chat', '-q', prompt, '-Q', '--max-turns', str(timeout)], None, agent_type
        if hermes_home:
            return [
                sys.executable, '-m', 'hermes_cli.main',
                'chat', '-q', prompt, '-Q', '--max-turns', str(timeout),
            ], hermes_cwd, agent_type
        return [], None, agent_type

    if agent_type == 'custom':
        exe = (os.getenv('CUSTOM_AGENT_BIN', '').strip() or bin_path)
        if exe:
            return [exe, '--message', prompt, '--timeout', str(timeout)], None, agent_type
        return [], None, agent_type

    return [], None, agent_type


def _cmd_for_log(cmd):
    logged = []
    skip_next = False
    for part in cmd:
        if skip_next:
            logged.append('<prompt>')
            skip_next = False
            continue
        logged.append(part)
        if part in ('--message', '--prompt', '-q', '--query'):
            skip_next = True
    return ' '.join(logged)


_CODEX_ENV_ALLOWLIST = (
    'CODEX_HOME', 'HOME', 'PATH', 'LANG', 'LC_ALL', 'LC_CTYPE',
    'TMPDIR', 'TEMP', 'TMP', 'SSL_CERT_FILE', 'SSL_CERT_DIR',
    'REQUESTS_CA_BUNDLE', 'CURL_CA_BUNDLE', 'HTTP_PROXY', 'HTTPS_PROXY',
    'NO_PROXY', 'http_proxy', 'https_proxy', 'no_proxy',
)


def _codex_environment(source):
    codex_home = str(source.get('CODEX_HOME') or '').strip()
    if not codex_home:
        raise ValueError('CODEX_HOME is required')
    environment = {
        name: str(source[name]) for name in _CODEX_ENV_ALLOWLIST
        if name in source and str(source[name])
    }
    environment['CODEX_HOME'] = codex_home
    environment['PYTHONIOENCODING'] = 'utf-8'
    environment['PYTHONUTF8'] = '1'
    return environment


def _run_codex_process(cmd, prompt, timeout, env, cwd, stdout_mode='raw'):
    """Run Codex in a Linux process group and kill all descendants on timeout."""
    if stdout_mode != 'raw':
        raise ValueError('Codex worker requires raw protocol output')
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=cwd or None,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(
            prompt.encode('utf-8'), timeout=timeout
        )
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()
        raise
    limit = 2 * 1024 * 1024
    if len(stdout) > limit or len(stderr) > limit:
        raise ValueError('Codex worker output exceeds protocol limit')
    return subprocess.CompletedProcess(
        cmd,
        proc.returncode,
        stdout.decode('utf-8', errors='replace'),
        stderr.decode('utf-8', errors='replace'),
    )


def _call_codex_provider(prompt, timeout, task_kind, session_key=None):
    """Invoke the SDK inside a timeout-bounded, secret-minimized process tree."""
    global _codex_provider
    # Defense in depth: Hub-authored message/todo/workflow fields must not be
    # able to echo the locally configured credential into the SDK prompt.
    if CLAW_TOKEN:
        prompt = prompt.replace(CLAW_TOKEN, '<redacted>')
    try:
        from codex_sdk_provider import CodexSdkProvider
        from provider_runtime import CancellationToken, ProviderInvocation
    except Exception as exc:
        return False, '', f'codex_provider_import_failed:{type(exc).__name__}'

    with _codex_provider_lock:
        if _codex_provider is None:
            _codex_provider = CodexSdkProvider(
                model=os.getenv('CODEX_MODEL', '').strip() or None,
                process_runner=_run_codex_process,
            )
        provider = _codex_provider

    workspace = os.getenv('CODEX_WORKSPACE', '').strip() or None
    try:
        codex_environment = _codex_environment(os.environ)
    except ValueError:
        return False, '', 'provider_config_invalid'
    invocation = ProviderInvocation(
        invocation_id=str(uuid.uuid4()),
        task_kind=(task_kind if task_kind in ('message', 'todo', 'workflow', 'wecom')
                   else 'message'),
        prompt=prompt,
        context={'claw_id': CLAW_ID, 'source': 'hub-sse-sidecar'},
        session_key=session_key or f'hub-claw-{CLAW_ID}-{task_kind}',
        timeout_seconds=max(1, min(int(timeout), 3600)),
        workspace=workspace,
        execution_scope='repo_read',
        result_schema=None,
        environment=codex_environment,
    )

    def on_event(event):
        log(f'[codex] event={event.kind} message={event.message[:200]!r}')

    try:
        if not _execution_slots.acquire(blocking=False):
            return False, '', 'provider_busy'
        try:
            result = provider.invoke(invocation, on_event, CancellationToken())
        finally:
            _execution_slots.release()
    except Exception as exc:
        return False, '', f'codex_provider_error:{type(exc).__name__}'
    if not result.ok:
        return False, '', result.error or 'codex_provider_failed'
    return True, result.final_response, ''


def call_llm(prompt, task_kind='message', session_key=None):
    """根据当前 agent_type 调用对应 CLI，返回 (ok, response_text, err)。"""
    timeout = int(get_cfg('agent_timeout', 300))
    wecom_enabled = bool(get_cfg('wecom_enabled', False))
    # A managed deployment pins provider identity locally. Hub runtime config
    # may tune the provider, but cannot silently switch an installed binary.
    agent_type = (os.getenv('AGENT_TYPE', '').strip()
                  or get_cfg('agent_type', 'openclaw')
                  or 'openclaw').strip().lower()
    if agent_type == 'codex':
        log(f'[llm] 调用 codex sdk timeout={timeout}s prompt_len={len(prompt)}')
        return _call_codex_provider(prompt, timeout, task_kind, session_key)
    env = os.environ.copy()
    if wecom_enabled:
        wecom_key = get_cfg('wecom_bot_id', '')
        wecom_secret = get_cfg('wecom_bot_secret', '')
        if wecom_key:
            env['WECOM_KEY'] = wecom_key
            env['WECOM_BOT_ID'] = wecom_key
        if wecom_secret:
            env['WECOM_SECRET'] = wecom_secret
        env['WECOM_ALLOW_ALL_USERS'] = 'true'
        env['WECOM_DM_POLICY'] = 'open'
        owner_wecom = get_cfg('owner_wecom_userid', '')
        if owner_wecom:
            env['WECOM_HOME_CHANNEL'] = owner_wecom
            env['WECOM_HOME_CHANNEL_NAME'] = owner_wecom
    env.setdefault('PYTHONIOENCODING', 'utf-8')
    env.setdefault('PYTHONUTF8', '1')
    hermes_home = (get_cfg('hermes_home') or os.getenv('HERMES_HOME', '')).strip()
    if hermes_home:
        env['HERMES_HOME'] = hermes_home

    cmd, cwd, agent_type = _build_agent_command(prompt, timeout, wecom_enabled)
    if not cmd:
        return False, '', (
            f'unsupported or unconfigured agent_type={agent_type!r}; '
            'set openclaw_bin/HERMES_BIN or hermes_home for hermes'
        )

    log(f'[llm] 调用 {agent_type} timeout={timeout}s prompt_len={len(prompt)} '
        f'cmd={_cmd_for_log(cmd)!r} cwd={cwd or ""!r}')
    try:
        proc = subprocess.run(
            cmd, capture_output=True,
            timeout=timeout + 30, text=True,
            encoding='utf-8', errors='replace',
            env=env,
            cwd=cwd or None,
        )
        if proc.returncode != 0:
            return False, '', (
                f'returncode={proc.returncode} '
                f'stderr={(proc.stderr or "")[:500]}'
            )
        return True, (proc.stdout or '').strip(), ''
    except subprocess.TimeoutExpired:
        return False, '', f'subprocess timeout {timeout + 30}s'
    except FileNotFoundError:
        return False, '', f'binary not found: {cmd[0] if cmd else agent_type}'
    except Exception as e:
        return False, '', f'{type(e).__name__}: {e}'


# ===================== 消息处理 =====================

def _is_workflow_hub_notification(msg):
    """Workflow 可选聊天提醒，只提升可见性；节点完成必须走 AgentTask/result API。"""
    if (msg.get('msg_type') or '') != 'task_delegate':
        return False
    sender = (msg.get('sender_name') or '').strip()
    if sender != 'Workflow':
        return False
    content = msg.get('content') or ''
    return any(token in content for token in (
        'Workflow Step #',
        'Workflow Run #',
        'workflow_',
    ))


def _ack_workflow_notification(msg_id):
    """Ack Workflow reminder without invoking Agent LLM."""
    http('PUT', f'/api/openclaws/{CLAW_ID}/messages/{msg_id}/processing')
    http(
        'PUT', f'/api/openclaws/{CLAW_ID}/messages/{msg_id}/read',
        body={
            'reply': (
                '已收到 Workflow 节点通知。实际执行由 AgentTask 驱动，'
                '完成后 sidecar 会自动回写 Flow 结果。'
            )[:4000],
        },
    )
    http('PUT', f'/api/openclaws/{CLAW_ID}/messages/{msg_id}/done', body={})


def handle_message(msg):
    """处理一条 ClawMessage：processing → LLM → done/failed。"""
    msg_id = msg.get('id')
    if not msg_id:
        return

    with _inflight_lock:
        if msg_id in _inflight_msgs:
            return
        _inflight_msgs.add(msg_id)

    try:
        msg_type = msg.get('msg_type', 'text')
        sender = msg.get('sender_name', 'unknown')
        content = msg.get('content', '')
        log(f'[msg] 开始处理 id={msg_id} type={msg_type} from={sender} '
            f'content_len={len(content)}')

        if _is_workflow_hub_notification(msg):
            log(f'[msg] workflow notification only id={msg_id}; skip chat LLM')
            _ack_workflow_notification(msg_id)
            return

        # 1. processing
        code, body = http('PUT', f'/api/openclaws/{CLAW_ID}/messages/{msg_id}/processing')
        if code not in (200, 404):
            log(f'[msg] PUT processing 失败 id={msg_id} code={code} body={body}')

        # 2. 拼 prompt
        owner_wecom = get_cfg('owner_wecom_userid', '')
        claw_name = get_cfg('claw_name', '')
        from_claw_id = msg.get('from_claw_id')

        prompt_lines = [
            f"[Hub聊天处理任务]",
            f"你（{claw_name or 'OpenClaw'}）收到了一条来自 {sender} 的 {msg_type} 消息：",
        ]
        cap_lines = capability_digest_lines()
        if cap_lines:
            prompt_lines.append("")
            prompt_lines.extend(cap_lines)
        memo_lines = memo_index_lines()
        if memo_lines:
            prompt_lines.append("")
            prompt_lines.extend(memo_lines)
        profile_lines = agent_profile_lines()
        if profile_lines:
            prompt_lines.append("")
            prompt_lines.extend(profile_lines)
        prompt_lines.extend([
            "",
            f"- 发件人: {sender}",
            f"- 消息类型: {msg_type}",
            f"- 当前消息 id: {msg_id}",
            f"- 内容: {content}",
            "",
            f"必须完成：",
            f"1) 认真阅读消息内容，生成有价值的自然回复（禁止发送协议说明/状态模板）。",
            f"2) 只输出最终回复文本，不要调用任何工具、接口、终端或浏览器。",
            f"3) Hub sidecar 会自动把你的回复写回消息闭环并标记完成。",
        ])

        # claw→claw 消息：额外要求回复发送方
        if from_claw_id:
            prompt_lines.extend([
                f"4) 这是来自另一个 OpenClaw 的消息，sidecar 会自动把同一回复同步发给对方；你仍然只输出回复文本。",
            ])

        # 通知 owner（企微）
        if owner_wecom:
            prompt_lines.extend([
                f"",
                f"无需主动企微通知 owner；Hub 会在通信中心保留处理记录。",
            ])

        prompt_lines.extend([
            "",
            "禁止事项：",
            "- 禁止调用 send-to-claw 给自己发消息",
            "- 禁止发送'重复消息处理/系统状态播报'模板",
            "- 禁止发送协议说明（如'我已收到消息''根据通信协议'）",
        ])

        ok, resp, err = call_llm('\n'.join(prompt_lines))

        # 3. done / failed
        if ok:
            reply = (resp or '').strip() or '已收到。'
            code, body = http(
                'PUT', f'/api/openclaws/{CLAW_ID}/messages/{msg_id}/read',
                body={'reply': reply[:4000]},
            )
            if code not in (200, 404):
                log(f'[msg] read/reply 失败 id={msg_id} code={code} body={body}')
            if from_claw_id:
                code, body = http(
                    'POST', f'/api/openclaws/{CLAW_ID}/send-to-claw',
                    body={
                        'target_claw_ids': [from_claw_id],
                        'content': reply[:4000],
                        'msg_type': 'text',
                    },
                )
                if code not in (200, 201):
                    log(f'[msg] send-to-claw 失败 id={msg_id} target={from_claw_id} code={code} body={body}')
            code, body = http(
                'PUT', f'/api/openclaws/{CLAW_ID}/messages/{msg_id}/done',
                body={},
            )
            log(f'[msg] done id={msg_id} hub_code={code}')
        else:
            code, body = http(
                'PUT', f'/api/openclaws/{CLAW_ID}/messages/{msg_id}/failed',
                body={'failed_reason': err[:1500]},
            )
            log(f'[msg] failed id={msg_id} err={err} hub_code={code}')
    finally:
        with _inflight_lock:
            _inflight_msgs.discard(msg_id)


def _extract_json_object(text):
    raw = (text or '').strip()
    if not raw:
        return {}
    candidates = [raw]
    if '```' in raw:
        parts = raw.split('```')
        for part in parts:
            part = part.strip()
            if part.startswith('json'):
                part = part[4:].strip()
            if part.startswith('{') and part.endswith('}'):
                candidates.append(part)
    start = raw.find('{')
    end = raw.rfind('}')
    if start >= 0 and end > start:
        candidates.append(raw[start:end + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
            if isinstance(value, dict):
                return value
        except Exception:
            continue
    return {}


def _format_node_references(payload):
    """渲染节点 references（Owner 在节点上指定的知识库/报告/用例等参考资料）。"""
    refs = payload.get('references') or []
    if not isinstance(refs, list) or not refs:
        return []
    lines = ['节点参考资料（Owner 指定，务必查阅）：']
    for r in refs:
        if isinstance(r, dict):
            line = (f"  · [{r.get('type', '')}] "
                    f"{r.get('title') or r.get('name') or ''} "
                    f"{r.get('url') or r.get('ref') or ''}").rstrip()
            lines.append(line)
        else:
            lines.append(f'  · {r}')
    return lines


def _workflow_agent_prompt(task, payload):
    claw_name = get_cfg('claw_name', '')
    # task_context：payload 内嵌优先，否则按 agent_task ref 拉
    tc = payload.get('task_context')
    if not isinstance(tc, dict) or not tc:
        tc = fetch_task_context('agent_task', task.get('id'))
    ctx_block = []
    ctx_lines = format_task_context(tc)
    if ctx_lines:
        ctx_block = ['', '=== 任务上下文（Hub 记忆路由注入，执行前必读）==='] + ctx_lines + ['=== 上下文结束 ===']
    profile_lines = agent_profile_lines()
    if profile_lines:
        ctx_block = ctx_block + [''] + profile_lines
    node_refs = _format_node_references(payload)
    if node_refs:
        ctx_block = ctx_block + [''] + node_refs
    return '\n'.join([
        '[Workflow Agent 节点任务]',
        f"你是 {claw_name or 'OpenClaw Agent'}，现在需要以自己的身份、记忆、Skills、Rules 和可用工具执行一个 Workflow 节点。",
        '',
        '任务元信息：',
        f"- AgentTask ID: {task.get('task_id')}",
        f"- Workflow Run: {payload.get('run_id')} / {payload.get('run_name')}",
        f"- Step: {payload.get('step_id')} / {payload.get('step_name')}",
        f"- Runner: {payload.get('runner')}",
        f"- Progress API: {payload.get('progress_api')}",
        f"- Result API: {payload.get('result_api')}",
        *ctx_block,
        '',
        '节点 Prompt：',
        str(payload.get('prompt') or '(无)'),
        '',
        '固定输入 inputs：',
        json.dumps(payload.get('inputs') or {}, ensure_ascii=False, indent=2),
        '',
        '变量输入 input_vars：',
        json.dumps(payload.get('input_vars') or {}, ensure_ascii=False, indent=2),
        '',
        '上游 outputs：',
        json.dumps(payload.get('outputs') or {}, ensure_ascii=False, indent=2),
        '',
        'Run context：',
        json.dumps(payload.get('context') or {}, ensure_ascii=False, indent=2),
        '',
        '执行要求：',
        '1) 可以使用你自己的记忆、Skills、Rules、项目上下文和可用工具完成任务。',
        '2) 不要只回复“收到”；必须给出真实执行结论。',
        '3) 长耗时任务必须阶段性调用 Progress API，上报 phase/message/percent；例如下载包体、安装、执行测试、分析日志都应各上报一次，必要时携带 {"heartbeat": true} 同步刷新心跳。',
        '4) 最终只输出一个 JSON 对象，不要输出额外解释。',
        '5) JSON 字段建议如下：',
        '{"status":"passed|blocked|failed","summary":"一句话结论","metrics":{},"outputs":{},"evidence":{},"logs":{},"blocker":{"type":"","message":"","suggested_action":""}}',
    ])


def _normalize_workflow_result(response_text, ok, err):
    if not ok:
        return {
            'status': 'blocked',
            'summary': 'Workflow agent task failed before producing result',
            'blocker': {
                'type': 'agent_invocation_failed',
                'message': err or 'agent invocation failed',
                'suggested_action': '检查 sidecar 日志、Hermes/OpenClaw CLI 和 Agent 配置',
            },
            'logs': {'agent_error': err or ''},
        }
    parsed = _extract_json_object(response_text)
    if not parsed:
        return {
            'status': 'passed',
            'summary': (response_text or 'Workflow agent task completed')[:500],
            'logs': {'raw_agent_response': (response_text or '')[:4000]},
        }
    status = parsed.get('status') or 'passed'
    if status not in ('passed', 'blocked', 'failed', 'skipped'):
        status = 'passed'
    result = {
        'status': status,
        'summary': parsed.get('summary') or '',
        'metrics': parsed.get('metrics') if isinstance(parsed.get('metrics'), dict) else {},
        'outputs': parsed.get('outputs') if isinstance(parsed.get('outputs'), dict) else {},
        'evidence': parsed.get('evidence') if isinstance(parsed.get('evidence'), dict) else {},
        'logs': parsed.get('logs') if isinstance(parsed.get('logs'), dict) else {},
        'blocker': parsed.get('blocker') if isinstance(parsed.get('blocker'), dict) else {},
    }
    if not result['summary']:
        result['summary'] = 'Workflow agent task completed'
    return result


def _workflow_heartbeat_path(payload):
    run_id = payload.get('run_id')
    step_id = payload.get('step_id')
    if not run_id or not step_id:
        return ''
    return f'/api/v1/workflow-runs/{run_id}/steps/{step_id}/heartbeat'


def _workflow_progress_path(payload):
    path = payload.get('progress_api') or ''
    if path:
        return path
    run_id = payload.get('run_id')
    step_id = payload.get('step_id')
    if not run_id or not step_id:
        return ''
    return f'/api/v1/workflow-runs/{run_id}/steps/{step_id}/progress'


def _post_workflow_heartbeat(payload):
    path = _workflow_heartbeat_path(payload)
    if not path:
        return
    code, body = http('POST', path, {
        'worker_id': f'sidecar:{CLAW_ID}',
    }, timeout=15)
    if code not in (200, 201):
        log(f'[workflow-heartbeat] failed run={payload.get("run_id")} '
            f'step={payload.get("step_id")} code={code} body={body}')


def _post_workflow_progress(payload, phase, message, percent=None, detail=None, heartbeat=False):
    path = _workflow_progress_path(payload)
    if not path:
        return
    body = {
        'worker_id': f'sidecar:{CLAW_ID}',
        'phase': phase,
        'message': message,
        'heartbeat': bool(heartbeat),
    }
    if percent is not None:
        body['percent'] = percent
    if isinstance(detail, dict) and detail:
        body['progress'] = detail
    code, resp = http('POST', path, body, timeout=15)
    if code not in (200, 201):
        log(f'[workflow-progress] failed run={payload.get("run_id")} '
            f'step={payload.get("step_id")} code={code} body={resp}')


def _workflow_heartbeat_loop(payload, stop_event):
    while True:
        try:
            _post_workflow_heartbeat(payload)
        except Exception as exc:
            log(f'[workflow-heartbeat] error run={payload.get("run_id")} '
                f'step={payload.get("step_id")}: {type(exc).__name__}: {exc}')
        if stop_event.wait(max(1, WORKFLOW_HEARTBEAT_SEC)):
            return


def _start_workflow_heartbeat(payload):
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_workflow_heartbeat_loop,
        args=(payload, stop_event),
        daemon=True,
    )
    thread.start()
    return stop_event, thread


def handle_task(task):
    task_type = task.get('task_type')
    task_id = task.get('task_id')
    if task_type != 'workflow_agent_task':
        log(f'[task] 忽略不支持任务 type={task_type} id={task_id}')
        return
    payload = task.get('payload') or {}
    if not isinstance(payload, dict):
        payload = {}
    log(f'[workflow-task] start task_id={task_id} run={payload.get("run_id")} step={payload.get("step_id")}')
    heartbeat_stop = None
    heartbeat_thread = None
    try:
        heartbeat_stop, heartbeat_thread = _start_workflow_heartbeat(payload)
        _post_workflow_progress(
            payload,
            'agent_started',
            'sidecar 已启动 Workflow Agent 节点，正在调用 Agent 执行',
            percent=1,
            heartbeat=True,
        )
        ok, resp, err = call_llm(_workflow_agent_prompt(task, payload), task_kind='workflow')
        result = _normalize_workflow_result(resp, ok, err)
    except Exception as exc:
        result = {
            'status': 'blocked',
            'summary': f'Workflow sidecar exception: {type(exc).__name__}',
            'metrics': {},
            'outputs': {},
            'evidence': {},
            'logs': {'error': str(exc)},
            'blocker': {
                'type': 'workflow_sidecar_exception',
                'message': str(exc),
                'suggested_action': '检查 hub-sse-sidecar 日志并重试 workflow step',
            },
        }
    finally:
        if heartbeat_stop:
            heartbeat_stop.set()
        if heartbeat_thread:
            heartbeat_thread.join(timeout=5)
    try:
        final_status = result.get('status') or 'passed'
        _post_workflow_progress(
            payload,
            'agent_completed' if final_status == 'passed' else 'agent_blocked',
            result.get('summary') or 'Workflow Agent 节点已结束，准备回写结果',
            percent=100 if final_status == 'passed' else None,
            heartbeat=True,
        )
    except Exception as exc:
        log(f'[workflow-progress] final update error run={payload.get("run_id")} '
            f'step={payload.get("step_id")}: {type(exc).__name__}: {exc}')
    result_api = payload.get('result_api')
    if result_api:
        code, body = http('POST', result_api, result, timeout=60)
        if code not in (200, 201):
            err_msg = f'post workflow result failed code={code} body={body}'
            log(f'[workflow-task] {err_msg}')
            http('POST', f'/api/openclaws/{CLAW_ID}/report', {
                'task_id': task_id,
                'status': 'failed',
                'error': err_msg,
            })
            return
    http('POST', f'/api/openclaws/{CLAW_ID}/report', {
        'task_id': task_id,
        'status': 'completed' if result.get('status') in ('passed', 'skipped') else 'failed',
        'result': json.dumps(result, ensure_ascii=False),
        'error': '' if result.get('status') in ('passed', 'skipped') else (
            (result.get('blocker') or {}).get('message') or result.get('summary') or ''
        ),
    })
    log(f'[workflow-task] done task_id={task_id} status={result.get("status")}')


def _todo_key(todo_id):
    return str(todo_id)


def enqueue_todo(todo):
    """把待办放入本地串行队列，避免 Hub 重推时反复触发 LLM。"""
    todo_id = todo.get('id')
    if not todo_id:
        return

    key = _todo_key(todo_id)
    now = time.time()
    with _todo_lock:
        cooldown_until = _todo_cooldown_until.get(key, 0)
        if cooldown_until > now:
            remain = int(cooldown_until - now)
            log(f'[todo] skip id={todo_id} cooling_down={remain}s')
            return
        if key in _queued_todos:
            log(f'[todo] skip id={todo_id} already_queued_or_running')
            return
        _queued_todos.add(key)
        _todo_queue.put(todo)
        log(f'[todo] queued id={todo_id} title={todo.get("title", "")!r}')


def todo_worker_loop():
    """单 worker 串行处理 todo，防止一次 pending 列表触发多个 LLM 进程。"""
    log('[todo] worker started')
    while not _stop_event.is_set():
        try:
            todo = _todo_queue.get(timeout=1)
        except queue.Empty:
            continue

        todo_id = todo.get('id')
        key = _todo_key(todo_id)
        try:
            ok = handle_todo(todo)
            cooldown = TODO_SUCCESS_COOLDOWN_SEC if ok else TODO_RETRY_SEC
            with _todo_lock:
                _todo_cooldown_until[key] = time.time() + cooldown
            if not ok:
                log(f'[todo] id={todo_id} failed; cooldown={cooldown}s')
        finally:
            with _todo_lock:
                _queued_todos.discard(key)
            _todo_queue.task_done()


def _post_complete(todo_id, result_summary=''):
    """sidecar 直接回调 Hub 标记 todo 完成。"""
    url = f"{HUB_URL}/api/v1/openclaws/{CLAW_ID}/todos/{todo_id}/complete"
    data = json.dumps({
        'result_summary': result_summary or '已处理',
    }).encode('utf-8')
    req = urllib.request.Request(url, data=data, method='POST')
    req.add_header('Authorization', f'Bearer {CLAW_TOKEN}')
    req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status == 200
    except Exception as e:
        log(f'[todo] complete 回调失败 id={todo_id} err={e}')
        return False


def post_message_to_hub(content, msg_type='text'):
    """OpenClaw → Hub：主动给 Hub 发消息（如部署汇报、状态通知）。"""
    url = f"{HUB_URL}/api/openclaws/{CLAW_ID}/messages"
    data = json.dumps({
        'content': content,
        'msg_type': msg_type,
    }).encode('utf-8')
    req = urllib.request.Request(url, data=data, method='POST')
    req.add_header('Authorization', f'Bearer {CLAW_TOKEN}')
    req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status == 200 or r.status == 201
    except Exception as e:
        log(f'[hub] 主动发消息失败 err={e}')
        return False


def send_to_claw(target_claw_ids, content, msg_type='text'):
    """OpenClaw → OpenClaw：给其他 claw 发消息。"""
    url = f"{HUB_URL}/api/openclaws/{CLAW_ID}/send-to-claw"
    data = json.dumps({
        'target_claw_ids': target_claw_ids,
        'content': content,
        'msg_type': msg_type,
    }).encode('utf-8')
    req = urllib.request.Request(url, data=data, method='POST')
    req.add_header('Authorization', f'Bearer {CLAW_TOKEN}')
    req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status == 200 or r.status == 201
    except Exception as e:
        log(f'[claw] 给 claw 发消息失败 err={e}')
        return False


def handle_todo(todo):
    """处理一条 ClawTodo；凭据与完成回调始终由 Sidecar 持有。

    Codex 只接收任务上下文并返回最终结果，不会看到 Hub URL、Claw ID
    或 CLAW_TOKEN。只有 Provider 成功返回后，Sidecar 才调用 complete；
    回调失败会保持 todo 未完成并进入正常重试冷却。
    """
    todo_id = todo.get('id')
    title = todo.get('title', '')
    description = todo.get('description', '')
    pitfall_notice = todo.get('pitfall_notice', '')
    primary_skill = todo.get('primary_skill', '')
    urgency = todo.get('urgency', '')
    task_category = todo.get('task_category', '')
    schedule_time = todo.get('schedule_time', '')
    if not todo_id:
        return

    claw_name = get_cfg('claw_name', '')
    prompt_lines = [
        f"[Hub待办任务]",
        f"你（{claw_name or 'OpenClaw'}）有一个待办任务需要处理：",
    ]
    cap_lines = capability_digest_lines()
    if cap_lines:
        prompt_lines.append("")
        prompt_lines.extend(cap_lines)
    memo_lines = memo_index_lines()
    if memo_lines:
        prompt_lines.append("")
        prompt_lines.extend(memo_lines)
    profile_lines = agent_profile_lines()
    if profile_lines:
        prompt_lines.append("")
        prompt_lines.extend(profile_lines)
    prompt_lines.extend([
        "",
        f"- 标题：{title}",
        f"- 任务ID：{todo_id}",
    ])
    if description:
        prompt_lines.append(f"- 详情：{description}")
    # 统一任务上下文包（记忆路由注入）：内嵌优先，否则按 todo ref 拉
    tc = todo.get('task_context')
    if not isinstance(tc, dict) or not tc:
        tc = fetch_task_context('todo', todo_id)
    ctx_lines = format_task_context(tc)
    if ctx_lines:
        prompt_lines.append("")
        prompt_lines.append("=== 任务上下文（Hub 记忆路由注入，执行前必读）===")
        prompt_lines.extend(ctx_lines)
        prompt_lines.append("=== 上下文结束 ===")
    elif pitfall_notice:
        # 回退：无上下文包时用旧字段
        prompt_lines.append(f"- 任务前公共经验：{pitfall_notice}")
    if primary_skill and not ctx_lines:
        prompt_lines.append(f"- 推荐主 Skill：{primary_skill}（先加载 agent-operating-protocol 再执行）")
    if urgency:
        prompt_lines.append(f"- 紧急度：{urgency}")
    if task_category:
        prompt_lines.append(f"- 类别：{task_category}")
    if schedule_time:
        prompt_lines.append(f"- 计划时间：{schedule_time}")

    prompt_lines.extend([
        "",
        "你必须完成：",
        "1) 在当前只读权限范围内处理任务并核对结果。",
        "2) 最终只输出一个 JSON 对象，不要输出代码块或额外解释：",
        '   {"status":"completed|blocked|failed",'
        '"result_summary":"非空结果摘要"}',
        "3) 只有任务确实完成时使用 completed；超出只读权限或无法完成时使用 blocked/failed。",
        "4) 不要调用 Hub API 或企微接口，不要读取、索取、推断或输出任何凭据。",
        "",
        "Sidecar 会在 Provider 成功返回后持久化结果并完成待办闭环。",
        "如果任务超出只读权限，请在最终结果中明确说明阻断原因。",
    ])

    log(f'[todo] 派发给 LLM id={todo_id} title={title!r}')
    ok, resp, err = call_llm(
        '\n'.join(prompt_lines),
        task_kind='todo',
        session_key=f'todo:{todo_id}',
    )
    if not ok:
        log(f'[todo] LLM 处理失败 id={todo_id} err={err}（Hub watcher 会兜底告警）')
        return False
    try:
        result = json.loads(resp)
        status = result.get('status') if isinstance(result, dict) else None
        result_summary = (
            str(result.get('result_summary') or '').strip()[:4000]
            if isinstance(result, dict) else ''
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        status = None
        result_summary = ''
    if status not in ('completed', 'blocked', 'failed') or not result_summary:
        log(f'[todo] LLM 结果协议无效 id={todo_id}，保留为未完成等待重试')
        return False
    if status != 'completed':
        log(f'[todo] LLM 未完成 id={todo_id} status={status}，保留为未完成等待人工处理')
        return False
    if not _post_complete(todo_id, result_summary):
        log(f'[todo] complete 回调未确认 id={todo_id}，保留为未完成等待重试')
        return False
    log(f'[todo] LLM 已处理且 Sidecar 已完成闭环 id={todo_id} '
        f'resp_len={len(resp or "")}')
    return True


# ===================== SSE =====================

def parse_sse_block(block):
    """解析一个 SSE 块（已用 \\n\\n 分隔），返回 (event, data_str)。"""
    event_name = ''
    data_lines = []
    for line in block.split('\n'):
        if line.startswith('event:'):
            event_name = line[len('event:'):].strip()
        elif line.startswith('data:'):
            data_lines.append(line[len('data:'):].lstrip())
    return event_name, '\n'.join(data_lines)


def sse_loop_once():
    """单次 SSE 长连接，断了返回。"""
    sse_url = f"{HUB_URL}/api/openclaws/{CLAW_ID}/events"
    log(f'[sse] 连接 {sse_url}')
    req = urllib.request.Request(sse_url)
    req.add_header('Authorization', f'Bearer {CLAW_TOKEN}')
    req.add_header('Accept', 'text/event-stream')

    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            buf = ''
            for raw in r:
                if _stop_event.is_set():
                    return
                try:
                    buf += raw.decode('utf-8', 'replace')
                except Exception:
                    continue
                while '\n\n' in buf:
                    block, buf = buf.split('\n\n', 1)
                    block = block.strip()
                    if not block:
                        continue
                    event_name, data_str = parse_sse_block(block)
                    if not event_name and not data_str:
                        continue
                    handle_event(event_name, data_str)
    except Exception as e:
        log(f'[sse] 断开: {e}')


def handle_event(event_name, data_str):
    if event_name == 'connected':
        log(f'[sse] connected')
        return
    if event_name == 'ping':
        return
    if event_name == 'heartbeat':
        return
    if event_name == 'error':
        log(f'[sse] server error: {data_str[:300]}')
        return

    if event_name == 'message':
        try:
            msg = json.loads(data_str)
        except Exception as e:
            log(f'[sse] message 解析失败: {e} data={data_str[:200]}')
            return
        # 异步处理，避免堵塞 SSE 接收下一条
        threading.Thread(target=handle_message, args=(msg,), daemon=True).start()
        return

    if event_name == 'task':
        try:
            task = json.loads(data_str)
        except Exception as e:
            log(f'[sse] task 解析失败: {e} data={data_str[:200]}')
            return
        threading.Thread(target=handle_task, args=(task,), daemon=True).start()
        return

    if event_name == 'todos_pending':
        if not TODO_WORKER_ENABLED:
            log('[todo] worker disabled; skip todos_pending')
            return
        try:
            payload = json.loads(data_str)
        except Exception:
            return
        todos = payload.get('todos', [])
        for todo in todos:
            enqueue_todo(todo)
        return

    log(f'[sse] unhandled event={event_name} data={data_str[:200]}')


def sse_loop():
    while not _stop_event.is_set():
        try:
            sse_loop_once()
        except Exception as e:
            log(f'[sse] 循环异常: {e}')
        if _stop_event.is_set():
            return
        log(f'[sse] {SSE_RECONNECT_SEC}s 后重连...')
        _stop_event.wait(SSE_RECONNECT_SEC)


# ===================== 主流程 =====================

def _start_codex_wecom_channel():
    """Start the official WeCom SDK transport without exposing secrets to Codex."""
    global _wecom_channel
    agent_type = (os.getenv('AGENT_TYPE', '').strip()
                  or get_cfg('agent_type', 'openclaw')).lower()
    if agent_type != 'codex' or os.getenv('WECOM_ENABLED', '').lower() != 'true':
        return None
    from wecom_channel import WeComChannel
    scripts_dir = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.dirname(scripts_dir)
    ready_path = os.path.join(data_dir, 'wecom.ready')
    try:
        os.unlink(ready_path)
    except FileNotFoundError:
        pass
    required = {
        'WECOM_NODE_BIN': os.getenv('WECOM_NODE_BIN', '').strip(),
        'WECOM_SDK_ROOT': os.getenv('WECOM_SDK_ROOT', '').strip(),
    }
    missing = [key for key, value in required.items() if not value]
    if missing:
        raise RuntimeError('missing WeCom runtime settings: ' + ','.join(missing))
    _wecom_channel = WeComChannel(
        credentials_path=os.path.join(data_dir, 'wecom-credentials.json'),
        database_path=os.path.join(data_dir, 'wecom-state.db'),
        node_path=required['WECOM_NODE_BIN'],
        bridge_script=os.path.join(scripts_dir, 'wecom-bridge.mjs'),
        sdk_root=required['WECOM_SDK_ROOT'],
        invoke=lambda prompt, key: call_llm(
            prompt, task_kind='wecom', session_key=key),
        logger=log,
    )
    _wecom_channel.start()
    descriptor = os.open(
        ready_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
        json.dump({'version': 1, 'status': 'authenticated'}, handle)
        handle.write('\n')
    return _wecom_channel

def main():
    if not (HUB_URL and CLAW_ID and CLAW_TOKEN):
        sys.stderr.write(
            'FATAL: 必须设置环境变量 HUB_URL, CLAW_ID, CLAW_TOKEN\n'
        )
        sys.exit(2)

    log(f'sidecar v{SIDECAR_VERSION} 启动 claw_id={CLAW_ID}')

    # 等到首次配置成功（最多等 30s）
    deadline = time.time() + 30
    while time.time() < deadline and not _stop_event.is_set():
        cfg = fetch_config()
        if cfg:
            set_cfg(cfg)
            break
        log('[config] 首次拉取失败，2s 后重试...')
        _stop_event.wait(2)
    if not _config:
        sys.stderr.write('FATAL: 30s 内无法拉到 sidecar-config\n')
        sys.exit(2)

    if not get_cfg('enabled', True):
        log('[config] enabled=false，sidecar 直接退出')
        sys.exit(0)

    # 启动成功后向 Hub 汇报（openclaw → hub）
    claw_name = get_cfg('claw_name', '')
    post_message_to_hub(
        f"【{claw_name or 'OpenClaw'}】sidecar v{SIDECAR_VERSION} 已启动，"
        f"SSE 连接就绪，todo worker {'运行中' if TODO_WORKER_ENABLED else '已关闭'}。",
        msg_type='system'
    )

    # 信号处理
    def stop(signum, frame):
        log(f'收到 signal={signum}，准备退出...')
        _stop_event.set()
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    try:
        _start_codex_wecom_channel()
    except Exception as exc:
        log(f'[wecom] startup failed error={type(exc).__name__}')
        sys.exit(3)

    threading.Thread(target=config_refresh_loop, name='cfg', daemon=True).start()
    if TODO_WORKER_ENABLED:
        threading.Thread(target=todo_worker_loop, name='todo-worker', daemon=True).start()
    else:
        log('[todo] worker disabled by TODO_WORKER_ENABLED=false')

    sse_loop()
    if _wecom_channel is not None:
        _wecom_channel.close()
        try:
            os.unlink(os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                'wecom.ready'))
        except FileNotFoundError:
            pass
    log('sidecar 已退出')


if __name__ == '__main__':
    main()
