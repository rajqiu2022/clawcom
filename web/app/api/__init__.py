from flask import Blueprint

api_bp = Blueprint('api', __name__)

from app.api import openclaws, skills, knowledge, dashboard, projects, agent_hub, agent_client, rules, ai_generator, testcases, reports, audit, system, tapd, auth, memos_api, todos, packs, snapshots, registration  # noqa

# 注册 Agent Hub 通信中心蓝图
api_bp.register_blueprint(agent_hub.agent_hub_bp, url_prefix='/agent-hub')

# 注册子agent客户端蓝图（SSE长连接、任务派发）
# 注意：agent_bp 已有自己的 url_prefix='/api/openclaws'
api_bp.register_blueprint(agent_client.agent_bp)
