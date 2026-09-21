"""Opt-in, authenticated Test Plan supervision API."""
from datetime import datetime, time, timedelta
from flask import jsonify, request
from app import db
from app.api import api_bp
from app.api.auth_utils import get_current_claw, get_current_user, user_project_ids
from app.models import TestPlan, OpenClawInstance, WorkflowMission, WorkflowMissionDispatch, AgentTeamMission, _now
from app.models_plan_supervision import PlanSupervisor, PlanSupervisorEvent, PlanSupervisorReceipt
from app.services import plan_supervision as svc


@api_bp.errorhandler(svc.SupervisionError)
def supervision_error(exc):
    db.session.rollback()
    return jsonify({'code': exc.code, 'error': exc.message}), exc.status


def load(plan_id, write=False):
    if not svc.enabled():
        svc.fail('PLAN_SUPERVISION_DISABLED', '计划监督功能未启用', 503)
    plan = TestPlan.query.filter_by(id=plan_id).with_for_update().first()
    if not plan:
        svc.fail('PLAN_NOT_FOUND', '计划不存在', 404)
    claw, user = get_current_claw(), get_current_user()
    if claw:
        allowed = claw.project_id == plan.project_id
        if write:
            allowed = allowed and plan.created_by in (claw.name, claw.owner)
    else:
        allowed = user and (user.role == 'super_admin' or
            (plan.project_id in user_project_ids(user) and
             (not write or user.role == 'admin' or plan.created_by == user.username)))
    if not allowed:
        svc.fail('PLAN_ACCESS_DENIED', '无权访问或管理此计划', 403)
    return plan, svc.locked(plan_id), claw


def payload():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        svc.fail('PLAN_BODY_INVALID', '请求体须为 JSON 对象', 400)
    return body


def self_supervisor(plan_id):
    plan, sup, claw = load(plan_id)
    if not sup:
        svc.fail('PLAN_SUPERVISION_NOT_STARTED', '计划尚未启动监督', 409)
    if not claw or claw.id != sup.orchestrator_claw_id:
        svc.fail('PLAN_ORCHESTRATOR_REQUIRED', '仅绑定的监督 Agent 可以操作', 403)
    return sup, claw


@api_bp.route('/test-plans/<int:plan_id>/supervision', methods=['GET'])
def get_plan_supervision(plan_id):
    plan, sup, _ = load(plan_id)
    if not sup:
        return jsonify({'supervision': None})
    try:
        after = int(request.args.get('after', sup.acknowledged_cursor))
    except ValueError:
        svc.fail('PLAN_CURSOR_INVALID', 'after 必须为非负整数', 400)
    through = sup.lease_cursor if sup.status == 'leased' else sup.cursor
    if after < 0 or after > through:
        svc.fail('PLAN_CURSOR_INVALID', 'after 超出当前监督事件窗口', 400)
    events = PlanSupervisorEvent.query.filter(PlanSupervisorEvent.plan_id == plan_id,
        PlanSupervisorEvent.sequence > after, PlanSupervisorEvent.sequence <= through).order_by(
            PlanSupervisorEvent.sequence).limit(200).all()
    runs = WorkflowMissionDispatch.query.filter_by(mission_id=sup.mission_id).all() if sup.mission_id else []
    return jsonify({'supervision': sup.to_dict(),
        'events': [{'id': r.id, 'sequence': r.sequence, 'kind': r.kind,
                    'payload': r.payload_json} for r in events],
        'next_cursor': events[-1].sequence if events else after,
        'has_more': bool(events and events[-1].sequence < through),
        'through_cursor': through,
        'run_ids': [r.workflow_run_id for r in runs],
        'lease_valid': bool(svc.available(sup) and sup.status == 'leased'
                            and sup.lease_expires_at and sup.lease_expires_at > _now())})


@api_bp.route('/test-plans/<int:plan_id>/supervision/receipts/<int:receipt_id>', methods=['GET'])
def get_plan_supervision_receipt(plan_id, receipt_id):
    load(plan_id)
    record = PlanSupervisorReceipt.query.filter_by(plan_id=plan_id, id=receipt_id).first_or_404()
    return jsonify(record.response_json)


@api_bp.route('/test-plans/<int:plan_id>/supervision/start', methods=['POST'])
def start_plan_supervision(plan_id):
    plan, sup, _ = load(plan_id, write=True)
    body = payload()
    if set(body) - {'command_key', 'orchestrator_claw_id', 'team_id'}:
        svc.fail('PLAN_BODY_INVALID', '启动仅接受 command_key/orchestrator_claw_id/team_id', 400)
    claw_id = body.get('orchestrator_claw_id')
    claw = db.session.get(OpenClawInstance, claw_id) if type(claw_id) is int else None
    if not claw or claw.status == 'deleted' or claw.project_id != plan.project_id:
        svc.fail('PLAN_ORCHESTRATOR_INVALID', '必须绑定同项目的有效 Agent', 400)
    team_id = body.get('team_id')
    if team_id is not None and (type(team_id) is not int or team_id <= 0):
        svc.fail('PLAN_TEAM_INVALID', 'team_id 必须为正整数', 400)
    if not svc.team_binding_valid(team_id, plan.project_id, claw_id):
        svc.fail('PLAN_TEAM_NOT_ENABLED', '团队未启用监督，或监督者并非该项目团队的当前主经理', 403)
    if not plan.start_date or not plan.end_date or plan.end_date < plan.start_date or _now() >= svc.ends_at(plan):
        svc.fail('PLAN_DATES_INVALID', '计划日期无效或已结束', 400)
    if plan.status not in ('draft', 'active'):
        svc.fail('PLAN_NOT_STARTABLE', '已完成/归档计划不可启动')
    hashed = svc.digest(body)
    if sup and sup.start_hash != hashed:
        svc.fail('PLAN_ALREADY_SUPERVISED', '计划已绑定监督工作项；不可重复启动或更换身份')
    if not sup:
        sup = PlanSupervisor(plan_id=plan_id, team_id=team_id, orchestrator_claw_id=claw_id,
                             start_hash=hashed, status='waiting', cursor=0,
                             acknowledged_cursor=0, fencing_token=0, expired_turns=0)
        db.session.add(sup)
    def apply():
        plan.status = 'active'
        sup.next_check_at = max(_now(), datetime.combine(plan.start_date, time.min))
        db.session.flush()
        svc.add_event(plan_id, 'plan_started', ['start', plan_id], {'orchestrator_claw_id': claw_id})
        svc.pump(sup)
        return {'scheduled': True}
    result = svc.receipt(sup, 'start', body, apply)
    db.session.commit()
    svc.wake(claw_id)
    return jsonify(result)


@api_bp.route('/test-plans/<int:plan_id>/supervision/claim', methods=['POST'])
def claim_plan_supervision(plan_id):
    sup, claw = self_supervisor(plan_id)
    result = svc.claim(sup, claw.id, payload())
    db.session.commit()
    return jsonify(result)


@api_bp.route('/test-plans/<int:plan_id>/supervision/heartbeat', methods=['POST'])
def heartbeat_plan_supervision(plan_id):
    sup, claw = self_supervisor(plan_id)
    svc.require_lease(sup, claw.id, payload())
    now = _now()
    sup.lease_expires_at = min(now + timedelta(seconds=180), sup.turn_deadline_at)
    db.session.commit()
    return jsonify({'supervision': sup.to_dict()})


@api_bp.route('/test-plans/<int:plan_id>/supervision/decision', methods=['POST'])
def decide_plan_supervision(plan_id):
    sup, claw = self_supervisor(plan_id)
    result = svc.decide(sup, claw.id, payload())
    target = svc.pump(sup)
    db.session.commit()
    if target:
        svc.wake(target)
    return jsonify(result)


@api_bp.route('/test-plans/<int:plan_id>/supervision/stop', methods=['POST'])
def stop_plan_supervision(plan_id):
    _, sup, claw = load(plan_id, write=True)
    if not sup:
        svc.fail('PLAN_SUPERVISION_NOT_STARTED', '计划尚未启动监督')
    body = payload()
    def apply():
        if claw:
            svc.require_lease(sup, claw.id, body)
        svc.stop(sup, 'stopped')
        svc.add_event(plan_id, 'supervisor_stopped', ['stop', body.get('command_key')])
    result = svc.receipt(sup, 'stop', body, apply)
    db.session.commit()
    return jsonify(result)


@api_bp.route('/test-plans/<int:plan_id>/supervision/resume', methods=['POST'])
def resume_plan_supervision(plan_id):
    _, sup, claw = load(plan_id, write=True)
    if claw:
        svc.fail('PLAN_HUMAN_RESUME_REQUIRED', '阻断后的恢复须由登录的计划管理者确认', 403)
    if not sup:
        svc.fail('PLAN_SUPERVISION_NOT_STARTED', '计划尚未启动监督')
    body = payload()
    def apply():
        plan = db.session.get(TestPlan, plan_id)
        if plan.status != 'active' or _now() >= svc.ends_at(plan):
            svc.fail('PLAN_SUPERVISION_INACTIVE', '计划已结束或不在 active 状态')
        if sup.status not in ('blocked', 'stopped'):
            svc.fail('PLAN_SUPERVISION_NOT_PAUSED', '不能重置正在监督的计划')
        sup.status = 'waiting'
        sup.expired_turns = 0
        sup.next_check_at = _now()
        svc.add_event(plan_id, 'supervisor_resumed', ['resume', body.get('command_key')])
        svc.pump(sup)
    result = svc.receipt(sup, 'resume', body, apply)
    db.session.commit()
    svc.wake(sup.orchestrator_claw_id)
    return jsonify(result)


@api_bp.route('/test-plans/<int:plan_id>/supervision/bind-mission', methods=['POST'])
def bind_plan_mission(plan_id):
    sup, claw = self_supervisor(plan_id)
    body = payload()
    def apply():
        svc.require_lease(sup, claw.id, body)
        if type(body.get('mission_id')) is not int or body['mission_id'] <= 0:
            svc.fail('PLAN_BODY_INVALID', 'mission_id 须为正整数', 400)
        mission = db.session.get(WorkflowMission, body.get('mission_id'))
        if not mission or mission.main_claw_id != claw.id or mission.project_id != claw.project_id:
            svc.fail('PLAN_MISSION_FORBIDDEN', '仅能绑定同项目、同监督 Agent 的 Mission', 403)
        if sup.team_id and not AgentTeamMission.query.filter_by(
                mission_id=mission.id, team_id=sup.team_id).first():
            svc.fail('PLAN_MISSION_FORBIDDEN', 'Mission 必须属于计划绑定的团队', 403)
        if sup.mission_id and sup.mission_id != mission.id:
            svc.fail('PLAN_MISSION_ALREADY_BOUND', '本计划已绑定 Mission')
        if sup.mission_id == mission.id:
            return
        other = PlanSupervisor.query.filter_by(mission_id=mission.id).first()
        if other and other.plan_id != plan_id:
            svc.fail('PLAN_MISSION_ALREADY_BOUND', '该 Mission 已属于其他计划')
        sup.mission_id = mission.id
        mission.context_json = dict(mission.context_json or {}, plan_supervision_id=plan_id)
        svc.add_event(plan_id, 'mission_created', ['mission', mission.id], {'mission_id': mission.id})
    result = svc.receipt(sup, 'bind_mission', body, apply)
    db.session.commit()
    return jsonify(result)


# Event callbacks below record source changes in the same transaction. The
# after-request drain is the normal path; timeout_watcher recovers missed wakes.
from app.services.plan_supervision_events import register_events  # noqa: E402
register_events()
