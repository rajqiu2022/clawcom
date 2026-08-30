import json
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace


_ROOT = Path(__file__).resolve().parents[1]
_WEB = _ROOT / 'web'
if str(_WEB) not in sys.path:
    sys.path.insert(0, str(_WEB))


def _stub(name, **attrs):
    if name in sys.modules:
        return
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module


class _Noop:
    def __init__(self, *args, **kwargs):
        pass

    def __call__(self, *args, **kwargs):
        return self

    def __getattr__(self, _name):
        return _Noop()


_stub('flask_cors', CORS=_Noop)
_stub('flask_socketio', SocketIO=_Noop, emit=_Noop(),
      join_room=_Noop(), leave_room=_Noop())

from app.services.artifact_contracts import validate_artifact_contract  # noqa: E402


class GameTestAgentTeamV1Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = json.loads((
            _ROOT / 'openclaw-agent' / 'profiles' /
            'game-test-team-v1.json').read_text(encoding='utf-8'))
        cls.baseline = 'sha256:' + ('a' * 64)
        cls.source_ref = {
            'type': 'knowledge', 'id': 334,
            'sha256': 'sha256:' + ('b' * 64),
        }
        cls.mission = SimpleNamespace(context_json={
            'input_baseline_sha256': cls.baseline,
            'remote_source_ids': ['racinggo_unity'],
            'selected_case_ids': [101, 102],
            'environment_baseline': {
                'code_commit': 'abc123', 'build_id': 'build-9',
                'runner_release': 'runner-1',
                'adapter_release': 'adapter-1',
                'device_generation': 'device-gen-3',
            },
        })

    def test_profile_seed_maps_three_roles_to_discoverable_skill_packages(self):
        profiles = self.document['profiles']
        self.assertEqual(1, self.document['schema'])
        self.assertEqual(3, len(profiles))
        self.assertEqual(
            {'requirement_analyst', 'engineering_analyst', 'test_executor'},
            {row['contract']['role_key'] for row in profiles})
        for profile in profiles:
            contract = profile['contract']
            self.assertEqual(2, contract['task_envelope_schema'])
            self.assertTrue(contract['context_digest_required'])
            self.assertEqual('empty', contract['experience_candidates_p0'])
            self.assertEqual(
                profile['post']['post_key'], contract['role_key'])
            for skill_name in profile['required_skills']:
                skill_path = (
                    _ROOT / 'openclaw-agent' / 'skills' /
                    skill_name / 'SKILL.md')
                self.assertTrue(skill_path.is_file(), skill_path)
            mission_skill = profile['required_skills'][-1]
            content = (_ROOT / 'openclaw-agent' / 'skills' /
                       mission_skill / 'SKILL.md').read_text(encoding='utf-8')
            self.assertTrue(content.startswith('---\nname: '))
            self.assertIn(f'name: {mission_skill}\n', content)

    def _requirement(self, index):
        return {
            'schema': 1, 'artifact_type': 'requirement_analysis',
            'input_baseline_sha256': self.baseline,
            'summary': f'Golden {index}: 奖励结算需求分析。',
            'actors': ['玩家'],
            'functional_flows': [f'进入玩法并完成第{index}类结算'],
            'acceptance_criteria': [{
                'id': f'AC-{index:03d}', 'statement': '结算后奖励到账',
                'priority': 'P0', 'source_refs': [self.source_ref],
                'testable': True,
            }],
            'risks': [{
                'id': f'RISK-{index:03d}', 'description': '重连可能重复领奖',
                'impact': 'high', 'probability': 'medium',
                'source_refs': [self.source_ref],
                'recommended_coverage': ['结算阶段断线重连'],
            }],
            'ambiguities': [], 'open_questions': [], 'out_of_scope': [],
            'evidence_refs': [self.source_ref], 'confidence': 'high',
        }

    def _engineering(self, index):
        module = f'settlement{index}'
        path = f'Assets/Scripts/{module}/Reward.cs'
        return {
            'schema': 1, 'artifact_type': 'engineering_analysis',
            'input_baseline_sha256': self.baseline,
            'repositories': [{
                'source_id': 'racinggo_unity', 'commit': f'abc12{index}',
                'tree_digest': 'sha256:' + (str(index) * 64),
            }],
            'affected_modules': [{'module': module}],
            'changed_paths': [{
                'module': module, 'path': path,
                'evidence_refs': [self.source_ref],
            }],
            'dependency_edges': [], 'runtime_entrypoints': [],
            'protocols': [], 'resource_dependencies': [],
            'testability_findings': [],
            'code_requirement_links': [{
                'acceptance_id': f'AC-{index:03d}', 'path': path,
                'symbol': 'GrantReward', 'relation': 'implements',
                'evidence_refs': [self.source_ref],
            }],
            'test_recommendations': ['验证断线重连幂等'],
            'inferences': [f'推断-{index}'], 'unknowns': [f'未知-{index}'],
            'evidence_refs': [self.source_ref], 'confidence': 'medium',
        }

    def _case_result(self, case_id, index):
        return {
            'case_id': case_id, 'status': 'passed', 'classification': 'case',
            'started_at': f'2026-08-28T10:0{index}:00+08:00',
            'finished_at': f'2026-08-28T10:0{index}:30+08:00',
            'action_receipts': [{
                'receipt_id': f'receipt-{case_id}-{index}',
                'side_effect': True,
                'idempotency_key': f'case-{case_id}-{index}',
                'lease_id': f'device-lease-{index}', 'fencing_token': index,
            }],
            'observation_refs': [self.source_ref],
            'evidence_refs': [self.source_ref],
            'failure_fingerprint': '', 'retry_count': 0,
        }

    def _execution(self, index):
        return {
            'schema': 1, 'artifact_type': 'execution_record',
            'environment_baseline': dict(
                self.mission.context_json['environment_baseline']),
            'case_results': [
                self._case_result(101, index),
                self._case_result(102, index),
            ],
            'environment_failures': [], 'tool_failures': [],
            'business_failures': [],
            'summary': f'Golden {index}: 两条选例均有Runner证据并通过。',
            'evidence_manifest_id': 80 + index,
        }

    def test_each_role_has_five_golden_artifacts_passing_the_hub_contract(self):
        factories = {
            'requirement_analysis': self._requirement,
            'engineering_analysis': self._engineering,
            'execution_record': self._execution,
        }
        validated = []
        for profile in self.document['profiles']:
            artifact_type = profile['contract']['artifact_type']
            for index in range(1, 6):
                result = validate_artifact_contract(
                    artifact_type, 1, factories[artifact_type](index),
                    self.baseline if artifact_type != 'execution_record' else '',
                    self.mission)
                self.assertTrue(result['valid'])
                validated.append((artifact_type, index))
        self.assertEqual(15, len(validated))

    def test_eval_suite_has_five_golden_and_three_challenge_cases_per_role(self):
        suite = json.loads((
            _ROOT / 'openclaw-agent' / 'eval' /
            'game-test-team-v1.json').read_text(encoding='utf-8'))
        self.assertEqual(2, suite['review_policy']['required_reviewers'])
        grouped = {}
        all_keys = set()
        for dataset in suite['datasets']:
            key = (dataset['role_key'], dataset['split'])
            grouped[key] = len(dataset['cases'])
            for case in dataset['cases']:
                self.assertNotIn(case['case_key'], all_keys)
                all_keys.add(case['case_key'])
                self.assertTrue(case['input_snapshot'])
                self.assertTrue(case['expected_contract'])
                self.assertTrue(case['deterministic_checks'])
        for role_key in (
                'requirement_analyst', 'engineering_analyst', 'test_executor'):
            self.assertEqual(5, grouped[(role_key, 'golden')])
            self.assertEqual(3, grouped[(role_key, 'challenge')])
        self.assertEqual(24, len(all_keys))


if __name__ == '__main__':
    unittest.main()
