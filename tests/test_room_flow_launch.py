"""Owner-to-Agent team room Flow launch is bound and idempotent."""

import copy
import unittest

import test_agent_teams_api as team_fixtures
from app import db
from app.models import ChatRoom, ChatRoomDelivery, ChatRoomMember, WorkflowRun
from app.services.chat_rooms import pending_agent_events


class RoomFlowLaunchTest(unittest.TestCase):
    setUp = team_fixtures.AgentTeamsApiTest.setUp
    tearDown = team_fixtures.AgentTeamsApiTest.tearDown
    _headers = team_fixtures.AgentTeamsApiTest._headers
    _setup_team = team_fixtures.AgentTeamsApiTest._setup_team
    _definition = team_fixtures.AgentTeamsApiTest._definition
    _login_admin = team_fixtures.AgentTeamsApiTest._login_admin

    def _question(self, mention_ids):
        self._setup_team()
        self.app.config['CHAT_ROOM_ENABLED'] = True
        definition = copy.deepcopy(self.flow_a.definition_json)
        definition['steps'][0]['target_claw_id'] = self.other_claw.id
        self.flow_a.definition_json = definition
        self.flow_a.executor_acl_json = {
            'user_ids': [self.admin.id], 'claw_ids': [self.other_claw.id]}
        db.session.commit()
        response = self.client.post(
            f'/api/v1/agent-teams/{self.team_id}/chat-room/messages',
            headers={'Idempotency-Key': 'owner-start-flow-55'},
            json={'content': '小魏，请跑 Flow #55',
                  'mention_claw_ids': mention_ids or [],
                  'mention_all': mention_ids is None})
        self.assertEqual(response.status_code, 201, response.get_json())
        message = response.get_json()['message']
        room_id = message['room_id']
        delivery = (ChatRoomDelivery.query.join(
            ChatRoomMember, ChatRoomDelivery.member_id == ChatRoomMember.id)
            .filter(ChatRoomDelivery.message_id == message['id'],
                    ChatRoomMember.claw_id == self.other_claw.id).first())
        return room_id, message['id'], delivery

    def _request(self, room_id, message_id, delivery_id, definition_id=None):
        definition_id = definition_id or self.flow_a.id
        return self.client.post(
            f'/api/v1/chat-rooms/{room_id}/deliveries/{delivery_id}/workflow-runs',
            headers=dict(self._headers(self.other_token), **{
                'Idempotency-Key': f'room-flow:{room_id}:{message_id}'}),
            json={
                'definition_id': definition_id,
                'reason': 'Owner 请求执行',
                'trigger_source': 'hub_team_room',
                'correlation_id': f'hub-room:{room_id}:{message_id}',
                'worker_claw_id': self.other_claw.id,
                'executor_claw_ids': [self.other_claw.id],
                'start_vars': {
                    'worker_claw_id': self.other_claw.id,
                    'executor_claw_ids': [self.other_claw.id],
                },
            })

    def test_direct_mention_creates_one_readable_run(self):
        room_id, message_id, delivery = self._question([self.other_claw.id])
        self.assertIsNotNone(delivery)
        with self.client.session_transaction() as session:
            session.clear()
        first = self._request(room_id, message_id, delivery.id)
        self.assertIn(first.status_code, (200, 201), first.get_json())
        run_id = first.get_json().get('id') or first.get_json().get('run_id')
        self.assertIsInstance(run_id, int)
        second = self._request(room_id, message_id, delivery.id)
        self.assertIn(second.status_code, (200, 201), second.get_json())
        self.assertEqual(second.get_json().get('id') or second.get_json().get('run_id'), run_id)
        self.assertEqual(WorkflowRun.query.filter_by(
            correlation_id=f'hub-room:{room_id}:{message_id}').count(), 1)
        run_context = db.session.get(WorkflowRun, run_id).context_json
        self.assertEqual(run_context['workflow_start']['worker_claw_id'],
                         self.other_claw.id)
        readback = self.client.get(
            f'/api/v1/workflow-runs/{run_id}',
            headers=self._headers(self.other_token))
        self.assertEqual(readback.status_code, 200, readback.get_json())
        manager_member_id = db.session.get(ChatRoom, room_id).owner_member_id
        reply = self.client.post(
            f'/api/v1/chat-rooms/{room_id}/messages',
            headers=dict(self._headers(self.other_token), **{
                'Idempotency-Key': 'room-flow-receipt-1'}),
            json={
                'content': f'Flow 已启动，Run #{run_id}',
                'reply_to_message_id': message_id,
                'origin_delivery_id': delivery.id,
                'mentions': [{'type': 'member',
                              'member_id': manager_member_id}],
            })
        self.assertEqual(reply.status_code, 201, reply.get_json())
        manager_events = [value for kind, value in
                          pending_agent_events(self.main_claw.id)
                          if kind == 'room_message']
        followup = next(value for value in manager_events
                        if value['message']['id'] == reply.get_json()['id'])
        self.assertEqual(
            followup['message']['verified_flow_followup']['run_id'], run_id)

    def test_wrong_identity_and_non_mention_cannot_launch(self):
        room_id, message_id, delivery = self._question(None)
        self.assertIsNotNone(delivery)
        with self.client.session_transaction() as session:
            session.clear()
        denied = self._request(room_id, message_id, delivery.id)
        self.assertEqual(denied.status_code, 403, denied.get_json())
        self.assertEqual(WorkflowRun.query.filter_by(
            correlation_id=f'hub-room:{room_id}:{message_id}').count(), 0)
        manager_member_id = db.session.get(ChatRoom, room_id).owner_member_id
        fake = self.client.post(
            f'/api/v1/chat-rooms/{room_id}/messages',
            headers=dict(self._headers(self.other_token), **{
                'Idempotency-Key': 'fake-room-flow-receipt'}),
            json={
                'content': 'Flow #55 Run #999 已启动',
                'reply_to_message_id': message_id,
                'origin_delivery_id': delivery.id,
                'mentions': [{'type': 'member',
                              'member_id': manager_member_id}],
            })
        self.assertEqual(fake.status_code, 201, fake.get_json())
        manager_events = [value for kind, value in
                          pending_agent_events(self.main_claw.id)
                          if kind == 'room_message']
        fake_event = next(value for value in manager_events
                          if value['message']['id'] == fake.get_json()['id'])
        self.assertNotIn('verified_flow_followup', fake_event['message'])


if __name__ == '__main__':
    unittest.main()
