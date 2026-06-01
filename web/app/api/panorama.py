"""游戏功能模块全景视图 API（Game Module Panorama）

提供功能模块树的 CRUD、变更记录管理、树形查询等接口。
Agent 通过 Bearer Token 调用来建立和更新全景数据。
"""
from datetime import datetime

from flask import request, jsonify
from sqlalchemy import desc, or_

from app import db
from app.api import api_bp
from app.api.skills import _get_current_user
from app.models import (
    GameModuleChangeLog,
    GameModulePanorama,
    Project,
    TestCaseLibrary,
    _now,
)


def _operator_info():
    """获取操作者信息（用户名或 agent 名）"""
    user = _get_current_user()
    if user:
        return user.username if hasattr(user, 'username') else (user.get('username') or user.get('_claw_name') or 'unknown')
    return 'anonymous'


# ===== 模块节点 CRUD =====

@api_bp.route('/panorama/modules', methods=['GET'])
def list_panorama_modules():
    """获取功能模块列表（支持树形和平铺）"""
    project_id = request.args.get('project_id', type=int)
    tree = request.args.get('tree', '0') == '1'
    status = request.args.get('status')

    q = GameModulePanorama.query
    if project_id:
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
        project_id=data.get('project_id'),
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
            parent = GameModulePanorama.query.filter_by(path=parent_path, project_id=project_id).first()
            if parent:
                parent_id = parent.id
                path = f"{parent_path}/{name}"

        # 查找已存在的
        existing = GameModulePanorama.query.filter_by(path=path, project_id=project_id).first()
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
    q = GameModulePanorama.query.filter_by(highlight=True)
    if project_id is not None:
        q = q.filter_by(project_id=project_id)
    count = q.update({'highlight': False}, synchronize_session=False)
    db.session.commit()
    return jsonify({'cleared': count})


@api_bp.route('/panorama/modules/<int:module_id>', methods=['DELETE'])
def delete_panorama_module(module_id):
    """删除模块节点（级联删除子节点和变更记录）"""
    module = GameModulePanorama.query.get_or_404(module_id)

    # 递归删除子节点
    def _delete_recursive(mid):
        children = GameModulePanorama.query.filter_by(parent_id=mid).all()
        for child in children:
            _delete_recursive(child.id)
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
    if project_id:
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
    module_id = data.get('module_id')
    if not module_id:
        # 通过 module_path 查找
        module_path = data.get('module_path')
        project_id = data.get('project_id')
        if module_path:
            module = GameModulePanorama.query.filter_by(path=module_path, project_id=project_id).first()
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
    operator = _operator_info()
    created = 0
    errors = []

    for i, item in enumerate(items):
        module_id = item.get('module_id')
        if not module_id:
            module_path = item.get('module_path')
            project_id = item.get('project_id')
            if module_path:
                module = GameModulePanorama.query.filter_by(path=module_path, project_id=project_id).first()
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

    q = GameModulePanorama.query
    if project_id:
        q = q.filter_by(project_id=project_id)

    total_modules = q.count()
    active_modules = q.filter_by(status='active').count()

    cq = GameModuleChangeLog.query
    if project_id:
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

    return jsonify({
        'total_modules': total_modules,
        'active_modules': active_modules,
        'total_changes': total_changes,
        'recent_changes_7d': recent_changes,
        'risk_stats': risk_stats,
    })
