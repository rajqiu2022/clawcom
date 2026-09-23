"""Lease, fencing and deployment contracts for ordinary AgentTask rows.

Workflow AgentTasks have their own Run/Step claim contract and deliberately do
not use this module's lease.  This state machine protects the older, generic
AgentTask channel used by Hub-managed operations.
"""
import hashlib
import json
import re
import secrets
from datetime import timedelta

from app import db
from app.models import AgentTask, AuditLog, WorkerRelease, _now


AGENT_TASK_LEASE_SECONDS = 300
AGENT_TASK_MAX_LEASE_SECONDS = 1800
AGENT_TASK_UNLEASED_TIMEOUT_SECONDS = 600
AGENT_TASK_MAX_RETRIES = 5
DEPLOYMENT_KINDS = frozenset((
    'hub_application', 'worker_release', 'agent_instance'))
WORKER_PLATFORMS = frozenset(('linux-x86_64', 'windows-x86_64'))
HUB_REPOSITORIES = frozenset((
    'https://git.woa.com/J1_QA_Group1/clawteam',
))
_SHA40 = re.compile(r'^[0-9a-f]{40}$')
_SHA256 = re.compile(r'^[0-9a-f]{64}$')
_REF = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$')


class AgentTaskContractError(ValueError):
    def __init__(self, code, message, status=422):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def task_payload(task_or_payload):
    value = getattr(task_or_payload, 'payload', task_or_payload)
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            value = {}
    return value if isinstance(value, dict) else {}


def _repository(value):
    repository = str(value or '').strip().rstrip('/')
    if repository.endswith('.git'):
        repository = repository[:-4]
    return repository


def _deployment_shaped(payload):
    fields = {
        'deployment_kind', 'source_repository', 'source_branch',
        'source_ref', 'source_commit', 'release_id',
        'release_manifest_sha256', 'artifact_sha256', 'platform',
        'worker_release_record_id', 'target_claw_id',
    }
    return any(name in payload for name in fields)


def validate_deployment_contract(payload, target_claw_id=None):
    """Validate a deployment-shaped ordinary AgentTask before it is created.

    Returns a normalized shallow copy. Non-deployment payloads pass through.
    """
    normalized = dict(payload or {})
    if not _deployment_shaped(normalized):
        return normalized
    kind = str(normalized.get('deployment_kind') or '').strip()
    if not kind:
        raise AgentTaskContractError(
            'DEPLOYMENT_KIND_REQUIRED',
            'deployment_kind is required for deployment tasks')
    if kind not in DEPLOYMENT_KINDS:
        raise AgentTaskContractError(
            'DEPLOYMENT_KIND_INVALID',
            'deployment_kind must be hub_application, worker_release or agent_instance')
    normalized['deployment_kind'] = kind

    if kind == 'hub_application':
        repository = _repository(normalized.get('source_repository'))
        branch = str(
            normalized.get('source_branch')
            or normalized.get('source_ref') or '').strip()
        commit = str(normalized.get('source_commit') or '').strip().lower()
        if repository not in HUB_REPOSITORIES:
            raise AgentTaskContractError(
                'HUB_REPOSITORY_MISMATCH',
                'hub_application must reference the approved Hub repository')
        if not branch or not _REF.fullmatch(branch) or '..' in branch:
            raise AgentTaskContractError(
                'HUB_SOURCE_REF_INVALID',
                'hub_application source branch is invalid')
        if not _SHA40.fullmatch(commit):
            raise AgentTaskContractError(
                'HUB_SOURCE_COMMIT_INVALID',
                'hub_application source commit must be a full 40 character SHA')
        if normalized.get('release_id') or normalized.get('worker_release_record_id'):
            raise AgentTaskContractError(
                'HUB_WORKER_RELEASE_MIXED',
                'hub_application cannot enter the Worker Release contract')
        normalized.update({
            'source_repository': repository,
            'source_branch': branch,
            'source_commit': commit,
        })
        return normalized

    if kind == 'worker_release':
        release_id = str(normalized.get('release_id') or '').strip()
        commit = str(normalized.get('source_commit') or '').strip().lower()
        manifest_sha = str(
            normalized.get('release_manifest_sha256') or '').strip().lower()
        artifact_sha = str(
            normalized.get('artifact_sha256') or '').strip().lower()
        platform = str(normalized.get('platform') or '').strip().lower()
        if not _SHA40.fullmatch(commit) or release_id != 'worker-' + commit:
            raise AgentTaskContractError(
                'WORKER_RELEASE_IDENTITY_MISMATCH',
                'worker_release release_id must equal worker-<source_commit>')
        if not _SHA256.fullmatch(manifest_sha):
            raise AgentTaskContractError(
                'WORKER_RELEASE_MANIFEST_INVALID',
                'worker_release manifest SHA-256 is invalid')
        if not _SHA256.fullmatch(artifact_sha):
            raise AgentTaskContractError(
                'WORKER_RELEASE_ARTIFACT_INVALID',
                'worker_release artifact SHA-256 is invalid')
        if platform not in WORKER_PLATFORMS:
            raise AgentTaskContractError(
                'WORKER_RELEASE_PLATFORM_INVALID',
                'worker_release platform is invalid')
        record = WorkerRelease.query.filter_by(
            release_id=release_id,
            source_commit=commit,
            release_manifest_sha256=manifest_sha,
            platform=platform,
            artifact_sha256=artifact_sha,
            approval_status='approved',
        ).first()
        if not record:
            raise AgentTaskContractError(
                'WORKER_RELEASE_NOT_APPROVED',
                'worker_release does not match an approved immutable Catalog record')
        requested_record = normalized.get('worker_release_record_id')
        if requested_record not in (None, ''):
            try:
                requested_record = int(requested_record)
            except (TypeError, ValueError):
                requested_record = 0
            if requested_record != record.id:
                raise AgentTaskContractError(
                    'WORKER_RELEASE_RECORD_MISMATCH',
                    'worker_release record ID does not match the immutable artifact')
        normalized['worker_release_record_id'] = record.id
        normalized['source_repository'] = record.source_repository
        return normalized

    forbidden = {
        'source_repository', 'source_branch', 'source_ref', 'source_commit',
        'release_id', 'release_manifest_sha256', 'artifact_sha256',
        'worker_release_record_id',
    }
    if any(normalized.get(name) not in (None, '') for name in forbidden):
        raise AgentTaskContractError(
            'AGENT_INSTANCE_SOURCE_MIXED',
            'agent_instance cannot carry Hub or Worker release source fields')
    if target_claw_id is not None and normalized.get('target_claw_id') not in (None, ''):
        try:
            requested_target = int(normalized['target_claw_id'])
        except (TypeError, ValueError):
            requested_target = 0
        if requested_target != int(target_claw_id):
            raise AgentTaskContractError(
                'AGENT_INSTANCE_TARGET_MISMATCH',
                'agent_instance target does not match the dispatch Claw')
    normalized['target_claw_id'] = int(target_claw_id) if target_claw_id else None
    return normalized


def normalize_retry_max(value):
    try:
        retry_max = int(value or 0)
    except (TypeError, ValueError):
        raise AgentTaskContractError(
            'AGENT_TASK_RETRY_INVALID', 'retry_max must be an integer', 400)
    if retry_max < 0 or retry_max > AGENT_TASK_MAX_RETRIES:
        raise AgentTaskContractError(
            'AGENT_TASK_RETRY_INVALID',
            'retry_max must be between 0 and %d' % AGENT_TASK_MAX_RETRIES,
            400)
    return retry_max


def _lease_seconds(value=None):
    try:
        seconds = int(value or AGENT_TASK_LEASE_SECONDS)
    except (TypeError, ValueError):
        seconds = AGENT_TASK_LEASE_SECONDS
    return max(30, min(seconds, AGENT_TASK_MAX_LEASE_SECONDS))


def _audit_transition(action, task, detail, operator='hub-agent-task-watcher'):
    db.session.add(AuditLog(
        action=action,
        resource_type='agent_task',
        resource_id=task.id,
        resource_name=task.task_id,
        operator=operator,
        detail=json.dumps(
            detail, ensure_ascii=False, sort_keys=True)))


def claim_pending_tasks(claw_id, limit=10, now=None, lease_seconds=None):
    """Atomically claim ordinary pending tasks for one Claw."""
    now = now or _now()
    seconds = _lease_seconds(lease_seconds)
    candidates = (AgentTask.query.filter(
        AgentTask.claw_id == claw_id,
        AgentTask.status == 'pending',
        AgentTask.task_type != 'workflow_agent_task',
    ).order_by(AgentTask.created_at.asc(), AgentTask.id.asc())
        .limit(max(1, min(int(limit or 10), 50))).all())
    claimed = []
    for candidate in candidates:
        try:
            validate_deployment_contract(
                task_payload(candidate), target_claw_id=claw_id)
        except AgentTaskContractError as exc:
            before = candidate.status
            candidate.status = 'failed'
            candidate.error = exc.message
            candidate.terminal_reason = 'invalid_deployment_contract'
            candidate.completed_at = now
            candidate.version = int(candidate.version or 0) + 1
            _audit_transition('contract_rejected', candidate, {
                'before': before,
                'after': candidate.status,
                'code': exc.code,
                'reason': exc.message,
            })
            db.session.commit()
            continue
        expected_version = int(candidate.version or 0)
        claim_token = secrets.token_hex(32)
        updated = AgentTask.query.filter(
            AgentTask.id == candidate.id,
            AgentTask.claw_id == claw_id,
            AgentTask.status == 'pending',
            AgentTask.version == expected_version,
        ).update({
            AgentTask.status: 'running',
            AgentTask.assigned_at: now,
            AgentTask.last_heartbeat_at: now,
            AgentTask.lease_expires_at: now + timedelta(seconds=seconds),
            AgentTask.attempt_no: int(candidate.attempt_no or 0) + 1,
            AgentTask.claim_token: claim_token,
            AgentTask.fencing_token: int(candidate.fencing_token or 0) + 1,
            AgentTask.version: expected_version + 1,
            AgentTask.completed_at: None,
            AgentTask.terminal_reason: '',
        }, synchronize_session=False)
        if updated == 1:
            db.session.commit()
            claimed_task = db.session.get(AgentTask, candidate.id)
            # A supervised direct TestTask uses the same durable claim as any
            # ordinary AgentTask.  Promote its plan/stage only after the real
            # Worker claim exists; Todo delivery is never an execution receipt.
            from app.services import plan_supervision
            plan_supervision.record_agent_task_claim(
                claimed_task, now=now)
            db.session.commit()
            claimed.append(claimed_task)
        else:
            db.session.rollback()
    return claimed


def _claim_error(task, data, now=None):
    now = now or _now()
    if task.status != 'running':
        return 'AGENT_TASK_TERMINAL', 'AgentTask is no longer running'
    if task.lease_expires_at and task.lease_expires_at <= now:
        return 'AGENT_TASK_LEASE_EXPIRED', 'AgentTask lease has expired'
    try:
        attempt_no = int(data.get('attempt_no'))
        fencing_token = int(data.get('fencing_token'))
    except (TypeError, ValueError):
        return 'AGENT_TASK_CLAIM_REQUIRED', 'attempt_no and fencing_token are required'
    if (
        not data.get('claim_token')
        or not secrets.compare_digest(
            str(data.get('claim_token')), str(task.claim_token or ''))
        or attempt_no != int(task.attempt_no or 0)
        or fencing_token != int(task.fencing_token or 0)
    ):
        return 'AGENT_TASK_FENCING_STALE', 'AgentTask claim is stale'
    return None, None


def heartbeat_task(task, data, now=None):
    now = now or _now()
    code, message = _claim_error(task, data, now=now)
    if code:
        raise AgentTaskContractError(code, message, 409)
    progress = data.get('progress')
    duplicate = False
    if progress is not None:
        if not isinstance(progress, dict):
            raise AgentTaskContractError(
                'AGENT_TASK_PROGRESS_INVALID', 'progress must be an object', 400)
        encoded = json.dumps(
            progress, ensure_ascii=False, sort_keys=True,
            separators=(',', ':'))
        digest = hashlib.sha256(encoded.encode('utf-8')).hexdigest()
        duplicate = digest == (task.progress_digest or '')
        if not duplicate:
            task.progress_digest = digest
            task.progress_json = encoded
            task.progress_at = now
    task.last_heartbeat_at = now
    task.lease_expires_at = now + timedelta(
        seconds=_lease_seconds(data.get('lease_seconds')))
    task.version = int(task.version or 0) + 1
    db.session.commit()
    return duplicate


def _structured_result(data):
    result = data.get('result')
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
            result = parsed if isinstance(parsed, dict) else {'summary': result}
        except (TypeError, ValueError):
            result = {'summary': result}
    elif result is None:
        result = {}
    if not isinstance(result, dict):
        raise AgentTaskContractError(
            'AGENT_TASK_RESULT_INVALID', 'result must be an object or string', 400)
    for field in ('error_code', 'reason', 'retryable', 'evidence'):
        if field in data and field not in result:
            result[field] = data[field]
    return result


def complete_task(task, data, now=None):
    now = now or _now()
    code, message = _claim_error(task, data, now=now)
    if code:
        db.session.add(AuditLog(
            action='late_result', resource_type='agent_task',
            resource_id=task.id, resource_name=task.task_id,
            operator='claw-%s' % task.claw_id,
            detail=json.dumps({
                'code': code,
                'attempt_no': data.get('attempt_no'),
                'fencing_token': data.get('fencing_token'),
            }, ensure_ascii=False, sort_keys=True)))
        db.session.commit()
        raise AgentTaskContractError(code, message, 409)
    result = _structured_result(data)
    raw_status = str(data.get('status') or result.get('status') or 'completed').lower()
    if raw_status in ('completed', 'passed', 'succeeded', 'success'):
        status = 'completed'
    elif raw_status in ('failed', 'blocked'):
        status = raw_status
    else:
        raise AgentTaskContractError(
            'AGENT_TASK_STATUS_INVALID',
            'status must be completed, failed or blocked', 400)
    retryable = result.get('retryable') is True
    if status == 'failed' and retryable and int(task.retry_count or 0) < int(task.retry_max or 0):
        task.status = 'pending'
        task.retry_count = int(task.retry_count or 0) + 1
        task.claim_token = None
        task.lease_expires_at = None
        task.last_heartbeat_at = None
        task.assigned_at = None
        task.version = int(task.version or 0) + 1
        task.error = str(result.get('reason') or data.get('error') or '')[:2000]
        db.session.commit()
        return 'retried'
    task.status = status
    task.result = json.dumps(result, ensure_ascii=False, sort_keys=True)
    task.error = str(result.get('reason') or data.get('error') or '')[:2000]
    task.terminal_reason = str(
        result.get('error_code') or status)[:128]
    task.completed_at = now
    task.lease_expires_at = None
    task.claim_token = None
    task.version = int(task.version or 0) + 1
    from app.services import plan_supervision
    plan_supervision.record_agent_task_terminal(
        task, result, status, now=now)
    db.session.commit()
    return 'completed'


def expire_stale_ordinary_tasks(now=None, limit=500):
    """Expire or finitely retry ordinary tasks with invalid/stale contracts."""
    from app.services import plan_supervision

    now = now or _now()
    rows = (AgentTask.query.filter(
        AgentTask.task_type != 'workflow_agent_task',
        AgentTask.status.in_(('pending', 'running')),
    ).order_by(AgentTask.id.asc()).limit(limit).all())
    changed = 0
    for task in rows:
        payload = task_payload(task)
        try:
            validate_deployment_contract(payload, target_claw_id=task.claw_id)
        except AgentTaskContractError as exc:
            before = task.status
            task.status = 'failed'
            task.error = exc.message
            task.terminal_reason = 'invalid_deployment_contract'
            task.completed_at = now
            task.claim_token = None
            task.lease_expires_at = None
            task.version = int(task.version or 0) + 1
            _audit_transition('contract_rejected', task, {
                'before': before,
                'after': task.status,
                'code': exc.code,
                'reason': exc.message,
            })
            plan_supervision.record_agent_task_terminal(task, {
                'status': 'failed',
                'error_code': task.terminal_reason,
                'reason': task.error,
                'retryable': False,
            }, 'failed', now=now)
            changed += 1
            continue
        expired = False
        if task.status == 'running':
            if task.lease_expires_at:
                expired = task.lease_expires_at <= now
            elif task.assigned_at:
                expired = task.assigned_at <= now - timedelta(
                    seconds=AGENT_TASK_UNLEASED_TIMEOUT_SECONDS)
        if not expired:
            continue
        if int(task.retry_count or 0) < int(task.retry_max or 0):
            before = task.status
            task.status = 'pending'
            task.retry_count = int(task.retry_count or 0) + 1
            task.assigned_at = None
            task.last_heartbeat_at = None
            task.lease_expires_at = None
            task.claim_token = None
            task.error = 'agent_task_lease_expired_retrying'
            _audit_transition('lease_expired_retry', task, {
                'before': before,
                'after': task.status,
                'retry_count': task.retry_count,
                'retry_max': task.retry_max,
            })
            plan_supervision.record_agent_task_retry_pending(
                task, now=now)
        else:
            before = task.status
            task.status = 'failed'
            task.error = 'agent_task_lease_expired'
            task.terminal_reason = 'agent_task_lease_expired'
            task.completed_at = now
            task.lease_expires_at = None
            task.claim_token = None
            _audit_transition('lease_expired', task, {
                'before': before,
                'after': task.status,
                'retry_count': task.retry_count,
                'retry_max': task.retry_max,
            })
            plan_supervision.record_agent_task_terminal(task, {
                'status': 'failed',
                'error_code': task.terminal_reason,
                'reason': '执行心跳超时，任务未完成',
                'retryable': False,
            }, 'failed', now=now)
        task.version = int(task.version or 0) + 1
        changed += 1
    if changed:
        db.session.commit()
    return changed
