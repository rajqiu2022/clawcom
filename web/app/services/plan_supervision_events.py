"""Transactional event capture and post-commit wake delivery."""
from flask import g, has_app_context, has_request_context, current_app
from sqlalchemy import event, inspect
from sqlalchemy.orm import Session
from app import db
from app.models import TestPlan, TestTask, WorkflowRun, WorkflowRunStep, WorkflowMission, WorkflowMissionDispatch
from app.models_plan_supervision import PlanSupervisor
from app.services import plan_supervision as svc

_registered = False


def plans_for_run(run_id):
    return [r.plan_id for r in db.session.query(PlanSupervisor.plan_id).join(
        WorkflowMissionDispatch, WorkflowMissionDispatch.mission_id == PlanSupervisor.mission_id
    ).filter(WorkflowMissionDispatch.workflow_run_id == run_id).all()]


def _capture(session, flush_context):
    if not has_app_context() or not svc.enabled():
        return
    for obj in list(session.new) + list(session.dirty):
        new = obj in session.new
        kind, key, payload, plans = None, None, None, []
        def changed(names):
            return new or any(inspect(obj).attrs[name].history.has_changes() for name in names)
        if isinstance(obj, TestTask) and changed(('status', 'assignee_claw_id', 'progress')):
            plans = [obj.plan_id]
            kind, key = 'task_changed', ['task', obj.id, obj.status, obj.updated_at]
            payload = {'task_id': obj.id, 'status': obj.status, 'progress': obj.progress}
        elif isinstance(obj, TestPlan) and changed(('status', 'start_date', 'end_date', 'project_id')):
            plans = [obj.id]
            kind, key = 'plan_changed', ['plan', obj.id, obj.status, obj.start_date, obj.end_date, obj.project_id]
            payload = {'status': obj.status}
        elif isinstance(obj, WorkflowMissionDispatch) and new:
            plans = [r.plan_id for r in session.query(PlanSupervisor.plan_id).filter_by(mission_id=obj.mission_id)]
            kind, key, payload = 'run_created', ['dispatch', obj.id], {'run_id': obj.workflow_run_id}
        elif isinstance(obj, WorkflowMission) and changed(('status',)):
            plans = [r.plan_id for r in session.query(PlanSupervisor.plan_id).filter_by(mission_id=obj.id)]
            kind, key, payload = 'mission_changed', ['mission-state', obj.id, obj.status], {'mission_id': obj.id, 'status': obj.status}
        elif isinstance(obj, WorkflowRun) and changed(('status', 'current_step_id')):
            plans = plans_for_run(obj.id)
            kind = 'run_changed' if obj.status in ('running', 'retrying', 'pending', 'waiting_approval') else 'run_terminal'
            key, payload = ['run', obj.id, obj.status, obj.current_step_id, obj.updated_at], {'run_id': obj.id, 'status': obj.status}
        elif isinstance(obj, WorkflowRunStep) and changed(('status', 'progress_phase', 'progress_percent', 'progress_message', 'progress_json', 'health_status')):
            plans = plans_for_run(obj.run_id)
            kind = 'heartbeat_anomaly' if obj.health_status not in (None, '', 'healthy', 'idle') else 'step_progress'
            signature = svc.digest([obj.status, obj.progress_phase, obj.progress_percent,
                                    obj.progress_message, obj.progress_json])
            key = (['step-health', obj.id, obj.health_status, obj.heartbeat_at]
                   if kind == 'heartbeat_anomaly' else ['step-observation', obj.id, signature])
            payload = {'run_id': obj.run_id, 'step_id': obj.step_id, 'status': obj.status,
                       'health_status': obj.health_status, 'progress_percent': obj.progress_percent}
        if not kind:
            continue
        for plan_id in plans:
            # Unsupervised Plans/Workflows are unchanged.
            if session.query(PlanSupervisor.plan_id).filter_by(plan_id=plan_id).first():
                svc.add_event(plan_id, kind, key, payload)
                if has_request_context():
                    g.plan_supervision_pending = getattr(g, 'plan_supervision_pending', set()) | {plan_id}


def note_rejection(run, step, operation, code='HUB_LIFECYCLE_CONFLICT'):
    if not svc.enabled() or not has_request_context() or operation not in ('heartbeat', 'progress'):
        return
    from app.api.auth_utils import get_current_claw
    from app.api.workflows import _step_belongs_to_claw
    claw = get_current_claw()
    if claw and step and _step_belongs_to_claw(step, claw.id):
        g.plan_supervision_rejection = (run.id, step.step_id, run.status, operation, code)


def drain(response):
    if not svc.enabled():
        return response
    plans = getattr(g, 'plan_supervision_pending', set()) if response.status_code < 400 else set()
    rejection = getattr(g, 'plan_supervision_rejection', None)
    if not plans and not rejection:
        return response
    # Source routes have committed already; never commit a failed source write.
    db.session.rollback()
    try:
        if rejection:
            run_id, step_id, status, operation, code = rejection
            plans |= set(plans_for_run(run_id))
            for plan_id in plans:
                svc.add_event(plan_id, 'heartbeat_anomaly', ['rejected', *rejection],
                              {'run_id': run_id, 'step_id': step_id, 'code': code})
            db.session.commit()
        for plan_id in plans:
            sup = svc.locked(plan_id)
            if not sup:
                continue
            target = svc.pump(sup)
            db.session.commit()
            if target:
                svc.wake(target)
    except Exception:
        db.session.rollback()
        current_app.logger.exception('Plan supervisor wake deferred to watchdog')
    return response


def register_events():
    global _registered
    if not _registered:
        event.listen(Session, 'after_flush', _capture)
        from app.api import api_bp
        api_bp.after_request(drain)
        _registered = True
