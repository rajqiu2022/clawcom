import sys
import types
import unittest
from datetime import timedelta
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
from app.models import (OpenClawInstance, Skill, SkillFile,  # noqa: E402
                        SkillUsageEvent, _now, hash_token)


class SkillUsageHeatApiTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SECRET_KEY='test-secret',
            TESTING=True,
        )
        db.init_app(self.app)
        self.app.register_blueprint(api_bp, url_prefix='/api/v1')
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()

        self.token = 'hub_tk_heat_test'
        self.claw = OpenClawInstance(
            name='heat-reader',
            safe_name='heat-reader',
            claw_tag='heat-reader',
            owner='tester',
            api_token_hash=hash_token(self.token),
        )
        self.skill = Skill(
            name='heat-demo',
            display_name='Heat Demo',
            description='usage heat test',
            template_content='# Complete skill',
            review_status='approved',
        )
        db.session.add_all([self.claw, self.skill])
        db.session.flush()
        db.session.add(SkillFile(
            skill_id=self.skill.id,
            filename='SKILL.md',
            content='# Complete skill',
        ))
        db.session.commit()
        self.headers = {'Authorization': f'Bearer {self.token}'}

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_market_list_is_summary_and_reports_recent_three_day_count(self):
        db.session.add_all([
            SkillUsageEvent(skill_id=self.skill.id, access_type='detail',
                            accessed_at=_now() - timedelta(hours=71)),
            SkillUsageEvent(skill_id=self.skill.id, access_type='detail',
                            accessed_at=_now() - timedelta(hours=73)),
        ])
        db.session.commit()

        response = self.client.get('/api/v1/skills?summary=true', headers=self.headers)

        self.assertEqual(response.status_code, 200)
        item = response.get_json()[0]
        self.assertNotIn('template_content', item)
        self.assertNotIn('mirror_content', item)
        self.assertNotIn('content_history', item)
        self.assertEqual(item['recent_usage_count'], 1)

    def test_each_successful_full_content_api_read_records_once(self):
        urls = [
            f'/api/v1/skills/{self.skill.id}',
            f'/api/v1/skills/{self.skill.id}/raw',
            f'/api/v1/skills/{self.skill.id}/files',
            f'/api/v1/skills/{self.skill.id}/files/SKILL.md',
            f'/api/v1/skills/{self.skill.id}/pack',
        ]

        for url in urls:
            response = self.client.get(url, headers=self.headers)
            self.assertEqual(response.status_code, 200, url)

        events = SkillUsageEvent.query.filter_by(skill_id=self.skill.id).all()
        self.assertEqual(len(events), len(urls))
        self.assertEqual(
            {event.access_type for event in events},
            {'detail', 'raw', 'file_list', 'file', 'pack'},
        )

        market = self.client.get('/api/v1/skills?summary=true', headers=self.headers)
        self.assertEqual(market.get_json()[0]['recent_usage_count'], 5)

    def test_legacy_full_list_is_audited_but_excluded_from_heat(self):
        response = self.client.get('/api/v1/skills', headers=self.headers)

        self.assertEqual(response.status_code, 200)
        item = response.get_json()[0]
        self.assertEqual(item['template_content'], '# Complete skill')
        self.assertEqual(item['recent_usage_count'], 0)
        event = SkillUsageEvent.query.one()
        self.assertEqual(event.access_type, 'list_full')

    def test_bulk_list_batches_do_not_inflate_targeted_usage(self):
        now = _now()
        db.session.add_all([
            SkillUsageEvent(
                skill_id=self.skill.id,
                access_type='list_full',
                accessed_at=now - timedelta(minutes=index),
            )
            for index in range(32)
        ] + [
            SkillUsageEvent(
                skill_id=self.skill.id,
                access_type='detail',
                accessed_at=now - timedelta(hours=1),
            ),
            SkillUsageEvent(
                skill_id=self.skill.id,
                access_type='agent_pack',
                accessed_at=now - timedelta(hours=2),
            ),
        ])
        db.session.commit()

        response = self.client.get(
            '/api/v1/skills?summary=true', headers=self.headers)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()[0]['recent_usage_count'], 2)
        self.assertEqual(SkillUsageEvent.query.count(), 34)

    def test_missing_content_does_not_record_usage(self):
        response = self.client.get(
            f'/api/v1/skills/{self.skill.id}/files/missing.md',
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(SkillUsageEvent.query.count(), 0)


class SkillHeatFrontendContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = (_WEB / 'templates' / 'skills.html').read_text(
            encoding='utf-8')

    def test_heat_thresholds_and_tooltip_are_present(self):
        self.assertIn('function skillHeatLevel(count)', self.template)
        self.assertIn("count <= 5", self.template)
        self.assertIn("count <= 10", self.template)
        self.assertIn("count <= 15", self.template)
        self.assertIn("count <= 20", self.template)
        self.assertIn('最近 3 天使用 ${count} 次', self.template)
        self.assertIn('skill-heat--light-green', self.template)
        self.assertIn('skill-heat--dark-green', self.template)
        self.assertIn('skill-heat--blue', self.template)
        self.assertIn('skill-heat--orange', self.template)
        self.assertIn('skill-heat--red', self.template)


if __name__ == '__main__':
    unittest.main()
