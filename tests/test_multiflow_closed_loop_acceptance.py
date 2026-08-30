import sys
import types
import unittest
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

    def __getattr__(self, name):
        return _Noop()


_stub('flask_cors', CORS=_Noop)
_stub('flask_socketio', SocketIO=_Noop, emit=_Noop(),
      join_room=_Noop(), leave_room=_Noop())

from flask import Flask  # noqa: E402

from app import db  # noqa: E402
from app.api import api_bp  # noqa: E402
from app.models import (  # noqa: E402
    AnalysisRule,
    AnalysisRuleReplay,
    AnalysisRuleVersion,
    AutomationCaseCandidate,
    CapabilityGap,
    Project,
    ShiftLeftAnalysisRun,
    User,
    WorkflowDefinition,
    WorkflowRun,
)


class MultiFlowClosedLoopAcceptanceTest(unittest.TestCase):
    """Acceptance cases that deliberately span more than one P0 module."""

    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SECRET_KEY='test-secret',
            TESTING=True,
            SHIFT_LEFT_ENABLED=True,
        )
        db.init_app(self.app)
        self.app.register_blueprint(api_bp, url_prefix='/api/v1')
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.project = Project(name='RacingGO')
        self.admin = User(username='acceptance_owner', role='super_admin')
        self.admin.set_password('secret')
        db.session.add_all([self.project, self.admin])
        db.session.flush()
        self.flow12 = WorkflowDefinition(
            id=12,
            workflow_key='racinggo_flow12_acceptance',
            name='RacingGO Flow12 acceptance',
            project_id=self.project.id,
            definition_json={
                'key': 'racinggo_flow12_acceptance',
                'steps': [{
                    'id': 'execute_legacy_flow',
                    'name': 'Execute unchanged Flow12',
                    'type': 'agent_task',
                    'depends_on': [],
                }],
            },
            status='active',
            owner_type='user',
            owner_id=self.admin.id,
            executor_acl_json={'user_ids': [self.admin.id], 'claw_ids': []},
            visibility_scope='project',
        )
        db.session.add(self.flow12)
        db.session.flush()
        self._seed_failed_peripheral_objects()
        db.session.commit()
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['user_id'] = self.admin.id

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _seed_failed_peripheral_objects(self):
        self.analysis = ShiftLeftAnalysisRun(
            project_id=self.project.id,
            baseline_fingerprint='a' * 64,
            status='failed',
            baseline_json={'commit': 'broken-analysis'},
            result_summary_json={'error': 'simulated code analysis failure'},
            created_by=self.admin.username,
            updated_by=self.admin.username,
        )
        self.gap = CapabilityGap(
            project_id=self.project.id,
            gap_key='missing-unity-reset',
            title='Missing Unity reset hook',
            missing_capabilities_json=['unity.reset'],
            status='open',
            candidate_requeue_pending=False,
            development_requirement_ref='TAPD-AT07',
        )
        db.session.add_all([self.analysis, self.gap])
        db.session.flush()
        self.candidate = AutomationCaseCandidate(
            project_id=self.project.id,
            title='Peripheral candidate waiting for capability',
            state='WAITING_CAPABILITY',
            capability_gap_id=self.gap.id,
            qualification_outcome='AUTOMATION_CAPABILITY_GAP',
            dedupe_key='at07-waiting-candidate',
        )
        self.rule = AnalysisRule(
            project_id=self.project.id,
            rule_key='at07-failed-replay',
            name='AT07 failed replay rule',
            status='draft',
            latest_version=1,
        )
        db.session.add_all([self.candidate, self.rule])
        db.session.flush()
        self.rule_version = AnalysisRuleVersion(
            rule_id=self.rule.id,
            version_no=1,
            status='draft',
            definition_json={'kind': 'acceptance-only'},
            thresholds_json={'min_precision': 0.9},
            definition_hash='b' * 64,
        )
        db.session.add(self.rule_version)
        db.session.flush()
        self.replay = AnalysisRuleReplay(
            rule_version_id=self.rule_version.id,
            dataset_snapshot_ref='hub://acceptance/at07',
            dataset_hash='c' * 64,
            metrics_json={
                'sample_count': 10,
                'hit_count': 5,
                'true_positive_count': 1,
                'false_positive_count': 4,
                'false_negative_count': 2,
            },
            gate_passed=False,
            gate_reasons_json=['precision below threshold'],
            status='completed',
            idempotency_key='at07-failed-replay',
            request_hash='d' * 64,
            actor_type='user',
            actor_id=self.admin.id,
            actor_name=self.admin.username,
        )
        db.session.add(self.replay)

    def test_at07_peripheral_failures_do_not_block_or_mutate_flow12(self):
        definition_before = dict(self.flow12.definition_json)
        response = self.client.post('/api/v1/workflow-runs', json={
            'workflow_definition_id': self.flow12.id,
            'idempotency_key': 'at07-flow12-start',
            'controller_run_id': 'at07-controller',
            'correlation_id': 'at07-correlation',
            'trigger_source': 'acceptance_test',
            'start_vars': {'legacy_flow': True},
        })

        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        payload = response.get_json()
        self.assertEqual(payload['definition_id'], self.flow12.id)
        self.assertEqual(payload['controller_run_id'], 'at07-controller')
        self.assertNotIn('warnings', payload['context'])
        self.assertEqual(WorkflowRun.query.count(), 1)

        db.session.refresh(self.flow12)
        db.session.refresh(self.analysis)
        db.session.refresh(self.candidate)
        db.session.refresh(self.gap)
        db.session.refresh(self.rule_version)
        db.session.refresh(self.replay)
        self.assertEqual(self.flow12.definition_json, definition_before)
        self.assertEqual(self.analysis.status, 'failed')
        self.assertEqual(self.candidate.state, 'WAITING_CAPABILITY')
        self.assertEqual(self.gap.status, 'open')
        self.assertEqual(self.rule_version.status, 'draft')
        self.assertFalse(self.replay.gate_passed)


if __name__ == '__main__':
    unittest.main()
