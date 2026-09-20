"""Local-only browser fixture. Run explicitly; never connects to production."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))
from test_agent_teams_api import AgentTeamsApiTest
from app import db
from app.models import AgentTeamMemberStatus, _now
from datetime import timedelta
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
    def report(claw, token, body):
        response = suite.client.post('/api/v1/agent-teams/%s/members/%s/activity' % (suite.team_id, claw.id),
                                     headers=suite._headers(token), json=body)
        assert response.status_code == 200, response.get_json()
    task = {'task_key':'bug-123-attempt-1', 'title':'回归 Bug #123：大厅返回异常', 'task_type':'bug_regression',
            'reference':'Bug #123', 'status':'working', 'progress_percent':10, 'progress_message':'正在准备回归环境'}
    report(suite.main_claw, suite.main_token, {'event_id':'s1','expected_version':0,'state':'working','task':task})
    task.update(status='completed', progress_percent=100, progress_message='回归结束，已整理复现截图与日志（本地模拟数据）')
    report(suite.main_claw, suite.main_token, {'event_id':'s2','expected_version':1,'state':'idle','task':task})
    task = {'task_key':'flow-36-run-42', 'title':'执行 Flow #36 · 功能发现与代码分析', 'task_type':'flow',
            'reference':'Flow #36 / Run #42', 'status':'working', 'progress_percent':45,
            'progress_message':'已完成仓库同步，正在分析登录模块与需求变更。<b>这是原文，不应变成 HTML</b>'}
    report(suite.main_claw, suite.main_token, {'event_id':'s3','expected_version':2,'state':'working','task':task})
    report(suite.backup, suite.backup_token, {'event_id':'idle','expected_version':0,'state':'idle','summary':'等待测试经理分配任务'})
    task.update(task_key='editor-42', title='执行编辑器回归', status='blocked', progress_percent=20, progress_message='等待测试设备恢复连接')
    report(suite.other_claw, suite.other_token, {'event_id':'s1','expected_version':0,'state':'blocked','task':task})
    row = AgentTeamMemberStatus.query.filter_by(claw_id=suite.other_claw.id).one()
    row.reported_at = _now() - timedelta(seconds=240)
    db.session.commit()
    for name, objective, status in (
        ('编辑器回归团队', '验证编辑器流程与关键交互，不启动实际游戏进程。', 'active'),
        ('客户端性能团队', '跟踪帧率、内存与加载耗时的基线变化。', 'paused'),
    ):
        suite.client.post('/api/v1/agent-teams', json=dict(suite.config, name=name, objective=objective, status=status))
    suite.ctx.pop()
    app.run(host='127.0.0.1', port=18891, debug=False, use_reloader=False, threaded=False)


if __name__ == '__main__':
    main()
