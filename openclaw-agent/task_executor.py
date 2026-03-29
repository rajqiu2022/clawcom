"""
OpenClaw Manager 子agent - 任务执行器
根据任务类型调用相应的操作
"""
import logging
from typing import Dict, Any

from claw_operator import ClawOperator
from sse_client import TaskReporter

logger = logging.getLogger(__name__)


class TaskExecutor:
    """任务执行器"""

    def __init__(self):
        self.operator = ClawOperator()
        self.reporter = TaskReporter()

    def execute(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行任务

        Args:
            task: 任务对象，包含 task_id, task_type, command, target_path, payload

        Returns:
            {"status": "completed/failed", "result": "...", "error": "..."}
        """
        task_id = task.get("task_id")
        task_type = task.get("task_type")
        command = task.get("command")
        target_path = task.get("target_path")
        payload = task.get("payload", {})

        logger.info(f"执行任务 {task_id}: type={task_type}")

        try:
            if task_type == "modify_config":
                result = self._handle_modify_config(payload)
            elif task_type == "execute_command":
                result = self._handle_execute_command(command, payload)
            elif task_type == "update_file":
                result = self._handle_update_file(target_path, payload)
            elif task_type == "read_file":
                result = self._handle_read_file(target_path)
            elif task_type == "delete_file":
                result = self._handle_delete_file(target_path)
            elif task_type == "list_files":
                result = self._handle_list_files(payload)
            elif task_type == "restart":
                result = self._handle_restart()
            elif task_type == "get_status":
                result = self._handle_get_status()
            elif task_type == "sync_skills":
                result = self._handle_sync_skills(payload)
            elif task_type == "sync_rules":
                result = self._handle_sync_rules(payload)
            else:
                result = {"error": f"未知任务类型: {task_type}"}
                status = "failed"
                self.reporter.report(task_id, "failed", error=result["error"])
                return {"status": "failed", "result": None, "error": result["error"]}

            # 上报结果
            if "error" in result:
                status = "failed"
                self.reporter.report(task_id, "failed", error=result["error"])
            else:
                status = "completed"
                self.reporter.report(task_id, "completed", result=str(result))

            return {"status": status, "result": result}

        except Exception as e:
            logger.exception(f"任务执行异常: {e}")
            self.reporter.report(task_id, "failed", error=str(e))
            return {"status": "failed", "result": None, "error": str(e)}

    def _handle_modify_config(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """处理配置修改"""
        updated = self.operator.modify_config(payload)
        return {"message": "配置已更新", "config": updated}

    def _handle_execute_command(self, command: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """处理命令执行"""
        if not command:
            return {"error": "command不能为空"}

        cwd = payload.get("cwd")
        timeout = payload.get("timeout", 60)

        result = self.operator.execute_command(command, cwd=cwd, timeout=timeout)
        return {
            "returncode": result["returncode"],
            "stdout": result["stdout"],
            "stderr": result["stderr"],
        }

    def _handle_update_file(self, target_path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """处理文件更新"""
        if not target_path:
            return {"error": "target_path不能为空"}

        content = payload.get("content", "")
        self.operator.write_file(target_path, content)
        return {"message": f"文件已更新: {target_path}"}

    def _handle_read_file(self, target_path: str) -> Dict[str, Any]:
        """处理文件读取"""
        if not target_path:
            return {"error": "target_path不能为空"}

        content = self.operator.read_file(target_path)
        return {"path": target_path, "content": content}

    def _handle_delete_file(self, target_path: str) -> Dict[str, Any]:
        """处理文件删除"""
        if not target_path:
            return {"error": "target_path不能为空"}

        self.operator.delete_file(target_path)
        return {"message": f"文件已删除: {target_path}"}

    def _handle_list_files(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """处理文件列表"""
        pattern = payload.get("pattern", "**/*.md")
        files = self.operator.list_workspace_files(pattern)
        return {"pattern": pattern, "files": files, "count": len(files)}

    def _handle_restart(self) -> Dict[str, Any]:
        """处理重启"""
        success = self.operator.restart_openclaw()
        return {"message": "OpenClaw已重启" if success else "重启失败"}

    def _handle_get_status(self) -> Dict[str, Any]:
        """处理状态查询"""
        status = self.operator.get_openclaw_status()
        disk = self.operator.get_disk_usage()
        config = self.operator.read_config()

        return {
            "openclaw": status,
            "disk": disk,
            "config_keys": list(config.keys()) if config else [],
        }

    def _handle_sync_skills(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """处理 Skills 同步"""
        skills = payload.get('skills', [])
        if not skills:
            return {"message": "没有需要同步的 Skills", "synced": 0}

        # 更新 OpenClaw 的 skills 配置
        config_path = self.operator.config_path
        config = self.operator.read_config() or {}

        # 存储 skills 到配置
        config['skills'] = skills
        self.operator.modify_config({'skills': skills})

        return {
            "message": f"已同步 {len(skills)} 个 Skills",
            "synced": len(skills),
            "skills": [s.get('name') for s in skills],
        }

    def _handle_sync_rules(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """处理 Rules 同步"""
        rules = payload.get('rules', [])
        if not rules:
            return {"message": "没有需要同步的 Rules", "synced": 0}

        # 更新 OpenClaw 的 rules 配置
        config = self.operator.read_config() or {}

        # 存储 rules 到配置
        config['rules'] = rules
        self.operator.modify_config({'rules': rules})

        return {
            "message": f"已同步 {len(rules)} 个 Rules",
            "synced": len(rules),
            "rules": [r.get('name') for r in rules],
        }
