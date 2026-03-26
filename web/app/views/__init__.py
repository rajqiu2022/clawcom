from flask import Blueprint, render_template

views_bp = Blueprint('views', __name__)


@views_bp.route('/')
def dashboard():
    """Dashboard 总览"""
    return render_template('dashboard.html')


@views_bp.route('/openclaws')
def openclaws_list():
    """OpenClaw 列表"""
    return render_template('openclaws.html')


@views_bp.route('/openclaws/<int:claw_id>')
def openclaw_detail(claw_id):
    """OpenClaw 详情"""
    return render_template('openclaw_detail.html', claw_id=claw_id)


@views_bp.route('/skills')
def skills_market():
    """Skills 市场"""
    return render_template('skills.html')


@views_bp.route('/knowledge')
def knowledge_base():
    """知识库管理"""
    return render_template('knowledge.html')


@views_bp.route('/settings')
def settings():
    """系统设置"""
    return render_template('settings.html')


@views_bp.route('/reports')
def reports():
    """日报中心"""
    return render_template('reports.html')


@views_bp.route('/tapd')
def tapd():
    """TAPD 集成"""
    return render_template('tapd.html')


@views_bp.route('/hub')
def hub():
    """通信中心"""
    return render_template('hub.html')


@views_bp.route('/rules')
def rules():
    """工作规范 Rules"""
    return render_template('rules.html')


@views_bp.route('/testcases')
def testcases():
    """AI 用例库"""
    return render_template('testcases.html')
