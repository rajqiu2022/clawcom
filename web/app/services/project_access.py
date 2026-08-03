"""Project access initialization and gating helpers."""

import logging
import os
import re
import time


logger = logging.getLogger(__name__)

_TAPD_REQUEST_TIMEOUT = float(os.getenv('PROJECT_INIT_TAPD_TIMEOUT', '3'))
_TAPD_SYNC_TIME_BUDGET = float(os.getenv('PROJECT_INIT_TAPD_TIME_BUDGET', '8'))


_PUBLIC_EXACT_PATHS = {
    '/',
    '/login',
    '/login/woa',
    '/logout',
    '/woa',
    '/_ngn_probe',
    '/project-required',
}

_PUBLIC_PREFIXES = (
    '/static/',
    '/r/',
    '/test-reports/share/',
    '/api/v1/auth/',
    '/api/v1/projects',
)


def is_project_required_path(path):
    """Whether a path should be blocked for logged-in users with no project."""
    path = (path or '/').split('?', 1)[0].rstrip('/') or '/'
    if path in _PUBLIC_EXACT_PATHS:
        return False
    return not any(path.startswith(prefix) for prefix in _PUBLIC_PREFIXES)


def _normalize_account(value):
    value = str(value or '').strip().lower()
    if not value:
        return ''
    if '\\' in value:
        value = value.rsplit('\\', 1)[-1]
    if '@' in value:
        value = value.split('@', 1)[0]
    return value.strip()


def _account_tokens(value):
    """Extract possible account tokens from TAPD member field values."""
    if value is None:
        return set()
    if isinstance(value, (list, tuple, set)):
        tokens = set()
        for item in value:
            tokens.update(_account_tokens(item))
        return tokens
    if not isinstance(value, str):
        value = str(value)

    raw = value.strip()
    if not raw:
        return set()

    tokens = {_normalize_account(raw)}
    for part in re.split(r'[\s,;，；/|()（）<>]+', raw):
        part = part.strip()
        if part:
            tokens.add(_normalize_account(part))
    for email_local in re.findall(r'([A-Za-z0-9_.-]+)@[A-Za-z0-9_.-]+', raw):
        tokens.add(_normalize_account(email_local))
    return {t for t in tokens if t}


def _walk_values(value):
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk_values(child)
    elif isinstance(value, (list, tuple, set)):
        for child in value:
            yield from _walk_values(child)
    else:
        yield value


def tapd_member_matches_username(member, username):
    """Match TAPD workspace member payload against an OA English account."""
    expected = _normalize_account(username)
    if not expected:
        return False
    for value in _walk_values(member):
        if expected in _account_tokens(value):
            return True
    return False


def _normalize_tapd_member_rows(data):
    if isinstance(data, dict):
        for key in ('items', 'list', 'users', 'data'):
            if isinstance(data.get(key), list):
                return data[key]
        return [data]
    if isinstance(data, list):
        return data
    return []


def tapd_workspace_has_user(workspace_id, username, api_user='', api_password=''):
    """Query TAPD and check whether username belongs to a workspace."""
    import requests
    from app.api.tapd import TAPD_API_BASE_URL

    workspace_id = str(workspace_id or '').strip()
    username = (username or '').strip()
    if not workspace_id or not username:
        return False
    if not api_user or not api_password:
        from app.api.tapd import _get_tapd_credentials
        api_user, api_password = _get_tapd_credentials()
    if not api_user or not api_password:
        raise ValueError('TAPD API 凭证未配置')

    endpoints = ('/workspaces/users', '/workspace_users')
    for endpoint in endpoints:
        try:
            resp = requests.request(
                'GET',
                f'{TAPD_API_BASE_URL}{endpoint}',
                params={
                    'workspace_id': workspace_id,
                    'user': username,
                    'limit': 200,
                    'page': 1,
                },
                auth=(api_user, api_password),
                timeout=_TAPD_REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            payload = resp.json()
            if payload.get('status') != 1:
                raise ValueError(f'TAPD API 错误: {payload.get("info", "未知错误")}')
            data = payload.get('data', [])
        except requests.exceptions.HTTPError as exc:
            # Different TAPD deployments expose the workspace-user endpoint under
            # different names. Try the next known path only for "not found".
            if exc.response is not None and exc.response.status_code == 404:
                continue
            raise

        rows = _normalize_tapd_member_rows(data)
        if any(tapd_member_matches_username(row, username) for row in rows):
            return True
        return False

    raise ValueError('TAPD workspace 成员接口不可用（/workspaces/users 与 /workspace_users 均失败）')


def user_has_project_access(user):
    """Return True if user should be treated as having project access."""
    if not user:
        return False
    if getattr(user, 'role', None) in ('super_admin', 'admin'):
        return True

    for pid in (getattr(user, 'managed_projects', None) or []):
        try:
            if int(pid) > 0:
                return True
        except Exception:
            continue

    bound_claw_id = getattr(user, 'bound_claw_id', None)
    if bound_claw_id:
        from app.models import OpenClawInstance
        claw = OpenClawInstance.query.get(bound_claw_id)
        if claw and claw.project_id:
            return True
    return False


def sync_user_projects_from_tapd(user):
    """Initialize a normal user's managed_projects from TAPD workspace membership."""
    if not user or not getattr(user, 'username', None):
        return {'assigned_project_ids': [], 'checked': 0, 'skipped': 'no_user'}
    if getattr(user, 'role', None) in ('super_admin', 'admin'):
        return {'assigned_project_ids': [], 'checked': 0, 'skipped': 'admin_role'}
    if user_has_project_access(user):
        return {'assigned_project_ids': [], 'checked': 0, 'skipped': 'already_has_project'}

    from app import db
    from app.api.tapd import _get_tapd_credentials
    from app.models import Project

    user_id = user.id
    username = user.username
    projects = [
        (int(p.id), p.name, str(p.tapd_workspace_id or '').strip())
        for p in (Project.query
                  .filter(Project.tapd_workspace_id.isnot(None),
                          Project.tapd_workspace_id != '')
                  .order_by(Project.id.asc())
                  .all())
    ]
    api_user, api_password = _get_tapd_credentials()
    # TAPD 是外部网络调用，不能在等待期间占着 SQLAlchemy 连接池连接。
    db.session.rollback()

    assigned = []
    checked = 0
    started_at = time.monotonic()
    for project_id, project_name, workspace_id in projects:
        if time.monotonic() - started_at > _TAPD_SYNC_TIME_BUDGET:
            logger.warning(
                '[PROJECT-INIT] TAPD sync time budget exceeded user=%s checked=%s assigned=%s',
                username, checked, assigned,
            )
            break
        checked += 1
        try:
            if tapd_workspace_has_user(workspace_id, username, api_user, api_password):
                assigned.append(project_id)
        except Exception as exc:
            logger.warning(
                '[PROJECT-INIT] TAPD workspace check failed user=%s project=%s workspace=%s error=%s',
                username, project_id, workspace_id or project_name, exc,
            )

    if assigned:
        from app.models import User
        user = User.query.get(user_id)
        if not user:
            return {'assigned_project_ids': [], 'checked': checked, 'skipped': 'user_missing'}
        user.managed_projects = assigned
        db.session.commit()
        logger.warning('[PROJECT-INIT] user=%s assigned projects=%s from TAPD',
                       username, assigned)

    return {'assigned_project_ids': assigned, 'checked': checked}


def ensure_user_projects_initialized(user, session_obj):
    """Run TAPD project initialization once per user session when needed."""
    if user_has_project_access(user):
        return {'assigned_project_ids': [], 'checked': 0, 'skipped': 'already_has_project'}

    key = 'tapd_project_init_checked_user_id'
    if session_obj.get(key) == getattr(user, 'id', None):
        return {'assigned_project_ids': [], 'checked': 0, 'skipped': 'checked_this_session'}

    result = sync_user_projects_from_tapd(user)
    session_obj[key] = getattr(user, 'id', None)
    session_obj.permanent = True
    return result
