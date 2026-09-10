import importlib.util
from pathlib import Path
import sys
import types
import unittest


ROOT = Path(__file__).resolve().parents[2]
MODULE_FILE = ROOT / "web" / "app" / "services" / "memo_index.py"


class _Claw:
    def __init__(self, name):
        self.name = name


class MemoIndexCacheTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("memo_index_under_test", MODULE_FILE)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.calls = []

        def search_memos(**kwargs):
            self.calls.append(kwargs)
            name = kwargs["claw_name"]
            return [{
                "name": "memos/1",
                "content": f"# Decision\n#openclaw/decision/config\n#claw-{name}\nkeep it",
                "updateTime": "2026-08-31T10:00:00Z",
            }]

        fake_app = types.ModuleType("app")
        fake_app.memos_client = types.SimpleNamespace(search_memos=search_memos)
        self.previous_app = sys.modules.get("app")
        sys.modules["app"] = fake_app

    def tearDown(self):
        if self.previous_app is None:
            sys.modules.pop("app", None)
        else:
            sys.modules["app"] = self.previous_app

    def test_repeated_build_uses_cache_and_returns_copy(self):
        first = self.module.build_memo_index(_Claw("xiaohe"), limit=20)
        first[0]["title"] = "mutated"
        second = self.module.build_memo_index(_Claw("xiaohe"), limit=20)

        self.assertEqual(1, len(self.calls))
        self.assertEqual("Decision", second[0]["title"])

    def test_invalidation_refreshes_one_claw(self):
        self.module.build_memo_index(_Claw("xiaohe"), limit=20)
        self.module.build_memo_index(_Claw("xiaoma"), limit=20)
        self.module.invalidate_memo_index("xiaohe")
        self.module.build_memo_index(_Claw("xiaohe"), limit=20)
        self.module.build_memo_index(_Claw("xiaoma"), limit=20)

        self.assertEqual(3, len(self.calls))


if __name__ == "__main__":
    unittest.main()
