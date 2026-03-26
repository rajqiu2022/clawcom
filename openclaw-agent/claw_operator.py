"""
OpenClaw Manager 子agent - OpenClaw操作器
负责读取/修改OpenClaw配置、操作workspace文件、执行shell命令
"""
import os
import json
import subprocess
import logging
from typing import Dict, Any, Optional

from config import config

logger = logging.getLogger(__name__)


class ClawOperator:
    """OpenClaw操作器"""

    def __init__(self):
        self.config_path = config.config_path
        self.workspace_dir = config.workspace_dir

    # ==================== 配置文件操作 ====================

    def read_config(self) -> Dict[str, Any]:
        """读取openclaw.json配置"""
        try:
            if not os.path.exists(self.config_path):
                logger.warning(f"配置文件不存在: {self.config_path}")
                return {}

            with open(self.config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"读取配置失败: {e}")
            raise

    def write_config(self, config_data: Dict[str, Any]) -> bool:
        """写入openclaw.json配置"""
        try:
            # 确保目录存在
            os.makedirs(os.path.dirname(self.config_path), exist_ok=True)

            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(config_data, f, indent=2, ensure_ascii=False)

            logger.info(f"配置已更新: {self.config_path}")
            return True
        except Exception as e:
            logger.error(f"写入配置失败: {e}")
            raise

    def modify_config(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        """
        部分更新配置

        Args:
            updates: 要更新的配置项，如 {"mcp": {...}, "plugins": [...]}

        Returns:
            更新后的完整配置
        """
        current = self.read_config()
        current.update(updates)
        self.write_config(current)
        return current

    # ==================== 文件操作 ====================

    def read_file(self, relative_path: str) -> str:
        """
        读取workspace下的文件

        Args:
            relative_path: 相对于workspace的路径

        Returns:
            文件内容
        """
        full_path = os.path.join(self.workspace_dir, relative_path)

        if not os.path.exists(full_path):
            raise FileNotFoundError(f"文件不存在: {full_path}")

        with open(full_path, "r", encoding="utf-8") as f:
            return f.read()

    def write_file(self, relative_path: str, content: str, create_dirs: bool = True) -> bool:
        """
        写入workspace下的文件

        Args:
            relative_path: 相对于workspace的路径
            content: 文件内容
            create_dirs: 是否创建目录

        Returns:
            是否成功
        """
        full_path = os.path.join(self.workspace_dir, relative_path)

        if create_dirs:
            os.makedirs(os.path.dirname(full_path), exist_ok=True)

        try:
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(content)
            logger.info(f"文件已写入: {full_path}")
            return True
        except Exception as e:
            logger.error(f"写入文件失败: {e}")
            raise

    def list_workspace_files(self, pattern: str = "**/*.md") -> list:
        """
        列出workspace下的文件

        Args:
            pattern: glob模式，如 "**/*.md"

        Returns:
            文件路径列表
        """
        import glob

        search_pattern = os.path.join(self.workspace_dir, pattern)
        files = glob.glob(search_pattern, recursive=True)
        return [os.path.relpath(f, self.workspace_dir) for f in files]

    def delete_file(self, relative_path: str) -> bool:
        """删除workspace下的文件"""
        full_path = os.path.join(self.workspace_dir, relative_path)

        if not os.path.exists(full_path):
            logger.warning(f"文件不存在，跳过删除: {full_path}")
            return True

        try:
            os.remove(full_path)
            logger.info(f"文件已删除: {full_path}")
            return True
        except Exception as e:
            logger.error(f"删除文件失败: {e}")
            raise

    # ==================== Shell命令执行 ====================

    def execute_command(self, command: str, cwd: str = None, timeout: int = 60) -> Dict[str, Any]:
        """
        执行shell命令

        Args:
            command: 要执行的命令
            cwd: 工作目录，默认为openclaw_dir
            timeout: 超时时间（秒）

        Returns:
            {"returncode": 0, "stdout": "...", "stderr": "..."}
        """
        cwd = cwd or self.openclaw_dir

        logger.info(f"执行命令: {command}")

        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )

            return {
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        except subprocess.TimeoutExpired:
            logger.error(f"命令执行超时: {command}")
            raise
        except Exception as e:
            logger.error(f"命令执行失败: {e}")
            raise

    def restart_openclaw(self) -> bool:
        """重启OpenClaw服务（假设使用systemd）"""
        try:
            result = self.execute_command("systemctl restart openclaw", timeout=30)
            if result["returncode"] == 0:
                logger.info("OpenClaw服务已重启")
                return True
            else:
                logger.error(f"重启失败: {result['stderr']}")
                return False
        except Exception as e:
            logger.error(f"重启异常: {e}")
            return False

    # ==================== 信息查询 ====================

    def get_openclaw_status(self) -> Dict[str, Any]:
        """获取OpenClaw运行状态"""
        try:
            # 检查进程是否存在
            result = self.execute_command("pgrep -f openclaw || echo 'NOT_FOUND'")

            return {
                "running": "NOT_FOUND" not in result["stdout"],
                "process_check": result["stdout"].strip(),
            }
        except Exception as e:
            return {"error": str(e)}

    def get_disk_usage(self) -> Dict[str, Any]:
        """获取磁盘使用情况"""
        try:
            result = self.execute_command("df -h / | tail -1")
            parts = result["stdout"].split()
            if len(parts) >= 5:
                return {
                    "filesystem": parts[0],
                    "size": parts[1],
                    "used": parts[2],
                    "available": parts[3],
                    "use_percent": parts[4],
                }
        except Exception as e:
            return {"error": str(e)}
