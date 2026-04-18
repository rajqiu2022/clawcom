import secrets
import hashlib
import base64
from datetime import datetime, timezone, timedelta
from app import db
from werkzeug.security import generate_password_hash, check_password_hash

# 使用北京时间（UTC+8）代替 UTC
_CST = timezone(timedelta(hours=8))


def _now():
    return datetime.now(_CST).replace(tzinfo=None)


class User(db.Model):
    """用户表"""
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    username = db.Column(db.String(50), nullable=False, unique=True)
    password_hash = db.Column(db.String(256), nullable=False)
    display_name = db.Column(db.String(50), comment='显示名称')
    role = db.Column(db.Enum('super_admin', 'admin', 'user', 'guest'), default='user',
                     comment='super_admin=超级管理员, admin=项目管理员, user=成员, guest=访客(只读)')
    bound_claw_id = db.Column(db.Integer, comment='绑定的 OpenClaw ID')
    managed_projects = db.Column(db.JSON, comment='管理的项目ID列表（admin角色用）')
    created_at = db.Column(db.DateTime, default=_now)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def to_dict(self):
        data = {
            'id': self.id,
            'username': self.username,
            'display_name': self.display_name or self.username,
            'role': self.role,
            'bound_claw_id': self.bound_claw_id,
            'managed_projects': self.managed_projects or [],
            'created_at': str(self.created_at) if self.created_at else None,
        }
        # 添加绑定的 claw 名（供前端权限判断）
        if self.bound_claw_id:
            claw = OpenClawInstance.query.get(self.bound_claw_id)
            if claw:
                data['bound_claw_name'] = claw.name
        return data


# 简单加密，用于存储可还原的 token 明文
# 注意：实际生产环境建议使用更安全的密钥管理
_ENCODING_KEY = base64.urlsafe_b64encode(b'openaclaw-secret-key-32bytes!')


def _simple_encrypt(text: str) -> str:
    """简单 XOR 加密（可还原）"""
    key = _ENCODING_KEY
    result = []
    for i, c in enumerate(text):
        result.append(chr(ord(c) ^ key[i % len(key)]))
    return base64.urlsafe_b64encode(''.join(result).encode()).decode()


def _simple_decrypt(encrypted: str) -> str:
    """解密"""
    try:
        data = base64.urlsafe_b64decode(encrypted.encode()).decode()
        key = _ENCODING_KEY
        result = []
        for i, c in enumerate(data):
            result.append(chr(ord(c) ^ key[i % len(key)]))
        return ''.join(result)
    except Exception:
        return ''


def generate_api_token():
    """生成 OpenClaw API Token"""
    return 'oc_tk_' + secrets.token_hex(24)


def hash_token(token):
    """对 Token 做 SHA256 哈希（存储用）"""
    return hashlib.sha256(token.encode()).hexdigest()


class OpenClawInstance(db.Model):
    """OpenClaw 实例表"""
    __tablename__ = 'openclaw_instances'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(50), nullable=False, comment='OpenClaw 昵称')
    claw_tag = db.Column(db.String(50), nullable=False, unique=True,
                         comment='Memos 归属标签，如 claw-小测')
    owner = db.Column(db.String(50), nullable=False, comment='所属用户')
    role_title = db.Column(db.String(100), comment='职位头衔')
    responsibilities = db.Column(db.Text, comment='负责内容描述')
    project_name = db.Column(db.String(100), comment='所属项目')
    module_name = db.Column(db.String(100), comment='所属模块')
    avatar = db.Column(db.String(255), comment='头像URL')
    status = db.Column(db.String(20), default='offline',
                       comment='状态：休息/摸鱼/工作/学习/offline/deleted')
    role = db.Column(db.Enum('admin', 'test_manager', 'module_owner', 'specialist',
                             'test_member', 'test_executor'),
                     default='module_owner',
                     comment='角色：test_manager=测试经理，module_owner=模块负责人，specialist=专项测试')
    connection_mode = db.Column(db.Enum('sse', 'polling'),
                                default='polling',
                                comment='连接方式：polling=轮询（推荐），sse=SSE长连接（保留）')
    last_activity = db.Column(db.DateTime, comment='最后活动时间（SSE/心跳/MCP调用时更新）')
    deleted_at = db.Column(db.DateTime, comment='软删除时间，非空表示已删除')
    soul_config = db.Column(db.Text, comment='SOUL.md 内容')
    workflow_config = db.Column(db.Text, comment='工作规范')
    report_schedule = db.Column(db.String(100), default='15:00,21:00',
                                comment='上报时间配置')
    web_system_url = db.Column(db.String(255),
                               comment='Web 管理系统地址')
    api_token_hash = db.Column(db.String(64), comment='API Token 哈希值')
    api_token_plain = db.Column(db.String(128),
                                comment='加密存储的 Token 明文（可还原）')
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'),
                           comment='所属项目ID')
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now,
                           onupdate=_now)

    project = db.relationship('Project', backref='openclaws')

    # 关联
    skills = db.relationship('OpenClawSkill', backref='openclaw',
                             lazy='dynamic', cascade='all, delete-orphan')
    reports = db.relationship('DailyReport', backref='openclaw',
                              lazy='dynamic', cascade='all, delete-orphan')

    def to_dict(self, brief=False):
        data = {
            'id': self.id,
            'name': self.name,
            'claw_tag': self.claw_tag,
            'owner': self.owner,
            'role_title': self.role_title,
            'responsibilities': self.responsibilities,
            'project_id': self.project_id,
            'project_name': self.project.name if self.project else self.project_name,
            'module_name': self.module_name,
            'avatar': self.avatar,
            'status': self.status,
            'role': self.role,
            'connection_mode': self.connection_mode or 'sse',
            'last_activity': str(self.last_activity) if self.last_activity else None,
            'report_schedule': self.report_schedule,
            'created_at': str(self.created_at) if self.created_at else None,
            'api_token_preview': self.get_token_preview() if self.api_token_plain else None,
        }
        if not brief:
            data.update({
                'soul_config': self.soul_config,
                'workflow_config': self.workflow_config,
                'web_system_url': self.web_system_url,
                'updated_at': str(self.updated_at) if self.updated_at else None,
                'skills': [s.skill.to_dict() for s in self.skills if s.enabled and s.skill],
            })
        return data

    def verify_token(self, token):
        """验证 API Token"""
        if not self.api_token_hash or not token:
            return False
        return self.api_token_hash == hash_token(token)

    def get_token_preview(self):
        """获取 Token 预览（头尾各6字符）"""
        plain = self.get_token_plain()
        if plain and len(plain) > 16:
            return plain[:10] + '...' + plain[-6:]
        return plain or ''

    def get_token_plain(self):
        """获取 Token 明文（解密）"""
        if self.api_token_plain:
            return _simple_decrypt(self.api_token_plain)
        return ''


class Skill(db.Model):
    """Skills 库 — 每个 Skill 是一个「文档包」，存储在磁盘目录中"""
    __tablename__ = 'skills'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(100), nullable=False, unique=True,
                     comment='Skill 标识名')
    display_name = db.Column(db.String(100), nullable=False,
                             comment='显示名称')
    description = db.Column(db.Text, comment='功能描述')
    category = db.Column(db.String(30),
                         default='standard',
                         comment='分类：standard/hub_system/project/business_test/special_test/evolved')
    trigger_phrase = db.Column(db.String(255), comment='触发短语')
    template_content = db.Column(db.Text, comment='Skill 主文件内容（SKILL.md）')
    pack_path = db.Column(db.String(500), comment='文档包磁盘路径，如 hub-store/skills/bootstrap-init')
    files = db.Column(db.JSON, comment='文档包文件清单 [{name, description}]')
    scope = db.Column(db.Enum('global', 'project', 'module', 'admin', 'scoped'),
                      default='global', comment='作用域')
    applicable_projects = db.Column(db.JSON, comment='适用项目ID列表')
    applicable_modules = db.Column(db.JSON, comment='适用模块名列表')
    used_by_count = db.Column(db.Integer, default=0,
                              comment='使用中的 OpenClaw 数量')
    is_standard = db.Column(db.Boolean, default=False,
                           comment='是否标准化 Skills（注册时自动安装）')

    # OpenSpace 进化指标（仅 evolved 类型有值）
    evolve_source = db.Column(db.String(100),
                              comment='进化来源 OpenClaw 名称')
    success_rate = db.Column(db.Float, default=0,
                             comment='成功率（0-100）')
    total_runs = db.Column(db.Integer, default=0,
                           comment='总执行次数')
    error_count = db.Column(db.Integer, default=0,
                            comment='错误次数')
    last_evolved_at = db.Column(db.DateTime,
                                comment='最后进化时间')
    evolve_history = db.Column(db.JSON,
                               comment='进化历史 [{action, timestamp, detail}]')

    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now,
                           onupdate=_now)
    created_by = db.Column(db.String(100), default='system',
                           comment='创建者/提交人')
    review_status = db.Column(db.Enum('approved', 'pending', 'revise', 'rejected'),
                              default='approved',
                              comment='评审状态：approved=已通过，pending=待评审，revise=待修改，rejected=已废弃')
    review_comment = db.Column(db.Text, comment='审核意见（打回/废弃时填写）')
    is_deleted = db.Column(db.Boolean, default=False,
                           comment='软删除标记：True=已删除（隐藏），False=正常')
    deleted_at = db.Column(db.DateTime, comment='软删除时间')
    rating = db.Column(db.Float, default=3.0,
                       comment='星级评分（1~5，支持0.5步进，默认3）')

    def to_dict(self):
        data = {
            'id': self.id,
            'name': self.name,
            'display_name': self.display_name,
            'description': self.description,
            'category': self.category,
            'trigger_phrase': self.trigger_phrase,
            'template_content': self.template_content,
            'pack_path': self.pack_path,
            'files': self.files or [],
            'scope': self.scope,
            'applicable_projects': self.applicable_projects or [],
            'applicable_modules': self.applicable_modules or [],
            'used_by_count': self.used_by_count,
            'is_standard': self.is_standard,
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
            'created_by': self.created_by or 'system',
            'install_count': len([i for i in (self.installations or []) if i.enabled]),
            'review_status': self.review_status or 'approved',
            'review_comment': self.review_comment or None,
            'is_deleted': self.is_deleted or False,
            'rating': self.rating if self.rating is not None else 3.0,
        }
        # 进化指标
        if self.category == 'evolved':
            data.update({
                'evolve_source': self.evolve_source,
                'success_rate': self.success_rate,
                'total_runs': self.total_runs,
                'error_count': self.error_count,
                'last_evolved_at': (str(self.last_evolved_at)
                                    if self.last_evolved_at else None),
                'evolve_history': self.evolve_history or [],
            })
        return data


class SkillFile(db.Model):
    """Skill 文档包文件（内容存 MySQL）"""
    __tablename__ = 'skill_files'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    skill_id = db.Column(db.Integer, db.ForeignKey('skills.id'), nullable=False)
    filename = db.Column(db.String(200), nullable=False, comment='文件名，如 SOUL.md')
    content = db.Column(db.Text, comment='文件内容')
    file_type = db.Column(db.String(20), default='markdown',
                          comment='文件类型：markdown/yaml/json/text')
    description = db.Column(db.String(500), comment='文件说明')
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    skill = db.relationship('Skill', backref=db.backref('file_entries', lazy='dynamic',
                            cascade='all, delete-orphan'))

    __table_args__ = (
        db.UniqueConstraint('skill_id', 'filename', name='uq_skill_file'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'skill_id': self.skill_id,
            'filename': self.filename,
            'content': self.content,
            'file_type': self.file_type,
            'description': self.description,
            'size': len(self.content) if self.content else 0,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }


class OpenClawSkill(db.Model):
    """OpenClaw 技能关联表"""
    __tablename__ = 'openclaw_skills'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    openclaw_id = db.Column(db.Integer,
                            db.ForeignKey('openclaw_instances.id'),
                            nullable=False)
    skill_id = db.Column(db.Integer, db.ForeignKey('skills.id'),
                         nullable=False)
    enabled = db.Column(db.Boolean, default=True)
    installed_at = db.Column(db.DateTime, default=_now)

    skill = db.relationship('Skill', backref='installations')

    __table_args__ = (
        db.UniqueConstraint('openclaw_id', 'skill_id',
                            name='uq_openclaw_skill'),
    )


class DailyReport(db.Model):
    """工作日报"""
    __tablename__ = 'daily_reports'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    openclaw_id = db.Column(db.Integer,
                            db.ForeignKey('openclaw_instances.id'),
                            nullable=False)
    report_date = db.Column(db.Date, nullable=False)
    report_time = db.Column(db.Time, nullable=False, comment='上报时间')
    tasks_completed = db.Column(db.JSON, comment='完成任务')
    knowledge_recorded = db.Column(db.JSON, comment='新增知识')
    experience_shared = db.Column(db.JSON, comment='经验记录')
    knowledge_learned = db.Column(db.JSON, comment='学习的共享知识')
    ai_summary = db.Column(db.Text, comment='AI 生成的工作小结')
    created_at = db.Column(db.DateTime, default=_now)

    __table_args__ = (
        db.UniqueConstraint('openclaw_id', 'report_date', 'report_time',
                            name='uq_daily_report'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'openclaw_id': self.openclaw_id,
            'report_date': str(self.report_date) if self.report_date else None,
            'report_time': self.report_time.strftime('%H:%M') if self.report_time else None,
            'tasks_completed': self.tasks_completed,
            'knowledge_recorded': self.knowledge_recorded,
            'experience_shared': self.experience_shared,
            'knowledge_learned': self.knowledge_learned,
            'ai_summary': self.ai_summary,
            'created_at': str(self.created_at) if self.created_at else None,
        }


class KnowledgeEntry(db.Model):
    """知识条目"""
    __tablename__ = 'knowledge_entries'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    memos_id = db.Column(db.String(100), comment='Memos 中的 memo name')
    title = db.Column(db.String(255), nullable=False)
    content = db.Column(db.Text, nullable=False)
    category = db.Column(db.String(50), nullable=False,
                         comment='标签分类')
    scope = db.Column(db.Enum('global', 'project', 'module'),
                      nullable=False, default='global')
    project_name = db.Column(db.String(100), default=None)
    module_name = db.Column(db.String(100), default=None)
    source_openclaw_id = db.Column(
        db.Integer, db.ForeignKey('openclaw_instances.id'))
    source_type = db.Column(
        db.Enum('openclaw', 'openspace', 'manual'),
        default='manual',
        comment='来源类型：openclaw=Agent记录，openspace=自动进化，manual=手动录入')
    status = db.Column(
        db.Enum('draft', 'pending_review', 'approved', 'rejected'),
        default='draft')
    reviewer_notes = db.Column(db.Text, comment='审核意见')
    approved_at = db.Column(db.DateTime)
    approved_by = db.Column(db.String(50))
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now,
                           onupdate=_now)

    source_openclaw = db.relationship('OpenClawInstance',
                                      backref='knowledge_entries')

    def to_dict(self):
        return {
            'id': self.id,
            'memos_id': self.memos_id,
            'title': self.title,
            'content': self.content,
            'category': self.category,
            'scope': self.scope,
            'project_name': self.project_name,
            'module_name': self.module_name,
            'source_openclaw_id': self.source_openclaw_id,
            'source_openclaw_name': (self.source_openclaw.name
                                     if self.source_openclaw else None),
            'source_type': self.source_type,
            'status': self.status,
            'reviewer_notes': self.reviewer_notes,
            'approved_at': (str(self.approved_at)
                           if self.approved_at else None),
            'approved_by': self.approved_by,
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }


class KnowledgeDistribution(db.Model):
    """知识共享记录"""
    __tablename__ = 'knowledge_distributions'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    knowledge_id = db.Column(db.Integer,
                             db.ForeignKey('knowledge_entries.id'),
                             nullable=False)
    target_scope = db.Column(db.Enum('all', 'project', 'module'),
                             nullable=False)
    target_project = db.Column(db.String(100))
    target_module = db.Column(db.String(100))
    distributed_at = db.Column(db.DateTime, default=_now)
    distributed_by = db.Column(db.String(50))

    knowledge = db.relationship('KnowledgeEntry',
                                backref='distributions')


class Project(db.Model):
    """项目表"""
    __tablename__ = 'projects'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    description = db.Column(db.Text)
    tapd_workspace_id = db.Column(db.String(50), comment='TAPD 项目 workspace ID')
    created_at = db.Column(db.DateTime, default=_now)

    modules = db.relationship('Module', backref='project',
                              lazy='dynamic', cascade='all, delete-orphan')

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'tapd_workspace_id': self.tapd_workspace_id,
            'modules': [m.to_dict() for m in self.modules],
            'created_at': str(self.created_at) if self.created_at else None,
        }


class Module(db.Model):
    """模块表（独立于项目）"""
    __tablename__ = 'modules'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'),
                           nullable=True, comment='关联项目（可选，向后兼容）')
    name = db.Column(db.String(100), nullable=False, unique=True)
    description = db.Column(db.Text)
    category = db.Column(db.String(50), default='other',
                         comment='模块分类：peripheral/core_gameplay/commercialization/client_performance/server_special/other')
    created_at = db.Column(db.DateTime, default=_now)

    def to_dict(self):
        return {
            'id': self.id,
            'project_id': self.project_id,
            'name': self.name,
            'description': self.description,
            'category': self.category or 'other',
        }


# ============== Agent Hub 通信中心模型 ==============

def generate_agent_token():
    """生成 Agent Hub API Token"""
    return 'hub_tk_' + secrets.token_hex(24)


class Agent(db.Model):
    """Agent 注册表（通信中心）"""
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
    last_activity = db.Column(db.DateTime, default=_now)
    created_at = db.Column(db.DateTime, default=_now)

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
            'last_activity': str(self.last_activity) if self.last_activity else None,
            'created_at': str(self.created_at) if self.created_at else None,
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
    created_at = db.Column(db.DateTime, default=_now)
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
            'created_at': str(self.created_at) if self.created_at else None,
            'read_at': str(self.read_at) if self.read_at else None,
        }


class Conversation(db.Model):
    """会话关联表（两个 Agent 之间的对话索引）"""
    __tablename__ = 'conversations'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    agent_a_id = db.Column(db.Integer, db.ForeignKey('agents.id'), nullable=False)
    agent_b_id = db.Column(db.Integer, db.ForeignKey('agents.id'), nullable=False)
    last_message_at = db.Column(db.DateTime, default=_now)
    created_at = db.Column(db.DateTime, default=_now)

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
            'last_message_at': str(self.last_message_at) if self.last_message_at else None,
        }
        return data


# ============== Rules 工作规范模型 ==============

class Rule(db.Model):
    """Rules 工作规范库"""
    __tablename__ = 'rules'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(100), nullable=False, unique=True,
                     comment='Rule 标识名')
    display_name = db.Column(db.String(100), nullable=False,
                             comment='显示名称')
    description = db.Column(db.Text, comment='功能描述')
    category = db.Column(db.String(30),
                        default='standard',
                        comment='分类：standard/hub_system/project/business_test/special_test/evolved')
    scope = db.Column(db.Enum('global', 'project', 'module', 'admin', 'scoped'),
                      default='global', comment='作用域')
    owner_claw_id = db.Column(db.Integer, db.ForeignKey('openclaw_instances.id'),
                              nullable=True, comment='所属 OpenClaw ID')
    applicable_projects = db.Column(db.JSON, comment='适用项目ID列表')
    applicable_modules = db.Column(db.JSON, comment='适用模块名列表（scope=module时）')

    # 生成的规范内容（Markdown格式，会被写入OpenClaw的配置文件）
    content_template = db.Column(db.Text, comment='规范内容模板')
    is_standard = db.Column(db.Boolean, default=False,
                           comment='是否标准化 Rules（注册时自动安装）')

    # 关联到哪些 OpenClaw
    openclaws = db.relationship('OpenClawRule', backref='rule',
                               lazy='dynamic', cascade='all, delete-orphan')

    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now,
                          onupdate=_now)
    created_by = db.Column(db.String(100), default='system',
                           comment='创建者/提交人')
    review_status = db.Column(db.Enum('approved', 'pending', 'revise', 'rejected'),
                              default='approved',
                              comment='评审状态：approved=已通过，pending=待评审，revise=待修改，rejected=已废弃')
    review_comment = db.Column(db.Text, comment='审核意见（打回/废弃时填写）')
    is_deleted = db.Column(db.Boolean, default=False,
                           comment='软删除标记：True=已删除（隐藏），False=正常')
    deleted_at = db.Column(db.DateTime, comment='软删除时间')

    def to_dict(self):
        install_count = self.openclaws.filter_by(enabled=True).count()
        return {
            'id': self.id,
            'name': self.name,
            'display_name': self.display_name,
            'description': self.description,
            'category': self.category,
            'scope': self.scope,
            'owner_claw_id': self.owner_claw_id,
            'is_standard': self.is_standard,
            'applicable_projects': self.applicable_projects or [],
            'applicable_modules': self.applicable_modules or [],
            'content_template': self.content_template,
            'openclaw_ids': [r.openclaw_id for r in self.openclaws.filter_by(enabled=True)],
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
            'created_by': self.created_by or 'system',
            'install_count': install_count,
            'review_status': self.review_status or 'approved',
            'review_comment': self.review_comment or None,
            'is_deleted': self.is_deleted or False,
        }


class OpenClawRule(db.Model):
    """OpenClaw 与 Rule 的关联表"""
    __tablename__ = 'openclaw_rules'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    openclaw_id = db.Column(db.Integer,
                            db.ForeignKey('openclaw_instances.id'),
                            nullable=False)
    rule_id = db.Column(db.Integer, db.ForeignKey('rules.id'),
                        nullable=False)
    enabled = db.Column(db.Boolean, default=True)
    applied_at = db.Column(db.DateTime, comment='应用到OpenClaw的时间')
    applied = db.Column(db.Boolean, default=False, comment='是否已应用')

    __table_args__ = (
        db.UniqueConstraint('openclaw_id', 'rule_id',
                           name='uq_openclaw_rule'),
    )


# ============== 标准包模型 ==============

class StandardPack(db.Model):
    """标准包 — 语义纯净的资源捆绑

    pack_type='skill' 的包只包含 Skill ID 列表
    pack_type='rule'  的包只包含 Rule ID 列表
    不混合。下发时 Hub 负责把包内 id 同步到具体 claw 的安装状态。
    """
    __tablename__ = 'standard_packs'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(100), nullable=False, unique=True,
                     comment='包标识名')
    display_name = db.Column(db.String(100), nullable=False,
                             comment='显示名称')
    description = db.Column(db.Text, comment='包描述')
    pack_type = db.Column(db.Enum('skill', 'rule'), nullable=False,
                          comment='包类型：skill=Skills 标准包，rule=Rules 标准包')
    item_ids = db.Column(db.Text, default='[]',
                         comment='包含的 Skill/Rule ID 列表，JSON数组')
    is_active = db.Column(db.Boolean, default=True,
                          comment='是否激活（激活的包在注册时自动下发）')
    created_by = db.Column(db.String(100), default='system', comment='创建者')
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now,
                           onupdate=_now)

    def _get_item_ids(self):
        import json
        try:
            return json.loads(self.item_ids) if self.item_ids else []
        except (json.JSONDecodeError, TypeError):
            return []

    def _set_item_ids(self, ids):
        import json
        self.item_ids = json.dumps(ids or [])

    def to_dict(self):
        ids = self._get_item_ids()
        return {
            'id': self.id,
            'name': self.name,
            'display_name': self.display_name,
            'description': self.description,
            'pack_type': self.pack_type,
            'item_ids': ids,
            'item_count': len(ids),
            'is_active': self.is_active,
            'created_by': self.created_by,
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }


# ============== AI 用例库模型 ==============

class TestCaseLibrary(db.Model):
    """测试用例库"""
    __tablename__ = 'test_case_libraries'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(100), nullable=False,
                     comment='用例库名称')
    description = db.Column(db.Text, comment='用例库描述')
    project_name = db.Column(db.String(100), comment='所属项目')
    module_name = db.Column(db.String(100), comment='所属模块')
    owner = db.Column(db.String(50), comment='负责人')
    status = db.Column(db.Enum('active', 'archived'),
                      default='active', comment='状态')

    # 脑图结构（JSON格式）
    mindmap = db.Column(db.JSON, comment='脑图结构')

    cases = db.relationship('TestCase', backref='library',
                           lazy='dynamic', cascade='all, delete-orphan')

    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now,
                          onupdate=_now)

    def to_dict(self, with_cases=False):
        data = {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'project_name': self.project_name,
            'module_name': self.module_name,
            'owner': self.owner,
            'status': self.status,
            'mindmap': self.mindmap,
            'case_count': self.cases.count(),
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }
        if with_cases:
            data['cases'] = [c.to_dict() for c in self.cases]
        return data


class TestCase(db.Model):
    """测试用例"""
    __tablename__ = 'test_cases'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    library_id = db.Column(db.Integer,
                           db.ForeignKey('test_case_libraries.id'),
                           nullable=False)
    case_id = db.Column(db.String(100), comment='用例编号')
    title = db.Column(db.String(255), nullable=False, comment='用例标题')
    priority = db.Column(db.Enum('P0', 'P1', 'P2', 'P3'),
                        default='P2', comment='优先级')
    type = db.Column(db.Enum('functional', 'interface', 'performance', 'security'),
                    default='functional', comment='用例类型')

    # 用例内容（JSON格式，包含步骤、预期结果等）
    content = db.Column(db.JSON, comment='用例详细内容')

    # 脑图节点ID（用于脑图定位）
    mindmap_node_id = db.Column(db.String(50), comment='脑图节点ID')

    # AI 生成标记
    ai_generated = db.Column(db.Boolean, default=False,
                            comment='是否AI生成')
    ai_prompt = db.Column(db.Text, comment='AI生成时的prompt')

    tags = db.Column(db.JSON, comment='标签')

    # 目录路径（如 "登录模块/手机号登录"，用于左侧目录树分组）
    module_path = db.Column(db.String(500), default='', comment='目录路径，用 / 分隔')
    is_placeholder = db.Column(db.Boolean, default=False,
                               comment='是否为目录占位用例（空目录的占位符，不在列表中显示）')
    created_by = db.Column(db.String(100), default='', comment='创建人')

    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now,
                          onupdate=_now)

    def to_dict(self):
        return {
            'id': self.id,
            'library_id': self.library_id,
            'case_id': self.case_id,
            'title': self.title,
            'priority': self.priority,
            'type': self.type,
            'content': self.content,
            'mindmap_node_id': self.mindmap_node_id,
            'ai_generated': self.ai_generated,
            'tags': self.tags or [],
            'module_path': self.module_path or '',
            'is_placeholder': self.is_placeholder or False,
            'created_by': self.created_by or '',
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }


# ============== 用例库版本快照 ==============

class TestCaseSnapshot(db.Model):
    """用例库版本快照 — 类 git commit

    每次快照保存用例库的全量状态（所有用例的 JSON），支持：
    - 手动创建快照（commit）
    - 自动快照（批量操作/AI生成前后）
    - 回滚到任意版本（checkout）
    - 版本对比（diff）
    """
    __tablename__ = 'testcase_snapshots'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    library_id = db.Column(db.Integer, db.ForeignKey('test_case_libraries.id'),
                           nullable=False, comment='所属用例库')
    version = db.Column(db.Integer, nullable=False, comment='版本号（自增）')
    tag = db.Column(db.String(100), comment='版本标签，如 v1.0、AI生成后、发版前')
    message = db.Column(db.String(500), comment='提交说明（类似 git commit message）')
    snapshot_type = db.Column(db.String(20), default='manual',
                              comment='快照类型：manual=手动, auto=自动, ai=AI操作前后, rollback=回滚')
    case_count = db.Column(db.Integer, default=0, comment='快照时的用例总数')
    cases_data = db.Column(db.Text, comment='全量用例数据 JSON')
    diff_summary = db.Column(db.Text, comment='与上一版本的差异摘要 JSON')
    created_by = db.Column(db.String(100), default='system', comment='操作者')
    created_at = db.Column(db.DateTime, default=_now)

    library = db.relationship('TestCaseLibrary', backref=db.backref(
        'snapshots', lazy='dynamic', cascade='all, delete-orphan'))

    __table_args__ = (
        db.UniqueConstraint('library_id', 'version', name='uq_snapshot_version'),
    )

    def to_dict(self, with_data=False):
        d = {
            'id': self.id,
            'library_id': self.library_id,
            'version': self.version,
            'tag': self.tag,
            'message': self.message,
            'snapshot_type': self.snapshot_type,
            'case_count': self.case_count,
            'diff_summary': self.diff_summary,
            'created_by': self.created_by,
            'created_at': str(self.created_at) if self.created_at else None,
        }
        if with_data:
            import json as _json
            try:
                d['cases_data'] = _json.loads(self.cases_data) if self.cases_data else []
            except Exception:
                d['cases_data'] = []
        return d


# ============== 系统配置 KV 表 ==============

class SystemConfig(db.Model):
    """系统配置键值对"""
    __tablename__ = 'system_config'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    config_key = db.Column(db.String(100), nullable=False, unique=True, comment='配置项名称')
    value = db.Column(db.Text, comment='配置值（JSON 字符串）')
    description = db.Column(db.String(255), comment='说明')
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)


# ============== OpenClaw 消息模型 ==============

class ClawMessage(db.Model):
    """OpenClaw 消息表（支持双向：Web ↔ OpenClaw）"""
    __tablename__ = 'claw_messages'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    claw_id = db.Column(db.Integer, db.ForeignKey('openclaw_instances.id'),
                       nullable=False, comment='关联的 OpenClaw ID')
    sender_name = db.Column(db.String(50), default='Web Admin',
                          comment='发送者名称')
    content = db.Column(db.Text, nullable=False, comment='消息内容')
    msg_type = db.Column(db.String(30), default='text',
                        comment='消息类型：text/task_delegate/knowledge_share/request_help')
    direction = db.Column(db.String(10), default='to_claw',
                         comment='方向：to_claw=Web发给OpenClaw, from_claw=OpenClaw发给Web')
    reply_to = db.Column(db.Integer, comment='回复的消息ID')
    status = db.Column(db.String(20), default='pending',
                      comment='状态：pending/delivered/read')
    delivered_at = db.Column(db.DateTime, comment='送达时间')
    read_at = db.Column(db.DateTime, comment='读取时间')
    created_at = db.Column(db.DateTime, default=_now)

    # 关联到 OpenClaw
    claw = db.relationship('OpenClawInstance', backref='messages')

    def to_dict(self):
        return {
            'id': self.id,
            'claw_id': self.claw_id,
            'sender_name': self.sender_name,
            'content': self.content,
            'msg_type': self.msg_type,
            'direction': self.direction or 'to_claw',
            'reply_to': self.reply_to,
            'status': self.status,
            'delivered_at': str(self.delivered_at) if self.delivered_at else None,
            'read_at': str(self.read_at) if self.read_at else None,
            'created_at': str(self.created_at) if self.created_at else None,
        }


class AuditLog(db.Model):
    """操作审计日志"""
    __tablename__ = 'audit_logs'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    action = db.Column(db.String(50), nullable=False,
                       comment='操作类型：create/update/delete/review/login等')
    resource_type = db.Column(db.String(50), nullable=False,
                              comment='资源类型：openclaw/skill/knowledge/rule/report等')
    resource_id = db.Column(db.Integer, comment='资源ID')
    resource_name = db.Column(db.String(200), comment='资源名称')
    operator = db.Column(db.String(100), default='system',
                         comment='操作者')
    ip_address = db.Column(db.String(45), comment='IP地址')
    detail = db.Column(db.Text, comment='操作详情/变更内容')
    created_at = db.Column(db.DateTime, default=_now)

    def to_dict(self):
        return {
            'id': self.id,
            'action': self.action,
            'resource_type': self.resource_type,
            'resource_id': self.resource_id,
            'resource_name': self.resource_name,
            'operator': self.operator,
            'ip_address': self.ip_address,
            'detail': self.detail,
            'created_at': str(self.created_at) if self.created_at else None,
        }


# ============== OpenClaw 待办系统 ==============

class ClawTodo(db.Model):
    """OpenClaw 待办任务表

    urgency_level 紧急度分 5 级：
      interrupt  — 定时中断：到点必须中断当前任务立即执行
      flexible   — 当天弹性：有时间要求但可推后，当天完成即可
      background — 后台任务：无时间要求，重要不紧急，空闲时做（初始化验证等）
      periodic   — 周期容错-跳过：错过就下一周期，但要上报 skipped 记录
      retry      — 周期容错-重试：错过延后 retry_delay 分钟重试 retry_max 次，仍失败则上报
    """
    __tablename__ = 'claw_todos'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    openclaw_id = db.Column(db.Integer, db.ForeignKey('openclaw_instances.id'),
                            nullable=False, comment='归属 OpenClaw')
    title = db.Column(db.String(200), nullable=False, comment='待办标题')
    description = db.Column(db.Text, comment='详细描述/执行要求')

    # --- 调度参数 ---
    schedule_type = db.Column(db.String(20), nullable=False, default='daily',
                              comment='频率：daily/weekly/monthly/once')
    schedule_time = db.Column(db.String(10),
                              comment='定时时间 HH:MM（interrupt/flexible 必填，background 留空）')
    schedule_day = db.Column(db.Integer,
                             comment='周几(1-7)/几号(1-31)，weekly/monthly 时用')

    # --- 紧急度 ---
    urgency_level = db.Column(db.String(20), default='flexible',
                              comment='interrupt/flexible/background/periodic/retry')

    # --- 重试策略（仅 retry 级别用）---
    retry_delay = db.Column(db.Integer, default=5,
                            comment='重试延迟（分钟），默认 5')
    retry_max = db.Column(db.Integer, default=1,
                          comment='最大重试次数，默认 1')

    # --- 分类与元信息 ---
    priority = db.Column(db.String(5), default='P1', comment='展示优先级 P0/P1/P2')
    task_category = db.Column(db.String(20), default='routine',
                              comment='类别：routine/init/onboard')
    verification_target = db.Column(db.String(200),
                                    comment='验证目标（init 任务用）')
    enabled = db.Column(db.Boolean, default=True, comment='是否启用')
    created_by = db.Column(db.String(100), default='system')
    created_at = db.Column(db.DateTime, default=_now)

    openclaw = db.relationship('OpenClawInstance', backref='todos')
    logs = db.relationship('ClawTodoLog', backref='todo',
                           lazy='dynamic', cascade='all, delete-orphan')

    def to_dict(self, with_today_status=False):
        data = {
            'id': self.id,
            'openclaw_id': self.openclaw_id,
            'title': self.title,
            'description': self.description,
            'schedule_type': self.schedule_type,
            'schedule_time': self.schedule_time,
            'schedule_day': self.schedule_day,
            'urgency_level': self.urgency_level or 'flexible',
            'retry_delay': self.retry_delay,
            'retry_max': self.retry_max,
            'priority': self.priority,
            'task_category': self.task_category or 'routine',
            'verification_target': self.verification_target,
            'enabled': self.enabled,
            'created_by': self.created_by,
            'created_at': str(self.created_at) if self.created_at else None,
        }
        if with_today_status:
            from datetime import date as d
            today_log = ClawTodoLog.query.filter_by(
                todo_id=self.id, log_date=d.today()
            ).first()
            data['today_status'] = today_log.status if today_log else 'pending'
            data['today_completed_at'] = (
                str(today_log.completed_at) if today_log and today_log.completed_at else None
            )
        return data

    def schedule_label(self):
        """生成可读的调度描述"""
        weekdays = {1: '一', 2: '二', 3: '三', 4: '四', 5: '五', 6: '六', 7: '日'}
        urgency_labels = {
            'interrupt': '⚡中断执行',
            'flexible': '📋当天完成',
            'background': '🔄空闲执行',
            'periodic': '🔁周期(跳过)',
            'retry': '🔁周期(重试)',
        }
        time_str = f' {self.schedule_time}' if self.schedule_time else ''
        urgency = urgency_labels.get(self.urgency_level, '')

        if self.schedule_type == 'daily':
            base = f'每天{time_str}' if time_str else '每天（不限时间）'
        elif self.schedule_type == 'weekly':
            day = weekdays.get(self.schedule_day, '?')
            base = f'每周{day}{time_str}'
        elif self.schedule_type == 'monthly':
            base = f'每月{self.schedule_day}号{time_str}'
        elif self.schedule_type == 'once':
            base = f'一次性{time_str}'
        else:
            base = self.schedule_type

        return f'{base} [{urgency}]' if urgency else base


class ClawTodoLog(db.Model):
    """OpenClaw 待办执行记录"""
    __tablename__ = 'claw_todo_logs'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    todo_id = db.Column(db.Integer, db.ForeignKey('claw_todos.id'),
                        nullable=False)
    openclaw_id = db.Column(db.Integer, db.ForeignKey('openclaw_instances.id'),
                            nullable=False)
    log_date = db.Column(db.Date, nullable=False, comment='执行日期')
    completed_at = db.Column(db.DateTime, comment='完成时间')
    result_summary = db.Column(db.Text, comment='执行结果摘要')
    status = db.Column(db.String(20), default='completed',
                       comment='submitted(已提交)/approved(已审核)/completed(旧-兼容)/skipped/overdue/retry_failed')
    retry_count = db.Column(db.Integer, default=0,
                            comment='实际重试次数')
    created_at = db.Column(db.DateTime, default=_now)

    openclaw = db.relationship('OpenClawInstance', backref='todo_logs')

    __table_args__ = (
        db.UniqueConstraint('todo_id', 'log_date', name='uq_todo_log_date'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'todo_id': self.todo_id,
            'openclaw_id': self.openclaw_id,
            'log_date': str(self.log_date) if self.log_date else None,
            'completed_at': str(self.completed_at) if self.completed_at else None,
            'result_summary': self.result_summary,
            'status': self.status,
            'retry_count': self.retry_count or 0,
        }


# ============== 课题讨论模型 ==============

# 板块定义
TOPIC_BOARDS = {
    'test_methods': '测试用例和方法',
    'case_sharing': '典型案例分享',
    'risk_assessment': '质量风险评估',
    'client_perf': '客户端性能测试',
    'special_testing': '其他专项测试',
    'industry_news': '业界新闻分享',
    'case_review': '用例评审',
}


class Topic(db.Model):
    """课题讨论帖"""
    __tablename__ = 'topics'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    title = db.Column(db.String(200), nullable=False, comment='帖子标题')
    content = db.Column(db.Text, nullable=False, comment='帖子内容（Markdown）')
    board = db.Column(db.String(30), nullable=False, default='test_methods',
                      comment='板块标识')
    author_claw_id = db.Column(db.Integer, db.ForeignKey('openclaw_instances.id'),
                                nullable=True, comment='发帖 OpenClaw ID')
    author_user_id = db.Column(db.Integer, db.ForeignKey('users.id'),
                                nullable=True, comment='发帖用户 ID')
    author_name = db.Column(db.String(50), nullable=False, comment='发帖人显示名')
    project_name = db.Column(db.String(100), comment='发帖人所在项目')
    status = db.Column(db.String(20), default='open',
                       comment='open=讨论中, closed=已关闭, deleted=已删除')
    visibility = db.Column(db.String(20), default='public',
                           comment='public=公开, project=项目内可见可参与')
    # 用例评审关联字段（仅 board=case_review 时使用）
    review_library_id = db.Column(db.Integer, db.ForeignKey('test_case_libraries.id'),
                                   nullable=True, comment='关联用例库ID')
    review_module_paths = db.Column(db.JSON, nullable=True,
                                     comment='评审的模块路径列表，如["登录模块","支付模块/退款"]')
    review_knowledge_id = db.Column(db.Integer, db.ForeignKey('knowledge_entries.id'),
                                     nullable=True, comment='前置信息知识条目ID')
    reply_count = db.Column(db.Integer, default=0, comment='回复数')
    last_reply_at = db.Column(db.DateTime, comment='最后回复时间')
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    author_claw = db.relationship('OpenClawInstance', backref='topics')
    author_user = db.relationship('User', backref='topics')
    review_library = db.relationship('TestCaseLibrary', backref='review_topics')
    review_knowledge = db.relationship('KnowledgeEntry', backref='review_topics')
    replies = db.relationship('TopicReply', backref='topic',
                              lazy='dynamic', cascade='all, delete-orphan',
                              order_by='TopicReply.created_at')

    def to_dict(self, with_replies=False):
        data = {
            'id': self.id,
            'title': self.title,
            'content': self.content,
            'board': self.board,
            'board_label': TOPIC_BOARDS.get(self.board, self.board),
            'author_claw_id': self.author_claw_id,
            'author_user_id': self.author_user_id,
            'author_name': self.author_name,
            'project_name': self.project_name,
            'status': self.status,
            'visibility': self.visibility,
            'review_library_id': self.review_library_id,
            'review_module_paths': self.review_module_paths,
            'review_knowledge_id': self.review_knowledge_id,
            'reply_count': self.reply_count,
            'last_reply_at': str(self.last_reply_at) if self.last_reply_at else None,
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }
        if with_replies:
            data['replies'] = [r.to_dict() for r in self.replies.filter(
                TopicReply.status != 'deleted')]
        # 用例评审：附加用例库信息和知识条目信息
        if self.board == 'case_review':
            if self.review_library:
                lib = self.review_library
                data['review_library_name'] = lib.name
                data['review_library_project'] = lib.project_name
            if self.review_knowledge:
                data['review_knowledge_title'] = self.review_knowledge.title
        return data


class TopicReply(db.Model):
    """课题讨论回复"""
    __tablename__ = 'topic_replies'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    topic_id = db.Column(db.Integer, db.ForeignKey('topics.id'), nullable=False)
    content = db.Column(db.Text, nullable=False, comment='回复内容（Markdown）')
    author_claw_id = db.Column(db.Integer, db.ForeignKey('openclaw_instances.id'),
                                nullable=True)
    author_user_id = db.Column(db.Integer, db.ForeignKey('users.id'),
                                nullable=True)
    author_name = db.Column(db.String(50), nullable=False)
    reply_to_id = db.Column(db.Integer, db.ForeignKey('topic_replies.id'),
                             nullable=True, comment='回复某条回复的 ID')
    status = db.Column(db.String(20), default='active',
                       comment='active/deleted')
    created_at = db.Column(db.DateTime, default=_now)

    author_claw = db.relationship('OpenClawInstance', backref='topic_replies')
    author_user = db.relationship('User', backref='topic_replies')

    def to_dict(self):
        return {
            'id': self.id,
            'topic_id': self.topic_id,
            'content': self.content,
            'author_claw_id': self.author_claw_id,
            'author_user_id': self.author_user_id,
            'author_name': self.author_name,
            'reply_to_id': self.reply_to_id,
            'status': self.status,
            'created_at': str(self.created_at) if self.created_at else None,
        }


# ============== 测试计划与任务模型 ==============

class TestPlan(db.Model):
    """测试计划排期"""
    __tablename__ = 'test_plans'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(200), nullable=False, comment='计划名称')
    description = db.Column(db.Text, comment='计划描述')

    # 版本类型
    version_type = db.Column(db.Enum('regular', 'resource', 'hotfix'),
                             default='regular',
                             comment='版本类型：regular=常规版本, resource=资源版本, hotfix=紧急补丁')
    version_name = db.Column(db.String(100), comment='版本号/版本名称，如 v3.2.1')

    # 排期时间
    start_date = db.Column(db.Date, nullable=False, comment='开始日期')
    end_date = db.Column(db.Date, nullable=False, comment='结束日期')

    # 项目关联
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'),
                           comment='关联项目ID')

    # TAPD 迭代关联（JSON数组，支持多选）
    tapd_iteration_ids = db.Column(db.JSON, comment='TAPD 迭代 ID 列表，如 ["10001","10002"]')
    tapd_iteration_names = db.Column(db.JSON, comment='TAPD 迭代名称列表（冗余，方便展示）')
    tapd_workspace_id = db.Column(db.String(50), comment='TAPD workspace ID（冗余，方便查询）')

    # 状态
    status = db.Column(db.Enum('draft', 'active', 'completed', 'archived'),
                       default='draft',
                       comment='状态：draft=草稿, active=进行中, completed=已完成, archived=已归档')

    # 进度统计（由任务汇总计算，冗余存储）
    total_tasks = db.Column(db.Integer, default=0, comment='总任务数')
    completed_tasks = db.Column(db.Integer, default=0, comment='已完成任务数')
    total_bugs = db.Column(db.Integer, default=0, comment='关联 Bug 总数')
    resolved_bugs = db.Column(db.Integer, default=0, comment='已解决 Bug 数')

    # 创建者
    created_by = db.Column(db.String(100), default='', comment='创建人')
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    # 关联
    project = db.relationship('Project', backref='test_plans')
    tasks = db.relationship('TestTask', backref='plan',
                            lazy='dynamic', cascade='all, delete-orphan')

    def to_dict(self, with_tasks=False):
        data = {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'version_type': self.version_type,
            'version_name': self.version_name,
            'start_date': str(self.start_date) if self.start_date else None,
            'end_date': str(self.end_date) if self.end_date else None,
            'project_id': self.project_id,
            'project_name': self.project.name if self.project else None,
            'tapd_iteration_ids': self.tapd_iteration_ids or [],
            'tapd_iteration_names': self.tapd_iteration_names or [],
            'tapd_workspace_id': self.tapd_workspace_id,
            'status': self.status,
            'total_tasks': self.total_tasks,
            'completed_tasks': self.completed_tasks,
            'total_bugs': self.total_bugs,
            'resolved_bugs': self.resolved_bugs,
            'progress': round(self.completed_tasks / self.total_tasks * 100, 1) if self.total_tasks > 0 else 0,
            'created_by': self.created_by,
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }
        if with_tasks:
            data['tasks'] = [t.to_dict() for t in self.tasks.order_by(TestTask.created_at)]
        return data


class TestTask(db.Model):
    """测试任务"""
    __tablename__ = 'test_tasks'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    plan_id = db.Column(db.Integer, db.ForeignKey('test_plans.id'),
                        nullable=False, comment='所属测试计划')

    name = db.Column(db.String(200), nullable=False, comment='任务名称')
    description = db.Column(db.Text, comment='任务描述')

    # 任务类型
    task_type = db.Column(db.Enum('functional', 'automation', 'activity', 'performance',
                                  'compatibility', 'security', 'interface', 'other'),
                          default='functional',
                          comment='任务类型：functional=功能测试, automation=自动化测试, '
                                  'activity=活动测试, performance=性能测试, '
                                  'compatibility=兼容性测试, security=安全测试, '
                                  'interface=接口测试, other=其他')

    # 执行人（OpenClaw 实例）
    assignee_claw_id = db.Column(db.Integer, db.ForeignKey('openclaw_instances.id'),
                                  comment='指派的 OpenClaw ID')

    # 排期
    start_date = db.Column(db.Date, comment='开始日期')
    end_date = db.Column(db.Date, comment='截止日期')

    # 优先级
    priority = db.Column(db.Enum('P0', 'P1', 'P2', 'P3'), default='P2', comment='优先级')

    # 关联用例库
    library_id = db.Column(db.Integer, db.ForeignKey('test_case_libraries.id'),
                           comment='关联的用例库ID')

    # 用例筛选条件（API 创建时更灵活）
    case_filter = db.Column(db.JSON, comment='用例筛选条件，如 {"module_paths":["登录模块"], "priorities":["P0"], "case_ids":[1,2,3]}')

    # 进度和结果
    status = db.Column(db.Enum('pending', 'in_progress', 'completed', 'blocked', 'skipped'),
                       default='pending',
                       comment='状态：pending=待开始, in_progress=进行中, '
                               'completed=已完成, blocked=阻塞, skipped=跳过')
    progress = db.Column(db.Integer, default=0, comment='进度百分比 0-100')
    result_summary = db.Column(db.Text, comment='结果摘要')

    # 用例执行统计
    total_cases = db.Column(db.Integer, default=0, comment='用例总数')
    passed_cases = db.Column(db.Integer, default=0, comment='通过数')
    failed_cases = db.Column(db.Integer, default=0, comment='失败数')
    blocked_cases = db.Column(db.Integer, default=0, comment='阻塞数')
    skipped_cases = db.Column(db.Integer, default=0, comment='跳过数')

    # Bug 关联（TAPD）
    tapd_bug_ids = db.Column(db.JSON, comment='关联的 TAPD Bug ID 列表')
    bug_count = db.Column(db.Integer, default=0, comment='关联 Bug 数量')

    # 创建者
    created_by = db.Column(db.String(100), default='', comment='创建人')
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    # 关联
    assignee = db.relationship('OpenClawInstance', backref='test_tasks')
    library = db.relationship('TestCaseLibrary', backref='test_tasks')
    task_cases = db.relationship('TestTaskCase', backref='task',
                                  lazy='dynamic', cascade='all, delete-orphan')

    def to_dict(self, with_cases=False):
        data = {
            'id': self.id,
            'plan_id': self.plan_id,
            'name': self.name,
            'description': self.description,
            'task_type': self.task_type,
            'assignee_claw_id': self.assignee_claw_id,
            'assignee_name': self.assignee.name if self.assignee else None,
            'start_date': str(self.start_date) if self.start_date else None,
            'end_date': str(self.end_date) if self.end_date else None,
            'priority': self.priority,
            'library_id': self.library_id,
            'library_name': self.library.name if self.library else None,
            'case_filter': self.case_filter,
            'status': self.status,
            'progress': self.progress,
            'result_summary': self.result_summary,
            'total_cases': self.total_cases,
            'passed_cases': self.passed_cases,
            'failed_cases': self.failed_cases,
            'blocked_cases': self.blocked_cases,
            'skipped_cases': self.skipped_cases,
            'tapd_bug_ids': self.tapd_bug_ids or [],
            'bug_count': self.bug_count,
            'created_by': self.created_by,
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }
        if with_cases:
            data['task_cases'] = [tc.to_dict() for tc in self.task_cases]
        return data


class TestTaskCase(db.Model):
    """测试任务-用例关联表（记录每个用例在任务中的执行状态）"""
    __tablename__ = 'test_task_cases'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    task_id = db.Column(db.Integer, db.ForeignKey('test_tasks.id'),
                        nullable=False, comment='任务ID')
    case_id = db.Column(db.Integer, db.ForeignKey('test_cases.id'),
                        nullable=False, comment='用例ID')

    # 执行状态
    status = db.Column(db.Enum('pending', 'passed', 'failed', 'blocked', 'skipped'),
                       default='pending',
                       comment='执行状态：pending=待执行, passed=通过, '
                               'failed=失败, blocked=阻塞, skipped=跳过')
    executed_at = db.Column(db.DateTime, comment='执行时间')
    executed_by = db.Column(db.String(100), comment='执行人')
    note = db.Column(db.Text, comment='备注/失败原因')

    # 关联 Bug
    tapd_bug_id = db.Column(db.String(50), comment='关联的 TAPD Bug ID')

    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    case = db.relationship('TestCase', backref='task_executions')

    __table_args__ = (
        db.UniqueConstraint('task_id', 'case_id', name='uq_task_case'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'task_id': self.task_id,
            'case_id': self.case_id,
            'case_title': self.case.title if self.case else None,
            'case_priority': self.case.priority if self.case else None,
            'status': self.status,
            'executed_at': str(self.executed_at) if self.executed_at else None,
            'executed_by': self.executed_by,
            'note': self.note,
            'tapd_bug_id': self.tapd_bug_id,
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }


