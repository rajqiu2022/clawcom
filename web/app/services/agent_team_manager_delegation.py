"""Temporary handover of a team's primary test manager.

An operator sometimes has to move primary-manager authority to the configured
backup manager while the incumbent is unavailable — for example the incumbent's
model Provider is out of quota and its supervision turns keep failing at the
provider layer.

This cannot be a bare ``primary_manager_claw_id`` write. Plan supervision binds
the manager identity twice:

* ``PlanSupervisor.orchestrator_claw_id`` is the only caller accepted by
  ``dispatch_test_task`` / ``claim`` / ``heartbeat``;
* ``PlanSupervisor.start_hash`` is a frozen digest of
  ``{plan_id, team_id, orchestrator_claw_id}`` and ``bootstrap`` rejects any
  other identity with ``PLAN_ALREADY_SUPERVISED``.

So a column-only swap leaves every supervised Plan permanently unusable
(``PLAN_TEAM_NOT_ENABLED`` on tenure, 403 on dispatch) while the stored binding
still claims the old manager. This module performs the whole handover in the
caller's transaction — team roles, supervisor identity, start hash, fencing
token, supervisor Mission owner and the team-Mission snapshot — and keeps enough
provenance to reverse it exactly.

Scope of authority: the delegate must already hold the team's backup-manager
slot. That single precondition makes the swap lossless (nobody loses a slot) and
exactly reversible, because the displaced primary simply takes the backup slot.
"""

import copy
import json
from datetime import datetime, timedelta, timezone

from flask import current_app

from app import db
from app.models import (AgentTeam, AgentTeamMission, AuditLog, OpenClawInstance,
                        WorkflowMission, _now)
from app.models_plan_supervision import PlanSupervisor
from app.services import plan_supervision as svc
from app.services.agent_teams import TeamError

# How many finished handovers stay inline in the team row. The durable audit
# trail is AuditLog + PlanSupervisorEvent; this is the human-facing shortlist.
HISTORY_LIMIT = 5
# A leased/retryable supervisor can be re-pointed; a terminal one must be
# restarted explicitly, so the handover only refreshes its identity.
TERMINAL_STATUSES = {'stopped', 'expired'}
CHINA_TZ = timezone(timedelta(hours=8))


def _truthy(value):
    return value is True or str(value).lower() in ('1', 'true', 'yes', 'on')


def enabled():
    """Feature gate; defaults to the team-pilot switch when not set."""
    flag = current_app.config.get('PLAN_MANAGER_DELEGATION_ENABLED')
    if flag is None:
        flag = current_app.config.get('AGENT_TEAMS_ENABLED', False)
    return _truthy(flag)


def _stamp(value):
    if not value:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=CHINA_TZ)
    return value.isoformat()


def _parse_stamp(value):
    """Parse a stored ISO 8601 stamp into a naive Asia/Shanghai datetime."""
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).strip())
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(CHINA_TZ).replace(tzinfo=None)
    return parsed


def _deadline(value):
    """Parse an optional ISO 8601 input deadline; naive input means Asia/Shanghai."""
    if value in (None, ''):
        return None
    parsed = _parse_stamp(value)
    if parsed is None:
        raise TeamError(
            'TEAM_DELEGATION_DEADLINE_INVALID',
            'expires_at 必须是 ISO 8601 时间，例如 2026-09-26T18:00:00+08:00', 400)
    if parsed <= _now():
        raise TeamError(
            'TEAM_DELEGATION_DEADLINE_INVALID', 'expires_at 必须晚于当前时间', 400)
    return parsed


def _claw(claw_id, project_id, field):
    row = db.session.get(OpenClawInstance, claw_id) if claw_id else None
    if not row or row.status == 'deleted' or row.project_id != project_id:
        raise TeamError(
            'TEAM_MEMBER_SCOPE_INVALID',
            '%s 必须存在且属于同一项目' % field, 400)
    return row


def _locked_team(team_id):
    team = (AgentTeam.query.filter_by(id=team_id)
            .populate_existing().with_for_update().first())
    if not team:
        raise TeamError('TEAM_NOT_FOUND', '团队不存在', 404)
    return team


def state(team):
    """Return the delegation state in the response envelope used by the API."""
    record = team.manager_delegation_json or {}
    active = bool(record.get('active'))
    # A stored deadline may already be in the past; that is exactly the state
    # ``expired`` reports, so parse without the future-only input validation.
    deadline = _parse_stamp(record.get('expires_at')) if active else None
    return {
        'active': active,
        'delegate_claw_id': record.get('delegate_claw_id') if active else None,
        'delegate_name': (record.get('delegate_name') or '') if active else '',
        'previous_primary_claw_id': (
            record.get('previous_primary_claw_id') if active else None),
        'previous_primary_name': (
            (record.get('previous_primary_name') or '') if active else ''),
        'started_at': record.get('started_at') if active else None,
        'started_by': (record.get('started_by') or '') if active else '',
        'reason': (record.get('reason') or '') if active else '',
        'expires_at': _stamp(deadline),
        'expired': bool(active and deadline and deadline <= _now()),
        'revision': int(record.get('revision') or 0),
        'supervisors': list(record.get('supervisors') or []),
        'revoked_at': record.get('revoked_at'),
        'revoked_by': record.get('revoked_by') or '',
        'revoke_reason': record.get('revoke_reason') or '',
        'history_count': len(record.get('history') or []),
        'mode': 'temporary_primary_handover',
    }


def _rebind_supervisor(sup, team, previous_primary, new_primary, record, now):
    """Move one supervised Plan to the new manager with a fresh fence."""
    old_hash, old_token, old_status = sup.start_hash, int(sup.fencing_token or 0), sup.status
    sup.orchestrator_claw_id = new_primary
    sup.start_hash = svc.digest({
        'plan_id': sup.plan_id, 'team_id': team.id,
        'orchestrator_claw_id': new_primary,
    })
    svc.cancel_wake(sup)
    sup.fencing_token = old_token + 1
    sup.lease_owner = None
    sup.lease_expires_at = None
    sup.turn_deadline_at = None
    if sup.status not in TERMINAL_STATUSES:
        # The displaced manager may be mid-turn. Drop that turn (the fence makes
        # its late writes reject) and hand the wake to the new manager now.
        sup.status = 'retryable'
        sup.next_check_at = now
        sup.resume_condition = 'timer_or_event'
    mission = db.session.get(WorkflowMission, sup.mission_id) if sup.mission_id else None
    if mission:
        mission.main_claw_id = new_primary
        mission.version = int(mission.version or 1) + 1
        binding = db.session.get(AgentTeamMission, mission.id)
        if binding:
            snapshot = copy.deepcopy(binding.snapshot_json or {})
            snapshot['primary_manager_claw_id'] = new_primary
            snapshot['backup_manager_claw_id'] = team.backup_manager_claw_id
            snapshot['manager_epoch_at_creation'] = team.manager_epoch
            snapshot['manager_delegation'] = {
                'active': bool(record.get('active')),
                'delegate_claw_id': new_primary,
                'previous_primary_claw_id': previous_primary,
                'revision': record.get('revision'),
                'started_by': record.get('started_by'),
                'at': record.get('started_at'),
            }
            binding.snapshot_json = snapshot
            binding.team_version = team.version
    svc.add_event(sup.plan_id, 'manager_delegation', [
        'manager-delegation', sup.plan_id, record.get('revision'),
    ], {
        'from_claw_id': previous_primary, 'to_claw_id': new_primary,
        'team_id': team.id, 'manager_epoch': team.manager_epoch,
        'previous_status': old_status, 'fencing_token': sup.fencing_token,
        'started_by': record.get('started_by'), 'reason': record.get('reason'),
    }, now)
    db.session.flush()
    return {
        'plan_id': sup.plan_id, 'rebound': True, 'status': sup.status,
        'from_claw_id': previous_primary, 'to_claw_id': new_primary,
        'fencing_token': sup.fencing_token,
        'start_hash_changed': old_hash != sup.start_hash,
        'mission_id': sup.mission_id,
        'next_check_at': _stamp(sup.next_check_at),
    }


def _apply_handover(team, record, new_primary, new_backup, now):
    """Swap team roles and re-point every Plan this team supervises."""
    previous_primary = team.primary_manager_claw_id
    team.primary_manager_claw_id = new_primary
    team.backup_manager_claw_id = new_backup
    team.version = int(team.version or 1) + 1
    team.manager_epoch = int(team.manager_epoch or 0) + 1
    team.manager_session_id = 'team-manager:%s:%s:%s' % (
        team.id, team.version, new_primary)
    team.manager_lease_expires_at = None
    team.active_manager_claw_id = new_primary
    db.session.flush()

    rebound = []
    for sup in (PlanSupervisor.query.filter_by(team_id=team.id)
                .order_by(PlanSupervisor.plan_id).all()):
        if sup.orchestrator_claw_id == previous_primary:
            rebound.append(_rebind_supervisor(
                sup, team, previous_primary, new_primary, record, now))
        elif sup.orchestrator_claw_id != new_primary:
            # Anomalous binding: never silently steal another manager's Plan.
            rebound.append({
                'plan_id': sup.plan_id, 'rebound': False,
                'reason': 'orchestrator_not_team_primary',
                'from_claw_id': sup.orchestrator_claw_id,
            })
    return rebound


def _audit(action, team, record, actor_name, actor_ip, detail):
    db.session.add(AuditLog(
        action=action, resource_type='agent_team', resource_id=team.id,
        resource_name=team.name, operator=str(actor_name or '')[:160],
        ip_address=str(actor_ip or '')[:64],
        detail=svc.json.dumps(detail, ensure_ascii=False, sort_keys=True)))


def delegate(team_id, delegate_claw_id, actor_name, actor_ip='', reason='',
             expires_at=None, expected_version=None):
    """Promote the configured backup manager to primary until further notice.

    Returns the new team payload, the delegation state and one rebinding record
    per supervised Plan. The caller owns the commit and the post-commit wake.
    """
    if not enabled():
        raise TeamError(
            'TEAM_DELEGATION_DISABLED', '测试经理临时转正开关未开启', 404)
    team = _locked_team(team_id)
    if team.status != 'active':
        raise TeamError('TEAM_NOT_ACTIVE', '暂停/归档的团队不接受经理转正', 409)
    if expected_version is not None and int(expected_version) != int(team.version):
        raise TeamError('TEAM_VERSION_CONFLICT', '团队配置已更新，请回读重试', 409)
    current = team.manager_delegation_json or {}
    if current.get('active'):
        raise TeamError(
            'TEAM_DELEGATION_ALREADY_ACTIVE',
            '已有生效中的经理转正，请先回退后再转正其他人', 409)
    if isinstance(delegate_claw_id, bool) or not isinstance(delegate_claw_id, int):
        raise TeamError('TEAM_VALIDATION_FAILED', 'delegate_claw_id 必须是整数', 400)
    if delegate_claw_id == team.primary_manager_claw_id:
        raise TeamError(
            'TEAM_DELEGATION_TARGET_INVALID', '目标 Agent 已经是主测试经理', 400)
    if team.backup_manager_claw_id != delegate_claw_id:
        raise TeamError(
            'TEAM_DELEGATION_TARGET_INVALID',
            '仅当前配置的替补测试经理可以临时转正；请先在团队配置中把它设为主备经理', 400)
    delegate = _claw(delegate_claw_id, team.project_id, 'delegate_claw_id')
    incumbent = _claw(team.primary_manager_claw_id, team.project_id,
                      'primary_manager_claw_id')
    now = _now()
    record = {
        'active': True,
        'revision': int(current.get('revision') or 0) + 1,
        'delegate_claw_id': delegate.id,
        'delegate_name': delegate.name or 'claw:%s' % delegate.id,
        'previous_primary_claw_id': incumbent.id,
        'previous_primary_name': incumbent.name or 'claw:%s' % incumbent.id,
        # The delegate held the backup slot (validated above), so reversing the
        # handover must drop it again; the incumbent takes it in the meantime.
        'previous_backup_claw_id': None,
        'started_at': _stamp(now),
        'started_by': str(actor_name or '')[:160],
        'actor_ip': str(actor_ip or '')[:64],
        'reason': str(reason or '')[:500],
        'expires_at': _stamp(_deadline(expires_at)),
        'supervisors': [],
        'history': (current.get('history') or [])[-HISTORY_LIMIT:],
    }
    supervisors = _apply_handover(
        team, record, delegate.id, incumbent.id, now)
    record['supervisors'] = supervisors
    record['wake_claw_id'] = (
        delegate.id if any(row.get('rebound') for row in supervisors) else None)
    team.manager_delegation_json = record
    _audit('manager_delegation', team, record, actor_name, actor_ip, {
        'event': 'delegate',
        'team_id': team.id,
        'delegate_claw_id': delegate.id,
        'previous_primary_claw_id': incumbent.id,
        'new_backup_claw_id': team.backup_manager_claw_id,
        'manager_epoch': team.manager_epoch,
        'team_version': team.version,
        'expires_at': record['expires_at'],
        'reason': record['reason'],
        'rebound_plan_ids': [row['plan_id'] for row in supervisors
                             if row.get('rebound')],
        'skipped': [row for row in supervisors if not row.get('rebound')],
    })
    db.session.flush()
    return {
        'delegation': state(team),
        'team': team.to_dict(),
        'supervisors': supervisors,
        'wake_claw_id': record['wake_claw_id'],
    }


def revoke(team_id, actor_name, actor_ip='', reason='', expected_version=None):
    """Restore the original primary manager and send the delegate back to backup."""
    if not enabled():
        raise TeamError(
            'TEAM_DELEGATION_DISABLED', '测试经理临时转正开关未开启', 404)
    team = _locked_team(team_id)
    current = copy.deepcopy(team.manager_delegation_json or {})
    if not current.get('active'):
        raise TeamError(
            'TEAM_DELEGATION_NOT_ACTIVE', '当前没有生效中的经理转正', 409)
    if expected_version is not None and int(expected_version) != int(team.version):
        raise TeamError('TEAM_VERSION_CONFLICT', '团队配置已更新，请回读重试', 409)
    restore_primary = current.get('previous_primary_claw_id')
    delegate_id = current.get('delegate_claw_id')
    if not restore_primary or not delegate_id:
        raise TeamError(
            'TEAM_DELEGATION_STATE_CONFLICT', '转正记录缺少原主经理，需人工确认', 409)
    if team.primary_manager_claw_id != delegate_id:
        raise TeamError(
            'TEAM_DELEGATION_STATE_CONFLICT',
            '团队主经理已被其他操作变更（当前 #%s），请人工确认后再回退'
            % team.primary_manager_claw_id, 409)
    incumbent = _claw(restore_primary, team.project_id, 'previous_primary_claw_id')
    now = _now()
    archived = dict(current)
    archived['active'] = False
    archived['revoked_at'] = _stamp(now)
    archived['revoked_by'] = str(actor_name or '')[:160]
    archived['revoke_reason'] = str(reason or '')[:500]
    record = {
        'active': False,
        'revision': int(current.get('revision') or 0) + 1,
        'delegate_claw_id': delegate_id,
        'delegate_name': current.get('delegate_name') or '',
        'previous_primary_claw_id': incumbent.id,
        'previous_primary_name': incumbent.name or current.get('previous_primary_name') or '',
        'previous_backup_claw_id': None,
        'started_at': current.get('started_at'),
        'started_by': current.get('started_by') or '',
        'actor_ip': str(actor_ip or '')[:64],
        'reason': current.get('reason') or '',
        'expires_at': current.get('expires_at'),
        'revoked_at': archived['revoked_at'],
        'revoked_by': archived['revoked_by'],
        'revoke_reason': archived['revoke_reason'],
        'supervisors': [],
        'history': ((current.get('history') or []) + [archived])[-HISTORY_LIMIT:],
    }
    supervisors = _apply_handover(
        team, record, incumbent.id, delegate_id, now)
    record['supervisors'] = supervisors
    record['wake_claw_id'] = (
        incumbent.id if any(row.get('rebound') for row in supervisors) else None)
    team.manager_delegation_json = record
    _audit('manager_delegation', team, record, actor_name, actor_ip, {
        'event': 'revoke',
        'team_id': team.id,
        'restored_primary_claw_id': incumbent.id,
        'delegate_claw_id': delegate_id,
        'new_backup_claw_id': team.backup_manager_claw_id,
        'manager_epoch': team.manager_epoch,
        'team_version': team.version,
        'reason': record['revoke_reason'],
        'rebound_plan_ids': [row['plan_id'] for row in supervisors
                             if row.get('rebound')],
        'skipped': [row for row in supervisors if not row.get('rebound')],
    })
    db.session.flush()
    return {
        'delegation': state(team),
        'team': team.to_dict(),
        'supervisors': supervisors,
        'wake_claw_id': record['wake_claw_id'],
    }


def due_for_revoke(now=None):
    """Return teams whose handover deadline has passed, oldest first."""
    now = now or _now()
    rows = []
    for team in AgentTeam.query.filter(AgentTeam.manager_delegation_json.isnot(None)).all():
        record = team.manager_delegation_json or {}
        if not record.get('active'):
            continue
        deadline = _parse_stamp(record.get('expires_at'))
        if deadline and deadline <= now:
            rows.append((deadline, team))
    return [team for _, team in sorted(rows, key=lambda item: item[0])]
