import unittest
import importlib.util
from pathlib import Path


MODULE_PATH = (Path(__file__).resolve().parents[1] / 'web' / 'app' /
               'services' / 'chat_message_presentation.py')
SPEC = importlib.util.spec_from_file_location('chat_message_presentation', MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
message_presentation = MODULE.message_presentation


class ChatMessagePresentationTest(unittest.TestCase):
    def test_extracts_answer_from_json_suffix_and_keeps_structured_fields(self):
        raw = (
            '正在整理最终回复。'
            '{"answer":"登录成功。\\n下一步开始性能采样。",'
            '"image_paths":["F:/evidence/login.png"],'
            '"memory_ops":[{"op":"upsert","scope":"project","key":"login"}]}'
        )

        result = message_presentation(raw)

        self.assertTrue(result['parsed_json'])
        self.assertEqual('登录成功。\n下一步开始性能采样。', result['text'])
        self.assertTrue(result['source_prefix_omitted'])
        self.assertEqual(['F:/evidence/login.png'], result['fields']['image_paths'])
        self.assertEqual('login', result['fields']['memory_ops'][0]['key'])

    def test_preserves_plain_text_and_braces(self):
        raw = '普通消息包含 {变量}，不应被误判。'
        self.assertEqual({
            'parsed_json': False, 'text': raw, 'fields': {},
        }, message_presentation(raw))

    def test_requires_a_human_readable_text_field(self):
        raw = '{"memory_ops":[{"op":"upsert"}]}'
        result = message_presentation(raw)
        self.assertFalse(result['parsed_json'])
        self.assertEqual(raw, result['text'])


if __name__ == '__main__':
    unittest.main()
