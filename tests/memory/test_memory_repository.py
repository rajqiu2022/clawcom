import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

from flask import Flask


ROOT = Path(__file__).resolve().parents[2]
WEB_ROOT = ROOT / "web"
for path in (str(ROOT), str(WEB_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

if "flask_cors" not in sys.modules:
    stub = types.ModuleType("flask_cors")
    stub.CORS = lambda *args, **kwargs: None
    sys.modules["flask_cors"] = stub

from app import db  # noqa: E402
from app.memory_models import (  # noqa: E402
    MemoryChangeLog,
    MemoryMutationRequest,
    MemoryRecord,
)
from app.services.memory_repository import delta_records, process_mutation_request  # noqa: E402


def make_app():
    app = Flask(__name__)
    app.config.update(
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="test-secret",
    )
    db.init_app(app)
    return app


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


class MemoryRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.app = make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.claw = SimpleNamespace(id=7, owner="alice", project_id=3)

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _process(self, body, key="idem-1"):
        return process_mutation_request(
            claw=self.claw,
            body=body,
            raw_body=b"stable-body",
            idempotency_key=key,
        )

    def test_accepts_create_update_delete_with_versions_and_change_log(self):
        status, payload = self._process({
            "protocol_version": 1,
            "mutations": [upsert()],
        })
        self.assertEqual(200, status)
        result = payload["results"][0]
        self.assertEqual("accepted", result["status"])
        self.assertEqual(1, result["record"]["version"])

        record = MemoryRecord.query.one()
        self.assertEqual("language", record.memory_key)
        self.assertEqual(1, MemoryChangeLog.query.count())

        status, payload = self._process({
            "protocol_version": 1,
            "mutations": [upsert(
                mutation_id="22222222-2222-4222-8222-222222222222",
                value="英文",
                base_version=1,
                source_id="message:2",
            )],
        }, key="idem-2")
        self.assertEqual("accepted", payload["results"][0]["status"])
        self.assertEqual(2, payload["results"][0]["record"]["version"])

        status, payload = self._process({
            "protocol_version": 1,
            "mutations": [{
                "schema": 1,
                "mutation_id": "33333333-3333-4333-8333-333333333333",
                "op": "delete",
                "scope": "owner",
                "kind": "preference",
                "key": "language",
                "base_version": 2,
                "source_id": "todo:3",
                "source_kind": "todo",
            }],
        }, key="idem-3")
        deleted = payload["results"][0]["record"]
        self.assertTrue(deleted["tombstone"])
        self.assertEqual(3, deleted["version"])
        self.assertEqual(3, MemoryChangeLog.query.count())

    def test_conflict_returns_current_record_without_change(self):
        self._process({"protocol_version": 1, "mutations": [upsert()]})
        status, payload = self._process({
            "protocol_version": 1,
            "mutations": [upsert(
                mutation_id="22222222-2222-4222-8222-222222222222",
                value="冲突",
                base_version=0,
                source_id="message:2",
            )],
        }, key="idem-2")
        self.assertEqual(200, status)
        result = payload["results"][0]
        self.assertEqual("conflict", result["status"])
        self.assertEqual("VERSION_CONFLICT", result["error"]["code"])
        self.assertEqual(1, result["current"]["version"])
        self.assertEqual(1, MemoryChangeLog.query.count())

    def test_request_idempotency_replays_same_body_and_rejects_reused_key(self):
        body = {"protocol_version": 1, "mutations": [upsert()]}
        status1, payload1 = self._process(body, key="idem")
        status2, payload2 = process_mutation_request(
            claw=self.claw,
            body=body,
            raw_body=b"stable-body",
            idempotency_key="idem",
        )
        self.assertEqual((status1, payload1), (status2, payload2))
        self.assertEqual(1, MemoryMutationRequest.query.count())

        status3, payload3 = process_mutation_request(
            claw=self.claw,
            body=body,
            raw_body=b"different-body",
            idempotency_key="idem",
        )
        self.assertEqual(409, status3)
        self.assertEqual("IDEMPOTENCY_KEY_REUSED", payload3["error"]["code"])

    def test_secret_item_is_rejected_without_blocking_other_items(self):
        status, payload = self._process({
            "protocol_version": 1,
            "mutations": [
                upsert(value="Bearer abcdef"),
                upsert(
                    mutation_id="22222222-2222-4222-8222-222222222222",
                    key="timezone",
                    value="UTC+8",
                    source_id="message:2",
                ),
            ],
        })
        self.assertEqual(200, status)
        self.assertEqual(["rejected", "accepted"], [r["status"] for r in payload["results"]])
        self.assertEqual(1, MemoryRecord.query.count())

    def test_delta_applies_limit_after_scope_authorization(self):
        other_claw = SimpleNamespace(id=8, owner="bob", project_id=4)
        process_mutation_request(
            claw=other_claw,
            body={"protocol_version": 1, "mutations": [upsert(key="other")]},
            raw_body=b"other-body",
            idempotency_key="other-idem",
        )
        self._process({
            "protocol_version": 1,
            "mutations": [upsert(
                mutation_id="22222222-2222-4222-8222-222222222222",
                key="mine",
            )],
        })

        items = delta_records(self.claw, cursor_sequence=0, limit=1)

        self.assertEqual(1, len(items))
        self.assertEqual("mine", items[0]["key"])


if __name__ == "__main__":
    unittest.main()
