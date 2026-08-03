"""Shared-memory protocol validation for Hub canonical storage."""

import json
import re
import uuid


PROTOCOL_VERSION = 1
TENANT_ID = 1
MAX_BATCH_ITEMS = 100
MAX_BATCH_BYTES = 256 * 1024
VALID_SCOPES = ("owner", "project", "conversation")
SYNCABLE_SCOPES = ("owner", "project")
VALID_KINDS = ("preference", "decision", "fact", "pitfall")
VALID_SOURCE_KINDS = ("message", "wecom", "todo", "workflow", "memo_migration", "admin")
_SECRET = re.compile(
    r"(?ix)(?:\bBearer\s+\S+|"
    r"(?:API_KEY|TOKEN|SECRET|PASSWORD|ACCESS_KEY|SECRET_KEY)"
    r"\s*(?:=|:)\s*\S+|"
    r"(?:^|[\\/])(?:\.aws[\\/]credentials|credentials(?:\.json)?)(?:$|[\\/]))"
)


class MemoryProtocolError(ValueError):
    def __init__(self, code, message, status=400):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status


def error_response(code, message, status=400):
    return {"error": {"code": code, "message": message}}, status


def _required_text(value, name, maximum):
    if not isinstance(value, str) or not value.strip():
        raise MemoryProtocolError("INVALID_REQUEST", f"{name} must be a nonempty string")
    value = value.strip()
    if "\0" in value or len(value) > maximum:
        raise MemoryProtocolError("INVALID_REQUEST", f"{name} is invalid or too long")
    return value


def _uuid_text(value, name):
    text = _required_text(value, name, 64)
    try:
        return str(uuid.UUID(text))
    except (TypeError, ValueError) as exc:
        raise MemoryProtocolError("INVALID_REQUEST", f"{name} must be a UUID") from exc


def _contains_secret(value):
    return isinstance(value, str) and bool(_SECRET.search(value))


def _rejected(mutation_id, code, message):
    return {
        "mutation_id": mutation_id or "",
        "status": "rejected",
        "error": {"code": code, "message": message},
    }


def validate_mutation_envelope(body, idempotency_key):
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise MemoryProtocolError(
            "IDEMPOTENCY_KEY_REQUIRED",
            "Idempotency-Key header is required",
        )
    key = _required_text(idempotency_key, "Idempotency-Key", 128)
    if not isinstance(body, dict):
        raise MemoryProtocolError("INVALID_REQUEST", "request body must be an object")
    if set(body) != {"protocol_version", "mutations"}:
        raise MemoryProtocolError("INVALID_REQUEST", "unknown or missing envelope fields")
    if body.get("protocol_version") != PROTOCOL_VERSION:
        raise MemoryProtocolError("INVALID_REQUEST", "unsupported protocol version")
    mutations = body.get("mutations")
    if not isinstance(mutations, list) or not mutations:
        raise MemoryProtocolError("INVALID_REQUEST", "mutations must be a nonempty array")
    if len(mutations) > MAX_BATCH_ITEMS:
        raise MemoryProtocolError("INVALID_REQUEST", "mutation batch exceeds 100 items")
    encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) > MAX_BATCH_BYTES:
        raise MemoryProtocolError("PAYLOAD_TOO_LARGE", "mutation batch exceeds 256 KiB", 413)

    seen_mutations = set()
    seen_keys = set()
    for raw in mutations:
        if not isinstance(raw, dict):
            raise MemoryProtocolError("INVALID_REQUEST", "each mutation must be an object")
        mutation_id = raw.get("mutation_id")
        if mutation_id in seen_mutations:
            raise MemoryProtocolError("INVALID_REQUEST", "duplicate mutation_id in batch")
        seen_mutations.add(mutation_id)
        biz_key = (raw.get("scope"), raw.get("kind"), raw.get("key"))
        if biz_key in seen_keys:
            raise MemoryProtocolError("INVALID_REQUEST", "duplicate memory key in batch")
        seen_keys.add(biz_key)
        _validate_shape(raw)
    return {"idempotency_key": key, "mutations": mutations}


def _validate_shape(raw):
    operation = raw.get("op")
    if operation == "upsert":
        fields = {
            "schema", "mutation_id", "op", "scope", "kind", "key",
            "value", "confidence", "base_version", "source_id", "source_kind",
        }
    elif operation == "delete":
        fields = {
            "schema", "mutation_id", "op", "scope", "kind", "key",
            "base_version", "source_id", "source_kind",
        }
    else:
        raise MemoryProtocolError("INVALID_REQUEST", "unsupported memory operation")
    if set(raw) != fields:
        raise MemoryProtocolError("INVALID_REQUEST", "unknown or missing mutation fields")


def validate_mutation_item(raw):
    try:
        _validate_shape(raw)
        mutation = {
            "schema": raw.get("schema"),
            "mutation_id": _uuid_text(raw.get("mutation_id"), "mutation_id"),
            "op": raw.get("op"),
            "scope": raw.get("scope"),
            "kind": raw.get("kind"),
            "key": _required_text(raw.get("key"), "key", 128),
            "base_version": raw.get("base_version"),
            "source_id": _required_text(raw.get("source_id"), "source_id", 256),
            "source_kind": raw.get("source_kind"),
        }
        if mutation["schema"] != 1:
            return _rejected(mutation["mutation_id"], "UNSUPPORTED_SCHEMA", "unsupported schema")
        if mutation["scope"] not in VALID_SCOPES or mutation["kind"] not in VALID_KINDS:
            return _rejected(mutation["mutation_id"], "INVALID_ITEM", "invalid enum")
        if mutation["scope"] not in SYNCABLE_SCOPES:
            return _rejected(mutation["mutation_id"], "SCOPE_NOT_SYNCABLE", "scope is not syncable")
        if mutation["source_kind"] not in VALID_SOURCE_KINDS:
            return _rejected(mutation["mutation_id"], "INVALID_ITEM", "invalid source kind")
        if isinstance(mutation["base_version"], bool) or not isinstance(mutation["base_version"], int):
            return _rejected(mutation["mutation_id"], "INVALID_ITEM", "base_version must be integer")
        if mutation["base_version"] < 0:
            return _rejected(mutation["mutation_id"], "INVALID_ITEM", "base_version must be nonnegative")
        if _contains_secret(mutation["key"]) or _contains_secret(mutation["source_id"]):
            return _rejected(mutation["mutation_id"], "SECRET_DETECTED", "secret material detected")
        if mutation["op"] == "upsert":
            value = _required_text(raw.get("value"), "value", 4000)
            confidence = raw.get("confidence")
            if (
                isinstance(confidence, bool)
                or not isinstance(confidence, (int, float))
                or not 0 <= confidence <= 1
            ):
                return _rejected(mutation["mutation_id"], "INVALID_ITEM", "invalid confidence")
            if _contains_secret(value):
                return _rejected(mutation["mutation_id"], "SECRET_DETECTED", "secret material detected")
            mutation["value"] = value
            mutation["confidence"] = float(confidence)
        return mutation
    except MemoryProtocolError as exc:
        return _rejected(raw.get("mutation_id"), exc.code, exc.message)
