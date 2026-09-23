"""Explicit team ownership, manager authoring, schedule projections and no dispatch."""
import unittest
from datetime import date
from unittest.mock import patch
import test_agent_teams_api as fixtures
from app import db
from app.models import (AgentTeam, ClawMessage, MissionStage, TestPlan,
                        TestPlanReport, TestReport, TestTask, TestTaskReport,
                        WorkflowRun, WorkflowMission)
from app.models_plan_supervision import PlanSupervisor


class AgentTeamPlansTest(unittest.TestCase):
    tearDown = fixtures.AgentTeamsApiTest.tearDown
    _definition = fixtures.AgentTeamsApiTest._definition
    _headers = fixtures.AgentTeamsApiTest._headers
    _login_admin = fixtures.AgentTeamsApiTest._login_admin
    _setup_team = fixtures.AgentTeamsApiTest._setup_team

    def setUp(self):
        fixtures.AgentTeamsApiTest.setUp(self)
        self._setup_team()
        self.url = '/api/v1/agent-teams/%s/test-plans' % self.team_id
        self.body = {'name':'团队周计划 <script>', 'start_date':'2026-09-21', 'end_date':'2026-09-27'}

    def create(self, **extra):
        response = self.client.post(self.url, headers=self._headers(), json=dict(self.body, **extra))
        self.assertEqual(response.status_code, 201, response.json)
        return response.json

    def test_manager_creates_real_plan_and_tasks_without_runtime_or_run(self):
        plan = self.create()
        self.assertEqual(plan['team_id'], self.team_id)
        self.assertEqual(plan['project_id'], self.project.id)
        self.assertEqual(plan['status'], 'draft')
        task = self.client.post('/api/v1/test-plans/%s/tasks' % plan['id'], headers=self._headers(),
                                json={'name':'回归 Bug', 'assignee_claw_id':self.other_claw.id})
        self.assertEqual(task.status_code, 201, task.json)
        self.assertEqual(WorkflowRun.query.count(), 0)
        self.assertEqual(WorkflowMission.query.count(), 0)
        self.assertEqual(self.client.put('/api/v1/test-plans/%s' % plan['id'],
            headers=self._headers(), json={'name':'已调整'}).status_code, 200)

    def test_active_team_plan_bootstraps_supervisor_mission_and_task_stage(self):
        self.app.config.update(
            PLAN_SUPERVISION_ENABLED=True,
            PLAN_SUPERVISION_TEAM_IDS=str(self.team_id),
        )
        with patch('app.services.plan_supervision.wake') as wake:
            plan = self.create(status='active')
            supervisor = db.session.get(PlanSupervisor, plan['id'])
            self.assertIsNotNone(supervisor)
            self.assertIsNotNone(supervisor.mission_id)
            self.assertEqual(WorkflowRun.query.count(), 0)
            self.assertEqual(WorkflowMission.query.count(), 1)
            self.assertEqual(ClawMessage.query.filter_by(
                msg_type='plan_supervision').count(), 1)
            wake.assert_called_once_with(self.main_claw.id)

            task = self.client.post(
                '/api/v1/test-plans/%s/tasks' % plan['id'],
                headers=self._headers(), json={
                    'name': '编辑器回归', 'task_type': 'automation',
                    'assignee_claw_id': self.other_claw.id,
                })
            self.assertEqual(task.status_code, 201, task.json)
            stage = MissionStage.query.filter_by(
                mission_id=supervisor.mission_id).one()
            self.assertEqual(stage.assigned_claw_id, self.other_claw.id)
            self.assertEqual(stage.input_snapshot_json['test_task_id'], task.json['id'])

    def test_non_manager_cannot_create_edit_attach_or_create_tasks_even_same_owner(self):
        denied = self.client.post(self.url, headers=self._headers(self.other_token), json=self.body)
        self.assertEqual(denied.status_code, 403)
        plan = self.create()
        path = '/api/v1/test-plans/%s' % plan['id']
        self.assertEqual(self.client.put(path, headers=self._headers(self.other_token), json={'name':'bad'}).status_code, 403)
        self.assertEqual(self.client.post(path+'/tasks', headers=self._headers(self.other_token), json={'name':'bad'}).status_code, 403)
        self.assertEqual(self.client.get(path, headers=self._headers(self.other_token)).status_code, 200)
        self.assertEqual(self.client.delete(path, headers=self._headers(self.other_token)).status_code, 403)

    def test_scope_dates_and_paused_team_fail_closed(self):
        for fields in ({'project_id':9999}, {'team_id':9999}, {'start_date':'no'},
                       {'start_date':'2026-10-01'}, {'iteration_id':9999}, {'status':'fake'}):
            with self.subTest(fields=fields):
                self.assertEqual(self.client.post(self.url, headers=self._headers(), json=dict(self.body, **fields)).status_code, 400)
        team = db.session.get(AgentTeam, self.team_id)
        team.status = 'paused'; db.session.commit()
        self.assertEqual(self.client.post(self.url, headers=self._headers(),json=self.body).status_code, 409)
        self.assertEqual(TestPlan.query.count(), 0)

    def test_existing_plan_explicit_link_then_immutable_team_project(self):
        plan = TestPlan(name='Existing', project_id=self.project.id, status='draft',
                        start_date=date(2026,9,21), end_date=date(2026,9,25))
        db.session.add(plan); db.session.commit()
        path = '/api/v1/test-plans/%s' % plan.id
        linked = self.client.put(path, headers=self._headers(), json={'team_id':self.team_id})
        self.assertEqual(linked.status_code, 200, linked.json)
        for fields in ({'team_id':None}, {'team_id':999}, {'project_id':999}, {'end_date':None}):
            self.assertIn(self.client.put(path, headers=self._headers(),json=fields).status_code,(400,409))
        self.assertEqual(plan.team_id, self.team_id)

    def test_cannot_attach_foreign_plan_by_overriding_project(self):
        from app.models import Project
        foreign = Project(name='Foreign team plans')
        db.session.add(foreign); db.session.flush()
        plan = TestPlan(name='Other project plan', project_id=foreign.id,
                        start_date=date(2026,9,21), end_date=date(2026,9,25))
        db.session.add(plan); db.session.commit()
        response = self.client.put('/api/v1/test-plans/%s' % plan.id, headers=self._headers(),
            json={'team_id':self.team_id,'project_id':self.project.id})
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(plan.team_id)
        self.assertEqual(plan.project_id, foreign.id)

    def test_daily_weekly_overview_uses_real_tasks_and_separates_unscheduled(self):
        plan = self.create()
        for name, start, end, status in (
            ('daily',date(2026,9,21),date(2026,9,21),'completed'),
            ('spanning',date(2026,9,20),date(2026,9,22),'in_progress'),
            ('sunday',date(2026,9,27),date(2026,9,27),'blocked'),
            ('next week',date(2026,9,28),date(2026,9,28),'pending'),
            ('undated',None,None,'assigned'),
            ('deadline only',None,date(2026,9,21),'pending')):
            db.session.add(TestTask(plan_id=plan['id'],name=name,start_date=start,end_date=end,status=status))
        db.session.commit()
        day = self.client.get(self.url+'?period=day&date=2026-09-21',headers=self._headers()).json
        self.assertEqual(day['summary']['total'],3)
        self.assertEqual(day['items'][0]['unscheduled_tasks'],1)
        self.assertEqual(day['items'][0]['total_tasks'],6)
        self.assertEqual(day['items'][0]['completed_tasks'],1)
        self.assertEqual(day['items'][0]['progress'],17)
        self.assertEqual(day['items'][0]['report_count'], 0)
        self.assertTrue(all(task['report_count'] == 0 for task in day['items'][0]['tasks']))
        self.assertIsNone(day['items'][0]['supervision'])
        week = self.client.get(self.url+'?period=week&date=2026-09-27',headers=self._headers()).json
        self.assertEqual(week['start_date'],'2026-09-21')
        self.assertEqual(week['summary']['total'],4)
        self.assertEqual(week['summary']['blocked'],1)
        all_tasks = self.client.get(self.url+'?period=all',headers=self._headers()).json
        self.assertEqual(all_tasks['summary']['total'],6)
        self.assertEqual(len(all_tasks['items'][0]['tasks']),6)
        self.assertEqual(
            {task['name'] for task in all_tasks['items'][0]['tasks']},
            {'daily', 'spanning', 'sunday', 'next week', 'undated', 'deadline only'})
        self.assertEqual(all_tasks['items'][0]['url'],'/testplans?plan_id=%s' % plan['id'])

    def test_overview_exposes_report_count_and_legacy_report(self):
        first = self.create(name='has report rows')
        second = self.create(name='legacy report only')
        db.session.add_all([
            TestPlanReport(plan_id=first['id'], title='报告一', content='one'),
            TestPlanReport(plan_id=first['id'], title='报告二', content='two'),
            TestReport(title='全局报告', content='three', report_type='functional',
                       project_id=self.project.id,
                       source_ref_type='test_plan', source_ref_id=first['id']),
        ])
        legacy = db.session.get(TestPlan, second['id'])
        legacy.report_content = '# 旧版报告'
        db.session.commit()

        items = self.client.get(self.url+'?period=all', headers=self._headers()).json['items']
        counts = {item['id']: item['report_count'] for item in items}
        self.assertEqual(counts[first['id']], 3)
        self.assertEqual(counts[second['id']], 1)

    def test_overview_exposes_deduplicated_task_report_count(self):
        plan = self.create(name='task reports')
        task = TestTask(plan_id=plan['id'], name='客户端性能测试',
                        start_date=date(2026, 9, 23), end_date=date(2026, 9, 23),
                        status='in_progress')
        db.session.add(task)
        db.session.flush()
        global_report = TestReport(
            title='任务全局报告', content='global', report_type='performance',
            project_id=self.project.id, source_ref_type='test_task',
            source_ref_id=task.id)
        db.session.add(global_report)
        db.session.flush()
        linked = TestTaskReport(
            task_id=task.id, title='已迁移旧报告', content='linked',
            linked_test_report_id=global_report.id)
        db.session.add_all([
            linked,
            TestTaskReport(task_id=task.id, title='独立旧报告', content='legacy'),
        ])
        db.session.commit()

        items = self.client.get(self.url+'?period=all', headers=self._headers()).json['items']
        task_data = next(item for item in items if item['id'] == plan['id'])['tasks'][0]
        self.assertEqual(task_data['id'], task.id)
        self.assertEqual(task_data['report_count'], 2)
        self.assertEqual(linked.to_dict()['linked_test_report_id'], global_report.id)

    def test_other_team_unbound_plans_pagination_and_input_errors(self):
        for i in range(7): self.create(name='plan '+str(i))
        db.session.add(TestPlan(name='Unbound',project_id=self.project.id,start_date=date(2026,9,21),end_date=date(2026,9,27)))
        db.session.commit()
        result = self.client.get(self.url+'?period=all&offset=6',headers=self._headers()).json
        self.assertEqual(result['total'],7)
        self.assertEqual(len(result['items']),1)
        for query in ('period=month','date=bad','limit=25','offset=-1','date=9999-12-31&period=week'):
            self.assertEqual(self.client.get(self.url+'?'+query,headers=self._headers()).status_code,400)

    def test_anonymous_and_foreign_project_cannot_read_team_plan(self):
        plan = self.create()
        with self.client.session_transaction() as s: s.clear()
        self.assertEqual(self.client.get(self.url).status_code,401)
        self.assertEqual(self.client.get('/api/v1/test-plans/%s' % plan['id']).status_code,401)
        self.other_claw.project_id = None; self.other_claw.role = 'specialist'; db.session.commit()
        self.assertEqual(self.client.get(self.url,headers=self._headers(self.other_token)).status_code,403)


if __name__ == '__main__':
    unittest.main()
