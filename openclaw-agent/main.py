#!/usr/bin/env python3
"""
OpenClaw Manager 子agent 主入口（**LEGACY / FALLBACK 模式**）

>>> 自 Phase 1 起，**主推路径** 是 ``mcp-servers/openclaw-hub`` MCP server，
>>> 由 AI host (Cursor / Claude Code / CodeBuddy) 直接 spawn，
>>> 进程内维护 SSE 连接并通过 MCP 工具集让 AI 真实回复消息。
>>>
>>> 本 sidecar 仅作为 **MCP 不可用时的兜底**：
>>>   - host 不支持 MCP（极端场景）
>>>   - 仅有 cron / nohup 形式部署的 OpenClaw
>>>
>>> 在兜底模式下，本进程不再发送 "✅ 已转交" 的罐头回复，
>>> 也不再把 todos_pending 反向 POST 回 /messages（消除自循环）；
>>> 改为把所有 SSE 事件写入 ``~/.qclaw/inbox/pending/<urgency>__<source>__<ts>__<id>.json``
>>> 由 AI host 通过 hub-inbox skill 在每回合开始时主动 ``ls`` 这个目录处理。

部署在 OpenClaw 所在服务器上，通过SSE长连接与Manager通信：
1. 接收Manager下发的任务（修改配置、操作文件、执行命令） → 仍走 task_executor 实际执行
2. 接收消息/待办/知识更新 → 写入 inbox/pending（不再罐头回复，不再自循环）

使用方法：
    python main.py

配置来源（优先级从高到低）：
    1. ~/.qclaw/agent.md 中的 YAML front matter（hub_url, claw_id, api_token）
    2. 环境变量（MANAGER_URL, CLAW_ID, API_TOKEN）
    3. 默认值

环境变量（作为备选）：
    MANAGER_URL      - Manager服务地址，默认 http://your-hub-host:8088
    CLAW_ID          - OpenClaw实例ID
    API_TOKEN        - OpenClaw的API Token
    OPENCLAW_DIR     - OpenClaw配置目录，默认 /root/.qclaw
    HEARTBEAT_INTERVAL - 心跳间隔，默认30秒
"""

import json
import os
import secrets
import sys
import time
import logging
import signal
import threading
from datetime import datetime
from pathlib import Path

import requests

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

from config import config
from sse_client import SSClient, TaskReporter
from task_executor import TaskExecutor


# ---- inbox 文件协议（与 mcp-servers/openclaw-hub/src/inbox.ts 保持一致） ----

def _inbox_root() -> Path:
    """``~/.qclaw/inbox`` 根目录，启动时确保 pending/processed/replies 三个子目录存在"""
    root = Path(os.path.expanduser(config.openclaw_dir or '~/.qclaw')) / 'inbox'
    for sub in ('pending', 'processed', 'replies'):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


def _infer_msg_urgency(msg_type: str) -> str:
    mt = (msg_type or 'text').lower()
    if mt in ('request_help', 'task_delegate'):
        return 'interrupt'
    if mt in ('knowledge_share', 'system'):
        return 'background'
    return 'flexible'


def _write_inbox_event(*, source: str, urgency: str, payload: dict, hub_id=None) -> Path:
    """把一个 SSE 事件落盘为 inbox/pending/<urgency>__<source>__<ts>__<rand>.json

    与 MCP server 的 inbox 目录布局兼容；AI host 任意一种 reader（MCP / hub-inbox skill）
    都可以读到同样的文件。
    """
    inbox_id = secrets.token_hex(6)
    received_at = datetime.utcnow().isoformat() + 'Z'
    record = {
        'inboxId': inbox_id,
        'source': source,
        'urgency': urgency,
        'payload': payload,
        'receivedAt': received_at,
        'hubId': hub_id,
    }
    fn = (
        f"{urgency}__{source}__"
        f"{received_at.replace(':', '-').replace('.', '-')}__{inbox_id}.json"
    )
    fp = _inbox_root() / 'pending' / fn
    fp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    return fp


class OpenClawAgent:
    """OpenClaw Manager 子agent主类（legacy 兜底实现）"""

    def __init__(self):
        self.sse_client: SSClient = None
        self.executor = TaskExecutor()
        self._running = False
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {config.api_token}",
            "Content-Type": "application/json",
        })
        # 启动时确保 inbox 目录存在
        _inbox_root()

    def start(self):
        """启动agent"""
        logger.info("=" * 50)
        logger.info("OpenClaw Manager 子agent 启动")
        logger.info(f"Manager URL: {config.manager_url}")
        logger.info(f"Claw ID: {config.claw_id}")
        logger.info(f"API Token: {config.api_token[:20]}..." if config.api_token else "API Token: (未设置)")
        logger.info(f"OpenClaw Dir: {config.openclaw_dir}")
        logger.info("=" * 50)

        self._running = True

        # 设置信号处理
        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

        # 启动SSE客户端
        self.sse_client = SSClient(
            on_task=self._on_task_received,
            on_heartbeat=self._on_heartbeat,
            on_message=self._on_message_received,
            on_knowledge_updated=self._on_knowledge_updated,
            on_todos_pending=self._on_todos_pending,
        )
        self.sse_client.start()

        # 主线程保持运行（在线状态由 SSE 连接管理）
        self._wait_loop()

    def _wait_loop(self):
        """主循环（心跳由 SSE 连接自动管理）"""
        last_status_check = 0

        while self._running:
            try:
                now = time.time()

                # 每60秒检查一次OpenClaw状态
                if now - last_status_check > 60:
                    try:
                        status = self.executor.operator.get_openclaw_status()
                        logger.info(f"OpenClaw状态: {status}")
                    except Exception as e:
                        logger.warning(f"获取OpenClaw状态失败: {e}")
                    last_status_check = now

                time.sleep(10)

            except Exception as e:
                logger.exception(f"主循环异常: {e}")
                time.sleep(5)

    def _on_task_received(self, task: dict):
        """
        任务接收回调

        Args:
            task: 任务对象
        """
        task_id = task.get("task_id", "unknown")
        task_type = task.get("task_type", "unknown")

        logger.info(f"收到任务: {task_id} [{task_type}]")

        # 在独立线程中执行任务，避免阻塞SSE接收
        thread = threading.Thread(
            target=self._execute_task_async,
            args=(task,),
            daemon=True,
        )
        thread.start()

    def _execute_task_async(self, task: dict):
        """异步执行任务"""
        try:
            result = self.executor.execute(task)
            logger.info(f"任务完成: {task.get('task_id')}, 结果: {result['status']}")
        except Exception as e:
            logger.exception(f"任务执行异常: {e}")

    def _on_heartbeat(self):
        """心跳回调"""
        logger.debug("收到Manager心跳")

    def _on_message_received(self, msg: dict):
        """消息接收回调（**legacy fallback**：写入 inbox，不再罐头回复）

        以前这里会自动 PUT /messages/{id}/read 并回写 ``"✅ 已转交"``，
        但那只是个假装"AI 已处理"的占位文本，根本没真过 LLM。

        新做法：把消息原样落到 ``~/.qclaw/inbox/pending/`` 让 AI host
        通过 hub-inbox skill 在下回合开始时主动 ``ls`` + 处理 + 真实回复。
        """
        msg_type = msg.get("msg_type", "unknown")
        sender = msg.get("sender_name", "Hub")
        content = msg.get("content", "")
        msg_id = msg.get("id")
        # urgency 优先取 SSE payload；若 Hub 没带，按 msg_type 推断
        urgency = msg.get("urgency") or _infer_msg_urgency(msg_type)

        logger.info(
            f"收到消息: [{msg_type}/{urgency}] from {sender}: {content[:100]}"
        )

        try:
            fp = _write_inbox_event(
                source='message', urgency=urgency, payload=msg, hub_id=msg_id,
            )
            logger.info(f"消息已写入 inbox: {fp}")
        except Exception as e:
            logger.error(f"写入 inbox 失败: {e}")

    def _on_knowledge_updated(self, data: dict):
        """知识库变更回调 - 写入 inbox（urgency=background）"""
        action = data.get("action", "unknown")
        title = data.get("title", "")
        entry_id = data.get("entry_id", 0)
        logger.info(f"知识库变更: [{action}] #{entry_id} {title}")
        try:
            _write_inbox_event(
                source='knowledge_updated', urgency='background',
                payload=data, hub_id=entry_id,
            )
        except Exception as e:
            logger.error(f"写入 knowledge inbox 失败: {e}")

    def _on_todos_pending(self, data: dict):
        """待办任务推送回调 - 把每条 todo 写入 inbox

        以前这里会拼成提醒文本然后 POST /messages 发回 Hub —— 这会让 SSE 把
        刚发的"提醒"再推下来，形成 **正反馈逗回路**。
        新做法：每条 todo 单独入 inbox，AI 自己读后调 hub_complete_todo /
        相应工具，由 AI 真实输出反馈给 Hub。
        """
        count = data.get("count", 0)
        todos = data.get("todos", [])
        if count == 0:
            logger.debug("无待办任务")
            return

        logger.info(f"📢 收到 {count} 条待办（写入 inbox 而非回写消息）")
        for t in todos:
            try:
                urgency = t.get("urgency") or 'flexible'
                _write_inbox_event(
                    source='todos_pending', urgency=urgency,
                    payload=t, hub_id=t.get('id'),
                )
            except Exception as e:
                logger.error(f"写入 todo inbox 失败 (todo_id={t.get('id')}): {e}")

    def _handle_shutdown(self, signum, frame):
        """处理关闭信号"""
        logger.info("收到关闭信号，正在停止...")
        self.stop()
        sys.exit(0)

    def stop(self):
        """停止agent"""
        self._running = False
        if self.sse_client:
            self.sse_client.stop()
        logger.info("Agent已停止")


def main():
    """主入口"""
    # 检查必要配置
    if not config.api_token:
        logger.error("API_TOKEN 未设置，请在 ~/.qclaw/agent.md 的 front matter 中配置 api_token，或设置环境变量 API_TOKEN")
        sys.exit(1)

    # 创建并启动agent
    agent = OpenClawAgent()

    try:
        agent.start()
    except Exception as e:
        logger.exception(f"Agent启动失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
