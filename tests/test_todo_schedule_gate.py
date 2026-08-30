import sys
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch


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
from app.api.agent_client import _runnable_todo_payload  # noqa: E402
from app.models import (  # noqa: E402
    ClawTodo,
    ClawTodoLog,
    OpenClawInstance,
    User,
    hash_token,
)
from app.services.todo_schedule import todo_schedule_state  # noqa: E402


class TodoScheduleGateTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SECRET_KEY='todo-schedule-test',
            TESTING=True,
        )
        db.init_app(self.app)
        self.app.register_blueprint(api_bp, url_prefix='/api/v1')
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.owner = User(username='todo_owner', role='super_admin')
        self.owner.set_password('secret')
        self.claw = OpenClawInstance(
            name='小橙', safe_name='orange', claw_tag='claw-orange',
            owner='todo_owner', status='工作',
            api_token_hash=hash_token('claw47-test-token'))
        db.session.add_all([self.owner, self.claw])
        db.session.flush()
        self.daily = ClawTodo(
            openclaw_id=self.claw.id,
            title='每日完整冒烟测试',
            schedule_type='daily', schedule_time='10:10',
            urgency_level='interrupt', enabled=True,
            created_at=datetime(2026, 8, 24, 16, 45, 18),
        )
        self.weekly = ClawTodo(
            openclaw_id=self.claw.id,
            title='每周清理',
            schedule_type='weekly', schedule_day=5, schedule_time='22:30',
            urgency_level='interrupt', enabled=True,
            created_at=datetime(2026, 8, 20, 10, 0, 0),
        )
        self.once = ClawTodo(
            openclaw_id=self.claw.id,
            title='历史一次性整改',
            schedule_type='once', schedule_time=None,
            urgency_level='flexible', enabled=True,
            created_at=datetime(2026, 8, 24, 18, 0, 0),
        )
        db.session.add_all([self.daily, self.weekly, self.once])
        db.session.commit()
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['user_id'] = self.owner.id

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_daily_is_scheduled_before_china_due_time(self):
        state = todo_schedule_state(
            self.daily, now=datetime(2026, 8, 25, 0, 0, 5))
        self.assertFalse(state['is_due'])
        self.assertEqual(state['today_status'], 'scheduled')
        self.assertEqual(state['timezone'], 'Asia/Shanghai')
        self.assertEqual(state['due_at'], '2026-08-25T10:10:00+08:00')

        due = todo_schedule_state(
            self.daily, now=datetime(2026, 8, 25, 10, 10, 0))
        self.assertTrue(due['is_due'])
        self.assertEqual(due['today_status'], 'pending')

    def test_sse_payload_is_silent_until_due_time(self):
        with patch(
                'app.services.todo_schedule.cst_now_naive',
                return_value=datetime(2026, 8, 25, 0, 0, 5)):
            self.assertIsNone(_runnable_todo_payload(self.daily))
        with patch(
                'app.services.todo_schedule.cst_now_naive',
                return_value=datetime(2026, 8, 25, 10, 10, 0)):
            payload = _runnable_todo_payload(self.daily)
        self.assertEqual(payload['id'], self.daily.id)
        self.assertTrue(payload['is_due'])
        self.assertEqual(payload['timezone'], 'Asia/Shanghai')

    def test_weekly_wrong_day_is_not_due_and_overdue_once_remains_due(self):
        now = datetime(2026, 8, 25, 12, 0, 0)  # Tuesday
        weekly = todo_schedule_state(self.weekly, now=now)
        once = todo_schedule_state(self.once, now=now)
        self.assertFalse(weekly['is_due'])
        self.assertEqual(weekly['today_status'], 'scheduled')
        self.assertEqual(weekly['due_at'], '2026-08-28T22:30:00+08:00')
        self.assertTrue(once['is_due'])
        self.assertTrue(once['is_overdue'])

    def test_agent_list_hides_not_due_but_management_can_include_it(self):
        fixed_now = datetime(2026, 8, 25, 0, 0, 5)
        with self.client.session_transaction() as session:
            session.clear()
        with patch('app.services.todo_schedule.cst_now_naive', return_value=fixed_now):
            agent_view = self.client.get(
                f'/api/v1/openclaws/{self.claw.id}/todos?enabled_only=false',
                headers={'Authorization': 'Bearer claw47-test-token'})
            with self.client.session_transaction() as session:
                session['user_id'] = self.owner.id
            management_view = self.client.get(
                f'/api/v1/openclaws/{self.claw.id}/todos'
                '?enabled_only=false&include_not_due=true')

        self.assertEqual(agent_view.status_code, 200)
        self.assertEqual([item['id'] for item in agent_view.get_json()], [self.once.id])
        self.assertEqual(management_view.status_code, 200)
        by_id = {item['id']: item for item in management_view.get_json()}
        self.assertEqual(set(by_id), {self.daily.id, self.weekly.id, self.once.id})
        self.assertEqual(by_id[self.daily.id]['today_status'], 'scheduled')
        self.assertFalse(by_id[self.daily.id]['is_due'])

    def test_complete_rejects_early_execution_and_accepts_at_due_time(self):
        with patch(
                'app.api.todos.cst_now_naive',
                return_value=datetime(2026, 8, 25, 0, 0, 5)):
            early = self.client.post(
                f'/api/v1/openclaws/{self.claw.id}/todos/{self.daily.id}/complete',
                json={'result_summary': '不应执行'})
        self.assertEqual(early.status_code, 409, early.get_json())
        self.assertEqual(early.get_json()['code'], 'TODO_NOT_DUE')
        self.assertEqual(ClawTodoLog.query.filter_by(todo_id=self.daily.id).count(), 0)

        with patch(
                'app.api.todos.cst_now_naive',
                return_value=datetime(2026, 8, 25, 10, 10, 1)):
            accepted = self.client.post(
                f'/api/v1/openclaws/{self.claw.id}/todos/{self.daily.id}/complete',
                json={'result_summary': '到点执行'})
        self.assertEqual(accepted.status_code, 200, accepted.get_json())
        log = ClawTodoLog.query.filter_by(todo_id=self.daily.id).one()
        self.assertEqual(log.log_date.isoformat(), '2026-08-25')
        self.assertEqual(log.status, 'submitted')

    def test_heartbeat_does_not_advertise_future_interrupt_todo(self):
        headers = {'Authorization': 'Bearer claw47-test-token'}
        fixed_now = datetime(2026, 8, 25, 0, 0, 5)
        with patch('app.api.openclaws.cst_now_naive', return_value=fixed_now), patch(
                'app.services.todo_schedule.cst_now_naive', return_value=fixed_now):
            response = self.client.post(
                f'/api/v1/openclaws/{self.claw.id}/heartbeat',
                json={'status': '工作'}, headers=headers)

        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload['timezone'], 'Asia/Shanghai')
        self.assertTrue(payload['server_time'].endswith('+08:00'))
        self.assertEqual(payload['todos']['pending'], 1)  # 仅逾期 once
        self.assertEqual(payload['todos']['interrupt'], [])

    def test_dashboard_distinguishes_today_scheduled_from_wrong_day(self):
        fixed_now = datetime(2026, 8, 25, 0, 0, 5)
        with patch('app.api.dashboard.cst_now_naive', return_value=fixed_now), patch(
                'app.services.todo_schedule.cst_now_naive', return_value=fixed_now):
            response = self.client.get('/api/v1/dashboard/stats')

        self.assertEqual(response.status_code, 200, response.get_json())
        upcoming = {item['id']: item for item in response.get_json()['upcoming_todos']}
        self.assertIn(self.daily.id, upcoming)
        self.assertEqual(upcoming[self.daily.id]['today_status'], 'scheduled')
        self.assertFalse(upcoming[self.daily.id]['is_due'])
        self.assertNotIn(self.weekly.id, upcoming)


if __name__ == '__main__':
    unittest.main()
