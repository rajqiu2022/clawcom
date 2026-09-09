"""Regression cases from the 2026-09-09 cross-project audit."""
import hashlib
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from tests import test_workflow_worker_contract_api as fixture
from app import db
from app.api import workflows as api
from app.models import WorkflowArtifact, TestReport, WorkflowEvidenceManifest
from app.services.workflows import merge_workflow_run_outcomes, normalize_step
from app.services.workflow_evidence_scope import scoped_evidence_missing


class WorkflowReliabilityTest(unittest.TestCase):
    setUp = fixture.WorkflowWorkerContractApiTest.setUp
    tearDown = fixture.WorkflowWorkerContractApiTest.tearDown
    _seed = fixture.WorkflowWorkerContractApiTest._seed
    _headers = fixture.WorkflowWorkerContractApiTest._headers
    _post = fixture.WorkflowWorkerContractApiTest._post
    def _report(self, kind='engineering_analysis', project_id=None):
        row = TestReport(title='Publisher', report_type=kind,
            project_id=project_id or self.run.project_id, content='report body',
            status='published', source_ref_type='engineering_batch', source_ref_id=3,
            submitter_type='openclaw', submitter_claw_id=self.claw.id)
        db.session.add(row)
        db.session.commit()
        return row

    def _manifest(self, report):
        return {'schema_version': 1, 'report_id': report.id, 'run_id': self.run.id,
            'step_id': self.agent_step.step_id, 'attempt_no': self.agent_step.attempt_no or 1,
            'report_type': report.report_type, 'source_ref_type': report.source_ref_type,
            'source_ref_id': report.source_ref_id,
            'content_sha256': hashlib.sha256(report.content.encode()).hexdigest()}

    def test_nested_incomplete_cannot_be_hidden_by_other_case(self):
        result = merge_workflow_run_outcomes({'evidence': 'COMPLETE'}, {
            'business_conclusion': 'COMPLETED_WITH_BUGS',
            'outputs': {'cases': [{'case_id': 'Endless', 'evidence_outcome': 'ANALYSIS_INCOMPLETE'}]},
        })
        self.assertEqual(result['evidence'], 'ANALYSIS_INCOMPLETE')
        self.assertEqual(result['business'], 'FAILED')
        self.assertEqual(merge_workflow_run_outcomes(result, {
            'evidence_outcome': 'COMPLETE'})['evidence'], 'ANALYSIS_INCOMPLETE')

    def test_scoped_screenshot_must_match_case_and_stage(self):
        requirement = {'case_id': 'Endless', 'stage': 'failure', 'channel': 'screenshots'}
        artifact = {'type': 'screenshots', 'uri': 'https://example.test/s.png',
                    'metadata': {'case_id': 'Other', 'stage': 'failure'}}
        self.assertEqual(len(scoped_evidence_missing([requirement], [artifact])), 1)
        artifact['metadata']['case_id'] = 'Endless'
        self.assertEqual(scoped_evidence_missing([requirement], [artifact]), [])
        artifact['metadata'].update(status='not_applicable', reason='headless')
        self.assertEqual(len(scoped_evidence_missing([requirement], [artifact])), 1)
        self.assertEqual(scoped_evidence_missing([dict(requirement, allow_not_applicable=True)], [artifact]), [])

    def test_manifest_keeps_case_gap_and_confirmed_bug(self):
        result = {'status': 'passed', 'business_conclusion': 'COMPLETED_WITH_BUGS',
            'outputs': {'cases': [{'case_id': 'Endless', 'evidence_outcome': 'ANALYSIS_INCOMPLETE'}]},
            'evidence_manifest': {'coverage': dict.fromkeys(
                ('case_result', 'ui_snapshot', 'console', 'screenshots'), 'complete'), 'artifacts': []}}
        response = self._post('agent_step', 'result', result)
        self.assertEqual(response.status_code, 200, response.get_json())
        manifest = WorkflowEvidenceManifest.query.one()
        self.assertEqual(manifest.completeness_status, 'incomplete')
        self.assertTrue(any('cases[0]' in item.get('path', '') for item in manifest.missing_required_json))
        self.assertEqual(self.run.outcomes_json['business'], 'FAILED')
        self.assertEqual(self.run.outcomes_json['evidence'], 'ANALYSIS_INCOMPLETE')
        # A corrected current result closes the gap; an old Run-level sticky
        # value must not keep it incomplete forever.
        self.agent_step.status = 'running'
        self.run.status = 'running'
        db.session.commit()
        result['outputs']['cases'][0]['evidence_outcome'] = 'COMPLETE'
        response = self._post('agent_step', 'result', result)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(self.run.outcomes_json['evidence'], 'COMPLETE')

    def test_scoped_requirement_is_enforced_during_ingestion(self):
        self.agent_step.step_config_json = {'evidence_requirements': [
            {'case_id': 'Endless', 'stage': 'failure', 'channel': 'screenshots'}]}
        db.session.commit()
        response = self._post('agent_step', 'result', {'status': 'passed',
            'evidence_manifest': {'coverage': dict.fromkeys(
                ('case_result', 'ui_snapshot', 'console', 'screenshots'), 'complete'),
                'artifacts': [{'type': 'screenshots', 'uri': 'https://example.test/other.png',
                               'case_id': 'Other', 'metadata': {'stage': 'failure'}}]}})
        self.assertEqual(response.status_code, 200, response.get_json())
        missing = WorkflowEvidenceManifest.query.one().missing_required_json
        self.assertTrue(any(item.get('case_id') == 'Endless' for item in missing))

    def test_publisher_manifest_binds_standard_artifact(self):
        report = self._report()
        response = self._post('agent_step', 'result', {'status': 'passed',
            'test_report_id': report.id, 'workflow_report_manifest': self._manifest(report)})
        self.assertEqual(response.status_code, 200, response.get_json())
        artifact = WorkflowArtifact.query.filter_by(artifact_type='workflow_report').one()
        self.assertEqual(artifact.metadata_json['publisher_manifest']['report_id'], report.id)

    def test_publisher_changed_content_rejects_stale_manifest(self):
        report = self._report()
        manifest = self._manifest(report)
        report.content = 'changed'
        db.session.commit()
        response = self._post('agent_step', 'result', {'status': 'passed',
            'test_report_id': report.id, 'workflow_report_manifest': manifest})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(WorkflowArtifact.query.count(), 0)

    def test_publisher_wrong_attempt_rejected(self):
        report = self._report()
        manifest = dict(self._manifest(report), attempt_no=50)
        self.assertEqual(self._post('agent_step', 'result', {'status': 'passed',
            'test_report_id': report.id, 'workflow_report_manifest': manifest}).status_code, 400)

    def test_unbound_published_claim_is_not_report_completion(self):
        self.agent_step.outputs_json = {'report_outcome': 'PUBLISHED', 'hub_report_id': 12345}
        api._sync_run_outcomes(self.run)
        self.assertEqual(self.run.outcomes_json['report'], 'FAILED')

    def test_cross_project_legacy_report_rejected(self):
        project = fixture.Project(name='Other project')
        db.session.add(project)
        db.session.flush()
        report = self._report('workflow', project.id)
        self.assertEqual(self._post('agent_step', 'result', {
            'status': 'passed', 'test_report_id': report.id}).status_code, 400)

    def test_outbox_normalization_keeps_artifact_gate(self):
        step = normalize_step({'id': 'notify', 'type': 'agent_task',
            'notification_delivery_mode': 'outbox', 'gates': [
                {'expression': 'metrics.wecom_sent == true'},
                {'expression': 'metrics.wecom_errcode == 0'},
                {'expression': 'metrics.artifact_valid == true'},
            ]}, 0)
        self.assertEqual(len(step['gates']), 1)
        self.assertIn('artifact_valid', step['gates'][0]['expression'])
        self.assertEqual(len(step['notification_delivery_gates']), 2)

    def test_outbox_pending_is_not_sent_and_updates_after_run_terminal(self):
        report = self._report()
        self.agent_step.step_config_json = {'notification_delivery_mode': 'outbox'}
        db.session.commit()
        delivery = {'claw_id': self.claw.id, 'run_id': self.run.id,
            'step_id': 'agent_step', 'attempt_no': self.agent_step.attempt_no or 1,
            'notification_id': 'outbox-1', 'message_sha256': 'a' * 64,
            'report_id': report.id, 'state': 'pending'}
        response = self._post('agent_step', 'result', {'status': 'passed',
            'test_report_id': report.id, 'workflow_report_manifest': self._manifest(report),
            'notification_delivery': delivery})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(self.run.outcomes_json['notification'], 'PENDING')
        self.run.status = 'succeeded'
        db.session.commit()
        response = self._post('agent_step', 'notification-delivery', dict(delivery, state='failed'))
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(self.run.status, 'succeeded')
        self.assertEqual(self.run.outcomes_json['notification'], 'FAILED')
        self.assertEqual(self._post('agent_step', 'notification-delivery',
            dict(delivery, state='sent')).status_code, 400)
        self.assertEqual(self._post('agent_step', 'notification-delivery',
            dict(delivery, notification_id='different')).status_code, 400)
        receipt = {'transport': 'wecom', 'errcode': 0, 'notification_id': 'outbox-1',
                   'message_sha256': 'a' * 64, 'received_at': '2026-09-09T10:00:00+08:00'}
        response = self._post('agent_step', 'notification-delivery',
                             dict(delivery, state='sent', delivery_receipt=receipt))
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()['receipt_origin'], 'authenticated_worker')
        self.assertEqual(self.run.outcomes_json['notification'], 'SENT')
        self._post('agent_step', 'notification-delivery',
                   dict(delivery, state='failed'))
        self.assertEqual(self.run.outcomes_json['notification'], 'SENT')

    def test_outbox_backfills_report_link_from_accepted_producer_result(self):
        report = self._report()
        producer_manifest = dict(
            self._manifest(report),
            step_id=self.worker_step.step_id,
            attempt_no=self.worker_step.attempt_no or 1,
        )
        self.worker_step.status = 'passed'
        self.worker_step.outputs_json = {
            'hub_report_id': report.id,
            'workflow_report_manifest': producer_manifest,
        }
        self.agent_step.step_config_json = {
            'notification_delivery_mode': 'outbox'}
        db.session.commit()
        delivery = {
            'claw_id': self.claw.id,
            'run_id': self.run.id,
            'step_id': self.agent_step.step_id,
            'attempt_no': self.agent_step.attempt_no or 1,
            'notification_id': 'outbox-legacy-report-link',
            'message_sha256': 'b' * 64,
            'report_id': report.id,
            'state': 'pending',
        }

        response = self._post('agent_step', 'result', {
            'status': 'passed', 'notification_delivery': delivery})

        self.assertEqual(200, response.status_code, response.get_json())
        artifact = WorkflowArtifact.query.filter_by(
            run_id=self.run.id,
            step_id=self.worker_step.step_id,
            artifact_type='workflow_report',
            test_report_id=report.id,
        ).one()
        self.assertEqual(
            producer_manifest['content_sha256'],
            artifact.metadata_json['publisher_manifest']['content_sha256'])

    def test_silent_step_never_blindly_replays_retry_budget(self):
        step = self.agent_step
        step.attempt_no = 1
        step.step_config_json = {'retry_max': 5}
        step.started_at = datetime.now() - timedelta(hours=2)
        now = datetime.now()
        with patch.object(api, '_dispatch_step_message') as dispatch:
            self.assertFalse(api._reconcile_silent_step(step, now))
            self.assertEqual(step.progress_json['hub_reconciliation']['state'], 'recovering')
            self.assertTrue(api._reconcile_silent_step(step, now + timedelta(minutes=31)))
            dispatch.assert_not_called()
        self.assertEqual(step.attempt_no, 1)
        self.assertEqual(step.blocker_json['code'], 'HUMAN_GATE')
        self.assertTrue(step.blocker_json['requires_reconciliation'])

    def test_live_lease_protects_slow_provider(self):
        self.agent_step.claim_expires_at = datetime.now() + timedelta(minutes=5)
        self.assertFalse(api._reconcile_silent_step(self.agent_step, datetime.now()))
        self.assertEqual(self.agent_step.status, 'running')

    def test_new_progress_resets_reconciliation_budget(self):
        now = datetime.now()
        self.agent_step.started_at = now - timedelta(hours=1)
        api._reconcile_silent_step(self.agent_step, now)
        self.agent_step.progress_at = now + timedelta(minutes=10)
        self.assertFalse(api._reconcile_silent_step(self.agent_step, now + timedelta(minutes=31)))
        self.assertEqual(self.agent_step.progress_json['hub_reconciliation']['checks'], 1)

    def test_progress_cannot_forge_hub_reconciliation_clock(self):
        now = datetime.now()
        api._apply_step_progress(self.agent_step, {'progress': {
            'hub_reconciliation': {'started_at': '2000-01-01', 'checks': 999},
            'phase_detail': 'checking',
        }}, 'worker', now)
        self.assertNotIn('hub_reconciliation', self.agent_step.progress_json)

    def test_unresolved_execution_blocks_retry_resume_and_restart(self):
        self.run.status = 'blocked'
        self.agent_step.status = 'blocked'
        self.agent_step.blocker_json = {'requires_reconciliation': True}
        db.session.commit()
        for path in ('steps/agent_step/retry', 'resume', 'restart'):
            response = self.client.post('/api/v1/workflow-runs/%s/%s' % (self.run.id, path),
                headers=dict(self._headers(), **{'Idempotency-Key': 'reconcile-' + path}), json={})
            self.assertEqual(response.status_code, 409, response.get_json())
            self.assertEqual(response.get_json()['code'], 'EXECUTION_RECONCILIATION_REQUIRED')

    def test_retry_rejects_running_step_without_creating_another_attempt(self):
        self.run.status = 'running'
        self.agent_step.status = 'running'
        db.session.commit()
        response = self.client.post(
            '/api/v1/workflow-runs/%s/steps/agent_step/retry' % self.run.id,
            headers=self._headers(), json={})
        self.assertEqual(response.status_code, 409, response.get_json())
        self.assertEqual(response.get_json()['code'], 'WORKFLOW_STEP_NOT_RETRYABLE')
        self.assertEqual(self.agent_step.status, 'running')

    def test_retry_expires_prior_agent_tasks_before_redispatch(self):
        self.run.status = 'blocked'
        self.agent_step.status = 'blocked'
        task = fixture.AgentTask(
            claw_id=self.claw.id,
            task_id='workflow_%s_agent_step_1_%s' % (self.run.id, self.claw.id),
            task_type='workflow_agent_task',
            status='running')
        db.session.add(task)
        db.session.commit()
        response = self.client.post(
            '/api/v1/workflow-runs/%s/steps/agent_step/retry' % self.run.id,
            headers=self._headers(), json={})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(task.status, 'failed')
        self.assertEqual(task.error, 'workflow_step_retried')

    def test_resolution_requires_authority_and_receipt(self):
        self.agent_step.blocker_json = {'requires_reconciliation': True}
        db.session.commit()
        payload = {'attempt_no': self.agent_step.attempt_no or 1, 'execution_stopped': True,
                   'side_effects_reconciled': True, 'receipt_ref': 'artifact://reconciliation/1'}
        self.assertEqual(self._post('agent_step', 'execution-reconciliation',
            payload, self.other_token).status_code, 403)
        self.assertEqual(self._post('agent_step', 'execution-reconciliation', {}).status_code, 400)
        response = self._post('agent_step', 'execution-reconciliation', payload)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertFalse(self.agent_step.blocker_json['requires_reconciliation'])
