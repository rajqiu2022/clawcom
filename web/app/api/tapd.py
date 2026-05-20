"""
TAPD 集成 API
对接 TAPD 开放 API（https://www.tapd.cn/help/view#1120003271001001539）
支持需求、缺陷、迭代的拉取与同步
"""
import requests
import os
from datetime import datetime
from flask import request, jsonify
from sqlalchemy import text
from app import db
from app.models import Project
from app.api import api_bp

TAPD_API_BASE_URL = os.getenv('TAPD_API_BASE_URL', 'http://apiv2.tapd.woa.com').rstrip('/')


def _get_tapd_credentials():
    """获取 TAPD API 凭证（存储在 system_config 中）"""
    try:
        rows = db.session.execute(
            text("SELECT config_key, value FROM system_config WHERE config_key LIKE 'tapd_%'")
        ).fetchall()
        cfg = {r[0]: r[1] for r in rows}
        return cfg.get('tapd_api_user', ''), cfg.get('tapd_api_password', '')
    except Exception:
        return '', ''


def _tapd_request(method, url, params=None, api_user='', api_password=''):
    """调用 TAPD 开放 API"""
    if not api_user or not api_password:
        api_user, api_password = _get_tapd_credentials()

    if not api_user or not api_password:
        raise ValueError('TAPD API 凭证未配置，请在系统设置中配置 tapd_api_user 和 tapd_api_password')

    try:
        resp = requests.request(
            method,
            url,
            params=params,
            auth=(api_user, api_password),
            timeout=30,
        )
        resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response is not None else 0
        if code in (401, 403):
            raise ValueError(
                'TAPD 拒绝鉴权（HTTP %s）：Hub 当前请求 %s。请核对「系统设置」里保存的是'
                'apiv2.tapd.woa.com 可用的 Basic 认证账号/口令，并确认账号有对应项目权限。'
                % (code, url)
            ) from e
        raise ValueError('TAPD HTTP 调用失败（HTTP %s）: %s' % (code, e)) from e
    except requests.exceptions.RequestException as e:
        raise ValueError('TAPD 网络请求失败: %s' % e) from e

    data = resp.json()

    if data.get('status') != 1:
        raise ValueError(f'TAPD API 错误: {data.get("info", "未知错误")}')

    return data.get('data', [])


@api_bp.route('/tapd/story-title', methods=['GET'])
def get_tapd_story_title():
    """根据 story_id 获取 TAPD 需求标题

    查询参数：
    - story_id: TAPD 需求 ID
    - workspace_id: TAPD 项目 workspace_id（可选，从 story URL 中解析）
    - story_url: 完整 TAPD 需求链接（可选，自动解析 workspace_id 和 story_id）
    """
    story_url = request.args.get('story_url', '')
    story_id = request.args.get('story_id', '')
    workspace_id = request.args.get('workspace_id', '')

    # 从完整 URL 解析 workspace_id 和 story_id
    # 格式: https://www.tapd.cn/{workspace_id}/stories/view/{story_id}
    import re
    if story_url and (not workspace_id or not story_id):
        m = re.match(r'https?://www\.tapd\.cn/(\d+)/stories/view/(\d+)', story_url)
        if m:
            workspace_id = workspace_id or m.group(1)
            story_id = story_id or m.group(2)

    if not story_id:
        return jsonify({'error': '缺少 story_id 或 story_url'}), 400

    try:
        params = {'id': story_id}
        if workspace_id:
            params['workspace_id'] = workspace_id
            params['limit'] = 1

        data = _tapd_request('GET', f'{TAPD_API_BASE_URL}/stories', params)

        for item in data:
            story = item.get('Story', {})
            return jsonify({
                'id': story.get('id'),
                'title': story.get('name', ''),
                'status': story.get('status', ''),
                'priority': story.get('priority', ''),
                'owner': story.get('owner', ''),
            })

        return jsonify({'error': '未找到该需求', 'title': ''}), 404

    except ValueError as e:
        return jsonify({'error': str(e), 'title': ''}), 400
    except Exception as e:
        return jsonify({'error': f'TAPD API 调用失败: {str(e)}', 'title': ''}), 500


@api_bp.route('/tapd/config', methods=['GET'])
def get_tapd_config():
    """获取 TAPD 配置"""
    api_user, api_password = _get_tapd_credentials()
    updated_at = None
    try:
        row = db.session.execute(text("""
            SELECT MAX(updated_at) FROM system_config
            WHERE config_key IN ('tapd_api_user', 'tapd_api_password')
        """)).fetchone()
        updated_at = str(row[0]) if row and row[0] else None
    except Exception:
        updated_at = None
    return jsonify({
        'configured': bool(api_user and api_password),
        'api_user': api_user[:8] + '***' if api_user else '',
        'password_configured': bool(api_password),
        'updated_at': updated_at,
    })


@api_bp.route('/tapd/config', methods=['PUT'])
def update_tapd_config():
    """更新 TAPD 配置"""
    data = request.get_json()

    try:
        for key in ['tapd_api_user', 'tapd_api_password']:
            if key in data:
                existing = db.session.execute(text(
                    "SELECT 1 FROM system_config WHERE config_key=:k"
                ), {'k': key}).fetchone()
                if existing:
                    db.session.execute(text(
                        "UPDATE system_config SET value=:v, updated_at=CURRENT_TIMESTAMP WHERE config_key=:k"
                    ), {'k': key, 'v': data[key]})
                else:
                    db.session.execute(text(
                        "INSERT INTO system_config (config_key, value, updated_at) VALUES (:k, :v, CURRENT_TIMESTAMP)"
                    ), {'k': key, 'v': data[key]})
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': f'配置保存失败: {str(e)}'}), 500

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
        iteration_id = (request.args.get('iteration_id') or '').strip()
        name = (request.args.get('name') or request.args.get('keyword') or '').strip()

        params = {
            'workspace_id': workspace_id,
            'limit': limit,
            'page': page,
            'order': 'created desc',
        }
        if status:
            params['status'] = status
        if iteration_id:
            params['iteration_id'] = iteration_id
        if name:
            params['name'] = name

        data = _tapd_request('GET', f'{TAPD_API_BASE_URL}/stories', params)

        stories = []
        if not isinstance(data, list):
            data = []
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
        iteration_id = (request.args.get('iteration_id') or '').strip()
        title = (request.args.get('title') or request.args.get('keyword') or '').strip()

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
        if iteration_id:
            params['iteration_id'] = iteration_id
        if title:
            params['title'] = title

        data = _tapd_request('GET', f'{TAPD_API_BASE_URL}/bugs', params)

        bugs = []
        if not isinstance(data, list):
            data = []
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
    """获取 TAPD 迭代列表

    重构（2026-04）：Hub 不再直连 TAPD，改读 tapd_iterations_cache 本地缓存。
    缓存由 OpenClaw Agent 经 mcporter-internal/MCP 推送维护。
    用户可通过"实时刷新"按钮 POST /requirements/tapd-refresh-requests 触发更新。
    """
    workspace_id = request.args.get('workspace_id')
    if not workspace_id:
        return jsonify({'error': '缺少 workspace_id'}), 400

    from app.models import TapdIterationsCache
    from sqlalchemy import desc as _desc, func as _func

    rows = (TapdIterationsCache.query
            .filter_by(tapd_workspace_id=workspace_id)
            .order_by(_desc(TapdIterationsCache.startdate))
            .all())
    last_synced = (db.session.query(_func.max(TapdIterationsCache.last_synced_at))
                   .filter(TapdIterationsCache.tapd_workspace_id == workspace_id)
                   .scalar())

    iterations = [{
        'id': r.tapd_iteration_id,
        'name': r.name or '',
        'status': r.status,
        'startdate': str(r.startdate) if r.startdate else None,
        'enddate': str(r.enddate) if r.enddate else None,
        'creator': r.creator or '',
    } for r in rows]

    return jsonify({
        'iterations': iterations,
        'count': len(iterations),
        'data_source': 'local_cache',
        'last_synced_at': str(last_synced) if last_synced else None,
        'hint': '本地缓存为空，请点击"需求分析"页面的"实时刷新"按钮通知 Agent 同步 TAPD 数据' if not iterations else None,
    })


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
                'GET', f'{TAPD_API_BASE_URL}/stories/count',
                {'workspace_id': p.tapd_workspace_id},
                api_user, api_password
            )
            # 拉取缺陷统计
            bugs_data = _tapd_request(
                'GET', f'{TAPD_API_BASE_URL}/bugs/count',
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
