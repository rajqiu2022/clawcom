"""用例库与功能全景正式关联 API。"""
from datetime import datetime

from flask import jsonify, request
from sqlalchemy import desc

from app import db
from app.api import api_bp
from app.api.skills import _get_current_user
from app.api.testcases import _ensure_library_access
from app.models import (
    GameModulePanorama,
    PanoramaModuleTestMetric,
    TestCase,
    TestCaseChangeLog,
    TestCaseLibrary,
    TestCasePanoramaLink,
    _now,
)
from app.services.testcase_panorama_links import (
    agent_test_metric_payload,
    aggregate_module_test_metrics,
    collect_descendant_module_ids,
    detect_orphan_links,
    link_case_count,
)


def _operator():
    user = _get_current_user()
    if not user:
        return 'system'
    return getattr(user, '_claw_name', None) or getattr(user, 'username', None) or 'system'


def _parse_dt(raw):
    if not raw:
        return None
    value = str(raw).strip()
    for fmt in ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M',
                '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _link_dict(link):
    d = link.to_dict()
    module = link.module
    if module:
        metric = PanoramaModuleTestMetric.query.filter_by(module_id=module.id).first()
        d['module_risk_level'] = module.risk_level or 'normal'
        d['module_test_focus'] = module.test_focus or ''
        d['module_code_paths'] = module.code_paths or []
        d['test_metrics'] = metric.to_dict() if metric else None
    return d


def _case_rows(library_id=None):
    q = TestCase.query.filter(TestCase.is_placeholder != True)  # noqa: E712
    if library_id:
        q = q.filter_by(library_id=library_id)
    return [c.to_dict() for c in q.all()]


def _link_rows(query):
    return [
        {
            'id': link.id,
            'module_id': link.module_id,
            'library_id': link.library_id,
            'module_path': link.module_path or '',
            'case_pk': link.case_pk,
            'link_level': link.link_level or 'library',
            'case_count': link.case_count or 0,
        }
        for link in query.all()
    ]


def _upsert_metric(module_id, payload, *, source='sync'):
    module = GameModulePanorama.query.get(module_id)
    if not module:
        return None
    metric = PanoramaModuleTestMetric.query.filter_by(module_id=module_id).first()
    if not metric:
        metric = PanoramaModuleTestMetric(module_id=module_id)
        db.session.add(metric)
    metric.project_id = module.project_id
    metric.workspace_id = module.workspace_id
    metric.direct_case_count = int(payload.get('direct_case_count') or 0)
    metric.subtree_case_count = int(payload.get('subtree_case_count') or 0)
    metric.linked_library_count = int(payload.get('linked_library_count') or 0)
    metric.linked_directory_count = int(payload.get('linked_directory_count') or 0)
    if payload.get('bug_count') is not None:
        metric.bug_count = int(payload.get('bug_count') or 0)
    if payload.get('bug_risk_score') is not None:
        metric.bug_risk_score = int(payload.get('bug_risk_score') or 0)
    if payload.get('bug_risk_level'):
        metric.bug_risk_level = payload.get('bug_risk_level')
    metric.metrics_payload = payload.get('metrics_payload') or metric.metrics_payload or {}
    metric.source = source
    metric.updated_by = _operator()
    metric.synced_at = _now()
    return metric


def recalc_module_test_metrics(module_ids=None, project_id=None, workspace_id=None):
    modules_q = GameModulePanorama.query
    links_q = TestCasePanoramaLink.query
    if project_id is not None:
        modules_q = modules_q.filter_by(project_id=project_id)
        links_q = links_q.filter_by(project_id=project_id)
    if workspace_id is not None:
        modules_q = modules_q.filter_by(workspace_id=workspace_id)
        links_q = links_q.filter_by(workspace_id=workspace_id)
    modules = modules_q.all()
    links = links_q.all()
    module_rows = [m.to_dict() for m in modules]
    link_rows = [
        {
            'module_id': l.module_id,
            'library_id': l.library_id,
            'module_path': l.module_path or '',
            'link_level': l.link_level or 'library',
            'case_count': l.case_count or 0,
        }
        for l in links
    ]
    metrics = aggregate_module_test_metrics(module_rows, link_rows)
    if module_ids:
        parent_by_id = {m.id: m.parent_id for m in modules}
        target_ids = set()
        for mid in module_ids:
            current = mid
            while current and current not in target_ids:
                target_ids.add(current)
                current = parent_by_id.get(current)
    else:
        target_ids = set(metrics.keys())
    updated = []
    for mid in target_ids:
        payload = metrics.get(mid)
        if not payload:
            continue
        metric = _upsert_metric(mid, payload)
        if metric:
            updated.append(metric)
    return updated


def sync_testcase_panorama_links(project_id=None, workspace_id=None, library_id=None,
                                 dry_run=False):
    links_q = TestCasePanoramaLink.query
    if project_id is not None:
        links_q = links_q.filter_by(project_id=project_id)
    if workspace_id is not None:
        links_q = links_q.filter_by(workspace_id=workspace_id)
    if library_id is not None:
        links_q = links_q.filter_by(library_id=library_id)

    links = links_q.all()
    module_ids = {m.id for m in GameModulePanorama.query.all()}
    library_ids = {l.id for l in TestCaseLibrary.query.all()}
    cases = _case_rows(library_id=library_id)
    orphan_items = detect_orphan_links(_link_rows(links_q), module_ids, library_ids, cases)
    orphan_ids = {item['id'] for item in orphan_items}
    affected_module_ids = {link.module_id for link in links}
    case_count_updates = []

    for link in links:
        if link.id in orphan_ids:
            if not dry_run:
                db.session.delete(link)
            continue
        count = link_case_count(link.to_dict(), cases)
        if count != (link.case_count or 0):
            case_count_updates.append({'id': link.id, 'old': link.case_count or 0, 'new': count})
            if not dry_run:
                link.case_count = count
                link.last_verified_at = _now()
        affected_module_ids.add(link.module_id)

    metrics_updated = []
    if not dry_run:
        metrics_updated = recalc_module_test_metrics(
            module_ids=affected_module_ids,
            project_id=project_id,
            workspace_id=workspace_id,
        )

    return {
        'checked': len(links),
        'deleted_orphans': len(orphan_ids),
        'orphans': orphan_items,
        'case_count_updates': case_count_updates,
        'metrics_updated': len(metrics_updated),
        'dry_run': bool(dry_run),
    }


@api_bp.route('/testcase-panorama-links', methods=['GET'])
def list_testcase_panorama_links():
    q = TestCasePanoramaLink.query
    module_id = request.args.get('module_id', type=int)
    library_id = request.args.get('library_id', type=int)
    workspace_id = request.args.get('workspace_id', type=int)
    if module_id:
        q = q.filter_by(module_id=module_id)
    if library_id:
        library = TestCaseLibrary.query.get_or_404(library_id)
        _, err = _ensure_library_access(library, write=False)
        if err:
            return err
        q = q.filter_by(library_id=library_id)
    if workspace_id:
        q = q.filter_by(workspace_id=workspace_id)
    links = q.order_by(TestCasePanoramaLink.updated_at.desc()).all()
    return jsonify([_link_dict(link) for link in links])


@api_bp.route('/testcase-panorama-links', methods=['POST'])
def create_testcase_panorama_link():
    data = request.get_json(force=True) or {}
    module = GameModulePanorama.query.get_or_404(int(data.get('module_id') or 0))
    library = TestCaseLibrary.query.get_or_404(int(data.get('library_id') or 0))
    _, err = _ensure_library_access(library, write=True)
    if err:
        return err
    level = data.get('link_level') or ('directory' if data.get('module_path') else 'library')
    if level not in ('library', 'directory', 'case'):
        return jsonify({'error': 'link_level 必须是 library/directory/case'}), 400
    link = TestCasePanoramaLink(
        project_id=module.project_id,
        workspace_id=module.workspace_id,
        module_id=module.id,
        library_id=library.id,
        module_path=(data.get('module_path') or '').strip(),
        case_pk=data.get('case_pk'),
        link_level=level,
        source=data.get('source') or 'manual',
        confidence=float(data.get('confidence') or 1),
        created_by=_operator(),
        updated_by=_operator(),
    )
    existing = TestCasePanoramaLink.query.filter_by(
        workspace_id=link.workspace_id,
        module_id=link.module_id,
        library_id=link.library_id,
        module_path=link.module_path,
        case_pk=link.case_pk,
        link_level=link.link_level,
    ).first()
    if existing:
        return jsonify({'error': '关联已存在', 'link': existing.to_dict()}), 400
    link.case_count = link_case_count(link.to_dict(), _case_rows(library_id=library.id))
    link.last_verified_at = _now()
    db.session.add(link)
    try:
        db.session.flush()
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': f'关联已存在或参数无效: {e}'}), 400
    recalc_module_test_metrics(module_ids=collect_descendant_module_ids(
        [m.to_dict() for m in GameModulePanorama.query.filter_by(workspace_id=module.workspace_id).all()],
        module.id,
    ), workspace_id=module.workspace_id)
    db.session.commit()
    return jsonify(_link_dict(link)), 201


@api_bp.route('/testcase-panorama-links/<int:link_id>', methods=['PUT'])
def update_testcase_panorama_link(link_id):
    link = TestCasePanoramaLink.query.get_or_404(link_id)
    _, err = _ensure_library_access(link.library, write=True)
    if err:
        return err
    data = request.get_json(force=True) or {}
    for key in ('module_path', 'link_level', 'source'):
        if key in data:
            setattr(link, key, data[key] or '')
    if 'confidence' in data:
        link.confidence = float(data.get('confidence') or 1)
    link.updated_by = _operator()
    link.case_count = link_case_count(link.to_dict(), _case_rows(library_id=link.library_id))
    link.last_verified_at = _now()
    recalc_module_test_metrics(module_ids=[link.module_id], workspace_id=link.workspace_id)
    db.session.commit()
    return jsonify(_link_dict(link))


@api_bp.route('/testcase-panorama-links/<int:link_id>', methods=['DELETE'])
def delete_testcase_panorama_link(link_id):
    link = TestCasePanoramaLink.query.get_or_404(link_id)
    _, err = _ensure_library_access(link.library, write=True)
    if err:
        return err
    module_id = link.module_id
    workspace_id = link.workspace_id
    db.session.delete(link)
    recalc_module_test_metrics(module_ids=[module_id], workspace_id=workspace_id)
    db.session.commit()
    return jsonify({'ok': True})


@api_bp.route('/testcase-panorama-links/sync', methods=['POST'])
def sync_testcase_panorama_links_endpoint():
    data = request.get_json(force=True) or {}
    result = sync_testcase_panorama_links(
        project_id=data.get('project_id'),
        workspace_id=data.get('workspace_id'),
        library_id=data.get('library_id'),
        dry_run=bool(data.get('dry_run')),
    )
    if not data.get('dry_run'):
        db.session.commit()
    return jsonify(result)


@api_bp.route('/testcase-libraries/<int:library_id>/panorama-links', methods=['GET'])
def get_library_panorama_links(library_id):
    library = TestCaseLibrary.query.get_or_404(library_id)
    _, err = _ensure_library_access(library, write=False)
    if err:
        return err
    links = (TestCasePanoramaLink.query
             .filter_by(library_id=library_id)
             .order_by(TestCasePanoramaLink.module_path, TestCasePanoramaLink.id)
             .all())
    return jsonify([_link_dict(link) for link in links])


@api_bp.route('/panorama/modules/<int:module_id>/testcase-links', methods=['GET'])
def get_module_testcase_links(module_id):
    module = GameModulePanorama.query.get_or_404(module_id)
    include_children = request.args.get('include_children') == '1'
    module_ids = {module_id}
    if include_children:
        modules = GameModulePanorama.query.filter_by(workspace_id=module.workspace_id).all()
        module_ids = collect_descendant_module_ids([m.to_dict() for m in modules], module_id)
    links = (TestCasePanoramaLink.query
             .filter(TestCasePanoramaLink.module_id.in_(list(module_ids)))
             .order_by(TestCasePanoramaLink.library_id, TestCasePanoramaLink.module_path)
             .all())
    return jsonify([_link_dict(link) for link in links])


@api_bp.route('/panorama/modules/<int:module_id>/test-metrics', methods=['GET', 'POST'])
def module_test_metrics(module_id):
    module = GameModulePanorama.query.get_or_404(module_id)
    if request.method == 'POST':
        data = request.get_json(force=True) or {}
        metric = PanoramaModuleTestMetric.query.filter_by(module_id=module_id).first()
        try:
            base = agent_test_metric_payload(
                metric.to_dict() if metric else None,
                data,
            )
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 400
        metric = _upsert_metric(module_id, base, source=data.get('source') or 'agent')
        db.session.commit()
        return jsonify(metric.to_dict())
    metric = PanoramaModuleTestMetric.query.filter_by(module_id=module_id).first()
    if not metric:
        recalc_module_test_metrics(module_ids=[module_id], workspace_id=module.workspace_id)
        db.session.commit()
        metric = PanoramaModuleTestMetric.query.filter_by(module_id=module_id).first()
    return jsonify(metric.to_dict() if metric else {'module_id': module_id})


@api_bp.route('/testcase-libraries/<int:library_id>/changes', methods=['GET'])
def list_library_case_changes(library_id):
    library = TestCaseLibrary.query.get_or_404(library_id)
    _, err = _ensure_library_access(library, write=False)
    if err:
        return err
    q = TestCaseChangeLog.query.filter_by(library_id=library_id)
    since = _parse_dt(request.args.get('since'))
    until = _parse_dt(request.args.get('until'))
    module_path = request.args.get('module_path')
    changed_by = request.args.get('changed_by')
    change_type = request.args.get('change_type')
    if since:
        q = q.filter(TestCaseChangeLog.changed_at >= since)
    if until:
        q = q.filter(TestCaseChangeLog.changed_at <= until)
    if module_path is not None:
        q = q.filter(TestCaseChangeLog.module_path.like(f'{module_path}%') if module_path
                     else TestCaseChangeLog.module_path == '')
    if changed_by:
        q = q.filter_by(changed_by=changed_by)
    if change_type:
        q = q.filter_by(change_type=change_type)
    limit = min(request.args.get('limit', 200, type=int), 500)
    rows = q.order_by(desc(TestCaseChangeLog.changed_at)).limit(limit).all()
    return jsonify([r.to_dict() for r in rows])


@api_bp.route('/panorama/modules/<int:module_id>/testcase-changes', methods=['GET'])
def list_module_case_changes(module_id):
    module = GameModulePanorama.query.get_or_404(module_id)
    include_children = request.args.get('include_children') == '1'
    module_ids = {module_id}
    if include_children:
        modules = GameModulePanorama.query.filter_by(workspace_id=module.workspace_id).all()
        module_ids = collect_descendant_module_ids([m.to_dict() for m in modules], module_id)
    links = TestCasePanoramaLink.query.filter(TestCasePanoramaLink.module_id.in_(list(module_ids))).all()
    library_ids = {link.library_id for link in links}
    if not library_ids:
        return jsonify([])
    q = TestCaseChangeLog.query.filter(TestCaseChangeLog.library_id.in_(list(library_ids)))
    since = _parse_dt(request.args.get('since'))
    until = _parse_dt(request.args.get('until'))
    if since:
        q = q.filter(TestCaseChangeLog.changed_at >= since)
    if until:
        q = q.filter(TestCaseChangeLog.changed_at <= until)
    rows = q.order_by(desc(TestCaseChangeLog.changed_at)).limit(
        min(request.args.get('limit', 200, type=int), 500)
    ).all()
    return jsonify([r.to_dict() for r in rows])
