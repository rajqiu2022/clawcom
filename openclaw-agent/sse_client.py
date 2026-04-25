"""
OpenClaw Manager 子agent - SSE客户端
负责与Manager建立SSE长连接，接收实时任务
支持离线检测与自动重连
"""
import json
import os
import time
import threading
import logging
from typing import Callable, Optional
import requests

from config import config

logger = logging.getLogger(__name__)

# 离线检测阈值（秒）：超过此时间未收到心跳，判定为离线
OFFLINE_THRESHOLD = 90
# 守护线程检查间隔（秒）
WATCHDOG_INTERVAL = 30
# 心跳时间戳持久化文件
HEARTBEAT_FILE = os.path.join(
    os.getenv("OPENCLAW_DIR", os.path.expanduser("~/.qclaw")),
    "last_heartbeat.txt"
)

# 指数退避重连延迟表（秒）
_RECONNECT_DELAYS = [0, 5, 10, 20, 30, 60]


def _get_reconnect_delay(attempt: int) -> int:
    """根据重连次数返回延迟秒数（指数退避，最大 60s）"""
    if attempt < len(_RECONNECT_DELAYS):
        return _RECONNECT_DELAYS[attempt]
    return 60


class SSClient:
    """SSE客户端，连接Manager接收任务，含离线重连机制"""

    def __init__(self, on_task: Callable, on_heartbeat: Optional[Callable] = None,
                 on_message: Optional[Callable] = None,
                 on_knowledge_updated: Optional[Callable] = None,
                 on_todos_pending: Optional[Callable] = None):
        """
        Args:
            on_task: 任务回调函数，签名为 on_task(task_dict)
            on_heartbeat: 心跳回调函数，签名为 on_heartbeat()
            on_message: 消息回调函数，签名为 on_message(msg_dict)
            on_knowledge_updated: 知识库变更回调，签名为 on_knowledge_updated(changes_dict)
            on_todos_pending: 待办任务回调，签名为 on_todos_pending(todos_dict)
        """
        self.on_task = on_task
        self.on_heartbeat = on_heartbeat
        self.on_message = on_message
        self.on_knowledge_updated = on_knowledge_updated
        self.on_todos_pending = on_todos_pending
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._watchdog_thread: Optional[threading.Thread] = None
        self._response: Optional[requests.Response] = None

        # 离线重连状态
        self._last_event_time: float = 0.0  # 最近一次收到任何事件的时间
        self._reconnect_count: int = 0      # 连续重连次数
        self._status: str = 'offline'       # online / offline / reconnecting

        # 确保持久化目录存在
        os.makedirs(os.path.dirname(HEARTBEAT_FILE), exist_ok=True)

    @property
    def status(self) -> str:
        return self._status

    def start(self):
        """启动SSE连接（在独立线程中运行）"""
        if self._running:
            return
        self._running = True
        self._last_event_time = time.time()

        # SSE 主连接线程
        self._thread = threading.Thread(target=self._run, daemon=True, name="sse-main")
        self._thread.start()

        # 离线检测守护线程
        self._watchdog_thread = threading.Thread(target=self._watchdog_run, daemon=True, name="sse-watchdog")
        self._watchdog_thread.start()

        logger.info("SSE客户端已启动（含离线守护）")

    def stop(self):
        """停止SSE连接"""
        self._running = False
        self._status = 'offline'
        # 强制关闭响应以解除 iter_lines 阻塞
        if self._response:
            try:
                self._response.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=5)
        if self._watchdog_thread:
            self._watchdog_thread.join(timeout=5)
        logger.info("SSE客户端已停止")

    # ==================== 心跳时间戳管理 ====================

    def _record_event_time(self):
        """记录收到事件的时间（内存 + 文件持久化）"""
        self._last_event_time = time.time()
        try:
            with open(HEARTBEAT_FILE, 'w') as f:
                f.write(str(self._last_event_time))
        except Exception:
            pass  # 写文件失败不影响核心逻辑

    def _load_last_heartbeat(self) -> float:
        """从文件加载上次心跳时间（进程重启恢复用）"""
        try:
            with open(HEARTBEAT_FILE, 'r') as f:
                return float(f.read().strip())
        except Exception:
            return 0.0

    def _seconds_since_last_event(self) -> float:
        """距离上次收到事件的秒数"""
        last = self._last_event_time or self._load_last_heartbeat()
        if last <= 0:
            return 0  # 没有记录，不判定离线
        return time.time() - last

    # ==================== 离线检测守护线程 ====================

    def _watchdog_run(self):
        """守护线程：每 30 秒检查心跳是否超时，超时则强制重连"""
        logger.info(f"离线守护已启动，阈值 {OFFLINE_THRESHOLD}s，检查间隔 {WATCHDOG_INTERVAL}s")
        while self._running:
            time.sleep(WATCHDOG_INTERVAL)
            if not self._running:
                break

            elapsed = self._seconds_since_last_event()
            if elapsed > OFFLINE_THRESHOLD and self._status == 'online':
                logger.warning(
                    f"离线检测：已 {elapsed:.0f}s 未收到事件（阈值 {OFFLINE_THRESHOLD}s），"
                    f"强制关闭连接触发重连"
                )
                self._status = 'reconnecting'
                # 关闭当前响应，使 iter_lines 退出
                if self._response:
                    try:
                        self._response.close()
                    except Exception:
                        pass

    # ==================== SSE 连接主循环 ====================

    def _run(self):
        """SSE连接主循环，含指数退避重连"""
        # 尝试从文件恢复上次心跳时间
        saved = self._load_last_heartbeat()
        if saved > 0:
            self._last_event_time = saved

        while self._running:
            try:
                self._connect_sse()
            except Exception as e:
                logger.error(f"SSE连接异常: {e}")

            # 连接断开后
            self._status = 'offline'
            if not self._running:
                break

            delay = _get_reconnect_delay(self._reconnect_count)
            self._reconnect_count += 1
            if delay > 0:
                logger.info(f"第 {self._reconnect_count} 次重连，等待 {delay}s...")
                time.sleep(delay)
            else:
                logger.info("立即重连...")

    def _connect_sse(self):
        """建立SSE连接并处理事件"""
        headers = {
            "Authorization": f"Bearer {config.api_token}",
            "Accept": "text/event-stream",
            "Cache-Control": "no-cache",
        }

        logger.info(f"连接SSE: {config.events_url}")

        self._response = requests.get(
            config.events_url,
            headers=headers,
            stream=True,
            timeout=(10, 90),  # (connect_timeout, read_timeout=90s 匹配离线阈值)
        )

        if self._response.status_code != 200:
            logger.error(f"SSE连接失败: HTTP {self._response.status_code}")
            self._response.raise_for_status()

        logger.info("SSE连接已建立，等待事件...")

        # 解析SSE事件流
        event_type = None
        event_data = []

        for line in self._response.iter_lines(decode_unicode=True):
            if not self._running:
                break

            if line is None:
                # read_timeout 到了也可能返回 None
                continue

            if line.startswith("event:"):
                event_type = line[6:].strip()
            elif line.startswith("data:"):
                event_data.append(line[5:].strip())
            elif line == "":
                if event_type and event_data:
                    self._process_event(event_type, "\n".join(event_data))
                event_type = None
                event_data = []

        self._response.close()
        self._response = None

    def _process_event(self, event_type: str, data: str):
        """处理SSE事件"""
        # 任何事件都刷新心跳时间
        self._record_event_time()

        try:
            if event_type == "connected":
                logger.info(f"已连接: {data}")
                self._status = 'online'
                self._reconnect_count = 0  # 连接成功，重置重连计数
                logger.info(f"重连计数已清零，状态: online")

            elif event_type == "heartbeat":
                logger.debug(f"心跳: {data}")
                if self.on_heartbeat:
                    self.on_heartbeat()

            elif event_type == "task":
                logger.info(f"收到任务: {data[:100]}...")
                task = json.loads(data)
                if self.on_task:
                    self.on_task(task)

            elif event_type == "message":
                logger.info(f"收到消息: {data[:100]}...")
                msg = json.loads(data)
                if self.on_message:
                    self.on_message(msg)

            elif event_type == "knowledge_updated":
                logger.info(f"知识库变更: {data[:200]}")
                changes = json.loads(data)
                if self.on_knowledge_updated:
                    self.on_knowledge_updated(changes)

            elif event_type == "todos_pending":
                logger.info(f"待办任务: {data[:200]}")
                todos = json.loads(data)
                if self.on_todos_pending:
                    self.on_todos_pending(todos)

            elif event_type == "ping":
                pass  # 保活，已通过 _record_event_time 刷新

            elif event_type == "error":
                logger.error(f"Server错误: {data}")

        except json.JSONDecodeError as e:
            logger.error(f"JSON解析失败: {e}, raw data: {data[:200]}")
        except Exception as e:
            logger.error(f"事件处理异常: {e}")


class TaskReporter:
    """任务结果上报器"""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {config.api_token}",
            "Content-Type": "application/json",
        })

    def report(self, task_id: str, status: str, result: str = None, error: str = None):
        """
        上报任务执行结果

        Args:
            task_id: 任务ID
            status: completed/failed
            result: 执行结果
            error: 错误信息
        """
        payload = {
            "task_id": task_id,
            "status": status,
            "result": result,
            "error": error,
        }

        try:
            resp = self.session.post(config.report_url, json=payload, timeout=10)
            if resp.status_code == 200:
                logger.info(f"任务 {task_id} 结果已上报: {status}")
                return True
            else:
                logger.error(f"上报失败: HTTP {resp.status_code}, {resp.text}")
                return False
        except Exception as e:
            logger.error(f"上报异常: {e}")
            return False
