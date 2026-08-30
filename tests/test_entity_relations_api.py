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
    AutomationCaseCandidate,
    EntityRelation as RelationModel,
    Project,
    TestCase as CaseModel,
    TestCaseLibrary as LibraryModel,
    User,
    WorkflowDefinition,
    WorkflowRun,
)


class EntityRelationsApiTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SECRET_KEY='test-secret',
            TESTING=True,
            SHIFT_LEFT_ENABLED=False,
        )
        db.init_app(self.app)
        self.app.register_blueprint(api_bp, url_prefix='/api/v1')
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.project = Project(name='RacingGO')
        self.admin = User(username='lineage_owner', role='super_admin')
        self.admin.set_password('secret')
        db.session.add_all([self.project, self.admin])
        db.session.flush()
        self.qualification_definition = WorkflowDefinition(
            workflow_key='qualification-lineage',
            name='Qualification lineage',
            project_id=self.project.id,
            definition_json={'key': 'qualification-lineage', 'steps': []},
            owner_type='user', owner_id=self.admin.id,
            executor_acl_json={'user_ids': [self.admin.id], 'claw_ids': []},
            visibility_scope='project',
        )
        self.flow_definition = WorkflowDefinition(
            workflow_key='flow12-lineage',
            name='Flow12 lineage',
            project_id=self.project.id,
            definition_json={
                'key': 'flow12-lineage',
                'name': 'Flow12 lineage',
                'steps': [{
                    'id': 'execute', 'name': 'Execute frozen cases',
                    'type': 'approval', 'approval_required': True,
                    'depends_on': [],
                }],
            },
            owner_type='user', owner_id=self.admin.id,
            executor_acl_json={'user_ids': [self.admin.id], 'claw_ids': []},
            visibility_scope='project',
        )
        self.library = LibraryModel(
            name='RacingGO production cases',
            project_name=self.project.name,
            owner=self.admin.username,
            mindmap={'id': 'root', 'title': 'Production'},
        )
        db.session.add_all([
            self.qualification_definition, self.flow_definition, self.library])
        db.session.flush()
        self.qualification_run = WorkflowRun(
            definition_id=self.qualification_definition.id,
            run_name='Candidate qualification',
            status='succeeded',
            project_id=self.project.id,
        )
        db.session.add(self.qualification_run)
        db.session.commit()
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['user_id'] = self.admin.id

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _batch(self, relations):
        return self.client.post(
            '/api/v1/entity-relations:batch-upsert',
            json={'project_id': self.project.id, 'relations': relations})

    def test_batch_upsert_is_natural_key_idempotent_and_updates_metadata(self):
        edge = {
            'from_type': 'commit', 'from_id': 'abc123',
            'relation_type': 'source_of',
            'to_type': 'automation_case_candidate', 'to_id': '1001',
            'metadata': {'branch': 'dev'},
        }
        first = self._batch([edge, edge])
        replay = self._batch([edge])
        changed = self._batch([dict(edge, metadata={'branch': 'release'})])

        self.assertEqual(first.status_code, 201)
        self.assertEqual(first.get_json()['created_count'], 1)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.get_json()['unchanged_count'], 1)
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(changed.get_json()['updated_count'], 1)
        self.assertEqual(RelationModel.query.count(), 1)
        self.assertEqual(RelationModel.query.first().metadata_json['branch'], 'release')

    def test_batch_validation_is_atomic(self):
        response = self._batch([
            {
                'from_type': 'commit', 'from_id': 'good',
                'relation_type': 'source_of',
                'to_type': 'automation_case_candidate', 'to_id': '1',
            },
            {
                'from_type': 'testcase', 'from_id': '7',
                'relation_type': 'invalid relation with spaces',
                'to_type': 'workflow_run', 'to_id': '8',
            },
        ])

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['code'], 'ENTITY_RELATION_VALIDATION_FAILED')
        self.assertEqual(RelationModel.query.count(), 0)

    def test_from_and_to_queries_are_bidirectional_and_paginated(self):
        self._batch([
            {
                'from_type': 'automation_case_candidate', 'from_id': '10',
                'relation_type': 'promoted_to',
                'to_type': 'testcase', 'to_id': '20',
            },
            {
                'from_type': 'testcase', 'from_id': '20',
                'relation_type': 'consumed_by',
                'to_type': 'workflow_run', 'to_id': '30',
            },
        ])

        upstream = self.client.get(
            '/api/v1/entity-relations?'
            f'project_id={self.project.id}&to_type=testcase&to_id=20')
        downstream = self.client.get(
            '/api/v1/entity-relations?'
            f'project_id={self.project.id}&from_type=testcase&from_id=20'
            '&page=1&page_size=1')

        self.assertEqual(upstream.status_code, 200)
        self.assertEqual(upstream.get_json()['items'][0]['from_id'], '10')
        self.assertEqual(downstream.status_code, 200)
        self.assertEqual(downstream.get_json()['total'], 1)
        self.assertEqual(downstream.get_json()['page_size'], 1)
        self.assertEqual(downstream.get_json()['items'][0]['to_id'], '30')

    def _create_qualified_candidate(self):
        created = self.client.post(
            '/api/v1/automation-case-candidates:upsert', json={
                'project_id': self.project.id,
                'title': 'Claim one sign-in reward with business assertion',
                'module_key': 'lobby.sign_in',
                'source_type': 'content_scan',
                'source_refs': [
                    {'type': 'commit', 'value': 'commit-racinggo-001'},
                    {'type': 'finding', 'value': 'PRE-FINDING-1'},
                ],
                'case_draft': {
                    'case_id': 'AUTO-LINEAGE-001',
                    'preconditions': ['account is eligible'],
                    'steps': ['claim exactly once'],
                    'expected_results': [
                        'claimed state and balance change exactly once'],
                    'automation': {'close_to_lobby': True},
                },
                'required_capabilities': ['claim_once', 'read_reward_state'],
                'state': 'DESIGNED',
                'production_library_id': self.library.id,
                'dedupe_key': 'lineage-candidate-1',
                'idempotency_key': 'lineage-candidate-1',
            })
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        candidate_id = created.get_json()['id']
        ready = self.client.patch(
            f'/api/v1/automation-case-candidates/{candidate_id}',
            json={'expected_version': 1, 'state': 'READY_FOR_CANARY'})
        self.assertEqual(ready.status_code, 200)
        qualified = self.client.post(
            f'/api/v1/automation-case-candidates/{candidate_id}/qualification-result',
            json={
                'expected_version': 2,
                'qualification_outcome': 'QUALIFIED',
                'qualification_run_id': self.qualification_run.id,
                'evidence': {'business_assertion': 'passed'},
                'idempotency_key': 'qualification-lineage-1',
            })
        self.assertEqual(
            qualified.status_code, 200, qualified.get_data(as_text=True))
        return candidate_id

    def test_at11_full_lineage_from_production_case_and_reverse(self):
        candidate_id = self._create_qualified_candidate()
        promoted = self.client.post(
            f'/api/v1/testcase-libraries/{self.library.id}/promotions',
            json={
                'candidate_ids': [candidate_id],
                'expected_library_revision': 0,
                'idempotency_key': 'lineage-promotion-1',
                'message': 'publish qualified lineage case',
                'source_run_ids': [self.qualification_run.id],
            })
        self.assertEqual(promoted.status_code, 201, promoted.get_data(as_text=True))
        case_id = promoted.get_json()['created_case_ids'][0]
        flow = self.client.post('/api/v1/workflow-runs', json={
            'workflow_definition_id': self.flow_definition.id,
            'idempotency_key': 'flow12-lineage-run-1',
            'start_vars': {'test_case_library_id': self.library.id},
        })
        self.assertEqual(flow.status_code, 201, flow.get_data(as_text=True))
        flow_run_id = flow.get_json()['id']
        downstream = self._batch([
            {
                'from_type': 'workflow_run', 'from_id': str(flow_run_id),
                'relation_type': 'produced',
                'to_type': 'finding', 'to_id': 'POST-FINDING-1',
                'metadata': {'classification': 'PRODUCT_BUG_CANDIDATE'},
            },
            {
                'from_type': 'finding', 'from_id': 'POST-FINDING-1',
                'relation_type': 'reported_as',
                'to_type': 'bug', 'to_id': 'TAPD-BUG-9001',
            },
            {
                'from_type': 'bug', 'from_id': 'TAPD-BUG-9001',
                'relation_type': 'learned_into',
                'to_type': 'learned_rule', 'to_id': 'RULE-77',
            },
        ])
        self.assertEqual(downstream.status_code, 201)

        trace = self.client.get(
            '/api/v1/entity-relations/trace?'
            f'project_id={self.project.id}&entity_type=testcase&entity_id={case_id}'
            '&direction=both&max_depth=5')

        self.assertEqual(trace.status_code, 200, trace.get_data(as_text=True))
        payload = trace.get_json()
        nodes = {
            (node['entity_type'], node['entity_id']) for node in payload['nodes']}
        expected = {
            ('testcase', str(case_id)),
            ('automation_case_candidate', str(candidate_id)),
            ('commit', 'commit-racinggo-001'),
            ('module', 'lobby.sign_in'),
            ('workflow_run', str(self.qualification_run.id)),
            ('workflow_run', str(flow_run_id)),
            ('finding', 'POST-FINDING-1'),
            ('bug', 'TAPD-BUG-9001'),
            ('learned_rule', 'RULE-77'),
        }
        self.assertTrue(expected.issubset(nodes), expected - nodes)
        self.assertFalse(payload['truncated'])

        reverse = self.client.get(
            '/api/v1/entity-relations/trace?'
            f'project_id={self.project.id}&entity_type=learned_rule&entity_id=RULE-77'
            '&direction=both&max_depth=5')
        reverse_nodes = {
            (node['entity_type'], node['entity_id'])
            for node in reverse.get_json()['nodes']}
        self.assertIn(('testcase', str(case_id)), reverse_nodes)
        self.assertEqual(CaseModel.query.get(case_id).case_id, 'AUTO-LINEAGE-001')
        db.session.refresh(db.session.get(AutomationCaseCandidate, candidate_id))

    def test_missing_relation_table_does_not_block_candidate_creation(self):
        RelationModel.__table__.drop(db.engine)

        response = self.client.post(
            '/api/v1/automation-case-candidates:upsert', json={
                'project_id': self.project.id,
                'title': 'Candidate survives lineage outage',
                'module_key': 'lobby.outage',
                'source_refs': [{'type': 'commit', 'value': 'outage-commit'}],
                'case_draft': {},
                'state': 'DISCOVERED',
                'dedupe_key': 'lineage-outage',
            })

        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        self.assertEqual(AutomationCaseCandidate.query.count(), 1)


if __name__ == '__main__':
    unittest.main()
