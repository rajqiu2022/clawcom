"""Temporary primary-manager handover: atomic swap, authority transfer, reversal."""
import unittest
from datetime import timedelta
from unittest.mock import patch

import test_workflow_missions_api as fixtures
from app import db
from app.models import (AgentTeam, AgentTeamMission, AuditLog, ClawMessage,
                        OpenClawInstance, TestPlan, User, WorkflowMission, _now)
from app.models_plan_supervision import PlanSupervisor, PlanSupervisorEvent
from app.services import plan_supervision as svc
from app.services.agent_team_manager_delegation import delegate, revoke, state
from app.services.agent_teams import TeamError


class ManagerDelegationTest(unittest.TestCase):
    tearDown = fixtures.WorkflowMissionsApiTest.tearDown
    _definition = fixtures.WorkflowMissionsApiTest._definition
    _login_admin = fixtures.WorkflowMissionsApiTest._login_admin
    _headers = fixtures.WorkflowMissionsApiTest._headers

    def setUp(self):
        fixtures.WorkflowMissionsApiTest.setUp(self)
        self.app.config.update(
            PLAN_SUPERVISION_ENABLED=True,
            PLAN_SUPERVISION_TEAM_IDS='*',
            AGENT_TEAMS_ENABLED=True,
            AGENT_TEAM_CONTRACTS_ENABLED=True,
            AGENT_TEAMS_PROJECT_IDS=[self.project.id],
            PLAN_MANAGER_DELEGATION_ENABLED=True,
        )
        # main_claw = 在位主经理（小策），backup_claw = 待转正的小数。
        self.third_claw = OpenClawInstance(
            name='第三Agent', safe_name='mission-third', claw_tag='mission-third',
            owner='third', project_id=self.project.id, status='工作')
        db.session.add(self.third_claw)
        db.session.flush()
        self.team = AgentTeam(
            project_id=self.project.id, name='Delegation team', objective='Handover',
            primary_manager_claw_id=self.main_claw.id,
            backup_manager_claw_id=self.other_claw.id, status='active',
            policy_json={'allowed_definition_ids': [self.flow_a.id],
                         'max_child_runs': 3})
        db.session.add(self.team)
        db.session.flush()
        self.plan = TestPlan(
            name='Delegated plan', project_id=self.project.id, team_id=self.team.id,
            start_date=_now().date(), end_date=(_now() + timedelta(days=7)).date(),
            status='draft', created_by=self.admin.username)
        db.session.add(self.plan)
        db.session.commit()
        self.base = '/api/v1/agent-teams/%s/manager-delegation' % self.team.id
        self.patcher = patch('app.services.plan_supervision.wake')
        self.wake = self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.start_supervision()

    # ---------------------------------------------------------------- helpers
    def start_supervision(self):
        response = self.client.post(
            '/api/v1/test-plans/%s/supervision/start' % self.plan.id,
            json={'command_key': 'start-1', 'team_id': self.team.id,
                  'orchestrator_claw_id': self.main_claw.id})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))

    def _clear_session(self):
        """Web session wins over Bearer auth, so clear it to act as a claw."""
        with self.client.session_transaction() as session:
            session.clear()

    def sup(self):
        return db.session.get(PlanSupervisor, self.plan.id)

    def claim(self):
        """Take a real turn lease as the incumbent manager."""
        sup = self.sup()
        self._clear_session()
        response = self.client.post(
            '/api/v1/test-plans/%s/supervision/claim' % self.plan.id,
            json={'command_key': 'claim-1', 'worker_id': 'worker-incumbent',
                  'wake_message_id': sup.wake_message_id},
            headers=self._headers(self.main_token))
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self._login_admin()
        return response.get_json()

    def team_row(self):
        db.session.expire_all()
        return db.session.get(AgentTeam, self.team.id)

    def post_delegate(self, claw_id, **changes):
        body = {'expected_version': self.team_row().version,
                'delegate_claw_id': claw_id,
                'reason': '在位经理 Provider 额度耗尽', **changes}
        return self.client.post(self.base, json=body)

    def post_revoke(self, **changes):
        body = {'expected_version': self.team_row().version,
                'reason': '在位经理额度已恢复', **changes}
        return self.client.delete(self.base, json=body)

    # ------------------------------------------------------------------ tests
    def test_handover_moves_roles_supervisor_hash_and_mission_owner(self):
        sup = self.sup()
        self.claim()
        sup = self.sup()
        self.assertEqual(sup.status, 'leased')
        old_hash = sup.start_hash
        old_token = sup.fencing_token
        old_epoch = self.team_row().manager_epoch
        mission_id = sup.mission_id

        response = self.post_delegate(self.other_claw.id, expires_at=None)
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        payload = response.get_json()

        team = self.team_row()
        # 小数转正、小策变替补。
        self.assertEqual(team.primary_manager_claw_id, self.other_claw.id)
        self.assertEqual(team.backup_manager_claw_id, self.main_claw.id)
        self.assertEqual(team.manager_epoch, old_epoch + 1)
        self.assertEqual(team.active_manager_claw_id, self.other_claw.id)
        self.assertIn(str(self.other_claw.id), team.manager_session_id)
        self.assertTrue(team.has_manager_authority(self.other_claw.id))
        self.assertTrue(team.has_manager_authority(self.main_claw.id))

        # 监督绑定整体迁移：orchestrator + start_hash + fence + 旧轮作废。
        sup = self.sup()
        self.assertEqual(sup.orchestrator_claw_id, self.other_claw.id)
        self.assertNotEqual(sup.start_hash, old_hash)
        self.assertEqual(sup.start_hash, svc.digest({
            'plan_id': self.plan.id, 'team_id': team.id,
            'orchestrator_claw_id': self.other_claw.id}))
        self.assertEqual(sup.fencing_token, old_token + 1)
        # 旧轮作废；after_request 的 drain 随即为**新经理**补一条唤醒，所以终态
        # 可能是 retryable（尚未 pump）或 pending（唤醒已入队）。
        self.assertIn(sup.status, ('retryable', 'pending'))
        self.assertIsNone(sup.lease_owner)
        self.assertIsNone(sup.lease_expires_at)
        self.assertIsNotNone(sup.next_check_at)
        self.assertTrue(svc.available(sup))
        # 唤醒必须只指向新经理，不得留下双头。
        wake = db.session.get(ClawMessage, sup.wake_message_id)
        self.assertIsNotNone(wake)
        self.assertEqual(wake.claw_id, self.other_claw.id)
        self.assertEqual(wake.msg_type, 'plan_supervision')
        self.assertEqual(ClawMessage.query.filter_by(
            msg_type='plan_supervision', status='pending').count(), 1)

        # Mission 归属与团队快照同步，否则下次 bootstrap 会 PLAN_MISSION_CONFLICT。
        mission = db.session.get(WorkflowMission, mission_id)
        self.assertEqual(mission.main_claw_id, self.other_claw.id)
        snapshot = db.session.get(AgentTeamMission, mission_id).snapshot_json
        self.assertEqual(snapshot['primary_manager_claw_id'], self.other_claw.id)
        self.assertEqual(snapshot['backup_manager_claw_id'], self.main_claw.id)
        self.assertTrue(snapshot['manager_delegation']['active'])

        # 证据留痕。
        self.assertTrue(PlanSupervisorEvent.query.filter_by(
            plan_id=self.plan.id, kind='manager_delegation').count())
        self.assertTrue(AuditLog.query.filter_by(
            resource_type='agent_team', action='manager_delegation').count())
        self.assertEqual(payload['delegation']['delegate_claw_id'], self.other_claw.id)
        self.assertEqual(payload['delegation']['previous_primary_claw_id'],
                         self.main_claw.id)
        self.assertEqual(payload['supervisors'], [{
            'plan_id': self.plan.id, 'rebound': True, 'status': 'retryable',
            'from_claw_id': self.main_claw.id, 'to_claw_id': self.other_claw.id,
            'fencing_token': old_token + 1, 'start_hash_changed': True,
            'mission_id': mission_id,
            'next_check_at': payload['supervisors'][0]['next_check_at']}])

    def test_authority_moves_to_the_delegate_and_leaves_the_incumbent(self):
        self.post_delegate(self.other_claw.id)
        sup = self.sup()
        # 新主经理通过鉴权门，走到任务存在性检查。
        with self.assertRaises(svc.SupervisionError) as ctx:
            svc.dispatch_test_task(sup, self.other_claw.id, 999999, {})
        self.assertEqual(ctx.exception.code, 'TEST_TASK_NOT_FOUND')
        # 原主经理被硬拦。
        with self.assertRaises(svc.SupervisionError) as ctx:
            svc.dispatch_test_task(sup, self.main_claw.id, 999999, {})
        self.assertEqual(ctx.exception.code, 'PLAN_MANAGER_REQUIRED')
        self.assertEqual(ctx.exception.status, 403)

    def test_revoke_restores_the_original_binding_exactly(self):
        original_hash = self.sup().start_hash
        self.post_delegate(self.other_claw.id)
        delegated_token = self.sup().fencing_token

        response = self.post_revoke()
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))

        team = self.team_row()
        self.assertEqual(team.primary_manager_claw_id, self.main_claw.id)
        self.assertEqual(team.backup_manager_claw_id, self.other_claw.id)
        self.assertEqual(team.manager_epoch, 2)
        sup = self.sup()
        self.assertEqual(sup.orchestrator_claw_id, self.main_claw.id)
        self.assertEqual(sup.start_hash, original_hash)
        self.assertEqual(sup.fencing_token, delegated_token + 1)
        self.assertIsNone(sup.lease_owner)
        self.assertTrue(svc.available(sup))
        # 回退后唤醒必须回到原主经理。
        self.assertEqual(
            db.session.get(ClawMessage, sup.wake_message_id).claw_id,
            self.main_claw.id)
        self.assertTrue(PlanSupervisorEvent.query.filter_by(
            plan_id=self.plan.id, kind='manager_delegation').count() >= 2)

        record = team.manager_delegation_json
        self.assertFalse(record['active'])
        self.assertEqual(record['revoked_by'], self.admin.username)
        self.assertEqual(len(record['history']), 1)
        self.assertFalse(record['history'][0]['active'])
        self.assertFalse(state(team)['active'])

        # 回退后原主经理恢复派工权，小数的经理授权随替补角色保留。
        with self.assertRaises(svc.SupervisionError) as ctx:
            svc.dispatch_test_task(self.sup(), self.other_claw.id, 999999, {})
        self.assertEqual(ctx.exception.code, 'PLAN_MANAGER_REQUIRED')

    def test_readback_exposes_delegation_state(self):
        response = self.client.get(self.base)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()['manager_delegation']['active'])
        self.post_delegate(self.other_claw.id, reason='额度耗尽', expires_at=None)
        payload = self.client.get(self.base).get_json()
        self.assertTrue(payload['manager_delegation']['active'])
        self.assertEqual(payload['manager_delegation']['delegate_name'], '其他Agent')
        self.assertEqual(payload['team']['manager_delegation']['active'], True)
        self.assertEqual(payload['team']['manager_delegation']['revision'], 1)

    def test_guards(self):
        # 只有配置在替补位的经理可以转正。
        self.assertEqual(self.post_delegate(self.third_claw.id).status_code, 400)
        # 在位的本人不能再转正一次。
        self.assertEqual(self.post_delegate(self.main_claw.id).status_code, 400)
        # 版本冲突。
        self.assertEqual(
            self.post_delegate(self.other_claw.id, expected_version=999).status_code,
            409)
        # 过期的截止时间。
        self.assertEqual(
            self.post_delegate(self.other_claw.id,
                               expires_at='2020-01-01T00:00:00+08:00').status_code,
            400)
        # 尚未转正时不能回退。
        self.assertEqual(self.post_revoke().status_code, 409)
        # 正常转正后不能重复转正。
        self.assertEqual(self.post_delegate(self.other_claw.id).status_code, 201)
        self.assertEqual(self.post_delegate(self.other_claw.id).status_code, 409)
        # 转正期间主经理不再是原主经理，回退必须仍然可用。
        self.assertEqual(self.post_revoke().status_code, 200)

    def test_team_config_update_cannot_split_the_live_handover(self):
        """团队配置页不得在转正期间把主备改回，否则监督绑定会自相矛盾。"""
        self.post_delegate(self.other_claw.id)
        team = self.team_row()
        body = {
            'project_id': self.project.id, 'name': team.name,
            'objective': team.objective, 'status': 'active',
            # 试图把主经理改回原主经理，绕过转正。
            'primary_manager_claw_id': self.main_claw.id,
            'backup_manager_claw_id': self.other_claw.id,
            'members': [],
            'policy': {'allowed_definition_ids': [self.flow_a.id],
                       'max_child_runs': 3},
            'expected_version': team.version,
        }
        url = '/api/v1/agent-teams/%s' % self.team.id
        response = self.client.put(url, json=body)
        self.assertEqual(response.status_code, 409, response.get_data(as_text=True))
        self.assertIn('TEAM_DELEGATION_ACTIVE', response.get_data(as_text=True))
        self.assertEqual(self.team_row().primary_manager_claw_id,
                         self.other_claw.id)

        # 回退后配置页恢复可用。
        self.post_revoke()
        body['expected_version'] = self.team_row().version
        self.assertEqual(self.client.put(url, json=body).status_code, 200)
        self.assertEqual(self.team_row().primary_manager_claw_id, self.main_claw.id)

    def test_switch_off_and_authorization(self):
        self.app.config['PLAN_MANAGER_DELEGATION_ENABLED'] = False
        response = self.post_delegate(self.other_claw.id)
        self.assertEqual(response.status_code, 404)
        self.assertIn('TEAM_DELEGATION_DISABLED', response.get_data(as_text=True))
        self.app.config['PLAN_MANAGER_DELEGATION_ENABLED'] = True

        member = User(username='plain_member', role='user',
                      managed_projects=[self.project.id])
        member.set_password('secret')
        db.session.add(member)
        db.session.commit()
        with self.client.session_transaction() as session:
            session.clear()
            session['user_id'] = member.id
        response = self.post_delegate(self.other_claw.id)
        self.assertEqual(response.status_code, 403)
        self.assertIn('TEAM_ADMIN_REQUIRED', response.get_data(as_text=True))

        anonymous = self.app.test_client()
        self.assertEqual(
            anonymous.post(self.base, json={'delegate_claw_id': self.other_claw.id}
                           ).status_code, 401)

    def test_deadline_and_expiry_readback(self):
        """expires_at 只是记录截止时间；过期必须能被如实回读。"""
        future = (_now() + timedelta(hours=6)).replace(
            microsecond=0).isoformat() + '+08:00'
        response = self.post_delegate(
            self.other_claw.id, expires_at=future,
            reason='额度耗尽，先顶到今晚')
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        payload = response.get_json()['delegation']
        self.assertEqual(payload['expires_at'], future)
        self.assertFalse(payload['expired'])

        # 手工把截止时间推到过去，回读必须报 expired=true（且不误报清除）。
        team = self.team_row()
        record = dict(team.manager_delegation_json)
        record['expires_at'] = '2020-01-01T00:00:00+08:00'
        team.manager_delegation_json = record
        db.session.commit()
        team = self.team_row()
        self.assertTrue(state(team)['expired'])
        self.assertEqual(
            self.client.get(self.base).get_json()['manager_delegation']['expired'],
            True)

    def test_service_layer_requires_the_feature_gate(self):
        team = self.team_row()
        self.app.config['PLAN_MANAGER_DELEGATION_ENABLED'] = False
        with self.assertRaises(TeamError) as ctx:
            delegate(team.id, self.other_claw.id, 'ops:test')
        self.assertEqual(ctx.exception.code, 'TEAM_DELEGATION_DISABLED')
        self.app.config['PLAN_MANAGER_DELEGATION_ENABLED'] = True
        with self.assertRaises(TeamError) as ctx:
            revoke(team.id, 'ops:test')
        self.assertEqual(ctx.exception.code, 'TEAM_DELEGATION_NOT_ACTIVE')


if __name__ == '__main__':
    unittest.main()
