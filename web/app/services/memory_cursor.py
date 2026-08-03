"""Opaque signed cursor helpers for shared-memory delta."""

import base64
import hashlib
import hmac
import json

from flask import current_app

from app.services.memory_protocol import MemoryProtocolError


def _scope_hash(scope_refs):
    payload = ",".join(sorted(str(ref) for ref in scope_refs))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _secret():
    return str(current_app.config.get("SECRET_KEY") or "dev-secret-key").encode("utf-8")


def encode_cursor(claw_id, sequence, scope_refs):
    body = {
        "v": 1,
        "claw_id": int(claw_id),
        "sequence": int(sequence or 0),
        "scope_hash": _scope_hash(scope_refs),
    }
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    sig = hmac.new(_secret(), raw, hashlib.sha256).hexdigest()
    token = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"{token}.{sig}"


def decode_cursor(cursor, claw_id, scope_refs):
    try:
        token, sig = str(cursor or "").split(".", 1)
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        expected = hmac.new(_secret(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            raise ValueError("bad signature")
        body = json.loads(raw.decode("utf-8"))
        if body.get("v") != 1:
            raise ValueError("bad version")
        if int(body.get("claw_id")) != int(claw_id):
            raise ValueError("wrong claw")
        if body.get("scope_hash") != _scope_hash(scope_refs):
            raise ValueError("wrong scopes")
        return {"sequence": int(body.get("sequence") or 0)}
    except Exception as exc:
        raise MemoryProtocolError("INVALID_CURSOR", "delta cursor is invalid") from exc
