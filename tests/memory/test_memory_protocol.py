import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WEB_ROOT = ROOT / "web"
for path in (str(ROOT), str(WEB_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

if "flask_cors" not in sys.modules:
    stub = types.ModuleType("flask_cors")
    stub.CORS = lambda *args, **kwargs: None
    sys.modules["flask_cors"] = stub

from app.services.memory_protocol import (  # noqa: E402
    MemoryProtocolError,
    validate_mutation_envelope,
    validate_mutation_item,
)


def upsert(**overrides):
    item = {
        "schema": 1,
        "mutation_id": "11111111-1111-4111-8111-111111111111",
        "op": "upsert",
        "scope": "owner",
        "kind": "preference",
        "key": "language",
        "value": "中文",
        "confidence": 0.9,
        "base_version": 0,
        "source_id": "message:1",
        "source_kind": "message",
    }
    item.update(overrides)
    return item


class MemoryProtocolTests(unittest.TestCase):
    def test_envelope_rejects_unknown_fields_duplicate_keys_and_missing_idempotency(self):
        with self.assertRaisesRegex(MemoryProtocolError, "IDEMPOTENCY_KEY_REQUIRED"):
            validate_mutation_envelope({"protocol_version": 1, "mutations": []}, "")

        with self.assertRaisesRegex(MemoryProtocolError, "INVALID_REQUEST"):
            validate_mutation_envelope({
                "protocol_version": 1,
                "mutations": [upsert(), dict(upsert(), mutation_id="22222222-2222-4222-8222-222222222222")],
            }, "idem")

        with self.assertRaisesRegex(MemoryProtocolError, "INVALID_REQUEST"):
            validate_mutation_envelope({
                "protocol_version": 1,
                "mutations": [upsert(extra=True)],
            }, "idem")

    def test_validates_upsert_delete_and_per_item_secret(self):
        envelope = validate_mutation_envelope({
            "protocol_version": 1,
            "mutations": [upsert()],
        }, "idem")
        self.assertEqual(1, len(envelope["mutations"]))

        delete = validate_mutation_item({
            "schema": 1,
            "mutation_id": "33333333-3333-4333-8333-333333333333",
            "op": "delete",
            "scope": "project",
            "kind": "decision",
            "key": "release",
            "base_version": 1,
            "source_id": "todo:2",
            "source_kind": "todo",
        })
        self.assertEqual("delete", delete["op"])

        rejected = validate_mutation_item(upsert(value="Bearer abcdef"))
        self.assertEqual("rejected", rejected["status"])
        self.assertEqual("SECRET_DETECTED", rejected["error"]["code"])

    def test_rejects_conversation_as_not_syncable_for_mvp(self):
        result = validate_mutation_item(upsert(scope="conversation"))
        self.assertEqual("rejected", result["status"])
        self.assertEqual("SCOPE_NOT_SYNCABLE", result["error"]["code"])


if __name__ == "__main__":
    unittest.main()
