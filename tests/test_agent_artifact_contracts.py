import sys
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path


_WEB = Path(__file__).resolve().parents[1] / 'web'
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

from flask import Flask  # noqa: E402

from app import db  # noqa: E402
from app.api import api_bp  # noqa: E402
from app.models import (  # noqa: E402
    OpenClawInstance, Project, User, WorkflowMission, hash_token,
)


class AgentArtifactContractsTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SECRET_KEY='artifact-contract-test', TESTING=True,
            AGENT_TEAM_CONTRACTS_ENABLED=True,
        )
        db.init_app(self.app)
        self.app.register_blueprint(api_bp, url_prefix='/api/v1')
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.project = Project(name='Contract Project')
        self.admin = User(
            username='contract_admin', role='super_admin', managed_projects=[])
        self.admin.set_password('secret')
        db.session.add_all([self.project, self.admin])
        db.session.flush()
        self.token = 'contract-agent-token'
        self.claw = OpenClawInstance(
            name='合同 Agent', safe_name='contract-agent',
            claw_tag='contract-agent', owner=self.admin.username,
            project_id=self.project.id, api_token_hash=hash_token(self.token),
            status='工作', role='specialist', llm_provider='timiai')
        db.session.add(self.claw)
        db.session.flush()
        self.baseline = 'sha256:' + ('a' * 64)
        self.mission = WorkflowMission(
            mission_key='contract-mission', project_id=self.project.id,
            main_claw_id=self.claw.id, objective='验证三类产物合同',
            status='active', control_mode='agent_autonomous',
            context_json={
                'input_baseline_sha256': self.baseline,
                'remote_source_ids': ['racinggo_unity'],
                'selected_case_ids': [101, 102],
                'environment_baseline': {
                    'code_commit': 'abc123', 'build_id': 'build-9',
                    'runner_release': 'runner-1',
                    'adapter_release': 'adapter-1',
                    'device_generation': 'device-gen-3',
                },
            },
            created_by_type='user', created_by_id=self.admin.id,
            created_by_name=self.admin.username,
            expires_at=datetime.now() + timedelta(hours=8))
        db.session.add(self.mission)
        db.session.commit()
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session.clear()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _headers(self, key):
        return {
            'Authorization': f'Bearer {self.token}',
            'Idempotency-Key': key,
        }

    def _post(self, artifact_type, content, key, version=1, baseline=None):
        return self.client.post(
            '/api/v1/agent-artifacts',
            json={
                'project_id': self.project.id,
                'mission_id': self.mission.id,
                'stage_id': artifact_type,
                'artifact_type': artifact_type,
                'schema_version': 1,
                'artifact_version': version,
                'input_baseline_sha256': (
                    self.baseline if baseline is None else baseline),
                'content': content,
            }, headers=self._headers(key))

    def _source_ref(self):
        return {
            'type': 'knowledge', 'id': 334,
            'sha256': 'sha256:' + ('b' * 64),
        }

    def _requirement(self):
        return {
            'schema': 1,
            'artifact_type': 'requirement_analysis',
            'input_baseline_sha256': self.baseline,
            'summary': '梳理新玩法入口、奖励领取和异常恢复边界。',
            'actors': ['玩家'],
            'functional_flows': ['进入玩法并完成一局'],
            'acceptance_criteria': [{
                'id': 'AC-001', 'statement': '结算后奖励到账',
                'priority': 'P0', 'source_refs': [self._source_ref()],
                'testable': True,
            }],
            'risks': [{
                'id': 'RISK-001', 'description': '重连可能重复领奖',
                'impact': 'high', 'probability': 'medium',
                'source_refs': [self._source_ref()],
                'recommended_coverage': ['结算阶段断线重连'],
            }],
            'ambiguities': [], 'open_questions': [], 'out_of_scope': [],
            'evidence_refs': [self._source_ref()], 'confidence': 'high',
        }

    def _engineering(self):
        return {
            'schema': 1, 'artifact_type': 'engineering_analysis',
            'input_baseline_sha256': self.baseline,
            'repositories': [{
                'source_id': 'racinggo_unity', 'commit': 'abc123',
                'tree_digest': 'sha256:' + ('c' * 64),
            }],
            'affected_modules': [{'module': 'settlement'}],
            'changed_paths': [{
                'module': 'settlement',
                'path': 'Assets/Scripts/Settlement/Reward.cs',
                'evidence_refs': [self._source_ref()],
            }],
            'dependency_edges': [], 'runtime_entrypoints': [],
            'protocols': [], 'resource_dependencies': [],
            'testability_findings': [],
            'code_requirement_links': [{
                'acceptance_id': 'AC-001',
                'path': 'Assets/Scripts/Settlement/Reward.cs',
                'symbol': 'GrantReward', 'relation': 'implements',
                'evidence_refs': [self._source_ref()],
            }],
            'test_recommendations': ['验证断线重连幂等'],
            'inferences': ['结算由客户端展示、服务端发奖'],
            'unknowns': ['灰度开关配置位置待确认'],
            'evidence_refs': [self._source_ref()], 'confidence': 'medium',
        }

    def _receipt(self):
        return {
            'receipt_id': 'receipt-101', 'side_effect': True,
            'idempotency_key': 'case-101-action-1',
            'lease_id': 'device-lease-1', 'fencing_token': 8,
        }

    def _case_result(self, case_id, status='passed'):
        return {
            'case_id': case_id, 'status': status,
            'classification': 'business' if status == 'failed' else 'case',
            'started_at': '2026-08-28T10:00:00+08:00',
            'finished_at': '2026-08-28T10:01:00+08:00',
            'action_receipts': [self._receipt()],
            'observation_refs': [self._source_ref()],
            'evidence_refs': [self._source_ref()],
            'failure_fingerprint': 'reward-not-granted' if status == 'failed' else '',
            'retry_count': 0,
        }

    def _execution(self):
        return {
            'schema': 1, 'artifact_type': 'execution_record',
            'environment_baseline': dict(
                self.mission.context_json['environment_baseline']),
            'case_results': [
                self._case_result(101), self._case_result(102)],
            'environment_failures': [], 'tool_failures': [],
            'business_failures': [],
            'summary': '两条人工选择用例均通过。',
            'evidence_manifest_id': 88,
        }

    def test_requirement_contract_accepts_valid_and_rejects_missing_source(self):
        valid = self._post(
            'requirement_analysis', self._requirement(), 'requirement-valid')
        self.assertEqual(valid.status_code, 201, valid.get_data(as_text=True))
        self.assertEqual(valid.get_json()['validation']['mode'], 'strict')
        self.assertTrue(valid.get_json()['validation']['valid'])

        invalid_content = self._requirement()
        invalid_content['acceptance_criteria'][0]['source_refs'] = []
        invalid = self._post(
            'requirement_analysis', invalid_content,
            'requirement-invalid', version=2)
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(
            invalid.get_json()['code'], 'ARTIFACT_CONTRACT_INVALID')
        self.assertIn(
            'AC_SOURCE_REQUIRED',
            {item['code'] for item in invalid.get_json()['details']['errors']})

    def test_requirement_untestable_item_requires_question_or_ambiguity(self):
        content = self._requirement()
        content['acceptance_criteria'][0]['testable'] = False
        response = self._post(
            'requirement_analysis', content, 'requirement-untestable')
        self.assertEqual(response.status_code, 400)
        self.assertIn(
            'UNTESTABLE_CRITERION_UNTRACKED',
            {item['code'] for item in response.get_json()['details']['errors']})

    def test_engineering_contract_checks_hub_approved_source_and_module_evidence(self):
        valid = self._post(
            'engineering_analysis', self._engineering(), 'engineering-valid')
        self.assertEqual(valid.status_code, 201, valid.get_data(as_text=True))

        content = self._engineering()
        content['repositories'][0]['source_id'] = 'unapproved_repo'
        content['changed_paths'] = []
        content['code_requirement_links'] = []
        invalid = self._post(
            'engineering_analysis', content, 'engineering-invalid', version=2)
        self.assertEqual(invalid.status_code, 400)
        codes = {item['code'] for item in invalid.get_json()['details']['errors']}
        self.assertIn('REMOTE_SOURCE_NOT_ALLOWED', codes)
        self.assertIn('MODULE_EVIDENCE_REQUIRED', codes)

    def test_execution_contract_checks_case_coverage_receipts_and_baseline(self):
        valid = self._post(
            'execution_record', self._execution(), 'execution-valid',
            baseline='')
        self.assertEqual(valid.status_code, 201, valid.get_data(as_text=True))

        content = self._execution()
        content['case_results'] = [self._case_result(101)]
        content['case_results'][0]['action_receipts'] = []
        content['environment_baseline']['build_id'] = 'wrong-build'
        invalid = self._post(
            'execution_record', content, 'execution-invalid', version=2,
            baseline='')
        self.assertEqual(invalid.status_code, 400)
        codes = {item['code'] for item in invalid.get_json()['details']['errors']}
        self.assertIn('CASE_COVERAGE_MISMATCH', codes)
        self.assertIn('PASSED_RECEIPT_REQUIRED', codes)
        self.assertIn('ENVIRONMENT_BASELINE_MISMATCH', codes)

    def test_sensitive_values_and_oversized_content_fail_closed(self):
        content = self._requirement()
        content['summary'] = 'accidental token oc_tk_' + ('1' * 48)
        sensitive = self._post(
            'requirement_analysis', content, 'requirement-sensitive')
        self.assertEqual(sensitive.status_code, 400)
        self.assertEqual(
            sensitive.get_json()['code'], 'SENSITIVE_DATA_DETECTED')

        oversized = {'blob': 'x' * (512 * 1024)}
        too_large = self._post(
            'legacy_large_artifact', oversized, 'artifact-too-large')
        self.assertEqual(too_large.status_code, 400)
        self.assertEqual(too_large.get_json()['code'], 'CONTENT_TOO_LARGE')

    def test_submit_revalidates_against_current_mission_baseline(self):
        created = self._post(
            'requirement_analysis', self._requirement(),
            'requirement-baseline-drift').get_json()
        context = dict(self.mission.context_json)
        context['input_baseline_sha256'] = 'sha256:' + ('d' * 64)
        self.mission.context_json = context
        db.session.commit()

        replay = self._post(
            'requirement_analysis', self._requirement(),
            'requirement-baseline-drift')
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.get_json()['id'], created['id'])

        submitted = self.client.post(
            f"/api/v1/agent-artifacts/{created['id']}/submit",
            headers=self._headers('requirement-submit-drift'))
        self.assertEqual(submitted.status_code, 409)
        self.assertEqual(
            submitted.get_json()['code'], 'ARTIFACT_CONTRACT_INVALID')
        self.assertIn(
            'MISSION_BASELINE_MISMATCH',
            {item['code'] for item in submitted.get_json()['details']['errors']})

    def test_accept_review_revalidates_baseline_after_submission(self):
        created = self._post(
            'requirement_analysis', self._requirement(),
            'requirement-review-drift').get_json()
        submitted = self.client.post(
            f"/api/v1/agent-artifacts/{created['id']}/submit",
            headers=self._headers('requirement-review-submit'))
        self.assertEqual(submitted.status_code, 200)
        context = dict(self.mission.context_json)
        context['input_baseline_sha256'] = 'sha256:' + ('e' * 64)
        self.mission.context_json = context
        db.session.commit()
        with self.client.session_transaction() as session:
            session.clear()
            session['user_id'] = self.admin.id
        reviewed = self.client.post(
            f"/api/v1/agent-artifacts/{created['id']}/review",
            json={'decision': 'accepted', 'comment': '旧基线不应通过'})
        self.assertEqual(reviewed.status_code, 409)
        self.assertIn(
            'MISSION_BASELINE_MISMATCH',
            {item['code'] for item in reviewed.get_json()['details']['errors']})


if __name__ == '__main__':
    unittest.main()
