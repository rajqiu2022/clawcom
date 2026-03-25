"""
Agent Hub - OpenClaw 通信中心
独立部署，端口 5232
"""
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()


def create_app():
    app = Flask(__name__, template_folder='../templates')
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///agent_hub.db'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['SECRET_KEY'] = 'agent-hub-secret-key-2026'

    db.init_app(app)

    from app.api import api_bp
    app.register_blueprint(api_bp, url_prefix='/api')

    from app.views import views_bp
    app.register_blueprint(views_bp)

    with app.app_context():
        db.create_all()

    return app
