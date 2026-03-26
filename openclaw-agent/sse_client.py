"""
OpenClaw Manager 子agent - SSE客户端
负责与Manager建立SSE长连接，接收实时任务
"""
import json
import time
import threading
import logging
from typing import Callable, Optional
import requests

from config import config

logger = logging.getLogger(__name__)


class SSClient:
    """SSE客户端，连接Manager接收任务"""

    def __init__(self, on_task: Callable, on_heartbeat: Optional[Callable] = None):
        """
        Args:
            on_task: 任务回调函数，签名为 on_task(task_dict)
            on_heartbeat: 心跳回调函数，签名为 on_heartbeat()
        """
        self.on_task = on_task
        self.on_heartbeat = on_heartbeat
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self):
        """启动SSE连接（在独立线程中运行）"""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("SSE客户端已启动")

    def stop(self):
        """停止SSE连接"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("SSE客户端已停止")

    def _run(self):
        """SSE连接主循环"""
        while self._running:
            try:
                self._connect_sse()
            except Exception as e:
                logger.error(f"SSE连接异常: {e}")
                if self._running:
                    logger.info(f"{config.sse_reconnect_delay}秒后重连...")
                    time.sleep(config.sse_reconnect_delay)

    def _connect_sse(self):
        """建立SSE连接并处理事件"""
        headers = {
            "Authorization": f"Bearer {config.api_token}",
            "Accept": "text/event-stream",
            "Cache-Control": "no-cache",
        }

        logger.info(f"连接SSE: {config.events_url}")

        # 使用 requests 的流式模式
        response = requests.get(
            config.events_url,
            headers=headers,
            stream=True,
            timeout=60,
        )

        if response.status_code != 200:
            logger.error(f"SSE连接失败: HTTP {response.status_code}")
            response.raise_for_status()

        logger.info("SSE连接已建立，等待事件...")

        # 解析SSE事件流
        event_type = None
        event_data = []

        for line in response.iter_lines(decode_unicode=True):
            if not self._running:
                break

            if line.startswith("event:"):
                event_type = line[6:].strip()
            elif line.startswith("data:"):
                event_data.append(line[5:].strip())
            elif line == "":
                # 事件结束，处理数据
                if event_type and event_data:
                    self._process_event(event_type, "\n".join(event_data))
                event_type = None
                event_data = []

        response.close()

    def _process_event(self, event_type: str, data: str):
        """处理SSE事件"""
        try:
            if event_type == "connected":
                logger.info(f"已连接: {data}")
                # 触发连接成功回调（如果有）

            elif event_type == "heartbeat":
                logger.debug(f"心跳: {data}")
                if self.on_heartbeat:
                    self.on_heartbeat()

            elif event_type == "task":
                logger.info(f"收到任务: {data[:100]}...")
                task = json.loads(data)
                if self.on_task:
                    self.on_task(task)

            elif event_type == "ping":
                # 保持连接活跃，无需特殊处理
                pass

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
