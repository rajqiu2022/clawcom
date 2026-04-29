#!/usr/bin/env python3
"""
OpenClaw Hub 工作进程 (v1.0)

由 sse_client.py 通过 worker.lock 唤醒，从 task_queue.jsonl 读取一批任务并执行。
执行方式：把每条任务构造成 prompt，调用本机的 OpenClaw Agent CLI：

    openclaw agent --message "<prompt>" --agent <name> [--channel <name>] --timeout <sec>

Agent 在新的 session 里：
  - 用自己的 LLM + skills + 历史对话处理消息
  - **必须** 调 PUT /api/openclaws/<claw_id>/messages/<msg_id>/read 完成回复 + 标记已读
  - 可选用自己已有的通道（--channel wecom 等）发企微通知

兜底：如果 Agent 在 AGENT_TIMEOUT 内没自己 PUT /read，worker 会用空 reply 调
PUT /read 把消息标记为"已派发未回复"，避免下次 SSE 重连无限重推。

骨架来自 Hermes 小赫 Skill 134 v1.2，做了如下修改：
  * 不再调 hermes_cli.py reply-hub / send-message（依赖 Hermes 专属 CLI）
  * 改成统一调 openclaw agent --message，让 Agent 自己决定回什么
  * 不再硬编码"含'收到请回复1'就回..." 业务，业务交给 Agent 自治
  * 新增 PUT /read 兜底，避免消息无限重推
"""

import datetime
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

# ── 配置加载 ──────────────────────────────────────
# BASE_DIR 不再硬编码，与 sse_client 一样从 CONFIG_FILE 所在目录推导，
# 保证 worker 与 sse_client 在同一目录下读写同一份 task_queue.jsonl。
CONFIG_FILE = os.environ.get(
    "OPENCLAW_SIDECAR_CONFIG",
    os.path.expanduser("~/.openclaw-sidecar/config.env"),
)


def load_config(path: str) -> dict:
    cfg = {}
    if not os.path.exists(path):
        sys.stderr.write(f"[fatal] config not found: {path}\n")
        sys.exit(2)
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
HUB_URL = CFG.get("HUB_URL", "http://9.134.11.169:8088").rstrip("/")
CLAW_ID = CFG.get("CLAW_ID")
TOKEN = CFG.get("API_TOKEN")
OPENCLAW_BIN = CFG.get("OPENCLAW_BIN", "openclaw")
AGENT_NAME = CFG.get("AGENT_NAME", "main")
WECOM_CHANNEL = CFG.get("WECOM_CHANNEL", "").strip()
AGENT_TIMEOUT = int(CFG.get("AGENT_TIMEOUT", "120"))
TODOS_VERIFY_DELAY = int(CFG.get("TODOS_VERIFY_DELAY", "3"))
TODOS_FORCE_COMPLETE_FALLBACK = CFG.get("TODOS_FORCE_COMPLETE_FALLBACK", "1").strip() not in ("0", "false", "False", "no", "NO")

BASE_DIR = os.path.dirname(os.path.abspath(CONFIG_FILE))
LOG_DIR = os.path.join(BASE_DIR, "logs")
LOG_FILE = os.path.join(LOG_DIR, "hub_worker.log")
QUEUE_FILE = os.path.join(LOG_DIR, "task_queue.jsonl")
WORKER_LOCK = os.path.join(LOG_DIR, "worker.lock")

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


# ── Hub 兜底调用 ──────────────────────────────────

def hub_mark_read(msg_id: int, reply: str = "") -> bool:
    """worker 兜底 PUT /read。Agent 已经处理过的话会在它那一侧成功，
    这里再调一次也是幂等的。Agent 没处理（超时/崩溃）的话，这里能保证消息
    不会被无限重推。"""
    if not msg_id:
        return False
    url = f"{HUB_URL}/api/openclaws/{CLAW_ID}/messages/{msg_id}/read"
    body = {}
    if reply:
        body["reply"] = reply
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="PUT",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            log(f"   ↳ PUT /read msg_id={msg_id} reply={'有' if reply else '无'} → http {resp.status}")
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        body = e.read()
        if isinstance(body, bytes):
            body = body.decode("utf-8", "replace")
        log(f"   ↳ PUT /read msg_id={msg_id} 失败: HTTP {e.code} {str(body)[:200]}")
        return False
    except Exception as e:
        log(f"   ↳ PUT /read msg_id={msg_id} 异常: {e}")
        return False


def list_pending_todo_ids(limit: int = 200) -> set[int]:
    """读取当前 claw 的 pending todo id 集合。"""
    url = f"{HUB_URL}/api/v1/openclaws/{CLAW_ID}/todos?status=pending&limit={limit}"
    req = urllib.request.Request(
        url,
        method="GET",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
            todos = payload.get("todos") if isinstance(payload, dict) else payload
            if not isinstance(todos, list):
                return set()
            ids: set[int] = set()
            for t in todos:
                tid = t.get("id") if isinstance(t, dict) else None
                if isinstance(tid, int):
                    ids.add(tid)
            return ids
    except Exception as e:
        log(f"   ↳ 拉 pending todos 失败: {e}")
        return set()


def hub_complete_todo(todo_id: int, summary: str) -> bool:
    url = f"{HUB_URL}/api/v1/openclaws/{CLAW_ID}/todos/{todo_id}/complete"
    body = json.dumps({"result_summary": summary}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            log(f"   ↳ POST /todos/{todo_id}/complete -> http {resp.status}")
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        msg = e.read()
        if isinstance(msg, bytes):
            msg = msg.decode("utf-8", "replace")
        log(f"   ↳ complete todo_id={todo_id} 失败: HTTP {e.code} {str(msg)[:200]}")
        return False
    except Exception as e:
        log(f"   ↳ complete todo_id={todo_id} 异常: {e}")
        return False


def list_unread_message_ids(limit: int = 200) -> set[int]:
    """读取当前 claw 的未读消息（pending + delivered）。"""
    url = (
        f"{HUB_URL}/api/openclaws/{CLAW_ID}/messages"
        f"?unread=true&direction=to_claw&limit={limit}"
    )
    req = urllib.request.Request(
        url,
        method="GET",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
            msgs = payload.get("messages", [])
            ids: set[int] = set()
            for m in msgs:
                mid = m.get("id")
                if isinstance(mid, int):
                    ids.add(mid)
            return ids
    except Exception as e:
        log(f"   ↳ 拉 unread 列表失败: {e}")
        return set()


def is_message_unread(msg_id: int) -> bool:
    return msg_id in list_unread_message_ids()


def extract_reply_candidate(agent_stdout: str, original_content: str) -> str:
    """从 agent 输出中提取一个可用兜底回复，避免空回复。"""
    text = (agent_stdout or "").strip()
    if text:
        m = re.search(r"HUB_REPLY::(.+)", text, flags=re.DOTALL)
        if m:
            v = m.group(1).strip()
            if v:
                return v[:400]
        # 没 marker 时，尽量取最后一段可读文本
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        for ln in reversed(lines):
            if ln.startswith("[") and ln.endswith("]"):
                continue
            if ln.lower().startswith("warning"):
                continue
            if len(ln) >= 4:
                return ln[:400]
    compact = (original_content or "").replace("\n", " ").strip()
    if len(compact) > 80:
        compact = compact[:80] + "..."
    return f"已收到：{compact or '你的消息'}，我正在处理中。"


# ── prompt 构造 ───────────────────────────────────

def build_chat_prompt(task: dict) -> str:
    msg_id = task.get("msg_id")
    sender = task.get("sender", "未知发件人")
    content = task.get("content", "")
    return (
        f"[Hub聊天处理任务]\n"
        f"你只处理当前这条消息，不要处理历史 backlog。\n"
        f"\n"
        f"- 发件人: {sender}\n"
        f"- 当前消息 id: {msg_id}\n"
        f"- 内容: {content}\n"
        f"\n"
        f"必须完成：\n"
        f"1) 生成自然回复，禁止发送协议说明/状态模板（如“我已收到消息”“根据通信协议”）。\n"
        f"2) 把回复写回当前消息 id={msg_id} 的闭环接口：\n"
        f"   PUT {HUB_URL}/api/openclaws/{CLAW_ID}/messages/{msg_id}/read\n"
        f"   Body: {{\"reply\": \"<你的回复>\"}}\n"
        f"3) 可选：通过企微发一句提示“已在Hub回复”。\n"
        f"4) 禁止调用 send-to-claw 给自己发消息，禁止发送“重复消息处理/系统状态播报”模板。\n"
        f"\n"
        f"为防兜底丢失，请在 stdout 最后一行额外输出：\n"
        f"HUB_REPLY::<与你写回Hub的同一条回复文本>\n"
    )


def build_todos_prompt(task: dict) -> str:
    todos = task.get("todos", [])
    count = task.get("count", len(todos))
    todo_lines = []
    for t in todos[:20]:  # 防止 prompt 过长
        todo_lines.append(
            f"  - id={t.get('id')} title={t.get('title')!r} "
            f"schedule={t.get('schedule_type', '-')}@{t.get('schedule_time', '-')}"
        )
    return (
        f"[Hub 推送的待办任务 — 你需要处理]\n"
        f"\n"
        f"- 待办数量: {count}\n"
        f"- 列表:\n" + "\n".join(todo_lines) + "\n"
        f"\n"
        f"请按你自己的节奏处理这些待办，每完成一条调:\n"
        f"  POST {HUB_URL}/api/openclaws/{CLAW_ID}/todos/<todo_id>/complete\n"
        f"  Authorization: Bearer <你的 token>\n"
        f"  Body: {{\"result_summary\": \"<完成结果简述>\"}}\n"
        f"\n"
        f"如果某条待办需要更长时间，可以先回复 \"已开始处理\"，做完后再调 complete。\n"
        f"待办有实质进展时，请通过你已有的企微通道给用户一条简短进度提醒。\n"
        f"\n"
        f"输出要求（用于自动校验）：请在最后一行输出\n"
        f"TODO_DONE_IDS::<用逗号分隔的已提交todo_id列表，例如 101,102；若无则留空>\n"
    )


def build_task_prompt(task: dict) -> str:
    return (
        f"[Hub 推送的执行型任务]\n"
        f"\n"
        f"- task_type: {task.get('task_type')}\n"
        f"- command: {task.get('command')}\n"
        f"- target_path: {task.get('target_path')}\n"
        f"- 完整 payload: {json.dumps(task.get('raw'), ensure_ascii=False, indent=2)[:1000]}\n"
        f"\n"
        f"请按你的判断执行；执行完用 hub_send_message / 调 Hub API 回报结果。\n"
    )


# ── 调 openclaw agent ─────────────────────────────

def call_openclaw_agent(prompt: str) -> tuple[bool, str]:
    cmd = [OPENCLAW_BIN, "agent", "--message", prompt, "--agent", AGENT_NAME]
    if WECOM_CHANNEL:
        cmd += ["--channel", WECOM_CHANNEL]
    cmd += ["--timeout", str(AGENT_TIMEOUT)]

    log(f"⚙️  调用: {OPENCLAW_BIN} agent --agent {AGENT_NAME} "
        f"--channel {WECOM_CHANNEL or '(none)'} --timeout {AGENT_TIMEOUT}")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=AGENT_TIMEOUT + 30,
        )
        out = (result.stdout or "")[-800:]
        err = (result.stderr or "")[-400:]
        if result.returncode == 0:
            log(f"   ↳ 成功 (rc=0)，stdout 末段: {out[:300]}")
            return True, out
        log(f"   ↳ 失败 rc={result.returncode}\n     stdout: {out[:300]}\n     stderr: {err[:300]}")
        return False, err or out
    except FileNotFoundError:
        log(f"   ↳ 找不到 {OPENCLAW_BIN}，请确认它在 PATH 里，或在 config.env 改 OPENCLAW_BIN")
        return False, "openclaw not found"
    except subprocess.TimeoutExpired:
        log(f"   ↳ 超时（>{AGENT_TIMEOUT + 30}s），杀掉子进程")
        return False, "timeout"
    except Exception as e:
        log(f"   ↳ 异常: {e}")
        return False, str(e)


# ── 任务处理 ──────────────────────────────────────

def process_task(task: dict) -> None:
    t = task.get("type")
    log(f"⚙️  处理任务: {task.get('summary')}")

    if t == "chat":
        prompt = build_chat_prompt(task)
        ok, output = call_openclaw_agent(prompt)
        # 兜底策略：
        # 1) 先给 Agent 1.5 秒完成它自己的 /read 闭环
        # 2) 仅当该 msg_id 仍在 unread 列表时，worker 才代为 /read
        #    并尽量带上可读回复文本，避免空回复和错绑到旧消息。
        msg_id = task.get("msg_id")
        if msg_id:
            mid = int(msg_id)
            time.sleep(1.5)
            unread = is_message_unread(mid)
            if not unread:
                log(f"   ↳ msg_id={mid} 已被 Agent 闭环，无需 worker 兜底")
            elif not ok:
                hub_mark_read(mid, reply="已收到消息，但自动处理失败，我会稍后重试。")
                log("   ↳ Agent 调用失败，worker 已兜底 /read 并回写失败提示")
            else:
                fallback_reply = extract_reply_candidate(output, task.get("content", ""))
                hub_mark_read(mid, reply=fallback_reply)
                log(f"   ↳ Agent 未闭环，worker 兜底 /read + reply: {fallback_reply[:80]}")
        return

    if t == "todos":
        todo_ids = []
        for one in task.get("todos", []) or []:
            tid = one.get("id") if isinstance(one, dict) else None
            if isinstance(tid, int):
                todo_ids.append(tid)
        todo_set = set(todo_ids)
        if not todo_set:
            log("   ↳ todos 任务没有有效 todo_id，跳过")
            return

        prompt = build_todos_prompt(task)
        ok, _ = call_openclaw_agent(prompt)
        time.sleep(max(1, TODOS_VERIFY_DELAY))
        pending_ids = list_pending_todo_ids()
        left = sorted(todo_set.intersection(pending_ids))
        done = sorted(todo_set.difference(pending_ids))
        log(
            f"   ↳ todos 闭环检查: total={len(todo_set)} done={len(done)} left={len(left)}"
            f" left_ids={left[:8]}"
        )
        if not left:
            return

        # 再催一次（只针对还没 complete 的 todo）
        todo_lines = "\n".join([f"- id={x}" for x in left[:20]])
        reprompt = (
            "[待办闭环补偿任务]\n"
            "你上一轮尚未把以下 todo 提交 complete，请立即补齐：\n"
            f"{todo_lines}\n\n"
            f"接口: POST {HUB_URL}/api/v1/openclaws/{CLAW_ID}/todos/<todo_id>/complete\n"
            "Body: {\"result_summary\":\"...\"}\n"
            "最后一行输出 TODO_DONE_IDS::<id列表>\n"
        )
        ok2, _ = call_openclaw_agent(reprompt)
        time.sleep(max(1, TODOS_VERIFY_DELAY))
        pending_ids2 = list_pending_todo_ids()
        left2 = sorted(set(left).intersection(pending_ids2))
        if not left2:
            log("   ↳ 二次催办后 todos 已全部闭环")
            return

        log(f"   ↳ 二次催办后仍未闭环: {left2[:8]}")
        if TODOS_FORCE_COMPLETE_FALLBACK:
            for tid in left2:
                summary = (
                    "系统兜底提交：Agent 调用已执行，但未按时回写 complete，"
                    "为避免任务长期 pending 先行闭环，请人工复核结果。"
                )
                hub_complete_todo(int(tid), summary)
            log(f"   ↳ 已执行 todos 兜底 complete: {left2[:8]}")
        else:
            log("   ↳ TODOS_FORCE_COMPLETE_FALLBACK=0，保留 pending 以待人工处理")
        return

    if t == "task":
        prompt = build_task_prompt(task)
        call_openclaw_agent(prompt)
        return

    log(f"⚠️  未识别任务类型: {t}")


def main() -> None:
    log("🚀 Hub 工作进程启动")
    log(f"   config: {CONFIG_FILE}")
    log(f"   openclaw: bin={OPENCLAW_BIN} agent={AGENT_NAME} channel={WECOM_CHANNEL or '(none)'} timeout={AGENT_TIMEOUT}")

    if not os.path.exists(QUEUE_FILE):
        log("🤷 任务队列文件不存在，退出")
        return

    # 处理多轮：处理完一批，再扫一次队列（避免处理期间新入队的被漏）
    rounds = 0
    while rounds < 5:  # 最多 5 轮，防止极端情况死循环
        with open(QUEUE_FILE, "r+", encoding="utf-8") as f:
            lines = f.readlines()
            f.truncate(0)

        tasks_to_process = []
        for line in lines:
            try:
                tasks_to_process.append(json.loads(line))
            except json.JSONDecodeError:
                log(f"⚠️ 无效的 JSON 任务: {line[:200]}")

        if not tasks_to_process:
            if rounds == 0:
                log("😴 任务队列为空，退出")
            break

        log(f"📨 第 {rounds + 1} 轮：发现 {len(tasks_to_process)} 个待处理任务")
        for task in tasks_to_process:
            try:
                process_task(task)
            except Exception as e:
                log(f"❌ 任务处理异常: {e}  task={json.dumps(task, ensure_ascii=False)[:200]}")
            time.sleep(1)
        rounds += 1

    log("🏁 所有任务处理完毕，工作进程退出")


if __name__ == "__main__":
    try:
        main()
    finally:
        if os.path.exists(WORKER_LOCK):
            try:
                os.remove(WORKER_LOCK)
            except Exception:
                pass
