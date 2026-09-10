"""
Memos 客户端 — 写入外部 Memos 服务（Claw 零碎笔记层）

设计（MEMORY / Rule #15）：
  - Hub ``knowledge_entries`` = 正式知识（审核、共享）
  - Memos = 各 Claw 每日零碎记录，共用服务账号 + ``#claw-{name}`` 标签隔离
  - 内容标签：``#openclaw/{category}/{scope_key}`` 便于检索与 upsert 去重

环境变量（或 Flask config）：
  - MEMOS_URL：默认 http://your-hub-host:5230
  - MEMOS_API_KEY：Memos 设置里生成的 Access Token（必填，否则写接口报错）
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import requests

logger = logging.getLogger(__name__)

try:
    import fcntl
except ImportError:  # pragma: no cover - Hub production is Linux
    fcntl = None


def _read_snapshot_ttl() -> float:
    try:
        configured = float(os.environ.get('MEMOS_LIST_CACHE_TTL_SECONDS', '300'))
    except (TypeError, ValueError):
        configured = 300.0
    return max(15.0, min(configured, 3600.0))


_MEMOS_SNAPSHOT_TTL_SECONDS = _read_snapshot_ttl()
_memos_snapshot_lock = threading.Lock()

KNOWLEDGE_TAGS = {
    'bug-experience': '缺陷经验',
    'test-method': '测试方法',
    'method': '测试方法',
    'tool-usage': '工具使用',
    'project-knowledge': '项目知识',
    'performance': '性能相关',
    'perf-baseline': '性能基线',
    'compatibility': '兼容性',
    'security': '安全相关',
    'automation': '自动化',
    'bug-pattern': 'Bug 模式',
    'bug-standard': 'Bug 标准',
    'pitfall': '踩坑记录',
    'workflow': '流程规范',
    'best-practice': '最佳实践',
    'general': '通用知识',
}


class MemosNotConfiguredError(RuntimeError):
    """MEMOS_API_KEY 未配置或 Memos 不可达"""


def _config() -> tuple:
    try:
        from flask import current_app
        base = (current_app.config.get('MEMOS_URL') or '').rstrip('/')
        key = current_app.config.get('MEMOS_API_KEY') or ''
    except Exception:
        import os
        base = os.environ.get('MEMOS_URL', 'http://your-hub-host:5230').rstrip('/')
        key = os.environ.get('MEMOS_API_KEY', '')
    if not base:
        base = 'http://your-hub-host:5230'
    return base, (key or '').strip()


def _require_key() -> tuple:
    base, key = _config()
    if not key:
        raise MemosNotConfiguredError(
            'MEMOS_API_KEY 未配置：请在 Memos 网页 → 设置 → Access Token 生成后 '
            '写入 Hub 的 /opt/openclaw-web/.env 并重启 openclaw-web'
        )
    return base, key


def _claw_tag(claw_name: Optional[str]) -> str:
    """Memos 内容标签，如 #claw-龙虾王"""
    raw = (claw_name or 'unknown').strip()
    safe = re.sub(r'[^\w\u4e00-\u9fff\-]', '-', raw).strip('-') or 'unknown'
    return f'claw-{safe}'


def _openclaw_tag(tag: str, scope_key: str = 'general') -> str:
    cat = (tag or 'general').strip()
    scope = (scope_key or 'general').strip() or 'general'
    return f'openclaw/{cat}/{scope}'


def _build_memo_content(
    title: str,
    body: str,
    claw_name: Optional[str],
    tag: str,
    scope_key: str = 'general',
    project_name: Optional[str] = None,
    module_name: Optional[str] = None,
    extra_tags: Optional[List[str]] = None,
) -> str:
    lines = [
        f'# {title.strip() or "笔记"}',
        '',
        f'#{_claw_tag(claw_name)} #{_openclaw_tag(tag, scope_key)}',
    ]
    if project_name:
        pn = re.sub(r'\s+', '-', str(project_name).strip())
        lines.append(f'#project-{pn}')
    if module_name:
        mn = re.sub(r'\s+', '-', str(module_name).strip())
        lines.append(f'#module-{mn}')
    if extra_tags:
        for t in extra_tags:
            if t and not t.startswith('#'):
                lines.append(f'#{t}')
            elif t:
                lines.append(t)
    lines.extend(['', (body or '').strip()])
    return '\n'.join(lines).strip() + '\n'


def _default_visibility() -> str:
    """Memos 0.24 已知 bug：PRIVATE memo 不会出现在 ListMemos（#4495），搜索永远 0 条。

    团队共用 API Token + ``#claw-{name}`` 标签隔离，默认用 PROTECTED（登录/API 可见、可列表）。
    可通过 MEMOS_DEFAULT_VISIBILITY 覆盖（PROTECTED / PUBLIC，勿用 PRIVATE）。
    """
    try:
        from flask import current_app
        v = (current_app.config.get('MEMOS_DEFAULT_VISIBILITY') or '').strip().upper()
    except Exception:
        import os
        v = (os.environ.get('MEMOS_DEFAULT_VISIBILITY') or '').strip().upper()
    if v in ('PUBLIC', 'PROTECTED'):
        return v
    return 'PROTECTED'


def _memo_id_from_name(name: str) -> str:
    """memos/123 → 123"""
    if not name:
        return ''
    return name.split('/')[-1]


def _request(
    method: str,
    path: str,
    *,
    json_body: Optional[dict] = None,
    params: Optional[dict] = None,
    timeout: int = 30,
) -> Any:
    base, key = _require_key()
    url = f'{base}{path}'
    if params:
        url = f'{url}?{urlencode({k: v for k, v in params.items() if v is not None})}'
    headers = {
        'Authorization': f'Bearer {key}',
        'Accept': 'application/json',
    }
    if json_body is not None:
        headers['Content-Type'] = 'application/json'
    try:
        resp = requests.request(
            method, url, headers=headers, json=json_body, timeout=timeout,
        )
    except requests.RequestException as e:
        raise RuntimeError(f'Memos 不可达 ({base}): {e}') from e
    if resp.status_code >= 400:
        raise RuntimeError(
            f'Memos API {method} {path} → HTTP {resp.status_code}: {resp.text[:500]}'
        )
    if not resp.content:
        return {}
    return resp.json()


def test_connection() -> dict:
    """连通性检查；返回 {ok, memo_count, message}"""
    try:
        base, key = _config()
        if not key:
            return {
                'ok': False,
                'message': 'MEMOS_API_KEY 未配置',
                'memo_count': 0,
            }
        data = _request('GET', '/api/v1/memos', params={'pageSize': 5})
        memos = data.get('memos') or []
        return {
            'ok': True,
            'message': 'Memos 连接正常',
            'memo_count': len(memos),
            'url': base,
        }
    except Exception as e:
        return {'ok': False, 'message': str(e), 'memo_count': 0}


def _normalize_memo(m: dict) -> dict:
    """统一为 Hub API 使用的 dict 形状（含 content / name）"""
    return {
        'name': m.get('name'),
        'content': m.get('content', ''),
        'visibility': m.get('visibility'),
        'createTime': m.get('createTime'),
        'updateTime': m.get('updateTime'),
        'tags': m.get('tags') or [],
        'rowStatus': m.get('rowStatus') or 'ACTIVE',
    }


def list_memos_raw(page_size: int = 50, page_token: str = '') -> dict:
    params = {'pageSize': min(page_size, 200), 'state': 'NORMAL'}
    if page_token:
        params['pageToken'] = page_token
    return _request('GET', '/api/v1/memos', params=params)


def _snapshot_cache_path() -> str:
    return os.environ.get(
        'MEMOS_LIST_CACHE_PATH',
        os.path.join(tempfile.gettempdir(), 'openclaw-memos-list-cache.json'),
    )


def _read_memos_snapshot(path: str, now: float) -> Optional[List[dict]]:
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            payload = json.load(handle)
        generated_at = float(payload.get('generated_at') or 0)
        memos = payload.get('memos')
        if now - generated_at <= _MEMOS_SNAPSHOT_TTL_SECONDS and isinstance(memos, list):
            return [dict(memo) for memo in memos if isinstance(memo, dict)]
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        pass
    return None


def _fetch_all_memos() -> List[dict]:
    memos: List[dict] = []
    page_token = ''
    seen_tokens = set()
    while True:
        batch = list_memos_raw(page_size=200, page_token=page_token)
        memos.extend(memo for memo in (batch.get('memos') or []) if isinstance(memo, dict))
        next_token = batch.get('nextPageToken') or ''
        if not next_token or next_token in seen_tokens or not batch.get('memos'):
            break
        seen_tokens.add(next_token)
        page_token = next_token
    return memos


def _write_memos_snapshot(path: str, memos: List[dict], generated_at: float) -> None:
    parent = os.path.dirname(path) or tempfile.gettempdir()
    os.makedirs(parent, mode=0o700, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix='.memos-cache-', suffix='.json', dir=parent)
    try:
        if hasattr(os, 'fchmod'):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(
                {'generated_at': generated_at, 'memos': memos},
                handle,
                ensure_ascii=False,
                separators=(',', ':'),
            )
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _all_memos_cached() -> List[dict]:
    """Return one cross-gunicorn snapshot instead of rescanning per claw."""
    path = _snapshot_cache_path()
    now = time.time()
    cached = _read_memos_snapshot(path, now)
    if cached is not None:
        return cached

    lock_path = f'{path}.lock'
    os.makedirs(os.path.dirname(lock_path) or tempfile.gettempdir(), mode=0o700, exist_ok=True)
    with _memos_snapshot_lock:
        with open(lock_path, 'a+', encoding='utf-8') as lock_handle:
            try:
                os.chmod(lock_path, 0o600)
            except OSError:
                pass
            if fcntl is not None:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                now = time.time()
                cached = _read_memos_snapshot(path, now)
                if cached is not None:
                    return cached
                memos = _fetch_all_memos()
                _write_memos_snapshot(path, memos, now)
                return [dict(memo) for memo in memos]
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def invalidate_memos_snapshot() -> None:
    """Invalidate the shared read cache after a Hub-authored Memos mutation."""
    path = _snapshot_cache_path()
    lock_path = f'{path}.lock'
    os.makedirs(os.path.dirname(lock_path) or tempfile.gettempdir(), mode=0o700, exist_ok=True)
    with _memos_snapshot_lock:
        with open(lock_path, 'a+', encoding='utf-8') as lock_handle:
            if fcntl is not None:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def search_memos(
    tag=None,
    keyword=None,
    limit=50,
    claw_name: Optional[str] = None,
) -> List[dict]:
    """从 Memos 列出并过滤（按 claw 标签 / openclaw 标签 / 关键词）"""
    try:
        if not _config()[1]:
            logger.warning('MEMOS_API_KEY 未配置，search_memos 返回空列表')
            return []
    except Exception:
        return []

    claw_needle = f'#{_claw_tag(claw_name)}' if claw_name else None
    openclaw_needle = None
    openclaw_prefix_only = False
    if tag:
        t = str(tag).strip()
        if t.lower() == 'openclaw':
            openclaw_prefix_only = True
        elif '/' in t:
            openclaw_needle = f'#{t}' if not t.startswith('#') else t
        else:
            openclaw_needle = f'#openclaw/{t}'

    collected: List[dict] = []
    for m in _all_memos_cached():
        # 本部署 ListMemos 不按状态过滤，已归档也会返回 → 客户端跳过 ARCHIVED。
        if (m.get('rowStatus') or 'ACTIVE') == 'ARCHIVED':
            continue
        content = m.get('content') or ''
        if claw_needle and claw_needle not in content:
            continue
        if openclaw_prefix_only:
            if '#openclaw/' not in content:
                continue
        elif openclaw_needle and openclaw_needle not in content:
            continue
        if keyword and keyword not in content:
            continue
        collected.append(_normalize_memo(m))
        if len(collected) >= limit:
            break
    return collected


def get_memo(memo_name: str) -> Optional[dict]:
    """按 memo name / uid 直接读取（List 搜不到 PRIVATE 时仍可用）"""
    mid = _memo_id_from_name(memo_name)
    if not mid:
        return None
    try:
        data = _request('GET', f'/api/v1/memos/{mid}')
        return _normalize_memo(data)
    except Exception as e:
        logger.warning('get_memo %s 失败: %s', memo_name, e)
        return None


def create_memo(content: str, visibility: Optional[str] = None) -> dict:
    vis = (visibility or _default_visibility()).upper()
    if vis == 'PRIVATE':
        vis = 'PROTECTED'
    body = {
        'content': content,
        'visibility': vis,
    }
    created = _request('POST', '/api/v1/memos', json_body=body)
    invalidate_memos_snapshot()
    return _normalize_memo(created)


def update_memo(memo_name: str, content: str, visibility: Optional[str] = None) -> dict:
    mid = _memo_id_from_name(memo_name)
    vis = (visibility or _default_visibility()).upper()
    if vis == 'PRIVATE':
        vis = 'PROTECTED'
    body = {
        'content': content,
        'visibility': vis,
    }
    updated = _request(
        'PATCH',
        f'/api/v1/memos/{mid}',
        json_body=body,
        params={'updateMask': 'content,visibility'},
    )
    invalidate_memos_snapshot()
    return _normalize_memo(updated)


def archive_memo(memo_name: str) -> Optional[dict]:
    """把 memo 置为 ARCHIVED（软抹除：不再出现在 list/搜索/索引，但可追溯）。"""
    mid = _memo_id_from_name(memo_name)
    if not mid:
        return None
    # 本部署的 Memos 用 rowStatus(ACTIVE/ARCHIVED)，非 state；mask 需蛇形 row_status（camelCase 会被忽略）。
    updated = _request(
        'PATCH',
        f'/api/v1/memos/{mid}',
        json_body={'rowStatus': 'ARCHIVED'},
        params={'updateMask': 'row_status'},
    )
    invalidate_memos_snapshot()
    return _normalize_memo(updated)


def archive_taskctx_memos(claw_name: Optional[str], ref: str) -> List[str]:
    """归档某任务的短期工作记忆（taskctx memo）。

    ref 形如 ``todo-2298`` / ``agent_task-123`` / ``workflow_step-45``；
    对应内容标签 ``#openclaw/taskctx/{ref}``。返回被归档的 memo name 列表。
    任务处理完即调用，保证笔记索引只保留"进行中"的上下文。
    """
    ref = (ref or '').strip()
    if not claw_name or not ref:
        return []
    try:
        if not _config()[1]:
            return []
    except Exception:
        return []
    needle = f'#openclaw/taskctx/{ref}'
    archived: List[str] = []
    try:
        # 用类目前缀 'taskctx' 检索（→ #openclaw/taskctx），再用完整 needle 精确到 ref。
        candidates = search_memos(tag='taskctx', limit=80, claw_name=claw_name)
    except Exception as e:
        logger.warning('archive_taskctx_memos 搜索失败 ref=%s: %s', ref, e)
        return []
    for m in candidates:
        content = m.get('content') or ''
        name = m.get('name')
        if name and needle in content:
            try:
                archive_memo(name)
                archived.append(name)
            except Exception as e:
                logger.warning('归档 taskctx memo %s 失败: %s', name, e)
    return archived


def _find_existing_memo(
    claw_name: Optional[str],
    tag: str,
    scope_key: str,
    title: str,
) -> Optional[dict]:
    needle_title = f'# {title.strip()}'
    claw_needle = f'#{_claw_tag(claw_name)}'
    tag_needle = f'#{_openclaw_tag(tag, scope_key)}'
    candidates = search_memos(tag=tag, limit=80, claw_name=claw_name)
    for m in candidates:
        c = m.get('content') or ''
        if claw_needle in c and tag_needle in c and needle_title in c.split('\n')[0:3]:
            return m
    return None


def extract_and_deposit_knowledge(
    claw_id,
    claw_name,
    content,
    module=None,
    project=None,
    created_by=None,
):
    """Claw 工作记录 → 单条 Memos 笔记（零碎层，不写 Hub knowledge_entries）"""
    text = (content or '').strip()
    if not text:
        return []

    cat = module if module and module in KNOWLEDGE_TAGS else 'general'
    title = '工作记录沉淀'
    for line in text.split('\n'):
        s = line.strip()
        if s:
            title = s[:80]
            break

    body = _build_memo_content(
        title=title,
        body=text,
        claw_name=claw_name or created_by,
        tag=cat,
        scope_key=module or 'general',
        project_name=project,
    )
    memo = create_memo(body)
    out = {
        'action': 'created',
        'memo_name': memo.get('name'),
        'tag': cat,
        'title': title,
        'claw_id': claw_id,
        'claw_name': claw_name,
        'content_preview': text[:200],
    }
    return [out]


def upsert_knowledge(
    tag,
    content,
    title=None,
    scope_key='general',
    project_name=None,
    module_name=None,
    source_openclaw_id=None,
    source_type='manual',
    created_by=None,
):
    """零碎知识 upsert 到 Memos（按 claw + openclaw 标签 + 标题去重）"""
    claw_name = created_by
    if not claw_name and source_openclaw_id:
        try:
            from app.models import OpenClawInstance
            c = OpenClawInstance.query.get(source_openclaw_id)
            if c:
                claw_name = c.name
        except Exception:
            pass

    t = (title or tag or '知识').strip()[:255] or '知识'
    cat = (tag or 'general').strip()[:50] or 'general'
    sk = (scope_key or 'general')
    if isinstance(sk, str):
        sk = sk.strip() or 'general'

    memo_content = _build_memo_content(
        title=t,
        body=content,
        claw_name=claw_name,
        tag=cat,
        scope_key=sk,
        project_name=project_name,
        module_name=module_name,
    )

    existing = _find_existing_memo(claw_name, cat, sk, t)
    if existing and existing.get('name'):
        memo = update_memo(existing['name'], memo_content)
        action = 'updated'
    else:
        memo = create_memo(memo_content)
        action = 'created'

    return {
        'action': action,
        'memo_name': memo.get('name'),
        'tag': cat,
        'scope_key': sk,
        'title': t,
        'source_openclaw_id': source_openclaw_id,
        'source_type': source_type,
        'created_by': created_by or claw_name,
        'storage': 'memos',
    }


def migrate_private_openclaw_memos() -> dict:
    """把 SQLite 里 visibility=PRIVATE 且含 #claw- 的 memo 批量改为 PROTECTED（修复 #4495 后遗留）。"""
    import sqlite3
    import os
    db_path = os.environ.get('MEMOS_SQLITE_PATH', '/data/memos/memos_prod.db')
    if not os.path.isfile(db_path):
        return {'ok': False, 'message': f'no db at {db_path}', 'migrated': 0}

    target_vis = _default_visibility()
    if target_vis == 'PRIVATE':
        target_vis = 'PROTECTED'

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        "SELECT uid, content FROM memo WHERE visibility='PRIVATE' AND content LIKE '%#claw-%'"
    )
    rows = cur.fetchall()
    conn.close()

    migrated = []
    errors = []
    for uid, content in rows:
        try:
            existing = get_memo(uid)
            if not existing:
                errors.append({'uid': uid, 'error': 'get 404'})
                continue
            update_memo(
                uid,
                existing.get('content') or content,
                visibility=target_vis,
            )
            migrated.append(uid)
        except Exception as e:
            errors.append({'uid': uid, 'error': str(e)[:200]})

    return {
        'ok': True,
        'target_visibility': target_vis,
        'migrated': len(migrated),
        'uids': migrated,
        'errors': errors,
    }
