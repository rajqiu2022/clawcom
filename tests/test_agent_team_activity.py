import unittest
from datetime import timedelta

import test_agent_teams_api as fixtures
from app import db
from app.models import (AgentTask, AgentTeamMemberReport, AgentTeamMemberStatus,
                        AgentTeamMemberTask, AgentTeam, _now)


class TeamActivityTest(unittest.TestCase):
    setUp = fixtures.AgentTeamsApiTest.setUp
    tearDown = fixtures.AgentTeamsApiTest.tearDown
    _headers = fixtures.AgentTeamsApiTest._headers
    _definition = fixtures.AgentTeamsApiTest._definition
    _login_admin = fixtures.AgentTeamsApiTest._login_admin
    _setup_team = fixtures.AgentTeamsApiTest._setup_team

    def setup_activity(self):
        self._setup_team()
        self.url = '/api/v1/agent-teams/%s/members/%s/activity' % (self.team_id, self.main_claw.id)
        self.list_url = '/api/v1/agent-teams/%s/members/activity' % self.team_id

    def report(self, version=0, event='start', state='working', task_state='working', **overrides):
        body = {'event_id':event, 'expected_version':version, 'state':state, 'summary':'执行 Flow',
                'task':{'task_key':'run-42', 'title':'执行 XX Flow', 'task_type':'flow',
                        'reference':'Flow #12 / Run #42（自报）', 'status':task_state,
                        'progress_percent':30, 'progress_message':'正在启动编辑器'}}
        body.update(overrides)
        return self.client.post(self.url, headers=self._headers(), json=body)

    def test_unknown_and_deduplicated_roster_connection_is_separate(self):
        self.setup_activity()
        data = self.client.get(self.list_url).get_json()
        self.assertEqual(len(data['items']), 3)  # duplicate roles -> one card per Claw
        self.assertTrue(all(i['effective_state'] == 'unknown' for i in data['items']))
        self.assertTrue(all(i['version'] == 0 for i in data['items']))
        self.assertEqual(data['report_contract']['recommended_interval_seconds'], 60)

    def test_hub_claim_is_authoritative_and_self_report_stays_separate(self):
        self.setup_activity()
        db.session.add(AgentTask(
            task_id='plan-authoritative-1', claw_id=self.main_claw.id,
            task_type='test_plan_agent_task', status='running',
            lease_expires_at=_now() + timedelta(minutes=2),
            last_heartbeat_at=_now()))
        db.session.commit()
        member = self.client.get(self.url).get_json()['member']
        self.assertEqual(member['effective_state'], 'working')
        self.assertEqual(member['source'], 'hub_authoritative_execution')
        self.assertEqual(
            member['authoritative_execution']['source'], 'agent_task_claim')
        self.assertIsNone(member['self_report']['state'])

    def test_agent_can_report_direct_work_without_a_task_instance(self):
        self.setup_activity()
        first = self.report(task=None).get_json()
        self.assertEqual(first['state'], 'working')
        self.assertIsNone(first['task'])
        self.assertEqual(AgentTeamMemberTask.query.count(), 0)
        member = self.client.get(self.url).get_json()['member']
        self.assertEqual(member['effective_state'], 'working')
        self.assertEqual(member['summary'], '执行 Flow')
        self.assertIsNone(member['current_task'])
        blocked = self.report(1, 'environment-blocked', state='blocked',
                              task=None, summary='设备离线，正在检查连接').get_json()
        self.assertEqual(blocked['state'], 'blocked')
        self.assertEqual(self.report(2, 'finished', state='idle', task=None,
                                     summary='本轮排查结束').status_code, 200)
        self.assertEqual(AgentTeamMemberTask.query.count(), 0)

    def test_current_progress_finish_and_history_no_claw_state_mutation(self):
        self.setup_activity()
        old = self.main_claw.status
        self.assertEqual(self.report().status_code, 200)
        self.assertEqual(self.report(1, 'progress', 'blocked', 'blocked').status_code, 200)
        current = self.client.get(self.url).get_json()
        self.assertEqual(current['member']['effective_state'], 'blocked')
        self.assertEqual(current['member']['current_task']['progress_percent'], 30)
        self.assertEqual(current['history']['total'], 0)
        self.assertEqual(len(current['recent_reports']), 2)
        self.assertEqual(self.report(2, 'end', 'idle', 'completed').status_code, 200)
        data = self.client.get(self.url).get_json()
        self.assertIsNone(data['member']['current_task'])
        self.assertEqual(data['member']['effective_state'], 'idle')
        self.assertEqual(data['history']['items'][0]['status'], 'completed')
        self.assertIsNotNone(data['history']['items'][0]['finished_at'])
        db.session.refresh(self.main_claw)
        self.assertEqual(self.main_claw.status, old)

    def test_retry_idempotent_even_after_newer_reports(self):
        self.setup_activity()
        self.report()
        self.report(1, 'progress')
        replay = self.report().get_json()
        self.assertTrue(replay['replayed'])
        self.assertEqual(replay['version'], 1)
        self.assertEqual(AgentTeamMemberReport.query.count(), 2)
        self.assertEqual(AgentTeamMemberTask.query.count(), 1)
        self.assertEqual(self.client.get(self.url).get_json()['member']['version'], 2)
        self.assertEqual(self.report(summary='changed').status_code, 409)

    def test_old_report_and_finished_task_cannot_overwrite(self):
        self.setup_activity()
        self.report()
        self.assertEqual(self.report(0, 'late').get_json()['code'], 'TEAM_ACTIVITY_VERSION_CONFLICT')
        self.report(1, 'finish', 'idle', 'failed')
        self.assertEqual(self.report(2, 'restart-old').get_json()['code'], 'TEAM_ACTIVITY_TASK_FINISHED')
        self.assertEqual(self.client.get(self.url).get_json()['member']['version'], 2)

    def test_self_only_including_admin_and_other_agent(self):
        self.setup_activity()
        payload = {'event_id':'idle', 'expected_version':0, 'state':'idle'}
        self.assertEqual(self.client.post(self.url, json=payload).status_code, 403)  # logged-in admin
        self.assertEqual(self.client.post(self.url, headers=self._headers(self.backup_token), json=payload).status_code, 403)
        self.assertEqual(self.client.post(self.url, headers=self._headers(), json=payload).status_code, 200)
        self.assertEqual(AgentTeamMemberReport.query.count(), 1)

    def test_removed_member_and_cross_project_are_denied(self):
        self.setup_activity()
        team = db.session.get(AgentTeam, self.team_id)
        team.primary_manager_claw_id = self.backup.id
        db.session.commit()
        self.assertEqual(self.report().status_code, 403)
        team.primary_manager_claw_id = self.main_claw.id
        self.main_claw.project_id = self.other_project.id
        db.session.commit()
        self.assertEqual(self.report().status_code, 403)
        self.assertNotIn(self.main_claw.id, [x['claw_id'] for x in self.client.get(self.list_url).get_json()['items']])

    def test_stale_is_not_idle_and_duplicate_does_not_refresh_age(self):
        self.setup_activity()
        self.report(state='idle', task=None)
        row = AgentTeamMemberStatus.query.one()
        row.reported_at = _now() - timedelta(seconds=181)
        db.session.commit()
        self.report(state='idle', task=None)
        member = self.client.get(self.url).get_json()['member']
        self.assertEqual(member['effective_state'], 'stale')
        self.assertEqual(member['reported_state'], 'idle')

    def test_invalid_payloads_do_not_create_rows(self):
        self.setup_activity()
        bad = [dict(state='online'), dict(state='working', task=None, summary=''), dict(expected_version=True),
               dict(event_id='bad key'), dict(summary='x' * 501), dict(worker_claw_id=self.backup.id),
               dict(state='working', task={'task_key':'a','title':'t','task_type':[], 'status':'working'})]
        for changes in bad:
            self.assertEqual(self.report(**changes).status_code, 400, changes)
        self.assertEqual(AgentTeamMemberReport.query.count(), 0)
        self.assertEqual(AgentTeamMemberTask.query.count(), 0)

    def test_active_task_cannot_disappear_or_switch_without_terminal_report(self):
        self.setup_activity()
        self.report()
        self.assertEqual(self.report(1, 'idle', state='idle', task=None).status_code, 400)
        current = self.client.get(self.url).get_json()['member']['current_task']
        new = {k:current[k] for k in ('title','task_type','reference','status','progress_percent','progress_message')}
        new['task_key'] = 'other-task'
        self.assertEqual(self.report(1, 'switch', task=new).get_json()['code'], 'TEAM_ACTIVITY_TASK_ACTIVE')
        self.assertEqual(AgentTeamMemberTask.query.count(), 1)

    def test_pagination_gate_and_no_auth(self):
        self.setup_activity()
        self.assertEqual(self.client.get(self.url + '?limit=0').status_code, 400)
        self.app.config['AGENT_TEAMS_ENABLED'] = False
        self.assertEqual(self.client.get(self.list_url).status_code, 404)
        self.assertEqual(self.report().status_code, 404)
        self.app.config['AGENT_TEAMS_ENABLED'] = True
        with self.client.session_transaction() as session:
            session.clear()
        self.assertEqual(self.client.get(self.list_url).status_code, 401)

    def test_paused_team_allows_observations_without_worker_requirement(self):
        self.setup_activity()
        team = db.session.get(AgentTeam, self.team_id)
        team.status = 'paused'
        db.session.commit()
        self.assertEqual(self.report().status_code, 200)
        self.assertEqual(self.report(1, 'cancel', 'idle', 'cancelled').status_code, 200)
        history = self.client.get(self.url).get_json()['history']
        self.assertEqual(history['items'][0]['status'], 'cancelled')

    def test_task_metadata_is_immutable_and_percent_is_strict(self):
        self.setup_activity()
        task = {'task_key':'bug-1','title':'回归 Bug #1','task_type':'bug_regression','status':'working'}
        for percent in (-1, 101, True, 1.5, '50'):
            self.assertEqual(self.report(task=dict(task, progress_percent=percent)).status_code, 400)
        self.assertEqual(self.report(task=task).status_code, 200)
        self.assertEqual(self.report(1, 'rename', task=dict(task,title='Changed')).get_json()['code'],
                         'TEAM_ACTIVITY_TASK_IDENTITY_CHANGED')
        self.assertIsNone(self.client.get(self.url).get_json()['member']['current_task']['progress_percent'])

    def test_distinct_teams_and_project_read_access(self):
        self.setup_activity()
        self.report()
        response = self.client.post('/api/v1/agent-teams', json=dict(self.config, name='另一团队'))
        second_id = response.get_json()['id']
        second = '/api/v1/agent-teams/%s/members/%s/activity' % (second_id, self.main_claw.id)
        self.assertEqual(self.client.get(second).get_json()['member']['version'], 0)
        self.assertEqual(self.client.get(second).get_json()['history']['total'], 0)
        self.other_claw.project_id = self.other_project.id
        db.session.commit()
        self.assertEqual(self.client.get(self.list_url, headers=self._headers(self.other_token)).status_code, 403)
        self.assertEqual(self.client.get(self.url, headers=self._headers(self.other_token)).status_code, 403)


if __name__ == '__main__':
    unittest.main()
