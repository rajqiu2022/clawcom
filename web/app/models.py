import secrets
import hashlib
import base64
from datetime import datetime
from app import db

# 简单加密，用于存储可还原的 token 明文
# 注意：实际生产环境建议使用更安全的密钥管理
_ENCODING_KEY = base64.urlsafe_b64encode(b'change-me-in-prod-32bytes-secret')


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
    status = db.Column(db.Enum('online', 'offline', 'busy'),
                       default='offline')
    role = db.Column(db.Enum('admin', 'test_manager', 'test_member', 'test_executor'),
                     default='test_member',
                     comment='角色：admin=管理员，test_manager=测试经理，test_member=测试成员，test_executor=测试执行')
    connection_mode = db.Column(db.Enum('sse', 'polling'),
                                default='sse',
                                comment='连接方式：sse=SSE长连接，polling=轮询')
    last_heartbeat = db.Column(db.DateTime, comment='最后心跳时间')
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
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow)

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
            'last_heartbeat': str(self.last_heartbeat) if self.last_heartbeat else None,
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
                'skills': [s.skill.to_dict() for s in self.skills if s.enabled],
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
    """Skills 库（支持 OpenSpace 自动进化技能）"""
    __tablename__ = 'skills'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(100), nullable=False, unique=True,
                     comment='Skill 标识名')
    display_name = db.Column(db.String(100), nullable=False,
                             comment='显示名称')
    description = db.Column(db.Text, comment='功能描述')
    category = db.Column(db.Enum('standard', 'custom', 'evolved'),
                         default='standard',
                         comment='分类：standard=标准预置，custom=手动创建，evolved=OpenSpace自动进化')
    trigger_phrase = db.Column(db.String(255), comment='触发短语')
    template_content = db.Column(db.Text, comment='Skill 模板内容')
    scope = db.Column(db.Enum('global', 'project', 'module'),
                      default='global', comment='作用域：global=全局，project=按项目，module=按模块')
    applicable_projects = db.Column(db.JSON, comment='适用项目ID列表（scope=project时）')
    applicable_modules = db.Column(db.JSON, comment='适用模块名列表（scope=module时）')
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

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        data = {
            'id': self.id,
            'name': self.name,
            'display_name': self.display_name,
            'description': self.description,
            'category': self.category,
            'trigger_phrase': self.trigger_phrase,
            'template_content': self.template_content,
            'scope': self.scope,
            'applicable_projects': self.applicable_projects or [],
            'applicable_modules': self.applicable_modules or [],
            'used_by_count': self.used_by_count,
            'is_standard': self.is_standard,
            'created_at': str(self.created_at) if self.created_at else None,
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
    installed_at = db.Column(db.DateTime, default=datetime.utcnow)

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
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

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
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow)

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
    distributed_at = db.Column(db.DateTime, default=datetime.utcnow)
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
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

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
    """模块表"""
    __tablename__ = 'modules'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'),
                           nullable=False)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('project_id', 'name', name='uq_project_module'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'project_id': self.project_id,
            'name': self.name,
            'description': self.description,
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
            'last_heartbeat': str(self.last_heartbeat) if self.last_heartbeat else None,
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
            'created_at': str(self.created_at) if self.created_at else None,
            'read_at': str(self.read_at) if self.read_at else None,
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
    category = db.Column(db.Enum('standard', 'custom', 'ai_generated'),
                        default='standard',
                        comment='分类：standard=标准预置，custom=手动创建，ai_generated=AI生成')
    scope = db.Column(db.Enum('global', 'project', 'module'),
                      default='global', comment='作用域：global=全局，project=按项目，module=按模块')
    applicable_projects = db.Column(db.JSON, comment='适用项目ID列表（scope=project时）')
    applicable_modules = db.Column(db.JSON, comment='适用模块名列表（scope=module时）')

    # 生成的规范内容（Markdown格式，会被写入OpenClaw的配置文件）
    content_template = db.Column(db.Text, comment='规范内容模板')
    is_standard = db.Column(db.Boolean, default=False,
                           comment='是否标准化 Rules（注册时自动安装）')

    # 关联到哪些 OpenClaw
    openclaws = db.relationship('OpenClawRule', backref='rule',
                               lazy='dynamic', cascade='all, delete-orphan')

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                          onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'display_name': self.display_name,
            'description': self.description,
            'category': self.category,
            'scope': self.scope,
            'is_standard': self.is_standard,
            'applicable_projects': self.applicable_projects or [],
            'applicable_modules': self.applicable_modules or [],
            'content_template': self.content_template,
            'openclaw_ids': [r.openclaw_id for r in self.openclaws.filter_by(enabled=True)],
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
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

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                          onupdate=datetime.utcnow)

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

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                          onupdate=datetime.utcnow)

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
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }


# ============== OpenClaw 消息模型 ==============

class ClawMessage(db.Model):
    """OpenClaw 消息表（Web -> OpenClaw）"""
    __tablename__ = 'claw_messages'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    claw_id = db.Column(db.Integer, db.ForeignKey('openclaw_instances.id'),
                       nullable=False, comment='接收消息的 OpenClaw ID')
    sender_name = db.Column(db.String(50), default='Web Admin',
                          comment='发送者名称')
    content = db.Column(db.Text, nullable=False, comment='消息内容')
    msg_type = db.Column(db.String(30), default='text',
                        comment='消息类型：text/task_delegate/knowledge_share/request_help')
    status = db.Column(db.String(20), default='pending',
                      comment='状态：pending=待送达，delivered=已送达，read=已读')
    delivered_at = db.Column(db.DateTime, comment='送达时间')
    read_at = db.Column(db.DateTime, comment='读取时间')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # 关联到 OpenClaw
    claw = db.relationship('OpenClawInstance', backref='messages')

    def to_dict(self):
        return {
            'id': self.id,
            'claw_id': self.claw_id,
            'sender_name': self.sender_name,
            'content': self.content,
            'msg_type': self.msg_type,
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
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

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

