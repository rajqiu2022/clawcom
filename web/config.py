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

    # Memos
    MEMOS_URL = os.getenv('MEMOS_URL', 'http://your-hub-host:5230')
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
