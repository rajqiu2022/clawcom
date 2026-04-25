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
    return render_template('users.html')


@views_bp.route('/office')
def pixel_office():
    """像素办公室 — 可视化 OpenClaw 状态"""
    return render_template('office.html')


@views_bp.route('/testplans')
def testplans():
    """测试计划排期"""
    return render_template('testplans.html')


@views_bp.route('/engineering')
@views_bp.route('/engineering/baselines/<int:baseline_id>')
@views_bp.route('/engineering/refresh/<int:batch_id>')
@views_bp.route('/engineering/architecture/<int:snap_id>')
def engineering(baseline_id=None, batch_id=None, snap_id=None):
    """工程分析中心：基线管理 + 增量刷新批次 + 架构快照 + 影响项闭环。

    Deep link 入口：
      /engineering/baselines/{id}     基线详情
      /engineering/refresh/{id}       刷新批次详情
      /engineering/architecture/{id}  架构快照详情（用于跨 OpenClaw 引用）
    """
    return render_template(
        'engineering.html',
        initial_baseline_id=baseline_id,
        initial_batch_id=batch_id,
        initial_arch_snap_id=snap_id,
    )


@views_bp.route('/requirements')
@views_bp.route('/requirements/iterations/<int:iteration_id>')
@views_bp.route('/requirements/items/<int:item_id>')
def requirements(iteration_id=None, item_id=None):
    """需求分析中心：迭代需求快照 + 变更跟踪 + 用例/工程关联"""
    return render_template(
        'requirements.html',
        initial_iteration_id=iteration_id,
        initial_item_id=item_id,
    )
