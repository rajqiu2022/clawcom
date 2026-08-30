import sys
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path


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
    ResourceLease,
    ShiftLeftAnalysisRun,
    ShiftLeftFinding,
    ShiftLeftFindingFeedback,
    TestCase as CaseModel,
    TestCaseLibrary as CaseLibraryModel,
    TestCaseLibraryPromotion as CaseLibraryPromotionModel,
    User,
    WorkflowDefinition,
    WorkflowEvidenceManifest,
    WorkflowRun,
    WorkflowRunLibrarySnapshot,
)


class AutomationClosedLoopApiTest(unittest.TestCase):
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
        self.other_project = Project(name='Other')
        self.admin = User(username='loop_owner', role='super_admin')
        self.admin.set_password('secret')
        self.member = User(
            username='loop_member', role='user', managed_projects=[])
        self.member.set_password('secret')
        db.session.add_all([
            self.project, self.other_project, self.admin, self.member])
        db.session.flush()
        self.member.managed_projects = [self.project.id]
        self._seed_overview()
        db.session.commit()
        self.client = self.app.test_client()
        self._login(self.admin.id)

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _login(self, user_id):
        with self.client.session_transaction() as session:
            session['user_id'] = user_id

    def _seed_overview(self):
        self.library = CaseLibraryModel(
            id=33, name='Production #33', project_name=self.project.name,
            owner='qa', revision=7, content_hash='sha256:' + 'a' * 64)
        db.session.add(self.library)
        db.session.flush()
        db.session.add(CaseModel(
            library_id=self.library.id, case_id='RG-001',
            title='Reward smoke', priority='P1', type='functional'))

        self.definition = WorkflowDefinition(
            id=12, workflow_key='flow12', name='Flow12',
            project_id=self.project.id, definition_json={'steps': []},
            owner_type='user', owner_id=self.admin.id,
            executor_acl_json={'user_ids': [self.admin.id]},
            visibility_scope='project')
        db.session.add(self.definition)
        db.session.flush()
        self.run = WorkflowRun(
            definition_id=self.definition.id, project_id=self.project.id,
            run_name='Flow12 smoke', status='succeeded',
            controller_run_id='controller-rg-1')
        db.session.add(self.run)
        db.session.flush()
        db.session.add(WorkflowRunLibrarySnapshot(
            workflow_run_id=self.run.id, library_id=33,
            library_revision=7, snapshot_version=1,
            content_hash='sha256:' + 'b' * 64,
            case_ids_json=[1], case_count=1,
            snapshot_json={
                'schema': 'workflow-run-library-snapshot-v1',
                'cases': [{'id': 1, 'title': 'Reward smoke'}]},
            frozen_by='loop_owner'))

        gap = CapabilityGap(
            project_id=self.project.id, gap_key='unity-reward-reset',
            title='Reward reset hook', status='in_progress',
            missing_capabilities_json=['reward.reset'],
            candidate_requeue_pending=True,
            development_requirement_ref='TAPD-1001',
            development_requirement_url='https://tapd.example/1001',
            development_requirement_status='developing')
        db.session.add(gap)
        db.session.flush()
        old = datetime.now() - timedelta(hours=72)
        db.session.add_all([
            AutomationCaseCandidate(
                project_id=self.project.id, title='Reward canary',
                state='READY_FOR_CANARY', dedupe_key='reward-canary',
                created_at=old, updated_at=old),
            AutomationCaseCandidate(
                project_id=self.project.id, title='Reward retry',
                state='WAITING_CAPABILITY', capability_gap_id=gap.id,
                dedupe_key='reward-retry'),
            AutomationCaseCandidate(
                project_id=self.project.id, title='Shop publication',
                state='PENDING_PUBLISH', dedupe_key='shop-publish'),
            AutomationCaseCandidate(
                project_id=self.other_project.id, title='Hidden candidate',
                state='READY_FOR_CANARY', dedupe_key='hidden'),
        ])
        db.session.add(CaseLibraryPromotionModel(
            library_id=33, operation_type='promotion',
            idempotency_key='promotion-1', request_hash='c' * 64,
            before_revision=6, after_revision=7,
            content_hash='sha256:' + 'a' * 64,
            response_json={'after_revision': 7}, created_by='loop_owner'))

        db.session.add_all([
            ResourceLease(
                lease_group_id='lease-rg', resource_key='device:rg-01',
                resource_type='device', active_slot='device:rg-01',
                owner_type='workflow_run', owner_id=str(self.run.id),
                controller_run_id='controller-rg-1', priority=20,
                status='active', ttl_seconds=900,
                idempotency_key='lease-1', request_hash='sha256:' + 'd' * 64,
                holder_actor_type='user', holder_actor_id=self.admin.id,
                holder_actor_name='loop_owner',
                expires_at=datetime.now() + timedelta(minutes=15)),
            ResourceLease(
                lease_group_id='lease-hidden', resource_key='device:hidden',
                resource_type='device', active_slot='device:hidden',
                owner_type='external', owner_id='hidden', priority=1,
                status='active', ttl_seconds=900,
                idempotency_key='lease-hidden',
                request_hash='sha256:' + 'e' * 64,
                holder_actor_type='user', holder_actor_id=self.admin.id,
                holder_actor_name='loop_owner',
                metadata_json={'project_id': self.other_project.id},
                expires_at=datetime.now() + timedelta(minutes=15)),
        ])

        analysis = ShiftLeftAnalysisRun(
            project_id=self.project.id, workflow_run_id=self.run.id,
            baseline_fingerprint='f' * 64, status='completed_with_findings',
            baseline_json={'commit': 'abc'}, created_by='loop_owner')
        db.session.add(analysis)
        db.session.flush()
        finding = ShiftLeftFinding(
            project_id=self.project.id, analysis_run_id=analysis.id,
            finding_key='reward-mismatch', title='Reward mismatch',
            severity='high', confidence=0.9,
            evidence_level='runtime_confirmed', status='confirmed',
            created_by='loop_owner', created_by_actor_key='user:1')
        db.session.add(finding)
        db.session.flush()
        db.session.add_all([
            WorkflowEvidenceManifest(
                project_id=self.project.id, workflow_run_id=self.run.id,
                coverage_json={'console': 'complete'}, artifacts_json=[],
                required_evidence_json=['console'],
                completeness_status='complete',
                classification='PRODUCT_BUG_CANDIDATE'),
            ShiftLeftFindingFeedback(
                project_id=self.project.id, finding_id=finding.id,
                label='true_positive', note='confirmed',
                evidence_refs_json=[], source_type='human',
                actor_type='user', actor_id=self.admin.id,
                actor_key=f'user:{self.admin.id}', actor_name='loop_owner',
                idempotency_key='feedback-1', request_hash='a' * 64),
        ])

        rule = AnalysisRule(
            project_id=self.project.id, rule_key='reward-consistency',
            name='Reward consistency', status='active', latest_version=1)
        db.session.add(rule)
        db.session.flush()
        version = AnalysisRuleVersion(
            rule_id=rule.id, version_no=1, status='active',
            definition_json={'kind': 'consistency'}, thresholds_json={},
            definition_hash='b' * 64)
        db.session.add(version)
        db.session.flush()
        rule.active_version_id = version.id
        db.session.add(AnalysisRuleReplay(
            rule_version_id=version.id, workflow_run_id=self.run.id,
            dataset_snapshot_ref='hub://dataset/1', dataset_hash='c' * 64,
            metrics_json={'sample_count': 100}, gate_passed=True,
            gate_reasons_json=[], status='completed',
            idempotency_key='replay-1', request_hash='d' * 64,
            actor_type='user', actor_id=self.admin.id,
            actor_name='loop_owner'))

    def test_overview_aggregates_the_full_loop_without_cross_project_data(self):
        response = self.client.get(
            f'/api/v1/automation-closed-loop/overview?project_id={self.project.id}')

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = response.get_json()
        self.assertEqual(data['filters']['library_id'], 33)
        self.assertEqual(data['filters']['workflow_definition_id'], 12)
        self.assertEqual(data['candidates']['total'], 3)
        self.assertEqual(data['candidates']['stale_count'], 1)
        self.assertEqual(
            len(data['candidates']['queues']['pending_qualification']), 1)
        self.assertEqual(data['capability_gaps']['waiting_candidate_count'], 1)
        self.assertEqual(
            data['capability_gaps']['items'][0]['development_requirement']['ref'],
            'TAPD-1001')
        self.assertEqual(data['resource_leases']['active_count'], 1)
        self.assertEqual(
            data['resource_leases']['items'][0]['resource_key'], 'device:rg-01')
        self.assertEqual(data['testcase_library']['current']['revision'], 7)
        self.assertEqual(
            data['workflow_runs']['items'][0]['library_snapshot']['case_count'], 1)
        self.assertEqual(data['evidence']['completeness_rate'], 1.0)
        self.assertEqual(data['findings']['feedback_counts']['true_positive'], 1)
        self.assertEqual(data['analysis_rules']['replay_counts']['passed'], 1)
        self.assertIn('/entity-relations/trace?',
                      data['candidates']['recent_items'][0]['trace_api'])
        self.assertEqual(response.headers['Cache-Control'], 'no-store')

    def test_project_access_and_query_validation_are_enforced(self):
        self._login(self.member.id)
        allowed = self.client.get(
            f'/api/v1/automation-closed-loop/overview?project_id={self.project.id}')
        denied = self.client.get(
            '/api/v1/automation-closed-loop/overview'
            f'?project_id={self.other_project.id}')
        invalid = self.client.get(
            f'/api/v1/automation-closed-loop/overview?project_id={self.project.id}'
            '&limit=999')

        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(denied.status_code, 404)
        self.assertEqual(denied.get_json()['code'], 'PROJECT_NOT_FOUND')
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(
            invalid.get_json()['code'], 'AUTOMATION_CLOSED_LOOP_QUERY_INVALID')

    def test_empty_project_returns_stable_zero_shape(self):
        response = self.client.get(
            '/api/v1/automation-closed-loop/overview'
            f'?project_id={self.other_project.id}&library_id=999'
            '&workflow_definition_id=999')

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data['candidates']['total'], 1)
        self.assertFalse(data['testcase_library']['found'])
        self.assertEqual(data['workflow_runs']['items'], [])
        self.assertIsNone(data['evidence']['completeness_rate'])

        default_library = self.client.get(
            '/api/v1/automation-closed-loop/overview'
            f'?project_id={self.other_project.id}').get_json()['testcase_library']
        self.assertFalse(default_library['found'])


class AutomationClosedLoopFrontendContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = (_WEB / 'templates' / 'automation_closed_loop.html').read_text(
            encoding='utf-8')
        cls.base = (_WEB / 'templates' / 'base.html').read_text(encoding='utf-8')
        cls.views = (_WEB / 'app' / 'views' / '__init__.py').read_text(
            encoding='utf-8')
        cls.workflows = (_WEB / 'templates' / 'workflows.html').read_text(
            encoding='utf-8')

    def test_page_has_project_filters_and_all_p0_dashboard_sections(self):
        for marker in (
                'acl-project', 'acl-library', 'acl-flow', '候选流水线',
                'Capability Gap', '用例库与 Flow 快照', '运行证据与结论',
                'Finding 与规则学习', '共享资源占用', 'openTrace'):
            self.assertIn(marker, self.template)
        self.assertIn('/automation-closed-loop/overview?', self.template)

    def test_route_and_navigation_entry_are_registered(self):
        self.assertIn("@views_bp.route('/automation-closed-loop')", self.views)
        self.assertIn('href="/automation-closed-loop"', self.base)
        self.assertIn('nav_automation_closed_loop', self.base)
        self.assertIn("get('run_id')", self.workflows)


if __name__ == '__main__':
    unittest.main()
