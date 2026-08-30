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
    EntityRelation,
    Project,
    ShiftLeftAnalysisRun,
    ShiftLeftFinding,
    ShiftLeftFindingEvent,
    ShiftLeftFindingFeedback,
    User,
    WorkflowDefinition,
    WorkflowRun,
)


class AnalysisRuleLearningApiTest(unittest.TestCase):
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
        self.admin = User(username='rule_owner', role='super_admin')
        self.admin.set_password('secret')
        db.session.add_all([self.project, self.admin])
        db.session.flush()
        definition = WorkflowDefinition(
            workflow_key='analysis-rule-replay', name='Analysis rule replay',
            project_id=self.project.id,
            definition_json={'key': 'analysis-rule-replay', 'steps': []},
            owner_type='user', owner_id=self.admin.id,
            executor_acl_json={'user_ids': [self.admin.id], 'claw_ids': []},
            visibility_scope='project')
        db.session.add(definition)
        db.session.flush()
        self.workflow_run = WorkflowRun(
            definition_id=definition.id, run_name='Historical replay',
            status='succeeded', project_id=self.project.id)
        db.session.add(self.workflow_run)
        db.session.flush()
        self.analysis_run = ShiftLeftAnalysisRun(
            project_id=self.project.id,
            workflow_run_id=self.workflow_run.id,
            baseline_fingerprint='b' * 64,
            status='completed_with_findings',
            baseline_json={'output_schema_version': 'v1'},
            created_by=self.admin.username,
            updated_by=self.admin.username)
        db.session.add(self.analysis_run)
        db.session.flush()
        self.finding = ShiftLeftFinding(
            project_id=self.project.id,
            analysis_run_id=self.analysis_run.id,
            finding_key='reward-state-mismatch',
            title='Reward state mismatch', module='lobby.reward',
            severity='high', confidence=0.95,
            evidence_level='runtime_confirmed',
            source_type='post_run_analysis', status='confirmed',
            created_by=self.admin.username,
            created_by_actor_key=f'user:{self.admin.id}',
            updated_by=self.admin.username, revision=3)
        db.session.add(self.finding)
        db.session.commit()
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['user_id'] = self.admin.id

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _feedback(self, key='feedback-1', **overrides):
        body = {
            'label': 'true_positive',
            'from_revision': self.finding.revision,
            'note': 'Runtime and business state both confirm the mismatch',
            'evidence_refs': ['hub-artifact://runs/1/console.log'],
        }
        body.update(overrides)
        return self.client.post(
            f'/api/v1/shift-left/findings/{self.finding.id}/feedback',
            json=body, headers={'Idempotency-Key': key})

    def _create_rule(self, key='rule-create-1'):
        return self.client.post('/api/v1/analysis-rules', json={
            'project_id': self.project.id,
            'rule_key': 'reward-state-transition-consistency',
            'name': 'Reward state transition consistency',
            'description': 'Detect claimed/balance state mismatches',
            'definition': {
                'kind': 'event_state_consistency',
                'event': 'reward_claimed',
                'assertions': ['claimed_once', 'balance_delta_matches'],
            },
            'thresholds': {
                'min_sample_count': 20,
                'min_precision': 0.8,
                'min_recall': 0.8,
                'max_false_positive_rate': 0.2,
                'max_false_negative_rate': 0.2,
            },
            'source_finding_ids': [self.finding.id],
            'change_summary': 'Initial rule learned from confirmed finding',
        }, headers={'Idempotency-Key': key})

    def _replay(self, version_id, metrics, key):
        return self.client.post(
            f'/api/v1/analysis-rule-versions/{version_id}/replays', json={
                'workflow_run_id': self.workflow_run.id,
                'dataset_snapshot_ref': 'hub-artifact://replays/racinggo-20260812.json',
                'dataset_hash': 'd' * 64,
                'metrics': metrics,
                'impact': {
                    'changed_modules': ['lobby.reward'],
                    'new_hits': 2,
                    'removed_hits': 1,
                    'finding_ids': [self.finding.id],
                },
                'artifact_refs': ['hub-artifact://replays/racinggo-20260812.html'],
            }, headers={'Idempotency-Key': key})

    def test_feedback_is_normalized_idempotent_and_visible_in_timeline(self):
        first = self._feedback()
        replay = self._feedback()
        stale = self._feedback(
            key='feedback-stale', from_revision=2,
            label='false_positive')
        invalid = self._feedback(key='feedback-invalid', label='maybe')

        self.assertEqual(first.status_code, 201, first.get_data(as_text=True))
        self.assertEqual(replay.status_code, 201)
        self.assertEqual(first.get_json(), replay.get_json())
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(ShiftLeftFindingFeedback.query.count(), 1)
        self.assertEqual(ShiftLeftFindingEvent.query.filter_by(
            finding_id=self.finding.id, event_type='feedback').count(), 1)
        listed = self.client.get(
            f'/api/v1/shift-left/findings/{self.finding.id}/feedback')
        self.assertEqual(listed.get_json()['counts']['true_positive'], 1)
        detail = self.client.get(
            f'/api/v1/shift-left/findings/{self.finding.id}')
        self.assertEqual(detail.get_json()['feedback'][0]['label'], 'true_positive')

    def test_rule_cannot_activate_before_passing_replay(self):
        created = self._create_rule()
        replay_create = self._create_rule()
        version = created.get_json()['latest_version_detail']
        blocked = self.client.post(
            f'/api/v1/analysis-rule-versions/{version["id"]}/activate',
            json={'expected_version': created.get_json()['version']},
            headers={'Idempotency-Key': 'activate-before-replay'})

        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        self.assertEqual(replay_create.status_code, 201)
        self.assertEqual(created.get_json(), replay_create.get_json())
        self.assertEqual(version['status'], 'draft')
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.get_json()['code'],
                         'ANALYSIS_RULE_REPLAY_GATE_REQUIRED')
        self.assertIsNone(db.session.get(AnalysisRule, created.get_json()['id']).active_version_id)
        relation = EntityRelation.query.filter_by(
            from_type='finding', from_id=str(self.finding.id),
            relation_type='learned_into').first()
        self.assertIsNotNone(relation)

    def test_failed_then_passing_replay_enables_activation(self):
        created = self._create_rule().get_json()
        version_id = created['latest_version_detail']['id']
        failed = self._replay(version_id, {
            'sample_count': 10,
            'hit_count': 5,
            'true_positive_count': 2,
            'false_positive_count': 3,
            'false_negative_count': 2,
        }, 'replay-failed')
        passed = self._replay(version_id, {
            'sample_count': 100,
            'hit_count': 10,
            'true_positive_count': 9,
            'false_positive_count': 1,
            'false_negative_count': 1,
        }, 'replay-passed')
        replay_passed = self._replay(version_id, {
            'sample_count': 100,
            'hit_count': 10,
            'true_positive_count': 9,
            'false_positive_count': 1,
            'false_negative_count': 1,
        }, 'replay-passed')

        self.assertEqual(failed.status_code, 201, failed.get_data(as_text=True))
        self.assertFalse(failed.get_json()['gate_passed'])
        self.assertTrue(failed.get_json()['gate_reasons'])
        self.assertEqual(failed.get_json()['rule_version_status'], 'draft')
        self.assertTrue(passed.get_json()['gate_passed'])
        self.assertEqual(passed.get_json()['rule_version_status'], 'replay_passed')
        self.assertEqual(passed.get_json(), replay_passed.get_json())
        self.assertEqual(AnalysisRuleReplay.query.count(), 2)

        activated = self.client.post(
            f'/api/v1/analysis-rule-versions/{version_id}/activate',
            json={'expected_version': created['version']},
            headers={'Idempotency-Key': 'activate-after-replay'})
        activation_replay = self.client.post(
            f'/api/v1/analysis-rule-versions/{version_id}/activate',
            json={'expected_version': created['version']},
            headers={'Idempotency-Key': 'activate-after-replay'})
        self.assertEqual(activated.status_code, 200, activated.get_data(as_text=True))
        self.assertEqual(activated.get_json()['status'], 'active')
        self.assertEqual(activated.get_json(), activation_replay.get_json())
        self.assertEqual(activated.get_json()['rule']['status'], 'active')

    def test_new_version_does_not_replace_active_until_its_own_replay_passes(self):
        created = self._create_rule().get_json()
        v1_id = created['latest_version_detail']['id']
        self._replay(v1_id, {
            'sample_count': 100, 'hit_count': 10,
            'true_positive_count': 9, 'false_positive_count': 1,
            'false_negative_count': 1,
        }, 'v1-pass')
        active_v1 = self.client.post(
            f'/api/v1/analysis-rule-versions/{v1_id}/activate',
            json={'expected_version': 1},
            headers={'Idempotency-Key': 'v1-activate'}).get_json()
        v2 = self.client.post(
            f'/api/v1/analysis-rules/{created["id"]}/versions', json={
                'expected_version': active_v1['rule']['version'],
                'based_on_version_id': v1_id,
                'definition': {
                    'kind': 'event_state_consistency',
                    'event': 'reward_claimed',
                    'assertions': [
                        'claimed_once', 'balance_delta_matches',
                        'reward_inventory_matches'],
                },
                'source_finding_ids': [self.finding.id],
                'change_summary': 'Add reward inventory assertion',
            }, headers={'Idempotency-Key': 'v2-create'})
        v2_replay = self.client.post(
            f'/api/v1/analysis-rules/{created["id"]}/versions', json={
                'expected_version': active_v1['rule']['version'],
                'based_on_version_id': v1_id,
                'definition': {
                    'kind': 'event_state_consistency',
                    'event': 'reward_claimed',
                    'assertions': [
                        'claimed_once', 'balance_delta_matches',
                        'reward_inventory_matches'],
                },
                'source_finding_ids': [self.finding.id],
                'change_summary': 'Add reward inventory assertion',
            }, headers={'Idempotency-Key': 'v2-create'})
        self.assertEqual(v2.status_code, 201, v2.get_data(as_text=True))
        self.assertEqual(v2.get_json(), v2_replay.get_json())
        rule_before = db.session.get(AnalysisRule, created['id'])
        self.assertEqual(rule_before.active_version_id, v1_id)
        self.assertEqual(db.session.get(AnalysisRuleVersion, v1_id).status, 'active')

        blocked = self.client.post(
            f'/api/v1/analysis-rule-versions/{v2.get_json()["id"]}/activate',
            json={'expected_version': v2.get_json()['rule_version']},
            headers={'Idempotency-Key': 'v2-activate-too-early'})
        self.assertEqual(blocked.status_code, 409)
        self._replay(v2.get_json()['id'], {
            'sample_count': 100, 'hit_count': 11,
            'true_positive_count': 10, 'false_positive_count': 1,
            'false_negative_count': 1,
        }, 'v2-pass')
        activated_v2 = self.client.post(
            f'/api/v1/analysis-rule-versions/{v2.get_json()["id"]}/activate',
            json={'expected_version': v2.get_json()['rule_version']},
            headers={'Idempotency-Key': 'v2-activate'})
        self.assertEqual(activated_v2.status_code, 200,
                         activated_v2.get_data(as_text=True))
        self.assertEqual(activated_v2.get_json()['retired_previous_version_id'],
                         v1_id)
        self.assertEqual(db.session.get(AnalysisRuleVersion, v1_id).status,
                         'retired')
        self.assertEqual(db.session.get(
            AnalysisRule, created['id']).active_version_id, v2.get_json()['id'])

        retired = self.client.post(
            f'/api/v1/analysis-rule-versions/{v2.get_json()["id"]}/retire',
            json={
                'expected_version': activated_v2.get_json()['rule']['version'],
                'reason': 'Superseded by platform-native assertion',
            }, headers={'Idempotency-Key': 'v2-retire'})
        self.assertEqual(retired.status_code, 200, retired.get_data(as_text=True))
        self.assertEqual(retired.get_json()['status'], 'retired')
        self.assertEqual(retired.get_json()['rule']['status'], 'retired')

    def test_replay_rejects_inconsistent_metrics_and_cross_project_run(self):
        created = self._create_rule().get_json()
        version_id = created['latest_version_detail']['id']
        inconsistent = self._replay(version_id, {
            'sample_count': 100,
            'hit_count': 10,
            'true_positive_count': 7,
            'false_positive_count': 1,
            'false_negative_count': 2,
        }, 'bad-metrics')
        self.assertEqual(inconsistent.status_code, 400)
        self.assertEqual(inconsistent.get_json()['code'],
                         'ANALYSIS_RULE_REPLAY_INVALID')

        other_project = Project(name='OtherGame')
        db.session.add(other_project)
        db.session.flush()
        other_definition = WorkflowDefinition(
            workflow_key='other-replay', name='Other replay',
            project_id=other_project.id,
            definition_json={'key': 'other-replay', 'steps': []},
            owner_type='user', owner_id=self.admin.id,
            executor_acl_json={'user_ids': [self.admin.id], 'claw_ids': []},
            visibility_scope='project')
        db.session.add(other_definition)
        db.session.flush()
        other_run = WorkflowRun(
            definition_id=other_definition.id, run_name='Other replay',
            status='succeeded', project_id=other_project.id)
        db.session.add(other_run)
        db.session.commit()
        foreign = self.client.post(
            f'/api/v1/analysis-rule-versions/{version_id}/replays', json={
                'workflow_run_id': other_run.id,
                'dataset_snapshot_ref': 'hub-artifact://replays/other.json',
                'dataset_hash': 'e' * 64,
                'metrics': {
                    'sample_count': 100, 'hit_count': 10,
                    'true_positive_count': 9, 'false_positive_count': 1,
                    'false_negative_count': 1,
                },
                'impact': {'changed_modules': ['other']},
            }, headers={'Idempotency-Key': 'foreign-run'})
        self.assertEqual(foreign.status_code, 400)
        self.assertEqual(foreign.get_json()['code'],
                         'ANALYSIS_RULE_REPLAY_RUN_MISMATCH')
        self.assertEqual(AnalysisRuleReplay.query.count(), 0)

    def test_rule_queries_are_project_scoped(self):
        created = self._create_rule().get_json()
        outsider = User(username='rule_outsider', role='user')
        outsider.set_password('secret')
        db.session.add(outsider)
        db.session.commit()
        with self.client.session_transaction() as session:
            session['user_id'] = outsider.id
        listed = self.client.get(
            f'/api/v1/analysis-rules?project_id={self.project.id}')
        detail = self.client.get(
            f'/api/v1/analysis-rules/{created["id"]}')
        self.assertIn(listed.status_code, {403, 404})
        self.assertIn(detail.status_code, {403, 404})


if __name__ == '__main__':
    unittest.main()
