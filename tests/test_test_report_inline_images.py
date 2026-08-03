import base64
import unittest
from pathlib import Path
import importlib.util


_MODULE_PATH = Path(__file__).resolve().parents[1] / 'web' / 'app' / 'services' / 'test_report_inline_images.py'
_SPEC = importlib.util.spec_from_file_location('test_report_inline_images', _MODULE_PATH)
service = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(service)


class TestReportInlineImagesTest(unittest.TestCase):
    def _save(self, raw, ext, alt):
        self.saved.append((raw, ext, alt))
        return {
            'filename': f'{alt}.{ext}',
            'download_url': '/api/v1/test-reports/1/attachments/2/download',
        }

    def setUp(self):
        self.saved = []

    def test_replaces_markdown_base64_image_with_attachment_link(self):
        encoded = base64.b64encode(b'fake-png').decode('ascii')
        content = f'前置\n![异常截图](data:image/png;base64,{encoded})\n后置'

        updated, count = service.extract_inline_report_images(
            content, 'markdown', self._save, max_bytes=1024,
        )

        self.assertEqual(count, 1)
        self.assertEqual(self.saved, [(b'fake-png', 'png', '异常截图')])
        self.assertIn('![异常截图.png](/api/v1/test-reports/1/attachments/2/download?inline=1)', updated)
        self.assertNotIn('data:image/png;base64', updated)

    def test_replaces_html_base64_image_with_attachment_preview(self):
        encoded = base64.b64encode(b'fake-jpg').decode('ascii')
        content = f'<p>说明</p><img alt="登录失败" src="data:image/jpeg;base64,{encoded}">'

        updated, count = service.extract_inline_report_images(
            content, 'html', self._save, max_bytes=1024,
        )

        self.assertEqual(count, 1)
        self.assertEqual(self.saved, [(b'fake-jpg', 'jpg', '登录失败')])
        self.assertIn('<figure class="report-image-attachment">', updated)
        self.assertIn('/api/v1/test-reports/1/attachments/2/download?inline=1', updated)
        self.assertNotIn('data:image/jpeg;base64', updated)

    def test_rejects_oversized_inline_image(self):
        encoded = base64.b64encode(b'a' * 32).decode('ascii')

        with self.assertRaises(ValueError):
            service.extract_inline_report_images(
                f'![大图](data:image/png;base64,{encoded})',
                'markdown',
                self._save,
                max_bytes=8,
            )


if __name__ == '__main__':
    unittest.main()
