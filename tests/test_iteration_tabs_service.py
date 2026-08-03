import importlib.util
import unittest
from pathlib import Path


_MODULE_PATH = Path(__file__).resolve().parents[1] / 'web' / 'app' / 'services' / 'iteration_tabs.py'
_SPEC = importlib.util.spec_from_file_location('iteration_tabs', _MODULE_PATH)
iteration_tabs = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(iteration_tabs)


class IterationTabsServiceTest(unittest.TestCase):
    def test_normalize_defaults_to_table_without_charts(self):
        payload = iteration_tabs.normalize_tab_payload({
            'tab_key': 'requirements',
            'title': '需求',
            'columns': [{'key': 'status', 'title': '状态', 'type': 'status'}],
            'rows': [{'id': 'r1', 'cells': {'status': '进行中'}}],
        })
        self.assertEqual(payload['view_mode'], 'table')
        self.assertEqual(payload['charts'], [])
        self.assertEqual(payload['columns'][0]['type'], 'status')

    def test_normalize_defaults_to_table_chart_when_charts_exist(self):
        payload = iteration_tabs.normalize_tab_payload({
            'tab_key': 'bugs',
            'title': 'Bug',
            'columns': [{'key': 'date', 'title': '日期'}],
            'rows': [{'id': 'd1', 'cells': {'date': '2026-06-17'}}],
            'charts': [{'id': 'trend', 'type': 'line', 'x': 'date', 'y': 'count'}],
        })
        self.assertEqual(payload['view_mode'], 'table_chart')

    def test_normalize_accepts_string_columns_and_flat_rows(self):
        payload = iteration_tabs.normalize_tab_payload({
            'title': '代码分析',
            'columns': ['序号', '类别', '标题', '状态'],
            'rows': [
                {
                    '序号': '1',
                    '类别': 'Bug',
                    '标题': 'EndGame GiveAward 重复发放风险',
                    '状态': '评估中',
                }
            ],
        })

        self.assertEqual(payload['columns'][0], {'key': '序号', 'title': '序号', 'type': 'text'})
        self.assertEqual(payload['rows'][0]['id'], 'row_1')
        self.assertEqual(payload['rows'][0]['cells']['标题'], 'EndGame GiveAward 重复发放风险')

    def test_normalize_supports_checkbox_and_text_colors(self):
        payload = iteration_tabs.normalize_tab_payload({
            'tab_key': 'risks',
            'title': '风险',
            'columns': [
                {'key': 'title', 'title': '标题', 'type': 'text', 'text_color': 'red'},
                {
                    'key': 'status',
                    'title': '状态',
                    'type': 'select',
                    'options': [
                        {'value': 'todo', 'label': '待处理', 'color': 'gray'},
                        {'value': 'done', 'label': '完成', 'color': 'green'},
                    ],
                },
                {'key': 'checked', 'title': '已确认', 'type': 'checkbox'},
            ],
            'rows': [{
                'id': 'risk_1',
                'cells': {
                    'title': {'text': 'P0 发奖风险', 'text_color': '#ef4444'},
                    'status': 'todo',
                    'checked': False,
                },
            }],
        })

        self.assertEqual(payload['columns'][0]['text_color'], 'red')
        self.assertEqual(payload['columns'][1]['options'][1]['color'], 'green')
        self.assertEqual(payload['columns'][2]['type'], 'checkbox')
        self.assertFalse(payload['rows'][0]['cells']['checked'])

    def test_update_cell_only_changes_target_cell(self):
        rows = [
            {'id': 'r1', 'cells': {'status': 'open', 'owner': 'alice'}},
            {'id': 'r2', 'cells': {'status': 'closed'}},
        ]
        updated = iteration_tabs.update_cell_value(rows, 'r1', 'status', 'done')
        self.assertEqual(updated[0]['cells']['status'], 'done')
        self.assertEqual(updated[0]['cells']['owner'], 'alice')
        self.assertEqual(updated[1]['cells']['status'], 'closed')

    def test_update_cell_rejects_unknown_row(self):
        with self.assertRaises(ValueError):
            iteration_tabs.update_cell_value([], 'missing', 'status', 'done')

    def test_preserve_existing_rows_when_full_save_has_empty_rows(self):
        existing = [{'id': 'row_1', 'cells': {'title': '已有分析项'}}]
        self.assertTrue(iteration_tabs.should_preserve_existing_rows(existing, [], {'rows': []}))
        self.assertTrue(iteration_tabs.should_preserve_existing_rows(existing, [], {'title': '代码分析'}))

    def test_allow_explicit_empty_rows_replacement(self):
        existing = [{'id': 'row_1', 'cells': {'title': '已有分析项'}}]
        self.assertFalse(iteration_tabs.should_preserve_existing_rows(existing, [], {'rows': [], 'clear_rows': True}))
        self.assertFalse(iteration_tabs.should_preserve_existing_rows(existing, [], {'rows': [], 'replace_rows': True}))
        self.assertFalse(iteration_tabs.should_preserve_existing_rows(existing, [], {'rows': [], 'allow_empty_rows': True}))

    def test_append_row_adds_normalized_row(self):
        rows = iteration_tabs.append_row([], {'id': 'r1', 'cells': {'status': 'todo'}})
        self.assertEqual(rows, [{'id': 'r1', 'cells': {'status': 'todo'}}])

    def test_append_row_rejects_duplicate_id(self):
        with self.assertRaises(ValueError):
            iteration_tabs.append_row([{'id': 'r1', 'cells': {}}], {'id': 'r1', 'cells': {}})

    def test_append_chart_preserves_summary_cards(self):
        charts = iteration_tabs.append_chart([], {
            'id': 'summary',
            'type': 'summary_cards',
            'cards': [{'label': '总 Bug', 'value_key': 'bug_count'}],
        })
        self.assertEqual(charts[0]['cards'][0]['label'], '总 Bug')

    def test_creator_can_manage_full_tab(self):
        self.assertTrue(iteration_tabs.can_manage_tab(
            {'role': 'user', 'username': 'alice'}, 'alice'))

    def test_other_agent_can_only_edit_cells(self):
        user = {'role': 'user', 'username': 'bob'}
        self.assertFalse(iteration_tabs.can_manage_tab(user, 'alice'))
        self.assertTrue(iteration_tabs.can_edit_tab_cell(user))


class FlowProgressServiceTest(unittest.TestCase):
    def test_flow_progress_is_valid_view_mode(self):
        self.assertIn('flow_progress', iteration_tabs.VALID_VIEW_MODES)

    def test_flow_status_color_canonical(self):
        self.assertEqual(iteration_tabs.flow_status_color('passed'), 'green')
        self.assertEqual(iteration_tabs.flow_status_color('通过'), 'green')
        self.assertEqual(iteration_tabs.flow_status_color('failed'), 'red')
        self.assertEqual(iteration_tabs.flow_status_color('失败'), 'red')
        self.assertEqual(iteration_tabs.flow_status_color('running'), 'blue')
        self.assertEqual(iteration_tabs.flow_status_color('进行中'), 'blue')
        self.assertEqual(iteration_tabs.flow_status_color('blocked'), 'orange')
        self.assertEqual(iteration_tabs.flow_status_color('pending'), 'gray')
        self.assertEqual(iteration_tabs.flow_status_color(''), 'gray')
        self.assertEqual(iteration_tabs.flow_status_color('unknown_xyz'), 'gray')

    def test_default_steps_generated_when_empty(self):
        rows = iteration_tabs.normalize_flow_steps_payload({})
        self.assertEqual(len(rows), 5)
        ids = [r['id'] for r in rows]
        self.assertEqual(ids, ['daily_merge', 'editor_smoke', 'submit_build', 'mobile_smoke', 'report'])
        self.assertEqual(rows[0]['cells']['name'], '每日合线')
        self.assertEqual(rows[0]['cells']['status'], 'pending')

    def test_custom_steps_normalized(self):
        data = {'steps': [
            {'id': 's1', 'name': '步骤一', 'status': 'passed', 'message': '全部通过'},
            {'name': '步骤二', 'status': 'running'},
        ]}
        rows = iteration_tabs.normalize_flow_steps_payload(data, actor='agent_x')
        self.assertEqual(rows[0]['id'], 's1')
        self.assertEqual(rows[0]['cells']['status'], 'passed')
        self.assertEqual(rows[0]['cells']['message'], '全部通过')
        self.assertEqual(rows[0]['cells']['updated_by'], 'agent_x')
        self.assertEqual(rows[1]['cells']['name'], '步骤二')

    def test_merge_updates_existing_steps(self):
        existing = [
            {'id': 'daily_merge', 'cells': {'name': '每日合线', 'status': 'passed', 'message': 'OK', 'updated_by': 'a'}},
            {'id': 'editor_smoke', 'cells': {'name': '编辑器冒烟', 'status': 'pending', 'message': '', 'updated_by': ''}},
        ]
        data = {'steps': [
            {'id': 'editor_smoke', 'status': 'failed', 'message': '3 case failed'},
        ]}
        rows = iteration_tabs.normalize_flow_steps_payload(data, existing_rows=existing, actor='bot')
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['cells']['status'], 'passed')
        self.assertEqual(rows[0]['cells']['message'], 'OK')
        self.assertEqual(rows[1]['cells']['status'], 'failed')
        self.assertEqual(rows[1]['cells']['message'], '3 case failed')
        self.assertEqual(rows[1]['cells']['updated_by'], 'bot')

    def test_merge_appends_new_steps(self):
        existing = [
            {'id': 'daily_merge', 'cells': {'name': '每日合线', 'status': 'passed', 'message': '', 'updated_by': ''}},
        ]
        data = {'steps': [
            {'id': 'new_step', 'name': '新步骤', 'status': 'running'},
        ]}
        rows = iteration_tabs.normalize_flow_steps_payload(data, existing_rows=existing)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['id'], 'daily_merge')
        self.assertEqual(rows[1]['id'], 'new_step')

    def test_replace_steps_replaces_all(self):
        existing = [
            {'id': 'daily_merge', 'cells': {'name': '每日合线', 'status': 'passed', 'message': '', 'updated_by': ''}},
        ]
        data = {'replace_steps': True, 'steps': [
            {'id': 'only_step', 'name': '唯一步骤', 'status': 'done'},
        ]}
        rows = iteration_tabs.normalize_flow_steps_payload(data, existing_rows=existing)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['id'], 'only_step')

    def test_freeform_message_preserved(self):
        data = {'steps': [
            {'id': 's1', 'name': 'test', 'status': 'done',
             'message': {'detail': 'passed all', 'count': 42}},
        ]}
        rows = iteration_tabs.normalize_flow_steps_payload(data)
        self.assertEqual(rows[0]['cells']['message'], {'detail': 'passed all', 'count': 42})


if __name__ == '__main__':
    unittest.main()
