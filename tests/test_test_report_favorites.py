import importlib.util
import unittest
from pathlib import Path


_MODULE_PATH = Path(__file__).resolve().parents[1] / 'web' / 'app' / 'services' / 'test_report_favorites.py'
_SPEC = importlib.util.spec_from_file_location('test_report_favorites', _MODULE_PATH)
test_report_favorites = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(test_report_favorites)


class TestReportFavoriteServiceTest(unittest.TestCase):
    def test_user_favorite_owner_uses_user_id(self):
        owner = test_report_favorites.favorite_owner_from_caller({
            'type': 'user',
            'user_id': 12,
            'claw_id': 7,
        })

        self.assertEqual(owner, {'user_id': 12, 'claw_id': None})

    def test_openclaw_favorite_owner_uses_claw_id(self):
        owner = test_report_favorites.favorite_owner_from_caller({
            'type': 'openclaw',
            'user_id': None,
            'claw_id': 7,
        })

        self.assertEqual(owner, {'user_id': None, 'claw_id': 7})

    def test_missing_identity_cannot_favorite(self):
        with self.assertRaises(ValueError):
            test_report_favorites.favorite_owner_from_caller(None)
        with self.assertRaises(ValueError):
            test_report_favorites.favorite_owner_from_caller({'type': 'user'})


if __name__ == '__main__':
    unittest.main()
