import sys
import types
import unittest
from pathlib import Path

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

from app.services.memory_cursor import decode_cursor, encode_cursor  # noqa: E402
from app.services.memory_protocol import MemoryProtocolError  # noqa: E402


class MemoryCursorTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config["SECRET_KEY"] = "cursor-secret"
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        self.ctx.pop()

    def test_cursor_is_opaque_signed_and_bound_to_claw_and_scopes(self):
        cursor = encode_cursor(claw_id=7, sequence=42, scope_refs=["a", "b"])
        payload = decode_cursor(cursor, claw_id=7, scope_refs=["b", "a"])
        self.assertEqual(42, payload["sequence"])

        with self.assertRaisesRegex(MemoryProtocolError, "INVALID_CURSOR"):
            decode_cursor(cursor + "x", claw_id=7, scope_refs=["a", "b"])
        with self.assertRaisesRegex(MemoryProtocolError, "INVALID_CURSOR"):
            decode_cursor(cursor, claw_id=8, scope_refs=["a", "b"])
        with self.assertRaisesRegex(MemoryProtocolError, "INVALID_CURSOR"):
            decode_cursor(cursor, claw_id=7, scope_refs=["a"])


if __name__ == "__main__":
    unittest.main()
