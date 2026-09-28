import sys
import io
import tempfile
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
    AgentTeam,
    ChatRoom,
    ChatRoomDelivery,
    ChatRoomImage,
    ChatRoomGuestSession,
    ChatRoomMember,
    ChatRoomMessage,
    OpenClawInstance,
    Project,
    User,
    WorkflowDefinition,
    WorkflowRun,
    hash_token,
)
from app.services.chat_rooms import pending_agent_events  # noqa: E402


class ChatRoomsApiTest(unittest.TestCase):
    def setUp(self):
        self.image_dir = tempfile.TemporaryDirectory()
        self.app = Flask(__name__)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SECRET_KEY='room-test',
            TESTING=True,
            CHAT_ROOM_ENABLED=True,
            CHAT_ROOM_GUEST_TOKEN_MINUTES=2880,
            CHAT_ROOM_GUEST_TOKEN_MAX_MINUTES=10080,
            HUB_PUBLIC_URL='http://clawteam.woa.com:18800',
            CHAT_ROOM_IMAGE_DIR=self.image_dir.name,
            CHAT_ROOM_IMAGE_MAX_BYTES=8 * 1024 * 1024,
            CHAT_ROOM_IMAGE_MAX_COUNT=4,
        )
        db.init_app(self.app)
        self.app.register_blueprint(api_bp, url_prefix='/api/v1')
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self._seed()
        self.client = self.app.test_client()
        self._login(self.owner.id)

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()
        self.image_dir.cleanup()

    def _seed(self):
        self.project = Project(name='RacingGO')
        db.session.add(self.project)
        db.session.flush()
        self.owner = User(username='room_owner', display_name='房主', role='super_admin')
        self.owner.set_password('secret')
        self.reviewer = User(
            username='reviewer', display_name='评审人', role='user',
            managed_projects=[self.project.id])
        self.reviewer.set_password('secret')
        db.session.add_all([self.owner, self.reviewer])
        db.session.flush()
        self.agent_token = 'agent-room-token'
        self.agent = OpenClawInstance(
            name='小牛', safe_name='xiaoniu-room', claw_tag='claw-room-xiaoniu',
            owner=self.owner.username, role='module_owner', project_id=self.project.id,
            api_token_hash=hash_token(self.agent_token), status='工作')
        db.session.add(self.agent)
        db.session.commit()

    def _login(self, user_id):
        with self.client.session_transaction() as sess:
            sess.clear()
            sess['user_id'] = user_id

    def _create_room(self):
        response = self.client.post('/api/v1/chat-rooms', json={
            'title': 'Flow #25 冷机验收',
            'description': 'Hub 与 Agent 协作',
            'project_id': self.project.id,
        })
        self.assertEqual(response.status_code, 201, response.get_json())
        room = response.get_json()
        self.client.post(f"/api/v1/chat-rooms/{room['id']}/members", json={
            'type': 'user', 'id': self.reviewer.id})
        add_agent = self.client.post(
            f"/api/v1/chat-rooms/{room['id']}/members",
            json={'type': 'agent', 'id': self.agent.id})
        self.assertEqual(add_agent.status_code, 201, add_agent.get_json())
        return room, add_agent.get_json()

    def _create_team_flow_instruction(self, *, sender_id=None, mention=True):
        self._login(self.owner.id)
        room, agent_member = self._create_room()
        manager = OpenClawInstance(
            name='小策', safe_name=f'room-manager-{room["id"]}',
            claw_tag=f'claw-room-manager-{room["id"]}',
            owner=self.owner.username, role='test_manager',
            project_id=self.project.id,
            api_token_hash=hash_token(f'room-manager-token-{room["id"]}'),
            status='工作')
        db.session.add(manager)
        db.session.flush()
        manager_response = self.client.post(
            f'/api/v1/chat-rooms/{room["id"]}/members',
            json={'type': 'agent', 'id': manager.id})
        self.assertEqual(manager_response.status_code, 201,
                         manager_response.get_json())
        team = AgentTeam(
            project_id=self.project.id, name=f'room-team-{room["id"]}',
            objective='test', primary_manager_claw_id=manager.id,
            policy_json={})
        definition = WorkflowDefinition(
            workflow_key=f'room-flow-{room["id"]}', name='Room Flow',
            project_id=self.project.id,
            definition_json={'name': 'Room Flow', 'steps': []},
            owner_type='user', owner_id=self.owner.id,
            executor_acl_json={
                'claw_ids': [self.agent.id],
                'user_ids': [self.owner.id],
            },
            visibility_scope='project')
        db.session.add_all([team, definition])
        db.session.flush()
        db.session.get(ChatRoom, room['id']).team_id = team.id
        db.session.commit()
        self._login(sender_id or self.owner.id)
        sent = self.client.post(
            f'/api/v1/chat-rooms/{room["id"]}/messages',
            json={
                'content': '请运行这个 Flow',
                'mentions': ([{'type': 'member', 'member_id': agent_member['id']}]
                             if mention else []),
            },
            headers={'Idempotency-Key': f'room-flow-message-{room["id"]}'})
        self.assertEqual(sent.status_code, 201, sent.get_json())
        delivery = ChatRoomDelivery.query.filter_by(
            message_id=sent.get_json()['id'], member_id=agent_member['id']).one()
        self._login(None)
        return room, definition, delivery, sent.get_json()

    def _start_room_flow(self, room, definition, delivery, message, **overrides):
        body = {
            'definition_id': definition.id,
            'reason': 'Owner明确要求运行',
            'trigger_source': 'hub_team_room',
            'correlation_id': f'hub-room:{room["id"]}:{message["id"]}',
            'worker_claw_id': self.agent.id,
            'executor_claw_ids': [self.agent.id],
            'start_vars': {'worker_claw_id': self.agent.id},
        }
        body.update(overrides)
        return self.client.post(
            f'/api/v1/chat-rooms/{room["id"]}/deliveries/{delivery.id}/workflow-runs',
            json=body,
            headers={
                'Authorization': f'Bearer {self.agent_token}',
                'Idempotency-Key': f'room-flow:{room["id"]}:{message["id"]}',
            })

    def test_owner_direct_mention_starts_one_verified_room_flow(self):
        room, definition, delivery, message = self._create_team_flow_instruction()
        event = next(payload for name, payload in pending_agent_events(self.agent.id)
                     if name == 'room_message')
        self.assertEqual(delivery.member_id, event['delivery']['member_id'])
        team = db.session.get(AgentTeam, db.session.get(ChatRoom, room['id']).team_id)
        manager_member_id = event['team_authority']['manager_member_id']
        self.assertIsInstance(manager_member_id, int)
        self.assertNotEqual(room['owner_member_id'], manager_member_id)
        first = self._start_room_flow(room, definition, delivery, message)
        self.assertEqual(first.status_code, 201, first.get_json())
        reply = self.client.post(
            f'/api/v1/chat-rooms/{room["id"]}/messages',
            json={
                'content': f'Run #{first.get_json()["id"]} 已启动。',
                'reply_to_message_id': message['id'],
                'origin_delivery_id': delivery.id,
                'mentions': [{
                    'type': 'member', 'member_id': manager_member_id,
                }],
            },
            headers={
                'Authorization': f'Bearer {self.agent_token}',
                'Idempotency-Key': 'room-flow-reply',
            })
        self.assertEqual(reply.status_code, 201, reply.get_json())
        manager_event = next(
            payload for name, payload in pending_agent_events(
                team.primary_manager_claw_id) if name == 'room_message')
        self.assertEqual(
            {'run_id': first.get_json()['id'],
             'definition_id': definition.id},
            manager_event['message']['verified_flow_followup'])
        definition.definition_json = {
            'name': 'Room Flow (new version)', 'steps': [],
        }
        definition.version = 2
        db.session.commit()
        second = self._start_room_flow(room, definition, delivery, message)
        self.assertEqual(second.status_code, 200, second.get_json())
        self.assertEqual(first.get_json()['id'], second.get_json()['id'])
        another = WorkflowDefinition(
            workflow_key='room-flow-other', name='Other Room Flow',
            project_id=self.project.id,
            definition_json={'name': 'Other Room Flow', 'steps': []},
            owner_type='user', owner_id=self.owner.id,
            executor_acl_json={'claw_ids': [self.agent.id]},
            visibility_scope='project')
        db.session.add(another)
        db.session.commit()
        conflict = self._start_room_flow(room, another, delivery, message)
        self.assertEqual(conflict.status_code, 409, conflict.get_json())
        self.assertEqual(
            conflict.get_json()['code'], 'ROOM_FLOW_ALREADY_STARTED')
        self.assertEqual(WorkflowRun.query.count(), 1)

    def test_room_flow_requires_owner_mention_and_binding(self):
        for sender_id, mention, expected in (
            (self.owner.id, False, 'ROOM_FLOW_OWNER_MENTION_REQUIRED'),
            (self.reviewer.id, True, 'ROOM_FLOW_OWNER_MENTION_REQUIRED'),
        ):
            with self.subTest(sender_id=sender_id, mention=mention):
                room, definition, delivery, message = (
                    self._create_team_flow_instruction(
                        sender_id=sender_id, mention=mention))
                denied = self._start_room_flow(room, definition, delivery, message)
                self.assertEqual(denied.status_code, 403, denied.get_json())
                self.assertEqual(denied.get_json()['code'], expected)
        room, definition, delivery, message = self._create_team_flow_instruction()
        denied = self._start_room_flow(
            room, definition, delivery, message,
            worker_claw_id=self.agent.id + 1)
        self.assertEqual(denied.status_code, 400, denied.get_json())
        self.assertEqual(denied.get_json()['code'], 'ROOM_FLOW_BINDING_INVALID')
        self.assertEqual(WorkflowRun.query.count(), 0)

    def test_room_owner_role_does_not_grant_flow_execution_acl(self):
        room, definition, delivery, message = (
            self._create_team_flow_instruction(sender_id=self.reviewer.id))
        sender = db.session.get(ChatRoomMember, message['sender_member_id'])
        sender.role = 'owner'
        db.session.commit()
        denied = self._start_room_flow(room, definition, delivery, message)
        self.assertEqual(denied.status_code, 403, denied.get_json())
        self.assertEqual(
            denied.get_json()['code'], 'ROOM_FLOW_SENDER_NOT_AUTHORIZED')
        self.assertEqual(WorkflowRun.query.count(), 0)

    def test_message_idempotency_and_agent_mention_gate(self):
        room, agent_member = self._create_room()
        path = f"/api/v1/chat-rooms/{room['id']}/messages"

        ordinary = self.client.post(path, json={'content': '普通同步'},
                                    headers={'Idempotency-Key': 'ordinary-1'})
        self.assertEqual(ordinary.status_code, 201, ordinary.get_json())
        ordinary_delivery = ChatRoomDelivery.query.filter_by(
            message_id=ordinary.get_json()['id'], member_id=agent_member['id']).one()
        self.assertFalse(ordinary_delivery.notify_agent)

        mentioned = self.client.post(path, json={
            'content': '请核验实时状态',
            'mentions': [{'type': 'member', 'member_id': agent_member['id']}],
        }, headers={'Idempotency-Key': 'mention-1'})
        self.assertEqual(mentioned.status_code, 201, mentioned.get_json())
        replay = self.client.post(path, json={
            'content': '即使正文变化也不能生成第二条',
        }, headers={'Idempotency-Key': 'mention-1'})
        self.assertEqual(replay.status_code, 200, replay.get_json())
        self.assertEqual(replay.get_json()['id'], mentioned.get_json()['id'])
        self.assertEqual(ChatRoomMessage.query.filter_by(
            room_id=room['id'], client_message_id='mention-1').count(), 1)
        mentioned_delivery = ChatRoomDelivery.query.filter_by(
            message_id=mentioned.get_json()['id'], member_id=agent_member['id']).one()
        self.assertTrue(mentioned_delivery.notify_agent)

        events = pending_agent_events(self.agent.id)
        names = [name for name, _ in events]
        self.assertIn('room_invited', names)
        self.assertIn('room_member_changed', names)
        self.assertIn('room_message', names)
        room_message = next(payload for name, payload in events if name == 'room_message')
        self.assertEqual(room_message['message']['id'], mentioned.get_json()['id'])

    def test_all_mention_deduplicates_delivery(self):
        room, agent_member = self._create_room()
        response = self.client.post(
            f"/api/v1/chat-rooms/{room['id']}/messages",
            json={'content': '所有人看一下', 'mentions': [
                {'type': 'all'},
                {'type': 'member', 'member_id': agent_member['id']},
            ]},
            headers={'Idempotency-Key': 'all-1'},
        )
        self.assertEqual(response.status_code, 201, response.get_json())
        self.assertEqual(ChatRoomDelivery.query.filter_by(
            message_id=response.get_json()['id'], member_id=agent_member['id']).count(), 1)

    def test_room_image_upload_bind_read_and_agent_delivery(self):
        room, agent_member = self._create_room()
        png = b'\x89PNG\r\n\x1a\n' + b'test-image-payload'
        uploaded = self.client.post(
            f"/api/v1/chat-rooms/{room['id']}/images",
            data={'file': (io.BytesIO(png), 'evidence.png')},
            content_type='multipart/form-data')
        self.assertEqual(uploaded.status_code, 201, uploaded.get_json())
        image = uploaded.get_json()
        self.assertEqual(image['content_type'], 'image/png')
        self.assertEqual(image['size'], len(png))

        sent = self.client.post(
            f"/api/v1/chat-rooms/{room['id']}/messages",
            json={
                'content': '请结合截图确认状态', 'image_ids': [image['id']],
                'mentions': [{'type': 'member', 'member_id': agent_member['id']}],
            }, headers={'Idempotency-Key': 'image-and-text-1'})
        self.assertEqual(sent.status_code, 201, sent.get_json())
        self.assertEqual(sent.get_json()['message_type'], 'mixed')
        self.assertEqual(sent.get_json()['images'][0]['id'], image['id'])
        self.assertEqual(
            sent.get_json()['images'][0]['agent_url'],
            'http://clawteam.woa.com:18800' + image['url'])
        row = db.session.get(ChatRoomImage, image['id'])
        self.assertEqual(row.message_id, sent.get_json()['id'])
        self.assertEqual(row.status, 'active')

        fetched = self.client.get(image['url'])
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.data, png)
        self.assertEqual(fetched.headers['X-Content-Type-Options'], 'nosniff')
        event = next(payload for name, payload in pending_agent_events(self.agent.id)
                     if name == 'room_message')
        self.assertEqual(event['message']['images'][0]['sha256'], image['sha256'])
        self.assertEqual(
            event['message']['images'][0]['agent_url'],
            sent.get_json()['images'][0]['agent_url'])
        self.assertEqual(event['message']['content'], '请结合截图确认状态')

    def test_room_image_cannot_cross_room_or_bind_twice(self):
        room_a, _ = self._create_room()
        room_b, _ = self._create_room()
        png = b'\x89PNG\r\n\x1a\n' + b'private'
        uploaded = self.client.post(
            f"/api/v1/chat-rooms/{room_a['id']}/images",
            data={'file': (io.BytesIO(png), 'private.png')},
            content_type='multipart/form-data').get_json()
        denied = self.client.post(
            f"/api/v1/chat-rooms/{room_b['id']}/messages",
            json={'content': 'wrong room', 'image_ids': [uploaded['id']]},
            headers={'Idempotency-Key': 'cross-room-image'})
        self.assertEqual(denied.status_code, 400, denied.get_json())
        self.assertEqual(denied.get_json()['code'], 'MESSAGE_IMAGE_NOT_AVAILABLE')
        first = self.client.post(
            f"/api/v1/chat-rooms/{room_a['id']}/messages",
            json={'content': 'first', 'image_ids': [uploaded['id']]},
            headers={'Idempotency-Key': 'bind-image-once'})
        self.assertEqual(first.status_code, 201, first.get_json())
        second = self.client.post(
            f"/api/v1/chat-rooms/{room_a['id']}/messages",
            json={'content': 'second', 'image_ids': [uploaded['id']]},
            headers={'Idempotency-Key': 'bind-image-twice'})
        self.assertEqual(second.status_code, 400, second.get_json())

    def test_invites_are_unique_and_bound_to_each_room(self):
        room_a, _ = self._create_room()
        room_b, _ = self._create_room()

        def issue(room_id):
            response = self.client.post(
                f'/api/v1/chat-rooms/{room_id}/invites',
                json={'label': 'Agent 外部协作'})
            self.assertEqual(response.status_code, 201, response.get_json())
            self.assertEqual(response.get_json()['room_id'], room_id)
            return response.get_json()

        first_a = issue(room_a['id'])
        second_a = issue(room_a['id'])
        first_b = issue(room_b['id'])
        self.assertEqual(len({
            first_a['invite_code'], second_a['invite_code'], first_b['invite_code'],
        }), 3)
        self.assertEqual(len({
            first_a['join_url'], second_a['join_url'], first_b['join_url'],
        }), 3)

    def test_external_guest_exchange_renew_and_history(self):
        room, _ = self._create_room()
        invite = self.client.post(
            f"/api/v1/chat-rooms/{room['id']}/invites",
            json={'label': '外部开发协作'})
        self.assertEqual(invite.status_code, 201, invite.get_json())
        self.assertTrue(invite.get_json()['join_url'].startswith(
            'http://clawteam.woa.com:18800/chat/join#invite=room_ci_'))
        self.assertEqual(invite.get_json()['room_id'], room['id'])
        self.assertIn('no-store', invite.headers.get('Cache-Control', ''))
        self.assertNotIn('web_join_url', invite.get_json())
        self._login(self.owner.id)
        exchange = self.client.post('/api/v1/chat-room-invites/exchange', json={
            'invite_code': invite.get_json()['invite_code'],
            'display_name': '外部开发',
        })
        self.assertEqual(exchange.status_code, 201, exchange.get_json())
        guest = exchange.get_json()
        headers = {'Authorization': f"Bearer {guest['access_token']}"}
        send = self.client.post(
            f"/api/v1/chat-rooms/{room['id']}/messages",
            json={'content': '外部意见'},
            headers={**headers, 'Idempotency-Key': 'guest-message-1'},
        )
        self.assertEqual(send.status_code, 201, send.get_json())

        renew = self.client.post('/api/v1/chat-room-sessions/renew', json={
            'room_id': room['id'], 'member_id': guest['member']['id'],
            'resume_secret': guest['resume_secret'],
        })
        self.assertEqual(renew.status_code, 201, renew.get_json())
        renewed_headers = {'Authorization': f"Bearer {renew.get_json()['access_token']}"}
        history = self.client.get(
            f"/api/v1/chat-rooms/{room['id']}/messages", headers=renewed_headers)
        self.assertEqual(history.status_code, 200, history.get_json())
        self.assertEqual(history.get_json()['items'][-1]['content'], '外部意见')

        denied = self.client.get('/api/v1/projects', headers=renewed_headers)
        self.assertEqual(denied.status_code, 403, denied.get_json())

    def test_external_guest_exchange_rejects_duplicate_active_display_name(self):
        room, _ = self._create_room()
        invite = self.client.post(
            f"/api/v1/chat-rooms/{room['id']}/invites", json={}).get_json()
        first = self.client.post('/api/v1/chat-room-invites/exchange', json={
            'invite_code': invite['invite_code'],
            'display_name': 'Hub Developer',
        })
        duplicate = self.client.post('/api/v1/chat-room-invites/exchange', json={
            'invite_code': invite['invite_code'],
            'display_name': '  hub   developer  ',
        })

        self.assertEqual(first.status_code, 201, first.get_json())
        self.assertEqual(duplicate.status_code, 400, duplicate.get_json())
        self.assertEqual(
            duplicate.get_json()['code'], 'ROOM_DISPLAY_NAME_ALREADY_ACTIVE')
        self.assertEqual(ChatRoomMember.query.filter_by(
            room_id=room['id'], member_type='guest', status='active').count(), 1)

    def test_room_delete_revokes_guest_access(self):
        room, _ = self._create_room()
        invite = self.client.post(f"/api/v1/chat-rooms/{room['id']}/invites", json={})
        guest = self.client.post('/api/v1/chat-room-invites/exchange', json={
            'invite_code': invite.get_json()['invite_code'], 'display_name': '临时成员',
        }).get_json()
        deleted = self.client.delete(f"/api/v1/chat-rooms/{room['id']}")
        self.assertEqual(deleted.status_code, 200, deleted.get_json())
        access = self.client.get(
            f"/api/v1/chat-rooms/{room['id']}",
            headers={'Authorization': f"Bearer {guest['access_token']}"})
        self.assertEqual(access.status_code, 401, access.get_json())
        self.assertEqual(ChatRoomGuestSession.query.filter_by(
            room_id=room['id'], status='active').count(), 0)


class ChatRoomsFrontendContractTest(unittest.TestCase):
    def test_hub_and_guest_pages_expose_room_contract(self):
        hub = (_WEB / 'templates' / 'hub.html').read_text(encoding='utf-8')
        guest = (_WEB / 'templates' / 'chat_room_join.html').read_text(encoding='utf-8')
        self.assertIn('data-view="rooms"', hub)
        self.assertIn('room-message-stream', hub)
        self.assertIn('Idempotency-Key', hub)
        self.assertIn('/chat-room-sessions/renew', guest)
        self.assertIn('resume_secret', guest)
        self.assertIn('min-height:0;overflow:hidden;flex-direction:column', guest)
        self.assertIn('renderedMessageSignature', guest)
        self.assertIn('tabindex="0" aria-label="聊天消息"', guest)
        self.assertIn('.room-stage { min-width:0; min-height:0; overflow:hidden;', hub)


if __name__ == '__main__':
    unittest.main()
