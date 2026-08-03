import importlib.util
import unittest
from pathlib import Path


_MODULE_PATH = Path(__file__).resolve().parents[1] / 'web' / 'app' / 'services' / 'hermes_timiai_projects.py'
_SPEC = importlib.util.spec_from_file_location('hermes_timiai_projects', _MODULE_PATH)
hermes_timiai_projects = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(hermes_timiai_projects)


class HermesTimiaiProjectTest(unittest.TestCase):
    def test_default_project_is_gbt(self):
        self.assertEqual(hermes_timiai_projects.normalize_timiai_project(''), 'gbt')
        self.assertEqual(hermes_timiai_projects.normalize_timiai_project(None), 'gbt')

    def test_project_aliases(self):
        self.assertEqual(hermes_timiai_projects.normalize_timiai_project('GBT'), 'gbt')
        self.assertEqual(hermes_timiai_projects.normalize_timiai_project('QQ飞车端游'), 'qqspeed_pc')
        self.assertEqual(hermes_timiai_projects.normalize_timiai_project('魂斗罗'), 'contra')

    def test_unknown_project_is_rejected(self):
        with self.assertRaises(ValueError):
            hermes_timiai_projects.normalize_timiai_project('unknown')


if __name__ == '__main__':
    unittest.main()
