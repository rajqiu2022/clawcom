"""Secure Linux WeCom transport for the Codex Hub Sidecar."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import threading
import time
from typing import Callable
from contextlib import closing


MAX_LINE_BYTES = 256 * 1024
MAX_REPLY_CHARS = 4000
MAX_TRUSTED_CONTEXT_BYTES = 64 * 1024
PROCESSING_LEASE_SECONDS = 65 * 60
_BRIDGE_ENV_KEYS = {
    "LANG", "LC_ALL", "LC_CTYPE", "TZ",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "no_proxy",
    "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE",
}
_CREDENTIAL_KEYS = {
    "schema", "bot_id", "secret", "owner_user_id",
    "allowed_user_ids", "allowed_chat_ids",
}


def _hashed(value: str) -> str:
    return hashlib.sha256(("wecom-log-v1:" + value).encode("utf-8")).hexdigest()[:16]


def load_credentials(path: str) -> dict:
    with open(path, "rb") as handle:
        raw = handle.read(MAX_LINE_BYTES + 1)
    if len(raw) > MAX_LINE_BYTES:
        raise ValueError("credentials too large")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict) or set(value) != _CREDENTIAL_KEYS or value.get("schema") != 2:
        raise ValueError("invalid credentials schema")
    for key in ("bot_id", "secret"):
        if not isinstance(value.get(key), str) or not value[key] or len(value[key]) > 128:
            raise ValueError("invalid credentials")
    for key in ("allowed_user_ids", "allowed_chat_ids"):
        if not isinstance(value.get(key), list) or any(not isinstance(item, str) for item in value[key]):
            raise ValueError("invalid allowlist")
    return value


class _Bridge:
    def __init__(self, node: str, script: str, sdk_root: str, credentials: str,
                 on_event: Callable[[dict], None], logger: Callable[[str], None]):
        self.node = node
        self.script = script
        self.sdk_root = sdk_root
        self.credentials = credentials
        self.on_event = on_event
        self.logger = logger
        self.process = None
        self.lock = threading.Lock()
        self.configured = threading.Event()
        self.activated = threading.Event()

    def _write(self, value: dict) -> None:
        line = json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
        if len(line.encode("utf-8")) > MAX_LINE_BYTES:
            raise ValueError("bridge command too large")
        with self.lock:
            self.process.stdin.write(line)
            self.process.stdin.flush()

    def start(self) -> None:
        # The bridge does not need Hub/Codex credentials. Build a minimal
        # environment instead of inheriting CLAW_TOKEN, CODEX_HOME or any
        # future secret-bearing variable from the Sidecar service.
        environment = {
            key: os.environ[key] for key in _BRIDGE_ENV_KEYS if key in os.environ
        }
        self.process = subprocess.Popen(
            [self.node, self.script], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", env=environment,
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        self._write({"v":1, "type":"configure", "credentials_path":self.credentials,
                     "sdk_root":self.sdk_root})
        if not self.configured.wait(10):
            self.close()
            raise RuntimeError("WeCom bridge configure timeout")
        self._write({"v":1, "type":"activate"})
        if not self.activated.wait(30):
            self.close()
            raise RuntimeError("WeCom bridge authentication timeout")

    def _read_stdout(self) -> None:
        for line in self.process.stdout:
            if len(line.encode("utf-8")) > MAX_LINE_BYTES:
                continue
            try:
                event = json.loads(line)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if event.get("type") == "ack" and event.get("request_id") == "configure":
                self.configured.set()
            elif event.get("type") == "ack" and event.get("request_id") == "activate":
                self.activated.set()
            elif event.get("type") == "inbound":
                threading.Thread(target=self.on_event, args=(event,), daemon=True).start()
            elif event.get("type") == "error":
                self.logger(f"[wecom] bridge error code={event.get('code', 'unknown')}")

    def _read_stderr(self) -> None:
        for line in self.process.stderr:
            self.logger("[wecom] bridge: " + line.strip()[:300])

    def reply(self, event_id: str, text: str, stream: bool = False) -> None:
        self._write({"v":1, "type":"reply", "event_id":event_id,
                     "stream":bool(stream), "text":text})

    def close(self) -> None:
        process = self.process
        if not process:
            return
        try:
            self._write({"v":1, "type":"shutdown"})
            process.wait(timeout=5)
        except Exception:
            process.terminate()
        self.process = None


class WeComChannel:
    """Authorize, deduplicate and deliver WeCom turns around a provider call."""

    def __init__(self, *, credentials_path: str, database_path: str,
                 node_path: str, bridge_script: str, sdk_root: str,
                 invoke: Callable[[str, str], tuple[bool, str, str]],
                 logger: Callable[[str], None],
                 context_lines: Callable[[], list[str]] | None = None,
                 command_handler: Callable[[dict], str | None] | None = None):
        self.credentials_path = credentials_path
        self.database_path = database_path
        self.credentials = load_credentials(credentials_path)
        self.invoke = invoke
        self.context_lines = context_lines or (lambda: [])
        self.command_handler = command_handler or (lambda _event: None)
        self.logger = logger
        self.bridge = _Bridge(node_path, bridge_script, sdk_root,
                              credentials_path, self._on_event, logger)
        self._init_db()

    def _connect(self):
        return sqlite3.connect(self.database_path, timeout=10)

    def _init_db(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.database_path)), exist_ok=True)
        with closing(self._connect()) as db:
            with db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS wecom_events ("
                    "event_id TEXT PRIMARY KEY, status TEXT NOT NULL, "
                    "final_text TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL)"
                )

    def start(self) -> None:
        self.bridge.start()
        self.logger("[wecom] Codex channel authenticated")

    def close(self) -> None:
        self.bridge.close()

    def _authorized(self, event: dict) -> bool:
        conversation = event.get("conversation") or {}
        sender = str(event.get("sender_id") or "")
        if conversation.get("kind") == "user":
            return sender == self.credentials.get("owner_user_id") or sender in self.credentials["allowed_user_ids"]
        return (
            conversation.get("kind") == "group"
            and conversation.get("id") in self.credentials["allowed_chat_ids"]
            and event.get("mentioned") is True
        )

    def _claim(self, event_id: str) -> tuple[bool, str, str]:
        with closing(self._connect()) as db:
            with db:
                cursor = db.execute(
                    "INSERT OR IGNORE INTO wecom_events(event_id,status,created_at) VALUES(?,?,?)",
                    (event_id, "processing", time.time()),
                )
                if cursor.rowcount == 1:
                    return True, "processing", ""
                row = db.execute(
                    "SELECT status, final_text, created_at FROM wecom_events WHERE event_id=?",
                    (event_id,),
                ).fetchone()
                status, final_text, created_at = row
                if (status == "processing" and
                        created_at < time.time() - PROCESSING_LEASE_SECONDS):
                    cursor = db.execute(
                        "UPDATE wecom_events SET created_at=? "
                        "WHERE event_id=? AND status='processing' AND created_at=?",
                        (time.time(), event_id, created_at),
                    )
                    if cursor.rowcount == 1:
                        return True, "processing", ""
                return False, status, final_text

    def _finish(self, event_id: str, status: str, final: str) -> None:
        with closing(self._connect()) as db:
            with db:
                db.execute(
                    "UPDATE wecom_events SET status=?, final_text=? WHERE event_id=?",
                    (status, final, event_id),
                )

    def _safe_reply(self, value: str) -> str:
        text = str(value or "").strip()[:MAX_REPLY_CHARS]
        for secret in (self.credentials.get("secret"), self.credentials.get("bot_id")):
            if secret:
                text = text.replace(secret, "<redacted>")
        return text or "处理失败，请稍后重试。"

    def _on_event(self, event: dict) -> None:
        event_id = str(event.get("event_id") or "")
        if not event_id or len(event_id) > 256:
            return
        if not self._authorized(event):
            self.logger(f"[wecom] rejected event=sha256:{_hashed(event_id)}")
            try:
                self.bridge.reply(event_id, "当前会话未授权。", stream=False)
            except Exception:
                pass
            return
        claimed, status, final = self._claim(event_id)
        if not claimed:
            terminal = status in ("completed", "failed") and bool(final)
            self.bridge.reply(
                event_id,
                final if terminal else "正在处理，请稍候。",
                stream=not terminal,
            )
            return
        try:
            command_reply = self.command_handler(event)
            if command_reply is not None:
                final = self._safe_reply(command_reply)
                self._finish(event_id, "completed", final)
                self.bridge.reply(event_id, final, stream=False)
                return
            self.bridge.reply(event_id, "正在处理…", stream=True)
            conversation = event.get("conversation") or {}
            conversation_ref = _hashed(str(conversation.get("id") or ""))
            trusted_context = "\n".join(
                str(line)[:16000] for line in self.context_lines()
            )
            trusted_bytes = trusted_context.encode("utf-8", errors="replace")
            if len(trusted_bytes) > MAX_TRUSTED_CONTEXT_BYTES:
                trusted_context = trusted_bytes[
                    :MAX_TRUSTED_CONTEXT_BYTES
                ].decode("utf-8", errors="ignore")
            prompt = (
                ("[HUB_CONTEXT]\n" + trusted_context + "\n[/HUB_CONTEXT]\n\n")
                if trusted_context else ""
            ) + (
                "你正在回复一条企业微信消息。只输出给用户看的最终回复，不要输出推理过程、"
                "协议说明或凭据。\n"
                f"会话类型：{conversation.get('kind', 'unknown')}\n"
                f"发送者引用：sha256:{_hashed(str(event.get('sender_id') or ''))}\n"
                f"用户消息：{str(event.get('text') or '')[:16000]}"
            )
            ok, response, _error = self.invoke(prompt, "wecom:" + conversation_ref)
            final = self._safe_reply(response if ok else "处理失败，请稍后重试。")
            self._finish(event_id, "completed" if ok else "failed", final)
            self.bridge.reply(event_id, final, stream=False)
        except Exception as exc:
            final = "处理失败，请稍后重试。"
            self._finish(event_id, "failed", final)
            self.logger(f"[wecom] turn failed event=sha256:{_hashed(event_id)} error={type(exc).__name__}")
            try:
                self.bridge.reply(event_id, final, stream=False)
            except Exception:
                pass
