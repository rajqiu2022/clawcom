from flask import Blueprint, render_template, session, redirect, request
from app import db

views_bp = Blueprint('views', __name__)


@views_bp.before_request
def check_login():
    """全局登录检查（排除 /login）"""
    if request.path == '/login':
        return None
    uid = session.get('user_id')
    if not uid:
        return redirect('/login')
    from app.models import User
    user = User.query.get(uid)
    if not user:
        session.pop('user_id', None)
        return redirect('/login')
    return None


def _require_role(*roles):
    """检查当前用户角色"""
    uid = session.get('user_id')
    if not uid:
        return redirect('/login')
    from app.models import User
    user = User.query.get(uid)
    if not user or user.role not in roles:
        return redirect('/')
    return None


@views_bp.route('/login')
def login_page():
    """登录页"""
    return render_template('login.html')


@views_bp.route('/')
def dashboard():
    return render_template('dashboard.html')


@views_bp.route('/openclaws')
def openclaws_list():
    return render_template('openclaws.html')


@views_bp.route('/openclaws/<int:claw_id>')
def openclaw_detail(claw_id):
    return render_template('openclaw_detail.html', claw_id=claw_id)


@views_bp.route('/skills')
def skills_market():
    return render_template('skills.html')


@views_bp.route('/knowledge')
def knowledge_base():
    return render_template('knowledge.html')


@views_bp.route('/settings')
def settings():
    r = _require_role('super_admin')
    if r: return r
    return render_template('settings.html')


@views_bp.route('/reports')
def reports():
    return render_template('reports.html')


@views_bp.route('/tapd')
def tapd():
    return render_template('tapd.html')


@views_bp.route('/hub')
def hub():
    r = _require_role('super_admin')
    if r: return r
    return render_template('hub.html')


@views_bp.route('/rules')
def rules():
    return render_template('rules.html')


@views_bp.route('/testcases')
@views_bp.route('/testcases/<int:lib_id>')
def testcases(lib_id=None):
    return render_template('testcases.html', initial_lib_id=lib_id)


@views_bp.route('/review')
def review_center():
    return render_template('review.html')


@views_bp.route('/topics')
def topics_page():
    return render_template('topics.html')


@views_bp.route('/topics/<int:topic_id>')
def topic_detail(topic_id):
    return render_template('topic_detail.html', topic_id=topic_id)


@views_bp.route('/audit-logs')
def audit_logs():
    r = _require_role('super_admin')
    if r: return r
    return render_template('audit_logs.html')


@views_bp.route('/users')
def users_management():
    r = _require_role('super_admin', 'admin')
    if r: return r
    return render_template('users.html')


@views_bp.route('/office')
def pixel_office():
    """像素办公室 — 可视化 OpenClaw 状态"""
    r = _require_role('super_admin')
    if r: return r
    return render_template('office.html')


@views_bp.route('/testplans')
def testplans():
    """测试计划排期"""
    return render_template('testplans.html')
