from flask import Blueprint

api_bp = Blueprint('api', __name__)

from app.api import openclaws, skills, knowledge, dashboard, projects, agent_hub  # noqa

# 注册 Agent Hub 通信中心蓝图
api_bp.register_blueprint(agent_hub.agent_hub_bp, url_prefix='/agent-hub')
