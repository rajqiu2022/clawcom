"""Repository/service layer for shared-memory mutations and deltas."""

from datetime import timedelta
import hashlib

from app import db
from app.models import _now
from sqlalchemy import and_, or_
from app.memory_models import (
    MemoryChangeLog,
    MemoryMutation,
    MemoryMutationRequest,
    MemoryRecord,
    new_uuid,
)
from app.services.memory_protocol import (
    PROTOCOL_VERSION,
    TENANT_ID,
    MemoryProtocolError,
    validate_mutation_envelope,
    validate_mutation_item,
)
from app.services.memory_scope import MemoryScopeError, authorized_scope_refs, resolve_scope_ref


def _request_hash(raw_body):
    return hashlib.sha256(raw_body or b"").hexdigest()


def _server_time():
    return _now().isoformat()


def _find_record(scope, scope_ref, item):
    return MemoryRecord.query.filter_by(
        tenant_id=TENANT_ID,
        scope=scope,
        scope_ref=scope_ref,
        kind=item["kind"],
        memory_key=item["key"],
    ).first()


def _conflict(record, mutation_id):
    return {
        "mutation_id": mutation_id,
        "status": "conflict",
        "error": {
            "code": "VERSION_CONFLICT",
            "message": "base_version does not match current version",
        },
        "current": record.to_protocol_record(),
    }


def _accepted(record, mutation_id):
    return {
        "mutation_id": mutation_id,
        "status": "accepted",
        "record": record.to_protocol_record(),
    }


def _append_change(record, change_type):
    db.session.add(MemoryChangeLog(
        tenant_id=TENANT_ID,
        scope=record.scope,
        scope_ref=record.scope_ref,
        record_id=record.id,
        record_version=record.record_version,
        change_type=change_type,
    ))


def _record_mutation(claw_id, request_id, item, response_item, record=None):
    result = response_item["status"]
    error = response_item.get("error") or {}
    db.session.add(MemoryMutation(
        tenant_id=TENANT_ID,
        claw_id=claw_id,
        request_id=request_id,
        mutation_id=response_item.get("mutation_id") or item.get("mutation_id") or new_uuid(),
        source_id=item.get("source_id", ""),
        source_kind=item.get("source_kind", ""),
        record_id=getattr(record, "id", None),
        result=result,
        error_code=error.get("code"),
        base_version=item.get("base_version"),
        result_version=(
            response_item.get("record", {}).get("version")
            or response_item.get("current", {}).get("version")
        ),
        response_item=response_item,
    ))


def _apply_valid_item(claw, item):
    try:
        scope_ref = resolve_scope_ref(claw, item["scope"])
    except MemoryScopeError as exc:
        return {
            "mutation_id": item["mutation_id"],
            "status": "rejected",
            "error": {"code": exc.code, "message": exc.message},
        }, None

    record = _find_record(item["scope"], scope_ref, item)
    base_version = item["base_version"]

    if item["op"] == "upsert":
        if record is None:
            if base_version != 0:
                return {
                    "mutation_id": item["mutation_id"],
                    "status": "conflict",
                    "error": {"code": "VERSION_CONFLICT", "message": "record is absent"},
                }, None
            record = MemoryRecord(
                id=new_uuid(),
                tenant_id=TENANT_ID,
                scope=item["scope"],
                scope_ref=scope_ref,
                kind=item["kind"],
                memory_key=item["key"],
                record_version=1,
                created_by_claw_id=claw.id,
            )
            db.session.add(record)
        elif int(record.record_version) != base_version:
            return _conflict(record, item["mutation_id"]), record
        else:
            record.record_version += 1
        record.value = item["value"]
        record.confidence = item["confidence"]
        record.tombstone = False
        record.disabled = False
        record.deleted_at = None
        record.purge_after = None
        record.source_kind = item["source_kind"]
        record.source_id = item["source_id"]
        record.updated_by_claw_id = claw.id
        record.updated_at = _now()
        db.session.flush()
        _append_change(record, "upsert")
        return _accepted(record, item["mutation_id"]), record

    if record is None:
        return {
            "mutation_id": item["mutation_id"],
            "status": "rejected",
            "error": {"code": "ALREADY_DELETED", "message": "record is absent"},
        }, None
    if int(record.record_version) != base_version:
        return _conflict(record, item["mutation_id"]), record
    if record.tombstone:
        return {
            "mutation_id": item["mutation_id"],
            "status": "rejected",
            "error": {"code": "ALREADY_DELETED", "message": "record already deleted"},
        }, record
    record.record_version += 1
    record.source_kind = item["source_kind"]
    record.source_id = item["source_id"]
    record.updated_by_claw_id = claw.id
    record.updated_at = _now()
    record.mark_deleted()
    db.session.flush()
    _append_change(record, "delete")
    return _accepted(record, item["mutation_id"]), record


def process_mutation_request(claw, body, raw_body, idempotency_key):
    """Process one mutations request and return ``(http_status, response_body)``."""
    try:
        envelope = validate_mutation_envelope(body, idempotency_key)
    except MemoryProtocolError as exc:
        return exc.status, {"error": {"code": exc.code, "message": exc.message}}

    digest = _request_hash(raw_body)
    existing = MemoryMutationRequest.query.filter_by(
        tenant_id=TENANT_ID,
        claw_id=claw.id,
        idempotency_key=envelope["idempotency_key"],
    ).first()
    if existing:
        if existing.request_hash != digest:
            return 409, {
                "error": {
                    "code": "IDEMPOTENCY_KEY_REUSED",
                    "message": "idempotency key was reused with a different body",
                }
            }
        return int(existing.http_status), existing.response_body

    request_row = MemoryMutationRequest(
        id=new_uuid(),
        tenant_id=TENANT_ID,
        claw_id=claw.id,
        idempotency_key=envelope["idempotency_key"],
        request_hash=digest,
        expires_at=_now() + timedelta(days=180),
    )
    db.session.add(request_row)
    db.session.flush()

    results = []
    for raw_item in envelope["mutations"]:
        item = validate_mutation_item(raw_item)
        if item.get("status") == "rejected":
            results.append(item)
            _record_mutation(claw.id, request_row.id, raw_item, item)
            continue
        duplicate = MemoryMutation.query.filter_by(
            tenant_id=TENANT_ID,
            claw_id=claw.id,
            mutation_id=item["mutation_id"],
        ).first()
        if duplicate:
            results.append(duplicate.response_item)
            continue
        response_item, record = _apply_valid_item(claw, item)
        results.append(response_item)
        _record_mutation(claw.id, request_row.id, item, response_item, record)

    payload = {
        "protocol_version": PROTOCOL_VERSION,
        "results": results,
        "server_time": _server_time(),
    }
    request_row.http_status = 200
    request_row.response_body = payload
    request_row.state = "completed"
    request_row.completed_at = _now()
    db.session.commit()
    return 200, payload


def delta_records(claw, cursor_sequence=0, limit=200):
    refs = authorized_scope_refs(claw)
    if not refs:
        return []
    authorized_filters = [
        and_(MemoryChangeLog.scope == scope, MemoryChangeLog.scope_ref == scope_ref)
        for scope, scope_ref in refs
    ]
    query = MemoryChangeLog.query.filter(
        MemoryChangeLog.tenant_id == TENANT_ID,
        MemoryChangeLog.sequence > int(cursor_sequence or 0),
        or_(*authorized_filters),
    ).order_by(MemoryChangeLog.sequence.asc())
    rows = []
    for change in query.limit(limit).all():
        record = MemoryRecord.query.get(change.record_id)
        if record:
            rows.append(record.to_protocol_record(sequence=change.sequence))
    return rows
