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
    AgentControlGate,
    AgentGoal,
    AgentTransitionReceipt,
    AgentTurn,
    GoalTodo,
    OpenClawInstance,
    Project,
    User,
    hash_token,
)
from app.services.agent_authorization import verify_envelope  # noqa: E402
from app.services.long_agent_control import sha256_ref  # noqa: E402


class LongAgentControlPlaneApiTest(unittest.TestCase):
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
        self._seed()
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['user_id'] = self.admin.id

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _seed(self):
        self.project = Project(name='Agent Pilot')
        self.admin = User(username='pilot_admin', role='super_admin')
        self.admin.set_password('secret')
        db.session.add_all([self.project, self.admin])
        db.session.flush()
        self.token_a = 'hub_tk_long_agent_a'
        self.token_b = 'hub_tk_long_agent_b'
        self.claw_a = OpenClawInstance(
            name='Worker A', safe_name='worker-a', claw_tag='worker-a',
            owner='pilot_admin', project_id=self.project.id,
            project_name=self.project.name,
            api_token_hash=hash_token(self.token_a),
        )
        self.claw_b = OpenClawInstance(
            name='Worker B', safe_name='worker-b', claw_tag='worker-b',
            owner='pilot_admin', project_id=self.project.id,
            project_name=self.project.name,
            api_token_hash=hash_token(self.token_b),
        )
        db.session.add_all([self.claw_a, self.claw_b])
        db.session.commit()

    def _headers(self, token=None, key=None):
        headers = {'Authorization': f'Bearer {token or self.token_a}'}
        if key:
            headers['Idempotency-Key'] = key
        return headers

    def _create_goal(self, **overrides):
        body = {
            'project_id': self.project.id,
            'title': 'Ship safely',
            'objective': 'Complete a durable multi-turn engineering task',
            'status': 'ACTIVE',
            'priority': 'P0',
            'compute_quota': 4,
        }
        body.update(overrides)
        response = self.client.post('/api/v1/agent-goals', json=body)
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.get_json()['goal']

    def _create_todo(self, goal_id, **overrides):
        body = {
            'title': 'Inspect repository',
            'description': 'Read state and propose the next bounded action',
            'action_kind': 'analyze',
            'priority': 'P1',
            'required_capabilities': ['repo.read'],
        }
        body.update(overrides)
        response = self.client.post(
            f'/api/v1/agent-goals/{goal_id}/todos', json=body)
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.get_json()['todo']

    def _dispatch(self, goal_id, key='dispatch-1', token=None, **overrides):
        body = {
            'worker_id': 'worker-process-a',
            'agent_id': 101,
            'capabilities': ['repo.read', 'repo.write'],
            'lease_seconds': 180,
        }
        body.update(overrides)
        return self.client.post(
            f'/api/v1/agent-goals/{goal_id}/turn-dispatches',
            json=body,
            headers=self._headers(token=token, key=key),
        )

    def test_should_run_is_read_only_and_grants_no_authority(self):
        goal = self._create_goal()
        todo = self._create_todo(goal['id'])

        response = self.client.post(
            f"/api/v1/agent-goals/{goal['id']}/should-run",
            json={
                'worker_id': 'worker-process-a',
                'agent_id': 101,
                'capabilities': ['repo.read'],
            },
            headers=self._headers(),
        )

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        payload = response.get_json()
        self.assertTrue(payload['should_run'])
        self.assertEqual(payload['candidate_todo']['id'], todo['id'])
        self.assertTrue(payload['read_only_preflight'])
        self.assertFalse(payload['authority_granted'])
        self.assertEqual(AgentTurn.query.count(), 0)
        stored = db.session.get(GoalTodo, todo['id'])
        self.assertEqual(stored.status, 'open')
        self.assertEqual(stored.fencing_token, 0)

    def test_work_item_gate_does_not_block_another_safe_todo(self):
        goal = self._create_goal()
        gated = self._create_todo(
            goal['id'], title='Apply patch', action_kind='write', priority='P0')
        safe = self._create_todo(
            goal['id'], title='Read documentation', action_kind='read',
            priority='P1')
        gate_response = self.client.post(
            '/api/v1/control-gates',
            json={
                'goal_id': goal['id'],
                'work_item_id': gated['id'],
                'gate_type': 'human_approval',
                'policy_level': 'hard',
                'blocking_scope': 'work_item',
                'question': 'May this patch be applied?',
                'reason_code': 'write_scope_expansion',
            },
            headers={'Idempotency-Key': 'gate-create-1'},
        )
        self.assertEqual(
            gate_response.status_code, 201, gate_response.get_data(as_text=True))

        response = self._dispatch(goal['id'])

        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        turn = response.get_json()['turn']
        self.assertEqual(turn['todo_id'], safe['id'])
        self.assertFalse(response.get_json()['claim_required'])
        self.assertEqual(db.session.get(GoalTodo, gated['id']).status, 'open')
        self.assertEqual(db.session.get(GoalTodo, safe['id']).status, 'open')

    def test_dispatch_claims_atomically_fences_and_is_idempotent(self):
        goal = self._create_goal()
        todo = self._create_todo(
            goal['id'], title='Apply patch', action_kind='write', priority='P0')

        first = self._dispatch(goal['id'], key='dispatch-write-1')
        replay = self._dispatch(goal['id'], key='dispatch-write-1')
        changed = self._dispatch(
            goal['id'], key='dispatch-write-1', lease_seconds=90)

        self.assertEqual(first.status_code, 201, first.get_data(as_text=True))
        self.assertEqual(replay.status_code, 200, replay.get_data(as_text=True))
        self.assertTrue(replay.get_json()['replayed'])
        self.assertEqual(
            first.get_json()['turn']['turn_id'],
            replay.get_json()['turn']['turn_id'])
        self.assertEqual(changed.status_code, 409)
        self.assertEqual(changed.get_json()['code'], 'IDEMPOTENCY_KEY_REUSED')
        stored = db.session.get(GoalTodo, todo['id'])
        self.assertEqual(stored.status, 'claimed')
        self.assertEqual(stored.claimed_by_worker_id, 'worker-process-a')
        self.assertEqual(stored.claimed_claw_id, self.claw_a.id)
        self.assertEqual(stored.fencing_token, 1)
        self.assertEqual(stored.version, 2)
        envelope = first.get_json()['turn']['envelope']
        self.assertEqual(envelope['authorization']['phase'], 'planning_only')
        self.assertTrue(
            envelope['authorization']['effect_authorization_required'])

        other = self._dispatch(
            goal['id'], key='dispatch-worker-b', token=self.token_b,
            worker_id='worker-process-b')
        self.assertEqual(other.status_code, 200, other.get_data(as_text=True))
        self.assertFalse(other.get_json()['dispatched'])

    def test_read_only_todo_still_has_one_active_turn(self):
        goal = self._create_goal()
        self._create_todo(goal['id'], action_kind='read')

        first = self._dispatch(goal['id'], key='read-turn-1')
        second = self._dispatch(goal['id'], key='read-turn-2')

        self.assertEqual(first.status_code, 201, first.get_data(as_text=True))
        self.assertEqual(second.status_code, 409, second.get_data(as_text=True))
        self.assertEqual(second.get_json()['code'], 'TURN_ALREADY_ACTIVE')

    def test_formal_mutations_have_no_worker_claim_bypass_and_turn_renews(self):
        forbidden_goal = self.client.post(
            '/api/v1/agent-goals',
            json={
                'project_id': self.project.id,
                'title': 'self-authorized',
                'objective': 'must not be created by Worker',
            }, headers=self._headers())
        self.assertEqual(forbidden_goal.status_code, 403)
        self.assertEqual(forbidden_goal.get_json()['code'], 'HUMAN_AUTH_REQUIRED')

        goal = self._create_goal()
        todo = self._create_todo(
            goal['id'], title='Write safely', action_kind='write', priority='P0')
        dispatched = self._dispatch(goal['id'], key='turn-lease-test')
        turn = dispatched.get_json()['turn']
        bypass = self.client.post(
            f"/api/v1/goal-todos/{todo['id']}/claim",
            json={'worker_id': 'worker-process-a'}, headers=self._headers())
        self.assertEqual(bypass.status_code, 409)
        self.assertEqual(bypass.get_json()['code'], 'ATOMIC_DISPATCH_REQUIRED')

        heartbeat = self.client.post(
            f"/api/v1/agent-turns/{turn['turn_id']}/heartbeat",
            json={
                'worker_id': 'worker-process-a',
                'fencing_token': turn['fencing_token'],
                'lease_seconds': 240,
            }, headers=self._headers())
        self.assertEqual(heartbeat.status_code, 200, heartbeat.get_data(as_text=True))
        self.assertTrue(heartbeat.get_json()['lease_extended'])

        released = self.client.post(
            f"/api/v1/agent-turns/{turn['turn_id']}/release",
            json={
                'worker_id': 'worker-process-a',
                'fencing_token': turn['fencing_token'],
            }, headers=self._headers())
        replay = self.client.post(
            f"/api/v1/agent-turns/{turn['turn_id']}/release",
            json={
                'worker_id': 'worker-process-a',
                'fencing_token': turn['fencing_token'],
            }, headers=self._headers())
        self.assertEqual(released.status_code, 200, released.get_data(as_text=True))
        self.assertEqual(released.get_json()['turn']['status'], 'released')
        self.assertTrue(replay.get_json()['replayed'])
        self.assertEqual(db.session.get(GoalTodo, todo['id']).status, 'open')

    def test_todo_claim_heartbeat_release_and_cas_endpoints(self):
        goal = self._create_goal(control_mode='legacy_passthrough')
        todo = self._create_todo(
            goal['id'], title='Edit shared source', action_kind='write')
        claim_body = {'worker_id': 'worker-process-a', 'lease_seconds': 60}
        first = self.client.post(
            f"/api/v1/goal-todos/{todo['id']}/claim",
            json=claim_body, headers=self._headers())
        renewed = self.client.post(
            f"/api/v1/goal-todos/{todo['id']}/claim",
            json=claim_body, headers=self._headers())

        self.assertEqual(first.status_code, 200, first.get_data(as_text=True))
        self.assertEqual(renewed.status_code, 200, renewed.get_data(as_text=True))
        token = first.get_json()['todo']['fencing_token']
        self.assertEqual(token, renewed.get_json()['todo']['fencing_token'])
        heartbeat = self.client.post(
            f"/api/v1/goal-todos/{todo['id']}/heartbeat",
            json={'worker_id': 'worker-process-a', 'fencing_token': token},
            headers=self._headers())
        stale_release = self.client.post(
            f"/api/v1/goal-todos/{todo['id']}/release",
            json={'worker_id': 'worker-process-a', 'fencing_token': token + 1},
            headers=self._headers())
        released = self.client.post(
            f"/api/v1/goal-todos/{todo['id']}/release",
            json={'worker_id': 'worker-process-a', 'fencing_token': token},
            headers=self._headers())
        reclaimed = self.client.post(
            f"/api/v1/goal-todos/{todo['id']}/claim",
            json=claim_body, headers=self._headers())

        self.assertEqual(heartbeat.status_code, 200)
        self.assertEqual(stale_release.status_code, 409)
        self.assertEqual(stale_release.get_json()['code'], 'FENCING_TOKEN_STALE')
        self.assertEqual(released.status_code, 200)
        self.assertEqual(
            reclaimed.get_json()['todo']['fencing_token'], token + 1)

        current = reclaimed.get_json()['todo']
        patched = self.client.patch(
            f"/api/v1/goal-todos/{todo['id']}",
            json={
                'expected_version': current['version'],
                'priority': 'P0',
                'status': 'cancelled',
            })
        stale_patch = self.client.patch(
            f"/api/v1/goal-todos/{todo['id']}",
            json={
                'expected_version': current['version'],
                'priority': 'P2',
            })
        self.assertEqual(patched.status_code, 200, patched.get_data(as_text=True))
        self.assertEqual(stale_patch.status_code, 409)
        self.assertEqual(stale_patch.get_json()['code'], 'TODO_VERSION_CONFLICT')

    def test_managed_effect_authorization_is_a_signed_second_phase(self):
        goal = self._create_goal()
        self._create_todo(
            goal['id'], title='Apply typed manifest', action_kind='write',
            priority='P0',
            required_write_scopes=['repo/src/**'],
            authorization_envelope={
                'allowed_effects': ['edit_workspace', 'run_tests'],
                'requires_gate': ['git_push'],
            })
        dispatched = self._dispatch(
            goal['id'], key='managed-dispatch', execution_mode='managed_typed')

        self.assertEqual(
            dispatched.status_code, 201, dispatched.get_data(as_text=True))
        turn = dispatched.get_json()['turn']
        self.assertFalse(turn['effect_authorization_ready'])
        self.assertEqual(
            turn['envelope']['authorization']['phase'],
            'effect_manifest_required')
        manifest = {
            'schema_version': 'agent_typed_manifest_v1',
            'actions': [
                {'action': 'edit_workspace', 'params': {'path': 'repo/src/app.py'}},
                {'action': 'run_tests', 'params': {'suite': 'unit'}},
            ],
        }
        auth_body = {
            'worker_id': 'worker-process-a',
            'manifest': manifest,
            'manifest_hash': sha256_ref(manifest),
            'approval_scope_hash': turn['envelope']['approval_scope_hash'],
            'audience': f'job-service:claw-{self.claw_a.id}',
        }
        authorized = self.client.post(
            f"/api/v1/agent-turns/{turn['turn_id']}/effect-authorizations",
            json=auth_body,
            headers=self._headers(key='effect-auth-1'))
        replay = self.client.post(
            f"/api/v1/agent-turns/{turn['turn_id']}/effect-authorizations",
            json=auth_body,
            headers=self._headers(key='effect-auth-1'))
        changed = dict(auth_body, manifest_hash='sha256:changed')
        conflict = self.client.post(
            f"/api/v1/agent-turns/{turn['turn_id']}/effect-authorizations",
            json=changed,
            headers=self._headers(key='effect-auth-1'))

        self.assertEqual(
            authorized.status_code, 201, authorized.get_data(as_text=True))
        envelope = authorized.get_json()['authorization_envelope']
        signature = envelope.pop('signature')
        valid, reason = verify_envelope(
            envelope,
            signature,
            expected_issuer='clawteam-hub',
            expected_subject='worker:worker-process-a',
            expected_audience=f'job-service:claw-{self.claw_a.id}',
            expected_manifest_hash=sha256_ref(manifest),
            expected_approval_scope_hash=turn['envelope']['approval_scope_hash'],
        )
        self.assertTrue(valid, reason)
        tampered = dict(envelope, manifest_hash='sha256:tampered')
        tampered_valid, _ = verify_envelope(tampered, signature)
        self.assertFalse(tampered_valid)
        self.assertEqual(envelope['manifest_hash'], sha256_ref(manifest))
        self.assertEqual(
            envelope['audience'], f'job-service:claw-{self.claw_a.id}')
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.get_json()['idempotent_replay'])
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(
            conflict.get_json()['code'],
            'EFFECT_AUTHORIZATION_ALREADY_ISSUED')

    def test_writeback_requires_fencing_and_independent_verification(self):
        goal = self._create_goal(control_mode='enforce_p1')
        self.assertEqual(goal['control_mode'], 'enforce_p1')
        self._create_todo(
            goal['id'], title='Apply patch', action_kind='write', priority='P0')
        dispatch = self._dispatch(goal['id'], key='writeback-turn')
        turn = dispatch.get_json()['turn']
        base = {
            'worker_id': 'worker-process-a',
            'transition_id': 'transition-1',
            'result_kind': 'VALIDATED_COMPLETION',
            'summary': 'Patch applied and tested',
            'expected_todo_version': turn['todo_version'],
            'fencing_token': turn['fencing_token'],
            'verification': {'status': 'passed', 'evidence_refs': ['test://1']},
            'evidence': {'tests': ['pytest']},
            'effect_receipts': [],
        }

        stale = dict(base, fencing_token=turn['fencing_token'] + 1)
        stale_response = self.client.post(
            f"/api/v1/agent-turns/{turn['turn_id']}/writeback",
            json=stale, headers=self._headers())
        self.assertEqual(stale_response.status_code, 409)
        self.assertEqual(stale_response.get_json()['code'], 'FENCING_TOKEN_STALE')

        unverified = dict(base, verification={'status': 'passed'})
        unverified_response = self.client.post(
            f"/api/v1/agent-turns/{turn['turn_id']}/writeback",
            json=unverified, headers=self._headers())
        self.assertEqual(unverified_response.status_code, 400)
        self.assertEqual(
            unverified_response.get_json()['code'],
            'VERIFICATION_EVIDENCE_REQUIRED')

        accepted = self.client.post(
            f"/api/v1/agent-turns/{turn['turn_id']}/writeback",
            json=base, headers=self._headers())
        replay = self.client.post(
            f"/api/v1/agent-turns/{turn['turn_id']}/writeback",
            json=base, headers=self._headers())
        changed = dict(base, summary='different payload')
        conflict = self.client.post(
            f"/api/v1/agent-turns/{turn['turn_id']}/writeback",
            json=changed, headers=self._headers())

        self.assertEqual(accepted.status_code, 201, accepted.get_data(as_text=True))
        self.assertEqual(replay.status_code, 200, replay.get_data(as_text=True))
        self.assertTrue(replay.get_json()['idempotent_replay'])
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.get_json()['code'], 'TRANSITION_ID_REUSED')
        self.assertEqual(
            db.session.get(GoalTodo, turn['todo_id']).status, 'completed')
        self.assertEqual(AgentTransitionReceipt.query.count(), 1)
        self.assertEqual(
            AgentTurn.query.filter_by(turn_id=turn['turn_id']).first().status,
            'completed')

    def test_shadow_writeback_records_contract_warning_without_blocking(self):
        goal = self._create_goal(control_mode='shadow')
        self._create_todo(
            goal['id'], title='Apply low-risk patch', action_kind='write')
        turn = self._dispatch(
            goal['id'], key='shadow-warning-turn').get_json()['turn']
        response = self.client.post(
            f"/api/v1/agent-turns/{turn['turn_id']}/writeback",
            json={
                'worker_id': 'worker-process-a',
                'transition_id': 'shadow-warning-transition',
                'result_kind': 'VALIDATED_COMPLETION',
                'summary': 'Completed but verifier evidence is incomplete',
                'expected_todo_version': turn['todo_version'],
                'fencing_token': turn['fencing_token'],
                'verification': {'status': 'passed'},
            },
            headers=self._headers())

        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        contract = response.get_json()['verification']['_contract']
        self.assertFalse(contract['valid'])
        self.assertEqual(contract['code'], 'CONTRACT_INVALID')
        self.assertEqual(contract['policy'], 'warning')
        self.assertEqual(
            db.session.get(GoalTodo, turn['todo_id']).status, 'completed')

    def test_gate_resolution_is_human_only_versioned_and_idempotent(self):
        goal = self._create_goal()
        todo = self._create_todo(
            goal['id'], title='Publish release', action_kind='deploy')
        created = self.client.post(
            '/api/v1/control-gates',
            json={
                'goal_id': goal['id'],
                'work_item_id': todo['id'],
                'gate_type': 'human_approval',
                'blocking_scope': 'work_item',
                'question': 'Publish now?',
                'reason_code': 'external_effect',
            }, headers={'Idempotency-Key': 'publish-gate'})
        gate = created.get_json()['gate']

        worker_rejected = self.client.post(
            f"/api/v1/control-gates/{gate['id']}/resolve",
            json={'resolution': 'approved', 'expected_version': gate['version']},
            headers=self._headers(key='worker-resolve'))
        self.assertEqual(worker_rejected.status_code, 403)

        body = {
            'resolution': 'approved',
            'expected_version': gate['version'],
            'note': 'Approved for this bounded Todo only',
        }
        resolved = self.client.post(
            f"/api/v1/control-gates/{gate['id']}/resolve",
            json=body, headers={'Idempotency-Key': 'human-resolve'})
        replay = self.client.post(
            f"/api/v1/control-gates/{gate['id']}/resolve",
            json=body, headers={'Idempotency-Key': 'human-resolve'})

        self.assertEqual(resolved.status_code, 200, resolved.get_data(as_text=True))
        self.assertEqual(resolved.get_json()['gate']['status'], 'approved')
        self.assertEqual(resolved.get_json()['wake']['work_item_id'], todo['id'])
        self.assertTrue(replay.get_json()['replayed'])
        stored = db.session.get(AgentControlGate, gate['id'])
        self.assertEqual(stored.resolution, 'approved')
        self.assertEqual(stored.version, 2)


if __name__ == '__main__':
    unittest.main()
