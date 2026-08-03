import importlib.util
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _load_module(name: str, rel_path: str):
    module_path = REPO_ROOT / rel_path
    spec = importlib.util.spec_from_file_location(name, module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


experience_trigger = _load_module(
    'collective_memory_experience_trigger',
    'tools/collective_memory/experience_trigger.py',
)
experience_draft = _load_module(
    'collective_memory_experience_draft',
    'tools/collective_memory/experience_draft.py',
)

extract_trigger_terms = experience_trigger.extract_trigger_terms
format_trigger_notice = experience_trigger.format_trigger_notice
MAX_NOTICE_ENTRIES = experience_trigger.MAX_NOTICE_ENTRIES
trigger_for_task = experience_trigger.trigger_for_task
infer_primary_skill = experience_trigger.infer_primary_skill
append_auto_pitfall_notice = experience_trigger.append_auto_pitfall_notice
strip_auto_pitfall_notice = experience_trigger.strip_auto_pitfall_notice
build_experience_draft = experience_draft.build_experience_draft
should_offer_draft = experience_draft.should_offer_draft


class ExperienceTriggerTest(unittest.TestCase):
    def test_extract_trigger_terms_for_wecom_image_report(self):
        text = '把龙虾图片和总结报告通过企业微信一起发给我'
        self.assertEqual(extract_trigger_terms(text), ['企业微信', '图片'])

    def test_extract_trigger_terms_for_hub_api(self):
        text = '调用 Hub API 拉取 RacingGO 项目配置'
        terms = extract_trigger_terms(text)
        self.assertIn('Hub', terms)
        self.assertIn('RacingGO', terms)

    def test_format_trigger_notice_empty(self):
        self.assertEqual(format_trigger_notice([]), '')

    def test_format_trigger_notice_with_entries(self):
        notice = format_trigger_notice([
            {'title': 'Hub API 路径前缀不统一', 'solution': '调用前确认 URL 前缀。', 'status': 'active'},
        ])
        self.assertIn('任务开始前命中以下公共经验', notice)
        self.assertIn('Hub API 路径前缀不统一', notice)

    def test_trigger_for_task_wecom_image_hits_pitfall(self):
        notice = trigger_for_task('把龙虾图片和总结报告通过企业微信一起发给我')
        self.assertIn('WeCom', notice)
        self.assertIn('media_id', notice)

    def test_trigger_for_task_irrelevant_returns_empty(self):
        notice = trigger_for_task('今天天气怎么样')
        self.assertEqual(notice, '')

    def test_build_experience_draft(self):
        draft = build_experience_draft(
            task_title='TAPD 截图上传失败',
            project='RacingGO',
            module='tapd',
            failure='直接调 OSS 504',
            fix='走本地脚本',
            verification='HTTP 200 返回 url',
            owner='condibot',
        )
        self.assertEqual(draft['type'], 'pitfall')
        self.assertEqual(draft['symptom'], '直接调 OSS 504')
        self.assertEqual(draft['verified_steps'], ['HTTP 200 返回 url'])

    def test_should_offer_draft_conditions(self):
        self.assertFalse(should_offer_draft())
        self.assertTrue(should_offer_draft(had_error_fix=True))
        self.assertTrue(should_offer_draft(retry_count=3))
        self.assertTrue(should_offer_draft(user_asked_record=True))

    def test_infer_primary_skill_for_tapd(self):
        self.assertEqual(infer_primary_skill('查询 TAPD 本周缺陷'), 'tapd-integration')

    def test_format_trigger_notice_limits_entries(self):
        entries = [
            {'title': f'坑{i}', 'solution': 'x' * 200, 'status': 'active'}
            for i in range(5)
        ]
        notice = format_trigger_notice(entries)
        self.assertIn('另有 2 条', notice)
        self.assertLessEqual(notice.count('坑'), 3)

    def test_append_and_strip_auto_pitfall_notice(self):
        base = '执行日报汇总'
        notice = '任务开始前命中以下公共经验，请先检查：\n- Hub API：确认前缀'
        enriched = append_auto_pitfall_notice(base, notice)
        self.assertIn('hub:auto-pitfall-notice begin', enriched)
        self.assertEqual(strip_auto_pitfall_notice(enriched), base)

    def test_rank_pitfalls_by_terms_orders_by_hit_count(self):
        entries = [
            {'id': 1, 'title': 'TAPD 提单字段幻觉',
             'content': 'baseline_find 手打错', 'keywords': ['TAPD', 'baseline_find']},
            {'id': 2, 'title': '无关条目', 'content': 'xxx', 'keywords': ['yyy']},
            {'id': 3, 'title': 'Hub 分页陷阱',
             'content': 'per_page 不生效 limit', 'keywords': ['Hub', '分页']},
        ]
        ranked = experience_trigger.rank_pitfalls_by_terms(
            entries, ['TAPD', 'Hub'], limit=5)
        self.assertEqual([e['id'] for e in ranked], [1, 3])

    def test_rank_pitfalls_by_terms_respects_limit(self):
        entries = [
            {'id': i, 'title': 'Hub 坑', 'content': 'Hub', 'keywords': ['Hub']}
            for i in range(1, 6)
        ]
        ranked = experience_trigger.rank_pitfalls_by_terms(
            entries, ['Hub'], limit=2)
        self.assertEqual(len(ranked), 2)

    def test_rank_pitfalls_by_terms_drops_zero_hits(self):
        entries = [{'id': 1, 'title': 'x', 'content': 'y', 'keywords': ['z']}]
        self.assertEqual(
            experience_trigger.rank_pitfalls_by_terms(entries, ['Hub'], limit=5),
            [],
        )


if __name__ == '__main__':
    unittest.main()
