"""Team-selected Flows are live grants, not duplicated manual ACL entries."""
import copy
import unittest

import test_agent_teams_api as fixtures
from app import db
from app.models import (
    AgentContextSnapshot, AgentTask, AgentTeam, ClawSidecarConfig,
    OpenClawInstance, WorkflowRun,
)
from app.services.agent_team_permissions import can_dispatch_to, can_execute, flow_grants
from app.services.agent_context_snapshots import ContextSnapshotError, freeze_run_context


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

    def test_team_dispatch_freezes_test_manager_contract_and_task_reference(self):
        self.setup_grants()
        mission_id = self._mission()
        self.assertEqual(self._plan(mission_id).status_code, 201)
        response = self._dispatch(mission_id)
        self.assertEqual(response.status_code, 201, response.get_json())
        run = db.session.get(WorkflowRun, response.get_json()['workflow_run_id'])
        snapshot = AgentContextSnapshot.query.filter_by(
            workflow_run_id=run.id).one()
        self.assertEqual(snapshot.snapshot_id, run.context_snapshot_id)
        self.assertEqual(snapshot.context_sha256, run.context_snapshot_sha256)
        readback = self.client.get(
            '/api/v1/workflow-runs/%s/agent-context-snapshot' % run.id,
            headers=self._headers())
        self.assertEqual(readback.status_code, 200, readback.get_json())
        self.assertEqual(readback.get_json()['snapshot_id'], snapshot.snapshot_id)
        document = snapshot.context_json
        self.assertEqual(document['assignment']['manager_claw_id'], self.main_claw.id)
        manager = next(item for item in document['participants']
                       if item['claw_id'] == self.main_claw.id)
        self.assertEqual(manager['role_key'], 'test_manager')
        responsibilities = manager['role_contract']['responsibilities']
        self.assertTrue(any('调度' in item for item in responsibilities))
        self.assertTrue(any('证据' in item for item in responsibilities))
        self.assertFalse(manager['role_contract']['dispatch_policy'].get(
            'manager_is_default_executor', False))
        task = AgentTask.query.filter_by(
            task_type='workflow_agent_task').order_by(AgentTask.id.desc()).first()
        if task:
            self.assertEqual(task.context_snapshot_id, snapshot.snapshot_id)
            self.assertEqual(task.context_snapshot_sha256, snapshot.context_sha256)

        before = snapshot.to_dict()
        team = db.session.get(AgentTeam, self.team_id)
        team.objective = 'changed after dispatch'
        team.version += 1
        db.session.commit()
        db.session.refresh(snapshot)
        self.assertEqual(snapshot.to_dict(), before)

    def test_test_manager_is_not_a_default_executor(self):
        self.setup_grants()
        run = WorkflowRun(
            definition_id=self.flow_a.id,
            project_id=self.project.id,
            context_json={'workflow_start': {
                'worker_claw_id': self.main_claw.id,
                'executor_claw_id': self.main_claw.id,
            }},
        )
        db.session.add(run)
        db.session.flush()
        with self.assertRaises(ContextSnapshotError) as raised:
            freeze_run_context(
                run, team=db.session.get(AgentTeam, self.team_id),
                manager_claw_id=self.main_claw.id)
        self.assertEqual(raised.exception.code, 'POLICY_ROLE_OVERLAP')
        db.session.rollback()

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

    def test_project_assistant_gets_execution_and_identity_but_not_manager_authority(self):
        self.setup_grants()
        assistant_token = 'mission-assistant-token'
        from app.models import hash_token
        assistant = OpenClawInstance(
            name='项目助理', safe_name='mission-assistant', claw_tag='mission-assistant',
            owner='assistant', project_id=self.project.id,
            api_token_hash=hash_token(assistant_token), status='工作')
        db.session.add(assistant)
        db.session.flush()
        db.session.add(ClawSidecarConfig(
            claw_id=assistant.id, agent_type='codebuddy', config_owner='worker',
            runtime_config_json={
                'schema': 1, 'kind': 'claw_worker', 'provider': 'codebuddy',
                'runtime_mode': 'agent_direct', 'platform': 'windows',
                'provider_version': 'codebuddy-cli', 'source': 'worker',
            }))
        db.session.commit()
        members = copy.deepcopy(self.config['members']) + [{
            'claw_id': assistant.id, 'role_key': 'project_assistant',
            'specialties': [],
        }]
        self.save_team(members=members)

        response = self.client.get(
            '/api/v1/workflow-definitions/%s' % self.flow_a.id,
            headers=self._headers(assistant_token))
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertTrue(response.get_json()['can_execute'])
        self.assertFalse(response.get_json()['can_edit'])
        self.assertTrue(can_dispatch_to(
            self.main_claw.id, assistant.id, self.flow_a))
        self.assertFalse(can_dispatch_to(
            assistant.id, self.other_claw.id, self.flow_a))

        sidecar = self.client.get(
            '/api/openclaws/%s/sidecar-config' % assistant.id,
            headers=self._headers(assistant_token))
        self.assertEqual(sidecar.status_code, 200, sidecar.get_json())
        team = next(item for item in sidecar.get_json()['agent_teams']
                    if item['team_id'] == self.team_id)
        self.assertEqual(team['self']['effective_role_key'], 'project_assistant')
        self.assertIn('version_data', team['activity_reporting']['task_types'])
        contract = team['role_contracts']['project_assistant']
        self.assertIn('版本', contract['purpose'])
        self.assertTrue(any('调度' in item for item in contract['forbidden']))

        activity = self.client.post(
            '/api/v1/agent-teams/%s/members/%s/activity' % (
                self.team_id, assistant.id),
            headers=self._headers(assistant_token), json={
                'event_id': 'assistant-version-data-1',
                'expected_version': 0,
                'state': 'working',
                'summary': '正在收集版本数据',
                'task': {
                    'task_key': 'release-20260924',
                    'title': '收集版本与构建信息',
                    'task_type': 'version_data',
                    'reference': 'release:20260924',
                    'status': 'working',
                    'progress_percent': 20,
                    'progress_message': '已取得提交号',
                },
            })
        self.assertEqual(activity.status_code, 200, activity.get_json())

    def test_codex_sidecar_team_grant_bypasses_old_ceiling_but_not_orchestrator_policy(self):
        self.setup_grants()
        db.session.add(ClawSidecarConfig(claw_id=self.main_claw.id, agent_type='codex',
            system_context_policy_json={'allowed_workflow_create_definition_ids': []}))
        db.session.commit()
        first = self.sidecar()
        self.assertIn(self.flow_a.id, first['system_context']['policy']['allowed_workflow_create_definition_ids'])
        team_rule = next(item for item in first['system_context']['rules']
                         if item.get('name') == 'agent_team_identity')
        self.assertIn('负责团队整体测试管理', team_rule['content'])
        self.assertIn('不合格结果必须退回', team_rule['content'])
        manager_team = next(item for item in first['agent_teams']
                            if item['team_id'] == self.team_id)
        self.assertEqual(manager_team['self']['effective_role_key'], 'test_manager')
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
