"""游戏功能模块全景视图 API（Game Module Panorama）

提供功能模块树的 CRUD、变更记录管理、树形查询等接口。
Agent 通过 Bearer Token 调用来建立和更新全景数据。
"""
from datetime import datetime

from flask import request, jsonify, abort
from sqlalchemy import desc, or_

from app import db
from app.api import api_bp
from app.api.skills import _get_current_user
from app.models import (
    GameModuleChangeLog,
    GameModulePanorama,
    GameModuleRelation,
    PanoramaWorkspace,
    PanoramaCodeEntity,
    PanoramaImpactAnalysis,
    PanoramaModuleCodeLink,
    PanoramaSnapshot,
    PanoramaSnapshotItem,
    PanoramaModuleTestMetric,
    Project,
    TestCaseLibrary,
    TestCasePanoramaLink,
    _now,
)
from app.services.panorama_graph import build_graph_payload
from app.services.panorama_impact import analyze_changed_files
from app.services.panorama_snapshot import (
    build_snapshot_items,
    diff_snapshot_items,
    plan_restore_items,
    snapshot_ids_to_prune,
)


RELATION_TYPES = {
    'child',
    'depends_on',
    'affects',
    'shared_resource',
    'api_flow',
    'data_flow',
    'test_overlap',
}


def _operator_info():
    """获取操作者信息（用户名或 agent 名）"""
    user = _get_current_user()
    if user:
        return user.username if hasattr(user, 'username') else (user.get('username') or user.get('_claw_name') or 'unknown')
    return 'anonymous'


def _project_filter(query, model, project_id):
    if project_id is not None:
        return query.filter(model.project_id == project_id)
    return query


def _normalize_title(title):
    return (title or '').strip()[:180]


def _project_id_from_request(data=None):
    if data and data.get('project_id') is not None:
        try:
            return int(data.get('project_id'))
        except Exception:
            return None
    return request.args.get('project_id', type=int)


def _workspace_id_from_request(data=None):
    if data and data.get('workspace_id') is not None:
        try:
            return int(data.get('workspace_id'))
        except Exception:
            return None
    return request.args.get('workspace_id', type=int)


def _workspace_title_from_request(data=None):
    if data and data.get('panorama_title') is not None:
        return _normalize_title(data.get('panorama_title'))
    if data and data.get('workspace_title') is not None:
        return _normalize_title(data.get('workspace_title'))
    return _normalize_title(
        request.args.get('panorama_title') or request.args.get('workspace_title')
    )


def _workspace_query_by_project_title(project_id, title):
    q = PanoramaWorkspace.query.filter(PanoramaWorkspace.title == title)
    if project_id is None:
        q = q.filter(PanoramaWorkspace.project_id.is_(None))
    else:
        q = q.filter(PanoramaWorkspace.project_id == project_id)
    return q


def _get_or_create_default_workspace(project_id):
    q = PanoramaWorkspace.query.filter_by(is_default=True)
    if project_id is None:
        q = q.filter(PanoramaWorkspace.project_id.is_(None))
    else:
        q = q.filter(PanoramaWorkspace.project_id == project_id)
    workspace = q.order_by(PanoramaWorkspace.id.asc()).first()
    if workspace:
        return workspace
    workspace = PanoramaWorkspace(
        project_id=project_id,
        title='项目功能全景图',
        description='默认功能全景',
        is_default=True,
        created_by=_operator_info(),
        updated_by=_operator_info(),
    )
    db.session.add(workspace)
    db.session.flush()
    return workspace


def _resolve_workspace(project_id=None, workspace_id=None, title=None, create=False):
    """Resolve a workspace from request fields while preserving old project-only APIs."""
    if project_id is not None:
        try:
            project_id = int(project_id)
        except Exception:
            project_id = None
    if workspace_id:
        workspace = PanoramaWorkspace.query.get(workspace_id)
        if workspace and project_id is not None and workspace.project_id != project_id:
            abort(400, description='workspace_id 与 project_id 不一致')
        return workspace
    title = _normalize_title(title)
    if title:
        workspace = _workspace_query_by_project_title(project_id, title).first()
        if workspace or not create:
            return workspace
        workspace = PanoramaWorkspace(
            project_id=project_id,
            title=title,
            description='',
            is_default=False,
            created_by=_operator_info(),
            updated_by=_operator_info(),
        )
        db.session.add(workspace)
        db.session.flush()
        return workspace
    if project_id is not None or create:
        return _get_or_create_default_workspace(project_id)
    return None


def _resolve_workspace_from_request(data=None, *, create=False):
    return _resolve_workspace(
        project_id=_project_id_from_request(data),
        workspace_id=_workspace_id_from_request(data),
        title=_workspace_title_from_request(data),
        create=create,
    )


def _filter_workspace(query, model, workspace):
    if workspace:
        return query.filter(model.workspace_id == workspace.id)
    return query


def _relation_payload(relation):
    return relation.to_dict()


def _validate_relation_payload(data):
    source_id = data.get('source_module_id')
    target_id = data.get('target_module_id')
    if not source_id or not target_id:
        return None, None, None, 'source_module_id 和 target_module_id 必填'
    if int(source_id) == int(target_id):
        return None, None, None, 'source_module_id 和 target_module_id 不能相同'

    relation_type = (data.get('relation_type') or 'depends_on').strip()
    if relation_type not in RELATION_TYPES:
        return None, None, None, f'不支持的 relation_type: {relation_type}'

    source = GameModulePanorama.query.get(source_id)
    target = GameModulePanorama.query.get(target_id)
    if not source or not target:
        return None, None, None, '源模块或目标模块不存在'
    if source.project_id != target.project_id:
        return None, None, None, '源模块和目标模块必须属于同一项目'
    if source.workspace_id != target.workspace_id:
        return None, None, None, '源模块和目标模块必须属于同一功能全景'

    return source, target, relation_type, None


def _collect_workspace_panorama_state(project_id, workspace):
    mq = GameModulePanorama.query
    rq = GameModuleRelation.query
    eq = PanoramaCodeEntity.query
    lq = PanoramaModuleCodeLink.query
    if workspace:
        mq = mq.filter(GameModulePanorama.workspace_id == workspace.id)
        rq = rq.filter(GameModuleRelation.workspace_id == workspace.id)
        eq = eq.filter(PanoramaCodeEntity.workspace_id == workspace.id)
        lq = lq.filter(PanoramaModuleCodeLink.workspace_id == workspace.id)
    elif project_id is not None:
        mq = mq.filter(GameModulePanorama.project_id == project_id)
        rq = rq.filter(GameModuleRelation.project_id == project_id)
        eq = eq.filter(PanoramaCodeEntity.project_id == project_id)
        lq = lq.filter(PanoramaModuleCodeLink.project_id == project_id)
    modules = mq.all()
    relations = rq.all()
    entities = eq.all()
    links = lq.all()
    module_path_by_id = {m.id: m.path or m.name for m in modules}
    items = build_snapshot_items(
        modules=[m.to_dict() for m in modules],
        relations=[r.to_dict() for r in relations],
        code_entities=[e.to_dict() for e in entities],
        module_code_links=[l.to_dict() for l in links],
        module_path_by_id=module_path_by_id,
    )
    return modules, relations, entities, items


def _collect_project_panorama_state(project_id):
    workspace = _resolve_workspace(project_id=project_id, create=True)
    return _collect_workspace_panorama_state(project_id, workspace)


# ===== 功能全景标题 / 工作区 =====

@api_bp.route('/panorama/workspaces', methods=['GET'])
def list_panorama_workspaces():
    project_id = request.args.get('project_id', type=int)
    q = PanoramaWorkspace.query
    if project_id is None and 'project_id' in request.args:
        q = q.filter(PanoramaWorkspace.project_id.is_(None))
    elif project_id is not None:
        q = q.filter(PanoramaWorkspace.project_id == project_id)
    items = q.order_by(PanoramaWorkspace.is_default.desc(),
                       PanoramaWorkspace.updated_at.desc(),
                       PanoramaWorkspace.id.asc()).all()
    if project_id is not None and not items:
        workspace = _get_or_create_default_workspace(project_id)
        db.session.commit()
        items = [workspace]
    return jsonify([w.to_dict() for w in items])


@api_bp.route('/panorama/workspaces', methods=['POST'])
def create_panorama_workspace():
    data = request.get_json(force=True) or {}
    project_id = data.get('project_id')
    title = _normalize_title(data.get('title') or data.get('panorama_title'))
    if not title:
        return jsonify({'error': 'title 不能为空'}), 400
    existing = _workspace_query_by_project_title(project_id, title).first()
    if existing:
        return jsonify(existing.to_dict()), 200
    workspace = PanoramaWorkspace(
        project_id=project_id,
        title=title,
        description=data.get('description'),
        is_default=bool(data.get('is_default', False)),
        created_by=_operator_info(),
        updated_by=_operator_info(),
    )
    db.session.add(workspace)
    db.session.commit()
    return jsonify(workspace.to_dict()), 201


@api_bp.route('/panorama/workspaces/<int:workspace_id>', methods=['PUT'])
def update_panorama_workspace(workspace_id):
    workspace = PanoramaWorkspace.query.get_or_404(workspace_id)
    data = request.get_json(force=True) or {}
    if 'title' in data:
        title = _normalize_title(data.get('title'))
        if not title:
            return jsonify({'error': 'title 不能为空'}), 400
        duplicate = (_workspace_query_by_project_title(workspace.project_id, title)
                     .filter(PanoramaWorkspace.id != workspace.id)
                     .first())
        if duplicate:
            return jsonify({'error': '同项目下已存在同名功能全景'}), 409
        workspace.title = title
    if 'description' in data:
        workspace.description = data.get('description')
    workspace.updated_by = _operator_info()
    db.session.commit()
    return jsonify(workspace.to_dict())


@api_bp.route('/panorama/workspaces/<int:workspace_id>', methods=['DELETE'])
def delete_panorama_workspace(workspace_id):
    workspace = PanoramaWorkspace.query.get_or_404(workspace_id)
    if workspace.is_default:
        return jsonify({'error': '默认功能全景不允许删除'}), 400
    module_ids = [m.id for m in GameModulePanorama.query.filter_by(workspace_id=workspace.id).all()]
    if module_ids:
        GameModuleChangeLog.query.filter(GameModuleChangeLog.module_id.in_(module_ids)).delete(synchronize_session=False)
        PanoramaModuleCodeLink.query.filter(PanoramaModuleCodeLink.module_id.in_(module_ids)).delete(synchronize_session=False)
    GameModuleRelation.query.filter_by(workspace_id=workspace.id).delete(synchronize_session=False)
    GameModulePanorama.query.filter_by(workspace_id=workspace.id).delete(synchronize_session=False)
    PanoramaCodeEntity.query.filter_by(workspace_id=workspace.id).delete(synchronize_session=False)
    PanoramaImpactAnalysis.query.filter_by(workspace_id=workspace.id).delete(synchronize_session=False)
    snapshots = PanoramaSnapshot.query.filter_by(workspace_id=workspace.id).all()
    for snapshot in snapshots:
        db.session.delete(snapshot)
    db.session.delete(workspace)
    db.session.commit()
    return jsonify({'ok': True, 'deleted_id': workspace_id})


# ===== 模块节点 CRUD =====

@api_bp.route('/panorama/modules', methods=['GET'])
def list_panorama_modules():
    """获取功能模块列表（支持树形和平铺）"""
    project_id = request.args.get('project_id', type=int)
    workspace = _resolve_workspace_from_request(create=project_id is not None)
    tree = request.args.get('tree', '0') == '1'
    status = request.args.get('status')

    q = GameModulePanorama.query
    if workspace:
        q = q.filter_by(workspace_id=workspace.id)
    elif project_id:
        q = q.filter_by(project_id=project_id)
    if status:
        q = q.filter_by(status=status)

    modules = q.order_by(GameModulePanorama.path, GameModulePanorama.name).all()

    if tree:
        # 构建树形结构
        id_map = {m.id: m.to_dict() for m in modules}
        roots = []
        for m in modules:
            d = id_map[m.id]
            d['children'] = []
        for m in modules:
            d = id_map[m.id]
            if m.parent_id and m.parent_id in id_map:
                id_map[m.parent_id]['children'].append(d)
            else:
                roots.append(d)
        return jsonify(roots)
    else:
        return jsonify([m.to_dict() for m in modules])


@api_bp.route('/panorama/modules', methods=['POST'])
def create_panorama_module():
    """创建功能模块节点"""
    data = request.get_json(force=True)
    workspace = _resolve_workspace_from_request(data, create=True)
    project_id = data.get('project_id')
    if project_id is None and workspace:
        project_id = workspace.project_id
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': '模块名称不能为空'}), 400

    parent_id = data.get('parent_id')
    # 自动计算 path
    path = name
    if parent_id:
        parent = GameModulePanorama.query.get(parent_id)
        if parent and parent.path:
            path = f"{parent.path}/{name}"

    module = GameModulePanorama(
        project_id=project_id,
        workspace_id=workspace.id if workspace else None,
        parent_id=parent_id,
        name=name,
        path=path,
        description=data.get('description'),
        code_paths=data.get('code_paths'),
        resource_paths=data.get('resource_paths'),
        test_focus=data.get('test_focus'),
        related_case_libraries=data.get('related_case_libraries'),
        status=data.get('status', 'active'),
        risk_level=data.get('risk_level', 'normal'),
        is_new=bool(data.get('is_new', False)),
        highlight=bool(data.get('highlight', False)),
        extra=data.get('extra'),
        created_by=_operator_info(),
        updated_by=_operator_info(),
    )
    db.session.add(module)
    db.session.commit()
    return jsonify(module.to_dict()), 201


@api_bp.route('/panorama/modules/batch', methods=['POST'])
def batch_upsert_panorama_modules():
    """批量创建/更新模块节点（Agent 用）
    
    请求体: { "modules": [ {...}, ... ], "project_id": int|null }
    每个模块需有 name 字段，可选 parent_path（用路径匹配父节点）
    如果同 project_id + path 已存在则更新，否则创建。
    """
    data = request.get_json(force=True)
    items = data.get('modules', [])
    project_id = data.get('project_id')
    workspace = _resolve_workspace_from_request(data, create=True)
    if project_id is None and workspace:
        project_id = workspace.project_id
    operator = _operator_info()

    created = 0
    updated = 0
    results = []

    for item in items:
        name = (item.get('name') or '').strip()
        if not name:
            continue

        path = item.get('path') or name
        parent_id = item.get('parent_id')

        # 通过 parent_path 查找父节点
        parent_path = item.get('parent_path')
        if parent_path and not parent_id:
            parent_q = GameModulePanorama.query.filter_by(path=parent_path)
            if workspace:
                parent_q = parent_q.filter_by(workspace_id=workspace.id)
            else:
                parent_q = parent_q.filter_by(project_id=project_id)
            parent = parent_q.first()
            if parent:
                parent_id = parent.id
                path = f"{parent_path}/{name}"

        # 查找已存在的
        existing_q = GameModulePanorama.query.filter_by(path=path)
        if workspace:
            existing_q = existing_q.filter_by(workspace_id=workspace.id)
        else:
            existing_q = existing_q.filter_by(project_id=project_id)
        existing = existing_q.first()
        if existing:
            # 更新
            if item.get('description') is not None:
                existing.description = item['description']
            if item.get('code_paths') is not None:
                existing.code_paths = item['code_paths']
            if item.get('resource_paths') is not None:
                existing.resource_paths = item['resource_paths']
            if item.get('test_focus') is not None:
                existing.test_focus = item['test_focus']
            if item.get('related_case_libraries') is not None:
                existing.related_case_libraries = item['related_case_libraries']
            if item.get('status') is not None:
                existing.status = item['status']
            if item.get('risk_level') is not None:
                existing.risk_level = item['risk_level']
            if item.get('is_new') is not None:
                existing.is_new = bool(item['is_new'])
            if item.get('highlight') is not None:
                existing.highlight = bool(item['highlight'])
            if item.get('extra') is not None:
                existing.extra = item['extra']
            existing.updated_by = operator
            updated += 1
            results.append(existing.to_dict())
        else:
            # 创建
            module = GameModulePanorama(
                project_id=project_id,
                workspace_id=workspace.id if workspace else None,
                parent_id=parent_id,
                name=name,
                path=path,
                description=item.get('description'),
                code_paths=item.get('code_paths'),
                resource_paths=item.get('resource_paths'),
                test_focus=item.get('test_focus'),
                related_case_libraries=item.get('related_case_libraries'),
                status=item.get('status', 'active'),
                risk_level=item.get('risk_level', 'normal'),
                is_new=bool(item.get('is_new', False)),
                highlight=bool(item.get('highlight', False)),
                extra=item.get('extra'),
                created_by=operator,
                updated_by=operator,
            )
            db.session.add(module)
            db.session.flush()
            results.append(module.to_dict())
            created += 1

    db.session.commit()
    return jsonify({'created': created, 'updated': updated, 'modules': results}), 200


@api_bp.route('/panorama/modules/<int:module_id>', methods=['GET'])
def get_panorama_module(module_id):
    """获取单个模块详情（含子节点和最近变更）"""
    module = GameModulePanorama.query.get_or_404(module_id)
    d = module.to_dict()
    # 子节点
    children = GameModulePanorama.query.filter_by(parent_id=module_id).order_by(GameModulePanorama.name).all()
    d['children'] = [c.to_dict() for c in children]
    # 最近变更（最多100条）
    recent_changes = GameModuleChangeLog.query.filter_by(module_id=module_id)\
        .order_by(desc(GameModuleChangeLog.created_at)).limit(100).all()
    d['recent_changes'] = [c.to_dict() for c in recent_changes]
    links = (TestCasePanoramaLink.query
             .filter_by(module_id=module_id)
             .order_by(TestCasePanoramaLink.library_id, TestCasePanoramaLink.module_path)
             .all())
    d['testcase_links'] = [link.to_dict() for link in links]
    metric = PanoramaModuleTestMetric.query.filter_by(module_id=module_id).first()
    d['test_metrics'] = metric.to_dict() if metric else None
    return jsonify(d)


@api_bp.route('/panorama/modules/<int:module_id>', methods=['PUT'])
def update_panorama_module(module_id):
    """更新模块节点"""
    module = GameModulePanorama.query.get_or_404(module_id)
    data = request.get_json(force=True)

    updatable = ['name', 'description', 'code_paths', 'resource_paths', 'test_focus',
                 'related_case_libraries', 'status', 'risk_level', 'is_new', 'highlight',
                 'extra', 'parent_id', 'path']
    for key in updatable:
        if key in data:
            setattr(module, key, data[key])

    module.updated_by = _operator_info()
    db.session.commit()
    return jsonify(module.to_dict())


@api_bp.route('/panorama/modules/clear-highlights', methods=['POST'])
def clear_panorama_highlights():
    """清空所有模块的 highlight 标记。

    Agent 在新一轮变更分析开始前调用：先清空全局 highlight，
    然后通过 batch upsert 把本轮涉及的模块标 highlight=true。

    可选参数：
    - project_id：仅清空指定项目下模块的 highlight
    """
    project_id = request.args.get('project_id', type=int)
    workspace = _resolve_workspace_from_request(create=False)
    q = GameModulePanorama.query.filter_by(highlight=True)
    if workspace:
        q = q.filter_by(workspace_id=workspace.id)
    elif project_id is not None:
        q = q.filter_by(project_id=project_id)
    count = q.update({'highlight': False}, synchronize_session=False)
    db.session.commit()
    return jsonify({'cleared': count})


# ===== 模块关系边 =====

@api_bp.route('/panorama/relations', methods=['GET'])
def list_panorama_relations():
    """获取功能模块关系边列表。"""
    project_id = request.args.get('project_id', type=int)
    workspace = _resolve_workspace_from_request(create=False)
    module_id = request.args.get('module_id', type=int)
    relation_type = request.args.get('relation_type')

    q = GameModuleRelation.query
    if workspace:
        q = q.filter(GameModuleRelation.workspace_id == workspace.id)
    elif project_id is not None:
        q = q.filter(GameModuleRelation.project_id == project_id)
    if module_id is not None:
        q = q.filter(or_(
            GameModuleRelation.source_module_id == module_id,
            GameModuleRelation.target_module_id == module_id,
        ))
    if relation_type:
        q = q.filter(GameModuleRelation.relation_type == relation_type)

    items = q.order_by(GameModuleRelation.updated_at.desc(),
                       GameModuleRelation.id.desc()).all()
    return jsonify([_relation_payload(r) for r in items])


@api_bp.route('/panorama/relations', methods=['POST'])
def create_panorama_relation():
    """创建或更新一条功能模块关系边。"""
    data = request.get_json(force=True) or {}
    source, target, relation_type, error = _validate_relation_payload(data)
    if error:
        return jsonify({'error': error}), 400

    relation = (GameModuleRelation.query
                .filter_by(project_id=source.project_id,
                           workspace_id=source.workspace_id,
                           source_module_id=source.id,
                           target_module_id=target.id,
                           relation_type=relation_type)
                .first())
    operator = _operator_info()
    if not relation:
        relation = GameModuleRelation(
            project_id=source.project_id,
            workspace_id=source.workspace_id,
            source_module_id=source.id,
            target_module_id=target.id,
            relation_type=relation_type,
            created_by=operator,
        )
        db.session.add(relation)

    relation.confidence = float(data.get('confidence', relation.confidence or 1.0))
    relation.evidence = data.get('evidence') or relation.evidence or {}
    relation.source = data.get('source') or relation.source or 'manual'
    relation.updated_by = operator
    db.session.commit()
    return jsonify(_relation_payload(relation)), 201


@api_bp.route('/panorama/relations/batch', methods=['POST'])
def batch_upsert_panorama_relations():
    """批量创建/更新功能模块关系边。"""
    data = request.get_json(force=True) or {}
    items = data.get('relations') or []
    created = 0
    updated = 0
    errors = []
    results = []

    for idx, item in enumerate(items, start=1):
        source, target, relation_type, error = _validate_relation_payload(item)
        if error:
            errors.append({'index': idx, 'error': error})
            continue
        relation = (GameModuleRelation.query
                    .filter_by(project_id=source.project_id,
                               workspace_id=source.workspace_id,
                               source_module_id=source.id,
                               target_module_id=target.id,
                               relation_type=relation_type)
                    .first())
        operator = _operator_info()
        if relation:
            updated += 1
        else:
            relation = GameModuleRelation(
                project_id=source.project_id,
                workspace_id=source.workspace_id,
                source_module_id=source.id,
                target_module_id=target.id,
                relation_type=relation_type,
                created_by=operator,
            )
            db.session.add(relation)
            created += 1
        relation.confidence = float(item.get('confidence', relation.confidence or 1.0))
        relation.evidence = item.get('evidence') or relation.evidence or {}
        relation.source = item.get('source') or relation.source or 'agent'
        relation.updated_by = operator
        results.append(relation)

    db.session.commit()
    return jsonify({
        'created': created,
        'updated': updated,
        'errors': errors,
        'relations': [_relation_payload(r) for r in results],
    })


@api_bp.route('/panorama/relations/<int:relation_id>', methods=['PUT'])
def update_panorama_relation(relation_id):
    """更新一条功能模块关系边。"""
    relation = GameModuleRelation.query.get_or_404(relation_id)
    data = request.get_json(force=True) or {}

    if any(k in data for k in ('source_module_id', 'target_module_id', 'relation_type')):
        source, target, relation_type, error = _validate_relation_payload({
            'source_module_id': data.get('source_module_id', relation.source_module_id),
            'target_module_id': data.get('target_module_id', relation.target_module_id),
            'relation_type': data.get('relation_type', relation.relation_type),
        })
        if error:
            return jsonify({'error': error}), 400
        relation.project_id = source.project_id
        relation.workspace_id = source.workspace_id
        relation.source_module_id = source.id
        relation.target_module_id = target.id
        relation.relation_type = relation_type

    if 'confidence' in data:
        relation.confidence = float(data['confidence'])
    if 'evidence' in data:
        relation.evidence = data.get('evidence') or {}
    if 'source' in data:
        relation.source = data.get('source') or 'manual'
    relation.updated_by = _operator_info()
    db.session.commit()
    return jsonify(_relation_payload(relation))


@api_bp.route('/panorama/relations/<int:relation_id>', methods=['DELETE'])
def delete_panorama_relation(relation_id):
    """删除一条功能模块关系边。"""
    relation = GameModuleRelation.query.get_or_404(relation_id)
    db.session.delete(relation)
    db.session.commit()
    return jsonify({'ok': True, 'deleted_id': relation_id})


@api_bp.route('/panorama/graph', methods=['GET'])
def panorama_graph():
    """统一拓扑图数据：nodes + edges + metrics。"""
    project_id = request.args.get('project_id', type=int)
    workspace = _resolve_workspace_from_request(create=project_id is not None)
    status = request.args.get('status')

    mq = GameModulePanorama.query
    if workspace:
        mq = mq.filter(GameModulePanorama.workspace_id == workspace.id)
    elif project_id is not None:
        mq = mq.filter(GameModulePanorama.project_id == project_id)
    if status:
        mq = mq.filter(GameModulePanorama.status == status)
    modules = mq.order_by(GameModulePanorama.path, GameModulePanorama.name).all()

    rq = GameModuleRelation.query
    if workspace:
        rq = rq.filter(GameModuleRelation.workspace_id == workspace.id)
    elif project_id is not None:
        rq = rq.filter(GameModuleRelation.project_id == project_id)
    relations = rq.all()
    return jsonify(build_graph_payload(
        [m.to_dict() for m in modules],
        [r.to_dict() for r in relations],
    ))


# ===== 代码实体与模块代码关联 =====

@api_bp.route('/panorama/code-entities', methods=['GET'])
def list_panorama_code_entities():
    """查询轻量代码实体索引。"""
    project_id = request.args.get('project_id', type=int)
    workspace = _resolve_workspace_from_request(create=False)
    file_path = (request.args.get('file_path') or '').strip()
    symbol = (request.args.get('symbol') or '').strip()
    repo_key = (request.args.get('repo_key') or '').strip()
    limit = min(request.args.get('limit', 200, type=int), 1000)

    q = PanoramaCodeEntity.query
    if workspace:
        q = q.filter(PanoramaCodeEntity.workspace_id == workspace.id)
    elif project_id is not None:
        q = q.filter(PanoramaCodeEntity.project_id == project_id)
    if repo_key:
        q = q.filter(PanoramaCodeEntity.repo_key == repo_key)
    if file_path:
        q = q.filter(PanoramaCodeEntity.file_path.like(f'%{file_path}%'))
    if symbol:
        q = q.filter(PanoramaCodeEntity.symbol_name.like(f'%{symbol}%'))

    items = q.order_by(PanoramaCodeEntity.file_path, PanoramaCodeEntity.start_line).limit(limit).all()
    return jsonify([e.to_dict() for e in items])


@api_bp.route('/panorama/code-entities/batch', methods=['POST'])
def batch_upsert_panorama_code_entities():
    """批量创建/更新代码实体索引。"""
    data = request.get_json(force=True) or {}
    items = data.get('entities') or []
    default_project_id = data.get('project_id')
    workspace = _resolve_workspace_from_request(data, create=True)
    if default_project_id is None and workspace:
        default_project_id = workspace.project_id
    default_repo_key = data.get('repo_key') or 'default'
    created = 0
    updated = 0
    errors = []
    results = []

    for idx, item in enumerate(items, start=1):
        file_path = (item.get('file_path') or '').replace('\\', '/').strip()
        if not file_path:
            errors.append({'index': idx, 'error': 'file_path 必填'})
            continue
        project_id = item.get('project_id', default_project_id)
        repo_key = item.get('repo_key') or default_repo_key
        entity_type = item.get('entity_type') or 'file'
        symbol_name = item.get('symbol_name') or ''
        start_line = int(item.get('start_line') or 0)

        entity_q = PanoramaCodeEntity.query.filter_by(
            repo_key=repo_key,
            file_path=file_path,
            entity_type=entity_type,
            symbol_name=symbol_name,
            start_line=start_line,
        )
        if workspace:
            entity_q = entity_q.filter_by(workspace_id=workspace.id)
        else:
            entity_q = entity_q.filter_by(project_id=project_id)
        entity = entity_q.first()
        if entity:
            updated += 1
        else:
            entity = PanoramaCodeEntity(
                project_id=project_id,
                workspace_id=workspace.id if workspace else None,
                repo_key=repo_key,
                file_path=file_path,
                entity_type=entity_type,
                symbol_name=symbol_name,
                start_line=start_line,
            )
            db.session.add(entity)
            created += 1
        entity.language = item.get('language') or entity.language or ''
        entity.end_line = int(item.get('end_line') or entity.end_line or 0)
        entity.content_hash = item.get('content_hash') or entity.content_hash or ''
        entity.extra = item.get('extra') or entity.extra or {}
        results.append(entity)

    db.session.commit()
    return jsonify({
        'created': created,
        'updated': updated,
        'errors': errors,
        'entities': [e.to_dict() for e in results],
    })


@api_bp.route('/panorama/module-code-links/batch', methods=['POST'])
def batch_upsert_panorama_module_code_links():
    """批量创建/更新功能模块与代码实体关联。"""
    data = request.get_json(force=True) or {}
    items = data.get('links') or []
    created = 0
    updated = 0
    errors = []
    results = []

    for idx, item in enumerate(items, start=1):
        module_id = item.get('module_id')
        entity_id = item.get('entity_id')
        link_type = item.get('link_type') or 'owns'
        if not module_id or not entity_id:
            errors.append({'index': idx, 'error': 'module_id 和 entity_id 必填'})
            continue
        module = GameModulePanorama.query.get(module_id)
        entity = PanoramaCodeEntity.query.get(entity_id)
        if not module or not entity:
            errors.append({'index': idx, 'error': '模块或代码实体不存在'})
            continue
        if module.project_id != entity.project_id:
            errors.append({'index': idx, 'error': '模块和代码实体必须属于同一项目'})
            continue
        if module.workspace_id != entity.workspace_id:
            errors.append({'index': idx, 'error': '模块和代码实体必须属于同一功能全景'})
            continue

        link = PanoramaModuleCodeLink.query.filter_by(
            module_id=module.id,
            entity_id=entity.id,
            link_type=link_type,
        ).first()
        if link:
            updated += 1
        else:
            link = PanoramaModuleCodeLink(
                project_id=module.project_id,
                workspace_id=module.workspace_id,
                module_id=module.id,
                entity_id=entity.id,
                link_type=link_type,
            )
            db.session.add(link)
            created += 1
        link.workspace_id = module.workspace_id
        link.project_id = module.project_id
        link.confidence = float(item.get('confidence', link.confidence or 1.0))
        link.evidence = item.get('evidence') or link.evidence or {}
        link.source = item.get('source') or link.source or 'agent'
        results.append(link)

    db.session.commit()
    return jsonify({
        'created': created,
        'updated': updated,
        'errors': errors,
        'links': [l.to_dict() for l in results],
    })


@api_bp.route('/panorama/modules/<int:module_id>/code-context', methods=['GET'])
def get_module_code_context(module_id):
    """获取模块关联代码上下文。"""
    module = GameModulePanorama.query.get_or_404(module_id)
    links = (PanoramaModuleCodeLink.query
             .filter_by(module_id=module_id)
             .order_by(PanoramaModuleCodeLink.link_type, PanoramaModuleCodeLink.id)
             .all())
    return jsonify({
        'module': module.to_dict(),
        'links': [l.to_dict() for l in links],
        'code_entities': [l.entity.to_dict() for l in links if l.entity],
    })


# ===== 影响分析 =====

@api_bp.route('/panorama/impact/analyze', methods=['POST'])
def create_panorama_impact_analysis():
    """基于 changed_files 做一次功能影响分析并保存结果。"""
    data = request.get_json(force=True) or {}
    project_id = data.get('project_id')
    workspace = _resolve_workspace_from_request(data, create=True)
    if project_id is None and workspace:
        project_id = workspace.project_id
    changed_files = data.get('changed_files') or []
    commit_range = data.get('commit_range')
    if not changed_files and not commit_range:
        return jsonify({'error': 'changed_files 或 commit_range 必须提供'}), 400

    mq = GameModulePanorama.query
    eq = PanoramaCodeEntity.query
    rq = GameModuleRelation.query
    if workspace:
        mq = mq.filter(GameModulePanorama.workspace_id == workspace.id)
        eq = eq.filter(PanoramaCodeEntity.workspace_id == workspace.id)
        rq = rq.filter(GameModuleRelation.workspace_id == workspace.id)
    elif project_id is not None:
        mq = mq.filter(GameModulePanorama.project_id == project_id)
        eq = eq.filter(PanoramaCodeEntity.project_id == project_id)
        rq = rq.filter(GameModuleRelation.project_id == project_id)
    modules = mq.all()
    entities = eq.all()
    module_ids = [m.id for m in modules]
    entity_ids = [e.id for e in entities]
    lq = PanoramaModuleCodeLink.query
    if module_ids:
        lq = lq.filter(PanoramaModuleCodeLink.module_id.in_(module_ids))
    if entity_ids:
        lq = lq.filter(PanoramaModuleCodeLink.entity_id.in_(entity_ids))
    links = lq.all() if module_ids and entity_ids else []
    relations = rq.all()

    result = analyze_changed_files(
        changed_files,
        modules=[m.to_dict() for m in modules],
        code_entities=[e.to_dict() for e in entities],
        module_code_links=[l.to_dict() for l in links],
        relations=[r.to_dict() for r in relations],
    )
    analysis = PanoramaImpactAnalysis(
        project_id=project_id,
        workspace_id=workspace.id if workspace else None,
        input_type='commit_range' if commit_range and not changed_files else 'changed_files',
        input_payload={'changed_files': changed_files, 'commit_range': commit_range},
        affected_modules=result.get('affected_modules'),
        affected_relations=result.get('affected_relations'),
        recommended_case_libraries=result.get('recommended_case_libraries'),
        risk_score=result.get('risk_score') or 0,
        risk_level=result.get('risk_level') or 'low',
        test_context=result.get('test_context'),
        token_savings=result.get('token_savings'),
        summary=result.get('summary'),
        created_by=_operator_info(),
    )
    db.session.add(analysis)
    for item in result.get('affected_modules') or []:
        module = GameModulePanorama.query.get(item.get('module_id'))
        if module:
            module.highlight = True
            module.updated_by = _operator_info()
    db.session.commit()
    return jsonify(analysis.to_dict()), 201


@api_bp.route('/panorama/impact/<int:analysis_id>', methods=['GET'])
def get_panorama_impact_analysis(analysis_id):
    """获取一次影响分析详情。"""
    analysis = PanoramaImpactAnalysis.query.get_or_404(analysis_id)
    return jsonify(analysis.to_dict())


@api_bp.route('/panorama/impact/recent', methods=['GET'])
def list_recent_panorama_impact_analyses():
    """获取最近影响分析记录。"""
    project_id = request.args.get('project_id', type=int)
    workspace = _resolve_workspace_from_request(create=False)
    limit = min(request.args.get('limit', 20, type=int), 100)
    q = PanoramaImpactAnalysis.query
    if workspace:
        q = q.filter(PanoramaImpactAnalysis.workspace_id == workspace.id)
    elif project_id is not None:
        q = q.filter(PanoramaImpactAnalysis.project_id == project_id)
    items = q.order_by(desc(PanoramaImpactAnalysis.created_at)).limit(limit).all()
    return jsonify([a.to_dict() for a in items])


# ===== 快照与 diff =====

@api_bp.route('/panorama/snapshots', methods=['GET'])
def list_panorama_snapshots():
    """获取功能全景快照列表。"""
    project_id = request.args.get('project_id', type=int)
    workspace = _resolve_workspace_from_request(create=project_id is not None)
    limit = min(request.args.get('limit', 20, type=int), 100)
    q = PanoramaSnapshot.query
    if workspace:
        q = q.filter(PanoramaSnapshot.workspace_id == workspace.id)
    elif project_id is not None:
        q = q.filter(PanoramaSnapshot.project_id == project_id)
    items = q.order_by(desc(PanoramaSnapshot.created_at)).limit(limit).all()
    return jsonify([s.to_dict() for s in items])


@api_bp.route('/panorama/snapshots', methods=['POST'])
def create_panorama_snapshot():
    """创建当前项目功能全景快照。"""
    data = request.get_json(force=True) or {}
    project_id = data.get('project_id')
    workspace = _resolve_workspace_from_request(data, create=True)
    if project_id is None and workspace:
        project_id = workspace.project_id
    name = (data.get('name') or '').strip()
    if not name:
        from datetime import datetime
        name = f"快照 {datetime.now().strftime('%Y-%m-%d %H:%M')}"

    modules, relations, entities, items = _collect_workspace_panorama_state(project_id, workspace)
    snapshot = PanoramaSnapshot(
        project_id=project_id,
        workspace_id=workspace.id if workspace else None,
        name=name,
        description=data.get('description'),
        module_count=len([i for i in items if i['item_type'] == 'module']),
        relation_count=len([i for i in items if i['item_type'] == 'relation']),
        code_entity_count=len(entities),
        created_by=_operator_info(),
    )
    db.session.add(snapshot)
    db.session.flush()
    for item in items:
        db.session.add(PanoramaSnapshotItem(
            snapshot_id=snapshot.id,
            item_type=item['item_type'],
            item_key=item['item_key'],
            payload=item.get('payload') or {},
        ))
    db.session.flush()
    recent = (PanoramaSnapshot.query
              .filter(PanoramaSnapshot.workspace_id == snapshot.workspace_id)
              .order_by(desc(PanoramaSnapshot.created_at), desc(PanoramaSnapshot.id))
              .all())
    prune_ids = snapshot_ids_to_prune([s.to_dict() for s in recent], keep=10)
    if prune_ids:
        PanoramaSnapshotItem.query.filter(PanoramaSnapshotItem.snapshot_id.in_(prune_ids)).delete(synchronize_session=False)
        PanoramaSnapshot.query.filter(PanoramaSnapshot.id.in_(prune_ids)).delete(synchronize_session=False)
    db.session.commit()
    return jsonify(snapshot.to_dict()), 201


@api_bp.route('/panorama/snapshots/<int:snapshot_id>', methods=['GET'])
def get_panorama_snapshot(snapshot_id):
    """获取快照详情。"""
    snapshot = PanoramaSnapshot.query.get_or_404(snapshot_id)
    include_items = request.args.get('include_items', '0') in ('1', 'true', 'yes')
    return jsonify(snapshot.to_dict(include_items=include_items))


@api_bp.route('/panorama/snapshots/diff', methods=['GET'])
def diff_panorama_snapshots():
    """对比两个功能全景快照。"""
    from_id = request.args.get('from', type=int)
    to_id = request.args.get('to', type=int)
    if not from_id or not to_id:
        return jsonify({'error': '需要 from 和 to 快照 ID'}), 400
    if from_id == to_id:
        return jsonify({'error': 'from 和 to 不能相同'}), 400

    snap_from = PanoramaSnapshot.query.get(from_id)
    snap_to = PanoramaSnapshot.query.get(to_id)
    if not snap_from or not snap_to:
        return jsonify({'error': '快照不存在'}), 404
    if snap_from.project_id != snap_to.project_id:
        return jsonify({'error': '两个快照必须属于同一项目'}), 400
    if snap_from.workspace_id != snap_to.workspace_id:
        return jsonify({'error': '两个快照必须属于同一功能全景'}), 400

    from_items = [i.to_dict() for i in snap_from.items.all()]
    to_items = [i.to_dict() for i in snap_to.items.all()]
    diff = diff_snapshot_items(from_items, to_items)
    diff['from_snapshot'] = snap_from.to_dict()
    diff['to_snapshot'] = snap_to.to_dict()
    return jsonify(diff)


@api_bp.route('/panorama/snapshots/<int:snapshot_id>/restore', methods=['POST'])
def restore_panorama_snapshot(snapshot_id):
    """将指定快照恢复为其所属功能全景的当前数据。"""
    snapshot = PanoramaSnapshot.query.get_or_404(snapshot_id)
    workspace = snapshot.workspace
    if not workspace:
        workspace = _resolve_workspace(project_id=snapshot.project_id, create=True)
        snapshot.workspace_id = workspace.id if workspace else None
        db.session.flush()
    items = [i.to_dict() for i in snapshot.items.order_by(PanoramaSnapshotItem.id).all()]
    plan = plan_restore_items(items)
    operator = _operator_info()

    old_module_ids = [
        m.id for m in GameModulePanorama.query.filter_by(workspace_id=workspace.id).all()
    ]
    if old_module_ids:
        GameModuleChangeLog.query.filter(GameModuleChangeLog.module_id.in_(old_module_ids)).delete(synchronize_session=False)
        PanoramaModuleCodeLink.query.filter(PanoramaModuleCodeLink.module_id.in_(old_module_ids)).delete(synchronize_session=False)
    GameModuleRelation.query.filter_by(workspace_id=workspace.id).delete(synchronize_session=False)
    GameModulePanorama.query.filter_by(workspace_id=workspace.id).delete(synchronize_session=False)
    PanoramaModuleCodeLink.query.filter_by(workspace_id=workspace.id).delete(synchronize_session=False)
    PanoramaCodeEntity.query.filter_by(workspace_id=workspace.id).delete(synchronize_session=False)
    db.session.flush()

    module_by_path = {}
    for item in plan['modules']:
        parent = module_by_path.get(item.get('parent_path'))
        module = GameModulePanorama(
            project_id=workspace.project_id,
            workspace_id=workspace.id,
            parent_id=parent.id if parent else None,
            name=item.get('name') or (item.get('path') or '').rsplit('/', 1)[-1],
            path=item.get('path'),
            description=item.get('description'),
            code_paths=item.get('code_paths') or [],
            resource_paths=item.get('resource_paths') or [],
            test_focus=item.get('test_focus'),
            related_case_libraries=item.get('related_case_libraries') or [],
            status=item.get('status') or 'active',
            risk_level=item.get('risk_level') or 'normal',
            extra=item.get('extra'),
            created_by=operator,
            updated_by=operator,
        )
        db.session.add(module)
        db.session.flush()
        module_by_path[module.path] = module

    restored_relations = 0
    for item in plan['relations']:
        source = module_by_path.get(item.get('source_path'))
        target = module_by_path.get(item.get('target_path'))
        if not source or not target:
            continue
        db.session.add(GameModuleRelation(
            project_id=workspace.project_id,
            workspace_id=workspace.id,
            source_module_id=source.id,
            target_module_id=target.id,
            relation_type=item.get('relation_type') or 'depends_on',
            confidence=float(item.get('confidence') if item.get('confidence') is not None else 1.0),
            evidence=item.get('evidence') or {},
            source='snapshot_restore',
            created_by=operator,
            updated_by=operator,
        ))
        restored_relations += 1

    entity_by_key = {}
    for item in plan.get('code_entities', []):
        entity = PanoramaCodeEntity(
            project_id=workspace.project_id,
            workspace_id=workspace.id,
            repo_key=item.get('repo_key') or 'default',
            file_path=item.get('file_path') or '',
            entity_type=item.get('entity_type') or 'file',
            symbol_name=item.get('symbol_name') or '',
            language=item.get('language') or '',
            start_line=int(item.get('start_line') or 0),
            end_line=int(item.get('end_line') or 0),
            content_hash=item.get('content_hash') or '',
            extra=item.get('extra') or {},
        )
        db.session.add(entity)
        db.session.flush()
        entity_by_key[item.get('entity_key')] = entity

    restored_code_links = 0
    for item in plan.get('code_links', []):
        module = module_by_path.get(item.get('module_path'))
        entity = entity_by_key.get(item.get('entity_key'))
        if not module or not entity:
            continue
        db.session.add(PanoramaModuleCodeLink(
            project_id=workspace.project_id,
            workspace_id=workspace.id,
            module_id=module.id,
            entity_id=entity.id,
            link_type=item.get('link_type') or 'owns',
            confidence=float(item.get('confidence') if item.get('confidence') is not None else 1.0),
            evidence=item.get('evidence') or {},
            source=item.get('source') or 'snapshot_restore',
        ))
        restored_code_links += 1

    workspace.updated_by = operator
    db.session.commit()
    return jsonify({
        'ok': True,
        'snapshot': snapshot.to_dict(),
        'restored_modules': len(module_by_path),
        'restored_relations': restored_relations,
        'restored_code_entities': len(entity_by_key),
        'restored_code_links': restored_code_links,
    })


@api_bp.route('/panorama/modules/<int:module_id>', methods=['DELETE'])
def delete_panorama_module(module_id):
    """删除模块节点（级联删除子节点和变更记录）"""
    module = GameModulePanorama.query.get_or_404(module_id)

    # 递归删除子节点
    def _delete_recursive(mid):
        children = GameModulePanorama.query.filter_by(parent_id=mid).all()
        for child in children:
            _delete_recursive(child.id)
        TestCasePanoramaLink.query.filter_by(module_id=mid).delete(synchronize_session=False)
        PanoramaModuleTestMetric.query.filter_by(module_id=mid).delete(synchronize_session=False)
        GameModuleRelation.query.filter(or_(
            GameModuleRelation.source_module_id == mid,
            GameModuleRelation.target_module_id == mid,
        )).delete(synchronize_session=False)
        PanoramaModuleCodeLink.query.filter_by(module_id=mid).delete(synchronize_session=False)
        GameModuleChangeLog.query.filter_by(module_id=mid).delete()
        GameModulePanorama.query.filter_by(id=mid).delete()

    _delete_recursive(module_id)
    db.session.commit()
    return jsonify({'ok': True})


# ===== 变更记录 =====

@api_bp.route('/panorama/modules/<int:module_id>/changes', methods=['GET'])
def get_module_changes(module_id):
    """获取某模块的变更记录（兼容 /modules/{id}/changes 路径）"""
    module = GameModulePanorama.query.get_or_404(module_id)
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    limit = min(request.args.get('limit', 20, type=int), 100)
    offset = request.args.get('offset', 0, type=int)

    q = GameModuleChangeLog.query.filter_by(module_id=module_id)
    if start_date:
        q = q.filter(GameModuleChangeLog.created_at >= start_date)
    if end_date:
        q = q.filter(GameModuleChangeLog.created_at < end_date + ' 23:59:59')

    total = q.count()
    changes = q.order_by(desc(GameModuleChangeLog.created_at)).offset(offset).limit(limit).all()
    return jsonify({'total': total, 'items': [c.to_dict() for c in changes], 'limit': limit, 'offset': offset})


@api_bp.route('/panorama/modules/<int:module_id>/changes', methods=['POST'])
def create_module_change(module_id):
    """为指定模块记录变更（兼容路径）"""
    module = GameModulePanorama.query.get_or_404(module_id)
    data = request.get_json(force=True)

    change = GameModuleChangeLog(
        module_id=module_id,
        change_type=data.get('change_type', 'logic'),
        summary=data.get('summary', ''),
        detail=data.get('detail'),
        affected_cases=data.get('affected_cases'),
        test_suggestion=data.get('test_suggestion'),
        risk_level=data.get('risk_level', 'normal'),
        source=data.get('source', 'agent'),
        created_by=_operator_info(),
    )
    db.session.add(change)

    module.last_change_summary = change.summary
    module.last_changed_at = _now()
    if data.get('risk_level'):
        module.risk_level = data['risk_level']
    module.updated_by = _operator_info()

    db.session.commit()
    return jsonify(change.to_dict()), 201


@api_bp.route('/panorama/modules/<int:module_id>/changes/batch', methods=['POST'])
def batch_create_module_changes(module_id):
    """为指定模块批量记录变更（兼容路径）"""
    module = GameModulePanorama.query.get_or_404(module_id)
    data = request.get_json(force=True)
    items = data.get('changes', [])
    operator = _operator_info()
    created = 0

    for item in items:
        change = GameModuleChangeLog(
            module_id=module_id,
            change_type=item.get('change_type', 'logic'),
            summary=item.get('summary', ''),
            detail=item.get('detail'),
            affected_cases=item.get('affected_cases'),
            test_suggestion=item.get('test_suggestion'),
            risk_level=item.get('risk_level', 'normal'),
            source=item.get('source', 'agent'),
            created_by=operator,
        )
        db.session.add(change)
        created += 1

    if items:
        last = items[-1]
        module.last_change_summary = last.get('summary', '')
        module.last_changed_at = _now()
        module.updated_by = operator

    db.session.commit()
    return jsonify({'created': created}), 200


@api_bp.route('/panorama/changes', methods=['GET'])
def list_panorama_changes():
    """查询变更记录（支持按模块、类型、时间范围筛选）

    特殊参数：
    - include_descendants=1 配合 module_id 时，会把所有子孙模块的变更也一并返回（按时间倒序）
    """
    module_id = request.args.get('module_id', type=int)
    include_descendants = request.args.get('include_descendants', '0') in ('1', 'true', 'yes')
    change_type = request.args.get('change_type')
    risk_level = request.args.get('risk_level')
    project_id = request.args.get('project_id', type=int)
    workspace = _resolve_workspace_from_request(create=False)
    start_date = request.args.get('start_date')  # YYYY-MM-DD
    end_date = request.args.get('end_date')      # YYYY-MM-DD
    limit = min(request.args.get('limit', 20, type=int), 100)
    offset = request.args.get('offset', 0, type=int)

    q = GameModuleChangeLog.query
    if module_id:
        if include_descendants:
            # 收集 module_id 自身 + 所有后代模块 ID
            module_ids = {module_id}
            frontier = [module_id]
            while frontier:
                kids = GameModulePanorama.query.filter(
                    GameModulePanorama.parent_id.in_(frontier)
                ).with_entities(GameModulePanorama.id).all()
                next_frontier = [r[0] for r in kids if r[0] not in module_ids]
                module_ids.update(next_frontier)
                frontier = next_frontier
            q = q.filter(GameModuleChangeLog.module_id.in_(list(module_ids)))
        else:
            q = q.filter_by(module_id=module_id)
    if change_type:
        q = q.filter_by(change_type=change_type)
    if risk_level:
        q = q.filter_by(risk_level=risk_level)
    if workspace:
        q = q.join(GameModulePanorama).filter(GameModulePanorama.workspace_id == workspace.id)
    elif project_id:
        q = q.join(GameModulePanorama).filter(GameModulePanorama.project_id == project_id)
    if start_date:
        q = q.filter(GameModuleChangeLog.created_at >= start_date)
    if end_date:
        q = q.filter(GameModuleChangeLog.created_at < end_date + ' 23:59:59')

    total = q.count()
    changes = q.order_by(desc(GameModuleChangeLog.created_at)).offset(offset).limit(limit).all()
    return jsonify({'total': total, 'items': [c.to_dict() for c in changes], 'limit': limit, 'offset': offset})


@api_bp.route('/panorama/changes', methods=['POST'])
def create_panorama_change():
    """记录一次模块变更"""
    data = request.get_json(force=True)
    workspace = _resolve_workspace_from_request(data, create=True)
    module_id = data.get('module_id')
    if not module_id:
        # 通过 module_path 查找
        module_path = data.get('module_path')
        project_id = data.get('project_id')
        if module_path:
            module_q = GameModulePanorama.query.filter_by(path=module_path)
            if workspace:
                module_q = module_q.filter_by(workspace_id=workspace.id)
            else:
                module_q = module_q.filter_by(project_id=project_id)
            module = module_q.first()
            if module:
                module_id = module.id
    if not module_id:
        return jsonify({'error': 'module_id 或 module_path 必须提供'}), 400

    module = GameModulePanorama.query.get(module_id)
    if not module:
        return jsonify({'error': '模块不存在'}), 404

    change = GameModuleChangeLog(
        module_id=module_id,
        change_type=data.get('change_type', 'logic'),
        summary=data.get('summary', ''),
        detail=data.get('detail'),
        affected_cases=data.get('affected_cases'),
        test_suggestion=data.get('test_suggestion'),
        risk_level=data.get('risk_level', 'normal'),
        source=data.get('source', 'agent'),
        created_by=_operator_info(),
    )
    db.session.add(change)

    # 更新模块的最近变更信息
    module.last_change_summary = change.summary
    module.last_changed_at = _now()
    if data.get('risk_level'):
        module.risk_level = data['risk_level']
    module.updated_by = _operator_info()

    db.session.commit()
    return jsonify(change.to_dict()), 201


@api_bp.route('/panorama/changes/<int:change_id>', methods=['GET'])
def get_panorama_change(change_id):
    """获取单条变更记录详情"""
    change = GameModuleChangeLog.query.get_or_404(change_id)
    return jsonify(change.to_dict())


@api_bp.route('/panorama/changes/<int:change_id>', methods=['PUT'])
def update_panorama_change(change_id):
    """更新一条变更记录（用于修正历史数据）

    可更新字段：change_type / summary / detail / affected_cases /
    test_suggestion / risk_level / source / module_id（迁移到其他模块）
    """
    change = GameModuleChangeLog.query.get_or_404(change_id)
    data = request.get_json(force=True) or {}

    # 允许迁移到其他模块
    new_module_id = data.get('module_id')
    if new_module_id is not None and new_module_id != change.module_id:
        target = GameModulePanorama.query.get(new_module_id)
        if not target:
            return jsonify({'error': f'目标模块 {new_module_id} 不存在'}), 400
        change.module_id = new_module_id

    for field in ('change_type', 'summary', 'detail', 'affected_cases',
                  'test_suggestion', 'risk_level', 'source'):
        if field in data:
            setattr(change, field, data[field])

    db.session.commit()
    return jsonify(change.to_dict())


@api_bp.route('/panorama/changes/<int:change_id>', methods=['DELETE'])
def delete_panorama_change(change_id):
    """删除一条变更记录（用于清理错误历史数据）"""
    change = GameModuleChangeLog.query.get_or_404(change_id)
    db.session.delete(change)
    db.session.commit()
    return jsonify({'ok': True, 'deleted_id': change_id})


@api_bp.route('/panorama/changes/batch', methods=['POST'])
def batch_create_panorama_changes():
    """批量记录变更（Agent 用）
    
    请求体: { "changes": [ {...}, ... ] }
    """
    data = request.get_json(force=True)
    items = data.get('changes', [])
    default_workspace = _resolve_workspace_from_request(data, create=True)
    operator = _operator_info()
    created = 0
    errors = []

    for i, item in enumerate(items):
        module_id = item.get('module_id')
        if not module_id:
            module_path = item.get('module_path')
            project_id = item.get('project_id')
            workspace = _resolve_workspace(
                project_id=project_id if project_id is not None else data.get('project_id'),
                workspace_id=item.get('workspace_id') or data.get('workspace_id'),
                title=item.get('panorama_title') or item.get('workspace_title') or data.get('panorama_title') or data.get('workspace_title'),
                create=False,
            ) or default_workspace
            if module_path:
                module_q = GameModulePanorama.query.filter_by(path=module_path)
                if workspace:
                    module_q = module_q.filter_by(workspace_id=workspace.id)
                else:
                    module_q = module_q.filter_by(project_id=project_id)
                module = module_q.first()
                if module:
                    module_id = module.id
        if not module_id:
            errors.append(f"第{i+1}条: 模块未找到")
            continue

        module = GameModulePanorama.query.get(module_id)
        if not module:
            errors.append(f"第{i+1}条: module_id={module_id} 不存在")
            continue

        change = GameModuleChangeLog(
            module_id=module_id,
            change_type=item.get('change_type', 'logic'),
            summary=item.get('summary', ''),
            detail=item.get('detail'),
            affected_cases=item.get('affected_cases'),
            test_suggestion=item.get('test_suggestion'),
            risk_level=item.get('risk_level', 'normal'),
            source=item.get('source', 'agent'),
            created_by=operator,
        )
        db.session.add(change)

        module.last_change_summary = change.summary
        module.last_changed_at = _now()
        module.updated_by = operator
        created += 1

    db.session.commit()
    return jsonify({'created': created, 'errors': errors}), 200


# ===== 统计与汇总 =====

@api_bp.route('/panorama/stats', methods=['GET'])
def panorama_stats():
    """全景视图统计"""
    project_id = request.args.get('project_id', type=int)
    workspace = _resolve_workspace_from_request(create=project_id is not None)

    q = GameModulePanorama.query
    if workspace:
        q = q.filter_by(workspace_id=workspace.id)
    elif project_id:
        q = q.filter_by(project_id=project_id)

    total_modules = q.count()
    active_modules = q.filter_by(status='active').count()

    cq = GameModuleChangeLog.query
    if workspace:
        cq = cq.join(GameModulePanorama).filter(GameModulePanorama.workspace_id == workspace.id)
    elif project_id:
        cq = cq.join(GameModulePanorama).filter(GameModulePanorama.project_id == project_id)
    total_changes = cq.count()

    # 按风险等级统计
    risk_stats = {}
    for level in ['low', 'normal', 'high', 'critical']:
        risk_stats[level] = q.filter_by(risk_level=level).count()

    # 最近7天变更数
    from datetime import timedelta
    week_ago = _now() - timedelta(days=7)
    recent_changes = cq.filter(GameModuleChangeLog.created_at >= week_ago).count()

    rq = GameModuleRelation.query
    if workspace:
        rq = rq.filter_by(workspace_id=workspace.id)
    elif project_id:
        rq = rq.filter_by(project_id=project_id)
    total_relations = rq.count()
    eq = PanoramaCodeEntity.query
    lq = PanoramaModuleCodeLink.query
    aq = PanoramaImpactAnalysis.query
    sq = PanoramaSnapshot.query
    if workspace:
        eq = eq.filter_by(workspace_id=workspace.id)
        lq = lq.filter_by(workspace_id=workspace.id)
        aq = aq.filter_by(workspace_id=workspace.id)
        sq = sq.filter_by(workspace_id=workspace.id)
    elif project_id:
        eq = eq.filter_by(project_id=project_id)
        lq = lq.filter_by(project_id=project_id)
        aq = aq.filter_by(project_id=project_id)
        sq = sq.filter_by(project_id=project_id)
    total_code_entities = eq.count()
    total_module_code_links = lq.count()
    recent_impacts = aq.order_by(desc(PanoramaImpactAnalysis.created_at)).limit(5).all()
    recent_snapshots = sq.order_by(desc(PanoramaSnapshot.created_at)).limit(5).all()

    modules = q.all()
    relations = rq.all()
    graph = build_graph_payload(
        [m.to_dict() for m in modules],
        [r.to_dict() for r in relations],
    )
    graph_metrics = graph.get('metrics', {})

    return jsonify({
        'total_modules': total_modules,
        'active_modules': active_modules,
        'total_changes': total_changes,
        'total_relations': total_relations,
        'total_code_entities': total_code_entities,
        'total_module_code_links': total_module_code_links,
        'recent_impacts': [a.to_dict() for a in recent_impacts],
        'recent_snapshots': [s.to_dict() for s in recent_snapshots],
        'total_snapshots': sq.count(),
        'recent_changes_7d': recent_changes,
        'risk_stats': risk_stats,
        'graph_metrics': {
            'hub_modules': graph_metrics.get('hub_modules', [])[:5],
            'bridge_modules': graph_metrics.get('bridge_modules', [])[:5],
            'isolated_count': len(graph_metrics.get('isolated_modules', [])),
            'untested_high_risk_count': len(graph_metrics.get('untested_high_risk_modules', [])),
            'surprising_relation_count': len(graph_metrics.get('surprising_relations', [])),
        },
    })
