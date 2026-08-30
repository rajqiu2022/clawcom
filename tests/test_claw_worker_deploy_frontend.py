import unittest
from pathlib import Path


TEMPLATE = (
    Path(__file__).resolve().parents[1] / 'web' / 'templates' / 'openclaws.html'
).read_text(encoding='utf-8')


class ClawWorkerDeployFrontendTest(unittest.TestCase):
    def test_create_entry_exposes_hermes_and_claw_worker(self):
        self.assertIn('value="hermes"', TEMPLATE)
        self.assertIn('value="codex"', TEMPLATE)
        self.assertIn('Claw Worker', TEMPLATE)
        self.assertIn('Codex SDK', TEMPLATE)

    def test_claw_worker_auth_choices_are_subscription_or_timiai(self):
        self.assertIn('value="chatgpt_subscription"', TEMPLATE)
        self.assertIn('value="timiai"', TEMPLATE)
        self.assertIn('create-codex-workspace', TEMPLATE)
        self.assertIn('create-codex-auth-mode', TEMPLATE)

    def test_create_payload_carries_worker_contract(self):
        for key in (
            "agent_type: agentType",
            "codex_workspace:",
            "codex_auth_mode:",
            "codex_model:",
        ):
            self.assertIn(key, TEMPLATE)

    def test_card_renders_runtime_separately_from_provider(self):
        for marker in (
            'has_worker_runtime',
            'runtime_provider',
            'runtime_config_owner',
            '本机配置',
            '统一 Claw Worker 运行时',
        ):
            self.assertIn(marker, TEMPLATE)


if __name__ == '__main__':
    unittest.main()
