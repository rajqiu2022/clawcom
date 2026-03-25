"""
Agent Hub Web UI
"""
from flask import Blueprint, render_template, jsonify, request
from app import db
from app.models import Agent, Message, Conversation

views_bp = Blueprint('views', __name__)


@views_bp.route('/')
def index():
    """通信中心首页"""
    return render_template('index.html')


@views_bp.route('/agents')
def agents_page():
    """Agent 管理页面"""
    return render_template('agents.html')


@views_bp.route('/messages')
def messages_page():
    """消息中心页面"""
    return render_template('messages.html')


@views_bp.route('/conversations')
def conversations_page():
    """会话中心页面"""
    return render_template('conversations.html')
