"""Shared-memory synchronization API for Windows Worker v2."""

from flask import g, jsonify, request

from app.api import api_bp
from app.services.memory_cursor import decode_cursor, encode_cursor
from app.services.memory_protocol import MemoryProtocolError
from app.services.memory_repository import delta_records, process_mutation_request
from app.services.memory_scope import authorized_scope_refs
from app.services.memory_sync_config import (
    memory_api_enabled,
    memory_pull_enabled,
    memory_push_enabled,
)


def _memory_error(code, message, status):
    return jsonify({"error": {"code": code, "message": message}}), status


def _require_memory_claw(claw_id):
    claw = getattr(g, "_auth_claw", None)
    if not claw:
        return None, _memory_error("UNAUTHORIZED", "Bearer Claw token required", 401)
    if int(getattr(claw, "id", 0)) != int(claw_id):
        return None, _memory_error("CLAW_SCOPE_DENIED", "token does not match claw", 403)
    if getattr(claw, "status", "") == "deleted":
        return None, _memory_error("CLAW_NOT_FOUND", "claw is deleted", 404)
    return claw, None


@api_bp.route("/openclaws/<int:claw_id>/memories/mutations", methods=["POST"])
def post_memory_mutations(claw_id):
    if not memory_api_enabled() or not memory_push_enabled():
        return _memory_error("MEMORY_DISABLED", "memory mutations are disabled", 404)
    claw, error = _require_memory_claw(claw_id)
    if error:
        return error
    raw_body = request.get_data(cache=True) or b"{}"
    body = request.get_json(silent=True) or {}
    idempotency_key = request.headers.get("Idempotency-Key", "")
    status, payload = process_mutation_request(
        claw=claw,
        body=body,
        raw_body=raw_body,
        idempotency_key=idempotency_key,
    )
    return jsonify(payload), status


@api_bp.route("/openclaws/<int:claw_id>/memories/delta", methods=["GET"])
def get_memory_delta(claw_id):
    if not memory_api_enabled() or not memory_pull_enabled():
        return _memory_error("MEMORY_DISABLED", "memory delta is disabled", 404)
    claw, error = _require_memory_claw(claw_id)
    if error:
        return error

    try:
        limit = int(request.args.get("limit") or 200)
    except ValueError:
        return _memory_error("INVALID_REQUEST", "limit must be integer", 400)
    limit = min(max(limit, 1), 500)

    refs = authorized_scope_refs(claw)
    scope_ref_values = [ref for _scope, ref in refs]
    try:
        if request.args.get("cursor"):
            decoded = decode_cursor(
                request.args.get("cursor"),
                claw_id=claw_id,
                scope_refs=scope_ref_values,
            )
            sequence = decoded["sequence"]
        else:
            sequence = 0
    except MemoryProtocolError as exc:
        return _memory_error(exc.code, exc.message, exc.status)

    items = delta_records(claw, cursor_sequence=sequence, limit=limit)
    next_sequence = max([item["sequence"] for item in items] or [sequence])
    next_cursor = encode_cursor(
        claw_id=claw_id,
        sequence=next_sequence,
        scope_refs=scope_ref_values,
    )
    return jsonify({
        "protocol_version": 1,
        "items": items,
        "next_cursor": next_cursor,
        "has_more": len(items) >= limit,
    })
