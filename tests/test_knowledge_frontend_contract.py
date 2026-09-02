import re
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class KnowledgeFrontendContractTest(unittest.TestCase):
    def test_authenticated_page_exposes_favorite_share_and_export_actions(self):
        text = (ROOT / 'web' / 'templates' / 'knowledge.html').read_text(
            encoding='utf-8')
        self.assertIn('id="filter-favorite"', text)
        self.assertIn('toggleFavorite(', text)
        self.assertIn('manageKnowledgeShare(', text)
        self.assertIn('openKnowledgeShare(', text)
        self.assertIn('window.open(', text)
        self.assertIn('share-url-', text)
        self.assertIn('分享外链（匿名只读）', text)
        self.assertIn('kn-share-quick', text)
        self.assertIn('复制链接', text)
        self.assertIn('/export.md', text)
        self.assertIn('仅收藏', text)

    def test_anonymous_page_reads_public_payload_and_exports_markdown(self):
        text = (
            ROOT / 'web' / 'templates' / 'knowledge_share.html'
        ).read_text(encoding='utf-8')
        self.assertIn('/api/v1/knowledge/shared/', text)
        self.assertIn('/export.md', text)
        self.assertIn('下载 Markdown', text)

    def test_view_routes_allow_short_and_compatible_share_paths(self):
        text = (
            ROOT / 'web' / 'app' / 'views' / '__init__.py'
        ).read_text(encoding='utf-8')
        self.assertIn("request.path.startswith('/k/')", text)
        self.assertIn("@views_bp.route('/k/<token>')", text)
        self.assertIn(
            "@views_bp.route('/knowledge/share/<token>')",
            text,
        )

    def test_inline_javascript_is_valid(self):
        for name in ('knowledge.html', 'knowledge_share.html'):
            text = (ROOT / 'web' / 'templates' / name).read_text(
                encoding='utf-8')
            scripts = re.findall(r'<script>(.*?)</script>', text, re.DOTALL)
            self.assertTrue(scripts, name)
            source = '\n'.join(scripts).replace(
                '{{ share_token | tojson }}',
                '"test-token"',
            )
            result = subprocess.run(
                ['node', '--check'],
                input=source.encode('utf-8'),
                capture_output=True,
                check=False,
            )
            self.assertEqual(
                result.returncode,
                0,
                f"{name}: {result.stderr.decode('utf-8', errors='replace')}",
            )

    def test_version_journal_workspace_and_revision_actions_are_present(self):
        text = (ROOT / 'web' / 'templates' / 'knowledge.html').read_text(
            encoding='utf-8')
        for marker in (
            'data-tab="journal"', 'kn-wiki-shell',
            'loadJournalNotebooks', 'createJournalNotebook',
            'saveJournalPage', 'compareJournalRevisions',
            'rollbackJournalRevision', 'journal-vditor',
            'listTestIterations', 'selectJournalOption',
            '关联测试迭代', '选择所属模块',
            'copyJournalPageLink', 'applyJournalDeepLink',
            '/knowledge/wiki/',
        ):
            self.assertIn(marker, text)

    def test_vditor_uses_hub_surface_colors(self):
        text = (ROOT / 'web' / 'templates' / 'knowledge.html').read_text(
            encoding='utf-8')
        self.assertIn('.vditor--dark .vditor-content', text)
        self.assertIn('background:var(--bg-card) !important', text)
        self.assertIn('background:var(--bg-secondary) !important', text)
        self.assertNotIn('background: #1a1b2e !important', text)

    def test_wiki_deep_link_view_route_is_present(self):
        text = (ROOT / 'web' / 'app' / 'views' / '__init__.py').read_text(
            encoding='utf-8')
        self.assertIn("@views_bp.route('/knowledge/wiki/<int:page_id>')", text)


if __name__ == '__main__':
    unittest.main()
