from flask import request, jsonify, session
from app import db
from app.models import Project, Module, User
from app.api import api_bp


# ==================== 项目管理 ====================

def _filter_projects_for_user(projects):
    """根据当前登录用户/Token 过滤项目列表。

    规则：
    - super_admin（用户）/ admin claw（如龙虾王，全平台）：看全部
    - admin（用户）：只看 managed_projects 中的项目
    - 其他用户：看 managed_projects + 绑定 claw 所属项目
    - 未登录：看全部（兼容性）
    """
    from app.api.auth_utils import get_current_user
    user = get_current_user()
    if not user:
        # 兼容未登录场景，返回全部（前端有自己的过滤）
        return projects

    role = getattr(user, 'role', None)

    # super_admin 或全平台 admin claw
    if role == 'super_admin' or getattr(user, 'is_global', False):
        return projects

    user_project_ids = set()
    for pid in (getattr(user, 'managed_projects', None) or []):
        try:
            user_project_ids.add(int(pid))
        except Exception:
            continue

    bound_claw_id = getattr(user, 'bound_claw_id', None)
    if bound_claw_id:
        from app.models import OpenClawInstance
        claw = OpenClawInstance.query.get(bound_claw_id)
        if claw:
            if claw.project_id:
                user_project_ids.add(int(claw.project_id))
            elif claw.project_name:
                p = Project.query.filter_by(name=claw.project_name).first()
                if p:
                    user_project_ids.add(p.id)

    return [p for p in projects if p.id in user_project_ids]


@api_bp.route('/projects', methods=['GET'])
def list_projects():
    """获取项目列表
    super_admin: 看全部项目
    admin: 只看自己 managed_projects 中的项目
    其他用户: 只看自己关联的项目（managed_projects 或 bound_claw 的项目）
    """
    projects = Project.query.order_by(Project.name).all()
    filtered = _filter_projects_for_user(projects)
    return jsonify([p.to_dict() for p in filtered])


@api_bp.route('/projects', methods=['POST'])
def create_project():
    """创建项目"""
    data = request.get_json()
    if not data or not data.get('name'):
        return jsonify({'error': '项目名称为必填项'}), 400

    if Project.query.filter_by(name=data['name']).first():
        return jsonify({'error': f'项目 "{data["name"]}" 已存在'}), 409

    project = Project(
        name=data['name'],
        description=data.get('description'),
        tapd_workspace_id=data.get('tapd_workspace_id'),
    )
    db.session.add(project)
    db.session.commit()
    return jsonify(project.to_dict()), 201


@api_bp.route('/projects/<int:project_id>', methods=['PUT'])
def update_project(project_id):
    """更新项目"""
    project = Project.query.get_or_404(project_id)
    data = request.get_json()

    if 'name' in data:
        project.name = data['name']
    if 'description' in data:
        project.description = data['description']
    if 'tapd_workspace_id' in data:
        project.tapd_workspace_id = data['tapd_workspace_id']

    db.session.commit()
    return jsonify(project.to_dict())


@api_bp.route('/projects/<int:project_id>', methods=['DELETE'])
def delete_project(project_id):
    """删除项目"""
    project = Project.query.get_or_404(project_id)
    db.session.delete(project)
    db.session.commit()
    return jsonify({'message': f'项目 "{project.name}" 已删除'})


# ==================== 模块管理（独立于项目） ====================

MODULE_CATEGORIES = {
    'peripheral': '外围系统',
    'core_gameplay': '核心单局',
    'commercialization': '商业化',
    'client_performance': '客户端性能',
    'server_special': '服务器专项',
    'other': '其他专项',
}


@api_bp.route('/modules', methods=['GET'])
def list_all_modules():
    """获取所有模块（独立列表）"""
    category = request.args.get('category')
    query = Module.query
    if category:
        query = query.filter_by(category=category)
    modules = query.order_by(Module.category, Module.name).all()
    return jsonify([m.to_dict() for m in modules])


@api_bp.route('/modules', methods=['POST'])
def create_standalone_module():
    """创建独立模块"""
    data = request.get_json()
    if not data or not data.get('name'):
        return jsonify({'error': '模块名称为必填项'}), 400

    if Module.query.filter_by(name=data['name']).first():
        return jsonify({'error': f'模块 "{data["name"]}" 已存在'}), 409

    module = Module(
        name=data['name'],
        description=data.get('description'),
        category=data.get('category', 'other'),
        project_id=data.get('project_id'),
    )
    db.session.add(module)
    db.session.commit()
    return jsonify(module.to_dict()), 201


@api_bp.route('/modules/<int:module_id>', methods=['PUT'])
def update_module(module_id):
    """更新模块"""
    module = Module.query.get_or_404(module_id)
    data = request.get_json()

    for field in ['name', 'description', 'category', 'project_id']:
        if field in data:
            setattr(module, field, data[field])

    db.session.commit()
    return jsonify(module.to_dict())


@api_bp.route('/modules/<int:module_id>', methods=['DELETE'])
def delete_module(module_id):
    """删除模块"""
    module = Module.query.get_or_404(module_id)
    db.session.delete(module)
    db.session.commit()
    return jsonify({'message': f'模块 "{module.name}" 已删除'})


@api_bp.route('/modules/categories', methods=['GET'])
def list_module_categories():
    """获取模块分类列表"""
    return jsonify(MODULE_CATEGORIES)


# ==================== 向后兼容：项目下模块 ====================

@api_bp.route('/projects/<int:project_id>/modules', methods=['GET'])
def list_modules(project_id):
    """获取项目关联的模块"""
    project = Project.query.get_or_404(project_id)
    modules = Module.query.filter_by(project_id=project_id).order_by(Module.name).all()
    return jsonify([m.to_dict() for m in modules])


@api_bp.route('/projects/<int:project_id>/modules', methods=['POST'])
def create_module(project_id):
    """为项目添加模块（向后兼容）"""
    Project.query.get_or_404(project_id)
    data = request.get_json()

    if not data or not data.get('name'):
        return jsonify({'error': '模块名称为必填项'}), 400

    existing = Module.query.filter_by(name=data['name']).first()
    if existing:
        return jsonify({'error': f'模块 "{data["name"]}" 已存在'}), 409

    module = Module(
        project_id=project_id,
        name=data['name'],
        description=data.get('description'),
        category=data.get('category', 'other'),
    )
    db.session.add(module)
    db.session.commit()
    return jsonify(module.to_dict()), 201
