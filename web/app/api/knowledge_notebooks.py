"""Project-scoped, versioned Wiki pages for release-test journals."""

import difflib
import hashlib
import json
import re
from datetime import datetime

from flask import jsonify, request

from app import db
from app.api import api_bp
from app.api.auth_utils import (
    get_current_claw,
    get_current_user,
    user_project_ids,
)
from app.models import (
    AuditLog,
    KnowledgeEntry,
    KnowledgeEntryRevision,
    KnowledgeDistribution,
    KnowledgeFavorite,
    KnowledgeNotebook,
    OpenClawInstance,
    Project,
    TestIteration,
    Topic,
)


DEFAULT_MODULES = [
    '研发需求变动',
    'AI用例设计问题',
    '质量问题',
    '测试进展与决策',
    '环境与自动化问题',
    '风险与待办',
]

_MARKDOWN_IMAGE = re.compile(r'(!\[[^\]]*\]\()\s*([^\s)]+)([^)]*\))')


def normalize_journal_markdown_media(content):
    """Keep Hub uploads same-origin and reject Agent-local image references."""
    text = str(content or '')

    def replace(match):
        prefix, url, suffix = match.groups()
        rendered = url.strip().replace('\\', '/')
        lowered = rendered.lower()
        if lowered.startswith('file:') or re.match(r'^[a-z]:/', lowered):
            raise ValueError(
                'Wiki 图片不能引用 Agent 本机路径；请先上传到 '
                'POST /api/v1/upload/image，再使用返回的 /static/uploads/... URL')
        static_match = re.match(
            r'^https?://[^/]+(/static/uploads/[^?#\s]+(?:[?#][^\s]*)?)$',
            rendered, flags=re.IGNORECASE)
        if static_match:
            rendered = static_match.group(1)
        elif lowered.startswith('http://'):
            raise ValueError(
                'HTTPS Wiki 不能引用 HTTP 图片；请上传到 Hub 或使用 HTTPS URL')
        elif not (lowered.startswith('https://')
                  or lowered.startswith('/static/uploads/')):
            raise ValueError(
                'Wiki 图片地址必须是 Hub /static/uploads/... 或 HTTPS URL；'
                'Agent 本机相对路径无法被其他协作者读取')
        return prefix + rendered + suffix

    return _MARKDOWN_IMAGE.sub(replace, text)


def _actor():
    claw = get_current_claw()
    if claw:
        return {
            'type': 'claw', 'id': int(claw.id),
            'name': claw.name or f'claw:{claw.id}',
            'claw': claw, 'user': None,
        }
    user = get_current_user()
    if user:
        return {
            'type': 'user', 'id': int(user.id),
            'name': (getattr(user, 'display_name', None)
                     or getattr(user, 'username', None)
                     or f'user:{user.id}'),
            'claw': None, 'user': user,
        }
    return None


def _actor_project_ids(actor):
    if not actor:
        return set()
    if actor['type'] == 'claw':
        project_id = getattr(actor['claw'], 'project_id', None)
        return {int(project_id)} if project_id else set()
    user = actor['user']
    ids = set(user_project_ids(user))
    username = str(getattr(user, 'username', '') or '').strip()
    if username:
        rows = (OpenClawInstance.query
                .filter(OpenClawInstance.owner == username,
                        OpenClawInstance.status != 'deleted')
                .with_entities(OpenClawInstance.project_id).all())
        ids.update(int(row[0]) for row in rows if row[0])
    return ids


def _can_access_project(actor, project_id):
    if not actor or not project_id:
        return False
    if actor['type'] == 'claw':
        claw = actor['claw']
        if claw.role == 'admin' and claw.project_id is None:
            return True
        return int(project_id) in _actor_project_ids(actor)
    role = getattr(actor['user'], 'role', None)
    project_ids = _actor_project_ids(actor)
    if role == 'super_admin' or (role == 'admin' and not project_ids):
        return True
    return int(project_id) in project_ids


def _error(code, message, status=400, **details):
    payload = {'error': message, 'code': code}
    if details:
        payload['details'] = details
    return jsonify(payload), status


def _notebook(notebook_id, actor):
    row = db.session.get(KnowledgeNotebook, notebook_id)
    if not row or not _can_access_project(actor, row.project_id):
        return None
    return row


def _journal_page(page_id, actor, lock=False):
    query = KnowledgeEntry.query.filter_by(
        id=page_id, entry_type='test_journal')
    if lock:
        query = query.with_for_update()
    row = query.first()
    project_id = row.project_id if row else None
    if row and not project_id and row.notebook:
        project_id = row.notebook.project_id
    if not row or not _can_access_project(actor, project_id):
        return None
    return row


def _sha256_text(value):
    return hashlib.sha256(str(value or '').encode('utf-8')).hexdigest()


def _request_sha(title, content, summary, rollback_from=None):
    return hashlib.sha256(json.dumps({
        'title': title, 'content': content,
        'change_summary': summary,
        'rollback_from_revision': rollback_from,
    }, ensure_ascii=False, sort_keys=True,
       separators=(',', ':')).encode('utf-8')).hexdigest()


def _audit(action, resource_id, resource_name, actor, detail):
    db.session.add(AuditLog(
        action=action,
        resource_type='knowledge_test_journal',
        resource_id=resource_id,
        resource_name=resource_name,
        operator=actor['name'],
        ip_address=request.remote_addr,
        detail=json.dumps(detail, ensure_ascii=False, sort_keys=True),
    ))


def _audit_notebook(action, notebook, actor, detail):
    db.session.add(AuditLog(
        action=action,
        resource_type='knowledge_test_journal_notebook',
        resource_id=notebook.id,
        resource_name=notebook.title,
        operator=actor['name'],
        ip_address=request.remote_addr,
        detail=json.dumps(detail, ensure_ascii=False, sort_keys=True),
    ))


def _remove_journal_page_references(page_ids):
    """Remove references that do not cascade with KnowledgeEntry deletion."""
    ids = [int(value) for value in (page_ids or []) if value]
    if not ids:
        return
    Topic.query.filter(Topic.review_knowledge_id.in_(ids)).update(
        {'review_knowledge_id': None}, synchronize_session=False)
    KnowledgeDistribution.query.filter(
        KnowledgeDistribution.knowledge_id.in_(ids)).delete(
            synchronize_session=False)
    KnowledgeFavorite.query.filter(
        KnowledgeFavorite.knowledge_id.in_(ids)).delete(
            synchronize_session=False)


def _modules(value):
    values = value if isinstance(value, list) else DEFAULT_MODULES
    result = []
    for item in values:
        name = str(item or '').strip()[:100]
        if name and name not in result:
            result.append(name)
    return result or list(DEFAULT_MODULES)


def _create_revision(page, actor, title, content, summary,
                     expected_revision, idempotency_key,
                     rollback_from=None):
    try:
        content = normalize_journal_markdown_media(content)
    except ValueError as exc:
        return None, _error('INVALID_JOURNAL_MEDIA_URL', str(exc), 400)
    current = int(page.current_revision or 0)
    try:
        expected = int(expected_revision)
    except (TypeError, ValueError):
        return None, _error(
            'EXPECTED_REVISION_REQUIRED',
            'expected_revision 必须是当前版本号', 400,
            current_revision=current)
    idem = str(idempotency_key or '').strip()[:128]
    if not idem:
        return None, _error(
            'IDEMPOTENCY_KEY_REQUIRED',
            '保存版本必须携带 Idempotency-Key', 400)
    request_sha = _request_sha(title, content, summary, rollback_from)
    replay = KnowledgeEntryRevision.query.filter_by(
        editor_type=actor['type'],
        editor_id=actor['id'],
        idempotency_key=idem,
    ).first()
    if replay:
        if replay.knowledge_id != page.id or replay.request_sha256 != request_sha:
            return None, _error(
                'IDEMPOTENCY_KEY_REUSED',
                '相同 Idempotency-Key 已用于不同内容', 409)
        return replay, None
    if expected != current:
        return None, _error(
            'KNOWLEDGE_REVISION_CONFLICT',
            '页面已被其他协作者更新，请比较最新版本后重试', 409,
            expected_revision=expected,
            current_revision=current)
    next_revision = current + 1
    revision = KnowledgeEntryRevision(
        knowledge_id=page.id,
        revision_no=next_revision,
        title=title,
        content_markdown=content,
        content_sha256=_sha256_text(content),
        change_summary=summary,
        based_on_revision=current or None,
        rollback_from_revision=rollback_from,
        editor_type=actor['type'], editor_id=actor['id'],
        editor_name=actor['name'], idempotency_key=idem,
        request_sha256=request_sha,
    )
    db.session.add(revision)
    page.title = title
    page.content = content
    page.current_revision = next_revision
    page.lock_version = int(page.lock_version or 0) + 1
    page.updated_at = datetime.now()
    return revision, None


@api_bp.route('/knowledge-notebooks', methods=['GET'])
def list_knowledge_notebooks():
    actor = _actor()
    if not actor:
        return _error('UNAUTHENTICATED', '未认证', 401)
    try:
        project_id = int(request.args.get('project_id') or 0)
    except (TypeError, ValueError):
        project_id = 0
    if not project_id or not _can_access_project(actor, project_id):
        return _error('PROJECT_ACCESS_DENIED', '无权访问该项目纪要', 403)
    status = str(request.args.get('status') or 'active').strip()
    query = KnowledgeNotebook.query.filter_by(project_id=project_id)
    if status != 'all':
        query = query.filter_by(status=status)
    rows = query.order_by(
        KnowledgeNotebook.updated_at.desc(), KnowledgeNotebook.id.desc()).all()
    return jsonify({'items': [row.to_dict(with_pages=True) for row in rows]})


@api_bp.route('/knowledge-notebooks', methods=['POST'])
def create_knowledge_notebook():
    actor = _actor()
    data = request.get_json() or {}
    try:
        project_id = int(data.get('project_id') or 0)
    except (TypeError, ValueError):
        project_id = 0
    if not actor:
        return _error('UNAUTHENTICATED', '未认证', 401)
    if not project_id or not _can_access_project(actor, project_id):
        return _error('PROJECT_ACCESS_DENIED', '无权创建该项目纪要', 403)
    project = db.session.get(Project, project_id)
    title = str(data.get('title') or '').strip()[:255]
    if not project or not title:
        return _error('INVALID_NOTEBOOK', '项目和纪要标题必填', 400)
    iteration = None
    if data.get('iteration_id') not in (None, ''):
        try:
            iteration_id = int(data.get('iteration_id'))
        except (TypeError, ValueError):
            return _error('INVALID_ITERATION', '测试迭代 ID 格式错误', 400)
        iteration = db.session.get(TestIteration, iteration_id)
        if not iteration or int(iteration.project_id or 0) != project_id:
            return _error(
                'ITERATION_PROJECT_MISMATCH',
                '测试迭代不存在或不属于当前项目', 409)
    row = KnowledgeNotebook(
        project_id=project_id,
        title=title,
        version_name=str(
            data.get('version_name')
            or (iteration.version_name if iteration else '')
            or (iteration.name if iteration else '')).strip()[:120],
        iteration_id=iteration.id if iteration else None,
        modules_json=_modules(data.get('modules')),
        status='active',
        created_by_type=actor['type'], created_by_id=actor['id'],
        created_by_name=actor['name'],
    )
    db.session.add(row)
    db.session.flush()
    _audit('create', row.id, row.title, actor, {
        'project_id': project_id, 'modules': row.modules_json})
    db.session.commit()
    return jsonify(row.to_dict(with_pages=True)), 201


@api_bp.route('/knowledge-notebooks/<int:notebook_id>', methods=['GET'])
def get_knowledge_notebook(notebook_id):
    actor = _actor()
    row = _notebook(notebook_id, actor)
    if not row:
        return _error('NOTEBOOK_NOT_FOUND', '纪要本不存在或无权访问', 404)
    return jsonify(row.to_dict(with_pages=True))


@api_bp.route('/knowledge-notebooks/<int:notebook_id>', methods=['DELETE'])
def permanently_delete_knowledge_notebook(notebook_id):
    actor = _actor()
    row = _notebook(notebook_id, actor)
    if not row:
        return _error('NOTEBOOK_NOT_FOUND', '纪要本不存在或无权访问', 404)
    data = request.get_json(silent=True) or {}
    if data.get('confirmed') is not True:
        return _error(
            'PERMANENT_DELETE_CONFIRMATION_REQUIRED',
            '永久删除纪要本必须显式提交 confirmed=true', 400)
    pages = list(row.pages)
    page_ids = [page.id for page in pages]
    revision_count = sum(len(page.revisions) for page in pages)
    result = {
        'deleted': True,
        'notebook_id': row.id,
        'title': row.title,
        'page_count': len(page_ids),
        'revision_count': revision_count,
    }
    _remove_journal_page_references(page_ids)
    _audit_notebook('permanent_delete', row, actor, result)
    db.session.delete(row)
    db.session.commit()
    return jsonify(result)


@api_bp.route('/knowledge-notebooks/<int:notebook_id>/pages', methods=['POST'])
def create_knowledge_notebook_page(notebook_id):
    actor = _actor()
    notebook = _notebook(notebook_id, actor)
    if not notebook:
        return _error('NOTEBOOK_NOT_FOUND', '纪要本不存在或无权访问', 404)
    data = request.get_json() or {}
    title = str(data.get('title') or '').strip()[:255]
    content = str(data.get('content') or '')
    module_name = str(data.get('module_name') or '').strip()[:100]
    if not title or not module_name:
        return _error('INVALID_PAGE', '页面标题和所属模块必填', 400)
    if module_name not in _modules(notebook.modules_json):
        return _error('INVALID_MODULE', '所属模块不在纪要本模块列表中', 400)
    content = content or '# ' + title
    summary = str(data.get('change_summary') or '创建页面')[:500]
    idem = str(request.headers.get('Idempotency-Key') or '').strip()[:128]
    if not idem:
        return _error('IDEMPOTENCY_KEY_REQUIRED',
                      '创建页面必须携带 Idempotency-Key', 400)
    request_sha = _request_sha(title, content, summary)
    replay = KnowledgeEntryRevision.query.filter_by(
        editor_type=actor['type'], editor_id=actor['id'],
        idempotency_key=idem).first()
    if replay:
        if (replay.request_sha256 != request_sha
                or not replay.knowledge
                or replay.knowledge.notebook_id != notebook.id):
            return _error('IDEMPOTENCY_KEY_REUSED',
                          '相同 Idempotency-Key 已用于其他内容', 409)
        return jsonify(replay.knowledge.to_dict()), 200
    page = KnowledgeEntry(
        title=title, content=content,
        category='test-journal', scope='project',
        project_id=notebook.project_id,
        project_name=notebook.project.name if notebook.project else None,
        module_name=module_name,
        entry_type='test_journal', notebook_id=notebook.id,
        status='approved', source_type=(
            'openclaw' if actor['type'] == 'claw' else 'manual'),
        source_openclaw_id=(actor['id'] if actor['type'] == 'claw' else None),
        created_by=actor['name'], current_revision=0, lock_version=0,
    )
    db.session.add(page)
    db.session.flush()
    revision, error = _create_revision(
        page, actor, title, page.content,
        summary, 0, idem)
    if error:
        db.session.rollback()
        return error
    _audit('create', page.id, page.title, actor, {
        'notebook_id': notebook.id, 'module_name': module_name,
        'revision': revision.revision_no})
    db.session.commit()
    return jsonify(page.to_dict()), 201


@api_bp.route('/knowledge/journal-pages/<int:page_id>', methods=['GET'])
def get_knowledge_journal_page(page_id):
    actor = _actor()
    page = _journal_page(page_id, actor)
    if not page:
        return _error('PAGE_NOT_FOUND', '纪要页面不存在或无权访问', 404)
    payload = page.to_dict()
    payload['can_edit'] = not bool(page.archived_at)
    return jsonify(payload)


@api_bp.route('/knowledge/<int:page_id>/revisions', methods=['GET'])
def list_knowledge_revisions(page_id):
    actor = _actor()
    page = _journal_page(page_id, actor)
    if not page:
        return _error('PAGE_NOT_FOUND', '纪要页面不存在或无权访问', 404)
    rows = KnowledgeEntryRevision.query.filter_by(
        knowledge_id=page.id).order_by(
        KnowledgeEntryRevision.revision_no.desc()).all()
    return jsonify({
        'knowledge_id': page.id,
        'current_revision': int(page.current_revision or 0),
        'items': [row.to_dict() for row in rows],
    })


@api_bp.route('/knowledge/<int:page_id>/revisions/<int:revision_no>',
              methods=['GET'])
def get_knowledge_revision(page_id, revision_no):
    actor = _actor()
    page = _journal_page(page_id, actor)
    if not page:
        return _error('PAGE_NOT_FOUND', '纪要页面不存在或无权访问', 404)
    revision = KnowledgeEntryRevision.query.filter_by(
        knowledge_id=page.id, revision_no=revision_no).first()
    if not revision:
        return _error('REVISION_NOT_FOUND', '历史版本不存在', 404)
    return jsonify(revision.to_dict(with_content=True))


@api_bp.route('/knowledge/<int:page_id>/revisions', methods=['POST'])
def create_knowledge_revision(page_id):
    actor = _actor()
    page = _journal_page(page_id, actor, lock=True)
    if not page:
        return _error('PAGE_NOT_FOUND', '纪要页面不存在或无权访问', 404)
    if page.archived_at:
        return _error(
            'JOURNAL_PAGE_ARCHIVED', '页面已归档，请恢复后再编辑', 409)
    data = request.get_json() or {}
    title = str(data.get('title') or page.title or '').strip()[:255]
    content = str(data.get('content') if data.get('content') is not None
                  else page.content or '')
    summary = str(data.get('change_summary') or '更新页面').strip()[:500]
    if not title or not content:
        return _error('INVALID_REVISION', '标题和正文不能为空', 400)
    revision, error = _create_revision(
        page, actor, title, content, summary,
        data.get('expected_revision'), request.headers.get('Idempotency-Key'))
    if error:
        db.session.rollback()
        return error
    if data.get('module_name'):
        module_name = str(data['module_name']).strip()[:100]
        if module_name not in _modules(page.notebook.modules_json):
            db.session.rollback()
            return _error('INVALID_MODULE', '所属模块不存在', 400)
        page.module_name = module_name
    _audit('revision', page.id, page.title, actor, {
        'revision': revision.revision_no,
        'based_on_revision': revision.based_on_revision,
        'change_summary': revision.change_summary})
    db.session.commit()
    return jsonify({
        'page': page.to_dict(),
        'revision': revision.to_dict(with_content=True),
    }), 201


def _revision(page_id, revision_no):
    return KnowledgeEntryRevision.query.filter_by(
        knowledge_id=page_id, revision_no=revision_no).first()


@api_bp.route('/knowledge/<int:page_id>/compare', methods=['GET'])
def compare_knowledge_revisions(page_id):
    actor = _actor()
    page = _journal_page(page_id, actor)
    if not page:
        return _error('PAGE_NOT_FOUND', '纪要页面不存在或无权访问', 404)
    try:
        from_revision = int(request.args.get('from') or 0)
        to_revision = int(request.args.get('to') or page.current_revision or 0)
    except (TypeError, ValueError):
        return _error('INVALID_REVISION', '版本号格式错误', 400)
    before = _revision(page.id, from_revision)
    after = _revision(page.id, to_revision)
    if not before or not after:
        return _error('REVISION_NOT_FOUND', '对比版本不存在', 404)
    lines = list(difflib.unified_diff(
        before.content_markdown.splitlines(),
        after.content_markdown.splitlines(),
        fromfile=f'revision-{from_revision}',
        tofile=f'revision-{to_revision}', lineterm=''))
    return jsonify({
        'from': before.to_dict(), 'to': after.to_dict(),
        'diff': lines,
        'stats': {
            'added': sum(1 for line in lines if line.startswith('+')
                         and not line.startswith('+++')),
            'removed': sum(1 for line in lines if line.startswith('-')
                           and not line.startswith('---')),
        },
    })


@api_bp.route('/knowledge/<int:page_id>/rollback', methods=['POST'])
def rollback_knowledge_revision(page_id):
    actor = _actor()
    page = _journal_page(page_id, actor, lock=True)
    if not page:
        return _error('PAGE_NOT_FOUND', '纪要页面不存在或无权访问', 404)
    if page.archived_at:
        return _error(
            'JOURNAL_PAGE_ARCHIVED', '页面已归档，请恢复后再回退', 409)
    data = request.get_json() or {}
    try:
        target_no = int(data.get('target_revision') or 0)
    except (TypeError, ValueError):
        target_no = 0
    target = _revision(page.id, target_no)
    if not target:
        return _error('REVISION_NOT_FOUND', '要恢复的版本不存在', 404)
    summary = str(data.get('change_summary')
                  or f'恢复到 Revision {target_no}').strip()[:500]
    revision, error = _create_revision(
        page, actor, target.title, target.content_markdown, summary,
        data.get('expected_revision'), request.headers.get('Idempotency-Key'),
        rollback_from=target_no)
    if error:
        db.session.rollback()
        return error
    _audit('rollback', page.id, page.title, actor, {
        'revision': revision.revision_no,
        'rollback_from_revision': target_no})
    db.session.commit()
    return jsonify({
        'page': page.to_dict(),
        'revision': revision.to_dict(with_content=True),
    }), 201


@api_bp.route('/knowledge/<int:page_id>/archive', methods=['POST'])
def archive_knowledge_journal_page(page_id):
    actor = _actor()
    page = _journal_page(page_id, actor, lock=True)
    if not page:
        return _error('PAGE_NOT_FOUND', '纪要页面不存在或无权访问', 404)
    if page.archived_at:
        return jsonify(page.to_dict())
    page.archived_at = datetime.now()
    page.lock_version = int(page.lock_version or 0) + 1
    _audit('archive', page.id, page.title, actor, {
        'notebook_id': page.notebook_id,
        'current_revision': page.current_revision})
    db.session.commit()
    return jsonify(page.to_dict())


@api_bp.route('/knowledge/<int:page_id>/restore', methods=['POST'])
def restore_knowledge_journal_page(page_id):
    actor = _actor()
    page = _journal_page(page_id, actor, lock=True)
    if not page:
        return _error('PAGE_NOT_FOUND', '纪要页面不存在或无权访问', 404)
    if not page.archived_at:
        return jsonify(page.to_dict())
    page.archived_at = None
    page.lock_version = int(page.lock_version or 0) + 1
    _audit('restore', page.id, page.title, actor, {
        'notebook_id': page.notebook_id,
        'current_revision': page.current_revision})
    db.session.commit()
    return jsonify(page.to_dict())


@api_bp.route('/knowledge/<int:page_id>/permanent', methods=['DELETE'])
def permanently_delete_knowledge_journal_page(page_id):
    actor = _actor()
    page = _journal_page(page_id, actor, lock=True)
    if not page:
        return _error('PAGE_NOT_FOUND', '纪要页面不存在或无权访问', 404)
    data = request.get_json(silent=True) or {}
    if data.get('confirmed') is not True:
        return _error(
            'PERMANENT_DELETE_CONFIRMATION_REQUIRED',
            '永久删除必须显式提交 confirmed=true', 400)
    page_info = {
        'page_id': page.id, 'title': page.title,
        'notebook_id': page.notebook_id,
        'revision_count': len(page.revisions),
    }
    _remove_journal_page_references([page.id])
    _audit('permanent_delete', page.id, page.title, actor, page_info)
    db.session.delete(page)
    db.session.commit()
    return jsonify(dict({'deleted': True}, **page_info))
