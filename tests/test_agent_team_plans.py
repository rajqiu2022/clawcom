"""Explicit team ownership, manager authoring, schedule projections and no dispatch."""
import unittest
from datetime import date, datetime, timedelta
from unittest.mock import patch
import test_agent_teams_api as fixtures
from app import db
from app.models import (AgentTask, AgentTeam, AgentTeamKnowledgeResource,
                        AgentTeamSkillResource, ClawMessage, KnowledgeEntry,
                        MissionStage, Skill, TestPlan, TestPlanReport,
                        TestReport, TestTask, TestTaskOccurrence,
                        TestTaskReport, WorkflowRun, WorkflowMission, _now)
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

    def test_task_dependencies_are_persisted_and_cycles_are_rejected(self):
        plan = self.create()
        path = '/api/v1/test-plans/%s/tasks' % plan['id']
        first = self.client.post(path, headers=self._headers(), json={
            'name': '生成版本基线',
            'assignee_claw_id': self.other_claw.id,
        })
        self.assertEqual(first.status_code, 201, first.json)
        second = self.client.post(path, headers=self._headers(), json={
            'name': '分析代码风险',
            'assignee_claw_id': self.other_claw.id,
            'depends_on_task_ids': [first.json['id']],
        })
        self.assertEqual(second.status_code, 201, second.json)
        self.assertEqual(
            second.json['depends_on_task_ids'], [first.json['id']])

        cycle = self.client.put(
            '%s/%s' % (path, first.json['id']),
            headers=self._headers(), json={
                'depends_on_task_ids': [second.json['id']],
            })
        self.assertEqual(cycle.status_code, 400, cycle.json)
        self.assertIn('循环依赖', cycle.json['error'])

    def test_manager_binds_task_references_and_agent_task_receives_manifest(self):
        self.body.update(start_date=str(_now().date()),
                         end_date=str(_now().date() + timedelta(days=7)))
        self.app.config.update(
            PLAN_SUPERVISION_ENABLED=True,
            PLAN_SUPERVISION_TEAM_IDS=str(self.team_id),
        )
        knowledge = KnowledgeEntry(
            title='移动端回归门禁', content='# 门禁', category='testing',
            scope='project', project_id=self.project.id, status='approved')
        skill = Skill(
            name='mobile-regression-reference', display_name='移动端回归参考',
            review_status='approved', visibility='public',
            applicable_projects=[self.project.id])
        report = TestReport(
            title='昨日性能基线', report_type='performance',
            project_id=self.project.id, status='published', risk_level='medium')
        db.session.add_all([knowledge, skill, report])
        db.session.flush()
        db.session.add_all([
            AgentTeamKnowledgeResource(
                team_id=self.team_id, knowledge_id=knowledge.id,
                linked_by_type='user', linked_by_name='tester'),
            AgentTeamSkillResource(
                team_id=self.team_id, skill_id=skill.id,
                linked_by_type='user', linked_by_name='tester'),
        ])
        db.session.commit()

        with patch('app.services.plan_supervision.wake'):
            plan = self.create(status='active')
        options = self.client.get(
            '/api/v1/test-plans/%s/task-reference-options' % plan['id'],
            headers=self._headers())
        self.assertEqual(options.status_code, 200, options.json)
        self.assertEqual([skill.id], [item['id'] for item in options.json['skills']])
        self.assertEqual([knowledge.id], [item['id'] for item in options.json['knowledge']])
        self.assertIn(report.id, [item['id'] for item in options.json['reports']])

        created = self.client.post(
            '/api/v1/test-plans/%s/tasks' % plan['id'],
            headers=self._headers(), json={
                'name': '微信小游戏性能回归',
                'description': '对照昨日基线执行',
                'assignee_claw_id': self.other_claw.id,
                'reference_skill_ids': [skill.id],
                'reference_knowledge_ids': [knowledge.id],
                'reference_report_ids': [report.id],
            })
        self.assertEqual(created.status_code, 201, created.json)
        self.assertEqual(created.json['reference_skill_ids'], [skill.id])
        self.assertEqual(
            created.json['references']['knowledge'][0]['pull_url'],
            '/api/v1/knowledge/%s/export.md' % knowledge.id)
        self.assertEqual(
            created.json['references']['reports'][0]['detail_api'],
            '/api/v1/test-reports/%s' % report.id)
        readable = self.client.get(
            '/api/v1/test-plans/%s/tasks/%s' % (
                plan['id'], created.json['id']),
            headers=self._headers(self.other_token))
        self.assertEqual(readable.status_code, 200, readable.json)
        self.assertEqual(
            readable.json['references']['skills'][0]['id'], skill.id)

        denied = self.client.put(
            '/api/v1/test-plans/%s/tasks/%s' % (plan['id'], created.json['id']),
            headers=self._headers(self.other_token),
            json={'reference_report_ids': []})
        self.assertEqual(denied.status_code, 403, denied.json)

        dispatched = self.client.post(
            '/api/v1/test-plans/%s/supervision/agent-tasks' % plan['id'],
            headers=self._headers(), json={
                'command_key': 'task-references-dispatch-v1',
                'test_task_id': created.json['id'],
            })
        self.assertEqual(dispatched.status_code, 201, dispatched.json)
        agent_task = AgentTask.query.filter_by(
            task_type='test_plan_agent_task').one()
        payload = __import__('json').loads(agent_task.payload)
        self.assertEqual(payload['description'], '对照昨日基线执行')
        self.assertEqual(payload['references']['skills'][0]['id'], skill.id)
        self.assertEqual(payload['references']['knowledge'][0]['id'], knowledge.id)
        self.assertEqual(payload['references']['reports'][0]['id'], report.id)
        self.assertTrue(payload['references']['read_policy']['required_before_execution'])
        self.assertIn('开始前必须读取 payload.references', agent_task.command)

        scheduled = self.client.post(
            '/api/v1/test-plans/%s/tasks' % plan['id'],
            headers=self._headers(), json={
                'name': '每日参考资料快照',
                'assignee_claw_id': self.other_claw.id,
                'schedule_enabled': True,
                'recurrence_type': 'daily',
                'auto_dispatch': False,
                'reference_skill_ids': [skill.id],
                'reference_knowledge_ids': [knowledge.id],
                'reference_report_ids': [report.id],
            })
        self.assertEqual(scheduled.status_code, 201, scheduled.json)
        occurrence = TestTaskOccurrence.query.filter_by(
            test_task_id=scheduled.json['id']).one()
        self.assertEqual(occurrence.reference_skill_ids_json, [skill.id])
        updated = self.client.put(
            '/api/v1/test-plans/%s/tasks/%s' % (
                plan['id'], scheduled.json['id']),
            headers=self._headers(), json={
                'reference_skill_ids': [],
                'reference_knowledge_ids': [],
                'reference_report_ids': [],
            })
        self.assertEqual(updated.status_code, 200, updated.json)
        db.session.refresh(occurrence)
        self.assertEqual(occurrence.reference_skill_ids_json, [skill.id])
        self.assertEqual(
            occurrence.to_dict()['references']['reports'][0]['id'], report.id)

    def test_active_team_plan_bootstraps_supervisor_mission_and_task_stage(self):
        self.body.update(start_date=str(_now().date()),
                         end_date=str(_now().date() + timedelta(days=7)))
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

            overview = self.client.get(
                self.url + '?period=all', headers=self._headers()).json
            supervision = overview['items'][0]['supervision']
            self.assertEqual(
                supervision['resume_api'],
                '/test-plans/%s/supervision/resume' % plan['id'])

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

    def test_task_status_filter_matches_before_plan_pagination(self):
        first = self.create(name='blocked plan')
        second = self.create(name='completed plan')
        db.session.add_all([
            TestTask(plan_id=first['id'], name='failed task', status='blocked',
                     start_date=date(2026, 9, 21), end_date=date(2026, 9, 21)),
            TestTask(plan_id=second['id'], name='done task', status='completed',
                     start_date=date(2026, 9, 21), end_date=date(2026, 9, 21)),
        ])
        db.session.commit()
        result = self.client.get(
            self.url + '?period=day&date=2026-09-21&task_status=blocked&limit=1',
            headers=self._headers())
        self.assertEqual(result.status_code, 200, result.json)
        self.assertEqual(result.json['total'], 1)
        self.assertEqual(result.json['summary']['total'], 2)
        self.assertEqual(result.json['items'][0]['id'], first['id'])
        self.assertEqual([task['name'] for task in result.json['items'][0]['tasks']],
                         ['failed task'])

    def test_task_status_filter_uses_scheduled_occurrence_not_template(self):
        plan = self.create(name='recurring')
        task = TestTask(plan_id=plan['id'], name='daily check', status='assigned',
                        schedule_enabled=True, recurrence_type='daily',
                        assignee_claw_id=self.other_claw.id,
                        start_date=date(2026, 9, 21), end_date=date(2026, 9, 27))
        db.session.add(task)
        db.session.flush()
        db.session.add(TestTaskOccurrence(
            plan_id=plan['id'], test_task_id=task.id,
            occurrence_date=date(2026, 9, 21),
            not_before_at=datetime(2026, 9, 21, 10),
            status='running', assignee_claw_id=self.other_claw.id))
        db.session.commit()
        path = self.url + '?period=day&date=2026-09-21&task_status='
        running = self.client.get(path + 'in_progress', headers=self._headers())
        self.assertEqual(running.status_code, 200, running.json)
        self.assertEqual(running.json['total'], 1)
        self.assertEqual(running.json['items'][0]['tasks'][0]['status'], 'running')
        pending = self.client.get(path + 'pending', headers=self._headers())
        self.assertEqual(pending.json['total'], 0)

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

    def test_assigned_agent_replaces_single_conclusion_without_creating_report(self):
        plan = self.create(name='lightweight conclusion')
        created = self.client.post(
            '/api/v1/test-plans/%s/tasks' % plan['id'],
            headers=self._headers(), json={
                'name': '简单巡检',
                'assignee_claw_id': self.other_claw.id,
            })
        self.assertEqual(created.status_code, 201, created.json)
        path = '/api/v1/test-plans/%s/tasks/%s/conclusion' % (
            plan['id'], created.json['id'])
        with self.client.session_transaction() as session:
            session.clear()

        saved = self.client.put(
            path, headers=self._headers(self.other_token),
            json={'content': '检查完成，无阻断问题'})
        self.assertEqual(saved.status_code, 200, saved.json)
        self.assertEqual(saved.json['content'], '检查完成，无阻断问题')
        self.assertFalse(saved.json['report_created'])
        self.assertTrue(saved.json['can_edit'])
        self.assertEqual(TestTaskReport.query.count(), 0)
        self.assertEqual(TestReport.query.count(), 0)

        replaced = self.client.put(
            path, headers=self._headers(self.other_token),
            json={'content': '补充：后续关注性能趋势'})
        self.assertEqual(replaced.status_code, 200, replaced.json)
        self.assertEqual(db.session.get(
            TestTask, created.json['id']).result_summary,
            '补充：后续关注性能趋势')
        self.assertEqual(TestTaskReport.query.count(), 0)
        self.assertEqual(self.client.put(
            path, headers=self._headers(self.other_token),
            json={'content': 'x' * 20001}).status_code, 400)

    def test_other_team_unbound_plans_pagination_and_input_errors(self):
        for i in range(7): self.create(name='plan '+str(i))
        db.session.add(TestPlan(name='Unbound',project_id=self.project.id,start_date=date(2026,9,21),end_date=date(2026,9,27)))
        db.session.commit()
        result = self.client.get(self.url+'?period=all&offset=6',headers=self._headers()).json
        self.assertEqual(result['total'],7)
        self.assertEqual(len(result['items']),1)
        for query in ('period=month','date=bad','limit=25','offset=-1',
                      'task_status=bogus','date=9999-12-31&period=week'):
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
