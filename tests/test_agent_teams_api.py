"""Team roles, immutable plans and manager fencing must not alter legacy roles."""
import copy
import unittest
from types import SimpleNamespace
from datetime import datetime, timedelta

import test_workflow_missions_api as fixtures
from app import db
from app.models import (
    AgentTeam, AgentTeamChatRound, AgentTeamKnowledgeResource, AgentTeamMember,
    AgentTeamMission, AgentTeamSkillResource, ChatRoom, ChatRoomDelivery,
    ChatRoomMember, ChatRoomMessage, ClawSidecarConfig, KnowledgeEntry,
    MissionStage, OpenClawInstance, Skill, WorkflowRun, hash_token,
)
from app.services.chat_rooms import pending_agent_events


class AgentTeamsApiTest(unittest.TestCase):
    setUp = fixtures.WorkflowMissionsApiTest.setUp
    tearDown = fixtures.WorkflowMissionsApiTest.tearDown
    _definition = fixtures.WorkflowMissionsApiTest._definition
    _login_admin = fixtures.WorkflowMissionsApiTest._login_admin
    _headers = fixtures.WorkflowMissionsApiTest._headers

    def _setup_team(self):
        self.app.config.update(AGENT_TEAM_CONTRACTS_ENABLED=True,
                               AGENT_TEAMS_ENABLED=True,
                               AGENT_TEAMS_PROJECT_IDS=[self.project.id])
        self.backup_token = 'team-backup-token'
        self.backup = OpenClawInstance(
            name='备用经理', safe_name='team-backup', claw_tag='team-backup',
            owner=self.admin.username, project_id=self.project.id,
            role='specialist', api_token_hash=hash_token(self.backup_token), status='工作')
        db.session.add(self.backup)
        db.session.flush()
        self.flow_a.editor_acl_json = {'claw_ids': [self.main_claw.id, self.backup.id]}
        self.flow_a.executor_acl_json = {'claw_ids': [self.other_claw.id], 'user_ids': [self.admin.id]}
        db.session.commit()
        self.config = {
            'project_id': self.project.id, 'name': '手游回归团队', 'objective': '修复验证',
            'primary_manager_claw_id': self.main_claw.id,
            'backup_manager_claw_id': self.backup.id,
            'members': [
                {'claw_id': self.other_claw.id, 'role_key': 'test_executor',
                 'specialties': ['editor', 'mobile_package']},
                {'claw_id': self.other_claw.id, 'role_key': 'code_analyst'},
                {'claw_id': self.backup.id, 'role_key': 'code_analyst'},
            ],
            'policy': {'allowed_definition_ids': [self.flow_a.id], 'max_child_runs': 3},
        }
        response = self.client.post('/api/v1/agent-teams', json=self.config)
        self.assertEqual(201, response.status_code, response.get_json())
        self.team_id = response.get_json()['id']
        return response.get_json()

    def _lease(self, token=None, session='primary-process', epoch=None):
        if epoch is None:
            epoch = db.session.get(AgentTeam, self.team_id).manager_epoch
        return self.client.post('/api/v1/agent-teams/{}/manager-lease'.format(self.team_id),
                                headers=self._headers(token), json={
                                    'expected_epoch': epoch, 'manager_session_id': session,
                                    'ttl_seconds': 60})

    def _auth(self, epoch=None, session='primary-process'):
        return {'manager_epoch': epoch if epoch is not None else db.session.get(AgentTeam, self.team_id).manager_epoch,
                'manager_session_id': session}

    def _mission(self):
        self.assertEqual(self._lease().status_code, 200)
        response = self.client.post('/api/v1/workflow-missions', headers=self._headers(), json=dict(
            self._auth(), team_id=self.team_id, project_id=self.project.id,
            objective='验证手机修复', allowed_worker_claw_ids=[self.other_claw.id]))
        self.assertEqual(201, response.status_code, response.get_json())
        return response.get_json()['id']

    def _plan(self, mission_id, specialty='mobile_package', token=None, **auth):
        return self.client.post('/api/v1/workflow-missions/{}/team-plan'.format(mission_id),
                                headers=self._headers(token), json=dict(auth or self._auth(), stages=[{
                                    'stage_key': 'verify', 'role_key': 'test_executor',
                                    'specialty': specialty, 'assigned_claw_id': self.other_claw.id,
                                    'input_snapshot': {'build_sha256': 'pinned-build'}}]))

    def _dispatch(self, mission_id, token=None, key='verify-1', **auth):
        return self.client.post('/api/v1/workflow-missions/{}/dispatch'.format(mission_id),
                                headers=self._headers(token), json=dict(auth or self._auth(),
                                    workflow_definition_id=self.flow_a.id, stage_key='verify', decision_key=key))

    def test_fixed_roles_multiple_members_shared_across_teams_without_claw_mutation(self):
        old_role = self.other_claw.role
        team = self._setup_team()
        self.assertEqual(len(team['members']), 3)
        self.config['name'] = '性能团队'
        self.config['members'].append({'claw_id': self.backup.id, 'role_key': 'test_executor',
                                       'specialties': ['client_performance']})
        second = self.client.post('/api/v1/agent-teams', json=self.config)
        self.assertEqual(second.status_code, 201, second.get_json())
        db.session.refresh(self.other_claw)
        self.assertEqual(self.other_claw.role, old_role)
        self.assertEqual(AgentTeamMember.query.filter_by(claw_id=self.other_claw.id).count(), 4)
        self.assertEqual(team['manager_epoch'], 0)
        self.assertTrue(team['manager_authority_active'])
        self.assertTrue(team['manager_lease_active'])  # compatibility alias
        self.assertIsNone(team['manager_lease_expires_at'])

    def test_team_shared_resources_reuse_canonical_content_and_agent_can_curate(self):
        self._setup_team()
        knowledge = KnowledgeEntry(
            title='回归策略', content='# 回归策略', category='testing',
            scope='project', project_id=self.project.id, status='approved',
            entry_type='test_journal', current_revision=3)
        skill = Skill(name='team-regression', display_name='团队回归',
                      review_status='approved', visibility='public',
                      applicable_projects=[self.project.id])
        db.session.add_all([knowledge, skill]); db.session.commit()
        with self.client.session_transaction() as session:
            session.clear()

        for kind, resource_id in (('knowledge', knowledge.id), ('skills', skill.id)):
            response = self.client.post(
                f'/api/v1/agent-teams/{self.team_id}/shared-resources/{kind}',
                headers=self._headers(self.other_token), json={'resource_id': resource_id})
            self.assertEqual(response.status_code, 201, response.get_json())
        manifest = self.client.get(
            f'/api/v1/agent-teams/{self.team_id}/shared-resources',
            headers=self._headers(self.other_token)).get_json()
        self.assertTrue(manifest['can_manage'])
        self.assertEqual(manifest['knowledge'][0]['revision'], 3)
        self.assertEqual(manifest['knowledge'][0]['pull_url'],
                         f'/api/v1/knowledge/{knowledge.id}/export.md')
        self.assertEqual(manifest['skills'][0]['pull_url'],
                         f'/api/v1/skills/{skill.id}/pack')
        self.assertEqual(AgentTeamKnowledgeResource.query.count(), 1)
        self.assertEqual(AgentTeamSkillResource.query.count(), 1)
        update = self.client.put(
            f'/api/v1/skills/{skill.id}', headers=self._headers(self.other_token),
            json={'description': '团队成员共同维护的更新'})
        self.assertEqual(update.status_code, 200, update.get_json())
        self.assertIn('团队成员共同维护', update.get_json()['mirror_content'])

        # Removing a team pointer never deletes canonical content.
        response = self.client.delete(
            f'/api/v1/agent-teams/{self.team_id}/shared-resources/knowledge/{knowledge.id}',
            headers=self._headers(self.other_token))
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(db.session.get(KnowledgeEntry, knowledge.id))

    def test_fixed_team_room_tracks_round_replies_and_reuses_one_room(self):
        self._setup_team()
        self.app.config['CHAT_ROOM_ENABLED'] = True

        first = self.client.get(
            f'/api/v1/agent-teams/{self.team_id}/chat-room')
        self.assertEqual(first.status_code, 200, first.get_json())
        payload = first.get_json()
        room_id = payload['room']['id']
        self.assertEqual(payload['room']['team_id'], self.team_id)
        self.assertEqual(
            {self.main_claw.id, self.backup.id, self.other_claw.id},
            {row['claw_id'] for row in payload['members'] if row['member_type'] == 'agent'})
        self.assertEqual(
            self.client.get(f'/api/v1/agent-teams/{self.team_id}/chat-room')
            .get_json()['room']['id'], room_id)
        self.assertEqual(ChatRoom.query.filter_by(team_id=self.team_id).count(), 1)

        question = self.client.post(
            f'/api/v1/agent-teams/{self.team_id}/chat-room/messages',
            headers={'Idempotency-Key': 'team-round-1'},
            json={
                'content': '请分别说明实际使用的知识库和 Skill。',
                'mention_claw_ids': [self.other_claw.id, self.backup.id],
                'timeout_seconds': 300,
            })
        self.assertEqual(question.status_code, 201, question.get_json())
        body = question.get_json()
        self.assertEqual(body['round']['expected_count'], 2)
        self.assertEqual(body['round']['status'], 'open')
        self.assertEqual(AgentTeamChatRound.query.count(), 1)

        events = pending_agent_events(self.other_claw.id)
        event = next(value for name, value in events if name == 'room_message')
        self.assertEqual(event['message']['id'], body['message']['id'])
        self.assertEqual(event['room']['team_id'], self.team_id)
        self.assertIn('history', event)

        for claw, token in ((self.other_claw, self.other_token),
                            (self.backup, self.backup_token)):
            delivery = (ChatRoomDelivery.query.join(
                ChatRoomMember, ChatRoomMember.id == ChatRoomDelivery.member_id)
                .filter(ChatRoomDelivery.message_id == body['message']['id'],
                        ChatRoomMember.claw_id == claw.id).one())
            transition = self.client.patch(
                f'/api/v1/chat-rooms/{room_id}/deliveries/{delivery.id}',
                headers=self._headers(token), json={'status': 'processing'})
            self.assertEqual(transition.status_code, 200, transition.get_json())
            reply = self.client.post(
                f'/api/v1/chat-rooms/{room_id}/messages',
                headers=dict(self._headers(token), **{
                    'Idempotency-Key': f'team-round-reply-{claw.id}'}),
                json={
                    'content': f'{claw.name} 的实际资源清单',
                    'reply_to_message_id': body['message']['id'],
                    'origin_delivery_id': delivery.id,
                })
            self.assertEqual(reply.status_code, 201, reply.get_json())
            done = self.client.patch(
                f'/api/v1/chat-rooms/{room_id}/deliveries/{delivery.id}',
                headers=self._headers(token), json={'status': 'done'})
            self.assertEqual(done.status_code, 200, done.get_json())
            # Terminal ACK replay is idempotent.
            replay = self.client.patch(
                f'/api/v1/chat-rooms/{room_id}/deliveries/{delivery.id}',
                headers=self._headers(token), json={'status': 'done'})
            self.assertEqual(replay.status_code, 200, replay.get_json())
            self.assertIsNotNone(replay.get_json()['response_message_id'])

        closed = self.client.get(
            f'/api/v1/agent-teams/{self.team_id}/chat-room').get_json()
        self.assertEqual(closed['rounds'][0]['status'], 'completed')
        self.assertEqual(closed['rounds'][0]['replied_count'], 2)
        self.assertEqual(ChatRoomMessage.query.filter_by(
            room_id=room_id, origin_delivery_id=None).count(), 1)

        with self.client.session_transaction() as session:
            session.clear()
        denied = self.client.delete(
            f'/api/v1/chat-rooms/{room_id}', headers=self._headers())
        self.assertEqual(denied.status_code, 403, denied.get_json())
        self.assertEqual(denied.get_json()['code'], 'TEAM_ROOM_DELETE_DENIED')

    def test_members_cannot_introduce_second_manager_or_arbitrary_specialty(self):
        self._setup_team()
        for member in (
            {'claw_id': self.other_claw.id, 'role_key': 'test_manager'},
            {'claw_id': self.other_claw.id, 'role_key': 'test_executor', 'specialties': []},
            {'claw_id': self.other_claw.id, 'role_key': 'test_executor', 'specialties': ['admin']},
            {'claw_id': self.other_claw.id, 'role_key': 'code_analyst', 'specialties': ['editor']},
        ):
            data = dict(self.config, name='bad', members=[member])
            response = self.client.post('/api/v1/agent-teams', json=data)
            self.assertEqual(response.status_code, 400, response.get_json())
        self.assertEqual(AgentTeam.query.count(), 1)

    def test_project_gate_and_admin_boundary(self):
        response = self.client.post('/api/v1/agent-teams', json={'project_id': self.project.id})
        self.assertEqual(response.status_code, 404)
        self._setup_team()
        response = self.client.post('/api/v1/agent-teams', json=dict(self.config, name='forbidden'), headers=self._headers())
        self.assertEqual(response.status_code, 403)
        self.app.config['AGENT_TEAMS_PROJECT_IDS'] = []
        self.assertEqual(self.client.get('/api/v1/agent-teams/{}'.format(self.team_id)).status_code, 404)

    def test_foreign_member_rejected(self):
        self._setup_team()
        self.other_claw.project_id = self.other_project.id
        db.session.commit()
        response = self.client.post('/api/v1/agent-teams', json=dict(self.config, name='bad'))
        self.assertEqual(response.status_code, 400)

    def test_manager_role_authority_is_durable_for_primary_and_backup(self):
        self._setup_team()
        first = self._lease()
        self.assertEqual(first.status_code, 200)
        self.assertEqual(self._lease(session='second-process').status_code, 200)
        backup = self._lease(self.backup_token, session='backup')
        self.assertEqual(backup.status_code, 200)
        self.assertEqual(self._lease(epoch=999).status_code, 200)
        self.assertEqual(self._lease().get_json()['manager_epoch'],
                         first.get_json()['manager_epoch'])
        self.assertEqual(backup.get_json()['manager_authority']['mode'],
                         'team_role_assignment')
        self.assertIsNone(backup.get_json()['manager_authority']['expires_at'])

    def test_immutable_plan_and_dispatch_are_idempotent_and_bound_to_stage(self):
        self._setup_team()
        mission_id = self._mission()
        self.assertEqual(self._plan(mission_id).status_code, 201)
        self.assertTrue(self._plan(mission_id).get_json()['idempotent_replay'])
        self.assertEqual(self._plan(mission_id, specialty='editor').status_code, 409)
        first = self._dispatch(mission_id)
        self.assertEqual(first.status_code, 201, first.get_json())
        replay = self._dispatch(mission_id)
        self.assertEqual(replay.status_code, 200, replay.get_json())
        self.assertEqual(replay.get_json()['workflow_run_id'], first.get_json()['workflow_run_id'])
        self.assertEqual(self._dispatch(mission_id, key='second-decision').status_code, 409)
        self.assertEqual(WorkflowRun.query.count(), 1)
        stage = MissionStage.query.filter_by(mission_id=mission_id).one()
        self.assertEqual(stage.workflow_run_id, first.get_json()['workflow_run_id'])
        self.assertEqual(first.get_json()['run']['context']['mission']['team_id'], self.team_id)

    def test_primary_and_backup_keep_authority_until_removed(self):
        self._setup_team()
        mission_id = self._mission()
        self.assertEqual(self._plan(mission_id).status_code, 201)
        old_epoch = db.session.get(AgentTeam, self.team_id).manager_epoch
        db.session.get(AgentTeam, self.team_id).manager_lease_expires_at = datetime.now() - timedelta(seconds=1)
        db.session.commit()
        takeover = self._lease(self.backup_token, session='backup')
        self.assertEqual(takeover.status_code, 200, takeover.get_json())
        self.assertEqual(takeover.get_json()['manager_epoch'], old_epoch)
        response = self._dispatch(mission_id, token=self.backup_token, **self._auth(session='backup'))
        self.assertEqual(response.status_code, 201, response.get_json())
        self.assertEqual(self._lease(session='primary-again').status_code, 200)

    def test_team_selection_grants_flow_execution_without_manual_acl(self):
        self._setup_team()
        mission_id = self._mission()
        self.assertEqual(self._plan(mission_id).status_code, 201)
        self.flow_a.executor_acl_json = {'claw_ids': []}
        db.session.commit()
        response = self._dispatch(mission_id)
        self.assertEqual(response.status_code, 201, response.get_json())
        self.assertEqual(WorkflowRun.query.count(), 1)

    def test_role_specialty_and_snapshot_intersection(self):
        self._setup_team()
        mission_id = self._mission()
        response = self._plan(mission_id, specialty='client_performance')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(MissionStage.query.count(), 0)
        self.assertEqual(self._plan(mission_id).status_code, 201)
        snapshot = copy.deepcopy(db.session.get(AgentTeamMission, mission_id).snapshot_json)
        data = copy.deepcopy(self.config)
        data['expected_version'] = 1
        data['members'][0]['specialties'] = ['editor']
        update = self.client.put('/api/v1/agent-teams/{}'.format(self.team_id), json=data)
        self.assertEqual(update.status_code, 200, update.get_json())
        self.assertEqual(db.session.get(AgentTeamMission, mission_id).snapshot_json, snapshot)
        self.assertEqual(self._lease().status_code, 200)
        self.assertEqual(self._dispatch(mission_id).get_json()['code'], 'TEAM_STAGE_MEMBER_INVALID')

    def test_pause_does_not_cancel_existing_runs_and_revokes_manager(self):
        self._setup_team()
        mission_id = self._mission()
        self._plan(mission_id)
        self.assertEqual(self._dispatch(mission_id).status_code, 201)
        run = WorkflowRun.query.one()
        old_status = run.status
        response = self.client.put('/api/v1/agent-teams/{}'.format(self.team_id),
                                   json=dict(self.config, expected_version=1, status='paused'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._lease().get_json()['code'], 'TEAM_NOT_ACTIVE')
        db.session.refresh(run)
        self.assertEqual(run.status, old_status)
        self.assertEqual(self._dispatch(mission_id).status_code, 409)

    def test_no_plan_no_dispatch_and_no_evidence_no_completion(self):
        self._setup_team()
        mission_id = self._mission()
        self.assertEqual(self._dispatch(mission_id).get_json()['code'], 'TEAM_STAGE_REQUIRED')
        response = self.client.post('/api/v1/workflow-missions/{}/complete'.format(mission_id),
                                   headers=self._headers(), json=self._auth())
        self.assertEqual(response.get_json()['code'], 'TEAM_STAGES_INCOMPLETE')

    def test_update_requires_version_and_does_not_infer_runtime(self):
        self._setup_team()
        response = self.client.put('/api/v1/agent-teams/{}'.format(self.team_id),
                                   json=dict(self.config, expected_version=99))
        self.assertEqual(response.get_json()['code'], 'TEAM_VERSION_CONFLICT')
        self.assertIsNone(db.session.get(ClawSidecarConfig, self.main_claw.id))

    def test_team_handoff_uses_assigned_member_not_legacy_claw_role(self):
        from app.api.mission_handoffs import _can_target
        self._setup_team()
        mission_id = self._mission()
        self._plan(mission_id)
        stage = MissionStage.query.filter_by(mission_id=mission_id).one()
        handoff = SimpleNamespace(mission=SimpleNamespace(control_mode='team_managed'),
                                  target_stage_record_id=stage.id, to_role='test_executor')
        self.assertTrue(_can_target({'type': 'claw', 'id': self.other_claw.id}, handoff, self.project.id))
        self.assertFalse(_can_target({'type': 'claw', 'id': self.backup.id}, handoff, self.project.id))
        self.assertFalse(_can_target({'type': 'user', 'id': self.admin.id}, handoff, self.project.id))

    def test_plan_validation_rolls_back_partial_stage_creation(self):
        self._setup_team()
        mission_id = self._mission()
        good = {'stage_key': 'first', 'role_key': 'test_executor', 'specialty': 'editor',
                'assigned_claw_id': self.other_claw.id, 'input_snapshot': {}}
        bad = dict(good, stage_key='second', specialty='client_performance')
        response = self.client.post('/api/v1/workflow-missions/{}/team-plan'.format(mission_id),
                                    headers=self._headers(), json=dict(self._auth(), stages=[good, bad]))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(MissionStage.query.count(), 0)
        self.assertIsNone(db.session.get(AgentTeamMission, mission_id).plan_sha256)

    def test_manager_cannot_self_enable_destructive_actions(self):
        self._setup_team()
        self._lease()
        response = self.client.post('/api/v1/workflow-missions', headers=self._headers(), json=dict(
            self._auth(), team_id=self.team_id, project_id=self.project.id,
            objective='unsafe', allow_destructive_actions=True))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['code'], 'TEAM_AUTHORITY_NOT_GRANTED')

    def test_foreign_worker_cannot_read_team_mission_even_after_project_move(self):
        self._setup_team()
        mission_id = self._mission()
        self.other_claw.project_id = self.other_project.id
        db.session.commit()
        response = self.client.get('/api/v1/workflow-missions/{}'.format(mission_id),
                                   headers=self._headers(self.other_token))
        self.assertEqual(response.status_code, 403)

    def test_queued_stage_cannot_be_claimed_before_dispatch(self):
        self._setup_team()
        mission_id = self._mission()
        self._plan(mission_id)
        response = self.client.post('/api/v1/missions/{}/stages/verify/claim'.format(mission_id),
                                    headers=self._headers(self.other_token),
                                    json={'expected_version': 1, 'idempotency_key': 'early-claim'})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()['code'], 'TEAM_STAGE_NOT_DISPATCHED')
        bypass = self.client.post('/api/v1/missions/{}/stages/verify/transition'.format(mission_id),
                                  headers=self._headers(self.other_token), json={
                                      'expected_version': 1, 'fencing_token': 0, 'to_state': 'running',
                                      'reason_code': 'STAGE_STARTED', 'evidence_refs': [],
                                      'idempotency_key': 'bypass-claim'})
        self.assertEqual(bypass.status_code, 409, bypass.get_json())
        self.assertEqual(bypass.get_json()['code'], 'TEAM_STAGE_NOT_DISPATCHED')

    def test_disabled_feature_does_not_query_unmigrated_team_tables(self):
        self.app.config['AGENT_TEAMS_ENABLED'] = False
        db.session.execute(db.text('DROP TABLE agent_teams'))
        db.session.commit()
        response = self.client.get('/api/v1/agent-teams/1')
        self.assertEqual(response.status_code, 404)
        legacy = self.client.post('/api/v1/workflow-missions', json={
            'project_id': self.project.id, 'main_claw_id': self.main_claw.id,
            'objective': 'legacy remains available'})
        self.assertEqual(legacy.status_code, 201, legacy.get_json())

    def test_feature_rollback_preserves_historical_mission_readback(self):
        self._setup_team()
        mission_id = self._mission()
        self.app.config['AGENT_TEAMS_ENABLED'] = False
        self.assertEqual(self.client.get('/api/v1/workflow-missions').status_code, 200)
        self.assertEqual(self.client.get('/api/v1/workflow-missions/{}'.format(mission_id),
                                         headers=self._headers()).status_code, 200)
        self.assertEqual(self._dispatch(mission_id).status_code, 404)

    def test_ui_options_are_project_scoped_and_never_include_credentials(self):
        self._setup_team()
        response = self.client.get('/api/v1/agent-teams/options?project_id={}'.format(self.project.id))
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload['can_manage'])
        self.assertEqual({row['id'] for row in payload['flows']}, {self.flow_a.id, self.flow_b.id})
        self.assertTrue(next(row for row in payload['agents'] if row['id'] == self.other_claw.id)['has_worker_runtime'])
        for row in payload['agents']:
            self.assertEqual(set(row), {'id', 'name', 'status', 'has_worker_runtime'})
        readonly = self.client.get('/api/v1/agent-teams/options?project_id={}'.format(self.project.id),
                                   headers=self._headers())
        self.assertFalse(readonly.get_json()['can_manage'])
        self.app.config['AGENT_TEAMS_PROJECT_IDS'].append(self.other_project.id)
        denied = self.client.get('/api/v1/agent-teams/options?project_id={}'.format(self.other_project.id),
                                 headers=self._headers())
        self.assertEqual(denied.status_code, 403)

    def test_team_list_pagination_and_mission_stage_projection(self):
        self._setup_team()
        second = self.client.post('/api/v1/agent-teams', json=dict(self.config, name='second'))
        self.assertEqual(second.status_code, 201)
        page = self.client.get('/api/v1/agent-teams?project_id={}&limit=1&offset=1'.format(self.project.id)).get_json()
        self.assertEqual(page['total'], 2)
        self.assertEqual(len(page['items']), 1)
        self.assertEqual(page['items'][0]['id'], second.get_json()['id'])
        invalid = self.client.get('/api/v1/agent-teams?project_id={}&offset=-1'.format(self.project.id))
        self.assertEqual(invalid.status_code, 400)
        mission_id = self._mission()
        self._plan(mission_id)
        response = self.client.get('/api/v1/agent-teams/{}/missions?limit=1'.format(self.team_id))
        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload['total'], 1)
        self.assertEqual(payload['items'][0]['stages'][0]['stage_key'], 'verify')
        self.assertIsNone(payload['items'][0]['stages'][0]['workflow_run_id'])
        empty = self.client.get('/api/v1/agent-teams/{}/missions'.format(second.get_json()['id']))
        self.assertEqual(empty.get_json()['total'], 0)


if __name__ == '__main__':
    unittest.main()
