"""用户认证与管理 API"""
from flask import request, jsonify, session
from app import db
from app.models import User
from app.api import api_bp

VALID_ROLES = ('super_admin', 'admin', 'user', 'guest')


@api_bp.route('/auth/register', methods=['POST'])
def register():
    """用户注册"""
    data = request.get_json()
    username = (data.get('username') or '').strip()
    password = (data.get('password') or '').strip()
    display_name = (data.get('display_name') or '').strip()

    if not username or not password:
        return jsonify({'error': '用户名和密码不能为空'}), 400
    if len(password) < 4:
        return jsonify({'error': '密码至少4位'}), 400

    if User.query.filter_by(username=username).first():
        return jsonify({'error': f'用户名 "{username}" 已存在'}), 409

    user = User(
        username=username,
        display_name=display_name or username,
        role='super_admin' if username == 'rajqiu' else 'user',
    )
    user.set_password(password)
    db.session.add(user)
    db.session.commit()

    session['user_id'] = user.id
    return jsonify({'message': '注册成功', 'user': user.to_dict()}), 201


@api_bp.route('/auth/login', methods=['POST'])
def login():
    """用户登录"""
    data = request.get_json()
    username = (data.get('username') or '').strip()
    password = (data.get('password') or '').strip()

    if not username or not password:
        return jsonify({'error': '用户名和密码不能为空'}), 400

    user = User.query.filter_by(username=username).first()
    if not user or not user.check_password(password):
        return jsonify({'error': '用户名或密码错误'}), 401

    session['user_id'] = user.id
    return jsonify({'message': '登录成功', 'user': user.to_dict()})


@api_bp.route('/auth/logout', methods=['POST'])
def logout():
    """登出"""
    session.pop('user_id', None)
    return jsonify({'message': '已登出'})


@api_bp.route('/auth/me', methods=['GET'])
def get_current_user():
    """获取当前登录用户"""
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': '未登录'}), 401
    user = User.query.get(user_id)
    if not user:
        session.pop('user_id', None)
        return jsonify({'error': '用户不存在'}), 401

    data = user.to_dict()

    # 解析负责项目名称（供前端项目筛选使用）
    from app.models import Project
    managed_ids = user.managed_projects or []
    managed_names = []
    if managed_ids:
        for pid in managed_ids:
            p = Project.query.get(pid)
            if p:
                managed_names.append(p.name)
    # 如果是普通用户且绑定了 claw，从 claw 推断项目
    if not managed_names and user.bound_claw_id:
        from app.models import OpenClawInstance
        claw = OpenClawInstance.query.get(user.bound_claw_id)
        if claw:
            pname = claw.project.name if claw.project else claw.project_name
            if pname:
                managed_names.append(pname)
    data['managed_project_names'] = managed_names

    # 权限信息
    role = user.role or 'user'
    is_sa = role == 'super_admin'
    is_admin = role == 'admin'
    is_user = role == 'user'

    data['permissions'] = {
        # 菜单可见性
        'nav_settings': is_sa or is_admin,
        'nav_audit_logs': is_sa or is_admin,
        'nav_hub': is_sa or is_admin,
        'nav_users': is_sa or is_admin,
        # 功能权限
        'can_create_openclaw': is_sa or is_admin,
        'can_delete_openclaw': is_sa or is_admin,
        'can_assign_skills': is_sa or is_admin,
        'can_assign_rules': is_sa or is_admin,
        'can_review': is_sa or is_admin,
        'can_edit_knowledge': is_sa or is_admin or is_user,
        'can_edit_testcases': is_sa or is_admin or is_user,
        'can_see_all_projects': is_sa,
        'can_see_all_claws': is_sa or is_admin,  # admin 看到全部但只能操作本项目
        'can_see_admin_claw': is_sa,  # 超级管理员可见龙虾王
        'can_see_tokens': is_sa or is_admin,  # 成员只能看自己绑定的 token
    }

    return jsonify(data)


@api_bp.route('/auth/change-password', methods=['POST'])
def change_password():
    """修改自己的密码（临时功能，OA 接入后移除）"""
    uid = session.get('user_id')
    if not uid:
        return jsonify({'error': '未登录'}), 401
    user = User.query.get(uid)
    if not user:
        return jsonify({'error': '用户不存在'}), 401

    data = request.get_json()
    old_pwd = (data.get('old_password') or '').strip()
    new_pwd = (data.get('new_password') or '').strip()

    if not old_pwd or not new_pwd:
        return jsonify({'error': '新旧密码不能为空'}), 400
    if len(new_pwd) < 4:
        return jsonify({'error': '新密码至少4位'}), 400
    if not user.check_password(old_pwd):
        return jsonify({'error': '原密码错误'}), 400

    user.set_password(new_pwd)
    db.session.commit()
    return jsonify({'message': '密码修改成功'})


# ============== 权限辅助函数 ==============

def _get_operator():
    """获取当前操作者"""
    uid = session.get('user_id')
    if not uid:
        return None
    return User.query.get(uid)


def _require_super_admin():
    """检查当前用户是否为管理员（super_admin 或 admin 同等权限）"""
    uid = session.get('user_id')
    if not uid:
        return None, (jsonify({'error': '未登录'}), 401)
    user = User.query.get(uid)
    if not user or user.role not in ('super_admin', 'admin'):
        return None, (jsonify({'error': '需要管理员权限'}), 403)
    return user, None


def _require_admin_or_above():
    """检查当前用户是否为 admin 或 super_admin"""
    uid = session.get('user_id')
    if not uid:
        return None, (jsonify({'error': '未登录'}), 401)
    user = User.query.get(uid)
    if not user or user.role not in ('super_admin', 'admin'):
        return None, (jsonify({'error': '需要管理员权限'}), 403)
    return user, None


# ============== 用户管理 ==============

@api_bp.route('/users', methods=['GET'])
def list_users():
    """
    获取用户列表
    - super_admin: 看所有用户
    - admin: 只看与自己同项目的用户
    """
    operator, err = _require_admin_or_above()
    if err:
        return err

    users = User.query.order_by(User.created_at).all()

    if operator.role in ('super_admin', 'admin'):
        return jsonify([u.to_dict() for u in users])

    # 普通用户：筛选同项目用户
    # 收集操作者关联的项目 ID
    from app.models import OpenClawInstance, Project
    operator_project_ids = set()
    if operator.managed_projects:
        operator_project_ids.update(operator.managed_projects)
    if operator.bound_claw_id:
        claw = OpenClawInstance.query.get(operator.bound_claw_id)
        if claw:
            if claw.project_id:
                operator_project_ids.add(claw.project_id)
            elif claw.project_name:
                p = Project.query.filter_by(name=claw.project_name).first()
                if p:
                    operator_project_ids.add(p.id)

    # 过滤：用户的 managed_projects 或 bound_claw 项目与操作者有交集
    filtered = []
    for u in users:
        # 自身始终可见（超管用户不显示给其他人）
        if u.id == operator.id:
            filtered.append(u)
            continue
        u_project_ids = set()
        if u.managed_projects:
            u_project_ids.update(u.managed_projects)
        if u.bound_claw_id:
            claw = OpenClawInstance.query.get(u.bound_claw_id)
            if claw:
                if claw.project_id:
                    u_project_ids.add(claw.project_id)
                elif claw.project_name:
                    p = Project.query.filter_by(name=claw.project_name).first()
                    if p:
                        u_project_ids.add(p.id)
        if operator_project_ids & u_project_ids:
            filtered.append(u)

    return jsonify([u.to_dict() for u in filtered])


@api_bp.route('/users', methods=['POST'])
def create_user():
    """管理员添加用户，默认密码 123456"""
    operator, err = _require_admin_or_above()
    if err:
        return err

    data = request.get_json()
    username = (data.get('username') or '').strip()
    display_name = (data.get('display_name') or '').strip()
    role = data.get('role', 'user')
    managed_projects = data.get('managed_projects') or []

    if not username:
        return jsonify({'error': '用户名不能为空'}), 400

    if User.query.filter_by(username=username).first():
        return jsonify({'error': f'用户名 "{username}" 已存在'}), 409

    # admin 不能创建 super_admin / admin
    if operator.role == 'admin' and role in ('super_admin', 'admin'):
        return jsonify({'error': 'admin 只能创建成员或访客'}), 403

    if role not in VALID_ROLES:
        role = 'user'

    user = User(
        username=username,
        display_name=display_name or username,
        role=role,
        managed_projects=managed_projects,
    )
    user.set_password(data.get('password', '123456'))
    db.session.add(user)
    db.session.commit()

    return jsonify({'message': f'用户 {username} 已创建（默认密码：123456）', 'user': user.to_dict()}), 201


@api_bp.route('/users/<int:user_id>', methods=['PUT'])
def update_user(user_id):
    """
    更新用户信息
    - super_admin: 可改所有人的角色、项目
    - admin: 只能改 user/guest 角色的用户，不能提升为 admin/super_admin
    """
    operator, err = _require_admin_or_above()
    if err:
        return err

    target = User.query.get_or_404(user_id)
    data = request.get_json()

    # 不允许修改自己的角色
    if target.id == operator.id and 'role' in data:
        return jsonify({'error': '不能修改自己的角色'}), 400

    # admin 不能修改 super_admin 或其他 admin
    if operator.role == 'admin':
        if target.role in ('super_admin', 'admin'):
            return jsonify({'error': '无权修改管理员'}), 403
        # admin 只能设置 user 或 guest
        if 'role' in data and data['role'] not in ('user', 'guest'):
            return jsonify({'error': 'admin 只能设置成员或访客角色'}), 403

    if 'role' in data and data['role'] in VALID_ROLES:
        target.role = data['role']
    if 'managed_projects' in data:
        target.managed_projects = data['managed_projects']
    if 'display_name' in data:
        target.display_name = data['display_name']
    if 'bound_claw_id' in data:
        target.bound_claw_id = data['bound_claw_id']

    db.session.commit()
    return jsonify(target.to_dict())


@api_bp.route('/users/<int:user_id>/reset-password', methods=['POST'])
def reset_user_password(user_id):
    """管理员重置用户密码（临时功能）"""
    operator, err = _require_admin_or_above()
    if err:
        return err

    target = User.query.get_or_404(user_id)
    data = request.get_json()
    new_pwd = (data.get('new_password') or '').strip()

    if not new_pwd or len(new_pwd) < 4:
        return jsonify({'error': '新密码至少4位'}), 400

    # admin 和 super_admin 同等权限，可重置任何用户密码

    target.set_password(new_pwd)
    db.session.commit()
    return jsonify({'message': f'已重置 {target.username} 的密码'})


@api_bp.route('/users/<int:user_id>', methods=['DELETE'])
def delete_user(user_id):
    """删除用户（仅 super_admin）"""
    operator, err = _require_super_admin()
    if err:
        return err

    target = User.query.get_or_404(user_id)
    if target.id == operator.id:
        return jsonify({'error': '不能删除自己'}), 400

    db.session.delete(target)
    db.session.commit()
    return jsonify({'message': f'用户 {target.username} 已删除'})
