import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    'hermes_models_catalog', ROOT / 'web' / 'app' / 'hermes_models.py')
models = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(models)


class HermesModelCatalogTest(unittest.TestCase):
    def test_new_timiai_default_uses_r1_pro(self):
        self.assertEqual(models.DEFAULT_TIMIAI_LLM_MODEL,
                         'deepseek-v4-pro-r1')
        self.assertEqual(models.default_hermes_model('timiai'),
                         'deepseek-v4-pro-r1')
        self.assertEqual(models.hermes_vision_config('timiai')['model'],
                         'deepseek-v4-pro-r1')

    def test_r1_models_are_supported_while_legacy_model_stays_readable(self):
        self.assertEqual(
            models.normalize_hermes_model('deepseek-v4-pro-r1', 'timiai'),
            'deepseek-v4-pro-r1')
        self.assertEqual(
            models.normalize_hermes_model('deepseek-v4-flash-r1', 'timiai'),
            'deepseek-v4-flash-r1')
        self.assertEqual(
            models.normalize_hermes_model('deepseek-v4-pro', 'timiai'),
            'deepseek-v4-pro')

    def test_timiai_model_keys_are_project_scoped(self):
        self.assertEqual(
            models.default_timiai_model_for_project('gbt'),
            'deepseek-v4-pro-r1')
        self.assertEqual(
            models.default_timiai_model_for_project('contra'),
            'deepseek-v4-pro')
        self.assertEqual(
            models.normalize_timiai_model_for_project(
                'deepseek-v4-pro', 'contra'),
            'deepseek-v4-pro')
        with self.assertRaises(ValueError):
            models.normalize_timiai_model_for_project(
                'deepseek-v4-pro-r1', 'contra')

    def test_create_agent_timiai_groups_use_project_specific_deepseek_keys(self):
        page = (ROOT / 'web' / 'templates' / 'openclaws.html').read_text(
            encoding='utf-8')
        for group in ('timiai:', 'gbt:'):
            start = page.index(group, page.index('TIMIAI_LLM_MODELS_BY_PROJECT')
                               if group != 'timiai:' else 0)
            end = page.find('],', start)
            section = page[start:end]
            self.assertIn("deepseek-v4-pro-r1", section)
            self.assertIn("deepseek-v4-flash-r1", section)
        for group in ('qqspeed_pc:', 'contra:'):
            start = page.index(
                group, page.index('TIMIAI_LLM_MODELS_BY_PROJECT'))
            end = page.find('],', start)
            section = page[start:end]
            self.assertIn("deepseek-v4-pro'", section)
            self.assertNotIn('deepseek-v4-pro-r1', section)
            self.assertNotIn('deepseek-v4-flash-r1', section)


if __name__ == '__main__':
    unittest.main()
