"""Transactional primitives for the long-running Agent control-plane pilot."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Iterable

from flask import current_app

from app import db
from app.models import (
    AgentControlGate,
    AgentGoal,
    AgentTransitionReceipt,
    AgentTurn,
    GoalTodo,
)
from app.services.agent_authorization import sign_envelope


ACTIVE_GOAL_STATUSES = frozenset({
    'ACTIVE', 'WAITING_USER', 'WAITING_EVIDENCE',
    'REPLAN_REQUIRED', 'REPAIR_REQUIRED',
})
TERMINAL_TODO_STATUSES = frozenset({'completed', 'cancelled', 'superseded'})
READ_ONLY_ACTION_KINDS = frozenset({'read', 'analyze', 'search', 'plan', 'observe'})
VALID_RESULT_KINDS = frozenset({
    'VALIDATED_PROGRESS', 'VALIDATED_COMPLETION', 'WAIT_USER_ACTION',
    'WAIT_EXTERNAL_EVIDENCE', 'REPLAN_REQUIRED', 'REPAIR_REQUIRED',
    'HOST_FAILURE', 'CONTRACT_INVALID', 'VALIDATION_FAILED',
    'WRITEBACK_FAILED', 'QUOTA_SPEND_FAILED', 'CANCELLED',
})
_PRIORITY_ORDER = {'P0': 0, 'P1': 1, 'P2': 2, 'P3': 3}


class ControlPlaneConflict(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class ControlPlaneRejected(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def now_cst_naive() -> datetime:
    # Existing Hub models persist local naive timestamps. Keep the pilot
    # compatible until the wider schema migrates to timezone-aware values.
    return datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None)


def stable_hash(value) -> str:
    rendered = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
    ).encode('utf-8')
    return hashlib.sha256(rendered).hexdigest()


def sha256_ref(value) -> str:
    return f'sha256:{stable_hash(value)}'


def approved_effect_scope(todo: GoalTodo) -> dict:
    preauthorized = todo.authorization_envelope_json or {}
    def string_list(value):
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item).strip()]
    return {
        'write_scope': string_list(preauthorized.get(
            'write_scope', todo.required_write_scopes_json or [])),
        'allowed_effects': string_list(
            preauthorized.get('allowed_effects', [])),
        'requires_gate': string_list(
            preauthorized.get('requires_gate', [])),
    }


def normalize_capabilities(value) -> frozenset[str]:
    if not isinstance(value, list):
        return frozenset()
    return frozenset(
        str(item).strip() for item in value if str(item).strip())


def todo_requires_claim(todo: GoalTodo) -> bool:
    return str(todo.action_kind or '').lower() not in READ_ONLY_ACTION_KINDS


def _gate_is_open(gate: AgentControlGate, now: datetime) -> bool:
    if gate.status != 'open' or gate.policy_level != 'hard':
        return False
    return not gate.expires_at or gate.expires_at > now


def gate_blocks_todo(
    gate: AgentControlGate,
    todo: GoalTodo,
    *,
    now: datetime,
) -> bool:
    if not _gate_is_open(gate, now):
        return False
    if gate.blocking_scope == 'goal':
        return True
    if gate.blocking_scope == 'work_item':
        return gate.work_item_type == 'goal_todo' and gate.work_item_id == todo.id
    # An agent-lane Gate blocks only the explicitly assigned lane. A Gate with
    # no work item cannot silently become a whole-goal blocker.
    if gate.blocking_scope == 'agent_lane':
        return bool(gate.work_item_id and gate.work_item_id == todo.id)
    return False


def _candidate_todos(goal_id: int) -> list[GoalTodo]:
    rows = (GoalTodo.query
            .filter(GoalTodo.goal_id == goal_id)
            .filter(GoalTodo.status.in_(('open', 'claimed', 'waiting')))
            .all())
    return sorted(
        rows,
        key=lambda item: (
            _PRIORITY_ORDER.get(item.priority or 'P1', 9), item.id or 0),
    )


def _todo_available(
    todo: GoalTodo,
    *,
    agent_id: int | None,
    worker_id: str,
    capabilities: frozenset[str],
    gates: Iterable[AgentControlGate],
    now: datetime,
) -> bool:
    if todo.status in TERMINAL_TODO_STATUSES or todo.status == 'waiting':
        return False
    if todo.assigned_agent_id and todo.assigned_agent_id != agent_id:
        return False
    required = frozenset(todo.required_capabilities_json or [])
    if not required.issubset(capabilities):
        return False
    if any(gate_blocks_todo(gate, todo, now=now) for gate in gates):
        return False
    if todo_requires_claim(todo) and todo.status == 'claimed':
        if todo.claim_expires_at and todo.claim_expires_at > now:
            return todo.claimed_by_worker_id == worker_id
    return True


def should_run(
    goal: AgentGoal,
    *,
    agent_id: int | None,
    worker_id: str,
    capabilities: frozenset[str],
    now: datetime | None = None,
) -> dict:
    """Read-only decision. It never grants a lease or execution authority."""
    now = now or now_cst_naive()
    if goal.status not in ACTIVE_GOAL_STATUSES:
        return {
            'should_run': False,
            'decision': 'GOAL_NOT_ACTIVE',
            'reason_code': f'goal_{str(goal.status or "unknown").lower()}',
            'next_eligible_at': None,
            'notification_policy': 'DONT_NOTIFY',
        }
    gates = AgentControlGate.query.filter_by(goal_id=goal.id).all()
    candidates = _candidate_todos(goal.id)
    for todo in candidates:
        if _todo_available(
                todo, agent_id=agent_id, worker_id=worker_id,
                capabilities=capabilities, gates=gates, now=now):
            return {
                'should_run': True,
                'decision': 'READY_FOR_WORK',
                'reason_code': 'ready_todo',
                'candidate_todo': {'id': todo.id, 'version': todo.version},
                'next_eligible_at': None,
                'notification_policy': 'DONT_NOTIFY',
            }
    active_gates = [gate for gate in gates if _gate_is_open(gate, now)]
    if active_gates and candidates:
        return {
            'should_run': False,
            'decision': 'WAIT_USER_ACTION',
            'reason_code': 'no_safe_todo_outside_gate',
            'next_eligible_at': min(
                (gate.expires_at for gate in active_gates if gate.expires_at),
                default=None,
            ),
            'notification_policy': 'NOTIFY_ONCE',
        }
    return {
        'should_run': False,
        'decision': 'NO_WORK',
        'reason_code': 'no_runnable_todo',
        'next_eligible_at': None,
        'notification_policy': 'DONT_NOTIFY',
    }


def dispatch_turn(
    goal: AgentGoal,
    *,
    claw_id: int,
    agent_id: int | None,
    worker_id: str,
    capabilities: frozenset[str],
    idempotency_key: str,
    request_document: dict,
    lease_seconds: int = 180,
    execution_mode: str = 'legacy_unmanaged',
    now: datetime | None = None,
) -> tuple[AgentTurn | None, dict]:
    """Atomically re-evaluate, optionally claim, and create one unique Turn."""
    now = now or now_cst_naive()
    request_hash = stable_hash(request_document)
    existing = AgentTurn.query.filter_by(
        goal_id=goal.id,
        worker_id=worker_id,
        idempotency_key=idempotency_key,
    ).first()
    if existing:
        if existing.request_hash != request_hash:
            raise ControlPlaneConflict(
                'IDEMPOTENCY_KEY_REUSED',
                'dispatch idempotency key was reused with another request')
        return existing, {'replayed': True}

    decision = should_run(
        goal, agent_id=agent_id, worker_id=worker_id,
        capabilities=capabilities, now=now)
    if not decision['should_run']:
        return None, decision

    candidate_id = decision['candidate_todo']['id']
    todo = (GoalTodo.query
            .filter(GoalTodo.id == candidate_id, GoalTodo.goal_id == goal.id)
            .with_for_update()
            .first())
    if not todo:
        raise ControlPlaneConflict('TODO_CHANGED', 'candidate Todo disappeared')

    gates = AgentControlGate.query.filter_by(goal_id=goal.id).all()
    if not _todo_available(
            todo, agent_id=agent_id, worker_id=worker_id,
            capabilities=capabilities, gates=gates, now=now):
        raise ControlPlaneConflict(
            'DISPATCH_RETRY_REQUIRED', 'candidate Todo is no longer runnable')

    # A read-only Todo does not need a write claim, but it still must have only
    # one live Turn. Otherwise two schedulers with different idempotency keys
    # can execute the same observation concurrently and publish conflicting
    # receipts. The Todo row lock above serializes this check on databases that
    # support SELECT FOR UPDATE.
    active_turn = AgentTurn.query.filter_by(
        active_todo_slot=f'goal_todo:{todo.id}').first()
    if active_turn:
        if not active_turn.deadline_at or active_turn.deadline_at > now:
            raise ControlPlaneConflict(
                'TURN_ALREADY_ACTIVE', 'Todo already has an active Turn')
        active_turn.status = 'expired'
        active_turn.active_todo_slot = None

    lease_seconds = max(30, min(int(lease_seconds or 180), 900))
    claim_required = todo_requires_claim(todo)
    claim_expires_at = None
    if claim_required:
        if (todo.status == 'claimed' and todo.claim_expires_at
                and todo.claim_expires_at > now
                and todo.claimed_by_worker_id != worker_id):
            raise ControlPlaneConflict('CLAIM_CONFLICT', 'Todo is already claimed')
        todo.fencing_token = int(todo.fencing_token or 0) + 1
        todo.claimed_by_worker_id = worker_id
        todo.claimed_claw_id = claw_id
        todo.claimed_at = now
        todo.claim_expires_at = now + timedelta(seconds=lease_seconds)
        todo.claim_lease_seconds = lease_seconds
        todo.status = 'claimed'
        todo.version = int(todo.version or 0) + 1
        claim_expires_at = todo.claim_expires_at

    turn_id = f'turn-{uuid.uuid4().hex}'
    deadline_at = now + timedelta(seconds=max(60, lease_seconds - 5))
    execution_mode = str(execution_mode or 'legacy_unmanaged').strip()
    if execution_mode not in ('legacy_unmanaged', 'managed_typed'):
        raise ControlPlaneRejected(
            'EXECUTION_MODE_INVALID', 'unsupported execution mode')
    envelope = {
        'schema_version': 'agent_turn_envelope_v1',
        'control_mode': goal.control_mode or 'shadow',
        'turn_id': turn_id,
        'goal_id': goal.id,
        'todo_id': todo.id,
        'todo_version': todo.version,
        'route': 'READY_FOR_HOST',
        'objective': todo.title,
        'description': todo.description or '',
        'required_capabilities': sorted(todo.required_capabilities_json or []),
        'required_write_scopes': todo.required_write_scopes_json or [],
        'execution_mode': execution_mode,
        'deadline_at': deadline_at.isoformat(),
    }
    authorization_payload = None
    authorization_signature = ''
    authorization_key_id = ''
    if execution_mode == 'managed_typed':
        # Dispatch cannot sign a manifest that does not exist yet. The model
        # session receives planning authority, produces a typed manifest, and
        # then the Worker requests a separate effect authorization bound to
        # that exact content hash.
        envelope['authorization'] = {
            'mode': 'managed_typed',
            'phase': 'effect_manifest_required',
            'effect_authorization_required': True,
            'managed_receipt_allowed': False,
        }
        envelope['approval_scope_hash'] = sha256_ref(
            approved_effect_scope(todo))
    else:
        envelope['authorization'] = {
            'mode': 'legacy_unmanaged',
            'phase': 'planning_only',
            'effect_authorization_required': True,
            'managed_receipt_allowed': False,
        }
    turn = AgentTurn(
        turn_id=turn_id,
        goal_id=goal.id,
        todo_id=todo.id,
        active_todo_slot=f'goal_todo:{todo.id}',
        todo_version=todo.version,
        claw_id=claw_id,
        agent_id=agent_id,
        worker_id=worker_id,
        execution_mode=execution_mode,
        route='READY_FOR_HOST',
        fencing_token=todo.fencing_token or 0,
        claim_expires_at=claim_expires_at,
        schedule_version=goal.version or 1,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        envelope_json=envelope,
        authorization_payload_json=authorization_payload,
        authorization_signature=authorization_signature,
        authorization_key_id=authorization_key_id,
        quota_reserved=True,
        deadline_at=deadline_at,
    )
    db.session.add(turn)
    db.session.flush()
    return turn, {'replayed': False, 'claim_required': claim_required}


def authorize_turn_effects(
    turn: AgentTurn,
    *,
    claw_id: int,
    worker_id: str,
    idempotency_key: str,
    manifest: dict,
    manifest_hash: str,
    approval_scope_hash: str,
    audience: str,
    request_document: dict,
    now: datetime | None = None,
) -> tuple[dict, bool]:
    """Sign phase-two authority for an already materialized typed manifest."""
    now = now or now_cst_naive()
    request_hash = stable_hash(request_document)
    if turn.authorization_idempotency_key:
        if (turn.authorization_idempotency_key == idempotency_key
                and turn.authorization_request_hash == request_hash):
            return dict(
                turn.authorization_payload_json or {},
                signature=turn.authorization_signature or ''), True
        raise ControlPlaneRejected(
            'EFFECT_AUTHORIZATION_ALREADY_ISSUED',
            'Turn already has another effect authorization', 409)
    if turn.claw_id != claw_id or turn.worker_id != worker_id:
        raise ControlPlaneRejected(
            'TURN_OWNER_MISMATCH', 'Turn belongs to another Worker', 403)
    if turn.status != 'dispatched':
        raise ControlPlaneRejected(
            'TURN_NOT_AUTHORIZABLE', 'Turn is no longer active', 409)
    if turn.deadline_at and turn.deadline_at <= now:
        raise ControlPlaneRejected('TURN_EXPIRED', 'Turn deadline has expired', 409)
    if turn.execution_mode != 'managed_typed':
        raise ControlPlaneRejected(
            'EXECUTION_MODE_NOT_MANAGED',
            'Only managed_typed Turns can receive effect authorization', 409)
    if not isinstance(manifest, dict):
        raise ControlPlaneRejected(
            'TYPED_MANIFEST_REQUIRED',
            'managed execution requires the complete typed manifest')
    if manifest.get('schema_version') != 'agent_typed_manifest_v1':
        raise ControlPlaneRejected(
            'TYPED_MANIFEST_SCHEMA_INVALID',
            'manifest schema_version must be agent_typed_manifest_v1')
    actions = manifest.get('actions')
    if not isinstance(actions, list) or not actions:
        raise ControlPlaneRejected(
            'TYPED_MANIFEST_SCHEMA_INVALID',
            'manifest actions must be a non-empty list')
    for action in actions:
        if (not isinstance(action, dict)
                or not str(action.get('action') or '').strip()
                or not isinstance(action.get('params', {}), dict)):
            raise ControlPlaneRejected(
                'TYPED_MANIFEST_SCHEMA_INVALID',
                'each manifest action needs action and object params')
    computed_manifest_hash = sha256_ref(manifest)
    manifest_hash = str(manifest_hash or computed_manifest_hash).strip()
    if manifest_hash != computed_manifest_hash:
        raise ControlPlaneRejected(
            'MANIFEST_HASH_MISMATCH',
            'manifest_hash does not match the submitted typed manifest', 409)
    approval_scope_hash = str(approval_scope_hash or '').strip()
    audience = str(audience or '').strip()
    if not manifest_hash or not approval_scope_hash or not audience:
        raise ControlPlaneRejected(
            'EFFECT_AUTHORIZATION_INPUT_REQUIRED',
            'manifest_hash, approval_scope_hash and audience are required')
    todo = db.session.get(GoalTodo, turn.todo_id)
    if not todo:
        raise ControlPlaneRejected('TODO_NOT_FOUND', 'Turn Todo no longer exists', 409)
    if todo.version != turn.todo_version:
        raise ControlPlaneRejected(
            'TODO_VERSION_CONFLICT', 'Todo changed after dispatch', 409)
    if todo_requires_claim(todo):
        if (todo.claimed_claw_id != claw_id
                or todo.claimed_by_worker_id != worker_id
                or int(todo.fencing_token or 0) != int(turn.fencing_token or 0)):
            raise ControlPlaneRejected(
                'FENCING_TOKEN_STALE', 'Turn no longer owns the Todo claim', 409)
        if todo.claim_expires_at and todo.claim_expires_at <= now:
            raise ControlPlaneRejected('CLAIM_EXPIRED', 'Claim has expired', 409)
    approved_scope = approved_effect_scope(todo)
    expected_scope_hash = sha256_ref(approved_scope)
    if approval_scope_hash != expected_scope_hash:
        raise ControlPlaneRejected(
            'APPROVAL_SCOPE_HASH_MISMATCH',
            'approval_scope_hash does not match the approved Todo scope', 409)
    allowed_effects = frozenset(
        str(item) for item in approved_scope.get('allowed_effects') or [])
    requested_effects = frozenset(
        str(item.get('action') or '') for item in actions)
    if not requested_effects.issubset(allowed_effects):
        raise ControlPlaneRejected(
            'MANIFEST_EFFECT_OUT_OF_SCOPE',
            'typed manifest requests an effect outside the approved scope', 403)
    expected_audience = str(current_app.config.get(
        'AGENT_CONTROL_JOB_SERVICE_AUDIENCE')
        or f'job-service:claw-{claw_id}')
    if audience != expected_audience:
        raise ControlPlaneRejected(
            'AUTHORIZATION_AUDIENCE_INVALID',
            'audience does not identify the authorized Job Service', 403)
    cst = timezone(timedelta(hours=8))
    issued_at = now.replace(tzinfo=cst)
    expires_at = turn.deadline_at.replace(tzinfo=cst)
    payload, signature = sign_envelope({
        'schema_version': 'authorization_envelope_v1',
        'issuer': str(current_app.config.get(
            'AGENT_CONTROL_AUTHORIZATION_ISSUER') or 'clawteam-hub'),
        'subject': f'worker:{worker_id}',
        'audience': audience,
        'issued_at': issued_at.isoformat(),
        'expires_at': expires_at.isoformat(),
        'nonce': uuid.uuid4().hex,
        'goal_id': turn.goal_id,
        'todo_id': todo.id,
        'turn_id': turn.turn_id,
        'fencing_token': turn.fencing_token or 0,
        'write_scope': approved_scope['write_scope'],
        'allowed_effects': approved_scope['allowed_effects'],
        'requires_gate': approved_scope['requires_gate'],
        'manifest_hash': manifest_hash,
        'approval_scope_hash': approval_scope_hash,
    })
    turn.authorization_payload_json = payload
    turn.authorization_signature = signature
    turn.authorization_key_id = payload['key_id']
    turn.effect_manifest_json = manifest
    turn.authorization_idempotency_key = idempotency_key
    turn.authorization_request_hash = request_hash
    envelope = dict(turn.envelope_json or {})
    envelope['authorization'] = {
        'mode': 'managed_typed',
        'phase': 'effect_authorized',
        'effect_authorization_required': True,
        'managed_receipt_allowed': True,
    }
    envelope['authorization_envelope'] = dict(payload, signature=signature)
    turn.envelope_json = envelope
    db.session.flush()
    return dict(payload, signature=signature), False


def accept_writeback(
    turn: AgentTurn,
    *,
    worker_id: str,
    transition_id: str,
    result_kind: str,
    summary: str,
    verification: dict,
    evidence: dict,
    effect_receipts: list,
    expected_todo_version: int | None,
    fencing_token: int,
    request_document: dict,
    now: datetime | None = None,
) -> tuple[AgentTransitionReceipt, bool]:
    """Validate and commit the Receipt and Todo transition together."""
    now = now or now_cst_naive()
    request_hash = stable_hash(request_document)
    existing = AgentTransitionReceipt.query.filter_by(
        transition_id=transition_id).first()
    if existing:
        if (existing.turn_id != turn.turn_id
                or existing.worker_id != worker_id
                or existing.request_hash != request_hash):
            raise ControlPlaneRejected(
                'TRANSITION_ID_REUSED',
                'transition id was reused with another request', 409)
        return existing, True
    if result_kind not in VALID_RESULT_KINDS:
        raise ControlPlaneRejected('RESULT_KIND_INVALID', 'unknown result kind')
    if turn.worker_id != worker_id:
        raise ControlPlaneRejected(
            'TURN_OWNER_MISMATCH', 'Turn belongs to another Worker', 409)
    if turn.status != 'dispatched':
        raise ControlPlaneRejected(
            'TURN_NOT_WRITABLE', 'Turn is no longer writable', 409)
    if turn.deadline_at and turn.deadline_at <= now:
        raise ControlPlaneRejected(
            'TURN_EXPIRED', 'Turn deadline has expired', 409)
    if (turn.execution_mode == 'managed_typed'
            and (not turn.authorization_payload_json
                 or not turn.authorization_signature)):
        raise ControlPlaneRejected(
            'EFFECT_AUTHORIZATION_REQUIRED',
            'managed_typed Turn has no signed effect authorization', 409)
    todo = (GoalTodo.query.filter_by(id=turn.todo_id).with_for_update().first()
            if turn.todo_id else None)
    if not todo:
        raise ControlPlaneRejected('TODO_NOT_FOUND', 'Turn Todo no longer exists', 409)
    if expected_todo_version is None or todo.version != expected_todo_version:
        raise ControlPlaneRejected('TODO_VERSION_CONFLICT', 'Todo version is stale', 409)
    if todo_requires_claim(todo):
        if todo.claimed_by_worker_id != worker_id:
            raise ControlPlaneRejected('CLAIM_OWNER_MISMATCH', 'Claim owner changed', 409)
        if todo.claim_expires_at and todo.claim_expires_at <= now:
            raise ControlPlaneRejected('CLAIM_EXPIRED', 'Claim has expired', 409)
        if int(todo.fencing_token or 0) != int(fencing_token or 0):
            raise ControlPlaneRejected(
                'FENCING_TOKEN_STALE', 'Fencing token is stale', 409)
    verification = dict(verification) if isinstance(verification, dict) else {}
    contract_warnings = []
    if result_kind in ('VALIDATED_PROGRESS', 'VALIDATED_COMPLETION'):
        if verification.get('status') != 'passed':
            contract_warnings.append({
                'code': 'VERIFICATION_REQUIRED',
                'message': 'validated result requires passed verification',
            })
        refs = verification.get('evidence_refs') or []
        readbacks = verification.get('readback_results') or []
        if not refs and not readbacks:
            contract_warnings.append({
                'code': 'VERIFICATION_EVIDENCE_REQUIRED',
                'message': (
                    'validated result requires evidence or independent readback'),
            })
    goal = db.session.get(AgentGoal, turn.goal_id)
    control_mode = str(
        getattr(goal, 'control_mode', '') or 'shadow').replace('-', '_').lower()
    if contract_warnings:
        if control_mode == 'enforce_p1':
            first = contract_warnings[0]
            raise ControlPlaneRejected(first['code'], first['message'])
        # Shadow/warn/enforce-p0 report quality problems without changing the
        # workflow state. P0 still enforces claim/fencing/idempotency and
        # signed effect authorization above.
        verification['_contract'] = {
            'valid': False,
            'code': 'CONTRACT_INVALID',
            'policy': 'warning',
            'control_mode': control_mode,
            'warnings': contract_warnings,
        }
    else:
        verification['_contract'] = {
            'valid': True,
            'code': 'CONTRACT_VALID',
            'policy': 'accepted',
            'control_mode': control_mode,
            'warnings': [],
        }

    from_state = todo.status
    if result_kind == 'VALIDATED_COMPLETION':
        to_state = 'completed'
    elif result_kind in ('WAIT_USER_ACTION', 'WAIT_EXTERNAL_EVIDENCE'):
        to_state = 'waiting'
    else:
        to_state = 'open'

    receipt = AgentTransitionReceipt(
        transition_id=transition_id,
        request_hash=request_hash,
        turn_id=turn.turn_id,
        goal_id=turn.goal_id,
        todo_id=todo.id,
        todo_version=todo.version,
        worker_id=worker_id,
        fencing_token=fencing_token or 0,
        from_state=from_state,
        to_state=to_state,
        result_kind=result_kind,
        summary=str(summary or '')[:10000],
        verification_json=verification,
        evidence_json=evidence if isinstance(evidence, dict) else {},
        effect_receipts_json=(
            effect_receipts if isinstance(effect_receipts, list) else []),
    )
    db.session.add(receipt)
    todo.status = to_state
    todo.claimed_by_worker_id = ''
    todo.claimed_claw_id = None
    todo.claimed_at = None
    todo.claim_expires_at = None
    todo.version = int(todo.version or 0) + 1
    turn.status = 'completed'
    turn.active_todo_slot = None
    turn.result_kind = result_kind
    turn.completed_at = now
    turn.quota_reserved = False
    db.session.flush()
    return receipt, False
