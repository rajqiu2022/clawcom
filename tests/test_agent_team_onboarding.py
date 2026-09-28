"""Joining a team queues one durable skill invitation per newly joined Claw."""
import copy
import unittest
from unittest.mock import patch

import test_agent_teams_api as fixtures
from app import db
from app.models import AgentTeam, ClawMessage, OpenClawInstance, OpenClawSkill, Skill
from app.services.agent_team_onboarding import queue_missing_join_notifications


class TeamOnboardingTest(unittest.TestCase):
    setUp = fixtures.AgentTeamsApiTest.setUp
    tearDown = fixtures.AgentTeamsApiTest.tearDown
    _definition = fixtures.AgentTeamsApiTest._definition
    _login_admin = fixtures.AgentTeamsApiTest._login_admin
    _headers = fixtures.AgentTeamsApiTest._headers
    _setup_team = fixtures.AgentTeamsApiTest._setup_team

    def _messages(self):
        return ClawMessage.query.filter_by(sender_name='Hub Agent Teams').order_by(ClawMessage.id).all()

    def _save(self, **changes):
        body = copy.deepcopy(self.config)
        body.update(changes)
        body.setdefault('expected_version', db.session.get(AgentTeam, self.team_id).version)
        return self.client.put('/api/v1/agent-teams/%s' % self.team_id, json=body)

    def test_create_notifies_every_unique_member_with_approved_skill_links(self):
        for name in ('agent-team-collaboration', 'agent-team-member-activity'):
            db.session.add(Skill(name=name, display_name=name, review_status='approved', visibility='public'))
        db.session.commit()
        with patch('app.api.agent_client.notify_claw') as wake:
            self._setup_team()
        messages = self._messages()
        ids = {self.main_claw.id, self.other_claw.id, self.backup.id}
        self.assertEqual(len(messages), 3)
        self.assertEqual({m.claw_id for m in messages}, ids)
        self.assertEqual({c.args[0] for c in wake.call_args_list}, ids)
        for message in messages:
            self.assertIn('claw_id=%s' % message.claw_id, message.content)
            self.assertIn('身份不一致或工具不可用时停止', message.content)
            self.assertEqual((message.status, message.direction, message.msg_type), ('pending', 'to_claw', 'text'))
            self.assertIn('手游回归团队', message.content)
            self.assertIn('/agent-teams?project_id=', message.content)
            self.assertIn('安装', message.content)
            self.assertIn('正式安装由 Worker 控制面完成', message.content)
            self.assertIn('普通消息 done 不代表安装成功', message.content)
            for skill in Skill.query.all():
                self.assertIn('/api/v1/skills/%s/raw' % skill.id, message.content)
        self.assertEqual(OpenClawSkill.query.count(), 0)

    def test_context_contains_only_own_teams_and_roles_not_display_names(self):
        from app.services.agent_team_context import build_team_context
        from app.services.agent_system_context import build_agent_system_context
        self._setup_team()
        self.other_claw.name = '旧显示名-高级测试经理'
        db.session.commit()
        context = build_team_context(self.other_claw)
        self.assertEqual(len(context), 1)
        self.assertEqual(context[0]['objective'], '修复验证')
        self.assertEqual(context[0]['primary_manager_claw_id'], self.main_claw.id)
        self.assertIn('test_executor', context[0]['self']['roles'])
        self.assertNotIn('primary_manager', context[0]['self']['roles'])
        self.assertEqual(context[0]['shared_resources']['mode'], 'on_demand')
        self.assertIn('/shared-resources', context[0]['shared_resources']['manifest_api'])
        self.assertEqual(
            context[0]['test_plans']['task_reference_contract']['read_policy'],
            'on_demand_before_execution')
        self.assertIn(
            'reference_skill_ids',
            context[0]['test_plans']['task_reference_contract']['fields'])
        payload = build_agent_system_context(self.other_claw, 'codebuddy', [], [], agent_teams=context)
        rule = next(r for r in payload['system_context']['rules'] if r['name'] == 'agent_team_identity')
        self.assertIn('不得由名字推断经理', rule['content'])
        self.assertNotIn('manager_session_id', rule['content'])
        outsider = OpenClawInstance(name='旁观者', safe_name='observer', claw_tag='observer',
                                   project_id=self.project.id, owner='other')
        db.session.add(outsider)
        db.session.commit()
        self.assertEqual(build_team_context(outsider), [])
        self.app.config['AGENT_TEAMS_ENABLED'] = False
        self.assertEqual(build_team_context(self.other_claw), [])

    def test_unchanged_roster_role_change_and_pause_do_not_repeat(self):
        self._setup_team()
        self.assertEqual(self._save().status_code, 200)
        self.assertEqual(self._save(objective='新目标').status_code, 200)
        self.assertEqual(self._save(primary_manager_claw_id=self.backup.id,
                                    backup_manager_claw_id=self.main_claw.id).status_code, 200)
        self.assertEqual(self._save(status='paused').status_code, 200)
        self.assertEqual(len(self._messages()), 3)

    def test_backfill_is_idempotent_and_keeps_existing_join_messages(self):
        self._setup_team()
        team = db.session.get(AgentTeam, self.team_id)
        self.assertEqual(queue_missing_join_notifications(team), [])
        # Simulate a historical team whose existing member never got a notice.
        ClawMessage.query.filter_by(claw_id=self.other_claw.id).delete()
        db.session.commit()
        self.assertEqual(queue_missing_join_notifications(team), [self.other_claw.id])
        db.session.commit()
        self.assertEqual(queue_missing_join_notifications(team), [])
        self.assertEqual(len(self._messages()), 3)

    def test_only_new_member_notified_and_rejoin_gets_new_notice(self):
        self._setup_team()
        newbie = OpenClawInstance(name='新成员', safe_name='new-member', claw_tag='new-member',
                                  owner='other', project_id=self.project.id, status='offline')
        db.session.add(newbie)
        db.session.commit()
        members = self.config['members'] + [{'claw_id': newbie.id, 'role_key': 'code_analyst'}]
        self.assertEqual(self._save(members=members).status_code, 200)
        self.assertEqual(len(self._messages()), 4)
        self.assertEqual(self._messages()[-1].claw_id, newbie.id)
        self.assertEqual(self._save().status_code, 200)  # removes newcomer
        self.assertEqual(len(self._messages()), 4)
        self.assertEqual(self._save(members=members).status_code, 200)
        self.assertEqual(len(self._messages()), 5)

    def test_rejected_update_and_duplicate_create_do_not_queue(self):
        self._setup_team()
        self.assertEqual(self._save().status_code, 200)
        self.assertEqual(self._save(expected_version=1).status_code, 409)
        self.assertEqual(self.client.post('/api/v1/agent-teams', json=self.config).status_code, 409)
        self.assertEqual(len(self._messages()), 3)

    def test_unauthorized_update_and_foreign_member_send_nothing(self):
        self._setup_team()
        body = dict(self.config, expected_version=1)
        response = self.client.put('/api/v1/agent-teams/%s' % self.team_id,
                                   json=body, headers=self._headers())
        self.assertEqual(response.status_code, 403)
        outsider = OpenClawInstance(name='外项目', safe_name='outsider', claw_tag='outsider',
                                    owner='other', project_id=self.other_project.id)
        db.session.add(outsider)
        db.session.commit()
        members = self.config['members'] + [{'claw_id': outsider.id, 'role_key': 'code_analyst'}]
        self.assertEqual(self._save(members=members).status_code, 400)
        self.assertEqual(len(self._messages()), 3)

    def test_queue_failure_rolls_back_team_and_does_not_wake(self):
        self._setup_team()
        self.config['name'] = '应回滚的团队'
        with patch('app.api.agent_teams.queue_join_notifications', side_effect=RuntimeError('queue failed')):
            with patch('app.api.agent_client.notify_claw') as wake:
                with self.assertRaises(RuntimeError):
                    self.client.post('/api/v1/agent-teams', json=self.config)
                db.session.rollback()
                wake.assert_not_called()
        self.assertEqual(AgentTeam.query.count(), 1)
        self.assertEqual(len(self._messages()), 3)

    def test_wakeup_failure_keeps_committed_pending_messages(self):
        with patch('app.api.agent_client.notify_claw', side_effect=RuntimeError('offline')) as wake:
            self._setup_team()
        self.assertEqual(AgentTeam.query.count(), 1)
        self.assertEqual(wake.call_count, 3)
        self.assertEqual(len(self._messages()), 3)
        self.assertTrue(all(m.status == 'pending' for m in self._messages()))

    def test_unpublished_skill_has_no_download_link_and_does_not_block_join(self):
        db.session.add(Skill(name='agent-team-collaboration', display_name='不应透露的私有标题',
                             visibility='private', review_status='approved'))
        db.session.add(Skill(name='agent-team-member-activity', display_name='待审',
                             review_status='pending', visibility='public'))
        db.session.commit()
        self._setup_team()
        for message in self._messages():
            self.assertIn('联系团队管理员', message.content)
            self.assertNotIn('/raw', message.content)
            self.assertNotIn('不应透露的私有标题', message.content)


if __name__ == '__main__':
    unittest.main()
