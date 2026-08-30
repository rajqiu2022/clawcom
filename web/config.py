import os
from datetime import timedelta
from dotenv import load_dotenv

load_dotenv()


class Config:
    SECRET_KEY = os.getenv('SECRET_KEY', 'dev-secret-key')

    # 测试左移首期为纯增量能力，生产默认关闭；灰度项目确认后显式开启。
    SHIFT_LEFT_ENABLED = os.getenv('SHIFT_LEFT_ENABLED', '0') in (
        '1', 'true', 'True', 'yes', 'on')

    # 多 Agent 交付合同（Artifact/Stage/Handoff/Eval）按项目灰度前默认关闭。
    AGENT_TEAM_CONTRACTS_ENABLED = os.getenv(
        'AGENT_TEAM_CONTRACTS_ENABLED', '0') in (
            '1', 'true', 'True', 'yes', 'on')

    # 主题聊天室是纯增量能力，默认关闭，避免部署后改变现有点对点聊天行为。
    CHAT_ROOM_ENABLED = os.getenv('CHAT_ROOM_ENABLED', '0') in (
        '1', 'true', 'True', 'yes', 'on')
    CHAT_ROOM_GUEST_TOKEN_MINUTES = int(os.getenv(
        'CHAT_ROOM_GUEST_TOKEN_MINUTES', '2880'))
    CHAT_ROOM_GUEST_TOKEN_MAX_MINUTES = int(os.getenv(
        'CHAT_ROOM_GUEST_TOKEN_MAX_MINUTES', '10080'))
    CHAT_ROOM_AGENT_MAX_DEPTH = int(os.getenv('CHAT_ROOM_AGENT_MAX_DEPTH', '5'))

    # Session（OA/WOA 单点登录后保持登录态）
    # SameSite=Lax 兼容 passport.woa.com 302 跳回我们的 callback。
    PERMANENT_SESSION_LIFETIME = timedelta(days=int(os.getenv('SESSION_DAYS', '30')))
    SESSION_COOKIE_NAME = os.getenv('SESSION_COOKIE_NAME', 'openclaw_session')
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    # Secure 默认关闭：兼容内网 http://clawteam.woa.com:18800 直连。
    # 部署到纯 https 域名后可通过 SESSION_COOKIE_SECURE=1 显式开启。
    SESSION_COOKIE_SECURE = os.getenv('SESSION_COOKIE_SECURE', '0') in ('1', 'true', 'True')

    # MySQL
    MYSQL_HOST = os.getenv('MYSQL_HOST', '')
    MYSQL_PORT = os.getenv('MYSQL_PORT', '3306')
    MYSQL_USER = os.getenv('MYSQL_USER', '')
    MYSQL_PASSWORD = os.getenv('MYSQL_PASSWORD', '')
    MYSQL_DATABASE = os.getenv('MYSQL_DATABASE', 'openclaw_manager')

    # 如果 MySQL 配置齐全就用 MySQL，否则回退 SQLite
    if MYSQL_HOST and MYSQL_USER:
        SQLALCHEMY_DATABASE_URI = (
            f"mysql+pymysql://{MYSQL_USER}:{MYSQL_PASSWORD}"
            f"@{MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DATABASE}?charset=utf8mb4"
        )
        SQLALCHEMY_ENGINE_OPTIONS = {
            # SSE 长连接 + gunicorn 多 worker 下默认 5+10 容易耗尽。
            # Flask-SQLAlchemy 新版本只认 engine_options；旧的 SQLALCHEMY_POOL_* 不生效。
            # 4 个 gunicorn worker 共享同一个 MySQL 实例；单 worker 上限不能过大，
            # 否则 SSE/sidecar 重连风暴时会超过 MySQL max_connections。
            'pool_size': int(os.getenv('SQLALCHEMY_POOL_SIZE', '8')),
            'max_overflow': int(os.getenv('SQLALCHEMY_MAX_OVERFLOW', '4')),
            'pool_recycle': int(os.getenv('SQLALCHEMY_POOL_RECYCLE', '3600')),
            'pool_pre_ping': os.getenv('SQLALCHEMY_POOL_PRE_PING', '1') not in ('0', 'false', 'False'),
            'pool_timeout': int(os.getenv('SQLALCHEMY_POOL_TIMEOUT', '30')),
        }
    else:
        SQLALCHEMY_DATABASE_URI = 'sqlite:///openclaw.db'

    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Memos
    MEMOS_URL = os.getenv('MEMOS_URL', 'http://your-hub-host:5230')
    MEMOS_API_KEY = os.getenv('MEMOS_API_KEY', '')
    # Memos 0.24：PRIVATE 不出现在 List API（#4495），Claw 笔记用 PROTECTED
    MEMOS_DEFAULT_VISIBILITY = os.getenv('MEMOS_DEFAULT_VISIBILITY', 'PROTECTED')

    # Default deployment settings for simplified Hermes Agent deployment
    DEPLOY_DEFAULT_HOST = os.getenv('DEPLOY_DEFAULT_HOST', '')
    DEPLOY_DEFAULT_SSH_USER = os.getenv('DEPLOY_DEFAULT_SSH_USER', 'root')
    DEPLOY_DEFAULT_SSH_PASSWORD = os.getenv('DEPLOY_DEFAULT_SSH_PASSWORD', '')
    DEPLOY_DEFAULT_SSH_KEY = os.getenv('DEPLOY_DEFAULT_SSH_KEY', '')

    # Agent 模板文件存储目录（模板隔离存储）
    AGENT_TEMPLATE_STORAGE_ROOT = os.getenv(
        'AGENT_TEMPLATE_STORAGE_ROOT',
        os.path.abspath(os.path.join(os.path.dirname(__file__), 'data', 'agent_templates'))
    )


class DevelopmentConfig(Config):
    DEBUG = True


class ProductionConfig(Config):
    DEBUG = False


config = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'default': DevelopmentConfig,
}
