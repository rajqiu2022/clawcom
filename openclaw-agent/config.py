"""
OpenClaw Manager 子agent 配置模块

配置优先级：
1. ~/.qclaw/agent.md 中的 YAML front matter（hub_url, claw_id, api_token）
2. 环境变量（MANAGER_URL, CLAW_ID, API_TOKEN）
3. 默认值
"""
import os
import re
from dataclasses import dataclass


def _load_agent_md_config(openclaw_dir: str) -> dict:
    """从 ~/.qclaw/agent.md 的 YAML front matter 读取配置"""
    agent_md_path = os.path.join(openclaw_dir, "agent.md")
    if not os.path.exists(agent_md_path):
        return {}

    try:
        with open(agent_md_path, "r", encoding="utf-8") as f:
            content = f.read()

        # 解析 YAML front matter (--- ... ---)
        match = re.match(r'^---\s*\n(.*?)\n---', content, re.DOTALL)
        if not match:
            return {}

        front_matter = match.group(1)
        config = {}

        for line in front_matter.split('\n'):
            line = line.strip()
            if ':' not in line:
                continue
            key, _, value = line.partition(':')
            key = key.strip()
            value = value.strip().strip('"').strip("'")

            if key in ('hub_url', 'api_token'):
                config[key] = value
            elif key == 'claw_id':
                try:
                    config[key] = int(value)
                except ValueError:
                    pass

        return config
    except Exception:
        return {}


# 先确定 openclaw_dir（仅此字段不受 agent.md 影响）
_openclaw_dir = os.getenv("OPENCLAW_DIR", "/root/.qclaw")

# 从 agent.md 加载配置
_agent_md = _load_agent_md_config(_openclaw_dir)


@dataclass
class AgentConfig:
    """子agent配置"""
    manager_url: str = _agent_md.get("hub_url") or os.getenv("MANAGER_URL", "http://clawteam.woa.com:18800")
    claw_id: int = _agent_md.get("claw_id") or int(os.getenv("CLAW_ID", "0"))
    api_token: str = _agent_md.get("api_token") or os.getenv("API_TOKEN", "")
    openclaw_dir: str = _openclaw_dir
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
