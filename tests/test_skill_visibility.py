import importlib.util
import unittest
from pathlib import Path


_MODULE_PATH = Path(__file__).resolve().parents[1] / 'web' / 'app' / 'services' / 'skill_visibility.py'
_SPEC = importlib.util.spec_from_file_location('skill_visibility', _MODULE_PATH)
skill_visibility = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(skill_visibility)


class SkillVisibilityTest(unittest.TestCase):
    def test_visibility_private_sets_private(self):
        result = skill_visibility.normalize_skill_visibility({'visibility': 'private'})
        self.assertEqual(result['visibility'], 'private')
        self.assertNotIn('scope', result['data'])

    def test_is_public_false_sets_private(self):
        result = skill_visibility.normalize_skill_visibility({'is_public': False})
        self.assertEqual(result['visibility'], 'private')

    def test_scope_private_is_compat_alias_not_scope_value(self):
        result = skill_visibility.normalize_skill_visibility({'scope': 'private', 'name': 'x'})
        self.assertEqual(result['visibility'], 'private')
        self.assertNotIn('scope', result['data'])
        self.assertEqual(result['data']['name'], 'x')

    def test_public_aliases_clear_private(self):
        for payload in ({'visibility': 'public'}, {'is_public': True}):
            result = skill_visibility.normalize_skill_visibility(payload)
            self.assertEqual(result['visibility'], 'public')


if __name__ == '__main__':
    unittest.main()
