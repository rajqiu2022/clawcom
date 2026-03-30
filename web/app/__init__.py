from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_cors import CORS
from config import config
import logging

logger = logging.getLogger(__name__)

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

    # 启动心跳超时检测后台任务
    from threading import Thread
    from datetime import datetime, timedelta
    
    def check_heartbeat_timeout():
        """检查心跳超时，将长时间无心跳的 OpenClaw 设为 offline"""
        from app.models import OpenClawInstance
        timeout = timedelta(minutes=3)  # 超过3分钟无心跳视为离线
        while True:
            try:
                with app.app_context():
                    now = datetime.utcnow()
                    # 查找状态为 online 但超过3分钟没有心跳的（包括从未发送过心跳的）
                    offline_claws = OpenClawInstance.query.filter(
                        OpenClawInstance.status == 'online',
                        db.or_(
                            OpenClawInstance.last_heartbeat < now - timeout,
                            OpenClawInstance.last_heartbeat == None
                        )
                    ).all()

                    if offline_claws:
                        for claw in offline_claws:
                            claw.status = 'offline'
                            db.session.commit()
                            logger.info(f"心跳超时，OpenClaw {claw.id} ({claw.name}) 已设为 offline")
            except Exception as e:
                logger.error(f"心跳超时检测异常: {e}")
            
            import time
            time.sleep(60)  # 每60秒检查一次
    
    # 启动后台线程（仅在 WSGI 模式下）
    if not app.debug:
        t = Thread(target=check_heartbeat_timeout, daemon=True)
        t.start()
        logger.info("心跳超时检测后台任务已启动")

    return app
