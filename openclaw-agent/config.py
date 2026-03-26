"""
OpenClaw Manager 子agent 配置模块
"""
import os
from dataclasses import dataclass


@dataclass
class AgentConfig:
    """子agent配置"""
    manager_url: str = os.getenv("MANAGER_URL", "http://your-hub-host:8088")
    claw_id: int = int(os.getenv("CLAW_ID", "4"))
    api_token: str = os.getenv("API_TOKEN", "")
    openclaw_dir: str = os.getenv("OPENCLAW_DIR", "/root/.qclaw")
    heartbeat_interval: int = int(os.getenv("HEARTBEAT_INTERVAL", "30"))
    sse_reconnect_delay: int = 5  # SSE断线重连延迟(秒)
    task_poll_interval: int = 3  # 任务轮询间隔(秒)

    @property
    def events_url(self) -> str:
        """SSE事件订阅URL"""
        return f"{self.manager_url}/api/openclaws/{self.claw_id}/events"

    @property
    def report_url(self) -> str:
        """任务结果上报URL"""
        return f"{self.manager_url}/api/openclaws/{self.claw_id}/report"

    @property
    def config_path(self) -> str:
        """OpenClaw配置文件路径"""
        return os.path.join(self.openclaw_dir, "openclaw.json")

    @property
    def workspace_dir(self) -> str:
        """OpenClaw workspace目录"""
        return os.path.join(self.openclaw_dir, "workspace")


# 全局配置实例
config = AgentConfig()
