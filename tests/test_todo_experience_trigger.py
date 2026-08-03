import importlib.util
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = REPO_ROOT / 'web'
if str(WEB_ROOT) not in sys.path:
    sys.path.insert(0, str(WEB_ROOT))


def _load_service():
    module_path = WEB_ROOT / 'app' / 'services' / 'experience_trigger_service.py'
    spec = importlib.util.spec_from_file_location('experience_trigger_service', module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


service = _load_service()


class TodoExperienceTriggerServiceTest(unittest.TestCase):
    def test_build_context_for_wecom_todo(self):
        ctx = service.build_task_operating_context(
            '通过企业微信发送测试报告和图片',
            '汇总今日 RacingGO 测试结果',
            project='RacingGO',
        )
        self.assertIn('WeCom', ctx['pitfall_notice'])
        self.assertEqual(ctx['primary_skill'], 'hub-sse-sidecar')
        self.assertIn('企业微信', ctx['trigger_terms'])

    def test_enrich_description_appends_marker_block(self):
        enriched = service.enrich_todo_description(
            '调用 Hub API 同步配置',
            '检查 openclaws 日报接口',
            project='RacingGO',
        )
        self.assertIn('hub:auto-pitfall-notice begin', enriched)
        self.assertIn('Hub', enriched)

    def test_enrich_description_idempotent_on_reapply(self):
        first = service.enrich_todo_description('TAPD 截图上传', '上传缺陷截图', project='RacingGO')
        second = service.enrich_todo_description('TAPD 截图上传', first, project='RacingGO')
        self.assertEqual(first.count('hub:auto-pitfall-notice begin'), 1)
        self.assertEqual(second.count('hub:auto-pitfall-notice begin'), 1)

    def test_build_context_prefers_injected_hub_source(self):
        fake = [{'id': 99, 'title': '线上专属坑', 'content': 'Hub 专属',
                 'keywords': ['Hub'], 'solution': '走线上方案', 'status': 'approved'}]
        ctx = service.build_task_operating_context(
            '调用 Hub API', '同步配置', project='RacingGO',
            pitfall_source=lambda project, module, terms, limit: fake,
        )
        self.assertIn('线上专属坑', ctx['pitfall_notice'])
        self.assertIn(99, ctx['matched_pitfall_ids'])

    def test_build_context_falls_back_to_seed_when_hub_empty(self):
        ctx = service.build_task_operating_context(
            '调用 Hub API', '同步配置', project='RacingGO',
            pitfall_source=lambda project, module, terms, limit: [],
        )
        self.assertTrue(ctx['pitfall_notice'])


if __name__ == '__main__':
    unittest.main()
