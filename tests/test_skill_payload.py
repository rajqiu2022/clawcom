import importlib.util
import unittest
from pathlib import Path


_MODULE_PATH = Path(__file__).resolve().parents[1] / 'web' / 'app' / 'services' / 'skill_payload.py'
_SPEC = importlib.util.spec_from_file_location('skill_payload', _MODULE_PATH)
skill_payload = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(skill_payload)


class SkillPayloadTest(unittest.TestCase):
    def test_content_alias_becomes_template_content(self):
        payload = {'name': 'demo', 'content': '# Demo'}

        result = skill_payload.normalize_skill_payload(payload)

        self.assertEqual(result['template_content'], '# Demo')
        self.assertNotIn('content', result)

    def test_body_alias_becomes_template_content(self):
        payload = {'name': 'demo', 'body': '# Demo'}

        result = skill_payload.normalize_skill_payload(payload)

        self.assertEqual(result['template_content'], '# Demo')
        self.assertNotIn('body', result)

    def test_template_content_wins_over_aliases(self):
        payload = {
            'template_content': '# Canonical',
            'content': '# Old',
            'body': '# Body',
        }

        result = skill_payload.normalize_skill_payload(payload)

        self.assertEqual(result['template_content'], '# Canonical')
        self.assertNotIn('content', result)
        self.assertNotIn('body', result)


if __name__ == '__main__':
    unittest.main()
