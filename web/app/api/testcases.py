"""
测试用例库 API
支持用例库的 CRUD、脑图结构管理、共享授权（开放权限）、评审流程
"""

import uuid
import json
from datetime import datetime
from flask import request, jsonify
from sqlalchemy import desc, or_
from sqlalchemy.orm import defer
from app import db
from app.models import (
    TestCaseLibrary, TestCase, Project, OpenClawInstance, User,
    TestCaseLibraryShare, TestCaseLibraryReview, Topic, CaseReviewRound, _now,
    TestCaseChangeLog, TestCasePanoramaLink,
)
from app.api import api_bp
from app.api.audit import log_action
from app.services.testcase_panorama_links import changed_fields, snapshot_case
from app.services.test_case_delete import (
    cleanup_test_case_dependencies,
    cleanup_test_case_library_dependencies,
)


def _get_current_user():
    from app.api.skills import _get_current_user
    return _get_current_user()


def _operator():
    user = _get_current_user()
    if not user:
        return 'system'
    return getattr(user, '_claw_name', None) or user.username or 'system'


def _directory_matches(case_path, link_path):
    case_path = (case_path or '').strip()
    link_path = (link_path or '').strip()
    if not link_path:
        return case_path == ''
    return case_path == link_path or case_path.startswith(link_path + '/')


def _linked_panorama_modules_for_case(case):
    links = TestCasePanoramaLink.query.filter_by(library_id=case.library_id).all()
    result = []
    for link in links:
        level = link.link_level or 'library'
        if level == 'case' and link.case_pk != case.id:
            continue
        if level == 'directory' and not _directory_matches(case.module_path, link.module_path):
            continue
        module = link.module
        result.append({
            'module_id': link.module_id,
            'module_name': module.name if module else '',
            'module_path': module.path if module else '',
            'link_level': level,
            'library_id': link.library_id,
            'directory_path': link.module_path or '',
        })
    return result


def _record_case_change(case, change_type, old_snapshot=None, new_snapshot=None,
                        operation_id='', source='web', changed_fields_override=None):
    old_snapshot = old_snapshot or {}
    new_snapshot = new_snapshot or {}
    current = new_snapshot or old_snapshot or snapshot_case(case)
    log = TestCaseChangeLog(
        library_id=case.library_id,
        case_pk=case.id,
        case_id=current.get('case_id') or case.case_id or '',
        case_title=current.get('title') or case.title or '',
        module_path=current.get('module_path') or case.module_path or '',
        change_type=change_type,
        changed_fields=changed_fields_override if changed_fields_override is not None
        else changed_fields(old_snapshot, new_snapshot),
        old_snapshot=old_snapshot,
        new_snapshot=new_snapshot,
        operation_id=operation_id or '',
        linked_panorama_modules=_linked_panorama_modules_for_case(case),
        changed_by=_operator(),
        source=source,
    )
    db.session.add(log)
    return log


def _collect_user_project_ids(user):
    ids = set()
    if not user:
        return ids
    for pid in (getattr(user, 'managed_projects', None) or []):
        try:
            ids.add(int(pid))
        except Exception:
            continue
    bound_claw_id = getattr(user, 'bound_claw_id', None)
    if bound_claw_id:
        claw = OpenClawInstance.query.get(bound_claw_id)
        if claw and claw.project_id:
            ids.add(int(claw.project_id))
    return ids


def _library_project_id(library):
    pname = (library.project_name or '').strip()
    if not pname:
        return None
    p = Project.query.filter_by(name=pname).first()
    return p.id if p else None


def _normalize_project_name(raw):
    """校验并规范化 project_name；返回 (name, error_message)。"""
    pname = (raw or '').strip()
    if not pname:
        return None, 'project_name 为必填项'
    if not Project.query.filter_by(name=pname).first():
        return None, f'项目不存在: {pname}'
    return pname, None


# ====================================================================
# 共享授权 / 评审权限工具（参考 engineering.py 的 _can_view 等同形）
# ====================================================================

def _user_managed_claw_ids(user):
    """该用户名下绑定的 OpenClaw ID 集合（共享给用户时，自动覆盖名下 claw）。"""
    if not user:
        return set()
    username = getattr(user, 'username', None)
    if not username:
        return set()
    rows = (OpenClawInstance.query
            .filter(OpenClawInstance.owner == username,
                    OpenClawInstance.status != 'deleted')
            .with_entities(OpenClawInstance.id).all())
    return {r[0] for r in rows}


def _list_active_library_share_grants(user):
    """构建当前调用方对所有用例库的「显式共享授权」索引。
    返回 dict: {library_id: permission}（permission 取最高权限）。

    覆盖：
      - public 共享（任何已登录用户/claw 可见）
      - 授权给当前用户的（target_user_id == user.id）
      - 授权给当前用户名下任意 claw 的（target_claw_id ∈ managed claws）
      - 授权给当前 token 绑定 claw 的（target_claw_id == bound_claw_id）
    自动剔除已过期记录。
    """
    if not user:
        return {}

    now = _now()
    user_id = getattr(user, 'id', None)
    bound_claw_id = getattr(user, 'bound_claw_id', None)
    managed_claw_ids = _user_managed_claw_ids(user)
    if bound_claw_id:
        managed_claw_ids.add(bound_claw_id)

    conds = [TestCaseLibraryShare.share_type == 'public']
    if user_id:
        conds.append(db.and_(TestCaseLibraryShare.share_type == 'user',
                             TestCaseLibraryShare.target_user_id == user_id))
    if managed_claw_ids:
        conds.append(db.and_(TestCaseLibraryShare.share_type == 'claw',
                             TestCaseLibraryShare.target_claw_id.in_(
                                 list(managed_claw_ids))))

    rows = (TestCaseLibraryShare.query
            .filter(or_(*conds))
            .filter(or_(TestCaseLibraryShare.expires_at == None,  # noqa: E711
                        TestCaseLibraryShare.expires_at > now))
            .with_entities(TestCaseLibraryShare.library_id,
                           TestCaseLibraryShare.permission).all())

    rank = {'readonly': 1, 'reviewer': 2, 'editor': 3}
    grants = {}
    for lib_id, perm in rows:
        perm = perm or 'reviewer'
        if rank.get(perm, 1) > rank.get(grants.get(lib_id, 'readonly'), 1):
            grants[lib_id] = perm
        elif lib_id not in grants:
            grants[lib_id] = perm
    return grants


def _can_manage_library(user, library):
    if not user:
        return False
    if user.role == 'super_admin':
        return True
    if user.role == 'admin':
        pid = _library_project_id(library)
        return pid is not None and pid in _collect_user_project_ids(user)
    owner = (library.owner or '').strip()
    if owner == user.username:
        return True
    claw_name = getattr(user, '_claw_name', None)
    return bool(claw_name and owner == claw_name)


def _can_share_library(user, library):
    """共享授权权限：与"管理"对齐（作者/项目 admin/super_admin）。"""
    return _can_manage_library(user, library)


def _can_review_library(user, library, share_grants=None):
    """是否能对该库发表评审意见 / 通过 / 驳回。
    标准：可管理 + 被授权为 reviewer/editor。
    """
    if _can_manage_library(user, library):
        return True
    if share_grants is None:
        share_grants = _list_active_library_share_grants(user)
    perm = share_grants.get(library.id)
    return perm in ('reviewer', 'editor')


def _ensure_library_access(library, write=False, share_grants=None):
    user = _get_current_user()
    if not user:
        return None, (jsonify({'error': '未登录'}), 401)
    if write:
        if _can_manage_library(user, library):
            return user, None
        # 显式共享为 editor 的用户/claw 可编辑内容（用例/脑图/目录/导入）
        if share_grants is None:
            share_grants = _list_active_library_share_grants(user)
        if share_grants.get(library.id) == 'editor':
            return user, None
        return user, (jsonify({'error': '无权操作该用例库'}), 403)

    if user.role == 'super_admin':
        return user, None
    own_names = {user.username, getattr(user, '_claw_name', None)}
    own_names.discard(None)
    if (library.owner or '') in own_names:
        return user, None
    pid = _library_project_id(library)
    if pid is not None and pid in _collect_user_project_ids(user):
        return user, None
    # 显式共享命中（reviewer/editor/readonly 均放行只读）
    if share_grants is None:
        share_grants = _list_active_library_share_grants(user)
    if library.id in share_grants:
        return user, None
    return user, (jsonify({'error': '无权访问该用例库'}), 403)


def _parse_expires_at(raw):
    """允许字符串 'YYYY-MM-DD HH:MM:SS' 或 ISO8601；返回 datetime 或 None；非法抛 ValueError。"""
    if raw in (None, '', 'null'):
        return None
    if isinstance(raw, datetime):
        return raw
    s = str(raw).strip()
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M',
                '%Y-%m-%d'):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f'expires_at 格式不识别: {raw}')


def generate_mindmap_node_id():
    """生成唯一的脑图节点ID"""
    return f"node_{uuid.uuid4().hex[:8]}"


def _slim_mindmap_for_storage(mindmap):
    """持久化脑图时去掉叶子节点上的 case 全量 data，避免 TEXT 上限截断 JSON。"""
    if not isinstance(mindmap, dict):
        return mindmap

    def _walk(node):
        if not isinstance(node, dict):
            return node
        slim = {k: v for k, v in node.items() if k != 'data'}
        children = slim.get('children')
        if children:
            slim['children'] = [_walk(c) for c in children]
        return slim

    return _walk(mindmap)


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
    user = _get_current_user()
    if not user:
        return jsonify({'error': '未登录'}), 401
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

    libraries = query.options(defer(TestCaseLibrary.mindmap)).order_by(TestCaseLibrary.updated_at.desc()).all()
    share_grants = {} if user.role == 'super_admin' else _list_active_library_share_grants(user)
    if user.role != 'super_admin':
        user_projects = _collect_user_project_ids(user)
        own_names = {user.username, getattr(user, '_claw_name', None)}
        own_names.discard(None)
        filtered = []
        for lib in libraries:
            if (lib.owner or '') in own_names:
                filtered.append(lib)
                continue
            pid = _library_project_id(lib)
            if user_projects and pid in user_projects:
                filtered.append(lib)
                continue
            # 被共享出去的库：放行（前端打"共享给我"标签）
            if lib.id in share_grants:
                filtered.append(lib)
        libraries = filtered

    out = []
    for lib in libraries:
        d = lib.to_dict(with_mindmap=False)
        d['shared_with_me'] = lib.id in share_grants
        d['my_share_permission'] = share_grants.get(lib.id) if lib.id in share_grants else None
        d['can_manage'] = _can_manage_library(user, lib)
        d['can_share'] = _can_share_library(user, lib)
        # 列表展示用：共享出去的总条数（只统计 active）
        d['active_share_count'] = TestCaseLibraryShare.query.filter_by(
            library_id=lib.id
        ).filter(or_(TestCaseLibraryShare.expires_at == None,  # noqa: E711
                     TestCaseLibraryShare.expires_at > _now())).count()
        out.append(d)
    return jsonify(out)


@api_bp.route('/testcase-libraries', methods=['POST'])
def create_testcase_library():
    """创建用例库"""
    user = _get_current_user()
    if not user:
        return jsonify({'error': '未登录'}), 401
    data = request.get_json()

    if not data or not data.get('name'):
        return jsonify({'error': 'name 为必填项'}), 400

    owner = getattr(user, '_claw_name', None) or user.username
    library = TestCaseLibrary(
        name=data['name'],
        description=data.get('description', ''),
        project_name=data.get('project_name'),
        module_name=data.get('module_name'),
        owner=owner,
        created_by=owner,
        updated_by=owner,
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
        created_by=data.get('owner', 'system'),
        updated_by=data.get('owner', 'system'),
        mindmap={'id': 'root', 'text': f'{project_name} 用例库', 'children': []},
    )
    db.session.add(library)
    db.session.commit()
    return jsonify(library.to_dict(with_cases=True)), 201


@api_bp.route('/testcase-libraries/<int:library_id>', methods=['GET'])
def get_testcase_library(library_id):
    """获取用例库详情"""
    library = TestCaseLibrary.query.get_or_404(library_id)
    user, err = _ensure_library_access(library, write=False)
    if err:
        return err
    data = library.to_dict(with_cases=True)
    share_grants = _list_active_library_share_grants(user)
    data['shared_with_me'] = library.id in share_grants
    data['my_share_permission'] = share_grants.get(library.id)
    data['can_manage'] = _can_manage_library(user, library)
    data['can_share'] = _can_share_library(user, library)
    data['can_review'] = _can_review_library(user, library, share_grants)
    links = (TestCasePanoramaLink.query
             .filter_by(library_id=library.id)
             .order_by(TestCasePanoramaLink.module_path, TestCasePanoramaLink.id)
             .all())
    data['panorama_links'] = [link.to_dict() for link in links]
    return jsonify(data)


@api_bp.route('/testcase-libraries/<int:library_id>', methods=['PUT'])
def update_testcase_library(library_id):
    """更新用例库"""
    library = TestCaseLibrary.query.get_or_404(library_id)
    user = _get_current_user()
    if not _can_manage_library(user, library):
        return jsonify({'error': '无权修改该用例库'}), 403
    data = request.get_json()

    if 'project_name' in data:
        project_name, perr = _normalize_project_name(data.get('project_name'))
        if perr:
            return jsonify({'error': perr}), 400
        data = dict(data)
        data['project_name'] = project_name

    updatable_fields = ['name', 'description', 'project_name', 'module_name', 'owner', 'status']
    for field in updatable_fields:
        if field in data:
            setattr(library, field, data[field])

    # 更新脑图根节点
    if 'name' in data and library.mindmap:
        library.mindmap['text'] = data['name']

    library.updated_by = _operator()
    db.session.commit()
    return jsonify(library.to_dict())


@api_bp.route('/testcase-libraries/<int:library_id>', methods=['DELETE'])
def delete_testcase_library(library_id):
    """删除用例库"""
    library = TestCaseLibrary.query.get_or_404(library_id)
    user = _get_current_user()
    if not _can_manage_library(user, library):
        return jsonify({'error': '无权删除该用例库'}), 403

    case_ids = [
        row[0] for row in db.session.query(TestCase.id)
        .filter_by(library_id=library_id).all()
    ]
    affected_module_ids = cleanup_test_case_library_dependencies(
        library_id, case_ids,
    )
    db.session.delete(library)
    if affected_module_ids:
        from app.api.testcase_panorama_links import recalc_module_test_metrics
        recalc_module_test_metrics(module_ids=affected_module_ids)
    db.session.commit()
    return jsonify({'message': f'用例库 "{library.name}" 已删除'})






@api_bp.route('/testcase-libraries/<int:library_id>/modules', methods=['GET'])
def get_library_modules(library_id):
    """获取用例库的目录树结构

    从所有用例的 module_path 字段自动构建目录树，无需单独建表。
    返回格式：
    [
        {"name": "登录模块", "path": "登录模块", "count": 10, "children": [
            {"name": "手机号登录", "path": "登录模块/手机号登录", "count": 5, "children": []}
        ]},
        {"name": "(未分类)", "path": "", "count": 3, "children": []}
    ]
    """
    library = TestCaseLibrary.query.get_or_404(library_id)
    cases = library.cases.all()

    # 统计每个路径的用例数（排除占位用例）
    path_counts = {}
    uncategorized = 0
    for c in cases:
        mp = (c.module_path or '').strip()
        if not mp:
            if not c.is_placeholder:
                uncategorized += 1
        else:
            # 统计完整路径和所有祖先路径
            parts = mp.split('/')
            for i in range(len(parts)):
                ancestor = '/'.join(parts[:i+1])
                if ancestor not in path_counts:
                    path_counts[ancestor] = 0
            if not c.is_placeholder:
                path_counts[mp] = path_counts.get(mp, 0) + 1

    # 重新计算：每个节点的直接用例数 = 只属于该路径的用例（不含子路径，不含占位）
    direct_counts = {}
    for c in cases:
        mp = (c.module_path or '').strip()
        if mp and not c.is_placeholder:
            direct_counts[mp] = direct_counts.get(mp, 0) + 1

    # 构建树（包含空目录：即使 count=0 也要显示）
    def build_tree(parent_path=''):
        children = []
        seen = set()
        for path in sorted(path_counts.keys()):
            if parent_path:
                if not path.startswith(parent_path + '/'):
                    continue
                remaining = path[len(parent_path) + 1:]
            else:
                remaining = path

            # 取第一层
            top = remaining.split('/')[0]
            if top in seen:
                continue
            seen.add(top)

            child_path = f'{parent_path}/{top}' if parent_path else top
            # 该节点下的总用例数（不含占位）
            total = sum(direct_counts.get(p, 0) for p in direct_counts if p == child_path or p.startswith(child_path + '/'))
            children.append({
                'name': top,
                'path': child_path,
                'count': total,
                'children': build_tree(child_path),
            })
        return children

    tree = build_tree()

    # 添加未分类
    if uncategorized > 0:
        tree.append({
            'name': '(未分类)',
            'path': '',
            'count': uncategorized,
            'children': [],
        })

    return jsonify(tree)


@api_bp.route('/testcase-libraries/<int:library_id>/modules', methods=['POST'])
def create_testcase_module(library_id):
    """新增子目录

    请求体：{
        "parent_path": "A模块",        // 父目录路径，空字符串表示根目录
        "name": "AA模块"               // 新目录名
    }

    实现方式：创建一个占位用例（is_placeholder=True），
    module_path 设为 parent_path/name，使空目录能显示在目录树中。
    当该目录下有真实用例时，占位用例会被自动清理。
    """
    library = TestCaseLibrary.query.get_or_404(library_id)
    user, err = _ensure_library_access(library, write=True)
    if err:
        return err
    data = request.get_json()

    if not data or not data.get('name'):
        return jsonify({'error': 'name 为必填项'}), 400

    name = data['name'].strip()
    parent_path = (data.get('parent_path') or '').strip()

    # 校验名称：不能包含 / 和特殊字符
    if '/' in name:
        return jsonify({'error': '目录名不能包含 /'}), 400
    if not name or name == '(未分类)':
        return jsonify({'error': '目录名无效'}), 400

    full_path = f'{parent_path}/{name}' if parent_path else name

    # 检查同名目录是否已存在
    existing = TestCase.query.filter_by(
        library_id=library_id,
        module_path=full_path,
    ).first()
    if existing:
        return jsonify({'error': f'目录 "{full_path}" 已存在'}), 409

    # 创建占位用例
    placeholder = TestCase(
        library_id=library_id,
        case_id=f'_dir_{uuid.uuid4().hex[:6]}',
        title=f'[目录] {name}',
        priority='P2',
        type='functional',
        content={},
        mindmap_node_id=generate_mindmap_node_id(),
        module_path=full_path,
        is_placeholder=True,
        created_by=(data.get('created_by') or getattr(user, '_claw_name', None) or user.username),
        updated_by=(data.get('created_by') or getattr(user, '_claw_name', None) or user.username),
    )
    db.session.add(placeholder)
    library.updated_by = _operator()
    db.session.commit()

    return jsonify({
        'message': f'目录 "{full_path}" 已创建',
        'path': full_path,
    }), 201


@api_bp.route('/testcase-libraries/<int:library_id>/modules', methods=['DELETE'])
def delete_testcase_module(library_id):
    """删除空目录（仅允许删除没有真实用例的目录）

    请求体：{ "path": "A模块/AA模块" }

    如果该目录下有真实用例，返回错误。
    只删除占位用例。
    """
    library = TestCaseLibrary.query.get_or_404(library_id)
    _, err = _ensure_library_access(library, write=True)
    if err:
        return err
    data = request.get_json()
    path = (data or {}).get('path', '').strip()
    if not path:
        return jsonify({'error': 'path 必填'}), 400

    # 检查是否有真实用例（不含占位）
    real_cases = TestCase.query.filter(
        TestCase.library_id == library_id,
        TestCase.is_placeholder != True,
        db.or_(
            TestCase.module_path == path,
            TestCase.module_path.like(f'{path}/%')
        )
    ).count()

    if real_cases > 0:
        return jsonify({'error': f'该目录下有 {real_cases} 条真实用例，无法删除'}), 400

    # 删除占位用例
    placeholders = TestCase.query.filter(
        TestCase.library_id == library_id,
        TestCase.is_placeholder == True,
        db.or_(
            TestCase.module_path == path,
            TestCase.module_path.like(f'{path}/%')
        )
    ).all()
    for p in placeholders:
        db.session.delete(p)

    library.updated_by = _operator()
    db.session.commit()
    return jsonify({'message': f'目录 "{path}" 已删除'})


@api_bp.route('/testcase-libraries/<int:library_id>/modules/rename', methods=['POST'])
def rename_module(library_id):
    """重命名目录（批量更新 module_path）"""
    library = TestCaseLibrary.query.get_or_404(library_id)
    _, err = _ensure_library_access(library, write=True)
    if err:
        return err
    data = request.get_json()
    old_path = data.get('old_path', '')
    new_path = data.get('new_path', '')
    if not old_path or not new_path:
        return jsonify({'error': 'old_path 和 new_path 必填'}), 400

    # 更新完全匹配的
    cases = TestCase.query.filter_by(library_id=library_id, module_path=old_path).all()
    for c in cases:
        c.module_path = new_path
        c.updated_by = _operator()

    # 更新子路径
    children = TestCase.query.filter(
        TestCase.library_id == library_id,
        TestCase.module_path.like(f'{old_path}/%')
    ).all()
    for c in children:
        c.module_path = new_path + c.module_path[len(old_path):]
        c.updated_by = _operator()

    library.updated_by = _operator()
    db.session.commit()
    return jsonify({'message': f'已重命名 {len(cases) + len(children)} 条用例的目录'})


# ==================== 脑图 API ====================

@api_bp.route('/testcase-libraries/<int:library_id>/mindmap', methods=['GET'])
def get_library_mindmap(library_id):
    """获取用例库脑图"""
    library = TestCaseLibrary.query.get_or_404(library_id)

    # 重新构建脑图
    cases = library.cases.filter(TestCase.is_placeholder != True).all()
    mindmap = build_mindmap_from_cases(cases)

    # 更新脑图结构到数据库
    library.mindmap = _slim_mindmap_for_storage(mindmap)
    db.session.commit()

    return jsonify(mindmap)


@api_bp.route('/testcase-libraries/<int:library_id>/mindmap', methods=['PUT'])
def update_library_mindmap(library_id):
    """
    更新用例库脑图结构
    支持拖拽调整节点
    """
    library = TestCaseLibrary.query.get_or_404(library_id)
    _, err = _ensure_library_access(library, write=True)
    if err:
        return err
    data = request.get_json()

    if 'mindmap' not in data:
        return jsonify({'error': 'mindmap 为必填项'}), 400

    library.mindmap = _slim_mindmap_for_storage(data['mindmap'])
    db.session.commit()

    return jsonify({'message': '脑图已更新'})


@api_bp.route('/testcase-libraries/<int:library_id>/directory-mindmap',
              methods=['GET'])
def get_library_directory_mindmap(library_id):
    """按目录层级返回脑图，可从任意目录开始。

    查询参数：
        module_path  起始目录，缺省=整库；空串 `?module_path=` 表示"(未分类)"根目录
        max_nodes    **用例叶子**上限（默认 2000，上限 5000），超限返回 truncated=true。
                     只限叶子：目录树与各目录 case_count 始终完整准确。

    鉴权走**用例库权限**。评审场景（评审人可能没有用例库权限）请用
    `GET /topics/<id>/review-mindmap`，那条走课题可见性。
    本接口不返回评审标记，保持用例库视图与评审镜像层隔离。
    """
    from app.services.case_mindmap import (DEFAULT_MAX_LEAVES,
                                           build_directory_mindmap)

    library = TestCaseLibrary.query.get_or_404(library_id)
    _, err = _ensure_library_access(library, write=False)
    if err:
        return err

    raw_path = request.args.get('module_path')
    scope_type = 'library' if raw_path is None else 'module'
    module_path = (raw_path or '').strip().strip('/')

    try:
        max_nodes = int(request.args.get('max_nodes', DEFAULT_MAX_LEAVES))
    except (TypeError, ValueError):
        return jsonify({'error': 'max_nodes 必须是整数'}), 400
    max_nodes = max(50, min(max_nodes, 5000))

    cases = _scope_case_filter(library_id, scope_type, module_path,
                               include_placeholders=True).all()

    root_text = (module_path.split('/')[-1] if module_path
                 else (library.name or '用例库#%d' % library_id))
    if scope_type == 'module' and not module_path:
        root_text = '(未分类)'

    mindmap = build_directory_mindmap(
        root_text, cases, root_module_path=module_path, max_nodes=max_nodes)
    mindmap['library_id'] = library_id
    mindmap['library_name'] = library.name
    return jsonify(mindmap)


# ==================== 用例 CRUD ====================

@api_bp.route('/testcase-libraries/<int:library_id>/cases', methods=['GET'])
def list_library_cases(library_id):
    """获取用例列表

    查询参数 include_placeholders=true 时包含占位用例（用于目录树构建）
    默认不含占位用例（用于用例列表展示）
    """
    library = TestCaseLibrary.query.get_or_404(library_id)
    _, err = _ensure_library_access(library, write=False)
    if err:
        return err

    priority = request.args.get('priority')
    case_type = request.args.get('type')
    search = request.args.get('search')
    module_path = request.args.get('module_path')
    include_placeholders = request.args.get('include_placeholders', 'false').lower() == 'true'

    query = library.cases

    # 默认过滤占位用例（除非显式请求包含）
    if not include_placeholders:
        query = query.filter(TestCase.is_placeholder != True)

    if priority:
        query = query.filter_by(priority=priority)
    if case_type:
        query = query.filter_by(type=case_type)
    if module_path is not None:
        if module_path == '':
            # 未分类：module_path 为空或 NULL
            query = query.filter(db.or_(TestCase.module_path == '', TestCase.module_path == None))
        else:
            # 精确匹配或前缀匹配（包含子目录）
            query = query.filter(db.or_(
                TestCase.module_path == module_path,
                TestCase.module_path.like(f'{module_path}/%')
            ))
    if search:
        query = query.filter(TestCase.title.ilike(f'%{search}%'))

    query = query.order_by(TestCase.created_at.desc())
    page = request.args.get('page', type=int)
    page_size = request.args.get('page_size', type=int)
    if page is not None or page_size is not None:
        page = max(page or 1, 1)
        page_size = min(max(page_size or 50, 1), 200)
        query = query.offset((page - 1) * page_size).limit(page_size)

    cases = query.all()
    return jsonify([c.to_dict() for c in cases])


@api_bp.route('/testcase-libraries/<int:library_id>/cases', methods=['POST'])
def create_case(library_id):
    """创建用例"""
    library = TestCaseLibrary.query.get_or_404(library_id)
    user, err = _ensure_library_access(library, write=True)
    if err:
        return err
    data = request.get_json()

    if not data or not data.get('title'):
        return jsonify({'error': 'title 为必填项'}), 400

    # 生成用例编号
    case_count = library.cases.filter(TestCase.is_placeholder != True).count()
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
        module_path=data.get('module_path', ''),
        created_by=(data.get('created_by') or getattr(user, '_claw_name', None) or user.username),
        updated_by=(data.get('created_by') or getattr(user, '_claw_name', None) or user.username),
    )
    db.session.add(case)
    db.session.flush()
    _record_case_change(case, 'created', new_snapshot=snapshot_case(case),
                        changed_fields_override=['case'])

    # 保留同路径占位用例作为目录元信息载体，不再因真实用例创建而删除。
    library.updated_by = _operator()

    db.session.commit()

    return jsonify(case.to_dict()), 201


@api_bp.route('/testcase-libraries/<int:library_id>/cases/<int:case_id>', methods=['GET'])
def get_case(library_id, case_id):
    """获取用例详情"""
    library = TestCaseLibrary.query.get_or_404(library_id)
    _, err = _ensure_library_access(library, write=False)
    if err:
        return err
    case = TestCase.query.filter_by(library_id=library_id, id=case_id).first_or_404()
    return jsonify(case.to_dict())


@api_bp.route('/testcase-libraries/<int:library_id>/cases/<int:case_id>', methods=['PUT'])
def update_case(library_id, case_id):
    """更新用例"""
    library = TestCaseLibrary.query.get_or_404(library_id)
    _, err = _ensure_library_access(library, write=True)
    if err:
        return err
    case = TestCase.query.filter_by(library_id=library_id, id=case_id).first_or_404()
    data = request.get_json()
    old_snapshot = snapshot_case(case)

    updatable_fields = ['title', 'priority', 'type', 'content', 'tags', 'module_path', 'created_by',
                        'tapd_story_url', 'tapd_story_title']
    for field in updatable_fields:
        if field in data:
            setattr(case, field, data[field])

    case.updated_by = _operator()
    library.updated_by = _operator()
    new_snapshot = snapshot_case(case)
    fields = changed_fields(old_snapshot, new_snapshot)
    if fields:
        _record_case_change(case, 'updated', old_snapshot=old_snapshot,
                            new_snapshot=new_snapshot,
                            changed_fields_override=fields)

    db.session.commit()
    return jsonify(case.to_dict())


@api_bp.route('/testcase-libraries/<int:library_id>/cases/<int:case_id>', methods=['DELETE'])
def delete_case(library_id, case_id):
    """删除用例"""
    library = TestCaseLibrary.query.get_or_404(library_id)
    _, err = _ensure_library_access(library, write=True)
    if err:
        return err
    case = TestCase.query.filter_by(library_id=library_id, id=case_id).first_or_404()
    old_snapshot = snapshot_case(case)
    _record_case_change(case, 'deleted', old_snapshot=old_snapshot,
                        changed_fields_override=['case'])
    affected_module_ids = cleanup_test_case_dependencies([case.id])
    db.session.delete(case)
    library.updated_by = _operator()
    if affected_module_ids:
        from app.api.testcase_panorama_links import recalc_module_test_metrics
        recalc_module_test_metrics(module_ids=affected_module_ids)
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
            {"title": "...", "priority": "P1", "module_path": "模块/子模块", ...},
            ...
        ]
    }
    """
    library = TestCaseLibrary.query.get_or_404(library_id)
    user, err = _ensure_library_access(library, write=True)
    if err:
        return err
    data = request.get_json()

    if not data or 'cases' not in data:
        return jsonify({'error': 'cases 为必填项'}), 400

    created_cases = []
    base_count = library.cases.filter(TestCase.is_placeholder != True).count()
    operation_id = data.get('operation_id') or str(uuid.uuid4())

    for i, case_data in enumerate(data['cases']):
        case = TestCase(
            library_id=library_id,
            case_id=case_data.get('case_id', f"TC_{base_count + i + 1:03d}"),
            title=case_data.get('title', '未命名用例'),
            priority=case_data.get('priority', 'P2'),
            type=case_data.get('type', 'functional'),
            content=case_data.get('content', {}),
            module_path=case_data.get('module_path', ''),
            mindmap_node_id=generate_mindmap_node_id(),
            tags=case_data.get('tags', []),
            tapd_story_url=case_data.get('tapd_story_url', ''),
            tapd_story_title=case_data.get('tapd_story_title', ''),
            created_by=(case_data.get('created_by') or getattr(user, '_claw_name', None) or user.username),
            updated_by=(case_data.get('created_by') or getattr(user, '_claw_name', None) or user.username),
        )
        db.session.add(case)
        db.session.flush()
        _record_case_change(case, 'batch_created',
                            new_snapshot=snapshot_case(case),
                            operation_id=operation_id,
                            changed_fields_override=['case'])
        created_cases.append(case)

    library.updated_by = _operator()
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
    _, err = _ensure_library_access(library, write=True)
    if err:
        return err
    data = request.get_json()

    if not data or 'case_ids' not in data:
        return jsonify({'error': 'case_ids 为必填项'}), 400

    # 自动快照：批量删除前保存
    from app.api.snapshots import auto_snapshot
    auto_snapshot(library_id, f'批量删除 {len(data["case_ids"])} 条用例前', 'auto')

    operation_id = data.get('operation_id') or str(uuid.uuid4())
    cases_to_delete = TestCase.query.filter(
        TestCase.library_id == library_id,
        TestCase.id.in_(data['case_ids'])
    ).all()
    case_pk_list = [case.id for case in cases_to_delete]
    for case in cases_to_delete:
        _record_case_change(case, 'batch_deleted',
                            old_snapshot=snapshot_case(case),
                            operation_id=operation_id,
                            changed_fields_override=['case'])
    affected_module_ids = cleanup_test_case_dependencies(case_pk_list)
    deleted_count = TestCase.query.filter(
        TestCase.library_id == library_id,
        TestCase.id.in_(data['case_ids'])
    ).delete(synchronize_session=False)

    library.updated_by = _operator()
    if affected_module_ids:
        from app.api.testcase_panorama_links import recalc_module_test_metrics
        recalc_module_test_metrics(module_ids=affected_module_ids)
    db.session.commit()

    return jsonify({'message': f'成功删除 {deleted_count} 个用例'})


# ==================== YAML 导入导出 ====================

@api_bp.route('/testcase-libraries/<int:library_id>/export/yaml', methods=['GET'])
def export_cases_yaml(library_id):
    """导出用例为 YAML 格式"""
    from flask import Response
    library = TestCaseLibrary.query.get_or_404(library_id)
    cases = library.cases.filter(TestCase.is_placeholder != True).order_by(TestCase.priority, TestCase.case_id).all()

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

    base_count = library.cases.filter(TestCase.is_placeholder != True).count()
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
            created_by=_operator(),
            updated_by=_operator(),
        )
        db.session.add(case)
        created.append(case)

    library.updated_by = _operator()
    db.session.commit()
    return jsonify({'message': f'导入 {len(created)} 条用例', 'count': len(created)}), 201


# ==================== XMind 导出 ====================

# ==================== TAPD 需求绑定 ====================

@api_bp.route('/testcase-libraries/<int:library_id>/cases/<int:case_id>/tapd-bind', methods=['PUT'])
def bind_case_tapd(library_id, case_id):
    """绑定单条用例到 TAPD 需求

    请求体：
    {
        "tapd_story_url": "https://www.tapd.cn/...",
        "tapd_story_title": "需求标题（可选，自动提取）"
    }
    """
    library = TestCaseLibrary.query.get_or_404(library_id)
    case = TestCase.query.filter_by(library_id=library_id, id=case_id).first_or_404()
    data = request.get_json()

    if not data:
        return jsonify({'error': '请求体不能为空'}), 400

    if 'tapd_story_url' in data:
        case.tapd_story_url = data['tapd_story_url'] or ''
    if 'tapd_story_title' in data:
        case.tapd_story_title = data['tapd_story_title'] or ''

    case.updated_by = _operator()
    library.updated_by = _operator()
    db.session.commit()
    return jsonify(case.to_dict())


@api_bp.route('/testcase-libraries/<int:library_id>/cases/tapd-bind-batch', methods=['POST'])
def batch_bind_tapd(library_id):
    """批量绑定 TAPD 需求（按目录或用例集）

    请求体：
    {
        "tapd_story_url": "https://www.tapd.cn/...",
        "tapd_story_title": "需求标题",
        "module_path": "登录模块/手机号登录",   // 按目录批量绑定（二选一）
        "case_ids": [1, 2, 3]                    // 按用例 ID 批量绑定（二选一）
    }
    """
    library = TestCaseLibrary.query.get_or_404(library_id)
    data = request.get_json()

    if not data or not data.get('tapd_story_url'):
        return jsonify({'error': 'tapd_story_url 必填'}), 400

    tapd_url = data['tapd_story_url']
    tapd_title = data.get('tapd_story_title', '')
    module_path = data.get('module_path')
    case_ids = data.get('case_ids')

    query = TestCase.query.filter_by(library_id=library_id).filter(TestCase.is_placeholder != True)

    if case_ids:
        query = query.filter(TestCase.id.in_(case_ids))
    elif module_path is not None:
        if module_path == '':
            query = query.filter(db.or_(TestCase.module_path == '', TestCase.module_path == None))
        else:
            query = query.filter(db.or_(
                TestCase.module_path == module_path,
                TestCase.module_path.like(f'{module_path}/%')
            ))
    else:
        return jsonify({'error': '请指定 module_path 或 case_ids'}), 400

    cases = query.all()
    for c in cases:
        c.tapd_story_url = tapd_url
        c.tapd_story_title = tapd_title
        c.updated_by = _operator()

    library.updated_by = _operator()
    db.session.commit()
    return jsonify({'message': f'已为 {len(cases)} 条用例绑定 TAPD 需求', 'count': len(cases)})


@api_bp.route('/testcase-libraries/<int:library_id>/export/xmind', methods=['GET'])
def export_cases_xmind(library_id):
    """导出为 XMind 格式（.xmind 文件）"""
    from flask import Response
    import zipfile
    import io

    library = TestCaseLibrary.query.get_or_404(library_id)
    cases = library.cases.filter(TestCase.is_placeholder != True).order_by(TestCase.priority, TestCase.case_id).all()

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


# ====================================================================
# 共享授权 API（开放权限/邀请评审）
# 与 engineering /shares 五件套一一对应：list / create / revoke / public on / public off
# ====================================================================

_VALID_PERMISSIONS = ('readonly', 'reviewer', 'editor')


def _share_forbidden(action_zh):
    return jsonify({
        'error': f'无权{action_zh}该用例库：仅作者 / 项目管理员 / super_admin 可操作',
    }), 403


@api_bp.route('/testcase-libraries/<int:library_id>/shares', methods=['GET'])
def list_library_shares(library_id):
    """列出该用例库当前所有共享授权（含已过期，前端按 is_active 区分）。"""
    library = TestCaseLibrary.query.get_or_404(library_id)
    user, err = _ensure_library_access(library, write=False)
    if err:
        return err
    shares = (TestCaseLibraryShare.query
              .filter_by(library_id=library_id)
              .order_by(desc(TestCaseLibraryShare.created_at)).all())
    return jsonify({
        'library_id': library_id,
        'shares': [s.to_dict() for s in shares],
        'total': len(shares),
        'can_share': _can_share_library(user, library),
    })


@api_bp.route('/testcase-libraries/<int:library_id>/shares', methods=['POST'])
def create_library_share(library_id):
    """新增一条共享授权。
    请求体：
    {
      "share_type": "user" | "claw" | "public",
      "target_user_id":   123,    // share_type=user 时必填（互斥）
      "target_claw_id":   45,     // share_type=claw 时必填（互斥）
      "permission": "reviewer",   // readonly/reviewer/editor，默认 reviewer
      "expires_at": "2026-04-30 18:00:00",   // 可选；不填=永久
      "note": "邀请评审登录模块"               // 可选
    }
    幂等：相同 (library, share_type, target) 已存在且仍有效 → 刷新 permission/expires_at/note 后返回 200。
    """
    library = TestCaseLibrary.query.get_or_404(library_id)
    user = _get_current_user()
    if not user:
        return jsonify({'error': '未登录'}), 401
    if not _can_share_library(user, library):
        return _share_forbidden('共享')

    data = request.get_json() or {}
    share_type = data.get('share_type')
    if share_type not in ('user', 'claw', 'public'):
        return jsonify({'error': 'share_type 必须是 user/claw/public'}), 400

    permission = (data.get('permission') or 'reviewer').strip()
    if permission not in _VALID_PERMISSIONS:
        return jsonify({
            'error': f'permission 非法：{permission}（合法：{_VALID_PERMISSIONS}）'
        }), 400

    target_user_id = data.get('target_user_id')
    target_claw_id = data.get('target_claw_id')
    if share_type == 'user':
        if target_user_id is None or target_user_id == '':
            return jsonify({'error': 'share_type=user 时 target_user_id 必填'}), 400
        if not User.query.get(target_user_id):
            return jsonify({'error': '目标用户不存在'}), 404
        target_claw_id = None
    elif share_type == 'claw':
        if target_claw_id is None or target_claw_id == '':
            return jsonify({'error': 'share_type=claw 时 target_claw_id 必填'}), 400
        if not OpenClawInstance.query.get(target_claw_id):
            return jsonify({'error': '目标 OpenClaw 不存在'}), 404
        target_user_id = None
    else:  # public
        target_user_id = None
        target_claw_id = None

    try:
        expires_at = _parse_expires_at(data.get('expires_at'))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    note = (data.get('note') or '').strip()[:500]

    # 幂等：相同维度已存在且未过期 → 刷新后直接返回
    existing = (TestCaseLibraryShare.query
                .filter_by(library_id=library_id,
                           share_type=share_type,
                           target_user_id=target_user_id,
                           target_claw_id=target_claw_id)
                .all())
    for ex in existing:
        if ex.is_active():
            updated = False
            if expires_at and ex.expires_at != expires_at:
                ex.expires_at = expires_at
                updated = True
            if note and ex.note != note:
                ex.note = note
                updated = True
            if permission and ex.permission != permission:
                ex.permission = permission
                updated = True
            if updated:
                db.session.commit()
                log_action('share_grant', 'test_case_library', library_id,
                           library.name, operator=_operator(),
                           detail=(f'刷新共享 share_type={share_type} '
                                   f'permission={permission}'))
            return jsonify({**ex.to_dict(), 'idempotent': True}), 200

    share = TestCaseLibraryShare(
        library_id=library_id,
        share_type=share_type,
        target_user_id=target_user_id,
        target_claw_id=target_claw_id,
        permission=permission,
        granted_by=(getattr(user, 'username', None)
                    or getattr(user, '_claw_name', None) or 'system'),
        note=note,
        expires_at=expires_at,
    )
    db.session.add(share)
    db.session.commit()

    log_action('share_grant', 'test_case_library', library_id, library.name,
               operator=_operator(),
               detail=(f'share_type={share_type} '
                       f'target_user={target_user_id or "-"} '
                       f'target_claw={target_claw_id or "-"} '
                       f'permission={permission} '
                       f'expires_at={expires_at or "永久"} '
                       f'note={note[:80]}'))
    return jsonify(share.to_dict()), 201


@api_bp.route('/testcase-libraries/shares/<int:share_id>', methods=['DELETE'])
def revoke_library_share(share_id):
    """撤销共享授权。权限：原资源的可共享方。"""
    share = TestCaseLibraryShare.query.get_or_404(share_id)
    library = TestCaseLibrary.query.get(share.library_id)
    user = _get_current_user()
    if not user:
        return jsonify({'error': '未登录'}), 401
    if library is None or not _can_share_library(user, library):
        return _share_forbidden('撤销共享授权')

    info = (f'share_type={share.share_type} '
            f'target_user={share.target_user_id or "-"} '
            f'target_claw={share.target_claw_id or "-"} '
            f'permission={share.permission}')
    lib_id = share.library_id
    db.session.delete(share)
    db.session.commit()

    log_action('share_revoke', 'test_case_library', lib_id, library.name,
               operator=_operator(), detail=info)
    return jsonify({'status': 'revoked', 'share_id': share_id})


@api_bp.route('/testcase-libraries/<int:library_id>/shares/public', methods=['POST'])
def toggle_library_public_on(library_id):
    """一键全部开放（语义：所有登录用户/claw 可见）。幂等。
    可选 body: {"expires_at": "...", "note": "...", "permission": "reviewer"}
    """
    library = TestCaseLibrary.query.get_or_404(library_id)
    user = _get_current_user()
    if not user:
        return jsonify({'error': '未登录'}), 401
    if not _can_share_library(user, library):
        return _share_forbidden('开放')

    data = request.get_json() or {}
    permission = (data.get('permission') or 'reviewer').strip()
    if permission not in _VALID_PERMISSIONS:
        return jsonify({'error': f'permission 非法：{permission}'}), 400
    try:
        expires_at = _parse_expires_at(data.get('expires_at'))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    note = (data.get('note') or '').strip()[:500]

    existing = (TestCaseLibraryShare.query
                .filter_by(library_id=library_id,
                           share_type='public',
                           target_user_id=None, target_claw_id=None)
                .first())
    if existing and existing.is_active():
        changed = False
        if expires_at and existing.expires_at != expires_at:
            existing.expires_at = expires_at
            changed = True
        if permission and existing.permission != permission:
            existing.permission = permission
            changed = True
        if changed:
            db.session.commit()
        return jsonify({**existing.to_dict(), 'idempotent': True}), 200

    if existing:  # 已过期 → 复用记录刷新
        existing.expires_at = expires_at
        existing.permission = permission
        existing.granted_by = (getattr(user, 'username', None)
                               or getattr(user, '_claw_name', None) or 'system')
        existing.note = note
        db.session.commit()
        log_action('share_grant', 'test_case_library', library_id, library.name,
                   operator=_operator(),
                   detail=f'public 开放（复用过期记录）permission={permission} expires_at={expires_at or "永久"}')
        return jsonify(existing.to_dict()), 200

    share = TestCaseLibraryShare(
        library_id=library_id,
        share_type='public',
        permission=permission,
        granted_by=(getattr(user, 'username', None)
                    or getattr(user, '_claw_name', None) or 'system'),
        note=note,
        expires_at=expires_at,
    )
    db.session.add(share)
    db.session.commit()
    log_action('share_grant', 'test_case_library', library_id, library.name,
               operator=_operator(),
               detail=f'public 开放 permission={permission} expires_at={expires_at or "永久"}')
    return jsonify(share.to_dict()), 201


@api_bp.route('/testcase-libraries/<int:library_id>/shares/public', methods=['DELETE'])
def toggle_library_public_off(library_id):
    """一键关闭全部开放（删除该库所有 share_type=public 记录）。"""
    library = TestCaseLibrary.query.get_or_404(library_id)
    user = _get_current_user()
    if not user:
        return jsonify({'error': '未登录'}), 401
    if not _can_share_library(user, library):
        return _share_forbidden('关闭全部开放')

    deleted = (TestCaseLibraryShare.query
               .filter_by(library_id=library_id, share_type='public')
               .delete(synchronize_session=False))
    db.session.commit()
    log_action('share_revoke', 'test_case_library', library_id, library.name,
               operator=_operator(),
               detail=f'关闭 public 开放 deleted={deleted}')
    return jsonify({'status': 'closed', 'deleted': deleted})


# ====================================================================
# 评审流程 API
# ====================================================================

_VALID_REVIEW_STATUSES = ('draft', 'pending_review', 'approved', 'rejected')


def _set_library_review_status(library, new_status, current_review_id=None):
    """统一更新 library.review_status + review_status_at + current_review_id。"""
    library.review_status = new_status
    library.review_status_at = _now()
    if current_review_id is not None:
        library.current_review_id = current_review_id
    elif new_status != 'pending_review':
        library.current_review_id = None


def _sync_topic_review_round(review, new_round_status):
    """从 testcases 侧操作审批时，反向同步关联课题的评审轮次状态。

    当通过 testcases.html 直接审批/驳回 review 时，确保课题的最新
    review round 状态也同步更新。
    """
    if not review.related_topic_id:
        return
    topic = Topic.query.get(review.related_topic_id)
    if not topic:
        return
    # 更新最新轮次状态
    latest_round = CaseReviewRound.query.filter_by(
        topic_id=topic.id
    ).order_by(CaseReviewRound.round_number.desc()).first()
    if latest_round and latest_round.status == 'pending':
        latest_round.status = new_round_status
    # 如果是最终审批通过/驳回，也关闭课题评审状态
    if new_round_status in ('approved', 'rejected'):
        topic.review_status = 'closed'


def _scope_case_filter(library_id, scope_type, scope_module_path,
                       include_placeholders=False):
    """构造评审范围内的用例查询。

    目录范围按前缀匹配整棵子树，与 `get_library_cases` 的 module_path 过滤保持一致。
    """
    query = TestCase.query.filter_by(library_id=library_id)
    if not include_placeholders:
        query = query.filter(db.or_(
            TestCase.is_placeholder.is_(False),
            TestCase.is_placeholder.is_(None),
        ))
    if scope_type == 'module':
        path = (scope_module_path or '').strip()
        if path:
            query = query.filter(db.or_(
                TestCase.module_path == path,
                TestCase.module_path.like(path + '/%'),
            ))
        else:
            # 空路径代表"(未分类)"根目录
            query = query.filter(db.or_(
                TestCase.module_path == '',
                TestCase.module_path.is_(None),
            ))
    return query


def _normalize_review_scope(data):
    """解析评审范围入参，返回 (scope_type, scope_module_path, error_message)。"""
    scope_type = (data.get('scope_type') or 'library').strip()
    if scope_type not in ('library', 'module'):
        return None, None, 'scope_type 只支持 library 或 module'

    if scope_type == 'library':
        return 'library', '', None

    # module 范围允许空字符串（代表"(未分类)"根目录），但字段必须显式出现
    if 'scope_module_path' not in data:
        return None, None, 'scope_type=module 时必须提供 scope_module_path'
    path = str(data.get('scope_module_path') or '').strip().strip('/')
    if len(path) > 500:
        return None, None, 'scope_module_path 超长（上限 500 字符）'
    return 'module', path, None


@api_bp.route('/testcase-libraries/<int:library_id>/reviews', methods=['GET'])
def list_library_reviews(library_id):
    """列出某用例库的全部评审记录（含历史）。"""
    library = TestCaseLibrary.query.get_or_404(library_id)
    _, err = _ensure_library_access(library, write=False)
    if err:
        return err
    reviews = (TestCaseLibraryReview.query
               .filter_by(library_id=library_id)
               .order_by(desc(TestCaseLibraryReview.created_at)).all())
    return jsonify({
        'library_id': library_id,
        'review_status': library.review_status or 'draft',
        'current_review_id': library.current_review_id,
        'reviews': [r.to_dict() for r in reviews],
        'total': len(reviews),
    })


@api_bp.route('/testcase-libraries/<int:library_id>/reviews', methods=['POST'])
def submit_library_review(library_id):
    """发起一次评审请求。
    请求体（全部可选）：
    {
      "scope_type": "module",                     # library=整库(默认) / module=某目录子树
      "scope_module_path": "登录模块/手机号登录",   # scope_type=module 时必填（空串=未分类根目录）
      "visibility": "public_all",                 # 课题可见范围四档，默认 public
      "grants": [{"type":"user","id":12}],        # visibility=assigned 时必填
      "submit_note": "本次重点评审登录与支付模块",
      "scope_summary": "登录 / 支付",
      "invited_reviewers": [{"type":"user","id":12,"name":"alice"}, ...],
      "related_topic_id": 87
    }
    权限：作者 / 项目 admin / super_admin。
    """
    from app.api.topics import _sync_topic_grants
    from app.services.review_visibility import normalize_visibility

    library = TestCaseLibrary.query.get_or_404(library_id)
    user = _get_current_user()
    if not user:
        return jsonify({'error': '未登录'}), 401
    if not _can_manage_library(user, library):
        return jsonify({'error': '无权对该用例库发起评审：仅作者/项目管理员/super_admin'}), 403

    data = request.get_json() or {}
    scope_type, scope_module_path, scope_err = _normalize_review_scope(data)
    if scope_err:
        return jsonify({'error': scope_err}), 400

    # 目录范围必须真实存在（含占位用例），否则等于评审一个空范围
    if scope_type == 'module':
        exists = _scope_case_filter(library_id, 'module', scope_module_path,
                                    include_placeholders=True).count()
        if not exists:
            return jsonify({
                'error': '目录不存在或为空：%s' % (scope_module_path or '(未分类)'),
            }), 400

    # 防重复下沉到"库 + 范围"：不同目录可以并行评审，同一目录不行
    dup = TestCaseLibraryReview.query.filter_by(
        library_id=library_id,
        status='submitted',
        scope_type=scope_type,
        scope_module_path=scope_module_path,
    ).first()
    if dup:
        return jsonify({
            'error': '该范围已有进行中的评审（#%d），请先撤回或等待审批' % dup.id,
            'current_review_id': dup.id,
            'related_topic_id': dup.related_topic_id,
        }), 409

    visibility = normalize_visibility(data.get('visibility'))
    grants = data.get('grants')
    if visibility == 'assigned' and not grants:
        return jsonify({'error': '指定范围的评审必须至少指定一个用户或 Agent'}), 400

    op_name = _operator()
    submit_note = (data.get('submit_note') or '').strip()
    scope_summary = (data.get('scope_summary') or '').strip()[:500]
    if not scope_summary:
        scope_summary = ('目录：%s' % (scope_module_path or '(未分类)')
                         if scope_type == 'module' else '整库评审')

    case_count = _scope_case_filter(library_id, scope_type,
                                    scope_module_path).count()

    review = TestCaseLibraryReview(
        library_id=library_id,
        status='submitted',
        submitted_by=op_name,
        submitted_at=_now(),
        submit_note=submit_note,
        scope_summary=scope_summary,
        scope_type=scope_type,
        scope_module_path=scope_module_path,
        scope_case_count=case_count,
        invited_reviewers=data.get('invited_reviewers') or [],
        related_topic_id=data.get('related_topic_id'),
    )
    db.session.add(review)
    db.session.flush()

    # --- 自动创建 case_review 课题（如果没有 related_topic_id）---
    if not review.related_topic_id:
        lib_label = library.name or '用例库#%d' % library_id
        if scope_type == 'module':
            topic_title = '用例评审：%s / %s' % (
                lib_label, scope_module_path or '(未分类)')
        else:
            topic_title = '用例评审：%s' % lib_label
        topic_content = submit_note or scope_summary
        topic = Topic(
            title=topic_title[:200],
            content=topic_content,
            board='case_review',
            author_claw_id=getattr(user, 'bound_claw_id', None),
            author_user_id=getattr(user, 'id', None),
            author_name=op_name,
            project_name=library.project_name,
            visibility=visibility,
            review_library_id=library_id,
            review_module_paths=([scope_module_path]
                                 if scope_type == 'module' else None),
            review_status='reviewing',
        )
        db.session.add(topic)
        db.session.flush()
        review.related_topic_id = topic.id

        if visibility == 'assigned':
            added, _skipped = _sync_topic_grants(topic, grants, op_name)
            if not added:
                db.session.rollback()
                return jsonify({
                    'error': '指定的用户或 Agent 均无效，请重新选择',
                }), 400

    _set_library_review_status(library, 'pending_review',
                               current_review_id=review.id)

    # --- 自动创建评审轮次（CaseReviewRound），以支持评审评分 ---
    if review.related_topic_id:
        # 关闭旧的 pending 轮次（确保同一时刻只有一轮开放）
        old_pending = CaseReviewRound.query.filter_by(
            topic_id=review.related_topic_id, status='pending').all()
        for opr in old_pending:
            opr.status = 'rejected'
        existing_max = db.session.query(
            db.func.coalesce(db.func.max(CaseReviewRound.round_number), 0)
        ).filter_by(topic_id=review.related_topic_id).scalar()
        round_obj = CaseReviewRound(
            topic_id=review.related_topic_id,
            round_number=(existing_max or 0) + 1,
            description=submit_note or scope_summary or '用例库评审',
            status='pending',
            submitted_by=op_name,
        )
        db.session.add(round_obj)

    db.session.commit()

    log_action('submit_review', 'test_case_library', library_id, library.name,
               operator=op_name,
               detail=(f'review#{review.id} scope={scope_type}:{scope_module_path} '
                       f'cases={case_count} visibility={visibility} '
                       f'invited={len(review.invited_reviewers or [])} '
                       f'topic={review.related_topic_id}'))
    return jsonify(review.to_dict()), 201


@api_bp.route('/testcase-libraries/reviews/<int:review_id>/approve',
              methods=['POST'])
def approve_library_review(review_id):
    """通过评审。权限：可管理 OR 被授权为 reviewer/editor。"""
    review = TestCaseLibraryReview.query.get_or_404(review_id)
    library = TestCaseLibrary.query.get_or_404(review.library_id)
    user = _get_current_user()
    if not user:
        return jsonify({'error': '未登录'}), 401
    share_grants = _list_active_library_share_grants(user)
    if not _can_review_library(user, library, share_grants):
        return jsonify({
            'error': '无权审批该评审：需具备评审权限（作者 / 项目管理员 / super_admin / 被授权 reviewer）',
        }), 403
    if review.status != 'submitted':
        return jsonify({
            'error': f'当前评审状态 {review.status} 不可审批通过'
        }), 400

    data = request.get_json() or {}
    review.status = 'approved'
    review.decided_by = _operator()
    review.decided_at = _now()
    review.decision_note = (data.get('decision_note') or '').strip()
    _set_library_review_status(library, 'approved')
    _sync_topic_review_round(review, 'approved')
    db.session.commit()

    log_action('approve_review', 'test_case_library', library.id, library.name,
               operator=_operator(),
               detail=(f'review#{review.id} note={review.decision_note[:80]}'))
    return jsonify(review.to_dict())


@api_bp.route('/testcase-libraries/reviews/<int:review_id>/reject',
              methods=['POST'])
def reject_library_review(review_id):
    """驳回评审。权限：同 approve。"""
    review = TestCaseLibraryReview.query.get_or_404(review_id)
    library = TestCaseLibrary.query.get_or_404(review.library_id)
    user = _get_current_user()
    if not user:
        return jsonify({'error': '未登录'}), 401
    share_grants = _list_active_library_share_grants(user)
    if not _can_review_library(user, library, share_grants):
        return jsonify({
            'error': '无权驳回该评审：需具备评审权限',
        }), 403
    if review.status != 'submitted':
        return jsonify({
            'error': f'当前评审状态 {review.status} 不可驳回'
        }), 400

    data = request.get_json() or {}
    review.status = 'rejected'
    review.decided_by = _operator()
    review.decided_at = _now()
    review.decision_note = (data.get('decision_note') or '').strip()
    _set_library_review_status(library, 'rejected')
    _sync_topic_review_round(review, 'rejected')
    db.session.commit()

    log_action('reject_review', 'test_case_library', library.id, library.name,
               operator=_operator(),
               detail=(f'review#{review.id} reason={review.decision_note[:80]}'))
    return jsonify(review.to_dict())


@api_bp.route('/testcase-libraries/reviews/<int:review_id>/withdraw',
              methods=['POST'])
def withdraw_library_review(review_id):
    """撤回评审请求。仅发起者本人 / 库管理者可操作。"""
    review = TestCaseLibraryReview.query.get_or_404(review_id)
    library = TestCaseLibrary.query.get_or_404(review.library_id)
    user = _get_current_user()
    if not user:
        return jsonify({'error': '未登录'}), 401

    candidates = {user.username, getattr(user, '_claw_name', None), _operator()}
    candidates.discard(None)
    is_submitter = review.submitted_by in candidates
    if not (is_submitter or _can_manage_library(user, library)):
        return jsonify({'error': '无权撤回该评审：仅发起人 / 用例库管理者'}), 403
    if review.status != 'submitted':
        return jsonify({
            'error': f'当前评审状态 {review.status} 不可撤回'
        }), 400

    review.status = 'withdrawn'
    review.decided_by = _operator()
    review.decided_at = _now()
    # 库状态回到上一个稳定态：之前没批准过 → draft；之前批准过 → 仍 approved
    prev_approved = TestCaseLibraryReview.query.filter_by(
        library_id=library.id, status='approved').first()
    if prev_approved:
        _set_library_review_status(library, 'approved')
    else:
        _set_library_review_status(library, 'draft')
    db.session.commit()

    log_action('withdraw_review', 'test_case_library', library.id, library.name,
               operator=_operator(),
               detail=f'review#{review.id} 撤回')
    return jsonify(review.to_dict())


@api_bp.route('/testcase-libraries/reviews/<int:review_id>', methods=['GET'])
def get_library_review(review_id):
    """单条评审详情（含发起说明、邀请名单、审批意见）。"""
    review = TestCaseLibraryReview.query.get_or_404(review_id)
    library = TestCaseLibrary.query.get_or_404(review.library_id)
    user, err = _ensure_library_access(library, write=False)
    if err:
        return err
    data = review.to_dict()
    data['library_name'] = library.name
    data['can_decide'] = _can_review_library(user, library)
    return jsonify(data)


@api_bp.route('/testcase-libraries/reviews/<int:review_id>/close',
              methods=['POST'])
def close_library_review(review_id):
    """关闭单条评审（关闭审核）。将该条 review 设为 closed，
    库状态回退到上一个稳定态。"""
    review = TestCaseLibraryReview.query.get_or_404(review_id)
    library = TestCaseLibrary.query.get_or_404(review.library_id)
    user = _get_current_user()
    if not user:
        return jsonify({'error': '未登录'}), 401
    if not _can_manage_library(user, library):
        return jsonify({'error': '无权关闭该评审'}), 403
    if review.status not in ('submitted',):
        return jsonify({'error': '当前评审状态 %s 不可关闭' % review.status}), 400

    review.status = 'closed'
    review.decided_by = _operator()
    review.decided_at = _now()

    # 库状态回退
    prev_approved = TestCaseLibraryReview.query.filter(
        TestCaseLibraryReview.library_id == library.id,
        TestCaseLibraryReview.status == 'approved',
        TestCaseLibraryReview.id != review.id).first()
    if prev_approved:
        _set_library_review_status(library, 'approved')
    else:
        _set_library_review_status(library, 'draft')
    db.session.commit()

    log_action('close_review', 'test_case_library', library.id, library.name,
               operator=_operator(),
               detail='review#%d 关闭审核' % review.id)
    return jsonify(review.to_dict())


@api_bp.route('/testcase-libraries/<int:library_id>/close-review',
              methods=['POST'])
def close_library_review_topic(library_id):
    """关闭整个评审课题：关闭当前进行中的 review 并关闭关联的课题。"""
    library = TestCaseLibrary.query.get_or_404(library_id)
    user = _get_current_user()
    if not user:
        return jsonify({'error': '未登录'}), 401
    if not _can_manage_library(user, library):
        return jsonify({'error': '无权关闭评审'}), 403

    # 关闭所有进行中的 review
    open_reviews = TestCaseLibraryReview.query.filter_by(
        library_id=library_id, status='submitted').all()
    topic_ids = set()
    for rv in open_reviews:
        rv.status = 'closed'
        rv.decided_by = _operator()
        rv.decided_at = _now()
        if rv.related_topic_id:
            topic_ids.add(rv.related_topic_id)

    # 关闭关联的课题（同时关闭 status 和 review_status）
    for tid in topic_ids:
        topic = Topic.query.get(tid)
        if topic:
            if topic.status != 'closed':
                topic.status = 'closed'
            if hasattr(topic, 'review_status') and topic.review_status != 'closed':
                topic.review_status = 'closed'

    # 库状态回退
    prev_approved = TestCaseLibraryReview.query.filter_by(
        library_id=library_id, status='approved').first()
    if prev_approved:
        _set_library_review_status(library, 'approved')
    else:
        _set_library_review_status(library, 'draft')
    db.session.commit()

    log_action('close_review_topic', 'test_case_library', library.id,
               library.name, operator=_operator(),
               detail='关闭整个评审课题 reviews=%d topics=%s' % (
                   len(open_reviews), list(topic_ids)))
    return jsonify({'message': '评审课题已关闭', 'closed_reviews': len(open_reviews)})
