import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    REPO_ROOT
    / "openclaw-agent"
    / "skills"
    / "hub-sse-sidecar"
    / "scripts"
    / "wecom_channel.py"
)
SPEC = importlib.util.spec_from_file_location("linux_wecom_channel_context", MODULE_PATH)
wecom_channel = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wecom_channel)


class LinuxWeComContextTest(unittest.TestCase):
    def test_authorized_turn_injects_sidecar_context_without_credentials(self):
        captured = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            credentials = root / "wecom-credentials.json"
            credentials.write_text(
                json.dumps(
                    {
                        "schema": 2,
                        "bot_id": "bot-secret-id",
                        "secret": "wecom-secret",
                        "owner_user_id": "owner",
                        "allowed_user_ids": [],
                        "allowed_chat_ids": [],
                    }
                ),
                encoding="utf-8",
            )
            channel = wecom_channel.WeComChannel(
                credentials_path=str(credentials),
                database_path=str(root / "wecom.db"),
                node_path="node",
                bridge_script="bridge.mjs",
                sdk_root="sdk",
                invoke=lambda prompt, key: (
                    captured.append((prompt, key)) or (True, "完成", "")
                ),
                logger=lambda _message: None,
                context_lines=lambda: [
                    "当前身份：Claw #12 / 小马-高级测试经理",
                    "当前 Provider：codex",
                    "Hub capability index",
                    "岗位：高级测试经理",
                    "Hub 操作只走 Sidecar 受控接口",
                ],
            )
            channel.bridge = mock.Mock()

            channel._on_event(
                {
                    "event_id": "event-1",
                    "conversation": {"kind": "user", "id": "owner"},
                    "sender_id": "owner",
                    "text": "查询测试任务",
                }
            )

        prompt, session_key = captured[0]
        self.assertIn("[HUB_CONTEXT]", prompt)
        self.assertIn("Claw #12 / 小马-高级测试经理", prompt)
        self.assertIn("Hub capability index", prompt)
        self.assertIn("岗位：高级测试经理", prompt)
        self.assertIn("用户消息：查询测试任务", prompt)
        self.assertNotIn("wecom-secret", prompt)
        self.assertNotIn("bot-secret-id", prompt)
        self.assertTrue(session_key.startswith("wecom:"))


if __name__ == "__main__":
    unittest.main()
