"""Local-only browser fixture. Run explicitly; never connects to production."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))
from test_agent_teams_api import AgentTeamsApiTest
from app import db
from flask import render_template, session


def main():
    suite = AgentTeamsApiTest()
    suite.setUp()
    app = suite.app
    app.config['TEMPLATES_AUTO_RELOAD'] = True
    web = Path(__file__).resolve().parents[1] / 'web'
    app.template_folder = str(web / 'templates')
    app.static_folder = str(web / 'static')
    admin_id = suite.admin.id

    @app.before_request
    def local_test_login():
        session['user_id'] = admin_id

    @app.route('/agent-teams')
    def preview():
        return render_template('agent_teams.html', hub_public_url='http://127.0.0.1:18891', hub_web_url='http://127.0.0.1:18891')

    suite._setup_team()
    suite.project.name = '团队管理 · 本地验证项目'
    suite.main_claw.name = '测试经理 A'
    suite.backup.name = '测试经理 B'
    suite.other_claw.name = '执行 Agent A'
    db.session.commit()
    mission_id = suite._mission()
    suite._plan(mission_id)
    for name, objective, status in (
        ('编辑器回归团队', '验证编辑器流程与关键交互，不启动实际游戏进程。', 'active'),
        ('客户端性能团队', '跟踪帧率、内存与加载耗时的基线变化。', 'paused'),
    ):
        suite.client.post('/api/v1/agent-teams', json=dict(suite.config, name=name, objective=objective, status=status))
    suite.ctx.pop()
    app.run(host='127.0.0.1', port=18891, debug=False, use_reloader=False, threaded=False)


if __name__ == '__main__':
    main()
