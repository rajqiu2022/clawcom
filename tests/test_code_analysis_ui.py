"""Render and browser-script contracts; no production mutations."""
from pathlib import Path
import subprocess
import shutil
import unittest
from flask import Flask, render_template

ROOT = Path(__file__).resolve().parents[1]


class CodeAnalysisUiTest(unittest.TestCase):
    def test_template_renders_and_reuses_base(self):
        app=Flask(__name__,template_folder=str(ROOT/'web/templates'))
        with app.test_request_context():
            html=render_template('code_analysis.html',hub_public_url='',hub_web_url='')
        for marker in ('项目代码分析','id="ca-project"','id="ca-dialog"','aria-labelledby="ca-dialog-title"',
                       'js/code_analysis.js','人工反馈驱动'):
            self.assertIn(marker,html)

    def test_navigation_and_plan_entry(self):
        self.assertIn('href="/code-analysis"',(ROOT/'web/templates/base.html').read_text(encoding='utf-8'))
        self.assertIn('/code-analysis?project_id=',(ROOT/'web/templates/testplans.html').read_text(encoding='utf-8'))
        self.assertIn("@views_bp.route('/code-analysis')",(ROOT/'web/app/views/__init__.py').read_text(encoding='utf-8'))

    @unittest.skipUnless(shutil.which('node'),'Node required for JS execution')
    def test_javascript_parses_and_escapes_untrusted_values(self):
        path=ROOT/'web/static/js/code_analysis.js'
        subprocess.run(['node','--check',str(path)],check=True,capture_output=True)
        script=path.read_text(encoding='utf-8')
        js="const document={addEventListener(){}};"+script+"\nif(caEsc('<img onerror=alert(1)>').includes('<'))throw Error('unescaped');if(caStatus('learned')!=='已学习')throw Error('status');"
        subprocess.run(['node','-e',js],check=True,capture_output=True)

    def test_feedback_version_and_sharing_controls_are_wired(self):
        js=(ROOT/'web/static/js/code_analysis.js').read_text(encoding='utf-8')
        for marker in ('ca-extra-knowledge','/publish-general','/submit-bug','/reconcile-bug',
                       '风险接受（不算误报）','expected_revision','data-skill','data-rollback','/compare?',
                       'data-share-project','/start','Idempotency-Key','caAsk(','epoch!==CA.loadEpoch','epoch!==CA.detailEpoch'):
            self.assertIn(marker,js)
        self.assertNotIn('showConfirm(',js)
        self.assertNotIn('showPrompt(',js)
        self.assertNotIn('href="/skills?skill_id=',js)

    def test_migration_defines_all_four_models(self):
        sql=(ROOT/'ops/migrations/20260930_code_analysis_specialty.sql').read_text(encoding='utf-8')
        for table in ('shared_resource_policies','code_analysis_projects','code_analysis_jobs','code_analysis_decisions'):
            self.assertIn('CREATE TABLE IF NOT EXISTS '+table,sql)
        self.assertIn('general_published_revision',sql)
        self.assertIn('knowledge_ids_json LONGTEXT',sql)
        self.assertNotIn('DROP TABLE',sql)
