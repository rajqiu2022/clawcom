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
    EntityRelation,
    Project,
    ShiftLeftAnalysisRun,
    ShiftLeftFinding,
    TestReport,
    User,
    WorkflowDefinition,
    WorkflowEvidenceManifest,
    WorkflowRun,
)


class WorkflowEvidenceManifestApiTest(unittest.TestCase):
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
        self.admin = User(username='evidence_owner', role='super_admin')
        self.admin.set_password('secret')
        db.session.add_all([self.project, self.admin])
        db.session.flush()
        self.definition = WorkflowDefinition(
            workflow_key='flow12-evidence-test',
            name='Flow12 evidence test',
            project_id=self.project.id,
            definition_json={'key': 'flow12-evidence-test', 'steps': []},
            owner_type='user', owner_id=self.admin.id,
            executor_acl_json={'user_ids': [self.admin.id], 'claw_ids': []},
            visibility_scope='project',
        )
        db.session.add(self.definition)
        db.session.flush()
        self.run = WorkflowRun(
            definition_id=self.definition.id,
            run_name='Flow12 run with evidence',
            status='succeeded',
            project_id=self.project.id,
        )
        db.session.add(self.run)
        db.session.commit()
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['user_id'] = self.admin.id

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _manifest(self, coverage, key='manifest-create', **extra):
        body = {
            'expected_version': extra.pop('expected_version', 0),
            'coverage': coverage,
            'artifacts': extra.pop('artifacts', [{
                'type': 'screenshot',
                'uri': 'hub-artifact://runs/1/screenshots/result.png',
                'sha256': 'a' * 64,
                'size': 12345,
                'case_id': '33001',
                'step_id': 'action_cases_execute',
            }]),
        }
        body.update(extra)
        return self.client.put(
            f'/api/v1/workflow-runs/{self.run.id}/evidence-manifest',
            json=body, headers={'Idempotency-Key': key})

    def _analysis(self, classification, version, key, **extra):
        body = {
            'classification': classification,
            'expected_version': version,
            'summary': extra.pop('summary', {'note': classification}),
        }
        body.update(extra)
        return self.client.post(
            f'/api/v1/workflow-runs/{self.run.id}/post-run-analysis',
            json=body, headers={'Idempotency-Key': key})

    def test_at10_incomplete_evidence_rejects_no_risk_and_accepts_incomplete(self):
        created = self._manifest({
            'case_result': 'complete',
            'ui_snapshot': 'complete',
            'console': 'missing',
            'screenshots': 'partial',
            'logcat': 'not_applicable',
        })

        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        self.assertEqual(created.get_json()['completeness_status'], 'incomplete')
        self.assertEqual(
            {item['type'] for item in created.get_json()['missing_required']},
            {'console', 'screenshots'})

        rejected = self._analysis('NO_RISK_FOUND', 1, 'no-risk-invalid')
        self.assertEqual(rejected.status_code, 409, rejected.get_data(as_text=True))
        self.assertEqual(
            rejected.get_json()['code'], 'EVIDENCE_INCOMPLETE_FOR_NO_RISK')
        self.assertEqual(WorkflowEvidenceManifest.query.first().classification, None)

        incomplete = self._analysis(
            'ANALYSIS_INCOMPLETE', 1, 'analysis-incomplete')
        self.assertEqual(incomplete.status_code, 200)
        self.assertEqual(incomplete.get_json()['classification'],
                         'ANALYSIS_INCOMPLETE')
        self.assertEqual(incomplete.get_json()['revision'], 2)

    def test_complete_evidence_allows_idempotent_no_risk_and_blocks_regression(self):
        complete_coverage = {
            'case_result': 'complete',
            'ui_snapshot': 'complete',
            'console': 'complete',
            'screenshots': 'complete',
        }
        created = self._manifest(complete_coverage)
        result = self._analysis('NO_RISK_FOUND', 1, 'no-risk-complete')
        replay = self._analysis('NO_RISK_FOUND', 1, 'no-risk-complete')

        self.assertEqual(created.get_json()['completeness_status'], 'complete')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(result.get_json(), replay.get_json())
        self.assertEqual(result.get_json()['revision'], 2)

        regression = self._manifest(
            dict(complete_coverage, console='missing'),
            key='manifest-regression', expected_version=2)
        self.assertEqual(regression.status_code, 409)
        self.assertEqual(
            regression.get_json()['code'],
            'EVIDENCE_REGRESSION_REQUIRES_REANALYSIS')
        self.assertEqual(WorkflowEvidenceManifest.query.first().revision, 2)

    def test_manifest_rejects_inline_artifact_payloads(self):
        response = self._manifest({
            'case_result': 'complete',
            'ui_snapshot': 'complete',
            'console': 'complete',
            'screenshots': 'complete',
        }, artifacts=[{
            'type': 'screenshot',
            'uri': 'hub-artifact://runs/1/result.png',
            'base64': 'large-inline-payload',
        }])

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.get_json()['code'], 'EVIDENCE_MANIFEST_VALIDATION_FAILED')
        self.assertEqual(WorkflowEvidenceManifest.query.count(), 0)

    def test_explicit_manifest_write_matches_worker_ingestion_when_flag_is_off(self):
        self.app.config['SHIFT_LEFT_ENABLED'] = False
        response = self._manifest({
            'case_result': 'complete',
            'ui_snapshot': 'complete',
            'console': 'complete',
            'screenshots': 'complete',
        }, key='manifest-compat-flag-off')

        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        self.assertEqual(response.get_json()['workflow_run_id'], self.run.id)
        self.assertEqual(WorkflowEvidenceManifest.query.count(), 1)

    def test_manifest_create_without_expected_version_replays_idempotently(self):
        body = {
            'coverage': {
                'case_result': 'complete',
                'ui_snapshot': 'complete',
                'console': 'complete',
                'screenshots': 'complete',
            },
            'artifacts': [],
        }
        headers = {'Idempotency-Key': 'manifest-create-replay-no-version'}

        first = self.client.put(
            f'/api/v1/workflow-runs/{self.run.id}/evidence-manifest',
            json=body, headers=headers)
        replay = self.client.put(
            f'/api/v1/workflow-runs/{self.run.id}/evidence-manifest',
            json=body, headers=headers)
        changed = self.client.put(
            f'/api/v1/workflow-runs/{self.run.id}/evidence-manifest',
            json=dict(body, coverage=dict(body['coverage'], console='missing')),
            headers=headers)

        self.assertEqual(first.status_code, 201, first.get_data(as_text=True))
        self.assertEqual(replay.status_code, 201, replay.get_data(as_text=True))
        self.assertEqual(first.get_json(), replay.get_json())
        self.assertEqual(changed.status_code, 409)
        self.assertEqual(changed.get_json()['code'], 'IDEMPOTENCY_KEY_REUSED')
        self.assertEqual(WorkflowEvidenceManifest.query.count(), 1)
        self.assertEqual(WorkflowEvidenceManifest.query.first().revision, 1)

    def test_custom_required_evidence_cannot_bypass_at10_core_or_relation_outage(self):
        EntityRelation.__table__.drop(db.engine)
        response = self._manifest({
            'case_result': 'complete',
            'console': 'not_applicable',
        }, required_evidence=['case_result'])

        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        payload = response.get_json()
        self.assertEqual(payload['completeness_status'], 'incomplete')
        self.assertTrue(
            {'ui_snapshot', 'console', 'screenshots'}.issubset(
                {item['type'] for item in payload['missing_required']}))
        console = next(
            item for item in payload['missing_required']
            if item['type'] == 'console')
        self.assertEqual(console['status'], 'not_applicable')
        self.assertEqual(WorkflowEvidenceManifest.query.count(), 1)

    def test_finding_relations_and_report_context_reuse_existing_data_plane(self):
        report = TestReport(
            title='Flow12 post-run report', report_type='workflow',
            project_id=self.project.id, content='<main>evidence report</main>',
            format='html', status='published', submitter_type='user',
            submitter_user_id=self.admin.id, submitter_name=self.admin.username,
        )
        db.session.add(report)
        db.session.flush()
        analysis = ShiftLeftAnalysisRun(
            project_id=self.project.id,
            workflow_run_id=self.run.id,
            report_id=report.id,
            baseline_fingerprint='f' * 64,
            status='completed_with_findings',
            baseline_json={'output_schema_version': 'v1'},
            created_by=self.admin.username,
            updated_by=self.admin.username,
        )
        db.session.add(analysis)
        db.session.flush()
        finding = ShiftLeftFinding(
            project_id=self.project.id,
            analysis_run_id=analysis.id,
            finding_key='post-run:reward-state-mismatch',
            title='Reward state mismatch after claim',
            severity='high', confidence=0.95,
            evidence_level='runtime_confirmed',
            source_type='post_run_analysis',
            created_by=self.admin.username,
            updated_by=self.admin.username,
        )
        db.session.add(finding)
        db.session.commit()

        manifest = self._manifest({
            'case_result': 'complete', 'ui_snapshot': 'complete',
            'console': 'complete', 'screenshots': 'complete',
        }, analysis_run_id=analysis.id)
        classified = self._analysis(
            'PRODUCT_BUG_CANDIDATE', 1, 'finding-classification',
            finding_ids=[finding.id])
        context = self.client.get(
            f'/api/v1/test-reports/{report.id}/analysis-context')
        report_findings = self.client.get(
            f'/api/v1/test-reports/{report.id}/findings')

        self.assertEqual(manifest.status_code, 201, manifest.get_data(as_text=True))
        self.assertEqual(classified.status_code, 200, classified.get_data(as_text=True))
        relation_types = {
            row.relation_type for row in EntityRelation.query.filter_by(
                project_id=self.project.id).all()}
        self.assertTrue({'has_evidence', 'produced', 'supports'}.issubset(
            relation_types))
        self.assertEqual(context.status_code, 200)
        self.assertEqual(len(context.get_json()['evidence_manifests']), 1)
        self.assertEqual(
            context.get_json()['evidence_manifests'][0]['finding_ids'],
            [finding.id])
        self.assertEqual(report_findings.status_code, 200)
        self.assertEqual(report_findings.get_json()['total'], 1)
        self.assertEqual(
            report_findings.get_json()['items'][0]['id'], finding.id)


if __name__ == '__main__':
    unittest.main()
