from flask import request, jsonify
from app import db
from app.models import Project, Module
from app.api import api_bp


@api_bp.route('/projects', methods=['GET'])
def list_projects():
    """获取项目列表"""
    projects = Project.query.order_by(Project.name).all()
    return jsonify([p.to_dict() for p in projects])


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


@api_bp.route('/projects/<int:project_id>/modules', methods=['GET'])
def list_modules(project_id):
    """获取项目模块"""
    project = Project.query.get_or_404(project_id)
    modules = project.modules.order_by(Module.name).all()
    return jsonify([m.to_dict() for m in modules])


@api_bp.route('/projects/<int:project_id>/modules', methods=['POST'])
def create_module(project_id):
    """添加模块"""
    project = Project.query.get_or_404(project_id)
    data = request.get_json()

    if not data or not data.get('name'):
        return jsonify({'error': '模块名称为必填项'}), 400

    existing = Module.query.filter_by(
        project_id=project_id, name=data['name']
    ).first()
    if existing:
        return jsonify({'error': f'模块 "{data["name"]}" 已存在'}), 409

    module = Module(
        project_id=project_id,
        name=data['name'],
        description=data.get('description'),
    )
    db.session.add(module)
    db.session.commit()
    return jsonify(module.to_dict()), 201
