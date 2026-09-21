"""Team-selected Flows are live grants, not duplicated manual ACL entries."""
import copy
import unittest

import test_agent_teams_api as fixtures
from app import db
from app.models import AgentTeam, ClawSidecarConfig, OpenClawInstance, WorkflowRun
from app.services.agent_team_permissions import can_dispatch_to, can_execute, flow_grants


class TeamFlowPermissionsTest(unittest.TestCase):
    def setUp(self):
        fixtures.AgentTeamsApiTest.setUp(self)
        from app.api.agent_client import agent_bp
        self.app.register_blueprint(agent_bp, url_prefix='/api/openclaws')
    tearDown = fixtures.AgentTeamsApiTest.tearDown
    _definition = fixtures.AgentTeamsApiTest._definition
    _login_admin = fixtures.AgentTeamsApiTest._login_admin
    _headers = fixtures.AgentTeamsApiTest._headers
    _setup_team = fixtures.AgentTeamsApiTest._setup_team
    _lease = fixtures.AgentTeamsApiTest._lease
    _auth = fixtures.AgentTeamsApiTest._auth
    _mission = fixtures.AgentTeamsApiTest._mission
    _plan = fixtures.AgentTeamsApiTest._plan
    _dispatch = fixtures.AgentTeamsApiTest._dispatch

    def setup_grants(self):
        self._setup_team()
        self.flow_a.editor_acl_json = {'claw_ids': []}
        self.flow_a.executor_acl_json = {'all': False, 'claw_ids': []}
        self.flow_a.visibility_scope = 'private'
        db.session.commit()
        with self.client.session_transaction() as session:
            session.clear()

    def save_team(self, **changes):
        data = copy.deepcopy(self.config)
        data.update(changes)
        data['expected_version'] = db.session.get(AgentTeam, self.team_id).version
        self._login_admin()
        response = self.client.put('/api/v1/agent-teams/%s' % self.team_id, json=data)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.config = data
        with self.client.session_transaction() as session:
            session.clear()

    def sidecar(self):
        response = self.client.get('/api/openclaws/%s/sidecar-config' % self.main_claw.id,
                                   headers=self._headers())
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()

    def test_selected_flow_visible_executable_not_editable_to_manager_and_members(self):
        self.setup_grants()
        for token in (self.main_token, self.other_token, self.backup_token):
            response = self.client.get('/api/v1/workflow-definitions/%s' % self.flow_a.id,
                                       headers=self._headers(token))
            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertTrue(response.get_json()['can_execute'])
            self.assertFalse(response.get_json()['can_edit'])
            self.assertFalse(response.get_json()['can_manage'])
        self.assertFalse(can_execute(self.main_claw.id, self.flow_b))
        self.assertFalse(can_execute(self.main_claw.id, self.foreign_flow))
        self.assertEqual(self.flow_a.editor_acl_json, {'claw_ids': []})
        self.assertEqual(self.flow_a.executor_acl_json, {'all': False, 'claw_ids': []})

    def test_manager_mission_dispatch_without_any_manual_acl(self):
        self.setup_grants()
        mission_id = self._mission()
        self.assertEqual(self._plan(mission_id).status_code, 201)
        response = self._dispatch(mission_id)
        self.assertEqual(response.status_code, 201, response.get_json())
        self.assertEqual(response.get_json()['run']['context']['workflow_start']['worker_claw_id'],
                         self.other_claw.id)

    def test_ordinary_delegation_only_to_same_team_member(self):
        self.setup_grants()
        self.assertTrue(can_dispatch_to(self.main_claw.id, self.other_claw.id, self.flow_a))
        self.assertFalse(can_dispatch_to(self.other_claw.id, self.main_claw.id, self.flow_a))
        outsider = OpenClawInstance(name='outsider', safe_name='outsider', claw_tag='outsider',
                                   owner='other', project_id=self.project.id)
        db.session.add(outsider)
        db.session.commit()
        self.assertFalse(can_dispatch_to(self.main_claw.id, outsider.id, self.flow_a))
        denied = self.client.post('/api/v1/workflow-runs', headers=self._headers(), json={
            'definition_id': self.flow_a.id, 'worker_claw_id': outsider.id})
        self.assertEqual(denied.status_code, 403, denied.get_json())
        result = self.client.post('/api/v1/workflow-runs', headers=self._headers(), json={
            'definition_id': self.flow_a.id, 'worker_claw_id': self.other_claw.id})
        self.assertEqual(result.status_code, 201, result.get_json())

    def test_codex_sidecar_team_grant_bypasses_old_ceiling_but_not_orchestrator_policy(self):
        self.setup_grants()
        db.session.add(ClawSidecarConfig(claw_id=self.main_claw.id, agent_type='codex',
            system_context_policy_json={'allowed_workflow_create_definition_ids': []}))
        db.session.commit()
        first = self.sidecar()
        self.assertIn(self.flow_a.id, first['system_context']['policy']['allowed_workflow_create_definition_ids'])
        cfg = db.session.get(ClawSidecarConfig, self.main_claw.id)
        self.assertEqual(cfg.system_context_policy_json['allowed_workflow_create_definition_ids'], [])
        self.save_team(policy={'allowed_definition_ids': [self.flow_b.id], 'max_child_runs': 3})
        second = self.sidecar()
        self.assertNotIn(self.flow_a.id, second['system_context']['policy']['allowed_workflow_create_definition_ids'])
        self.assertIn(self.flow_b.id, second['system_context']['policy']['allowed_workflow_create_definition_ids'])
        self.assertNotEqual(first['system_context_digest'], second['system_context_digest'])

    def test_revocation_keeps_manual_grants_and_other_team_grants(self):
        self.setup_grants()
        self.assertEqual(flow_grants(self.main_claw), [self.flow_a.id])
        self.save_team(status='paused')
        self.assertEqual(flow_grants(self.main_claw), [])
        self.save_team(status='active', members=[])
        self.assertFalse(can_execute(self.other_claw.id, self.flow_a))
        self.flow_a.executor_acl_json = {'claw_ids': [self.other_claw.id]}
        db.session.commit()
        # Manual ACL is unchanged, irrespective of private visibility policy.
        from app.api.workflow_missions import _claw_can_execute_definition
        self.assertTrue(_claw_can_execute_definition(self.other_claw.id, self.flow_a))
        self._login_admin()
        second_config = {key: value for key, value in dict(self.config, name='second').items()
                         if key != 'expected_version'}
        second = self.client.post('/api/v1/agent-teams', json=second_config)
        self.assertEqual(second.status_code, 201, second.get_json())
        self.save_team(status='archived')
        self.assertTrue(can_execute(self.main_claw.id, self.flow_a))
        self.app.config['AGENT_TEAMS_ENABLED'] = False
        self.assertFalse(can_execute(self.main_claw.id, self.flow_a))

    def test_live_project_and_member_checks_fail_closed(self):
        self.setup_grants()
        self.other_claw.project_id = self.other_project.id
        db.session.commit()
        self.assertFalse(can_execute(self.other_claw.id, self.flow_a))
        self.assertFalse(can_dispatch_to(self.main_claw.id, self.other_claw.id, self.flow_a))
        self.main_claw.status = 'deleted'
        db.session.commit()
        self.assertFalse(can_execute(self.main_claw.id, self.flow_a))

    def test_mission_does_not_bypass_runtime_or_current_team_scope(self):
        self.setup_grants()
        mission_id = self._mission()
        self.assertEqual(self._plan(mission_id).status_code, 201)
        cfg = db.session.get(ClawSidecarConfig, self.other_claw.id)
        cfg.runtime_config_json = {}
        db.session.commit()
        self.assertEqual(self._dispatch(mission_id).get_json()['code'], 'MISSION_WORKER_RUNTIME_REQUIRED')
        self.save_team(policy={'allowed_definition_ids': [self.flow_b.id], 'max_child_runs': 3})
        self.assertEqual(self._lease().status_code, 200)
        self.assertEqual(self._dispatch(mission_id).get_json()['code'], 'TEAM_FLOW_NOT_ALLOWED')
        self.assertEqual(WorkflowRun.query.count(), 0)

    def test_ordinary_mission_snapshot_cannot_preserve_revoked_team_grant(self):
        self.setup_grants()
        created = self.client.post('/api/v1/workflow-missions', headers=self._headers(), json={
            'project_id': self.project.id, 'objective': 'ordinary team-authorized work',
            'allowed_definition_ids': [self.flow_a.id],
            'context': {'team_grant_definition_ids': []}})
        self.assertEqual(created.status_code, 201, created.get_json())
        mission_id = created.get_json()['id']
        self.save_team(status='paused')
        response = self.client.post('/api/v1/workflow-missions/%s/dispatch' % mission_id,
            headers=self._headers(), json={'workflow_definition_id': self.flow_a.id,
                                          'decision_key': 'revoked-grant'})
        self.assertEqual(response.status_code, 403, response.get_json())
        self.assertEqual(response.get_json()['code'], 'MISSION_TEAM_GRANT_REVOKED')
        self.assertEqual(WorkflowRun.query.count(), 0)

    def test_sidecar_invalid_policy_remains_closed_and_orchestrator_is_not_expanded(self):
        from app.services.agent_system_context import _resolve_workflow_policy
        policy, warnings = _resolve_workflow_policy('codex', [55], {'broken': True}, [55])
        self.assertEqual(policy['allowed_workflow_create_definition_ids'], [])
        self.assertIn('CODEX_ORCHESTRATOR_POLICY_INVALID', warnings)
        configured = {'allowed_workflow_create_definition_ids': [25], 'codex_orchestrator': {
            'enabled': True, 'session_key': 'test:orchestrator', 'resume_on': ['blocked'],
            'allowed_next_flows': [25], 'max_retries': 2, 'review_success': True}}
        policy, warnings = _resolve_workflow_policy('codex', [25, 55], configured, [55])
        self.assertEqual(policy['allowed_workflow_create_definition_ids'], [25, 55])
        self.assertEqual(policy['codex_orchestrator']['allowed_next_flows'], [25])
