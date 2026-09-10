import importlib.util
import os
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
MODULE_FILE = ROOT / "web" / "app" / "memos_client.py"


class MemosSnapshotCacheTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.cache_path = str(Path(self.tempdir.name) / "memos.json")
        self.previous_path = os.environ.get("MEMOS_LIST_CACHE_PATH")
        os.environ["MEMOS_LIST_CACHE_PATH"] = self.cache_path
        spec = importlib.util.spec_from_file_location("memos_client_under_test", MODULE_FILE)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.calls = []

        def request(method, path, **kwargs):
            self.calls.append((method, path, kwargs))
            token = (kwargs.get("params") or {}).get("pageToken", "")
            if not token:
                return {
                    "memos": [{"name": "memos/1", "content": "#claw-xiaohe"}],
                    "nextPageToken": "page-2",
                }
            return {
                "memos": [{"name": "memos/2", "content": "#claw-xiaoma"}],
                "nextPageToken": "",
            }

        self.module._request = request
        self.module._config = lambda: ("http://memos", "configured")

    def tearDown(self):
        if self.previous_path is None:
            os.environ.pop("MEMOS_LIST_CACHE_PATH", None)
        else:
            os.environ["MEMOS_LIST_CACHE_PATH"] = self.previous_path
        self.tempdir.cleanup()

    def test_different_claw_searches_share_one_paginated_snapshot(self):
        xiaohe = self.module.search_memos(claw_name="xiaohe", limit=20)
        xiaoma = self.module.search_memos(claw_name="xiaoma", limit=20)

        self.assertEqual(["memos/1"], [memo["name"] for memo in xiaohe])
        self.assertEqual(["memos/2"], [memo["name"] for memo in xiaoma])
        self.assertEqual(2, len(self.calls))

    def test_invalidation_forces_one_new_snapshot(self):
        self.module.search_memos(claw_name="xiaohe", limit=20)
        self.module.invalidate_memos_snapshot()
        self.module.search_memos(claw_name="xiaohe", limit=20)

        self.assertEqual(4, len(self.calls))


if __name__ == "__main__":
    unittest.main()
