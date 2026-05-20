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
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

SIDECAR_VERSION = '2.0.1'

HUB_URL = os.getenv('HUB_URL', '').rstrip('/')
CLAW_ID = os.getenv('CLAW_ID', '').strip()
CLAW_TOKEN = os.getenv('CLAW_TOKEN', '').strip()

SSE_RECONNECT_SEC = int(os.getenv('SSE_RECONNECT_SEC', '5'))
TODO_LOOP_SEC = int(os.getenv('TODO_LOOP_SEC', '120'))
CONFIG_REFRESH_SEC = int(os.getenv('CONFIG_REFRESH_SEC', '60'))
TODO_RETRY_SEC = int(os.getenv('TODO_RETRY_SEC', '3600'))
TODO_SUCCESS_COOLDOWN_SEC = int(os.getenv('TODO_SUCCESS_COOLDOWN_SEC', '300'))

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


# ===================== 配置 =====================

def fetch_config():
    """从 Hub 拉 sidecar 配置 + 心跳。"""
    query = {'sidecar_version': SIDECAR_VERSION}
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


def config_refresh_loop():
    while not _stop_event.is_set():
        try:
            new_cfg = fetch_config()
            if new_cfg:
                set_cfg(new_cfg)
        except Exception as e:
            log(f'[config] 异常: {e}')
        _stop_event.wait(CONFIG_REFRESH_SEC)


# ===================== 调用 LLM =====================

def call_llm(prompt):
    """根据当前 agent_type 调用对应 CLI，返回 (ok, response_text, err)。"""
    agent_type = get_cfg('agent_type', 'openclaw')
    bin_path = get_cfg('openclaw_bin') or 'openclaw'
    agent_name = get_cfg('agent_name', 'main')
    timeout = int(get_cfg('agent_timeout', 300))
    wecom_enabled = bool(get_cfg('wecom_enabled', False))
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

    if agent_type == 'openclaw':
        cmd = [bin_path, 'agent', '--message', prompt,
               '--agent', agent_name, '--timeout', str(timeout)]
        if wecom_enabled:
            cmd += ['--channel', 'wecom']
    elif agent_type == 'hermes':
        # hermes 入口由 install.sh 生成 wrapper 脚本，统一为 --message 入参
        cmd = [bin_path, '--message', prompt, '--timeout', str(timeout)]
    else:
        return False, '', f'unsupported agent_type={agent_type!r}'

    log(f'[llm] 调用 {agent_type} timeout={timeout}s prompt_len={len(prompt)}')
    try:
        proc = subprocess.run(
            cmd, capture_output=True,
            timeout=timeout + 30, text=True,
            encoding='utf-8', errors='replace',
            env=env,
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
        return False, '', f'binary not found: {bin_path}'
    except Exception as e:
        return False, '', f'{type(e).__name__}: {e}'


# ===================== 消息处理 =====================

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
            "",
            f"- 发件人: {sender}",
            f"- 消息类型: {msg_type}",
            f"- 当前消息 id: {msg_id}",
            f"- 内容: {content}",
            "",
            f"必须完成：",
            f"1) 认真阅读消息内容，生成有价值的自然回复（禁止发送协议说明/状态模板）。",
            f"2) 把回复写回当前消息的闭环接口：",
            f"   PUT {HUB_URL}/api/openclaws/{CLAW_ID}/messages/{msg_id}/read",
            f"   Headers: Authorization: Bearer {CLAW_TOKEN}",
            f"   Body: {{\"reply\": \"<你的回复>\"}}",
        ]

        # claw→claw 消息：额外要求回复发送方
        if from_claw_id:
            prompt_lines.extend([
                f"3) 使用 send-to-claw 工具回复发送方（target_claw_ids=[{from_claw_id}]），"
                f"   把你的回复也发送给对方，让对方能在聊天记录中看到你的回复。",
            ])

        # 通知 owner（企微）
        if owner_wecom:
            prompt_lines.extend([
                f"",
                f"另外，请通过企微通知我的 owner 有新消息到达：",
                f"  调用 send_message(action='send', target='wecom', "
                f"message='收到来自{sender}的消息，已回复。') "
                f"发企微通知；wecom home channel 已配置为 {owner_wecom}。",
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
            code, body = http(
                'PUT', f'/api/openclaws/{CLAW_ID}/messages/{msg_id}/done',
                body={'llm_response': resp[:4000]},
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
        'notified': True,
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
    """处理一条 ClawTodo：LLM 跑业务逻辑，sidecar 负责 complete 回调。"""
    todo_id = todo.get('id')
    title = todo.get('title', '')
    if not todo_id:
        return

    owner_wecom = get_cfg('owner_wecom_userid', '')
    claw_name = get_cfg('claw_name', '')
    prompt_lines = [
        f"你（{claw_name or 'OpenClaw'}）有一个待办任务：",
        f"标题：{title}",
        f"任务ID：{todo_id}",
        "",
        "请按 SOUL 流程完成。",
    ]
    if owner_wecom:
        prompt_lines.append(
            f"完成后请调用 send_message(action='send', target='wecom', "
            f"message='...') 发企微通知 owner；wecom home channel 已配置为 {owner_wecom}。"
        )
    log(f'[todo] 派发给 LLM id={todo_id} title={title!r}')
    ok, resp, err = call_llm('\n'.join(prompt_lines))
    if not ok:
        log(f'[todo] LLM 处理失败 id={todo_id} err={err}（不再 force_complete，'
            f'Hub watcher 会兜底告警）')
        return False
    log(f'[todo] LLM 处理完成 id={todo_id}（sidecar 自动回调 complete）')
    _post_complete(todo_id, result_summary=(resp or '')[:500])
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

    if event_name == 'todos_pending':
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
        f"SSE 连接就绪，todo worker 运行中。",
        msg_type='system'
    )

    # 信号处理
    def stop(signum, frame):
        log(f'收到 signal={signum}，准备退出...')
        _stop_event.set()
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    threading.Thread(target=config_refresh_loop, name='cfg', daemon=True).start()
    threading.Thread(target=todo_worker_loop, name='todo-worker', daemon=True).start()

    sse_loop()
    log('sidecar 已退出')


if __name__ == '__main__':
    main()
