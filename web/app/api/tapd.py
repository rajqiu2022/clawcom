"""
TAPD 集成 API
对接 TAPD 开放 API（https://www.tapd.cn/help/view#1120003271001001539）
支持需求、缺陷、迭代的拉取与同步
"""
import requests
from datetime import datetime
from flask import request, jsonify
from sqlalchemy import text
from app import db
from app.models import Project
from app.api import api_bp


def _get_tapd_credentials():
    """获取 TAPD API 凭证（存储在 system_config 中）"""
    try:
        rows = db.session.execute(
            text("SELECT config_key, config_value FROM system_config WHERE config_key LIKE 'tapd_%'")
        ).fetchall()
        cfg = {r.config_key: r.config_value for r in rows}
        return cfg.get('tapd_api_user', ''), cfg.get('tapd_api_password', '')
    except Exception:
        return '', ''


def _tapd_request(method, url, params=None, api_user='', api_password=''):
    """调用 TAPD 开放 API"""
    if not api_user or not api_password:
        api_user, api_password = _get_tapd_credentials()

    if not api_user or not api_password:
        raise ValueError('TAPD API 凭证未配置，请在系统设置中配置 tapd_api_user 和 tapd_api_password')

    resp = requests.request(
        method,
        url,
        params=params,
        auth=(api_user, api_password),
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get('status') != 1:
        raise ValueError(f'TAPD API 错误: {data.get("info", "未知错误")}')

    return data.get('data', [])


@api_bp.route('/tapd/config', methods=['GET'])
def get_tapd_config():
    """获取 TAPD 配置"""
    api_user, api_password = _get_tapd_credentials()
    return jsonify({
        'configured': bool(api_user and api_password),
        'api_user': api_user[:8] + '***' if api_user else '',
    })


@api_bp.route('/tapd/config', methods=['PUT'])
def update_tapd_config():
    """更新 TAPD 配置"""
    data = request.get_json()

    conn = db.engine.connect()
    for key in ['tapd_api_user', 'tapd_api_password']:
        if key in data:
            existing = conn.execute(text(
                "SELECT 1 FROM system_config WHERE config_key=:k"
            ), {'k': key}).fetchone()
            if existing:
                conn.execute(text(
                    "UPDATE system_config SET config_value=:v, updated_at=CURRENT_TIMESTAMP WHERE config_key=:k"
                ), {'k': key, 'v': data[key]})
            else:
                conn.execute(text(
                    "INSERT INTO system_config (config_key, config_value, updated_at) VALUES (:k, :v, CURRENT_TIMESTAMP)"
                ), {'k': key, 'v': data[key]})
    conn.commit()
    conn.close()
    return jsonify({'message': 'TAPD 配置已更新'})


@api_bp.route('/tapd/stories', methods=['GET'])
def list_tapd_stories():
    """获取 TAPD 需求列表"""
    workspace_id = request.args.get('workspace_id')
    if not workspace_id:
        return jsonify({'error': '缺少 workspace_id'}), 400

    try:
        limit = request.args.get('limit', 50, type=int)
        page = request.args.get('page', 1, type=int)
        status = request.args.get('status', '')

        params = {
            'workspace_id': workspace_id,
            'limit': limit,
            'page': page,
            'order': 'created desc',
        }
        if status:
            params['status'] = status

        data = _tapd_request('GET', 'https://api.tapd.cn/stories', params)

        stories = []
        for item in data:
            story = item.get('Story', {})
            stories.append({
                'id': story.get('id'),
                'name': story.get('name'),
                'status': story.get('status'),
                'priority': story.get('priority'),
                'owner': story.get('owner'),
                'creator': story.get('creator'),
                'created': story.get('created'),
                'modified': story.get('modified'),
                'iteration_id': story.get('iteration_id'),
                'category_id': story.get('category_id'),
                'description': story.get('description', '')[:200],
            })

        return jsonify({'stories': stories, 'count': len(stories)})

    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': f'TAPD API 调用失败: {str(e)}'}), 500


@api_bp.route('/tapd/bugs', methods=['GET'])
def list_tapd_bugs():
    """获取 TAPD 缺陷列表"""
    workspace_id = request.args.get('workspace_id')
    if not workspace_id:
        return jsonify({'error': '缺少 workspace_id'}), 400

    try:
        limit = request.args.get('limit', 50, type=int)
        page = request.args.get('page', 1, type=int)
        status = request.args.get('status', '')
        severity = request.args.get('severity', '')

        params = {
            'workspace_id': workspace_id,
            'limit': limit,
            'page': page,
            'order': 'created desc',
        }
        if status:
            params['status'] = status
        if severity:
            params['severity'] = severity

        data = _tapd_request('GET', 'https://api.tapd.cn/bugs', params)

        bugs = []
        for item in data:
            bug = item.get('Bug', {})
            bugs.append({
                'id': bug.get('id'),
                'title': bug.get('title'),
                'status': bug.get('status'),
                'severity': bug.get('severity'),
                'priority': bug.get('priority'),
                'current_owner': bug.get('current_owner'),
                'reporter': bug.get('reporter'),
                'created': bug.get('created'),
                'modified': bug.get('modified'),
                'iteration_id': bug.get('iteration_id'),
                'module': bug.get('module'),
                'description': bug.get('description', '')[:200],
            })

        return jsonify({'bugs': bugs, 'count': len(bugs)})

    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': f'TAPD API 调用失败: {str(e)}'}), 500


@api_bp.route('/tapd/iterations', methods=['GET'])
def list_tapd_iterations():
    """获取 TAPD 迭代列表"""
    workspace_id = request.args.get('workspace_id')
    if not workspace_id:
        return jsonify({'error': '缺少 workspace_id'}), 400

    try:
        params = {
            'workspace_id': workspace_id,
            'limit': 30,
            'order': 'created desc',
        }

        data = _tapd_request('GET', 'https://api.tapd.cn/iterations', params)

        iterations = []
        for item in data:
            it = item.get('Iteration', {})
            iterations.append({
                'id': it.get('id'),
                'name': it.get('name'),
                'status': it.get('status'),
                'startdate': it.get('startdate'),
                'enddate': it.get('enddate'),
                'creator': it.get('creator'),
            })

        return jsonify({'iterations': iterations, 'count': len(iterations)})

    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': f'TAPD API 调用失败: {str(e)}'}), 500


@api_bp.route('/tapd/dashboard', methods=['GET'])
def tapd_dashboard():
    """TAPD 项目概览（汇总多个已绑定项目）"""
    projects = Project.query.filter(
        Project.tapd_workspace_id.isnot(None),
        Project.tapd_workspace_id != ''
    ).all()

    if not projects:
        return jsonify({
            'message': '未找到已绑定 TAPD 的项目',
            'projects': [],
        })

    api_user, api_password = _get_tapd_credentials()
    if not api_user or not api_password:
        return jsonify({'error': 'TAPD API 凭证未配置'}), 400

    result = []
    for p in projects:
        try:
            # 拉取需求统计
            stories_data = _tapd_request(
                'GET', 'https://api.tapd.cn/stories/count',
                {'workspace_id': p.tapd_workspace_id},
                api_user, api_password
            )
            # 拉取缺陷统计
            bugs_data = _tapd_request(
                'GET', 'https://api.tapd.cn/bugs/count',
                {'workspace_id': p.tapd_workspace_id},
                api_user, api_password
            )

            result.append({
                'project_id': p.id,
                'project_name': p.name,
                'workspace_id': p.tapd_workspace_id,
                'story_count': stories_data.get('count', 0) if isinstance(stories_data, dict) else 0,
                'bug_count': bugs_data.get('count', 0) if isinstance(bugs_data, dict) else 0,
                'status': 'ok',
            })
        except Exception as e:
            result.append({
                'project_id': p.id,
                'project_name': p.name,
                'workspace_id': p.tapd_workspace_id,
                'error': str(e),
                'status': 'error',
            })

    return jsonify({'projects': result})
