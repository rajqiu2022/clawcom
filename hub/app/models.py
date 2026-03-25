"""
Agent 模型
"""
import secrets
import hashlib
from datetime import datetime
from app import db


def generate_agent_token():
    """生成 Agent API Token"""
    return 'hub_tk_' + secrets.token_hex(24)


def hash_token(token):
    """对 Token 做 SHA256 哈希"""
    return hashlib.sha256(token.encode()).hexdigest()


class Agent(db.Model):
    """Agent 注册表"""
    __tablename__ = 'agents'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(50), nullable=False, unique=True, comment='Agent 名称')
    agent_key = db.Column(db.String(50), nullable=False, unique=True,
                         comment='唯一标识 key，如 claw-小测-001')
    role = db.Column(db.String(50), default='test_member',
                     comment='角色：admin/test_manager/test_member/test_executor')
    project_name = db.Column(db.String(100), comment='所属项目')
    module_name = db.Column(db.String(100), comment='所属模块')
    status = db.Column(db.String(20), default='offline',
                       comment='online/offline/busy')
    api_token_hash = db.Column(db.String(64), comment='API Token 哈希')
    hub_url = db.Column(db.String(255), comment='Agent 的 Webhook 地址')
    last_heartbeat = db.Column(db.DateTime, default=datetime.utcnow)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # 关联消息
    sent_messages = db.relationship('Message', foreign_keys='Message.sender_id',
                                   backref='sender', lazy='dynamic')
    received_messages = db.relationship('Message', foreign_keys='Message.receiver_id',
                                        backref='receiver', lazy='dynamic')

    def to_dict(self, brief=False):
        data = {
            'id': self.id,
            'name': self.name,
            'agent_key': self.agent_key,
            'role': self.role,
            'project_name': self.project_name,
            'module_name': self.module_name,
            'status': self.status,
            'last_heartbeat': self.last_heartbeat.isoformat() if self.last_heartbeat else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }
        return data

    def verify_token(self, token):
        """验证 API Token"""
        if not self.api_token_hash or not token:
            return False
        return self.api_token_hash == hash_token(token)


class Message(db.Model):
    """消息表"""
    __tablename__ = 'messages'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    sender_id = db.Column(db.Integer, db.ForeignKey('agents.id'), nullable=False)
    receiver_id = db.Column(db.Integer, db.ForeignKey('agents.id'), nullable=False)
    content = db.Column(db.Text, nullable=False, comment='消息内容')
    msg_type = db.Column(db.String(30), default='text',
                         comment='消息类型：text/task_delegate/knowledge_share/request_help/report')
    status = db.Column(db.String(20), default='unread',
                       comment='状态：unread/read/archived')
    extra_data = db.Column(db.JSON, comment='附加数据，如任务ID、知识ID等')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    read_at = db.Column(db.DateTime, comment='读取时间')

    def to_dict(self):
        return {
            'id': self.id,
            'sender_id': self.sender_id,
            'sender_name': self.sender.name if self.sender else None,
            'sender_key': self.sender.agent_key if self.sender else None,
            'receiver_id': self.receiver_id,
            'receiver_name': self.receiver.name if self.receiver else None,
            'receiver_key': self.receiver.agent_key if self.receiver else None,
            'content': self.content,
            'msg_type': self.msg_type,
            'status': self.status,
            'metadata': self.extra_data,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'read_at': self.read_at.isoformat() if self.read_at else None,
        }


class Conversation(db.Model):
    """会话关联表（两个 Agent 之间的对话索引）"""
    __tablename__ = 'conversations'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    agent_a_id = db.Column(db.Integer, db.ForeignKey('agents.id'), nullable=False)
    agent_b_id = db.Column(db.Integer, db.ForeignKey('agents.id'), nullable=False)
    last_message_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    agent_a = db.relationship('Agent', foreign_keys=[agent_a_id])
    agent_b = db.relationship('Agent', foreign_keys=[agent_b_id])

    __table_args__ = (
        db.UniqueConstraint('agent_a_id', 'agent_b_id',
                           name='uq_conversation_pair'),
    )

    def to_dict(self, with_last_message=False):
        data = {
            'id': self.id,
            'agent_a_id': self.agent_a_id,
            'agent_a_name': self.agent_a.name if self.agent_a else None,
            'agent_a_key': self.agent_a.agent_key if self.agent_a else None,
            'agent_b_id': self.agent_b_id,
            'agent_b_name': self.agent_b.name if self.agent_b else None,
            'agent_b_key': self.agent_b.agent_key if self.agent_b else None,
            'last_message_at': self.last_message_at.isoformat() if self.last_message_at else None,
        }
        return data
