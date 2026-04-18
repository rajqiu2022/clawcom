import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    SECRET_KEY = os.getenv('SECRET_KEY', 'dev-secret-key')

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
    else:
        SQLALCHEMY_DATABASE_URI = 'sqlite:///openclaw.db'

    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # 连接池配置：SSE长连接 + 多worker下默认5+10容易耗尽
    SQLALCHEMY_POOL_SIZE = 20
    SQLALCHEMY_POOL_RECYCLE = 3600  # 1小时回收连接，避免MySQL gone away
    SQLALCHEMY_POOL_PRE_PING = True  # 连接前检测可用性
    SQLALCHEMY_MAX_OVERFLOW = 30  # 超出pool_size后最多再创建30个连接

    # Memos
    MEMOS_URL = os.getenv('MEMOS_URL', 'http://9.134.11.169:5230')
    MEMOS_API_KEY = os.getenv('MEMOS_API_KEY', '')


class DevelopmentConfig(Config):
    DEBUG = True


class ProductionConfig(Config):
    DEBUG = False


config = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'default': DevelopmentConfig,
}
