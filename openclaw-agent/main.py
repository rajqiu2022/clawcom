#!/usr/bin/env python3
"""
OpenClaw Manager 子agent 主入口

部署在 OpenClaw 所在服务器上，通过SSE长连接与Manager通信：
1. 接收Manager下发的任务（修改配置、操作文件、执行命令）
2. 执行任务并上报结果

使用方法：
    python main.py

环境变量：
    MANAGER_URL      - Manager服务地址，默认 http://9.134.11.169:8088
    CLAW_ID          - OpenClaw实例ID，默认 4
    API_TOKEN        - OpenClaw的API Token
    OPENCLAW_DIR     - OpenClaw配置目录，默认 /root/.qclaw
    HEARTBEAT_INTERVAL - 心跳间隔，默认30秒
"""

import os
import sys
import time
import logging
import signal
import threading

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


class OpenClawAgent:
    """OpenClaw Manager 子agent主类"""

    def __init__(self):
        self.sse_client: SSClient = None
        self.executor = TaskExecutor()
        self._running = False

    def start(self):
        """启动agent"""
        logger.info("=" * 50)
        logger.info("OpenClaw Manager 子agent 启动")
        logger.info(f"Manager URL: {config.manager_url}")
        logger.info(f"Claw ID: {config.claw_id}")
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
        )
        self.sse_client.start()

        # 主线程保持运行
        self._wait_loop()

    def _wait_loop(self):
        """主循环"""
        last_status_check = 0

        while self._running:
            try:
                # 每60秒检查一次OpenClaw状态
                now = time.time()
                if now - last_status_check > 60:
                    status = self.executor.operator.get_openclaw_status()
                    logger.info(f"OpenClaw状态: {status}")
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
        logger.error("API_TOKEN 未设置，请设置环境变量 API_TOKEN")
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
