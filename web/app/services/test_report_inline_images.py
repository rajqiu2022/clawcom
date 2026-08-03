"""Helpers for converting inline base64 report images into attachments."""

from __future__ import annotations

import base64
import binascii
import re
from typing import Callable, Dict, Tuple


ALLOWED_IMAGE_MIME_EXTS = {
    'image/png': 'png',
    'image/jpeg': 'jpg',
    'image/jpg': 'jpg',
    'image/gif': 'gif',
    'image/webp': 'webp',
}

_DATA_IMAGE_RE = (
    r'data:(?P<mime>image/(?:png|jpe?g|gif|webp));base64,'
    r'(?P<data>[A-Za-z0-9+/=\s]+)'
)
_MARKDOWN_IMAGE_RE = re.compile(
    r'!\[(?P<alt>[^\]]*)\]\(\s*' + _DATA_IMAGE_RE + r'\s*\)',
    re.IGNORECASE,
)
_HTML_IMAGE_RE = re.compile(
    r'<img\b(?P<before>[^>]*?)\bsrc=["\']'
    + _DATA_IMAGE_RE
    + r'["\'](?P<after>[^>]*)>',
    re.IGNORECASE,
)
_ALT_RE = re.compile(r'\balt=["\'](?P<alt>[^"\']*)["\']', re.IGNORECASE)


def _decode_base64_image(mime: str, data: str, max_bytes: int) -> Tuple[bytes, str]:
    normalized_mime = (mime or '').lower()
    ext = ALLOWED_IMAGE_MIME_EXTS.get(normalized_mime)
    if not ext:
        raise ValueError('仅支持 png/jpg/gif/webp 内嵌图片')
    compact = re.sub(r'\s+', '', data or '')
    if not compact:
        raise ValueError('内嵌图片数据为空')
    # Base64 inflates by roughly 4/3; reject obviously oversized payloads early.
    if len(compact) > int(max_bytes * 1.38) + 16:
        raise ValueError('内嵌图片超过 10MB 上限，请改用附件上传或企微发送')
    try:
        raw = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError('内嵌图片 Base64 数据无效') from exc
    if len(raw) > max_bytes:
        raise ValueError('内嵌图片超过 10MB 上限，请改用附件上传或企微发送')
    return raw, ext


def _safe_alt(text: str) -> str:
    return (text or '截图').strip()[:80] or '截图'


def _markdown_replacement(alt: str, info: Dict[str, object]) -> str:
    name = str(info.get('filename') or alt or '截图')
    url = str(info.get('download_url') or '')
    if not url:
        return f'[附件图片：{name}]'
    return f'![{name}]({url}?inline=1)'


def _html_replacement(alt: str, info: Dict[str, object]) -> str:
    name = str(info.get('filename') or alt or '截图')
    url = str(info.get('download_url') or '')
    if not url:
        return f'<p>附件图片：{name}</p>'
    escaped_name = (
        name.replace('&', '&amp;')
        .replace('<', '&lt;')
        .replace('>', '&gt;')
        .replace('"', '&quot;')
    )
    return (
        '<figure class="report-image-attachment">'
        f'<img src="{url}?inline=1" alt="{escaped_name}">'
        f'<figcaption>{escaped_name}</figcaption>'
        '</figure>'
    )


def extract_inline_report_images(
    content: str,
    fmt: str,
    save_image: Callable[[bytes, str, str], Dict[str, object]],
    *,
    max_bytes: int,
) -> Tuple[str, int]:
    """Replace inline base64 images with attachment references.

    ``save_image`` receives ``(raw_bytes, ext, alt)`` and must return a dict with
    at least ``filename`` and ``download_url`` after the attachment row is flushed.
    """
    if not content or 'data:image/' not in content:
        return content or '', 0

    count = 0

    def replace_markdown(match):
        nonlocal count
        alt = _safe_alt(match.group('alt'))
        raw, ext = _decode_base64_image(match.group('mime'), match.group('data'), max_bytes)
        info = save_image(raw, ext, alt)
        count += 1
        return _markdown_replacement(alt, info)

    updated = _MARKDOWN_IMAGE_RE.sub(replace_markdown, content)

    def replace_html(match):
        nonlocal count
        attr_text = (match.group('before') or '') + ' ' + (match.group('after') or '')
        alt_match = _ALT_RE.search(attr_text)
        alt = _safe_alt(alt_match.group('alt') if alt_match else '截图')
        raw, ext = _decode_base64_image(match.group('mime'), match.group('data'), max_bytes)
        info = save_image(raw, ext, alt)
        count += 1
        return _html_replacement(alt, info) if (fmt or '').lower() == 'html' else _markdown_replacement(alt, info)

    updated = _HTML_IMAGE_RE.sub(replace_html, updated)
    return updated, count
