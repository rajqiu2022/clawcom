import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / 'web' / 'app' / 'services' / 'knowledge_sharing.py'
)


def load_service():
    spec = importlib.util.spec_from_file_location('knowledge_sharing', MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class KnowledgeSharingServiceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = load_service()

    def entry(self, **overrides):
        values = {
            'id': 17,
            'memos_id': 'memo-secret',
            'title': '登录/支付：回归*清单？',
            'content': '## 步骤\n\n- 保留原始正文\n',
            'category': 'best-practice',
            'scope': 'project',
            'project_name': 'RacingGO',
            'module_name': '支付',
            'source_openclaw_id': 7,
            'source_openclaw': SimpleNamespace(name='小安'),
            'source_type': 'openclaw',
            'status': 'draft',
            'reviewer_notes': '内部审核意见',
            'approved_by': 'admin',
            'created_by': '小安',
            'created_at': '2026-08-03 20:00:00',
            'updated_at': '2026-08-03 20:10:00',
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_agent_favorite_owner_takes_precedence_over_owner_user(self):
        owner = self.service.knowledge_favorite_owner(
            SimpleNamespace(id=12, username='alice'),
            SimpleNamespace(id=7, name='小安'),
        )
        self.assertEqual(owner, {'user_id': None, 'claw_id': 7})

    def test_web_favorite_owner_uses_user_id(self):
        owner = self.service.knowledge_favorite_owner(
            SimpleNamespace(id=12, username='alice'),
            None,
        )
        self.assertEqual(owner, {'user_id': 12, 'claw_id': None})

    def test_missing_identity_cannot_favorite(self):
        with self.assertRaises(ValueError):
            self.service.knowledge_favorite_owner(None, None)

    def test_author_agent_owner_and_admin_can_manage_share(self):
        entry = self.entry()
        self.assertTrue(self.service.can_manage_knowledge_share(
            entry, SimpleNamespace(username='小安', role='user'), None, set()))
        self.assertTrue(self.service.can_manage_knowledge_share(
            entry, None, SimpleNamespace(id=7, name='小安'), set()))
        self.assertTrue(self.service.can_manage_knowledge_share(
            entry, SimpleNamespace(username='alice', role='user'), None, {7}))
        self.assertTrue(self.service.can_manage_knowledge_share(
            entry, SimpleNamespace(username='root', role='admin'), None, set()))

    def test_unrelated_user_cannot_manage_share(self):
        self.assertFalse(self.service.can_manage_knowledge_share(
            self.entry(),
            SimpleNamespace(username='mallory', role='user'),
            None,
            set(),
        ))

    def test_public_payload_uses_allowlist(self):
        payload = self.service.public_knowledge_payload(self.entry())
        self.assertEqual(payload['title'], '登录/支付：回归*清单？')
        self.assertEqual(payload['content'], '## 步骤\n\n- 保留原始正文\n')
        self.assertEqual(payload['source_openclaw_name'], '小安')
        for forbidden in (
            'id', 'memos_id', 'source_openclaw_id',
            'reviewer_notes', 'approved_by',
        ):
            self.assertNotIn(forbidden, payload)

    def test_markdown_filename_preserves_chinese_and_removes_windows_chars(self):
        filename = self.service.knowledge_markdown_filename(
            '登录/支付：回归*清单？<>:"\\|'
        )
        self.assertTrue(filename.endswith('.md'))
        self.assertIn('登录', filename)
        for invalid in '<>:"/\\|?*':
            self.assertNotIn(invalid, filename)

    def test_markdown_filename_is_bounded_and_has_fallback(self):
        self.assertEqual(
            self.service.knowledge_markdown_filename('...   '),
            'knowledge.md',
        )
        self.assertLessEqual(
            len(self.service.knowledge_markdown_filename('知识' * 200)),
            124,
        )

    def test_markdown_contains_metadata_and_original_body(self):
        content = self.service.knowledge_markdown(self.entry())
        self.assertTrue(content.startswith('# 登录/支付：回归*清单？\n'))
        self.assertIn('- 分类：best-practice', content)
        self.assertIn('- 项目：RacingGO', content)
        self.assertTrue(content.endswith('## 步骤\n\n- 保留原始正文\n'))


if __name__ == '__main__':
    unittest.main()
