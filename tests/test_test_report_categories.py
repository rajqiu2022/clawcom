import importlib.util
import unittest
from pathlib import Path


_MODULE_PATH = Path(__file__).resolve().parents[1] / 'web' / 'app' / 'services' / 'test_report_categories.py'
_SPEC = importlib.util.spec_from_file_location('test_report_categories', _MODULE_PATH)
test_report_categories = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(test_report_categories)


class TestReportCategoryServiceTest(unittest.TestCase):
    def test_normalize_custom_category_key_uses_trimmed_title_as_key(self):
        self.assertEqual(
            test_report_categories.normalize_custom_category_key('  每日代码分析报告  '),
            '每日代码分析报告',
        )

    def test_normalize_custom_category_key_rejects_empty_and_overlong_titles(self):
        with self.assertRaises(ValueError):
            test_report_categories.normalize_custom_category_key('   ')
        with self.assertRaises(ValueError):
            test_report_categories.normalize_custom_category_key('分' * 121)

    def test_parse_report_time_range_supports_dates_and_datetimes(self):
        since, until = test_report_categories.parse_report_time_range(
            '2026-06-01',
            '2026-06-16T21:30',
        )

        self.assertEqual(since.strftime('%Y-%m-%d %H:%M:%S'), '2026-06-01 00:00:00')
        self.assertEqual(until.strftime('%Y-%m-%d %H:%M:%S'), '2026-06-16 21:30:00')

    def test_build_report_category_link_uses_hub_prefix(self):
        self.assertEqual(
            test_report_categories.build_report_link('https://clawteam.woa.com', 42),
            'https://clawteam.woa.com/test-reports/42',
        )


if __name__ == '__main__':
    unittest.main()
