#!/usr/bin/env python3
"""
OpenClaw Hub SSE 守护进程 (v1.0)

唯一职责：
  - 与 Hub 建立 SSE 长连接
  - 处理 ping / heartbeat 心跳
  - 收到 message / todos_pending 等业务事件 → 写入本地任务队列 → 唤醒 hub_worker.py
  - 90s 无心跳 → 主动断流重连
  - 断线指数退避自动重连

不做的事：
  - 不调 LLM
  - 不写业务逻辑
  - 不做永久去重（去重交给 Hub v2 read_at 语义）

配置全部从 ~/.openclaw-sidecar/config.env 读取（KEY=VALUE 形式，注释以 # 开头）。

骨架来自 Hermes 小赫 Skill 134 v1.2，做了如下修改：
  * 去掉 SEEN_IDS_FILE / load_seen_ids / save_seen_id
    （依赖 Hub v2 read_at 语义，避免本地永久去重导致的同回合误丢）
  * 配置改成读 ~/.openclaw-sidecar/config.env，不写在脚本里（多 claw 复用同一份代码）
  * 路径前缀改成 ~/.openclaw-sidecar/，与 hermes/qclaw 命名空间隔离
"""

import datetime
import json
import os
import socket
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

# ── 配置加载 ──────────────────────────────────────
# BASE_DIR 不再硬编码：优先从 OPENCLAW_SIDECAR_CONFIG 所在目录推导，
# 这样同一台机器跑多个 sidecar 实例（小天 / 小赫 / 小马…）时
# 日志、队列、worker.lock 全部物理隔离，不会互相覆盖。
CONFIG_FILE = os.environ.get(
    "OPENCLAW_SIDECAR_CONFIG",
    os.path.expanduser("~/.openclaw-sidecar/config.env"),
)
BASE_DIR = os.path.dirname(os.path.abspath(CONFIG_FILE))
BOOTSTRAP_LOG_DIR = os.path.join(BASE_DIR, "logs")
BOOTSTRAP_LOG_FILE = os.path.join(BOOTSTRAP_LOG_DIR, "sse_client.bootstrap.log")


def _bootstrap_log(msg: str) -> None:
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        os.makedirs(BOOTSTRAP_LOG_DIR, exist_ok=True)
        with open(BOOTSTRAP_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    try:
        sys.stderr.write(line + "\n")
    except Exception:
        pass


def _fatal_exit(msg: str, code: int = 2) -> None:
    _bootstrap_log("[fatal] " + msg)
    sys.exit(code)


def load_config(path: str) -> dict:
    cfg = {}
    if not os.path.exists(path):
        _fatal_exit(f"config not found: {path}")
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, _, v = line.partition("=")
                cfg[k.strip()] = v.strip()
    return cfg


CFG = load_config(CONFIG_FILE)
HUB_URL = CFG.get("HUB_URL", "http://clawteam.woa.com:18800").rstrip("/")
CLAW_ID = CFG.get("CLAW_ID")
TOKEN = CFG.get("API_TOKEN")
if not CLAW_ID or not TOKEN:
    _fatal_exit(f"CLAW_ID / API_TOKEN missing in {CONFIG_FILE}")

SSE_URL = f"{HUB_URL}/api/openclaws/{CLAW_ID}/events"
LOG_DIR = os.path.join(BASE_DIR, "logs")
LOG_FILE = os.path.join(LOG_DIR, "sse_client.log")
PID_FILE = os.path.join(LOG_DIR, "sse_client.pid")
QUEUE_FILE = os.path.join(LOG_DIR, "task_queue.jsonl")
WORKER_LOCK = os.path.join(LOG_DIR, "worker.lock")
WORKER_SCRIPT = os.path.join(BASE_DIR, "scripts", "hub_worker.py")

HEARTBEAT_TIMEOUT = 90  # 秒
BACKOFF = [5, 10, 20, 30] + [60] * 9999
if CFG.get("HEARTBEAT_TIMEOUT", "").strip().isdigit():
    HEARTBEAT_TIMEOUT = max(30, int(CFG["HEARTBEAT_TIMEOUT"]))

os.makedirs(LOG_DIR, exist_ok=True)


# ── 日志 ──────────────────────────────────────────

def log(msg: str) -> None:
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ── 任务队列 ──────────────────────────────────────

def enqueue_task(task: dict) -> None:
    task["enqueued_at"] = datetime.datetime.now().isoformat()
    try:
        with open(QUEUE_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(task, ensure_ascii=False) + "\n")
        log(f"📥 任务入队: type={task.get('type')} | {task.get('summary', '')}")
    except Exception as e:
        log(f"❌ 写队列失败: {e} task={json.dumps(task, ensure_ascii=False)[:300]}")


def is_worker_running() -> bool:
    if not os.path.exists(WORKER_LOCK):
        return False
    try:
        with open(WORKER_LOCK) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return True
    except (ValueError, ProcessLookupError, PermissionError):
        try:
            os.remove(WORKER_LOCK)
        except FileNotFoundError:
            pass
        return False


def spawn_worker() -> None:
    if is_worker_running():
        # 不重复唤醒；当前 worker 完事后会再次扫队列
        return
    if not os.path.exists(WORKER_SCRIPT):
        log(f"❌ worker 脚本不存在: {WORKER_SCRIPT}")
        return
    log("🚀 唤醒工作进程...")
    # 把 worker 的 stdout/stderr 重定向到 hub_worker.log；
    # worker 内部的 log() 也写同一个文件，append 模式安全。
    # 这样 worker 在 import / load_config 阶段崩溃时能看到原因，
    # 而不是默默 DEVNULL 掉。
    bootstrap_log = os.path.join(LOG_DIR, "hub_worker.log")
    try:
        log_fd = open(bootstrap_log, "a", buffering=1, encoding="utf-8")
        log_fd.write(
            f"\n[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
            f"=== worker bootstrap (spawned by sse_client PID {os.getpid()}) ===\n"
        )
    except Exception as e:
        log(f"⚠️ 打不开 {bootstrap_log}: {e}，worker 输出会丢")
        log_fd = subprocess.DEVNULL
    # 把 OPENCLAW_SIDECAR_CONFIG 显式传给子进程，
    # 保证多实例场景下 worker 也读对应实例的 config.env / queue / lock。
    child_env = os.environ.copy()
    child_env["OPENCLAW_SIDECAR_CONFIG"] = CONFIG_FILE
    proc = subprocess.Popen(
        # -u: 不缓冲 stdout/stderr，确保崩溃时最后几行能落盘
        ["python3", "-u", WORKER_SCRIPT],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=log_fd,
        stderr=subprocess.STDOUT,
        env=child_env,
    )
    try:
        with open(WORKER_LOCK, "w") as f:
            f.write(str(proc.pid))
    except Exception:
        pass
    log(f"✅ 工作进程已启动 PID={proc.pid}（日志 → {bootstrap_log}）")


# ── 事件分类 ──────────────────────────────────────

def should_trigger_worker(event_type: str, data: dict):
    """返回 (是否入队, task dict)。**不做任何去重**——Hub v2 read_at 已经保证重连只重推未读。"""
    if event_type == "todos_pending" and data.get("count", 0) > 0:
        return True, {
            "type": "todos",
            "count": data.get("count"),
            "todos": data.get("todos", []),
            "summary": f"{data.get('count')} 个待办任务",
        }
    if event_type == "message":
        # 兼容历史/异构客户端字段：只排除明确的 from_claw。
        direction = str(data.get("direction") or "").strip().lower()
        if direction in {"from_claw", "outbound"}:
            return False, {}
        return True, {
            "type": "chat",
            "msg_id": data.get("id"),
            "sender": data.get("sender_name", ""),
            "sender_claw_id": data.get("sender_claw_id"),
            "from_claw_id": data.get("from_claw_id"),
            "msg_type": data.get("msg_type", "text"),
            "content": data.get("content", ""),
            "summary": (
                f"来自 {data.get('sender_name', '?')} 的消息 "
                f"id={data.get('id')} dir={data.get('direction')}"
            ),
        }
    if event_type == "task":
        return True, {
            "type": "task",
            "task_id": data.get("task_id") or data.get("id"),
            "task_type": data.get("task_type"),
            "command": data.get("command"),
            "target_path": data.get("target_path"),
            "raw": data,
            "summary": f"task {data.get('task_type', '?')}",
        }
    return False, {}


def handle_event(event_type: str, data_str: str, last_heartbeat: list) -> None:
    if event_type in ("ping", "heartbeat", "connected"):
        last_heartbeat[0] = time.time()
        if event_type == "connected":
            log(f"✅ 已连接 Hub，CLAW_ID={CLAW_ID}")
        return

    try:
        data = json.loads(data_str) if data_str else {}
    except json.JSONDecodeError:
        log(f"⚠️ JSON 解析失败: {data_str[:200]}")
        return

    log(f"📨 事件[{event_type}]: {json.dumps(data, ensure_ascii=False)[:300]}")

    triggered, task = should_trigger_worker(event_type, data)
    if not triggered:
        return

    enqueue_task(task)
    threading.Thread(target=spawn_worker, daemon=True).start()


# ── SSE 主循环 ────────────────────────────────────

def connect_sse() -> None:
    headers = {"Authorization": f"Bearer {TOKEN}", "Accept": "text/event-stream"}
    req = urllib.request.Request(SSE_URL, headers=headers)
    log(f"🔌 正在连接 SSE: {SSE_URL}")

    resp = urllib.request.urlopen(req, timeout=30)

    last_heartbeat = [time.time()]
    stop_event = threading.Event()

    def watchdog():
        while not stop_event.is_set():
            if time.time() - last_heartbeat[0] > HEARTBEAT_TIMEOUT:
                log(f"⚠️  watchdog: {HEARTBEAT_TIMEOUT}s 无心跳，强制关闭连接")
                stop_event.set()
                try:
                    resp.close()
                except Exception:
                    pass
                break
            time.sleep(10)

    wdog = threading.Thread(target=watchdog, daemon=True)
    wdog.start()

    try:
        event_type, data_lines = "message", []
        while not stop_event.is_set():
            raw_line = resp.readline()
            # 关键修复：EOF 不能当空行继续，否则会形成“进程存活但连接已死”的假在线死循环
            if raw_line == b"":
                log("⚠️ SSE EOF（连接已关闭），触发重连")
                break
            line = raw_line.decode("utf-8", "replace").strip()
            if not line:
                # 空行：一条 SSE 消息结束
                if data_lines or event_type != "message":
                    handle_event(event_type, "\n".join(data_lines), last_heartbeat)
                event_type, data_lines = "message", []
                continue

            if line.startswith("event:"):
                event_type = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
            elif line.startswith(":"):
                # SSE 注释（很多服务器用作 keepalive）
                last_heartbeat[0] = time.time()

    except (urllib.error.URLError, ConnectionResetError, socket.timeout, TimeoutError) as e:
        log(f"⚠️ 连接已关闭或读取异常: {e}")
    except Exception as e:
        log(f"⚠️ SSE 读取异常（未分类）: {e}")
    finally:
        stop_event.set()
        try:
            resp.close()
        except Exception:
            pass


def run() -> None:
    _bootstrap_log(
        f"bootstrap ok: cfg={CONFIG_FILE} hub={HUB_URL} claw={CLAW_ID} pid={os.getpid()}"
    )
    log(f"🚀 SSE 守护进程启动 (PID: {os.getpid()})")
    log(f"   config: {CONFIG_FILE}")
    log(f"   hub:    {HUB_URL}  claw:{CLAW_ID}  token:{TOKEN[:6]}...")
    log(f"   heartbeat-timeout: {HEARTBEAT_TIMEOUT}s")
    try:
        with open(PID_FILE, "w") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass

    retry = 0
    while True:
        try:
            connect_sse()
            retry = 0
            log("🔌 SSE 连接已断开，准备重连...")
            time.sleep(BACKOFF[0])
        except Exception as e:
            delay = BACKOFF[min(retry, len(BACKOFF) - 1)]
            log(f"❌ 连接异常: {e}，{delay}s 后重连（第 {retry + 1} 次）")
            retry += 1
            time.sleep(delay)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    run()
