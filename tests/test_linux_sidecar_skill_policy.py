import importlib.util
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    REPO_ROOT / 'openclaw-agent' / 'skills' / 'hub-sse-sidecar'
    / 'scripts' / 'sidecar_v2.py'
)
SPEC = importlib.util.spec_from_file_location('linux_sidecar_v251', MODULE_PATH)
sidecar = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sidecar
SPEC.loader.exec_module(sidecar)


class LinuxSidecarSkillPolicyTest(unittest.TestCase):
    def test_requested_skills_are_advisory_by_default(self):
        lines = sidecar.format_task_context({
            'required_skills': ['requirement-analysis'],
            'blocking_skills': [],
            'operating_protocol_skill': 'agent-operating-protocol',
        })
        rendered = '\n'.join(lines)

        self.assertIn('建议 Skill（可用时优先加载）', rendered)
        self.assertIn('记录降级后继续', rendered)
        self.assertNotIn('必备 Skill（先加载再动手）', rendered)
        self.assertNotIn('务必遵循其生命周期', rendered)

    def test_only_explicit_blocking_skills_stop_execution(self):
        rendered = '\n'.join(sidecar.format_task_context({
            'required_skills': ['optional-helper', 'release-safety'],
            'blocking_skills': ['release-safety'],
        }))

        self.assertIn('阻断型 Skill（缺失则停止）：release-safety', rendered)
        self.assertIn('只有上述阻断型 Skill 缺失时才停止任务', rendered)

    def test_profile_required_skills_are_not_described_as_mandatory(self):
        old_config = sidecar._config
        try:
            sidecar._config = {
                'active_agent_profile': {
                    'profile': {
                        'required_skills': ['case-review'],
                        'blocking_skills': ['security-gate'],
                    },
                },
            }
            rendered = '\n'.join(sidecar.agent_profile_lines())
        finally:
            sidecar._config = old_config

        self.assertIn(
            '建议 Skills（可用时优先加载；缺失本身不阻断）：case-review',
            rendered,
        )
        self.assertIn('阻断型 Skills（缺失则停止）：security-gate', rendered)
        self.assertNotIn('必装/必读 Skills', rendered)

    def test_wecom_context_contains_identity_capability_and_profile(self):
        old_config = sidecar._config
        old_claw_id = sidecar.CLAW_ID
        try:
            sidecar.CLAW_ID = '12'
            sidecar._config = {
                'agent_type': 'codex',
                'claw_name': '小马-高级测试经理',
                'hub_capability_digest': 'Hub capability index',
                'active_agent_profile': {
                    'post_name': '高级测试经理',
                    'profile': {
                        'name': '高级测试经理',
                        'version': 2,
                        'system_prompt': '负责测试分析、用例设计和质量报告。',
                    },
                },
            }
            rendered = '\n'.join(sidecar.wecom_context_lines())
        finally:
            sidecar._config = old_config
            sidecar.CLAW_ID = old_claw_id

        self.assertIn('Claw #12 / 小马-高级测试经理', rendered)
        self.assertIn('当前 Provider：codex', rendered)
        self.assertIn('Hub capability index', rendered)
        self.assertIn('岗位说明书', rendered)
        self.assertIn('负责测试分析、用例设计和质量报告。', rendered)
        self.assertIn('Sidecar', rendered)
        self.assertNotIn('CLAW_TOKEN=', rendered)


if __name__ == '__main__':
    unittest.main()
