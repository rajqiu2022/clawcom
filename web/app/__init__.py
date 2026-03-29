from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_cors import CORS
from config import config

db = SQLAlchemy()
migrate = Migrate()

# SocketIO 实例（延迟初始化）
socketio = None


def create_app(config_name=None):
    if config_name is None:
        import os
        config_name = os.getenv('FLASK_ENV', 'default')

    app = Flask(__name__,
                static_folder='../static',
                template_folder='../templates')
    app.config.from_object(config[config_name])

    # 初始化扩展
    db.init_app(app)
    migrate.init_app(app, db)
    CORS(app)

    # 注册蓝图 - API
    from app.api import api_bp
    app.register_blueprint(api_bp, url_prefix='/api/v1')

    # 注册蓝图 - 页面
    from app.views import views_bp
    app.register_blueprint(views_bp)

    # 注册 MCP 蓝图
    from app.api.mcp_protocol import mcp_bp
    app.register_blueprint(mcp_bp)

    # 注册 Agent Client 蓝图（SSE 任务推送）
    from app.api.agent_client import agent_bp
    app.register_blueprint(agent_bp, url_prefix='/api/openclaws')

    # 初始化 SocketIO 并注册 WebSocket 处理器
    from app.api.gateway_ws import init_socketio, socketio as _socketio
    globals()['socketio'] = init_socketio(app)

    return app
