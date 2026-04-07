"""
测试用例库 API
支持用例库的 CRUD 和脑图结构管理
"""

import uuid
import json
from flask import request, jsonify
from app import db
from app.models import TestCaseLibrary, TestCase
from app.api import api_bp


def generate_mindmap_node_id():
    """生成唯一的脑图节点ID"""
    return f"node_{uuid.uuid4().hex[:8]}"


def build_mindmap_from_cases(cases):
    """
    从用例列表构建脑图结构

    结构：
    {
        "id": "root",
        "text": "用例库名称",
        "children": [
            {
                "id": "node_xxx",
                "text": "P0 - 用例标题",
                "data": {...}
            }
        ]
    }
    """
    # 按优先级分组
    groups = {'P0': [], 'P1': [], 'P2': [], 'P3': []}
    for case in cases:
        priority = case.priority or 'P2'
        if priority not in groups:
            groups[priority] = []
        groups[priority].append(case)

    children = []
    for priority in ['P0', 'P1', 'P2', 'P3']:
        if groups[priority]:
            # 按类型再分组
            by_type = {}
            for case in groups[priority]:
                case_type = case.type or 'functional'
                if case_type not in by_type:
                    by_type[case_type] = []
                by_type[case_type].append(case)

            for case_type, type_cases in by_type.items():
                type_node = {
                    'id': generate_mindmap_node_id(),
                    'text': f"{priority} - {case_type}",
                    'children': []
                }
                for case in type_cases:
                    type_node['children'].append({
                        'id': case.mindmap_node_id or generate_mindmap_node_id(),
                        'text': case.title,
                        'data': case.to_dict(),
                    })
                children.append(type_node)

    return {
        'id': 'root',
        'text': '用例库',
        'children': children,
    }


# ==================== 用例库 CRUD ====================

@api_bp.route('/testcase-libraries', methods=['GET'])
def list_testcase_libraries():
    """获取用例库列表"""
    project_name = request.args.get('project_name')
    search = request.args.get('search')

    query = TestCaseLibrary.query

    if project_name:
        query = query.filter_by(project_name=project_name)
    if search:
        query = query.filter(
            db.or_(
                TestCaseLibrary.name.ilike(f'%{search}%'),
                TestCaseLibrary.description.ilike(f'%{search}%')
            )
        )

    libraries = query.order_by(TestCaseLibrary.updated_at.desc()).all()
    return jsonify([lib.to_dict() for lib in libraries])


@api_bp.route('/testcase-libraries', methods=['POST'])
def create_testcase_library():
    """创建用例库"""
    data = request.get_json()

    if not data or not data.get('name'):
        return jsonify({'error': 'name 为必填项'}), 400

    library = TestCaseLibrary(
        name=data['name'],
        description=data.get('description', ''),
        project_name=data.get('project_name'),
        module_name=data.get('module_name'),
        owner=data.get('owner'),
        mindmap={'id': 'root', 'text': data['name'], 'children': []},
    )
    db.session.add(library)
    db.session.commit()

    return jsonify(library.to_dict()), 201


@api_bp.route('/testcase-libraries/by-project', methods=['POST'])
def get_or_create_library_by_project():
    """按项目获取用例库，不存在则自动创建（根节点）

    请求体：{ "project_name": "项目名" }
    返回：用例库详情（已有或新建的）
    """
    data = request.get_json()
    project_name = (data or {}).get('project_name')
    if not project_name:
        return jsonify({'error': 'project_name 必填'}), 400

    # 查找该项目的根用例库
    library = TestCaseLibrary.query.filter_by(
        project_name=project_name
    ).first()

    if library:
        return jsonify(library.to_dict(with_cases=True))

    # 不存在则自动创建
    library = TestCaseLibrary(
        name=f'{project_name} 用例库',
        description=f'{project_name} 项目用例库（自动创建）',
        project_name=project_name,
        owner=data.get('owner', 'system'),
        mindmap={'id': 'root', 'text': f'{project_name} 用例库', 'children': []},
    )
    db.session.add(library)
    db.session.commit()
    return jsonify(library.to_dict(with_cases=True)), 201


@api_bp.route('/testcase-libraries/<int:library_id>', methods=['GET'])
def get_testcase_library(library_id):
    """获取用例库详情"""
    library = TestCaseLibrary.query.get_or_404(library_id)
    return jsonify(library.to_dict(with_cases=True))


@api_bp.route('/testcase-libraries/<int:library_id>', methods=['PUT'])
def update_testcase_library(library_id):
    """更新用例库"""
    library = TestCaseLibrary.query.get_or_404(library_id)
    data = request.get_json()

    updatable_fields = ['name', 'description', 'project_name', 'module_name', 'owner', 'status']
    for field in updatable_fields:
        if field in data:
            setattr(library, field, data[field])

    # 更新脑图根节点
    if 'name' in data and library.mindmap:
        library.mindmap['text'] = data['name']

    db.session.commit()
    return jsonify(library.to_dict())


@api_bp.route('/testcase-libraries/<int:library_id>', methods=['DELETE'])
def delete_testcase_library(library_id):
    """删除用例库"""
    library = TestCaseLibrary.query.get_or_404(library_id)

    # 自动快照：删库前保存（最后的安全网）
    from app.api.snapshots import auto_snapshot
    auto_snapshot(library_id, f'删除用例库 "{library.name}" 前', 'auto')
    db.session.flush()

    db.session.delete(library)
    db.session.commit()
    return jsonify({'message': f'用例库 "{library.name}" 已删除'})


# ==================== 脑图 API ====================

@api_bp.route('/testcase-libraries/<int:library_id>/mindmap', methods=['GET'])
def get_library_mindmap(library_id):
    """获取用例库脑图"""
    library = TestCaseLibrary.query.get_or_404(library_id)

    # 重新构建脑图
    cases = library.cases.all()
    mindmap = build_mindmap_from_cases(cases)

    # 更新脑图结构到数据库
    library.mindmap = mindmap
    db.session.commit()

    return jsonify(mindmap)


@api_bp.route('/testcase-libraries/<int:library_id>/mindmap', methods=['PUT'])
def update_library_mindmap(library_id):
    """
    更新用例库脑图结构
    支持拖拽调整节点
    """
    library = TestCaseLibrary.query.get_or_404(library_id)
    data = request.get_json()

    if 'mindmap' not in data:
        return jsonify({'error': 'mindmap 为必填项'}), 400

    library.mindmap = data['mindmap']
    db.session.commit()

    return jsonify({'message': '脑图已更新'})


# ==================== 用例 CRUD ====================

@api_bp.route('/testcase-libraries/<int:library_id>/cases', methods=['GET'])
def list_library_cases(library_id):
    """获取用例列表"""
    library = TestCaseLibrary.query.get_or_404(library_id)

    priority = request.args.get('priority')
    case_type = request.args.get('type')
    search = request.args.get('search')

    query = library.cases

    if priority:
        query = query.filter_by(priority=priority)
    if case_type:
        query = query.filter_by(type=case_type)
    if search:
        query = query.filter(TestCase.title.ilike(f'%{search}%'))

    cases = query.order_by(TestCase.created_at.desc()).all()
    return jsonify([c.to_dict() for c in cases])


@api_bp.route('/testcase-libraries/<int:library_id>/cases', methods=['POST'])
def create_case(library_id):
    """创建用例"""
    library = TestCaseLibrary.query.get_or_404(library_id)
    data = request.get_json()

    if not data or not data.get('title'):
        return jsonify({'error': 'title 为必填项'}), 400

    # 生成用例编号
    case_count = library.cases.count()
    case_id = data.get('case_id', f"TC_{case_count + 1:03d}")

    case = TestCase(
        library_id=library_id,
        case_id=case_id,
        title=data['title'],
        priority=data.get('priority', 'P2'),
        type=data.get('type', 'functional'),
        content=data.get('content', {}),
        mindmap_node_id=generate_mindmap_node_id(),
        tags=data.get('tags', []),
    )
    db.session.add(case)
    db.session.commit()

    return jsonify(case.to_dict()), 201


@api_bp.route('/testcase-libraries/<int:library_id>/cases/<int:case_id>', methods=['GET'])
def get_case(library_id, case_id):
    """获取用例详情"""
    library = TestCaseLibrary.query.get_or_404(library_id)
    case = TestCase.query.filter_by(library_id=library_id, id=case_id).first_or_404()
    return jsonify(case.to_dict())


@api_bp.route('/testcase-libraries/<int:library_id>/cases/<int:case_id>', methods=['PUT'])
def update_case(library_id, case_id):
    """更新用例"""
    library = TestCaseLibrary.query.get_or_404(library_id)
    case = TestCase.query.filter_by(library_id=library_id, id=case_id).first_or_404()
    data = request.get_json()

    updatable_fields = ['title', 'priority', 'type', 'content', 'tags']
    for field in updatable_fields:
        if field in data:
            setattr(case, field, data[field])

    db.session.commit()
    return jsonify(case.to_dict())


@api_bp.route('/testcase-libraries/<int:library_id>/cases/<int:case_id>', methods=['DELETE'])
def delete_case(library_id, case_id):
    """删除用例"""
    library = TestCaseLibrary.query.get_or_404(library_id)
    case = TestCase.query.filter_by(library_id=library_id, id=case_id).first_or_404()
    db.session.delete(case)
    db.session.commit()
    return jsonify({'message': f'用例 "{case.title}" 已删除'})


# ==================== 批量操作 ====================

@api_bp.route('/testcase-libraries/<int:library_id>/cases/batch', methods=['POST'])
def batch_create_cases(library_id):
    """
    批量创建用例

    请求体：
    {
        "cases": [
            {"title": "...", "priority": "P1", ...},
            ...
        ]
    }
    """
    library = TestCaseLibrary.query.get_or_404(library_id)
    data = request.get_json()

    if not data or 'cases' not in data:
        return jsonify({'error': 'cases 为必填项'}), 400

    created_cases = []
    base_count = library.cases.count()

    for i, case_data in enumerate(data['cases']):
        case = TestCase(
            library_id=library_id,
            case_id=case_data.get('case_id', f"TC_{base_count + i + 1:03d}"),
            title=case_data.get('title', '未命名用例'),
            priority=case_data.get('priority', 'P2'),
            type=case_data.get('type', 'functional'),
            content=case_data.get('content', {}),
            mindmap_node_id=generate_mindmap_node_id(),
            tags=case_data.get('tags', []),
        )
        db.session.add(case)
        created_cases.append(case)

    db.session.commit()

    return jsonify({
        'message': f'成功创建 {len(created_cases)} 个用例',
        'cases': [c.to_dict() for c in created_cases],
    }), 201


@api_bp.route('/testcase-libraries/<int:library_id>/cases/batch', methods=['DELETE'])
def batch_delete_cases(library_id):
    """
    批量删除用例

    请求体：
    {
        "case_ids": [1, 2, 3]  // 用例ID列表
    }
    """
    library = TestCaseLibrary.query.get_or_404(library_id)
    data = request.get_json()

    if not data or 'case_ids' not in data:
        return jsonify({'error': 'case_ids 为必填项'}), 400

    # 自动快照：批量删除前保存
    from app.api.snapshots import auto_snapshot
    auto_snapshot(library_id, f'批量删除 {len(data["case_ids"])} 条用例前', 'auto')

    deleted_count = TestCase.query.filter(
        TestCase.library_id == library_id,
        TestCase.id.in_(data['case_ids'])
    ).delete(synchronize_session=False)

    db.session.commit()

    return jsonify({'message': f'成功删除 {deleted_count} 个用例'})


# ==================== YAML 导入导出 ====================

@api_bp.route('/testcase-libraries/<int:library_id>/export/yaml', methods=['GET'])
def export_cases_yaml(library_id):
    """导出用例为 YAML 格式"""
    from flask import Response
    library = TestCaseLibrary.query.get_or_404(library_id)
    cases = library.cases.order_by(TestCase.priority, TestCase.case_id).all()

    lines = [f"# 用例库: {library.name}",
             f"# 项目: {library.project_name or '未指定'}",
             f"# 模块: {library.module_name or '未指定'}",
             f"# 导出时间: {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M')}",
             "", "cases:"]

    for c in cases:
        lines.append(f"  - id: {c.case_id}")
        lines.append(f"    title: \"{c.title}\"")
        lines.append(f"    priority: {c.priority}")
        lines.append(f"    type: {c.type}")
        if c.tags:
            lines.append(f"    tags: [{', '.join(c.tags)}]")
        content = c.content or {}
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except Exception:
                content = {}
        if content.get('preconditions'):
            lines.append(f"    preconditions: \"{content['preconditions']}\"")
        steps = content.get('steps', [])
        if steps:
            lines.append("    steps:")
            for s in steps:
                if isinstance(s, dict):
                    lines.append(f"      - action: \"{s.get('action', '')}\"")
                    lines.append(f"        expected: \"{s.get('expected', '')}\"")
                else:
                    lines.append(f"      - action: \"{s}\"")
        if content.get('notes'):
            lines.append(f"    notes: \"{content['notes']}\"")
        lines.append("")

    yaml_str = "\n".join(lines)
    return Response(yaml_str, mimetype='text/yaml; charset=utf-8',
                    headers={'Content-Disposition': f'inline; filename="testcases-lib-{library_id}.yaml"'})


@api_bp.route('/testcase-libraries/<int:library_id>/import/yaml', methods=['POST'])
def import_cases_yaml(library_id):
    """从 YAML 导入用例"""
    library = TestCaseLibrary.query.get_or_404(library_id)
    data = request.get_json()
    yaml_content = data.get('yaml', '')
    if not yaml_content:
        return jsonify({'error': 'yaml 内容为空'}), 400

    try:
        import yaml as yaml_lib
        parsed = yaml_lib.safe_load(yaml_content)
    except Exception:
        # 简单解析（无 PyYAML 依赖的 fallback）
        return jsonify({'error': '服务器未安装 PyYAML，请先 pip install pyyaml'}), 500

    cases_data = parsed.get('cases', [])
    if not cases_data:
        return jsonify({'error': '未找到用例数据'}), 400

    base_count = library.cases.count()
    created = []
    for i, cd in enumerate(cases_data):
        steps = cd.get('steps', [])
        content = {
            'preconditions': cd.get('preconditions', ''),
            'steps': steps if isinstance(steps, list) else [],
            'notes': cd.get('notes', ''),
        }
        case = TestCase(
            library_id=library_id,
            case_id=cd.get('id', f"TC_{base_count + i + 1:03d}"),
            title=cd.get('title', '未命名'),
            priority=cd.get('priority', 'P2'),
            type=cd.get('type', 'functional'),
            content=content,
            mindmap_node_id=generate_mindmap_node_id(),
            tags=cd.get('tags', []),
        )
        db.session.add(case)
        created.append(case)

    db.session.commit()
    return jsonify({'message': f'导入 {len(created)} 条用例', 'count': len(created)}), 201


# ==================== XMind 导出 ====================

@api_bp.route('/testcase-libraries/<int:library_id>/export/xmind', methods=['GET'])
def export_cases_xmind(library_id):
    """导出为 XMind 格式（.xmind 文件）"""
    from flask import Response
    import zipfile
    import io

    library = TestCaseLibrary.query.get_or_404(library_id)
    cases = library.cases.order_by(TestCase.priority, TestCase.case_id).all()

    # 按优先级 → 类型分组构建 XMind 树
    groups = {}
    for c in cases:
        key = c.priority or 'P2'
        if key not in groups:
            groups[key] = {}
        ctype = c.type or 'functional'
        if ctype not in groups[key]:
            groups[key][ctype] = []
        groups[key][ctype].append(c)

    type_labels = {'functional': '功能测试', 'interface': '接口测试',
                   'performance': '性能测试', 'security': '安全测试'}

    # 构建 XMind content.json
    root_children = []
    for prio in ['P0', 'P1', 'P2', 'P3']:
        if prio not in groups:
            continue
        prio_children = []
        for ctype, type_cases in groups[prio].items():
            case_nodes = []
            for c in type_cases:
                node = {"title": f"[{c.case_id}] {c.title}"}
                # 步骤作为子节点
                content = c.content or {}
                if isinstance(content, str):
                    try:
                        content = json.loads(content)
                    except Exception:
                        content = {}
                steps = content.get('steps', [])
                if steps:
                    step_nodes = []
                    for s in steps:
                        if isinstance(s, dict):
                            step_nodes.append({"title": f"{s.get('action', '')} → {s.get('expected', '')}"})
                        else:
                            step_nodes.append({"title": str(s)})
                    node["children"] = {"attached": step_nodes}
                case_nodes.append(node)
            prio_children.append({
                "title": type_labels.get(ctype, ctype),
                "children": {"attached": case_nodes}
            })
        root_children.append({
            "title": prio,
            "children": {"attached": prio_children}
        })

    xmind_content = [{
        "id": "sheet1",
        "class": "sheet",
        "title": library.name,
        "rootTopic": {
            "id": "root",
            "class": "topic",
            "title": library.name,
            "children": {"attached": root_children},
            "structureClass": "org.xmind.ui.map.unbalanced"
        }
    }]

    # 打包为 .xmind（ZIP）
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('content.json', json.dumps(xmind_content, ensure_ascii=False, indent=2))
        zf.writestr('metadata.json', json.dumps({"creator": {"name": "OpenClaw Manager"}}))
        # manifest 必须有
        manifest = {
            "file-entries": {
                "content.json": {},
                "metadata.json": {}
            }
        }
        zf.writestr('manifest.json', json.dumps(manifest))

    buf.seek(0)
    return Response(buf.getvalue(),
                    mimetype='application/octet-stream',
                    headers={'Content-Disposition': f'attachment; filename="testcases-{library_id}.xmind"'})
