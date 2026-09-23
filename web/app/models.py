import secrets
import hashlib
import base64
import json
from datetime import datetime, timezone, timedelta
from app import db
from sqlalchemy.dialects.mysql import LONGTEXT
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
    last_login_at = db.Column(db.DateTime, comment='最后登录时间')

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
            'last_login_at': str(self.last_login_at) if self.last_login_at else None,
        }
        # 添加绑定的 claw 名（供前端权限判断）
        if self.bound_claw_id:
            claw = OpenClawInstance.query.get(self.bound_claw_id)
            if claw:
                data['bound_claw_name'] = claw.name
        return data


# 简单加密，用于存储可还原的 token 明文
# 注意：实际生产环境建议使用更安全的密钥管理
_ENCODING_KEY = base64.urlsafe_b64encode(b'change-me-in-prod-32bytes-secret')
_LEGACY_ENCODING_KEYS = (
    base64.urlsafe_b64encode(b'openaclaw-secret-key-32bytes!'),
)


def _simple_encrypt(text: str) -> str:
    """简单 XOR 加密（可还原）"""
    key = _ENCODING_KEY
    result = []
    for i, c in enumerate(text):
        result.append(chr(ord(c) ^ key[i % len(key)]))
    return base64.urlsafe_b64encode(''.join(result).encode()).decode()


def _looks_like_plain_secret(text: str) -> bool:
    """判断解密结果是否像正常明文，兼容历史 key 迁移。"""
    if not text:
        return False
    if text.startswith(('oc_tk_', 'hub_tk_')):
        return True
    return all((ch in '\r\n\t') or (ord(ch) >= 32) for ch in text)


def _simple_decrypt(encrypted: str) -> str:
    """解密"""
    try:
        data = base64.urlsafe_b64decode(encrypted.encode()).decode()
    except Exception:
        return ''

    fallback = ''
    for key in (_ENCODING_KEY, *_LEGACY_ENCODING_KEYS):
        try:
            result = []
            for i, c in enumerate(data):
                result.append(chr(ord(c) ^ key[i % len(key)]))
            plain = ''.join(result)
        except Exception:
            continue
        if _looks_like_plain_secret(plain):
            return plain
        if not fallback:
            fallback = plain
    return fallback


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
    safe_name = db.Column(db.String(50), nullable=False, default='',
                         comment='目录名安全版本（第一次创建时生成，后续不变）')
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
    last_modified_by = db.Column(db.String(100),
                                 comment='最近一次修改人（用户名/OpenClaw 名）')
    owner_wecom_userid = db.Column(db.String(64),
                                   comment='owner 的企微 userid/RTX 名（如 rajqiu），Hub 集中代发企微消息时使用')
    wecom_bot_id = db.Column(db.String(128), default='',
                             comment='Hermes Agent 企微机器人 Bot ID / key')
    wecom_bot_secret = db.Column(db.String(255), default='',
                                 comment='Hermes Agent 企微机器人 Secret（加密存储）')
    safe_name = db.Column(db.String(50), default='',
                          comment='部署目录名（首次部署时生成，后续不变）')
    llm_provider = db.Column(db.String(50), default='venus',
                          comment='Hermes 大模型平台：venus / timiai')
    llm_model = db.Column(db.String(100), default='venus',
                          comment='Hermes 大模型选择（随 llm_provider 变化）')
    work_dirs = db.Column(db.JSON, comment='Hermes Agent 额外可写工作目录列表')
    description = db.Column(db.Text, comment='描述')
    timiai_project = db.Column(db.String(50), default='gbt',
                               comment='TimiAI 项目标识：gbt / qqspeed_pc / contra')

    project = db.relationship('Project', backref='openclaws')

    # 关联
    skills = db.relationship('OpenClawSkill', backref='openclaw',
                             lazy='dynamic', cascade='all, delete-orphan')
    reports = db.relationship('DailyReport', backref='openclaw',
                              lazy='dynamic', cascade='all, delete-orphan')

    def to_dict(self, brief=False):
        _owner_user = User.query.filter_by(username=self.owner).first() if self.owner else None
        data = {
            'id': self.id,
            'name': self.name,
            'safe_name': self.safe_name or '',
            'claw_tag': self.claw_tag,
            'owner': self.owner,
            'owner_display_name': (_owner_user.display_name if _owner_user and _owner_user.display_name
                                   else self.owner),
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
            'updated_at': str(self.updated_at) if self.updated_at else None,
            'last_modified_by': self.last_modified_by or None,
            'api_token_preview': self.get_token_preview() if self.api_token_plain else None,
            'llm_provider': self.llm_provider or 'venus',
            'llm_model': self.llm_model or 'venus',
            'timiai_project': self.timiai_project or 'gbt',
            'work_dirs': self.work_dirs or [],
            'wecom_bot_id': self.wecom_bot_id or '',
            'has_wecom_bot_secret': bool(self.wecom_bot_secret),
        }
        if not brief:
            data.update({
                'soul_config': self.soul_config,
                'workflow_config': self.workflow_config,
                'web_system_url': self.web_system_url,
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

    def get_wecom_bot_secret_plain(self):
        """获取企微 Bot Secret 明文（仅下发给本 claw token 认证的 agent）。"""
        if self.wecom_bot_secret:
            return _simple_decrypt(self.wecom_bot_secret)
        return ''


class AgentProfile(db.Model):
    """Agent 岗位说明书：Hub 可下发、可版本化、可考核的行为配置。"""
    __tablename__ = 'agent_profiles'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    profile_key = db.Column(db.String(80), nullable=False, unique=True,
                            comment='稳定键，如 requirement_reviewer')
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text)
    system_prompt = db.Column(db.Text, comment='岗位 system prompt / 人格说明')
    workflow_config = db.Column(db.Text, comment='岗位工作规范')
    required_skills_json = db.Column(db.JSON, comment='必装 skill key/path 列表')
    contract_json = db.Column(db.JSON, comment='岗位产出契约声明')
    exam_paper_id = db.Column(db.Integer, comment='准入考试试卷 ID，可空')
    version = db.Column(db.Integer, default=1, nullable=False)
    status = db.Column(db.String(20), default='active', index=True,
                       comment='active/disabled')
    created_by = db.Column(db.String(100), default='')
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    def to_dict(self):
        data = {
            'id': self.id,
            'profile_key': self.profile_key,
            'name': self.name,
            'description': self.description or '',
            'system_prompt': self.system_prompt or '',
            'workflow_config': self.workflow_config or '',
            'required_skills': self.required_skills_json or [],
            'contract': self.contract_json or {},
            'exam_paper_id': self.exam_paper_id,
            'version': self.version or 1,
            'status': self.status or 'active',
            'created_by': self.created_by or '',
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }
        return data


class AgentPost(db.Model):
    """Agent 团队工位：需求分析岗、工程分析岗、用例设计岗、独立评审岗等。"""
    __tablename__ = 'agent_posts'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    post_key = db.Column(db.String(80), nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), index=True)
    profile_id = db.Column(db.Integer, db.ForeignKey('agent_profiles.id'),
                           nullable=False, index=True)
    required_profile_version = db.Column(db.Integer, default=1, nullable=False)
    status = db.Column(db.String(20), default='active', index=True,
                       comment='active/disabled')
    created_by = db.Column(db.String(100), default='')
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    project = db.relationship('Project', backref='agent_posts')
    profile = db.relationship('AgentProfile', backref='posts')

    __table_args__ = (
        db.UniqueConstraint('project_id', 'post_key',
                            name='uq_agent_post_project_key'),
    )

    def to_dict(self):
        data = {
            'id': self.id,
            'post_key': self.post_key,
            'name': self.name,
            'description': self.description or '',
            'project_id': self.project_id,
            'profile_id': self.profile_id,
            'profile': self.profile.to_dict() if self.profile else None,
            'required_profile_version': self.required_profile_version or 1,
            'status': self.status or 'active',
            'created_by': self.created_by or '',
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }
        return data


class AgentPostAssignment(db.Model):
    """Agent 工位值守关系。"""
    __tablename__ = 'agent_post_assignments'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    post_id = db.Column(db.Integer, db.ForeignKey('agent_posts.id'),
                        nullable=False, index=True)
    claw_id = db.Column(db.Integer, db.ForeignKey('openclaw_instances.id'),
                        nullable=False, index=True)
    profile_version = db.Column(db.Integer, default=1, nullable=False,
                                comment='该 agent 接受并通过的 profile 版本')
    exam_session_id = db.Column(db.Integer, comment='最近一次准入考试 session，可空')
    is_primary = db.Column(db.Boolean, default=False)
    status = db.Column(db.String(20), default='active', index=True,
                       comment='active/paused/disabled')
    assigned_by = db.Column(db.String(100), default='')
    assigned_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    post = db.relationship('AgentPost', backref=db.backref(
        'assignments', lazy='dynamic', cascade='all, delete-orphan'))
    claw = db.relationship('OpenClawInstance', backref=db.backref(
        'post_assignments', lazy='dynamic', cascade='all, delete-orphan'))

    __table_args__ = (
        db.UniqueConstraint('post_id', 'claw_id',
                            name='uq_agent_post_assignment'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'post_id': self.post_id,
            'claw_id': self.claw_id,
            'claw_name': self.claw.name if self.claw else '',
            'profile_version': self.profile_version or 1,
            'exam_session_id': self.exam_session_id,
            'is_primary': bool(self.is_primary),
            'status': self.status or 'active',
            'assigned_by': self.assigned_by or '',
            'assigned_at': str(self.assigned_at) if self.assigned_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }


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

    # 镜像内容：非管理员修改时写入镜像，审核通过后替换原内容
    mirror_content = db.Column(db.Text, comment='镜像内容（待审核的修改内容）')
    mirror_updated_by = db.Column(db.String(100), comment='镜像内容修改人')
    mirror_updated_at = db.Column(db.DateTime, comment='镜像内容修改时间')
    # 修改历史：最近10条修改记录
    content_history = db.Column(db.JSON, comment='最近10次修改记录 [{content, updated_by, updated_at, summary}]')

    # 最后修改人（不论镜像/直改/审核通过都会写入）
    last_modified_by = db.Column(db.String(100), comment='最后修改人（user.username 或 OpenClaw 名）')
    last_modified_at = db.Column(db.DateTime, comment='最后修改时间')
    last_modified_source = db.Column(db.Enum('web', 'openclaw', 'system'),
                                     default='web',
                                     comment='修改来源：web=人在网页改，openclaw=OpenClaw API 改，system=系统/迁移')

    is_deleted = db.Column(db.Boolean, default=False,
                           comment='软删除标记：True=已删除（隐藏），False=正常')
    deleted_at = db.Column(db.DateTime, comment='软删除时间')
    rating = db.Column(db.Float, default=3.0,
                       comment='星级评分（1~5，支持0.5步进，默认3）')

    # 私有 Skill：仅创建者自己可见可用，免审核
    visibility = db.Column(db.String(20), default='public',
                           comment='可见性：public=公开（默认），private=私有（仅作者可见）')
    owner_claw_id = db.Column(db.Integer, comment='私有 Skill 归属的 OpenClaw ID')

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
            'mirror_content': self.mirror_content or None,
            'mirror_updated_by': self.mirror_updated_by or None,
            'mirror_updated_at': str(self.mirror_updated_at) if self.mirror_updated_at else None,
            'content_history': self.content_history or [],
            'last_modified_by': self.last_modified_by or None,
            'last_modified_at': str(self.last_modified_at) if self.last_modified_at else None,
            'last_modified_source': self.last_modified_source or 'web',
            'visibility': self.visibility or 'public',
            'owner_claw_id': self.owner_claw_id,
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


class SkillUsageEvent(db.Model):
    """一次成功的 Skill 完整内容 API 读取事件。"""
    __tablename__ = 'skill_usage_events'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    skill_id = db.Column(db.Integer, db.ForeignKey('skills.id', ondelete='CASCADE'),
                         nullable=False)
    access_type = db.Column(db.String(32), nullable=False,
                            comment='完整内容读取入口，如 detail/list_full/raw/file/pack')
    accessed_at = db.Column(db.DateTime, nullable=False, default=_now, index=True)

    __table_args__ = (
        db.Index('ix_skill_usage_recent', 'skill_id', 'accessed_at'),
    )


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
    # Historical values predate receipt verification; never treat them as proof.
    installed_at = db.Column(db.DateTime)
    assigned_at = db.Column(db.DateTime, default=_now)
    installation_generation = db.Column(db.String(32), default=lambda: secrets.token_hex(16))
    installation_status = db.Column(db.String(24), default='pending')
    installation_verified_at = db.Column(db.DateTime)
    installation_receipt_json = db.Column(db.JSON)
    installation_todo_id = db.Column(db.Integer)

    skill = db.relationship('Skill', backref='installations')

    __table_args__ = (
        db.UniqueConstraint('openclaw_id', 'skill_id',
                            name='uq_openclaw_skill'),
    )


class SkillInstallationReceipt(db.Model):
    """Immutable, generation-fenced Worker acknowledgements (not chat replies)."""
    __tablename__ = 'skill_installation_receipts'
    id = db.Column(db.Integer, primary_key=True)
    assignment_id = db.Column(db.Integer, db.ForeignKey('openclaw_skills.id'), nullable=False)
    generation = db.Column(db.String(32), nullable=False)
    event_id = db.Column(db.String(96), nullable=False)
    request_sha256 = db.Column(db.String(64), nullable=False)
    response_json = db.Column(db.JSON, nullable=False)
    created_at = db.Column(db.DateTime, default=_now, nullable=False)
    __table_args__ = (db.UniqueConstraint('assignment_id', 'generation', 'event_id',
                                        name='uq_skill_install_receipt_event'),)


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


class KnowledgeNotebook(db.Model):
    """项目版本测试纪要本。"""
    __tablename__ = 'knowledge_notebooks'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'),
                           nullable=False, index=True)
    title = db.Column(db.String(255), nullable=False)
    version_name = db.Column(db.String(120), default='')
    iteration_id = db.Column(db.Integer, db.ForeignKey('test_iterations.id'),
                             nullable=True, index=True)
    modules_json = db.Column(db.JSON)
    status = db.Column(db.String(20), nullable=False, default='active', index=True)
    version = db.Column(db.Integer, nullable=False, default=1)
    created_by_type = db.Column(db.String(20), nullable=False, default='user')
    created_by_id = db.Column(db.Integer)
    created_by_name = db.Column(db.String(100), default='')
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    project = db.relationship('Project', backref='knowledge_notebooks')
    iteration = db.relationship('TestIteration', backref='knowledge_notebooks')

    def to_dict(self, with_pages=False):
        data = {
            'id': self.id,
            'project_id': self.project_id,
            'project_name': self.project.name if self.project else None,
            'title': self.title,
            'version_name': self.version_name or '',
            'iteration_id': self.iteration_id,
            'modules': self.modules_json or [],
            'status': self.status,
            'version': int(self.version or 1),
            'created_by_type': self.created_by_type,
            'created_by_id': self.created_by_id,
            'created_by_name': self.created_by_name or '',
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }
        if with_pages:
            pages = sorted(self.pages, key=lambda row: (
                str(row.module_name or ''), str(row.title or ''), row.id))
            data['pages'] = [page.to_dict() for page in pages]
        return data


class KnowledgeEntry(db.Model):
    """知识条目"""
    __tablename__ = 'knowledge_entries'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    memos_id = db.Column(db.String(100), comment='Memos 中的 memo name')
    title = db.Column(db.String(255), nullable=False)
    # Knowledge articles and the latest journal snapshot can exceed MySQL
    # TEXT's 65,535-byte ceiling.  Keep SQLite on TEXT for local/tests while
    # using LONGTEXT in MySQL, matching revision history storage.
    content = db.Column(
        db.Text().with_variant(LONGTEXT(), 'mysql'), nullable=False)
    category = db.Column(db.String(50), nullable=False,
                         comment='标签分类')
    scope = db.Column(db.String(50), nullable=False, default='global')
    project_name = db.Column(db.String(100), default=None)
    module_name = db.Column(db.String(100), default=None)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), index=True)
    entry_type = db.Column(db.String(30), nullable=False, default='article', index=True)
    notebook_id = db.Column(db.Integer, db.ForeignKey('knowledge_notebooks.id'),
                            nullable=True, index=True)
    current_revision = db.Column(db.Integer, nullable=False, default=0)
    lock_version = db.Column(db.Integer, nullable=False, default=0)
    archived_at = db.Column(db.DateTime)
    source_openclaw_id = db.Column(
        db.Integer, db.ForeignKey('openclaw_instances.id'))
    source_type = db.Column(
        db.String(50),
        default='manual',
        comment='来源类型：openclaw=Agent记录，openspace=自动进化，manual=手动录入')
    status = db.Column(db.String(50), default='draft')
    reviewer_notes = db.Column(db.Text, comment='审核意见')
    approved_at = db.Column(db.DateTime)
    approved_by = db.Column(db.String(50))
    created_by = db.Column(db.String(100), default='system', comment='创建人（用户名或claw_name）')
    is_shared = db.Column(db.Boolean, default=False, nullable=False)
    share_token = db.Column(db.String(64), unique=True, index=True)
    shared_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now,
                           onupdate=_now)

    source_openclaw = db.relationship('OpenClawInstance',
                                      backref='knowledge_entries')
    project = db.relationship('Project', backref='knowledge_entries')
    notebook = db.relationship('KnowledgeNotebook', backref=db.backref(
        'pages', cascade='all, delete-orphan'))

    def enable_share(self, refresh=False):
        """Enable anonymous sharing and return the active token."""
        import secrets
        if refresh or not self.share_token:
            self.share_token = secrets.token_urlsafe(16)
        self.is_shared = True
        self.shared_at = _now()
        return self.share_token

    def revoke_share(self):
        """Revoke anonymous sharing and invalidate the previous URL."""
        self.is_shared = False
        self.share_token = None
        self.shared_at = None

    def to_dict(self):
        data = {
            'id': self.id,
            'memos_id': self.memos_id,
            'title': self.title,
            'content': self.content,
            'category': self.category,
            # 项目基本信息「特别标识」：category=project_profile 的条目是每个项目
            # 接入 Hub 的必备身份卡；前端据此加标记、拉列表时可单独返回（见 knowledge.list_knowledge）。
            'is_project_profile': self.category == 'project_profile',
            'scope': self.scope,
            'project_name': self.project_name,
            'module_name': self.module_name,
            'project_id': self.project_id,
            'entry_type': self.entry_type or 'article',
            'notebook_id': self.notebook_id,
            'current_revision': int(self.current_revision or 0),
            'lock_version': int(self.lock_version or 0),
            'archived_at': str(self.archived_at) if self.archived_at else None,
            'journal_status': 'archived' if self.archived_at else 'normal',
            'source_openclaw_id': self.source_openclaw_id,
            'source_openclaw_name': (self.source_openclaw.name
                                     if self.source_openclaw else None),
            'source_type': self.source_type,
            'status': self.status,
            'reviewer_notes': self.reviewer_notes,
            'approved_at': (str(self.approved_at)
                           if self.approved_at else None),
            'approved_by': self.approved_by,
            'is_shared': bool(self.is_shared),
            'shared_at': str(self.shared_at) if self.shared_at else None,
            # created_by 兜底链（MEMORY #131）：
            #   self.created_by（如果是真名）
            #   → source_openclaw.name（旧代码 default='system' 时退一层）
            #   → 'system'
            # 注：model 定义 `default='system'`，所以新插入的 NULL 实际会变 'system'；
            # 因此判断"需要兜底"的条件不是 falsy，而是显式 in ('system','')。
            'created_by': (self.created_by
                           if self.created_by and self.created_by not in ('system',)
                           else ((self.source_openclaw.name
                                  if self.source_openclaw else None)
                                 or self.created_by  # 退回 'system' 也行
                                 or 'system')),
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }
        if (self.entry_type or 'article') == 'test_journal':
            latest = max(self.revisions, key=lambda row: row.revision_no,
                         default=None)
            data['journal_author'] = self.created_by or ''
            data['last_editor_name'] = (
                latest.editor_name if latest else (self.created_by or ''))
        return data


class KnowledgeEntryRevision(db.Model):
    """知识页面不可变版本；回退也只会追加新版本。"""
    __tablename__ = 'knowledge_entry_revisions'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    knowledge_id = db.Column(db.Integer, db.ForeignKey('knowledge_entries.id'),
                             nullable=False, index=True)
    revision_no = db.Column(db.Integer, nullable=False)
    title = db.Column(db.String(255), nullable=False)
    content_markdown = db.Column(db.Text, nullable=False)
    content_sha256 = db.Column(db.String(64), nullable=False)
    change_summary = db.Column(db.String(500), default='')
    based_on_revision = db.Column(db.Integer)
    rollback_from_revision = db.Column(db.Integer)
    editor_type = db.Column(db.String(20), nullable=False)
    editor_id = db.Column(db.Integer)
    editor_name = db.Column(db.String(100), default='')
    idempotency_key = db.Column(db.String(128), nullable=False)
    request_sha256 = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, default=_now, index=True)

    knowledge = db.relationship('KnowledgeEntry', backref=db.backref(
        'revisions', cascade='all, delete-orphan', order_by='KnowledgeEntryRevision.revision_no'))

    __table_args__ = (
        db.UniqueConstraint('knowledge_id', 'revision_no',
                            name='uq_knowledge_revision_no'),
        db.UniqueConstraint('editor_type', 'editor_id', 'idempotency_key',
                            name='uq_knowledge_revision_idem'),
    )

    def to_dict(self, with_content=False):
        data = {
            'id': self.id,
            'knowledge_id': self.knowledge_id,
            'revision_no': self.revision_no,
            'title': self.title,
            'content_sha256': self.content_sha256,
            'change_summary': self.change_summary or '',
            'based_on_revision': self.based_on_revision,
            'rollback_from_revision': self.rollback_from_revision,
            'editor_type': self.editor_type,
            'editor_id': self.editor_id,
            'editor_name': self.editor_name or '',
            'created_at': str(self.created_at) if self.created_at else None,
        }
        if with_content:
            data['content'] = self.content_markdown
        return data


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


class KnowledgeFavorite(db.Model):
    """知识收藏记录"""
    __tablename__ = 'knowledge_favorites'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    knowledge_id = db.Column(db.Integer,
                             db.ForeignKey('knowledge_entries.id'),
                             nullable=False,
                             index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), index=True)
    claw_id = db.Column(db.Integer,
                        db.ForeignKey('openclaw_instances.id'),
                        index=True)
    created_at = db.Column(db.DateTime, default=_now)

    knowledge = db.relationship('KnowledgeEntry', backref='favorites')
    user = db.relationship('User', backref='knowledge_favorites')
    claw = db.relationship('OpenClawInstance', backref='knowledge_favorites')

    __table_args__ = (
        db.UniqueConstraint('knowledge_id', 'user_id',
                            name='uq_knowledge_fav_user'),
        db.UniqueConstraint('knowledge_id', 'claw_id',
                            name='uq_knowledge_fav_claw'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'knowledge_id': self.knowledge_id,
            'user_id': self.user_id,
            'claw_id': self.claw_id,
            'created_at': str(self.created_at) if self.created_at else None,
        }


class TestIterationTab(db.Model):
    """测试迭代自定义页签"""
    __tablename__ = 'test_iteration_tabs'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    iteration_id = db.Column(db.Integer, db.ForeignKey('test_iterations.id'),
                             nullable=False, index=True)
    tab_key = db.Column(db.String(80), nullable=False)
    title = db.Column(db.String(120), nullable=False)
    tab_type = db.Column(db.String(20), default='custom')
    view_mode = db.Column(db.String(20), default='table')
    columns_json = db.Column(db.JSON)
    rows_json = db.Column(db.JSON)
    charts_json = db.Column(db.JSON)
    created_by = db.Column(db.String(100), default='')
    updated_by = db.Column(db.String(100), default='')
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    iteration = db.relationship('TestIteration', backref='tabs')

    def to_dict(self):
        return {
            'id': self.id,
            'iteration_id': self.iteration_id,
            'tab_key': self.tab_key,
            'title': self.title,
            'tab_type': self.tab_type,
            'view_mode': self.view_mode,
            'columns': self.columns_json or [],
            'rows': self.rows_json or [],
            'charts': self.charts_json or [],
            'created_by': self.created_by,
            'updated_by': self.updated_by,
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }


class TestReportFavorite(db.Model):
    """测试报告收藏记录"""
    __tablename__ = 'test_report_favorites'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    report_id = db.Column(db.Integer, db.ForeignKey('test_reports.id'),
                          nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), index=True)
    claw_id = db.Column(db.Integer,
                        db.ForeignKey('openclaw_instances.id'),
                        index=True)
    created_at = db.Column(db.DateTime, default=_now)

    report = db.relationship('TestReport', backref='favorites')
    user = db.relationship('User', backref='test_report_favorites')
    claw = db.relationship('OpenClawInstance', backref='test_report_favorites')

    def to_dict(self):
        return {
            'id': self.id,
            'report_id': self.report_id,
            'user_id': self.user_id,
            'claw_id': self.claw_id,
            'created_at': str(self.created_at) if self.created_at else None,
        }


class AgentTask(db.Model):
    """Hub 派发给 Agent sidecar 的任务"""
    __tablename__ = 'agent_tasks'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    task_id = db.Column(db.String(100), nullable=False, unique=True)
    claw_id = db.Column(db.Integer, db.ForeignKey('openclaw_instances.id'),
                        nullable=False, index=True)
    task_type = db.Column(db.String(50), nullable=False)
    command = db.Column(db.Text)
    target_path = db.Column(db.Text)
    payload = db.Column(db.Text().with_variant(LONGTEXT(), 'mysql'))
    task_data = db.Column(db.Text)
    status = db.Column(db.String(20), default='pending', index=True)
    result = db.Column(db.Text)
    error = db.Column(db.Text)
    attempt_no = db.Column(db.Integer, nullable=False, default=0)
    claim_token = db.Column(db.String(64))
    fencing_token = db.Column(db.Integer, nullable=False, default=0)
    lease_expires_at = db.Column(db.DateTime, index=True)
    last_heartbeat_at = db.Column(db.DateTime)
    version = db.Column(db.Integer, nullable=False, default=0)
    retry_count = db.Column(db.Integer, nullable=False, default=0)
    retry_max = db.Column(db.Integer, nullable=False, default=0)
    terminal_reason = db.Column(db.String(128), default='')
    progress_digest = db.Column(db.String(64), default='')
    progress_json = db.Column(db.Text)
    progress_at = db.Column(db.DateTime)
    context_snapshot_id = db.Column(db.String(64), index=True)
    context_snapshot_sha256 = db.Column(db.String(64), default='')
    created_at = db.Column(db.DateTime, default=_now)
    assigned_at = db.Column(db.DateTime)
    completed_at = db.Column(db.DateTime)

    claw = db.relationship('OpenClawInstance', backref='tasks')

    def to_dict(self, with_context=False, include_claim=False):
        import json
        try:
            payload = json.loads(self.payload) if self.payload else None
        except Exception:
            payload = self.payload
        now = _now()
        task_age = (
            max(0, int((now - self.created_at).total_seconds()))
            if self.created_at else None)
        heartbeat_age = (
            max(0, int((now - self.last_heartbeat_at).total_seconds()))
            if self.last_heartbeat_at else None)
        data = {
            'id': self.id,
            'task_id': self.task_id,
            'claw_id': self.claw_id,
            'task_type': self.task_type,
            'command': self.command,
            'target_path': self.target_path,
            'payload': payload,
            'task_data': self.task_data,
            'status': self.status,
            'result': self.result,
            'error': self.error,
            'attempt_no': int(self.attempt_no or 0),
            'fencing_token': int(self.fencing_token or 0),
            'lease_expires_at': (
                str(self.lease_expires_at) if self.lease_expires_at else None),
            'last_heartbeat_at': (
                str(self.last_heartbeat_at) if self.last_heartbeat_at else None),
            'version': int(self.version or 0),
            'retry_count': int(self.retry_count or 0),
            'retry_max': int(self.retry_max or 0),
            'terminal_reason': self.terminal_reason or '',
            'progress': self._progress_payload(),
            'progress_at': str(self.progress_at) if self.progress_at else None,
            'context_snapshot_id': self.context_snapshot_id,
            'context_snapshot_sha256': self.context_snapshot_sha256 or '',
            'task_age_seconds': task_age,
            'heartbeat_age_seconds': heartbeat_age,
            'lease_expired': bool(
                self.lease_expires_at and self.lease_expires_at <= now),
            'created_at': str(self.created_at) if self.created_at else None,
            'assigned_at': str(self.assigned_at) if self.assigned_at else None,
            'completed_at': str(self.completed_at) if self.completed_at else None,
        }
        if include_claim and self.claim_token:
            data['claim_token'] = self.claim_token
        if with_context:
            data['task_context'] = self._build_task_context(payload)
        return data

    def _progress_payload(self):
        if not self.progress_json:
            return None
        try:
            return json.loads(self.progress_json)
        except Exception:
            return None

    def _build_task_context(self, payload):
        try:
            from app.services.task_context import build_task_context_payload
            p = payload if isinstance(payload, dict) else {}
            title = (p.get('step_name') or p.get('title') or p.get('name')
                     or self.command or self.task_type or '')
            description = p.get('prompt') or p.get('description') or ''
            claw = getattr(self, 'claw', None)
            project = p.get('project') or getattr(claw, 'project_name', None)
            return build_task_context_payload(
                title, description, project=project, claw=claw)
        except Exception:
            return {}


class ClawOpsVerification(db.Model):
    """执行后校验记录：Agent 写操作后调 /ops/verify，Hub 从 DB 重读比对。

    设计文档：docs/superpowers/specs/2026-07-09-agent-memory-routing-design.md
    """
    __tablename__ = 'claw_ops_verifications'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    claw_id = db.Column(db.Integer, db.ForeignKey('openclaw_instances.id'),
                        index=True)
    token = db.Column(db.String(100), index=True)
    resource_type = db.Column(db.String(50), nullable=False)
    resource_id = db.Column(db.Integer, nullable=False)
    expected = db.Column(db.Text)
    actual = db.Column(db.Text)
    mismatches = db.Column(db.Text)
    verified = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=_now)
    verified_at = db.Column(db.DateTime)

    def to_dict(self):
        import json
        def _load(v):
            try:
                return json.loads(v) if v else None
            except Exception:
                return v
        return {
            'id': self.id,
            'claw_id': self.claw_id,
            'token': self.token,
            'resource_type': self.resource_type,
            'resource_id': self.resource_id,
            'expected': _load(self.expected),
            'actual': _load(self.actual),
            'mismatches': _load(self.mismatches),
            'verified': bool(self.verified),
            'created_at': str(self.created_at) if self.created_at else None,
            'verified_at': str(self.verified_at) if self.verified_at else None,
        }


class WorkflowOperationIdempotency(db.Model):
    """Persisted idempotency records for Workflow Worker write APIs."""
    __tablename__ = 'workflow_operation_idempotencies'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    actor_type = db.Column(db.String(20), nullable=False, default='claw')
    actor_id = db.Column(db.Integer, nullable=False)
    idempotency_key = db.Column(db.String(128), nullable=False)
    method = db.Column(db.String(10), nullable=False)
    path = db.Column(db.String(300), nullable=False)
    request_hash = db.Column(db.String(64), nullable=False)
    response_status = db.Column(db.Integer)
    response_body_json = db.Column(db.JSON)
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)
    expires_at = db.Column(db.DateTime)

    __table_args__ = (
        db.UniqueConstraint(
            'actor_type', 'actor_id', 'idempotency_key',
            name='uq_workflow_operation_idempotency_actor_key'),
    )


def _workflow_step_display_state_for_model(status):
    if status in ('running', 'retrying', 'waiting_approval'):
        return 'running'
    if status in ('blocked', 'failed'):
        return 'blocked'
    if status in ('passed', 'skipped', 'succeeded'):
        return 'done'
    return 'todo'


class WorkflowDefinition(db.Model):
    """Workflow 模板定义"""
    __tablename__ = 'workflow_definitions'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    workflow_key = db.Column(db.String(100), nullable=False, unique=True)
    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), index=True)
    definition_json = db.Column(db.JSON, nullable=False)
    status = db.Column(db.String(20), default='active', index=True)
    version = db.Column(db.Integer, default=1)
    created_by = db.Column(db.String(100), default='')
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)
    owner_type = db.Column(db.String(20), default='', index=True)
    owner_id = db.Column(db.Integer)
    executor_acl_json = db.Column(db.JSON)
    editor_acl_json = db.Column(db.JSON)
    visibility_scope = db.Column(db.String(20), default='project')

    project = db.relationship('Project', backref='workflow_definitions')

    def to_dict(self, brief=False):
        data = {
            'id': self.id,
            'workflow_key': self.workflow_key,
            'key': self.workflow_key,
            'name': self.name,
            'workflow_name': self.name,
            'description': self.description,
            'project_id': self.project_id,
            'project_name': self.project.name if self.project else None,
            'status': self.status,
            'version': self.version,
            'created_by': self.created_by,
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
            'owner_type': self.owner_type,
            'owner_id': self.owner_id,
            'executor_acl': self.executor_acl_json or {},
            'executor_acl_json': self.executor_acl_json or {},
            'editor_acl': self.editor_acl_json or {},
            'editor_acl_json': self.editor_acl_json or {},
            'visibility_scope': self.visibility_scope,
        }
        if not brief:
            data['definition'] = self.definition_json or {}
            data['definition_json'] = self.definition_json or {}
        return data


class WorkflowDefinitionFavorite(db.Model):
    """Workflow 模板收藏"""
    __tablename__ = 'workflow_definition_favorites'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    definition_id = db.Column(db.Integer,
                              db.ForeignKey('workflow_definitions.id'),
                              nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), index=True)
    claw_id = db.Column(db.Integer,
                        db.ForeignKey('openclaw_instances.id'),
                        index=True)
    created_at = db.Column(db.DateTime, default=_now)

    definition = db.relationship('WorkflowDefinition', backref='favorites')
    user = db.relationship('User', backref='workflow_definition_favorites')
    claw = db.relationship('OpenClawInstance',
                           backref='workflow_definition_favorites')


class WorkflowRun(db.Model):
    """Workflow 单次运行"""
    __tablename__ = 'workflow_runs'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    definition_id = db.Column(db.Integer,
                              db.ForeignKey('workflow_definitions.id'),
                              nullable=False, index=True)
    run_name = db.Column(db.String(200), default='')
    status = db.Column(db.String(30), default='pending', index=True)
    current_step_id = db.Column(db.String(100), default='')
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), index=True)
    goal_id = db.Column(
        db.Integer, db.ForeignKey('agent_goals.id'), nullable=True, index=True)
    triggered_by = db.Column(db.String(100), default='')
    # Optional Codex/controller metadata. Nullable fields keep all legacy Run
    # creation paths byte-for-byte compatible at the database boundary.
    idempotency_key = db.Column(db.String(128))
    idempotency_request_hash = db.Column(db.String(64))
    controller_run_id = db.Column(db.String(160), index=True)
    correlation_id = db.Column(db.String(160), index=True)
    trigger_source = db.Column(db.String(64), index=True)
    context_json = db.Column(db.JSON)
    context_snapshot_id = db.Column(db.String(64), index=True)
    context_snapshot_sha256 = db.Column(db.String(64), default='')
    summary = db.Column(db.Text)
    blocker_json = db.Column(db.JSON)
    business_conclusion = db.Column(db.String(64), default='')
    automation_conclusion = db.Column(db.String(64), default='')
    evidence_ingest_status = db.Column(db.String(64), default='')
    outcomes_json = db.Column(db.JSON)
    started_at = db.Column(db.DateTime)
    finished_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=_now, index=True)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    definition = db.relationship('WorkflowDefinition', backref='runs')
    project = db.relationship('Project', backref='workflow_runs')

    __table_args__ = (
        db.UniqueConstraint(
            'definition_id', 'idempotency_key',
            name='uq_workflow_run_definition_idempotency'),
        db.Index(
            'ix_workflow_runs_controller_created',
            'controller_run_id', 'created_at'),
        db.Index(
            'ix_workflow_runs_correlation_created',
            'correlation_id', 'created_at'),
    )

    def to_dict(self, with_steps=True):
        data = {
            'id': self.id,
            'definition_id': self.definition_id,
            'workflow_definition_id': self.definition_id,
            'run_name': self.run_name,
            'name': self.run_name,
            'status': self.status,
            'current_step_id': self.current_step_id,
            'project_id': self.project_id,
            'goal_id': self.goal_id,
            'triggered_by': self.triggered_by,
            'idempotency_key': self.idempotency_key,
            'controller_run_id': self.controller_run_id,
            'correlation_id': self.correlation_id,
            'trigger_source': self.trigger_source,
            'context': self.context_json or {},
            'context_json': self.context_json or {},
            'context_snapshot_id': self.context_snapshot_id,
            'context_snapshot_sha256': self.context_snapshot_sha256 or '',
            'summary': self.summary,
            'blocker': self.blocker_json or {},
            'blocker_json': self.blocker_json or {},
            'workflow_status': self.status,
            'business_conclusion': self.business_conclusion or '',
            'automation_conclusion': self.automation_conclusion or '',
            'evidence_ingest_status': self.evidence_ingest_status or '',
            'outcomes': self.outcomes_json or {},
            'recover_api': f'/api/v1/workflow-runs/{self.id}/recover',
            'restart_api': f'/api/v1/workflow-runs/{self.id}/restart',
            'restart_count': int(
                (self.context_json or {}).get('restart_count') or 0)
                if isinstance(self.context_json, dict) else 0,
            'started_at': str(self.started_at) if self.started_at else None,
            'finished_at': str(self.finished_at) if self.finished_at else None,
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }
        if self.definition:
            data['definition'] = self.definition.to_dict()
            data['workflow_key'] = self.definition.workflow_key
        snapshot_meta = (
            (self.context_json or {}).get('testcase_library_snapshot')
            if isinstance(self.context_json, dict) else None)
        if isinstance(snapshot_meta, dict):
            data['testcase_library_snapshot'] = snapshot_meta
        if with_steps:
            steps = sorted(self.steps, key=lambda s: s.position or 0)
            data['steps'] = [step.to_dict() for step in steps]
        return data


class AgentContextSnapshot(db.Model):
    """Immutable Hub-owned identity and team role contract for one Run.

    The document deliberately contains no credentials.  Workers receive the
    identifier and digest with every AgentTask and later acknowledge exactly
    the same pair in progress/result receipts.
    """
    __tablename__ = 'agent_context_snapshots'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    snapshot_id = db.Column(db.String(64), nullable=False, unique=True)
    schema_version = db.Column(db.Integer, nullable=False, default=1)
    workflow_run_id = db.Column(
        db.Integer, db.ForeignKey('workflow_runs.id', ondelete='CASCADE'), nullable=False,
        unique=True, index=True)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), index=True)
    team_id = db.Column(db.Integer, db.ForeignKey('agent_teams.id'), index=True)
    context_sha256 = db.Column(db.String(64), nullable=False, index=True)
    context_json = db.Column(db.JSON, nullable=False)
    created_at = db.Column(db.DateTime, default=_now, nullable=False)

    run = db.relationship('WorkflowRun', backref=db.backref(
        'agent_context_snapshot', uselist=False,
        cascade='all, delete-orphan', single_parent=True))

    def to_dict(self, include_context=True):
        data = {
            'snapshot_id': self.snapshot_id,
            'schema_version': int(self.schema_version or 1),
            'workflow_run_id': self.workflow_run_id,
            'project_id': self.project_id,
            'team_id': self.team_id,
            'context_sha256': self.context_sha256,
            'created_at': str(self.created_at) if self.created_at else None,
        }
        if include_context:
            data['context'] = self.context_json or {}
        return data


class AgentTeam(db.Model):
    """Project team roles are independent of Claw roles and AgentPost."""
    __tablename__ = 'agent_teams'

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=False, index=True)
    name = db.Column(db.String(160), nullable=False)
    objective = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(24), nullable=False, default='active')
    primary_manager_claw_id = db.Column(db.Integer, db.ForeignKey('openclaw_instances.id'), nullable=False)
    backup_manager_claw_id = db.Column(db.Integer, db.ForeignKey('openclaw_instances.id'))
    active_manager_claw_id = db.Column(db.Integer, db.ForeignKey('openclaw_instances.id'))
    manager_epoch = db.Column(db.Integer, nullable=False, default=0)
    manager_session_id = db.Column(db.String(128), nullable=False, default='')
    manager_lease_expires_at = db.Column(db.DateTime)
    policy_json = db.Column(db.JSON, nullable=False)
    version = db.Column(db.Integer, nullable=False, default=1)
    created_at = db.Column(db.DateTime, default=_now, nullable=False)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now, nullable=False)
    __table_args__ = (db.UniqueConstraint('project_id', 'name', name='uq_agent_team_project_name'),)

    def has_manager_authority(self, claw_id):
        """Return durable authority granted by an active team role."""
        return bool(
            self.status == 'active'
            and claw_id in (
                self.primary_manager_claw_id,
                self.backup_manager_claw_id,
            )
        )

    def to_dict(self):
        authority_active = bool(
            self.status == 'active' and self.primary_manager_claw_id)
        effective_manager_id = (
            self.primary_manager_claw_id if authority_active else None)
        return {
            'id': self.id, 'project_id': self.project_id, 'name': self.name,
            'objective': self.objective, 'status': self.status, 'version': self.version,
            'primary_manager_claw_id': self.primary_manager_claw_id,
            'backup_manager_claw_id': self.backup_manager_claw_id,
            # Compatibility fields project durable manager assignment. They no
            # longer represent a five-minute authorization lease.
            'active_manager_claw_id': effective_manager_id,
            'manager_epoch': self.manager_epoch,
            'manager_lease_expires_at': None,
            'manager_lease_active': authority_active,
            'manager_authority_active': authority_active,
            'manager_authority_mode': 'team_role_assignment',
            'policy': self.policy_json or {},
            'members': [member.to_dict() for member in sorted(self.members, key=lambda row: row.id or 0)],
            'updated_at': str(self.updated_at),
        }


class AgentTeamMember(db.Model):
    __tablename__ = 'agent_team_members'
    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey('agent_teams.id'), nullable=False, index=True)
    claw_id = db.Column(db.Integer, db.ForeignKey('openclaw_instances.id'), nullable=False, index=True)
    role_key = db.Column(db.String(40), nullable=False)
    specialties_json = db.Column(db.JSON, nullable=False)
    team = db.relationship('AgentTeam', backref=db.backref('members', cascade='all, delete-orphan'))
    __table_args__ = (db.UniqueConstraint('team_id', 'claw_id', 'role_key', name='uq_agent_team_member_role'),)

    def to_dict(self):
        return {'claw_id': self.claw_id, 'role_key': self.role_key,
                'specialties': self.specialties_json or []}


class AgentTeamKnowledgeResource(db.Model):
    """A team-scoped pointer to canonical Hub knowledge.

    Content and revisions stay in KnowledgeEntry; this table only defines the
    team library and its curation audit trail.
    """
    __tablename__ = 'agent_team_knowledge_resources'
    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey('agent_teams.id', ondelete='CASCADE'),
                        nullable=False, index=True)
    knowledge_id = db.Column(db.Integer, db.ForeignKey('knowledge_entries.id', ondelete='CASCADE'),
                             nullable=False, index=True)
    linked_by_type = db.Column(db.String(20), nullable=False)
    linked_by_id = db.Column(db.Integer)
    linked_by_name = db.Column(db.String(100), nullable=False, default='')
    created_at = db.Column(db.DateTime, default=_now, nullable=False)
    team = db.relationship('AgentTeam', backref=db.backref(
        'knowledge_resources', cascade='all, delete-orphan'))
    knowledge = db.relationship('KnowledgeEntry')
    __table_args__ = (db.UniqueConstraint(
        'team_id', 'knowledge_id', name='uq_agent_team_knowledge_resource'),)


class AgentTeamSkillResource(db.Model):
    """A team-scoped pointer to a canonical Skill market package."""
    __tablename__ = 'agent_team_skill_resources'
    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey('agent_teams.id', ondelete='CASCADE'),
                        nullable=False, index=True)
    skill_id = db.Column(db.Integer, db.ForeignKey('skills.id', ondelete='CASCADE'),
                         nullable=False, index=True)
    linked_by_type = db.Column(db.String(20), nullable=False)
    linked_by_id = db.Column(db.Integer)
    linked_by_name = db.Column(db.String(100), nullable=False, default='')
    created_at = db.Column(db.DateTime, default=_now, nullable=False)
    team = db.relationship('AgentTeam', backref=db.backref(
        'skill_resources', cascade='all, delete-orphan'))
    skill = db.relationship('Skill')
    __table_args__ = (db.UniqueConstraint(
        'team_id', 'skill_id', name='uq_agent_team_skill_resource'),)


class AgentTeamMemberTask(db.Model):
    """Agent-authored activity log; not an authoritative Workflow/Run result."""
    __tablename__ = 'agent_team_member_tasks'
    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey('agent_teams.id'), nullable=False)
    claw_id = db.Column(db.Integer, db.ForeignKey('openclaw_instances.id'), nullable=False)
    task_key = db.Column(db.String(96), nullable=False)
    title = db.Column(db.String(240), nullable=False)
    task_type = db.Column(db.String(24), nullable=False)
    reference = db.Column(db.String(240), nullable=False, default='')
    status = db.Column(db.String(24), nullable=False)
    progress_percent = db.Column(db.Integer)
    progress_message = db.Column(db.Text, default='')
    started_at = db.Column(db.DateTime, default=_now, nullable=False)
    updated_at = db.Column(db.DateTime, default=_now, nullable=False)
    finished_at = db.Column(db.DateTime)
    __table_args__ = (
        db.UniqueConstraint('team_id', 'claw_id', 'task_key', name='uq_team_member_task_key'),
        db.Index('ix_team_member_tasks_history', 'team_id', 'claw_id', 'id'),
    )

    def to_dict(self):
        return {
            'id': self.id, 'task_key': self.task_key, 'title': self.title, 'task_type': self.task_type,
            'reference': self.reference, 'status': self.status, 'progress_percent': self.progress_percent,
            'progress_message': self.progress_message, 'started_at': str(self.started_at),
            'updated_at': str(self.updated_at), 'finished_at': str(self.finished_at) if self.finished_at else None,
            'source': 'agent_self_report',
        }


class AgentTeamMemberStatus(db.Model):
    __tablename__ = 'agent_team_member_statuses'
    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey('agent_teams.id'), nullable=False)
    claw_id = db.Column(db.Integer, db.ForeignKey('openclaw_instances.id'), nullable=False)
    state = db.Column(db.String(24), nullable=False)
    summary = db.Column(db.String(500), nullable=False, default='')
    current_task_id = db.Column(db.Integer, db.ForeignKey('agent_team_member_tasks.id'))
    version = db.Column(db.Integer, nullable=False, default=0)
    reported_at = db.Column(db.DateTime, nullable=False, default=_now)
    __table_args__ = (db.UniqueConstraint('team_id', 'claw_id', name='uq_team_member_status'),)


class AgentTeamMemberReport(db.Model):
    """Immutable report receipts for retry deduplication and progress history."""
    __tablename__ = 'agent_team_member_reports'
    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey('agent_teams.id'), nullable=False)
    claw_id = db.Column(db.Integer, db.ForeignKey('openclaw_instances.id'), nullable=False)
    event_id = db.Column(db.String(96), nullable=False)
    request_sha256 = db.Column(db.String(64), nullable=False)
    task_id = db.Column(db.Integer, db.ForeignKey('agent_team_member_tasks.id'))
    response_json = db.Column(db.JSON, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=_now)
    __table_args__ = (
        db.UniqueConstraint('team_id', 'claw_id', 'event_id', name='uq_team_member_report_event'),
        db.Index('ix_team_member_report_task', 'team_id', 'claw_id', 'task_id', 'id'),
    )


class AgentTeamMission(db.Model):
    """Immutable team policy/roster baseline; execution stays in Mission/Stage."""
    __tablename__ = 'agent_team_missions'
    mission_id = db.Column(db.Integer, db.ForeignKey('workflow_missions.id'), primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey('agent_teams.id'), nullable=False, index=True)
    team_version = db.Column(db.Integer, nullable=False)
    snapshot_json = db.Column(db.JSON, nullable=False)
    plan_sha256 = db.Column(db.String(64))
    created_at = db.Column(db.DateTime, default=_now, nullable=False)


class WorkflowMission(db.Model):
    """One run-scoped autonomy grant owned by a main Agent."""
    __tablename__ = 'workflow_missions'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    mission_key = db.Column(db.String(128), nullable=False, unique=True)
    project_id = db.Column(
        db.Integer, db.ForeignKey('projects.id'), nullable=False, index=True)
    main_claw_id = db.Column(
        db.Integer, db.ForeignKey('openclaw_instances.id'),
        nullable=False, index=True)
    objective = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(24), nullable=False, default='active', index=True)
    control_mode = db.Column(
        db.String(32), nullable=False, default='agent_autonomous')
    allowed_definition_ids_json = db.Column(db.JSON)
    denied_definition_ids_json = db.Column(db.JSON)
    # Delegated Workers are an explicit allow-list in addition to the main
    # Agent. Mission dispatch still defaults to main_claw_id when omitted.
    allowed_worker_claw_ids_json = db.Column(db.JSON)
    max_child_runs = db.Column(db.Integer, nullable=False, default=20)
    child_run_count = db.Column(db.Integer, nullable=False, default=0)
    max_retries_per_flow = db.Column(db.Integer, nullable=False, default=3)
    allow_external_notification = db.Column(
        db.Boolean, nullable=False, default=False)
    allow_destructive_actions = db.Column(
        db.Boolean, nullable=False, default=False)
    context_json = db.Column(db.JSON)
    created_by_type = db.Column(db.String(16), nullable=False)
    created_by_id = db.Column(db.Integer, nullable=False)
    created_by_name = db.Column(db.String(160), default='')
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    completed_at = db.Column(db.DateTime)
    cancelled_at = db.Column(db.DateTime)
    version = db.Column(db.Integer, nullable=False, default=1)
    created_at = db.Column(db.DateTime, default=_now, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=_now, onupdate=_now, nullable=False)

    project = db.relationship('Project', backref='workflow_missions')
    main_claw = db.relationship('OpenClawInstance', backref='workflow_missions')

    __table_args__ = (
        db.Index('ix_workflow_mission_main_status', 'main_claw_id', 'status'),
        db.Index('ix_workflow_mission_project_status', 'project_id', 'status'),
    )

    def effective_status(self, now=None):
        current = now or _now()
        if self.status == 'active' and self.expires_at <= current:
            return 'expired'
        return self.status or 'active'

    def to_dict(self, with_dispatches=False):
        data = {
            'id': self.id,
            'mission_key': self.mission_key,
            'project_id': self.project_id,
            'main_claw_id': self.main_claw_id,
            'main_claw_name': self.main_claw.name if self.main_claw else '',
            'objective': self.objective,
            'status': self.status,
            'effective_status': self.effective_status(),
            'control_mode': self.control_mode or 'agent_autonomous',
            'allowed_definition_ids': self.allowed_definition_ids_json or [],
            'denied_definition_ids': self.denied_definition_ids_json or [],
            'allowed_worker_claw_ids': (
                self.allowed_worker_claw_ids_json or []),
            'max_child_runs': self.max_child_runs or 20,
            'child_run_count': self.child_run_count or 0,
            'max_retries_per_flow': self.max_retries_per_flow or 3,
            'allow_external_notification': bool(
                self.allow_external_notification),
            'allow_destructive_actions': bool(self.allow_destructive_actions),
            'context': self.context_json or {},
            'created_by_type': self.created_by_type,
            'created_by_id': self.created_by_id,
            'created_by_name': self.created_by_name or '',
            'expires_at': str(self.expires_at) if self.expires_at else None,
            'completed_at': str(self.completed_at) if self.completed_at else None,
            'cancelled_at': str(self.cancelled_at) if self.cancelled_at else None,
            'version': self.version or 1,
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }
        if with_dispatches:
            data['dispatches'] = [
                item.to_dict() for item in sorted(
                    self.dispatches, key=lambda row: row.id or 0)]
        return data


class MissionStage(db.Model):
    """Fenced, optimistically locked execution stage within a Mission."""
    __tablename__ = 'mission_stages'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    mission_id = db.Column(
        db.Integer, db.ForeignKey('workflow_missions.id', ondelete='CASCADE'),
        nullable=False, index=True)
    stage_key = db.Column(db.String(80), nullable=False)
    stage_version = db.Column(db.Integer, nullable=False, default=1)
    role_key = db.Column(db.String(80), nullable=False)
    assigned_claw_id = db.Column(
        db.Integer, db.ForeignKey('openclaw_instances.id'), index=True)
    workflow_run_id = db.Column(
        db.Integer, db.ForeignKey('workflow_runs.id'), index=True)
    state = db.Column(db.String(32), nullable=False, default='ready', index=True)
    input_snapshot_json = db.Column(db.JSON, nullable=False)
    output_artifact_id = db.Column(
        db.Integer, db.ForeignKey('agent_artifacts.id'), index=True)
    active_handoff_id = db.Column(db.Integer, index=True)
    evidence_refs_json = db.Column(db.JSON, nullable=False)
    last_reason_code = db.Column(db.String(80), nullable=False, default='')
    fencing_token = db.Column(db.Integer, nullable=False, default=0)
    retry_count = db.Column(db.Integer, nullable=False, default=0)
    repair_count = db.Column(db.Integer, nullable=False, default=0)
    version = db.Column(db.Integer, nullable=False, default=1)
    created_at = db.Column(db.DateTime, default=_now, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=_now, onupdate=_now, nullable=False)

    mission = db.relationship(
        'WorkflowMission', backref=db.backref(
            'stages', cascade='all, delete-orphan'))
    assigned_claw = db.relationship('OpenClawInstance')
    workflow_run = db.relationship('WorkflowRun')
    output_artifact = db.relationship(
        'AgentArtifact', foreign_keys=[output_artifact_id])

    __table_args__ = (
        db.UniqueConstraint(
            'mission_id', 'stage_key', 'stage_version',
            name='uq_mission_stage_key_version'),
        db.Index(
            'ix_mission_stage_mission_state',
            'mission_id', 'state', 'updated_at'),
    )

    def to_dict(self, include_input=False):
        data = {
            'id': self.id,
            'mission_id': self.mission_id,
            'stage_key': self.stage_key,
            'stage_version': self.stage_version or 1,
            'role_key': self.role_key,
            'assigned_claw_id': self.assigned_claw_id,
            'assigned_claw_name': (
                self.assigned_claw.name if self.assigned_claw else ''),
            'workflow_run_id': self.workflow_run_id,
            'state': self.state,
            'output_artifact_id': self.output_artifact_id,
            'active_handoff_id': self.active_handoff_id,
            'evidence_refs': self.evidence_refs_json or [],
            'last_reason_code': self.last_reason_code or '',
            'fencing_token': self.fencing_token or 0,
            'retry_count': self.retry_count or 0,
            'repair_count': self.repair_count or 0,
            'version': self.version or 1,
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }
        if include_input:
            data['input_snapshot'] = self.input_snapshot_json or {}
        return data


class MissionHandoff(db.Model):
    """Immutable, versioned hand-off contract between two Mission Stages."""
    __tablename__ = 'mission_handoffs'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    mission_id = db.Column(
        db.Integer, db.ForeignKey('workflow_missions.id', ondelete='CASCADE'),
        nullable=False, index=True)
    mission_version = db.Column(db.Integer, nullable=False)
    handoff_version = db.Column(db.Integer, nullable=False, default=1)
    source_stage_record_id = db.Column(
        db.Integer, db.ForeignKey('mission_stages.id'), nullable=False,
        index=True)
    target_stage_record_id = db.Column(
        db.Integer, db.ForeignKey('mission_stages.id'), nullable=False,
        index=True)
    source_stage_key = db.Column(db.String(80), nullable=False)
    target_stage_key = db.Column(db.String(80), nullable=False)
    from_role = db.Column(db.String(80), nullable=False)
    to_role = db.Column(db.String(80), nullable=False)
    objective = db.Column(db.Text, nullable=False)
    input_artifact_refs_json = db.Column(db.JSON, nullable=False)
    accepted_findings_json = db.Column(db.JSON, nullable=False)
    open_questions_json = db.Column(db.JSON, nullable=False)
    constraints_json = db.Column(db.JSON, nullable=False)
    acceptance_criteria_json = db.Column(db.JSON, nullable=False)
    known_pitfalls_json = db.Column(db.JSON, nullable=False)
    recommended_next_actions_json = db.Column(db.JSON, nullable=False)
    evidence_refs_json = db.Column(db.JSON, nullable=False)
    source_run_id = db.Column(db.Integer, index=True)
    source_step_id = db.Column(db.String(100), nullable=False, default='')

    producer_type = db.Column(db.String(16), nullable=False)
    producer_id = db.Column(db.Integer, nullable=False)
    producer_name = db.Column(db.String(160), nullable=False, default='')
    producer_claw_id = db.Column(
        db.Integer, db.ForeignKey('openclaw_instances.id'), index=True)
    producer_role_key = db.Column(db.String(80), nullable=False, default='')
    producer_provider = db.Column(db.String(50), nullable=False, default='')
    producer_profile_version = db.Column(
        db.Integer, nullable=False, default=1)

    status = db.Column(
        db.String(24), nullable=False, default='draft', index=True)
    content_sha256 = db.Column(db.String(71), nullable=False, index=True)
    idempotency_key = db.Column(db.String(128), nullable=False)
    request_sha256 = db.Column(db.String(71), nullable=False)
    version = db.Column(db.Integer, nullable=False, default=1)
    decision_reason_code = db.Column(db.String(80), nullable=False, default='')
    decision_comment = db.Column(db.Text)
    decided_by_type = db.Column(db.String(16))
    decided_by_id = db.Column(db.Integer)
    decided_by_name = db.Column(db.String(160))
    submitted_at = db.Column(db.DateTime)
    accepted_at = db.Column(db.DateTime)
    rejected_at = db.Column(db.DateTime)
    superseded_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=_now, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=_now, onupdate=_now, nullable=False)

    mission = db.relationship('WorkflowMission', backref='handoffs')
    source_stage = db.relationship(
        'MissionStage', foreign_keys=[source_stage_record_id])
    target_stage = db.relationship(
        'MissionStage', foreign_keys=[target_stage_record_id])
    producer_claw = db.relationship('OpenClawInstance')

    __table_args__ = (
        db.UniqueConstraint(
            'mission_id', 'source_stage_key', 'target_stage_key',
            'handoff_version', name='uq_mission_handoff_path_version'),
        db.UniqueConstraint(
            'mission_id', 'producer_type', 'producer_id', 'idempotency_key',
            name='uq_mission_handoff_producer_idempotency'),
        db.Index(
            'ix_mission_handoff_target_status',
            'mission_id', 'target_stage_record_id', 'status'),
        db.Index(
            'ix_mission_handoff_source_run_step',
            'source_run_id', 'source_step_id'),
    )

    def to_dict(self):
        return {
            'schema': 1,
            'handoff_id': self.id,
            'id': self.id,
            'handoff_version': self.handoff_version or 1,
            'version': self.version or 1,
            'mission_id': self.mission_id,
            'mission_version': self.mission_version,
            'source_stage_id': self.source_stage_key,
            'target_stage_id': self.target_stage_key,
            'from_role': self.from_role,
            'to_role': self.to_role,
            'objective': self.objective,
            'input_artifact_refs': self.input_artifact_refs_json or [],
            'accepted_findings': self.accepted_findings_json or [],
            'open_questions': self.open_questions_json or [],
            'constraints': self.constraints_json or [],
            'acceptance_criteria': self.acceptance_criteria_json or [],
            'known_pitfalls': self.known_pitfalls_json or [],
            'recommended_next_actions': (
                self.recommended_next_actions_json or []),
            'evidence_refs': self.evidence_refs_json or [],
            'source_run_id': self.source_run_id,
            'source_step_id': self.source_step_id or '',
            'producer': {
                'type': self.producer_type,
                'id': self.producer_id,
                'name': self.producer_name or '',
                'claw_id': self.producer_claw_id,
                'role_key': self.producer_role_key or '',
                'provider': self.producer_provider or '',
                'profile_version': self.producer_profile_version or 1,
            },
            'status': self.status,
            'content_sha256': self.content_sha256,
            'request_sha256': self.request_sha256,
            'decision': {
                'reason_code': self.decision_reason_code or '',
                'comment': self.decision_comment or '',
                'decided_by_type': self.decided_by_type,
                'decided_by_id': self.decided_by_id,
                'decided_by_name': self.decided_by_name or '',
            },
            'submitted_at': (
                str(self.submitted_at) if self.submitted_at else None),
            'accepted_at': (
                str(self.accepted_at) if self.accepted_at else None),
            'rejected_at': (
                str(self.rejected_at) if self.rejected_at else None),
            'superseded_at': (
                str(self.superseded_at) if self.superseded_at else None),
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }


class AgentEvalDataset(db.Model):
    """Versioned evaluation dataset for one project and Agent role."""
    __tablename__ = 'agent_eval_datasets'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    project_id = db.Column(
        db.Integer, db.ForeignKey('projects.id'), nullable=False, index=True)
    dataset_key = db.Column(db.String(100), nullable=False)
    role_key = db.Column(db.String(80), nullable=False, index=True)
    version = db.Column(db.Integer, nullable=False, default=1)
    split = db.Column(db.String(24), nullable=False, index=True)
    status = db.Column(
        db.String(24), nullable=False, default='draft', index=True)
    rubric_version = db.Column(db.Integer, nullable=False, default=1)
    dataset_sha256 = db.Column(db.String(71), nullable=False, default='')
    review_policy_json = db.Column(db.JSON, nullable=False, default=dict)
    review_status = db.Column(
        db.String(24), nullable=False, default='not_required', index=True)
    review_records_json = db.Column(db.JSON, nullable=False, default=list)
    review_completed_at = db.Column(db.DateTime)
    idempotency_key = db.Column(db.String(128), nullable=False)
    request_sha256 = db.Column(db.String(71), nullable=False)
    created_by_type = db.Column(db.String(16), nullable=False)
    created_by_id = db.Column(db.Integer, nullable=False)
    created_by_name = db.Column(db.String(160), nullable=False, default='')
    approved_by_type = db.Column(db.String(16))
    approved_by_id = db.Column(db.Integer)
    approved_by_name = db.Column(db.String(160))
    frozen_at = db.Column(db.DateTime)
    retired_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=_now, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=_now, onupdate=_now, nullable=False)

    project = db.relationship('Project', backref='agent_eval_datasets')

    __table_args__ = (
        db.UniqueConstraint(
            'project_id', 'dataset_key', 'version',
            name='uq_agent_eval_dataset_project_key_version'),
        db.UniqueConstraint(
            'project_id', 'created_by_type', 'created_by_id',
            'idempotency_key', name='uq_agent_eval_dataset_idempotency'),
        db.Index(
            'ix_agent_eval_dataset_project_role_status',
            'project_id', 'role_key', 'status'),
    )

    def to_dict(self, include_cases=False, include_hidden=False):
        latest_reviews = {}
        for item in (self.review_records_json or []):
            if isinstance(item, dict) and item.get('reviewer_key'):
                latest_reviews[str(item['reviewer_key'])] = item
        data = {
            'id': self.id,
            'project_id': self.project_id,
            'dataset_key': self.dataset_key,
            'role_key': self.role_key,
            'version': self.version or 1,
            'split': self.split,
            'status': self.status,
            'rubric_version': self.rubric_version or 1,
            'dataset_sha256': self.dataset_sha256 or '',
            'review_policy': self.review_policy_json or {},
            'review_status': self.review_status or 'not_required',
            'review_records': self.review_records_json or [],
            'approved_reviewer_count': sum(
                1 for item in latest_reviews.values()
                if item.get('decision') == 'approved'),
            'review_completed_at': (
                str(self.review_completed_at)
                if self.review_completed_at else None),
            'created_by': {
                'type': self.created_by_type,
                'id': self.created_by_id,
                'name': self.created_by_name or '',
            },
            'approved_by': {
                'type': self.approved_by_type,
                'id': self.approved_by_id,
                'name': self.approved_by_name or '',
            } if self.approved_by_id else None,
            'frozen_at': str(self.frozen_at) if self.frozen_at else None,
            'retired_at': str(self.retired_at) if self.retired_at else None,
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }
        if include_cases:
            rows = sorted(self.cases, key=lambda item: (item.case_key, item.id))
            data['cases'] = [
                row.to_dict(include_hidden=include_hidden) for row in rows]
        return data


class AgentEvalCase(db.Model):
    """Immutable input and expected contract inside an Eval Dataset version."""
    __tablename__ = 'agent_eval_cases'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    dataset_id = db.Column(
        db.Integer, db.ForeignKey('agent_eval_datasets.id', ondelete='CASCADE'),
        nullable=False, index=True)
    case_key = db.Column(db.String(120), nullable=False)
    input_snapshot_json = db.Column(db.JSON, nullable=False)
    expected_contract_json = db.Column(db.JSON, nullable=False)
    required_evidence_json = db.Column(db.JSON, nullable=False)
    deterministic_checks_json = db.Column(db.JSON, nullable=False)
    allowed_variance_json = db.Column(db.JSON, nullable=False)
    hidden_tags_json = db.Column(db.JSON, nullable=False)
    input_sha256 = db.Column(db.String(71), nullable=False, index=True)
    status = db.Column(
        db.String(24), nullable=False, default='active', index=True)
    idempotency_key = db.Column(db.String(128), nullable=False)
    request_sha256 = db.Column(db.String(71), nullable=False)
    created_by_type = db.Column(db.String(16), nullable=False)
    created_by_id = db.Column(db.Integer, nullable=False)
    created_by_name = db.Column(db.String(160), nullable=False, default='')
    created_at = db.Column(db.DateTime, default=_now, nullable=False)

    dataset = db.relationship(
        'AgentEvalDataset', backref=db.backref(
            'cases', cascade='all, delete-orphan'))

    __table_args__ = (
        db.UniqueConstraint(
            'dataset_id', 'case_key', name='uq_agent_eval_case_dataset_key'),
        db.UniqueConstraint(
            'dataset_id', 'created_by_type', 'created_by_id',
            'idempotency_key', name='uq_agent_eval_case_idempotency'),
    )

    def to_dict(self, include_hidden=False):
        data = {
            'id': self.id,
            'dataset_id': self.dataset_id,
            'case_key': self.case_key,
            'input_snapshot': self.input_snapshot_json or {},
            'expected_contract': self.expected_contract_json or {},
            'required_evidence': self.required_evidence_json or [],
            'deterministic_checks': self.deterministic_checks_json or [],
            'allowed_variance': self.allowed_variance_json or {},
            'input_sha256': self.input_sha256,
            'status': self.status,
            'created_at': str(self.created_at) if self.created_at else None,
        }
        if include_hidden:
            data['hidden_tags'] = self.hidden_tags_json or []
        return data


class AgentEvalRun(db.Model):
    """One reproducible execution of an Eval Case by a concrete Agent build."""
    __tablename__ = 'agent_eval_runs'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    project_id = db.Column(
        db.Integer, db.ForeignKey('projects.id'), nullable=False, index=True)
    case_id = db.Column(
        db.Integer, db.ForeignKey('agent_eval_cases.id'),
        nullable=False, index=True)
    dataset_version = db.Column(db.Integer, nullable=False)
    case_input_sha256 = db.Column(db.String(71), nullable=False)
    claw_id = db.Column(
        db.Integer, db.ForeignKey('openclaw_instances.id'),
        nullable=False, index=True)
    profile_version = db.Column(db.Integer, nullable=False)
    provider = db.Column(db.String(50), nullable=False)
    model = db.Column(db.String(120), nullable=False)
    worker_release = db.Column(db.String(160), nullable=False)
    mission_id = db.Column(
        db.Integer, db.ForeignKey('workflow_missions.id'), index=True)
    workflow_run_id = db.Column(
        db.Integer, db.ForeignKey('workflow_runs.id'), index=True)
    artifact_id = db.Column(
        db.Integer, db.ForeignKey('agent_artifacts.id'), index=True)
    status = db.Column(db.String(32), nullable=False, index=True)
    usage_json = db.Column(db.JSON, nullable=False)
    timing_json = db.Column(db.JSON, nullable=False)
    idempotency_key = db.Column(db.String(128), nullable=False)
    request_sha256 = db.Column(db.String(71), nullable=False)
    created_by_type = db.Column(db.String(16), nullable=False)
    created_by_id = db.Column(db.Integer, nullable=False)
    created_by_name = db.Column(db.String(160), nullable=False, default='')
    started_at = db.Column(db.DateTime)
    finished_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=_now, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=_now, onupdate=_now, nullable=False)

    project = db.relationship('Project')
    case = db.relationship('AgentEvalCase', backref='eval_runs')
    claw = db.relationship('OpenClawInstance')
    mission = db.relationship('WorkflowMission')
    workflow_run = db.relationship('WorkflowRun')
    artifact = db.relationship('AgentArtifact')

    __table_args__ = (
        db.UniqueConstraint(
            'project_id', 'created_by_type', 'created_by_id',
            'idempotency_key', name='uq_agent_eval_run_idempotency'),
        db.Index(
            'ix_agent_eval_run_project_status_created',
            'project_id', 'status', 'created_at'),
    )

    def to_dict(self, include_scores=True):
        data = {
            'id': self.id,
            'project_id': self.project_id,
            'case_id': self.case_id,
            'case_key': self.case.case_key if self.case else '',
            'dataset_id': self.case.dataset_id if self.case else None,
            'dataset_version': self.dataset_version,
            'case_input_sha256': self.case_input_sha256,
            'claw_id': self.claw_id,
            'claw_name': self.claw.name if self.claw else '',
            'profile_version': self.profile_version,
            'provider': self.provider,
            'model': self.model,
            'worker_release': self.worker_release,
            'mission_id': self.mission_id,
            'workflow_run_id': self.workflow_run_id,
            'artifact_id': self.artifact_id,
            'status': self.status,
            'usage': self.usage_json or {},
            'timing': self.timing_json or {},
            'started_at': str(self.started_at) if self.started_at else None,
            'finished_at': str(self.finished_at) if self.finished_at else None,
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }
        if include_scores:
            rows = sorted(self.scores, key=lambda item: item.id or 0)
            data['scores'] = [row.to_dict() for row in rows]
        return data


class AgentEvalScore(db.Model):
    """Version-preserving deterministic, human, or judge score."""
    __tablename__ = 'agent_eval_scores'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    eval_run_id = db.Column(
        db.Integer, db.ForeignKey('agent_eval_runs.id', ondelete='CASCADE'),
        nullable=False, index=True)
    scorer_type = db.Column(db.String(24), nullable=False, index=True)
    scorer_version = db.Column(db.Integer, nullable=False)
    scorer_actor_type = db.Column(db.String(16), nullable=False)
    scorer_actor_id = db.Column(db.Integer, nullable=False)
    scorer_name = db.Column(db.String(160), nullable=False, default='')
    dimension_scores_json = db.Column(db.JSON, nullable=False)
    fatal_violations_json = db.Column(db.JSON, nullable=False)
    total_score = db.Column(db.Float, nullable=False)
    review_comment = db.Column(db.Text)
    idempotency_key = db.Column(db.String(128), nullable=False)
    request_sha256 = db.Column(db.String(71), nullable=False)
    created_at = db.Column(db.DateTime, default=_now, nullable=False)

    eval_run = db.relationship(
        'AgentEvalRun', backref=db.backref(
            'scores', cascade='all, delete-orphan'))

    __table_args__ = (
        db.UniqueConstraint(
            'eval_run_id', 'scorer_type', 'scorer_actor_type',
            'scorer_actor_id', 'scorer_version',
            name='uq_agent_eval_score_scorer_version'),
        db.UniqueConstraint(
            'scorer_actor_type', 'scorer_actor_id', 'idempotency_key',
            name='uq_agent_eval_score_idempotency'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'eval_run_id': self.eval_run_id,
            'scorer_type': self.scorer_type,
            'scorer_version': self.scorer_version,
            'scorer': {
                'type': self.scorer_actor_type,
                'id': self.scorer_actor_id,
                'name': self.scorer_name or '',
            },
            'dimension_scores': self.dimension_scores_json or {},
            'fatal_violations': self.fatal_violations_json or [],
            'total_score': float(self.total_score or 0),
            'review_comment': self.review_comment or '',
            'created_at': str(self.created_at) if self.created_at else None,
        }


class WorkflowMissionDispatch(db.Model):
    """Idempotent child-Run decision made by the Mission main Agent."""
    __tablename__ = 'workflow_mission_dispatches'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    mission_id = db.Column(
        db.Integer, db.ForeignKey('workflow_missions.id', ondelete='CASCADE'),
        nullable=False, index=True)
    definition_id = db.Column(
        db.Integer, db.ForeignKey('workflow_definitions.id'),
        nullable=False, index=True)
    # Deliberately not an FK: Mission audit/idempotency must survive a user
    # deleting the child Run from the Workflow history page.
    workflow_run_id = db.Column(
        db.Integer, nullable=False, unique=True, index=True)
    decision_key = db.Column(db.String(128), nullable=False)
    idempotency_key = db.Column(db.String(128), nullable=False)
    request_hash = db.Column(db.String(64), nullable=False)
    reason = db.Column(db.Text)
    status = db.Column(db.String(24), nullable=False, default='created')
    created_by_claw_id = db.Column(db.Integer, nullable=False)
    # Nullable only for historical rows created before Worker delegation was
    # introduced; every new dispatch persists the selected Worker explicitly.
    worker_claw_id = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, default=_now, nullable=False)

    mission = db.relationship(
        'WorkflowMission', backref=db.backref(
            'dispatches', cascade='all, delete-orphan'))
    definition = db.relationship('WorkflowDefinition')

    __table_args__ = (
        db.UniqueConstraint(
            'mission_id', 'idempotency_key',
            name='uq_workflow_mission_dispatch_idempotency'),
        db.Index(
            'ix_workflow_mission_dispatch_definition',
            'mission_id', 'definition_id', 'created_at'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'mission_id': self.mission_id,
            'workflow_definition_id': self.definition_id,
            'definition_id': self.definition_id,
            'workflow_run_id': self.workflow_run_id,
            'decision_key': self.decision_key,
            'idempotency_key': self.idempotency_key,
            'reason': self.reason or '',
            'status': self.status or 'created',
            'created_by_claw_id': self.created_by_claw_id,
            'worker_claw_id': self.worker_claw_id,
            'created_at': str(self.created_at) if self.created_at else None,
        }


class WorkflowRunLibrarySnapshot(db.Model):
    """Immutable testcase-library content frozen for one Workflow Run."""
    __tablename__ = 'workflow_run_library_snapshots'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    workflow_run_id = db.Column(
        db.Integer, db.ForeignKey('workflow_runs.id', ondelete='CASCADE'),
        nullable=False, unique=True, index=True)
    # Deliberately not an FK: historical Runs must survive library deletion.
    library_id = db.Column(db.Integer, nullable=False, index=True)
    library_revision = db.Column(db.Integer, nullable=False)
    snapshot_version = db.Column(db.Integer, nullable=False)
    content_hash = db.Column(db.String(71), nullable=False, index=True)
    case_ids_json = db.Column(db.JSON)
    case_count = db.Column(db.Integer, nullable=False, default=0)
    snapshot_json = db.Column(db.JSON, nullable=False)
    frozen_by = db.Column(db.String(160), default='system')
    frozen_at = db.Column(db.DateTime, default=_now, index=True)

    run = db.relationship('WorkflowRun')

    __table_args__ = (
        db.Index(
            'ix_workflow_run_library_snapshot_library_revision',
            'library_id', 'library_revision'),
    )

    def to_dict(self, with_cases=False):
        data = {
            'id': self.id,
            'workflow_run_id': self.workflow_run_id,
            'library_id': self.library_id,
            'revision': self.library_revision,
            'snapshot_version': self.snapshot_version,
            'content_hash': self.content_hash,
            'case_ids': self.case_ids_json or [],
            'case_count': self.case_count,
            'frozen_by': self.frozen_by or 'system',
            'frozen_at': str(self.frozen_at) if self.frozen_at else None,
            'cases_api': (
                f'/api/v1/workflow-runs/{self.workflow_run_id}/'
                'testcase-library-snapshot'),
        }
        if with_cases:
            case_ids = set(self.case_ids_json or [])
            snapshot = self.snapshot_json or {}
            data['mindmap'] = snapshot.get('mindmap')
            data['cases'] = [
                case for case in (snapshot.get('cases') or [])
                if case.get('id') in case_ids
            ]
            data['snapshot_schema'] = snapshot.get('schema')
        return data


# ============== Shared execution resource leases (P0-E) ==============

class ResourceLease(db.Model):
    """Auditable TTL lease; one row per resource in an atomic lease group."""
    __tablename__ = 'resource_leases'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    lease_group_id = db.Column(db.String(64), nullable=False, index=True)
    resource_key = db.Column(db.String(255), nullable=False, index=True)
    resource_type = db.Column(db.String(32), nullable=False, index=True)
    # Equals resource_key only while active. UNIQUE makes cross-process
    # acquisition race-safe while allowing unlimited historical NULL rows.
    active_slot = db.Column(db.String(255), unique=True, index=True)
    owner_type = db.Column(db.String(40), nullable=False, index=True)
    owner_id = db.Column(db.String(160), nullable=False, index=True)
    controller_run_id = db.Column(db.String(160), index=True)
    priority = db.Column(db.Integer, nullable=False, default=0)
    status = db.Column(db.String(24), nullable=False, default='active', index=True)
    ttl_seconds = db.Column(db.Integer, nullable=False, default=900)
    idempotency_key = db.Column(db.String(128), nullable=False)
    request_hash = db.Column(db.String(71), nullable=False)
    holder_actor_type = db.Column(db.String(24), nullable=False)
    holder_actor_id = db.Column(db.Integer, nullable=False)
    holder_actor_name = db.Column(db.String(160), default='')
    metadata_json = db.Column(db.JSON)
    acquired_at = db.Column(db.DateTime, default=_now, index=True)
    heartbeat_at = db.Column(db.DateTime, default=_now)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    released_at = db.Column(db.DateTime)
    release_reason = db.Column(db.String(255), default='')
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    __table_args__ = (
        db.UniqueConstraint(
            'owner_type', 'owner_id', 'idempotency_key', 'resource_key',
            name='uq_resource_lease_owner_idempotency_resource'),
        db.Index(
            'ix_resource_lease_owner_status',
            'owner_type', 'owner_id', 'status'),
        db.Index(
            'ix_resource_lease_status_expiry', 'status', 'expires_at'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'lease_group_id': self.lease_group_id,
            'resource_key': self.resource_key,
            'resource_type': self.resource_type,
            'owner_type': self.owner_type,
            'owner_id': self.owner_id,
            'controller_run_id': self.controller_run_id,
            'priority': self.priority,
            'status': self.status,
            'ttl_seconds': self.ttl_seconds,
            'idempotency_key': self.idempotency_key,
            'holder_actor_type': self.holder_actor_type,
            'holder_actor_id': self.holder_actor_id,
            'holder_actor_name': self.holder_actor_name or '',
            'metadata': self.metadata_json or {},
            'acquired_at': str(self.acquired_at) if self.acquired_at else None,
            'heartbeat_at': str(self.heartbeat_at) if self.heartbeat_at else None,
            'expires_at': str(self.expires_at) if self.expires_at else None,
            'released_at': str(self.released_at) if self.released_at else None,
            'release_reason': self.release_reason or '',
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }


class ResourceLeaseEvent(db.Model):
    """Append-only acquisition, heartbeat, release and expiry audit."""
    __tablename__ = 'resource_lease_events'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    lease_id = db.Column(
        db.Integer, db.ForeignKey('resource_leases.id', ondelete='CASCADE'),
        nullable=False, index=True)
    lease_group_id = db.Column(db.String(64), nullable=False, index=True)
    event_type = db.Column(db.String(32), nullable=False, index=True)
    actor_type = db.Column(db.String(24), nullable=False)
    actor_id = db.Column(db.Integer)
    actor_name = db.Column(db.String(160), default='')
    payload_json = db.Column(db.JSON)
    created_at = db.Column(db.DateTime, default=_now, index=True)

    lease = db.relationship(
        'ResourceLease', backref=db.backref(
            'events', lazy='dynamic', cascade='all, delete-orphan'))

    def to_dict(self):
        return {
            'id': self.id,
            'lease_id': self.lease_id,
            'lease_group_id': self.lease_group_id,
            'event_type': self.event_type,
            'actor_type': self.actor_type,
            'actor_id': self.actor_id,
            'actor_name': self.actor_name or '',
            'payload': self.payload_json or {},
            'created_at': str(self.created_at) if self.created_at else None,
        }


# ============== Generic entity lineage (P0-F) ==============

class EntityRelation(db.Model):
    """Project-scoped, idempotent directed edge between arbitrary Hub entities."""
    __tablename__ = 'entity_relations'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    project_id = db.Column(
        db.Integer, db.ForeignKey('projects.id'), nullable=False, index=True)
    from_type = db.Column(db.String(64), nullable=False, index=True)
    from_id = db.Column(db.String(500), nullable=False, index=True)
    relation_type = db.Column(db.String(64), nullable=False, index=True)
    to_type = db.Column(db.String(64), nullable=False, index=True)
    to_id = db.Column(db.String(500), nullable=False, index=True)
    # SHA-256 of the project and five natural-key fields avoids oversized
    # utf8mb4 composite indexes while retaining cross-process idempotency.
    relation_key = db.Column(db.String(71), nullable=False, unique=True)
    metadata_json = db.Column(db.JSON)
    created_by = db.Column(db.String(160), default='system')
    updated_by = db.Column(db.String(160), default='system')
    created_at = db.Column(db.DateTime, default=_now, index=True)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now, index=True)

    project = db.relationship('Project')

    __table_args__ = (
        db.Index(
            'ix_entity_relation_from',
            'project_id', 'from_type', 'from_id'),
        db.Index(
            'ix_entity_relation_to',
            'project_id', 'to_type', 'to_id'),
        db.Index(
            'ix_entity_relation_project_updated',
            'project_id', 'updated_at'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'project_id': self.project_id,
            'from_type': self.from_type,
            'from_id': self.from_id,
            'relation_type': self.relation_type,
            'to_type': self.to_type,
            'to_id': self.to_id,
            'metadata': self.metadata_json or {},
            'created_by': self.created_by or 'system',
            'updated_by': self.updated_by or 'system',
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }


class WorkflowRunStep(db.Model):
    """Workflow 运行步骤"""
    __tablename__ = 'workflow_run_steps'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    run_id = db.Column(db.Integer, db.ForeignKey('workflow_runs.id'),
                       nullable=False, index=True)
    step_id = db.Column(db.String(100), nullable=False)
    position = db.Column(db.Integer, default=0)
    name = db.Column(db.String(200), nullable=False)
    step_type = db.Column(db.String(30), default='worker_task')
    runner = db.Column(db.String(200), default='')
    target_claw_id = db.Column(db.Integer,
                               db.ForeignKey('openclaw_instances.id'),
                               index=True)
    target_agent = db.Column(db.String(100), default='')
    target_post = db.Column(db.String(80), default='', index=True,
                            comment='目标工位 key，如 requirement_analyst')
    status = db.Column(db.String(30), default='pending', index=True)
    depends_on_json = db.Column(db.JSON)
    step_config_json = db.Column(db.JSON)
    attempt_no = db.Column(db.Integer, default=0)
    summary = db.Column(db.Text)
    metrics_json = db.Column(db.JSON)
    evidence_json = db.Column(db.JSON)
    logs_json = db.Column(db.JSON)
    blocker_json = db.Column(db.JSON)
    gate_result_json = db.Column(db.JSON)
    contract_result_json = db.Column(db.JSON)
    dispatched_at = db.Column(db.DateTime)
    started_at = db.Column(db.DateTime)
    finished_at = db.Column(db.DateTime)
    updated_by = db.Column(db.String(100), default='')
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)
    claimed_by = db.Column(db.String(120), default='', index=True)
    claimed_at = db.Column(db.DateTime)
    claimed_claw_id = db.Column(db.Integer, index=True)
    claim_expires_at = db.Column(db.DateTime, index=True)
    claim_lease_seconds = db.Column(db.Integer, default=180)
    claim_fencing_token = db.Column(db.Integer, nullable=False, default=0)
    outputs_json = db.Column(db.JSON)
    branch_result_json = db.Column(db.JSON)
    heartbeat_at = db.Column(db.DateTime)
    heartbeat_by = db.Column(db.String(120), default='')
    heartbeat_count = db.Column(db.Integer, default=0)
    missed_heartbeat_count = db.Column(db.Integer, default=0)
    health_status = db.Column(db.String(20), default='idle')
    health_checked_at = db.Column(db.DateTime)
    progress_at = db.Column(db.DateTime)
    progress_by = db.Column(db.String(120), default='')
    progress_phase = db.Column(db.String(80), default='')
    progress_message = db.Column(db.Text)
    progress_percent = db.Column(db.Integer)
    progress_json = db.Column(db.JSON)

    run = db.relationship('WorkflowRun', backref='steps')
    target_claw = db.relationship('OpenClawInstance',
                                  backref='workflow_run_steps')

    def to_dict(self, with_context=False):
        config = self.step_config_json or {}
        data = {
            'id': self.id,
            'run_id': self.run_id,
            'step_id': self.step_id,
            'position': self.position,
            'name': self.name,
            'step_type': self.step_type,
            'type': self.step_type,
            'runner': self.runner,
            'target_claw_id': self.target_claw_id,
            'target_agent': self.target_agent,
            'target_post': self.target_post or '',
            'status': self.status,
            'display_state': _workflow_step_display_state_for_model(self.status),
            'depends_on': self.depends_on_json or [],
            'depends_on_json': self.depends_on_json or [],
            'config': config,
            'step_config_json': config,
            'prompt': config.get('prompt', '') if isinstance(config, dict) else '',
            'references': config.get('references', []) if isinstance(config, dict) else [],
            'input_vars': config.get('input_vars', {}) if isinstance(config, dict) else {},
            'declared_outputs': config.get('outputs', []) if isinstance(config, dict) else [],
            'attempt_no': self.attempt_no,
            'summary': self.summary or '',
            'metrics': self.metrics_json or {},
            'metrics_json': self.metrics_json or {},
            'evidence': self.evidence_json or {},
            'evidence_json': self.evidence_json or {},
            'logs': self.logs_json or {},
            'logs_json': self.logs_json or {},
            'blocker': self.blocker_json or {},
            'blocker_json': self.blocker_json or {},
            'gate_result': self.gate_result_json or {},
            'gate_result_json': self.gate_result_json or {},
            'contract_result': self.contract_result_json or {},
            'contract_result_json': self.contract_result_json or {},
            'result_code': (
                (self.contract_result_json or {}).get('code')
                if isinstance(self.contract_result_json, dict) else ''),
            'outputs': self.outputs_json or {},
            'outputs_json': self.outputs_json or {},
            'branch_result_json': self.branch_result_json or {},
            'dispatched_at': str(self.dispatched_at) if self.dispatched_at else None,
            'started_at': str(self.started_at) if self.started_at else None,
            'finished_at': str(self.finished_at) if self.finished_at else None,
            'updated_by': self.updated_by,
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
            'claimed_by': self.claimed_by,
            'claimed_at': str(self.claimed_at) if self.claimed_at else None,
            'claimed_claw_id': self.claimed_claw_id,
            'claim_expires_at': str(self.claim_expires_at) if self.claim_expires_at else None,
            'claim_lease_seconds': self.claim_lease_seconds or 180,
            'claim_fencing_token': self.claim_fencing_token or 0,
            'heartbeat_at': str(self.heartbeat_at) if self.heartbeat_at else None,
            'heartbeat_by': self.heartbeat_by,
            'heartbeat_count': self.heartbeat_count or 0,
            'missed_heartbeat_count': self.missed_heartbeat_count or 0,
            'health_status': self.health_status or 'idle',
            'health_checked_at': str(self.health_checked_at) if self.health_checked_at else None,
            'progress_at': str(self.progress_at) if self.progress_at else None,
            'progress_by': self.progress_by,
            'progress_phase': self.progress_phase,
            'progress_message': self.progress_message or '',
            'progress_percent': self.progress_percent,
            'progress_json': self.progress_json or {},
        }
        if with_context:
            data['task_context'] = self._build_task_context(config)
        return data

    def _build_task_context(self, config):
        try:
            from app.services.task_context import build_task_context_payload
            cfg = config if isinstance(config, dict) else {}
            title = self.name or ''
            description = cfg.get('prompt') or cfg.get('description') or ''
            claw = getattr(self, 'target_claw', None)
            project = None
            run = getattr(self, 'run', None)
            definition = getattr(run, 'definition', None) if run else None
            if definition is not None:
                project = getattr(definition, 'project_name', None)
            if not project:
                project = getattr(claw, 'project_name', None)
            return build_task_context_payload(
                title, description, project=project, claw=claw)
        except Exception:
            return {}


class WorkflowApproval(db.Model):
    """Workflow 审批记录"""
    __tablename__ = 'workflow_approvals'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    run_id = db.Column(db.Integer, db.ForeignKey('workflow_runs.id'),
                       nullable=False, index=True)
    step_id = db.Column(db.String(100), nullable=False)
    action = db.Column(db.String(50), default='approve')
    status = db.Column(db.String(20), default='pending', index=True)
    approver = db.Column(db.String(100), default='')
    comment = db.Column(db.Text)
    created_by = db.Column(db.String(100), default='')
    approved_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=_now)

    run = db.relationship('WorkflowRun', backref='approvals')

    def to_dict(self):
        return {
            'id': self.id,
            'run_id': self.run_id,
            'step_id': self.step_id,
            'action': self.action,
            'status': self.status,
            'approver': self.approver,
            'comment': self.comment,
            'created_by': self.created_by,
            'approved_at': str(self.approved_at) if self.approved_at else None,
            'created_at': str(self.created_at) if self.created_at else None,
        }


class WorkflowArtifact(db.Model):
    """Workflow 产物/证据"""
    __tablename__ = 'workflow_artifacts'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    run_id = db.Column(db.Integer, db.ForeignKey('workflow_runs.id'),
                       nullable=False, index=True)
    step_id = db.Column(db.String(100), default='')
    artifact_type = db.Column(db.String(50), default='link')
    name = db.Column(db.String(200), default='')
    url = db.Column(db.String(1000), default='')
    local_path = db.Column(db.String(1000), default='')
    test_report_id = db.Column(db.Integer, db.ForeignKey('test_reports.id'),
                               index=True)
    metadata_json = db.Column(db.JSON)
    created_at = db.Column(db.DateTime, default=_now)

    run = db.relationship('WorkflowRun', backref='artifacts')
    test_report = db.relationship('TestReport', backref='workflow_artifacts')

    def to_dict(self):
        return {
            'id': self.id,
            'run_id': self.run_id,
            'step_id': self.step_id,
            'artifact_type': self.artifact_type,
            'name': self.name,
            'url': self.url,
            'local_path': self.local_path,
            'test_report_id': self.test_report_id,
            'metadata': self.metadata_json or {},
            'metadata_json': self.metadata_json or {},
            'created_at': str(self.created_at) if self.created_at else None,
        }


class AgentArtifact(db.Model):
    """Versioned, reviewable hand-off between Agents within one project."""
    __tablename__ = 'agent_artifacts'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    project_id = db.Column(
        db.Integer, db.ForeignKey('projects.id'), nullable=False, index=True)
    mission_id = db.Column(
        db.Integer, db.ForeignKey('workflow_missions.id'),
        nullable=False, index=True)
    stage_id = db.Column(db.String(80), nullable=False)
    workflow_run_id = db.Column(
        db.Integer, db.ForeignKey('workflow_runs.id'), index=True)
    artifact_type = db.Column(db.String(80), nullable=False, index=True)
    schema_version = db.Column(db.Integer, nullable=False, default=1)
    artifact_version = db.Column(db.Integer, nullable=False, default=1)

    producer_type = db.Column(db.String(16), nullable=False)
    producer_id = db.Column(db.Integer, nullable=False)
    producer_name = db.Column(db.String(160), nullable=False, default='')
    producer_claw_id = db.Column(
        db.Integer, db.ForeignKey('openclaw_instances.id'), index=True)
    producer_role_key = db.Column(db.String(80), nullable=False, default='')
    producer_provider = db.Column(db.String(50), nullable=False, default='')
    producer_profile_version = db.Column(
        db.Integer, nullable=False, default=1)

    input_baseline_sha256 = db.Column(db.String(71), nullable=False, default='')
    content_json = db.Column(db.JSON, nullable=False)
    content_sha256 = db.Column(db.String(71), nullable=False, index=True)
    contract_validation_json = db.Column(
        db.JSON, nullable=False,
        default=lambda: {'mode': 'legacy_generic', 'valid': True, 'errors': []})
    validated_at = db.Column(db.DateTime)
    status = db.Column(
        db.String(24), nullable=False, default='draft', index=True)

    idempotency_key = db.Column(db.String(128), nullable=False)
    request_sha256 = db.Column(db.String(64), nullable=False)
    review_comment = db.Column(db.Text)
    reviewed_by_type = db.Column(db.String(16))
    reviewed_by_id = db.Column(db.Integer)
    reviewed_by_name = db.Column(db.String(160))
    submitted_at = db.Column(db.DateTime)
    reviewed_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=_now, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=_now, onupdate=_now, nullable=False)

    project = db.relationship('Project', backref='agent_artifacts')
    mission = db.relationship('WorkflowMission', backref='agent_artifacts')
    workflow_run = db.relationship('WorkflowRun', backref='agent_artifacts')
    producer_claw = db.relationship('OpenClawInstance')

    __table_args__ = (
        db.UniqueConstraint(
            'mission_id', 'stage_id', 'artifact_type', 'artifact_version',
            name='uq_agent_artifact_mission_stage_type_version'),
        db.UniqueConstraint(
            'project_id', 'producer_type', 'producer_id', 'idempotency_key',
            name='uq_agent_artifact_producer_idempotency'),
        db.Index(
            'ix_agent_artifact_mission_status_created',
            'mission_id', 'status', 'created_at'),
    )

    def to_dict(self):
        return {
            'schema': 1,
            'artifact_id': self.id,
            'id': self.id,
            'project_id': self.project_id,
            'mission_id': self.mission_id,
            'stage_id': self.stage_id,
            'run_id': self.workflow_run_id,
            'workflow_run_id': self.workflow_run_id,
            'artifact_type': self.artifact_type,
            'schema_version': self.schema_version or 1,
            'artifact_version': self.artifact_version or 1,
            'input_baseline_sha256': self.input_baseline_sha256 or '',
            'content': self.content_json or {},
            'content_sha256': self.content_sha256,
            'validation': self.contract_validation_json or {},
            'validated_at': (
                str(self.validated_at) if self.validated_at else None),
            'status': self.status,
            'producer': {
                'type': self.producer_type,
                'id': self.producer_id,
                'name': self.producer_name or '',
                'claw_id': self.producer_claw_id,
                'role_key': self.producer_role_key or '',
                'provider': self.producer_provider or '',
                'profile_version': self.producer_profile_version or 1,
            },
            'review': {
                'comment': self.review_comment or '',
                'reviewed_by_type': self.reviewed_by_type,
                'reviewed_by_id': self.reviewed_by_id,
                'reviewed_by_name': self.reviewed_by_name or '',
                'reviewed_at': (
                    str(self.reviewed_at) if self.reviewed_at else None),
            },
            'submitted_at': (
                str(self.submitted_at) if self.submitted_at else None),
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }


# ============== Automation closed-loop candidates (P0-B) ==============

class AutomationCaseCandidate(db.Model):
    """A proposed automation case that has not entered a production library."""
    __tablename__ = 'automation_case_candidates'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    project_id = db.Column(
        db.Integer, db.ForeignKey('projects.id'), nullable=False, index=True)
    title = db.Column(db.String(300), nullable=False)
    module_key = db.Column(db.String(200), default='', index=True)
    source_type = db.Column(db.String(64), default='manual', index=True)
    source_refs_json = db.Column(db.JSON)
    case_draft_json = db.Column(db.JSON)
    required_capabilities_json = db.Column(db.JSON)
    state = db.Column(db.String(40), nullable=False, default='DISCOVERED', index=True)
    qualification_outcome = db.Column(db.String(48), index=True)
    qualification_run_id = db.Column(
        db.Integer, db.ForeignKey('workflow_runs.id'), index=True)
    qualification_evidence_json = db.Column(db.JSON)
    production_library_id = db.Column(
        db.Integer, db.ForeignKey('test_case_libraries.id'), index=True)
    production_case_id = db.Column(
        db.Integer, db.ForeignKey('test_cases.id'), index=True)
    capability_gap_id = db.Column(
        db.Integer, db.ForeignKey('capability_gaps.id'), index=True)
    dedupe_key = db.Column(db.String(255), nullable=False)
    version = db.Column(db.Integer, nullable=False, default=1)
    created_by = db.Column(db.String(160), default='system')
    updated_by = db.Column(db.String(160), default='system')
    created_at = db.Column(db.DateTime, default=_now, index=True)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now, index=True)

    project = db.relationship('Project', backref='automation_case_candidates')
    qualification_run = db.relationship(
        'WorkflowRun', backref='qualified_case_candidates')
    production_library = db.relationship(
        'TestCaseLibrary', foreign_keys=[production_library_id])
    production_case = db.relationship('TestCase', foreign_keys=[production_case_id])
    capability_gap = db.relationship(
        'CapabilityGap', foreign_keys=[capability_gap_id],
        backref='case_candidates')

    __table_args__ = (
        db.UniqueConstraint(
            'project_id', 'dedupe_key',
            name='uq_automation_candidate_project_dedupe'),
        db.Index(
            'ix_automation_candidate_project_state_updated',
            'project_id', 'state', 'updated_at'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'project_id': self.project_id,
            'title': self.title,
            'module_key': self.module_key or '',
            'source_type': self.source_type or 'manual',
            'source_refs': self.source_refs_json or [],
            'case_draft': self.case_draft_json or {},
            'required_capabilities': self.required_capabilities_json or [],
            'state': self.state,
            'qualification_outcome': self.qualification_outcome,
            'qualification_run_id': self.qualification_run_id,
            'qualification_evidence': self.qualification_evidence_json or {},
            'production_library_id': self.production_library_id,
            'production_case_id': self.production_case_id,
            'capability_gap_id': self.capability_gap_id,
            'dedupe_key': self.dedupe_key,
            'version': self.version,
            'created_by': self.created_by,
            'updated_by': self.updated_by,
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }


class AutomationCaseCandidateEvent(db.Model):
    """Append-only audit history for candidate changes and qualification."""
    __tablename__ = 'automation_case_candidate_events'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    candidate_id = db.Column(
        db.Integer, db.ForeignKey('automation_case_candidates.id', ondelete='CASCADE'),
        nullable=False, index=True)
    event_type = db.Column(db.String(48), nullable=False, index=True)
    from_state = db.Column(db.String(40))
    to_state = db.Column(db.String(40))
    version_before = db.Column(db.Integer)
    version_after = db.Column(db.Integer)
    payload_json = db.Column(db.JSON)
    actor_type = db.Column(db.String(24), nullable=False)
    actor_id = db.Column(db.Integer, nullable=False)
    actor_name = db.Column(db.String(160), default='')
    request_id = db.Column(db.String(128), index=True)
    idempotency_key = db.Column(db.String(128))
    created_at = db.Column(db.DateTime, default=_now, index=True)

    candidate = db.relationship(
        'AutomationCaseCandidate', backref=db.backref(
            'events', cascade='all, delete-orphan', order_by='AutomationCaseCandidateEvent.id'))

    __table_args__ = (
        db.UniqueConstraint(
            'candidate_id', 'event_type', 'actor_type', 'actor_id',
            'idempotency_key',
            name='uq_candidate_event_actor_idempotency'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'candidate_id': self.candidate_id,
            'event_type': self.event_type,
            'from_state': self.from_state,
            'to_state': self.to_state,
            'version_before': self.version_before,
            'version_after': self.version_after,
            'payload': self.payload_json or {},
            'actor_type': self.actor_type,
            'actor_id': self.actor_id,
            'actor_name': self.actor_name or '',
            'request_id': self.request_id,
            'idempotency_key': self.idempotency_key,
            'created_at': str(self.created_at) if self.created_at else None,
        }


class AutomationCapability(db.Model):
    """Versioned project-scoped automation capability catalog entry."""
    __tablename__ = 'automation_capabilities'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    project_id = db.Column(
        db.Integer, db.ForeignKey('projects.id'), nullable=False, index=True)
    capability_key = db.Column(db.String(255), nullable=False)
    name = db.Column(db.String(300), nullable=False)
    operations_json = db.Column(db.JSON)
    observables_json = db.Column(db.JSON)
    reset_hooks_json = db.Column(db.JSON)
    platforms_json = db.Column(db.JSON)
    status = db.Column(
        db.String(32), nullable=False, default='planned', index=True)
    implementation_status = db.Column(
        db.String(32), nullable=False, default='declared', index=True)
    verification_status = db.Column(
        db.String(32), nullable=False, default='unverified', index=True)
    implementation_version = db.Column(db.String(80), default='')
    producer_claw_id = db.Column(
        db.Integer, db.ForeignKey('openclaw_instances.id'), index=True)
    release_id = db.Column(db.String(255), default='')
    source_commit = db.Column(db.String(64), default='')
    manifest_sha256 = db.Column(db.String(64), default='')
    verification_json = db.Column(db.JSON)
    health_checked_at = db.Column(db.DateTime, index=True)
    health_expires_at = db.Column(db.DateTime, index=True)
    version = db.Column(db.Integer, nullable=False, default=1)
    created_by = db.Column(db.String(160), default='system')
    updated_by = db.Column(db.String(160), default='system')
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    __table_args__ = (
        db.UniqueConstraint(
            'project_id', 'capability_key',
            name='uq_automation_capability_project_key'),
        db.Index(
            'ix_automation_capability_project_status',
            'project_id', 'status'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'project_id': self.project_id,
            'key': self.capability_key,
            'name': self.name,
            'operations': self.operations_json or [],
            'observables': self.observables_json or [],
            'reset_hooks': self.reset_hooks_json or [],
            'platforms': self.platforms_json or [],
            'status': self.status,
            'implementation_status': self.implementation_status or 'declared',
            'verification_status': self.verification_status or 'unverified',
            'implementation_version': self.implementation_version or '',
            'producer_claw_id': self.producer_claw_id,
            'release_id': self.release_id or '',
            'source_commit': self.source_commit or '',
            'manifest_sha256': self.manifest_sha256 or '',
            'verification': self.verification_json or {},
            'health_checked_at': (
                str(self.health_checked_at) if self.health_checked_at else None),
            'health_expires_at': (
                str(self.health_expires_at) if self.health_expires_at else None),
            'version': self.version,
            'created_by': self.created_by or 'system',
            'updated_by': self.updated_by or 'system',
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }


class AutomationCapabilityEvent(db.Model):
    """Append-only, idempotent capability publication and health history."""
    __tablename__ = 'automation_capability_events'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    capability_id = db.Column(
        db.Integer, db.ForeignKey('automation_capabilities.id', ondelete='CASCADE'),
        nullable=False, index=True)
    event_type = db.Column(db.String(48), nullable=False, index=True)
    from_status = db.Column(db.String(32))
    to_status = db.Column(db.String(32))
    version_before = db.Column(db.Integer)
    version_after = db.Column(db.Integer)
    payload_json = db.Column(db.JSON)
    actor_type = db.Column(db.String(24), nullable=False)
    actor_id = db.Column(db.Integer, nullable=False)
    actor_name = db.Column(db.String(160), default='')
    request_id = db.Column(db.String(128), index=True)
    idempotency_key = db.Column(db.String(128))
    request_hash = db.Column(db.String(64))
    created_at = db.Column(db.DateTime, default=_now, index=True)

    capability = db.relationship(
        'AutomationCapability', backref=db.backref(
            'events', cascade='all, delete-orphan',
            order_by='AutomationCapabilityEvent.id'))

    __table_args__ = (
        db.UniqueConstraint(
            'capability_id', 'actor_type', 'actor_id', 'idempotency_key',
            name='uq_capability_event_actor_idempotency'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'capability_id': self.capability_id,
            'event_type': self.event_type,
            'from_status': self.from_status,
            'to_status': self.to_status,
            'version_before': self.version_before,
            'version_after': self.version_after,
            'payload': self.payload_json or {},
            'actor_type': self.actor_type,
            'actor_id': self.actor_id,
            'actor_name': self.actor_name or '',
            'request_id': self.request_id,
            'idempotency_key': self.idempotency_key,
            'created_at': str(self.created_at) if self.created_at else None,
        }


class CapabilityGap(db.Model):
    """Missing automation capability tracked through delivery and requeue."""
    __tablename__ = 'capability_gaps'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    project_id = db.Column(
        db.Integer, db.ForeignKey('projects.id'), nullable=False, index=True)
    gap_key = db.Column(db.String(255), nullable=False)
    title = db.Column(db.String(300), nullable=False)
    missing_capabilities_json = db.Column(db.JSON)
    required_operations_json = db.Column(db.JSON)
    required_observables_json = db.Column(db.JSON)
    required_reset_hooks_json = db.Column(db.JSON)
    evidence_json = db.Column(db.JSON)
    status = db.Column(db.String(32), nullable=False, default='open', index=True)
    candidate_requeue_pending = db.Column(db.Boolean, default=False, index=True)
    owner = db.Column(db.String(160), default='', index=True)
    development_requirement_ref = db.Column(db.String(500), default='', index=True)
    development_requirement_url = db.Column(db.String(1000), default='')
    development_requirement_status = db.Column(db.String(64), default='')
    resolution_json = db.Column(db.JSON)
    resolved_by = db.Column(db.String(160), default='')
    resolved_at = db.Column(db.DateTime)
    version = db.Column(db.Integer, nullable=False, default=1)
    created_by = db.Column(db.String(160), default='system')
    updated_by = db.Column(db.String(160), default='system')
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    project = db.relationship('Project', backref='capability_gaps')

    __table_args__ = (
        db.UniqueConstraint(
            'project_id', 'gap_key', name='uq_capability_gap_project_key'),
        db.Index(
            'ix_capability_gap_project_status', 'project_id', 'status'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'project_id': self.project_id,
            'gap_key': self.gap_key,
            'title': self.title,
            'missing_capabilities': self.missing_capabilities_json or [],
            'required_operations': self.required_operations_json or [],
            'required_observables': self.required_observables_json or [],
            'required_reset_hooks': self.required_reset_hooks_json or [],
            'evidence': self.evidence_json or {},
            'status': self.status,
            'candidate_requeue_pending': bool(self.candidate_requeue_pending),
            'owner': self.owner or '',
            'development_requirement': {
                'ref': self.development_requirement_ref or '',
                'url': self.development_requirement_url or '',
                'status': self.development_requirement_status or '',
            },
            'resolution': self.resolution_json or {},
            'resolved_by': self.resolved_by or '',
            'resolved_at': str(self.resolved_at) if self.resolved_at else None,
            'version': self.version,
            'created_by': self.created_by,
            'updated_by': self.updated_by,
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }


class CapabilityGapEvent(db.Model):
    """Append-only Capability Gap lifecycle and candidate requeue audit."""
    __tablename__ = 'capability_gap_events'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    capability_gap_id = db.Column(
        db.Integer, db.ForeignKey('capability_gaps.id', ondelete='CASCADE'),
        nullable=False, index=True)
    event_type = db.Column(db.String(48), nullable=False, index=True)
    from_status = db.Column(db.String(32))
    to_status = db.Column(db.String(32))
    version_before = db.Column(db.Integer)
    version_after = db.Column(db.Integer)
    payload_json = db.Column(db.JSON)
    actor_type = db.Column(db.String(24), nullable=False)
    actor_id = db.Column(db.Integer, nullable=False)
    actor_name = db.Column(db.String(160), default='')
    request_id = db.Column(db.String(128), index=True)
    idempotency_key = db.Column(db.String(128))
    request_hash = db.Column(db.String(64))
    created_at = db.Column(db.DateTime, default=_now, index=True)

    capability_gap = db.relationship(
        'CapabilityGap', backref=db.backref(
            'events', cascade='all, delete-orphan',
            order_by='CapabilityGapEvent.id'))

    __table_args__ = (
        db.UniqueConstraint(
            'capability_gap_id', 'event_type', 'actor_type', 'actor_id',
            'idempotency_key', name='uq_gap_event_actor_idempotency'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'capability_gap_id': self.capability_gap_id,
            'event_type': self.event_type,
            'from_status': self.from_status,
            'to_status': self.to_status,
            'version_before': self.version_before,
            'version_after': self.version_after,
            'payload': self.payload_json or {},
            'actor_type': self.actor_type,
            'actor_id': self.actor_id,
            'actor_name': self.actor_name or '',
            'request_id': self.request_id,
            'idempotency_key': self.idempotency_key,
            'created_at': str(self.created_at) if self.created_at else None,
        }


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
    visibility = db.Column(db.String(20), default='public',
                           comment='可见性：public=公开（默认），private=私有（仅作者可见）')
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

    # 镜像内容：非管理员修改时写入镜像，审核通过后替换原内容
    mirror_content = db.Column(db.Text, comment='镜像内容模板（待审核的修改内容）')
    mirror_updated_by = db.Column(db.String(100), comment='镜像内容修改人')
    mirror_updated_at = db.Column(db.DateTime, comment='镜像内容修改时间')
    # 修改历史：最近10条修改记录
    content_history = db.Column(db.JSON, comment='最近10次修改记录 [{content_template, updated_by, updated_at, summary}]')

    # Rule 最后修改人（不论镜像/直改/审核通过都会写入）
    last_modified_by = db.Column(db.String(100), comment='最后修改人（user.username 或 OpenClaw 名）')
    last_modified_at = db.Column(db.DateTime, comment='最后修改时间')
    last_modified_source = db.Column(db.Enum('web', 'openclaw', 'system'),
                                     default='web',
                                     comment='修改来源：web/openclaw/system')

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
            'mirror_content': self.mirror_content or None,
            'mirror_updated_by': self.mirror_updated_by or None,
            'mirror_updated_at': str(self.mirror_updated_at) if self.mirror_updated_at else None,
            'content_history': self.content_history or [],
            # rule last_modified_by emit
            'last_modified_by': self.last_modified_by or None,
            'last_modified_at': str(self.last_modified_at) if self.last_modified_at else None,
            'last_modified_source': self.last_modified_source or 'web',
            'visibility': self.visibility or 'public',
            'owner_claw_id': self.owner_claw_id,
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
    # 评审状态机：draft=草稿(未评审) / pending_review=待评审(已发起) /
    #            approved=已通过 / rejected=被驳回（仍可继续编辑、再次发起）
    review_status = db.Column(db.String(20), default='draft',
                              comment='评审状态：draft/pending_review/approved/rejected')
    # 当前进行中的评审记录 ID（pending_review 时有值）
    current_review_id = db.Column(db.Integer,
                                  comment='当前进行中的 TestCaseLibraryReview ID')
    review_status_at = db.Column(db.DateTime,
                                 comment='review_status 最近一次变更时间')
    created_by = db.Column(db.String(100), default='',
                           comment='创建人 username/claw_name')
    updated_by = db.Column(db.String(100), default='',
                           comment='最后修改人 username/claw_name')
    # Formal immutable revision chain used by automation candidate promotion.
    revision = db.Column(db.Integer, nullable=False, default=0)
    content_hash = db.Column(db.String(71), nullable=False, default='')

    # 脑图结构（JSON格式）
    mindmap = db.Column(db.JSON, comment='脑图结构')

    cases = db.relationship('TestCase', backref='library',
                           lazy='dynamic', cascade='all, delete-orphan')

    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now,
                          onupdate=_now)

    def to_dict(self, with_cases=False, with_mindmap=True):
        data = {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'project_name': self.project_name,
            'module_name': self.module_name,
            'owner': self.owner,
            'status': self.status,
            'review_status': self.review_status or 'draft',
            'current_review_id': self.current_review_id,
            'review_status_at': (str(self.review_status_at)
                                 if self.review_status_at else None),
            'created_by': self.created_by or self.owner or '',
            'updated_by': self.updated_by or '',
            'revision': int(self.revision or 0),
            'content_hash': self.content_hash or '',
            'case_count': self.cases.count(),
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }
        if with_mindmap:
            data['mindmap'] = self.mindmap
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
    updated_by = db.Column(db.String(100), default='', comment='最后修改人')

    # TAPD 需求绑定
    tapd_story_url = db.Column(db.String(500), comment='TAPD 需求链接')
    tapd_story_title = db.Column(db.String(255), comment='TAPD 需求标题（冗余，方便展示）')

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
            'updated_by': self.updated_by or '',
            'tapd_story_url': self.tapd_story_url or '',
            'tapd_story_title': self.tapd_story_title or '',
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }


# ============== Formal testcase-library revisions (P0-C) ==============

class TestCaseLibraryRevision(db.Model):
    """Append-only, full-content snapshot of one production library revision."""
    __tablename__ = 'testcase_library_revisions'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    library_id = db.Column(
        db.Integer, db.ForeignKey('test_case_libraries.id', ondelete='CASCADE'),
        nullable=False, index=True)
    revision = db.Column(db.Integer, nullable=False)
    content_hash = db.Column(db.String(71), nullable=False, index=True)
    case_count = db.Column(db.Integer, nullable=False, default=0)
    snapshot_json = db.Column(db.JSON, nullable=False)
    source_type = db.Column(db.String(32), nullable=False, default='baseline')
    source_reference = db.Column(db.String(160), default='')
    rollback_from_revision = db.Column(db.Integer)
    message = db.Column(db.String(500), default='')
    created_by = db.Column(db.String(160), default='system')
    created_at = db.Column(db.DateTime, default=_now, index=True)

    library = db.relationship(
        'TestCaseLibrary', backref=db.backref(
            'formal_revisions', lazy='dynamic', cascade='all, delete-orphan'))

    __table_args__ = (
        db.UniqueConstraint(
            'library_id', 'revision', name='uq_testcase_library_revision'),
        db.Index(
            'ix_testcase_library_revision_created',
            'library_id', 'created_at'),
    )

    def to_dict(self, with_snapshot=False):
        data = {
            'id': self.id,
            'library_id': self.library_id,
            'revision': self.revision,
            'content_hash': self.content_hash,
            'case_count': self.case_count,
            'source_type': self.source_type,
            'source_reference': self.source_reference or '',
            'rollback_from_revision': self.rollback_from_revision,
            'message': self.message or '',
            'created_by': self.created_by or 'system',
            'created_at': str(self.created_at) if self.created_at else None,
        }
        if with_snapshot:
            data['snapshot'] = self.snapshot_json or {}
        return data


class TestCaseLibraryPromotion(db.Model):
    """Idempotency and audit record for promotion and rollback mutations."""
    __tablename__ = 'testcase_library_promotions'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    library_id = db.Column(
        db.Integer, db.ForeignKey('test_case_libraries.id', ondelete='CASCADE'),
        nullable=False, index=True)
    operation_type = db.Column(
        db.String(24), nullable=False, default='promotion', index=True)
    idempotency_key = db.Column(db.String(128), nullable=False)
    request_hash = db.Column(db.String(71), nullable=False)
    before_revision = db.Column(db.Integer, nullable=False)
    after_revision = db.Column(db.Integer, nullable=False)
    content_hash = db.Column(db.String(71), nullable=False)
    rollback_from_revision = db.Column(db.Integer)
    candidate_ids_json = db.Column(db.JSON)
    created_case_ids_json = db.Column(db.JSON)
    updated_case_ids_json = db.Column(db.JSON)
    source_run_ids_json = db.Column(db.JSON)
    message = db.Column(db.String(500), default='')
    response_json = db.Column(db.JSON, nullable=False)
    created_by = db.Column(db.String(160), default='system')
    created_at = db.Column(db.DateTime, default=_now, index=True)

    library = db.relationship(
        'TestCaseLibrary', backref=db.backref(
            'formal_promotions', lazy='dynamic', cascade='all, delete-orphan'))

    __table_args__ = (
        db.UniqueConstraint(
            'library_id', 'idempotency_key',
            name='uq_testcase_library_promotion_idempotency'),
        db.Index(
            'ix_testcase_library_promotion_created',
            'library_id', 'created_at'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'library_id': self.library_id,
            'operation_type': self.operation_type,
            'idempotency_key': self.idempotency_key,
            'before_revision': self.before_revision,
            'after_revision': self.after_revision,
            'content_hash': self.content_hash,
            'rollback_from_revision': self.rollback_from_revision,
            'candidate_ids': self.candidate_ids_json or [],
            'created_case_ids': self.created_case_ids_json or [],
            'updated_case_ids': self.updated_case_ids_json or [],
            'source_run_ids': self.source_run_ids_json or [],
            'message': self.message or '',
            'created_by': self.created_by or 'system',
            'created_at': str(self.created_at) if self.created_at else None,
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


class TestCasePanoramaLink(db.Model):
    """用例库/目录/用例 与功能全景模块的正式关联。"""
    __tablename__ = 'testcase_panorama_links'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=True, index=True)
    workspace_id = db.Column(db.Integer, db.ForeignKey('panorama_workspaces.id'), nullable=True, index=True)
    module_id = db.Column(db.Integer, db.ForeignKey('game_module_panorama.id'), nullable=False, index=True)
    library_id = db.Column(db.Integer, db.ForeignKey('test_case_libraries.id'), nullable=False, index=True)
    module_path = db.Column(db.String(500), default='', comment='用例目录路径；库级关联为空')
    case_pk = db.Column(db.Integer, db.ForeignKey('test_cases.id'), nullable=True, index=True)
    link_level = db.Column(db.String(20), default='library',
                           comment='library/directory/case')
    case_count = db.Column(db.Integer, default=0, comment='当前关联范围内用例数缓存')
    source = db.Column(db.String(50), default='manual',
                       comment='manual/agent/migration')
    confidence = db.Column(db.Float, default=1.0)
    created_by = db.Column(db.String(120))
    updated_by = db.Column(db.String(120))
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)
    last_verified_at = db.Column(db.DateTime)

    module = db.relationship('GameModulePanorama', backref='testcase_links')
    library = db.relationship('TestCaseLibrary', backref='panorama_links')
    case = db.relationship('TestCase')

    __table_args__ = (
        db.UniqueConstraint('workspace_id', 'module_id', 'library_id',
                            'module_path', 'case_pk', 'link_level',
                            name='uq_testcase_panorama_link'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'project_id': self.project_id,
            'workspace_id': self.workspace_id,
            'module_id': self.module_id,
            'module_name': self.module.name if self.module else None,
            'module_path': self.module_path or '',
            'panorama_module_path': self.module.path if self.module else None,
            'library_id': self.library_id,
            'library_name': self.library.name if self.library else None,
            'case_pk': self.case_pk,
            'link_level': self.link_level or 'library',
            'case_count': self.case_count or 0,
            'source': self.source or 'manual',
            'confidence': self.confidence if self.confidence is not None else 1.0,
            'created_by': self.created_by or '',
            'updated_by': self.updated_by or '',
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
            'last_verified_at': str(self.last_verified_at) if self.last_verified_at else None,
        }


class PanoramaModuleTestMetric(db.Model):
    """功能全景模块的测试覆盖与 bug 风险聚合指标。"""
    __tablename__ = 'panorama_module_test_metrics'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    module_id = db.Column(db.Integer, db.ForeignKey('game_module_panorama.id'),
                          nullable=False, unique=True, index=True)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=True, index=True)
    workspace_id = db.Column(db.Integer, db.ForeignKey('panorama_workspaces.id'), nullable=True, index=True)
    direct_case_count = db.Column(db.Integer, default=0)
    subtree_case_count = db.Column(db.Integer, default=0)
    linked_library_count = db.Column(db.Integer, default=0)
    linked_directory_count = db.Column(db.Integer, default=0)
    bug_count = db.Column(db.Integer, default=0)
    bug_risk_score = db.Column(db.Integer, default=0)
    bug_risk_level = db.Column(db.String(20), default='low',
                               comment='low/normal/high/critical')
    metrics_payload = db.Column(db.JSON)
    source = db.Column(db.String(50), default='sync')
    updated_by = db.Column(db.String(120))
    synced_at = db.Column(db.DateTime, default=_now)
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    module = db.relationship('GameModulePanorama', backref=db.backref('test_metric', uselist=False))

    def to_dict(self):
        return {
            'id': self.id,
            'module_id': self.module_id,
            'project_id': self.project_id,
            'workspace_id': self.workspace_id,
            'direct_case_count': self.direct_case_count or 0,
            'subtree_case_count': self.subtree_case_count or 0,
            'linked_library_count': self.linked_library_count or 0,
            'linked_directory_count': self.linked_directory_count or 0,
            'bug_count': self.bug_count or 0,
            'bug_risk_score': self.bug_risk_score or 0,
            'bug_risk_level': self.bug_risk_level or 'low',
            'metrics_payload': self.metrics_payload or {},
            'source': self.source or 'sync',
            'updated_by': self.updated_by or '',
            'synced_at': str(self.synced_at) if self.synced_at else None,
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }


class TestCaseChangeLog(db.Model):
    """用例增量变更日志，用于按时间段快速评审/测试。"""
    __tablename__ = 'test_case_change_logs'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    library_id = db.Column(db.Integer, db.ForeignKey('test_case_libraries.id'),
                           nullable=False, index=True)
    case_pk = db.Column(db.Integer, index=True)
    case_id = db.Column(db.String(100), default='')
    case_title = db.Column(db.String(255), default='')
    module_path = db.Column(db.String(500), default='', index=True)
    change_type = db.Column(db.String(30), nullable=False,
                            comment='created/updated/deleted/moved/batch_created/batch_deleted')
    changed_fields = db.Column(db.JSON)
    old_snapshot = db.Column(db.JSON)
    new_snapshot = db.Column(db.JSON)
    operation_id = db.Column(db.String(80), default='', index=True)
    linked_panorama_modules = db.Column(db.JSON)
    changed_by = db.Column(db.String(120), default='')
    changed_at = db.Column(db.DateTime, default=_now, index=True)
    source = db.Column(db.String(50), default='web')

    library = db.relationship('TestCaseLibrary', backref='change_logs')

    def to_dict(self):
        return {
            'id': self.id,
            'library_id': self.library_id,
            'case_pk': self.case_pk,
            'case_id': self.case_id or '',
            'case_title': self.case_title or '',
            'module_path': self.module_path or '',
            'change_type': self.change_type,
            'changed_fields': self.changed_fields or [],
            'old_snapshot': self.old_snapshot or {},
            'new_snapshot': self.new_snapshot or {},
            'operation_id': self.operation_id or '',
            'linked_panorama_modules': self.linked_panorama_modules or [],
            'changed_by': self.changed_by or '',
            'changed_at': str(self.changed_at) if self.changed_at else None,
            'source': self.source or 'web',
        }


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
                       nullable=False, comment='关联的 OpenClaw ID（接收方）')
    from_claw_id = db.Column(db.Integer, db.ForeignKey('openclaw_instances.id'),
                            nullable=True, comment='发送方 OpenClaw ID（claw→claw / claw→hub 时填写）')
    sender_name = db.Column(db.String(50), default='Web Admin',
                          comment='发送者名称')
    content = db.Column(db.Text, nullable=False, comment='消息内容')
    msg_type = db.Column(db.String(30), default='text',
                        comment='消息类型：text/task_delegate/knowledge_share/request_help')
    direction = db.Column(db.String(10), default='to_claw',
                         comment='方向：to_claw=Web发给OpenClaw, from_claw=OpenClaw发给Web')
    reply_to = db.Column(db.Integer, comment='回复的消息ID')
    status = db.Column(db.String(20), default='pending',
                      comment='状态：pending/delivered/processing/done/failed/read')
    delivered_at = db.Column(db.DateTime, comment='送达时间')
    read_at = db.Column(db.DateTime, comment='读取时间')
    processing_at = db.Column(db.DateTime, comment='LLM 开始处理时间（B+ 状态机）')
    done_at = db.Column(db.DateTime, comment='LLM 处理完成时间（B+ 状态机）')
    failed_reason = db.Column(db.Text, comment='处理失败原因（B+ 状态机）')
    llm_response = db.Column(db.Text, comment='LLM 处理后的回复内容（可选）')
    created_at = db.Column(db.DateTime, default=_now)

    # 关联到 OpenClaw（接收方）
    claw = db.relationship('OpenClawInstance', foreign_keys=[claw_id], backref='messages')
    # 关联到发送方 OpenClaw
    from_claw = db.relationship('OpenClawInstance', foreign_keys=[from_claw_id], backref='sent_messages')

    def _infer_urgency(self):
        """根据 msg_type 推断 urgency（DB 没存就推断）

        规则：
          - request_help / task_delegate          → interrupt（要求 AI 立刻处理）
          - text                                   → flexible（可下回合处理）
          - knowledge_share / system / 其他      → background（默默归档即可）

        如果将来在 ClawMessage 上加 urgency 列，这里优先返回它即可。
        """
        explicit = getattr(self, 'urgency_level', None)
        if explicit:
            return explicit
        mt = (self.msg_type or 'text').lower()
        if mt in ('request_help', 'task_delegate'):
            return 'interrupt'
        if mt in ('knowledge_share', 'system'):
            return 'background'
        return 'flexible'

    def to_dict(self):
        return {
            'id': self.id,
            'claw_id': self.claw_id,
            'from_claw_id': self.from_claw_id,
            'sender_name': self.sender_name,
            'content': self.content,
            'msg_type': self.msg_type,
            'direction': self.direction or 'to_claw',
            'reply_to': self.reply_to,
            'status': self.status,
            'urgency': self._infer_urgency(),
            'delivered_at': str(self.delivered_at) if self.delivered_at else None,
            'read_at': str(self.read_at) if self.read_at else None,
            'processing_at': str(self.processing_at) if self.processing_at else None,
            'done_at': str(self.done_at) if self.done_at else None,
            'failed_reason': self.failed_reason,
            'created_at': str(self.created_at) if self.created_at else None,
        }


# ============== 主题聊天室模型 ==============

class ChatRoom(db.Model):
    """多人主题聊天室。与 ClawMessage 点对点链路完全隔离。"""
    __tablename__ = 'chat_rooms'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    team_id = db.Column(
        db.Integer, db.ForeignKey('agent_teams.id'), nullable=True,
        unique=True, index=True)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=True, index=True)
    title = db.Column(db.String(160), nullable=False)
    description = db.Column(db.Text)
    status = db.Column(db.String(20), nullable=False, default='active', index=True)
    agent_policy = db.Column(db.String(30), nullable=False, default='mention_only')
    created_by_type = db.Column(db.String(20), nullable=False)
    created_by_id = db.Column(db.Integer, nullable=False)
    owner_member_id = db.Column(db.Integer, nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=_now, nullable=False)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now, nullable=False)
    deleted_at = db.Column(db.DateTime)

    members = db.relationship(
        'ChatRoomMember', backref='room', lazy='dynamic',
        cascade='all, delete-orphan', foreign_keys='ChatRoomMember.room_id')
    messages = db.relationship(
        'ChatRoomMessage', backref='room', lazy='dynamic',
        cascade='all, delete-orphan')

    def to_dict(self):
        return {
            'id': self.id,
            'team_id': self.team_id,
            'project_id': self.project_id,
            'title': self.title,
            'description': self.description or '',
            'status': self.status,
            'agent_policy': self.agent_policy or 'mention_only',
            'owner_member_id': self.owner_member_id,
            'created_by': {'type': self.created_by_type, 'id': self.created_by_id},
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }


class ChatRoomGuestIdentity(db.Model):
    """外部成员的稳定身份；短期访问 Token 到期后身份与历史仍保留。"""
    __tablename__ = 'chat_room_guest_identities'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    display_name = db.Column(db.String(80), nullable=False)
    resume_secret_hash = db.Column(db.String(64), nullable=False)
    status = db.Column(db.String(20), nullable=False, default='active')
    created_at = db.Column(db.DateTime, default=_now, nullable=False)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now, nullable=False)


class ChatRoomMember(db.Model):
    """聊天室成员，member_id 是消息归属的稳定主体。"""
    __tablename__ = 'chat_room_members'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    room_id = db.Column(db.Integer, db.ForeignKey('chat_rooms.id'), nullable=False, index=True)
    member_type = db.Column(db.String(20), nullable=False)  # user / agent / guest
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    claw_id = db.Column(db.Integer, db.ForeignKey('openclaw_instances.id'), nullable=True, index=True)
    guest_identity_id = db.Column(
        db.Integer, db.ForeignKey('chat_room_guest_identities.id'), nullable=True, index=True)
    display_name = db.Column(db.String(80), nullable=False)
    role = db.Column(db.String(20), nullable=False, default='member')  # owner/admin/member
    status = db.Column(db.String(20), nullable=False, default='active', index=True)
    last_read_message_id = db.Column(db.Integer, nullable=True)
    invitation_notified_at = db.Column(db.DateTime)
    last_event_id = db.Column(db.Integer, nullable=True)
    joined_at = db.Column(db.DateTime, default=_now, nullable=False)
    removed_at = db.Column(db.DateTime)

    guest_identity = db.relationship('ChatRoomGuestIdentity')

    __table_args__ = (
        db.UniqueConstraint('room_id', 'user_id', name='uq_chat_room_member_user'),
        db.UniqueConstraint('room_id', 'claw_id', name='uq_chat_room_member_claw'),
        db.UniqueConstraint('room_id', 'guest_identity_id', name='uq_chat_room_member_guest'),
        db.Index('ix_chat_room_member_room_status', 'room_id', 'status'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'room_id': self.room_id,
            'member_type': self.member_type,
            'user_id': self.user_id,
            'claw_id': self.claw_id,
            'display_name': self.display_name,
            'role': self.role,
            'status': self.status,
            'last_read_message_id': self.last_read_message_id,
            'joined_at': str(self.joined_at) if self.joined_at else None,
        }


class ChatRoomMessage(db.Model):
    __tablename__ = 'chat_room_messages'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    room_id = db.Column(db.Integer, db.ForeignKey('chat_rooms.id'), nullable=False, index=True)
    sender_member_id = db.Column(
        db.Integer, db.ForeignKey('chat_room_members.id'), nullable=False, index=True)
    client_message_id = db.Column(db.String(100), nullable=False)
    message_type = db.Column(db.String(20), nullable=False, default='text')
    content = db.Column(db.Text, nullable=False)
    reply_to_message_id = db.Column(db.Integer, nullable=True)
    origin_delivery_id = db.Column(db.Integer, nullable=True, index=True)
    automation_depth = db.Column(db.Integer, nullable=False, default=0)
    status = db.Column(db.String(20), nullable=False, default='active')
    created_at = db.Column(db.DateTime, default=_now, nullable=False, index=True)
    edited_at = db.Column(db.DateTime)
    deleted_at = db.Column(db.DateTime)

    sender = db.relationship('ChatRoomMember', foreign_keys=[sender_member_id])
    __table_args__ = (
        db.UniqueConstraint(
            'room_id', 'sender_member_id', 'client_message_id',
            name='uq_chat_room_message_idempotency'),
        db.Index('ix_chat_room_message_room_id', 'room_id', 'id'),
    )

    def to_dict(self, mentions=None):
        return {
            'id': self.id,
            'room_id': self.room_id,
            'sender_member_id': self.sender_member_id,
            'sender': self.sender.to_dict() if self.sender else None,
            'client_message_id': self.client_message_id,
            'message_type': self.message_type,
            'content': self.content,
            'reply_to_message_id': self.reply_to_message_id,
            'origin_delivery_id': self.origin_delivery_id,
            'automation_depth': int(self.automation_depth or 0),
            'mentions': mentions or [],
            'created_at': str(self.created_at) if self.created_at else None,
        }


class ChatRoomImage(db.Model):
    """Private image attachment owned by exactly one room message."""
    __tablename__ = 'chat_room_images'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    room_id = db.Column(
        db.Integer, db.ForeignKey('chat_rooms.id', ondelete='CASCADE'),
        nullable=False, index=True)
    message_id = db.Column(
        db.Integer, db.ForeignKey('chat_room_messages.id', ondelete='CASCADE'),
        nullable=True, index=True)
    uploaded_by_member_id = db.Column(
        db.Integer, db.ForeignKey('chat_room_members.id'), nullable=False, index=True)
    client_upload_id = db.Column(db.String(100), nullable=True)
    original_name = db.Column(db.String(255), nullable=False)
    content_type = db.Column(db.String(50), nullable=False)
    file_size = db.Column(db.Integer, nullable=False)
    sha256 = db.Column(db.String(64), nullable=False)
    storage_key = db.Column(db.String(180), nullable=False, unique=True)
    status = db.Column(db.String(20), nullable=False, default='pending', index=True)
    created_at = db.Column(db.DateTime, default=_now, nullable=False)
    attached_at = db.Column(db.DateTime)

    __table_args__ = (
        db.UniqueConstraint(
            'room_id', 'uploaded_by_member_id', 'client_upload_id',
            name='uq_chat_room_image_idempotency'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'room_id': self.room_id,
            'message_id': self.message_id,
            'name': self.original_name,
            'content_type': self.content_type,
            'size': self.file_size,
            'sha256': self.sha256,
            'status': self.status,
            'url': '/api/v1/chat-rooms/%s/images/%s' % (self.room_id, self.id),
        }


class ChatRoomEvent(db.Model):
    """供 Agent SSE 消费的房间级可信事件。"""
    __tablename__ = 'chat_room_events'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    room_id = db.Column(db.Integer, db.ForeignKey('chat_rooms.id'), nullable=False, index=True)
    event_type = db.Column(db.String(40), nullable=False, index=True)
    actor_member_id = db.Column(db.Integer, nullable=True)
    payload_json = db.Column(db.JSON)
    created_at = db.Column(db.DateTime, default=_now, nullable=False)


class ChatRoomMention(db.Model):
    __tablename__ = 'chat_room_mentions'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    message_id = db.Column(
        db.Integer, db.ForeignKey('chat_room_messages.id'), nullable=False, index=True)
    mention_type = db.Column(db.String(20), nullable=False)  # member / all
    member_id = db.Column(db.Integer, db.ForeignKey('chat_room_members.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=_now, nullable=False)


class ChatRoomDelivery(db.Model):
    __tablename__ = 'chat_room_deliveries'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    room_id = db.Column(db.Integer, db.ForeignKey('chat_rooms.id'), nullable=False, index=True)
    message_id = db.Column(
        db.Integer, db.ForeignKey('chat_room_messages.id'), nullable=False, index=True)
    member_id = db.Column(
        db.Integer, db.ForeignKey('chat_room_members.id'), nullable=False, index=True)
    status = db.Column(db.String(20), nullable=False, default='unread', index=True)
    notify_agent = db.Column(db.Boolean, nullable=False, default=False, index=True)
    delivered_at = db.Column(db.DateTime)
    read_at = db.Column(db.DateTime)
    processing_at = db.Column(db.DateTime)
    done_at = db.Column(db.DateTime)
    failed_reason = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=_now, nullable=False)

    __table_args__ = (
        db.UniqueConstraint('message_id', 'member_id', name='uq_chat_room_delivery_member'),
        db.Index('ix_chat_room_delivery_member_status', 'member_id', 'status'),
    )


class AgentTeamChatRound(db.Model):
    """One manager question and the exact Agent replies it is waiting for."""
    __tablename__ = 'agent_team_chat_rounds'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    team_id = db.Column(
        db.Integer, db.ForeignKey('agent_teams.id', ondelete='CASCADE'),
        nullable=False, index=True)
    room_id = db.Column(
        db.Integer, db.ForeignKey('chat_rooms.id', ondelete='CASCADE'),
        nullable=False, index=True)
    question_message_id = db.Column(
        db.Integer, db.ForeignKey('chat_room_messages.id', ondelete='CASCADE'),
        nullable=False, unique=True, index=True)
    created_by_member_id = db.Column(
        db.Integer, db.ForeignKey('chat_room_members.id'), nullable=False)
    expected_claw_ids_json = db.Column(db.JSON, nullable=False)
    status = db.Column(db.String(20), nullable=False, default='open', index=True)
    deadline_at = db.Column(db.DateTime, nullable=False, index=True)
    completed_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=_now, nullable=False)

    __table_args__ = (
        db.Index('ix_agent_team_chat_round_team_status', 'team_id', 'status'),
    )


class ChatRoomInvite(db.Model):
    __tablename__ = 'chat_room_invites'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    room_id = db.Column(db.Integer, db.ForeignKey('chat_rooms.id'), nullable=False, index=True)
    token_hash = db.Column(db.String(64), nullable=False, unique=True)
    label = db.Column(db.String(100))
    status = db.Column(db.String(20), nullable=False, default='active')
    max_joins = db.Column(db.Integer, nullable=True)
    join_count = db.Column(db.Integer, nullable=False, default=0)
    expires_at = db.Column(db.DateTime)
    created_by_member_id = db.Column(
        db.Integer, db.ForeignKey('chat_room_members.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=_now, nullable=False)
    revoked_at = db.Column(db.DateTime)


class ChatRoomGuestSession(db.Model):
    __tablename__ = 'chat_room_guest_sessions'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    room_id = db.Column(db.Integer, db.ForeignKey('chat_rooms.id'), nullable=False, index=True)
    member_id = db.Column(db.Integer, db.ForeignKey('chat_room_members.id'), nullable=False, index=True)
    access_token_hash = db.Column(db.String(64), nullable=False, unique=True)
    status = db.Column(db.String(20), nullable=False, default='active', index=True)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    last_activity_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=_now, nullable=False)
    revoked_at = db.Column(db.DateTime)


class ChatRoomAudit(db.Model):
    __tablename__ = 'chat_room_audits'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    room_id = db.Column(db.Integer, nullable=True, index=True)
    actor_member_id = db.Column(db.Integer, nullable=True)
    action = db.Column(db.String(50), nullable=False, index=True)
    target_type = db.Column(db.String(30))
    target_id = db.Column(db.Integer)
    detail_json = db.Column(db.JSON)
    created_at = db.Column(db.DateTime, default=_now, nullable=False)


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
                              comment='类别：routine/init/onboard/test_task')
    verification_target = db.Column(db.String(200),
                                    comment='验证目标（init 任务用）')
    ref_task_id = db.Column(db.Integer,
                            comment='关联的测试任务ID（task_category=test_task 时使用）')
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
            'ref_task_id': self.ref_task_id,
            'enabled': self.enabled,
            'created_by': self.created_by,
            'created_at': str(self.created_at) if self.created_at else None,
        }
        if with_today_status:
            from datetime import date as d, datetime as dt, time as t, timedelta
            from app.services.todo_schedule import (
                cst_now_naive,
                todo_schedule_state,
            )
            now_cst = cst_now_naive()
            today = now_cst.date()
            today_log = ClawTodoLog.query.filter_by(
                todo_id=self.id, log_date=today
            ).first()

            # ============ today_status / today_completed_at ============
            effective_log = today_log
            if today_log:
                data['today_completed_at'] = str(today_log.completed_at) if today_log.completed_at else None
            else:
                # 跨天兜底：仅 once 类型保留"昨天 submitted 维持待审核"语义，
                # 避免一次性任务跨过 0 点后审核入口消失。
                # daily/weekly/monthly 是周期任务，每天必须重新算 pending，
                # 不能被昨天的 submitted log 粘住（否则今天的 daily 会从今日队列消失）。
                if (self.schedule_type or 'daily') == 'once':
                    latest_log = (ClawTodoLog.query
                                  .filter_by(todo_id=self.id)
                                  .order_by(ClawTodoLog.log_date.desc(),
                                            ClawTodoLog.created_at.desc())
                                  .first())
                    if latest_log and latest_log.status == 'submitted':
                        effective_log = latest_log
                        data['today_completed_at'] = str(latest_log.completed_at) if latest_log.completed_at else None
                    else:
                        data['today_completed_at'] = None
                else:
                    data['today_completed_at'] = None

            data.update(todo_schedule_state(
                self, today_log=effective_log, now=now_cst))

            # ============ cycle_started_at / created_at 覆盖 ============
            # 解决「周期任务模板 created_at 是几个月前，OpenClaw 误判为旧任务、
            # 不优先处理」的问题。OpenClaw 拉今日待办时，应当看到的是「本期实例」
            # 的开始时间，而不是模板创建时间。
            #
            # 注：此覆盖仅在 with_today_status=True 时发生（即 OpenClaw 视角），
            # 管理页面调用 to_dict() 默认不带该参数，因此模板真实 created_at 不受影响。
            stype = (self.schedule_type or 'daily').lower()
            cycle_start = None
            if stype == 'once':
                # 一次性任务：本期就是模板首次创建那一刻
                cycle_start = self.created_at
            else:
                if today_log and today_log.created_at:
                    # 已经被实例化过：以今天的 log 创建时间为准（更精确）
                    cycle_start = today_log.created_at
                else:
                    # 还没实例化：用本期"应该开始"的时刻
                    if stype == 'daily':
                        cycle_start = dt.combine(today, t(0, 0))
                    elif stype == 'weekly':
                        # schedule_day: 1=周一 ... 7=周日，与 isoweekday() 一致
                        target = self.schedule_day or today.isoweekday()
                        delta_days = (today.isoweekday() - target) % 7
                        cycle_start = dt.combine(today - timedelta(days=delta_days), t(0, 0))
                    elif stype == 'monthly':
                        # schedule_day: 几号；如果今天还没到本月那一天，回退到上月那天
                        target_day = self.schedule_day or today.day
                        if today.day >= target_day:
                            base = today.replace(
                                day=min(target_day,
                                        self._days_in_month(today.year, today.month)))
                        else:
                            year, month = (today.year, today.month - 1) if today.month > 1 \
                                          else (today.year - 1, 12)
                            base = d(year, month,
                                     min(target_day, self._days_in_month(year, month)))
                        cycle_start = dt.combine(base, t(0, 0))
                    else:
                        cycle_start = dt.combine(today, t(0, 0))

            data['template_created_at'] = data['created_at']
            data['cycle_started_at'] = str(cycle_start) if cycle_start else None
            # ⚠️ 关键：覆盖 created_at，让现有 OpenClaw 不改代码就能正确判优先级
            if cycle_start:
                data['created_at'] = str(cycle_start)
            data['is_today_instance'] = (stype != 'once')

        if with_today_status:
            try:
                from app.services.task_context import build_task_context_payload
                ctx = build_task_context_payload(
                    self.title or '',
                    self.description,
                    claw=self.openclaw,
                )
                data['pitfall_notice'] = ctx.get('pitfall_notice') or ''
                data['primary_skill'] = ctx.get('primary_skill') or 'agent-operating-protocol'
                data['trigger_terms'] = ctx.get('trigger_terms') or []
                data['matched_pitfall_ids'] = ctx.get('matched_pitfall_ids') or []
                data['operating_protocol_skill'] = 'agent-operating-protocol'
                # 统一任务上下文包新增字段
                data['required_skills'] = ctx.get('required_skills') or []
                data['preflight_checklist'] = ctx.get('preflight_checklist') or []
                data['top_pitfalls'] = ctx.get('top_pitfalls') or []
                data['references'] = ctx.get('references') or []
            except Exception:
                data['pitfall_notice'] = ''
                data['primary_skill'] = 'agent-operating-protocol'
                data['trigger_terms'] = []
                data['matched_pitfall_ids'] = []
                data['operating_protocol_skill'] = 'agent-operating-protocol'
                data['required_skills'] = []
                data['preflight_checklist'] = []
                data['top_pitfalls'] = []
                data['references'] = []
        return data

    @staticmethod
    def _days_in_month(year, month):
        import calendar
        return calendar.monthrange(year, month)[1]

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
    notified_at = db.Column(db.DateTime,
                            comment='企微通知时间：agent 自发或 Hub 兜底，NULL 表示尚未通知 owner')
    notified_strategy = db.Column(db.String(30), default='',
                                  comment='通知策略：agent_self(agent自己用 message 工具发) / timeout_fallback(Hub 5分钟兜底应用通知)')
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
            'notified_at': str(self.notified_at) if self.notified_at else None,
            'notified_strategy': self.notified_strategy or '',
        }


class ClawSidecarConfig(db.Model):
    """Sidecar 配置中心（B+ 方案）

    每个 OpenClaw 一条记录，存放运行时配置；Web 后台改动后 sidecar 通过
    心跳/拉配置接口感知并热加载。把 install.sh / 本地 env 的脏配置
    集中到 Hub，是"简单稳定"的关键。
    """
    __tablename__ = 'claw_sidecar_configs'

    claw_id = db.Column(db.Integer, db.ForeignKey('openclaw_instances.id'),
                        primary_key=True, comment='关联 OpenClaw ID')
    agent_type = db.Column(
        db.String(20), default='openclaw',
        comment='agent 类型：openclaw/hermes/codex/codebuddy/custom')
    openclaw_bin = db.Column(db.String(500), default='',
                             comment='openclaw CLI 绝对路径（自动探测后回填）')
    hermes_home = db.Column(db.String(500), default='',
                            comment='hermes 代码目录（仅 hermes 类型需要）')
    agent_name = db.Column(db.String(50), default='main',
                           comment='openclaw agent 子命令名，默认 main')
    agent_timeout = db.Column(db.Integer, default=300,
                              comment='单次 LLM 调用超时（秒），默认 300')
    safe_name = db.Column(db.String(50), default='',
                          comment='部署目录名（首次部署时生成，后续不变）')
    llm_provider = db.Column(db.String(50), default='venus',
                          comment='Hermes 大模型平台，当前固定 venus')
    llm_model = db.Column(db.String(100), default='venus',
                          comment='Hermes 大模型选择')
    wecom_enabled = db.Column(db.Boolean, default=False,
                              comment='本 claw 是否启用企微通知（owner_wecom_userid 必须有值）')
    enabled = db.Column(db.Boolean, default=True,
                        comment='是否启用 sidecar；关闭后 sidecar 自停')
    config_version = db.Column(db.Integer, default=1,
                               comment='每次配置变更 +1，sidecar 拿到比本地大就 reload')
    system_context_policy_json = db.Column(
        db.JSON,
        comment='Hub 管控的 trusted context 策略；原始值不直接下发',
    )
    runtime_config_json = db.Column(
        db.JSON,
        comment='无密钥的运行时身份：kind/provider/mode/platform/release 等',
    )
    config_owner = db.Column(
        db.String(20), default='hub', nullable=False,
        comment='运行配置归属：hub / worker；worker 时 Hub 不下发 llm_apply',
    )
    runtime_reported_at = db.Column(
        db.DateTime,
        comment='Worker 最近一次上报结构化运行时身份的时间',
    )
    sidecar_version = db.Column(db.String(30), default='',
                                comment='sidecar 上报的自身版本号')
    last_heartbeat_at = db.Column(db.DateTime,
                                  comment='sidecar 最近一次心跳/拉配置时间')
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)
    updated_by = db.Column(db.String(100), default='',
                           comment='最近一次修改人')

    claw = db.relationship('OpenClawInstance', backref=db.backref('sidecar_config', uselist=False))

    def to_dict(self):
        return {
            'claw_id': self.claw_id,
            'agent_type': self.agent_type,
            'openclaw_bin': self.openclaw_bin,
            'hermes_home': self.hermes_home,
            'agent_name': self.agent_name,
            'agent_timeout': self.agent_timeout,
            'llm_provider': self.llm_provider or 'venus',
            'llm_model': self.llm_model or 'venus',
            'wecom_enabled': bool(self.wecom_enabled),
            'enabled': bool(self.enabled),
            'config_version': self.config_version,
            'config_owner': self.config_owner or 'hub',
            'worker_runtime': self.runtime_config_json,
            'runtime_reported_at': (
                str(self.runtime_reported_at)
                if self.runtime_reported_at else None),
            'sidecar_version': self.sidecar_version,
            'last_heartbeat_at': str(self.last_heartbeat_at) if self.last_heartbeat_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
            'updated_by': self.updated_by,
        }


class WorkerRelease(db.Model):
    """Hub-imported immutable Claw Worker release candidate."""
    __tablename__ = 'worker_releases'
    __table_args__ = (
        db.UniqueConstraint('release_id', 'platform',
                            name='uq_worker_release_platform'),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    release_id = db.Column(db.String(80), nullable=False)
    source_repository = db.Column(db.String(500), default='')
    source_ref = db.Column(db.String(120), default='')
    source_commit = db.Column(db.String(40), nullable=False)
    committed_at = db.Column(db.String(40), default='')
    channel = db.Column(db.String(30), default='candidate')
    signature_status = db.Column(db.String(30), default='unsigned')
    release_manifest_sha256 = db.Column(db.String(64), nullable=False)
    platform = db.Column(db.String(40), nullable=False,
                         default='linux-x86_64')
    platform_manifest_sha256 = db.Column(db.String(64), nullable=False)
    package_manifest_sha256 = db.Column(db.String(64), nullable=False)
    artifact_filename = db.Column(db.String(255), nullable=False)
    artifact_sha256 = db.Column(db.String(64), nullable=False)
    artifact_size = db.Column(db.BigInteger, nullable=False)
    artifact_path = db.Column(db.String(1000), nullable=False,
                              comment='Hub 本机私有制品路径，不通过 API 返回')
    approval_status = db.Column(db.String(20), default='candidate',
                                comment='candidate/approved/rejected')
    is_default = db.Column(db.Boolean, default=False, nullable=False)
    imported_by = db.Column(db.String(100), default='system')
    imported_at = db.Column(db.DateTime, default=_now)
    approved_by = db.Column(db.String(100), default='')
    approved_at = db.Column(db.DateTime)
    rejected_by = db.Column(db.String(100), default='')
    rejected_at = db.Column(db.DateTime)

    def to_dict(self):
        return {
            'id': self.id,
            'release_id': self.release_id,
            'source_repository': self.source_repository,
            'source_ref': self.source_ref,
            'source_commit': self.source_commit,
            'committed_at': self.committed_at,
            'channel': self.channel,
            'signature_status': self.signature_status,
            'release_manifest_sha256': self.release_manifest_sha256,
            'platform': self.platform,
            'platform_manifest_sha256': self.platform_manifest_sha256,
            'package_manifest_sha256': self.package_manifest_sha256,
            'artifact_filename': self.artifact_filename,
            'artifact_sha256': self.artifact_sha256,
            'artifact_size': self.artifact_size,
            'approval_status': self.approval_status,
            'is_default': bool(self.is_default),
            'imported_by': self.imported_by,
            'imported_at': str(self.imported_at) if self.imported_at else None,
            'approved_by': self.approved_by,
            'approved_at': str(self.approved_at) if self.approved_at else None,
            'rejected_by': self.rejected_by,
            'rejected_at': str(self.rejected_at) if self.rejected_at else None,
        }


class AgentDeployment(db.Model):
    """Hub 代建 Agent 的远端部署记录

    每次 Hub 通过 SSH 在远端机器部署 / 重新部署一个 OpenClaw 对应的 Hermes Agent
    或 Claw Worker（Codex SDK），
    都会落一条记录。OpenClaw 创建本身不会因为部署失败回滚——部署状态独立追踪，
    支持后续重试和详情查看。

    敏感凭据（SSH 密码 / 私钥 / Venus API Key）只在请求生命周期内使用，绝不落库。
    """
    __tablename__ = 'agent_deployments'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    openclaw_id = db.Column(db.Integer, db.ForeignKey('openclaw_instances.id'),
                            nullable=False, comment='关联 OpenClaw ID')
    agent_type = db.Column(
        db.String(20), default='hermes',
        comment='agent 类型：hermes / codex / codebuddy（Claw Worker）')
    deploy_method = db.Column(db.String(20), default='systemd',
                              comment='部署方式：Hub 代建当前固定 systemd')
    host = db.Column(db.String(255), default='', comment='远端目标机 host:port 或纯 host')
    ssh_user = db.Column(db.String(64), default='', comment='SSH 登录用户')
    remote_base_dir = db.Column(db.String(500), default='',
                                comment='per-claw 远端工作目录：/opt/openclaw-agents/claw-12-xiaoma/...；systemd 记录 data + venv 摘要')
    container_name = db.Column(db.String(100), default='',
                               comment='docker=容器名 hermes-agent-claw-12；systemd=unit hermes-gateway-claw-12.service')
    image = db.Column(db.String(255), default='',
                      comment='docker 模式使用的 Hermes 镜像；systemd 模式留空')
    worker_release_record_id = db.Column(db.Integer,
                                         db.ForeignKey('worker_releases.id'))
    worker_release_id = db.Column(db.String(80), default='')
    worker_source_commit = db.Column(db.String(40), default='')
    worker_artifact_sha256 = db.Column(db.String(64), default='')
    status = db.Column(db.String(20), default='pending',
                       comment='pending/in_progress/success/failed/cancelled')
    log_tail = db.Column(db.Text, comment='最近一次 stdout/stderr 截尾，用于前端展示')
    error_message = db.Column(db.Text, comment='失败原因摘要')
    started_at = db.Column(db.DateTime, comment='开始执行时间')
    finished_at = db.Column(db.DateTime, comment='完成时间')
    triggered_by = db.Column(db.String(100), default='',
                             comment='触发人：用户名 / OpenClaw 名 / "system"')
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    claw = db.relationship('OpenClawInstance', backref='agent_deployments')
    worker_release = db.relationship('WorkerRelease')

    def to_dict(self):
        return {
            'id': self.id,
            'openclaw_id': self.openclaw_id,
            'agent_type': self.agent_type,
            'deploy_method': self.deploy_method,
            'host': self.host,
            'ssh_user': self.ssh_user,
            'remote_base_dir': self.remote_base_dir,
            'container_name': self.container_name,
            'image': self.image,
            'worker_release_record_id': self.worker_release_record_id,
            'worker_release_id': self.worker_release_id,
            'worker_source_commit': self.worker_source_commit,
            'worker_artifact_sha256': self.worker_artifact_sha256,
            'status': self.status,
            'log_tail': self.log_tail,
            'error_message': self.error_message,
            'started_at': str(self.started_at) if self.started_at else None,
            'finished_at': str(self.finished_at) if self.finished_at else None,
            'triggered_by': self.triggered_by,
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }


class WecomSendLog(db.Model):
    """企微发送日志（B+ 方案）

    所有 Hub 集中代发的企微消息都先落库，再异步/同步发出，方便排障和重试。
    """
    __tablename__ = 'wecom_send_logs'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    claw_id = db.Column(db.Integer, db.ForeignKey('openclaw_instances.id'),
                        comment='来源 claw（可空，表示系统级通知）')
    related_type = db.Column(db.String(30), default='',
                             comment='关联资源类型：todo_complete/message_failed/skill_review 等')
    related_id = db.Column(db.Integer, comment='关联资源 ID')
    target_userid = db.Column(db.String(64), default='',
                              comment='目标企微 userid（rajqiu 等）；空表示走群机器人')
    content = db.Column(db.Text, comment='发送内容（已渲染 markdown）')
    strategy = db.Column(db.String(30), default='',
                         comment='实际使用的发送策略：direct_api/group_robot/relay_lobster')
    status = db.Column(db.String(20), default='pending',
                       comment='pending/sent/failed')
    error = db.Column(db.Text, comment='失败错误信息')
    workflow_run_id = db.Column(db.Integer, index=True)
    workflow_step_id = db.Column(db.String(100), default='')
    request_summary_json = db.Column(db.JSON)
    template_version = db.Column(db.String(64), default='')
    message_hash = db.Column(db.String(64), default='', index=True)
    receipt_json = db.Column(db.JSON)
    gate_result_json = db.Column(db.JSON)
    created_at = db.Column(db.DateTime, default=_now)
    sent_at = db.Column(db.DateTime, comment='实际发送时间')

    claw = db.relationship('OpenClawInstance', backref='wecom_send_logs')

    def to_dict(self):
        return {
            'id': self.id,
            'claw_id': self.claw_id,
            'related_type': self.related_type,
            'related_id': self.related_id,
            'target_userid': self.target_userid,
            'content': self.content,
            'strategy': self.strategy,
            'status': self.status,
            'error': self.error,
            'workflow_run_id': self.workflow_run_id,
            'workflow_step_id': self.workflow_step_id or '',
            'request_summary': self.request_summary_json or {},
            'template_version': self.template_version or '',
            'message_hash': self.message_hash or '',
            'receipt': self.receipt_json or {},
            'gate_result': self.gate_result_json or {},
            'created_at': str(self.created_at) if self.created_at else None,
            'sent_at': str(self.sent_at) if self.sent_at else None,
        }


# ============== 课题讨论模型 ==============

# 板块定义
TOPIC_BOARDS = {
    'case_review': '用例评审',
    'test_methods': '测试用例和方法',
    'case_sharing': '典型案例分享',
    'risk_assessment': '质量风险评估',
    'client_perf': '客户端性能测试',
    'special_testing': '其他专项测试',
    'industry_news': '业界新闻分享',
}

# 课题可见性四档。
# 注意：`public` 沿用历史语义（登录 + 有项目权限），完全公开是新增的 `public_all`，
# 这样存量课题无需数据迁移，语义也不会漂移。
TOPIC_VISIBILITIES = {
    'public_all': '完全公开',
    'public': 'Hub 用户或 Agent',
    'project': '项目内用户或 Agent',
    'assigned': '指定用户或 Agent',
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
                           comment='public_all=完全公开(免项目权限), public=Hub 用户, '
                                   'project=项目内可见可参与, assigned=指定用户/Agent')
    # 用例评审关联字段（仅 board=case_review 时使用）
    review_library_id = db.Column(db.Integer, db.ForeignKey('test_case_libraries.id'),
                                   nullable=True, comment='关联用例库ID')
    review_module_paths = db.Column(db.JSON, nullable=True,
                                     comment='评审的模块路径列表，如["登录模块","支付模块/退款"]')
    review_case_ids = db.Column(db.JSON, nullable=True,
                                comment='评审的用例 ID 列表（指定部分用例评审时使用）')
    review_knowledge_id = db.Column(db.Integer, db.ForeignKey('knowledge_entries.id'),
                                     nullable=True, comment='前置信息知识条目ID')
    # 多轮评审总状态（仅 board=case_review 时使用）
    review_status = db.Column(db.String(20), default='reviewing',
                              comment='reviewing=评审中, closed=已关闭评审')
    review_summary = db.Column(db.Text, nullable=True,
                               comment='评审总结（Markdown），评审关闭后由发起者填写')
    review_summary_by = db.Column(db.String(100), nullable=True,
                                  comment='评审总结填写人')
    review_summary_at = db.Column(db.DateTime, nullable=True,
                                  comment='评审总结填写时间')
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
            'visibility_label': TOPIC_VISIBILITIES.get(
                self.visibility, TOPIC_VISIBILITIES['public']),
            'review_library_id': self.review_library_id,
            'review_module_paths': self.review_module_paths,
            'review_case_ids': self.review_case_ids,
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
            data['review_status'] = self.review_status or 'reviewing'
            data['review_summary'] = self.review_summary
            data['review_summary_by'] = self.review_summary_by
            data['review_summary_at'] = str(self.review_summary_at) if self.review_summary_at else None
            if self.review_library:
                lib = self.review_library
                data['review_library_name'] = lib.name
                data['review_library_project'] = lib.project_name
            if self.review_knowledge:
                data['review_knowledge_title'] = self.review_knowledge.title
            # 附加评审轮次列表
            rounds = CaseReviewRound.query.filter_by(
                topic_id=self.id
            ).order_by(CaseReviewRound.round_number).all()
            data['review_rounds'] = [rd.to_dict() for rd in rounds]
        return data


class CaseReviewRound(db.Model):
    """用例评审轮次 — 每次提交用例评审是一轮，支持多轮迭代。

    每轮包含：
    - 评审介绍（功能说明、编写方法等）
    - 用例内容（YAML 格式字符串）
    - 轮次状态：pending（待评审）/ approved（通过）/ rejected（打回）
    - 评审记录（评审人+意见）存在 CaseReviewComment 中
    """

    __tablename__ = 'case_review_rounds'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    topic_id = db.Column(db.Integer, db.ForeignKey('topics.id'), nullable=False,
                         comment='关联的评审课题 ID')
    round_number = db.Column(db.Integer, default=1, comment='轮次号，从1开始')
    description = db.Column(db.Text, default='',
                            comment='评审介绍（功能说明、编写方法等，Markdown）')
    case_content = db.Column(db.Text, default='',
                             comment='用例内容（YAML 格式）')
    status = db.Column(db.String(20), default='pending',
                       comment='pending=待评审, approved=通过, rejected=打回')
    submitted_by = db.Column(db.String(100), default='', comment='提交人')
    submitted_at = db.Column(db.DateTime, default=_now, comment='提交时间')

    is_deleted = db.Column(db.Boolean, default=False, comment='软删除标记')
    deleted_at = db.Column(db.DateTime, nullable=True, comment='软删除时间')

    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    topic = db.relationship('Topic', backref=db.backref(
        'review_rounds', lazy='dynamic', order_by='CaseReviewRound.round_number'))

    __table_args__ = (
        db.Index('ix_crr_topic', 'topic_id'),
        db.Index('ix_crr_topic_round', 'topic_id', 'round_number'),
    )

    def to_dict(self, with_comments=True):
        data = {
            'id': self.id,
            'topic_id': self.topic_id,
            'round_number': self.round_number,
            'description': self.description or '',
            'case_content': self.case_content or '',
            'status': self.status or 'pending',
            'is_deleted': self.is_deleted or False,
            'submitted_by': self.submitted_by or '',
            'submitted_at': str(self.submitted_at) if self.submitted_at else None,
            'created_at': str(self.created_at) if self.created_at else None,
        }
        if with_comments:
            comments = (CaseReviewComment.query.filter_by(round_id=self.id)
                        .filter(db.or_(CaseReviewComment.status == 'active',
                                       CaseReviewComment.status.is_(None)))
                        .order_by(CaseReviewComment.created_at).all())
            data['comments'] = [c.to_dict() for c in comments]
            # 计算综合评分（有评分的评审意见取平均）
            scores = [c.score for c in comments if c.score is not None]
            data['avg_score'] = round(sum(scores) / len(scores), 1) if scores else None
            data['score_count'] = len(scores)
        return data


class CaseReviewComment(db.Model):
    """用例评审意见 — 对某轮评审的评审记录"""

    __tablename__ = 'case_review_comments'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    round_id = db.Column(db.Integer, db.ForeignKey('case_review_rounds.id'),
                         nullable=False, comment='评审轮次 ID')
    author_name = db.Column(db.String(100), nullable=False, comment='评审人')
    author_claw_id = db.Column(db.Integer, nullable=True)
    author_user_id = db.Column(db.Integer, nullable=True)
    collaboration_session_id = db.Column(
        db.Integer, db.ForeignKey('collaboration_sessions.id'), nullable=True,
        index=True, comment='Developer AI 临时协作会话 ID；用于评审记录所有权')
    content = db.Column(db.Text, nullable=False, comment='评审意见（Markdown）')
    verdict = db.Column(db.String(20), default='comment',
                        comment='comment=评论, approve=通过, reject=打回')
    score = db.Column(db.Integer, nullable=True,
                      comment='评分（1-10分），可选')
    is_edited = db.Column(db.Boolean, default=False,
                          comment='是否已修改过（每人仅1次修改机会）')
    status = db.Column(db.String(20), default='active', nullable=False,
                       index=True, comment='active/deleted')
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)
    deleted_at = db.Column(db.DateTime, nullable=True)

    round = db.relationship('CaseReviewRound', backref=db.backref(
        'comments', lazy='dynamic', order_by='CaseReviewComment.created_at'))

    __table_args__ = (
        db.Index('ix_crc_round', 'round_id'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'round_id': self.round_id,
            'author_name': self.author_name or '',
            'author_claw_id': self.author_claw_id,
            'author_user_id': self.author_user_id,
            'source_type': ('developer_ai' if self.collaboration_session_id
                            else ('claw' if self.author_claw_id else 'user')),
            'content': self.content or '',
            'verdict': self.verdict or 'comment',
            'score': self.score,
            'is_edited': self.is_edited or False,
            'status': self.status or 'active',
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }


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
    author_name = db.Column(db.String(160), nullable=False)
    collaboration_session_id = db.Column(
        db.Integer, nullable=True, index=True,
        comment='外部临时协作会话 ID；仅用于回复所有权，不级联会话')
    reply_to_id = db.Column(db.Integer, db.ForeignKey('topic_replies.id'),
                             nullable=True, comment='回复某条回复的 ID')
    status = db.Column(db.String(20), default='active',
                       comment='active/deleted')
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

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
            'external_collaboration': bool(self.collaboration_session_id),
            'reply_to_id': self.reply_to_id,
            'status': self.status,
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }


class TopicGrant(db.Model):
    """课题定向授权 — visibility=assigned 时指定可参与的用户/Agent。

    与 `TestCaseLibraryReview.invited_reviewers`（仅快照，不参与鉴权）不同，
    本表是 `assigned` 可见性的唯一鉴权依据。授权给用户时，其名下 Agent
    （`OpenClawInstance.owner == username`）自动继承，避免"人能看、Agent 看不到"。
    """

    __tablename__ = 'topic_grants'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    topic_id = db.Column(db.Integer, db.ForeignKey('topics.id'), nullable=False,
                         comment='课题 ID')
    grant_type = db.Column(db.String(10), nullable=False,
                           comment='user=指定用户, claw=指定 Agent')
    target_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True,
                               comment='grant_type=user 时的用户 ID')
    target_claw_id = db.Column(db.Integer, db.ForeignKey('openclaw_instances.id'),
                               nullable=True, comment='grant_type=claw 时的 Agent ID')
    target_name = db.Column(db.String(100), default='',
                            comment='被授权对象显示名（冗余展示用）')
    granted_by = db.Column(db.String(100), default='',
                           comment='授权人 username/claw_name')
    granted_at = db.Column(db.DateTime, default=_now)
    expires_at = db.Column(db.DateTime, nullable=True,
                           comment='为空表示长期有效')

    topic = db.relationship('Topic', backref=db.backref('grants', lazy='dynamic',
                                                        cascade='all, delete-orphan'))
    target_user = db.relationship('User', foreign_keys=[target_user_id])
    target_claw = db.relationship('OpenClawInstance', foreign_keys=[target_claw_id])

    __table_args__ = (
        db.UniqueConstraint('topic_id', 'grant_type', 'target_user_id',
                            'target_claw_id', name='uq_topic_grant_target'),
        db.Index('ix_topic_grant_topic', 'topic_id'),
        db.Index('ix_topic_grant_user', 'target_user_id'),
        db.Index('ix_topic_grant_claw', 'target_claw_id'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'topic_id': self.topic_id,
            'grant_type': self.grant_type,
            'target_user_id': self.target_user_id,
            'target_claw_id': self.target_claw_id,
            'target_name': self.target_name or '',
            'granted_by': self.granted_by or '',
            'granted_at': str(self.granted_at) if self.granted_at else None,
            'expires_at': str(self.expires_at) if self.expires_at else None,
        }


class CaseReviewNodeMark(db.Model):
    """评审脑图节点标记（❗有问题 / ⚠️风险 / 🚩重点）。

    这是**评审侧的镜像层**：标记只属于某次评审课题，不回写 `TestCase`、
    不写 `content.status`，用例库自己的目录脑图也不展示它。目的是让评审
    过程中的标注不污染用例库数据；如需"评审结论回写用例"，另做显式同步动作。
    """

    __tablename__ = 'case_review_node_marks'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    topic_id = db.Column(db.Integer, db.ForeignKey('topics.id'), nullable=False,
                         comment='所属评审课题 ID')
    node_type = db.Column(db.String(10), nullable=False,
                          comment='module=目录节点, case=用例节点')
    node_key = db.Column(db.String(500), nullable=False,
                         comment='node_type=case 时为 test_cases.id；module 时为 module_path')
    # 唯一键不能直接用 node_key：utf8mb4 下 VARCHAR(500) 约占 2000 字节，
    # 老版本 MariaDB 的索引前缀上限是 767 字节会报 1071。改用定长哈希列。
    node_key_hash = db.Column(db.String(64), nullable=False,
                              comment='sha256(node_type:node_key)，仅用于唯一索引')
    mark = db.Column(db.String(20), nullable=False,
                     comment='question=有问题/待修改, risk=风险或待确认, flag=重点关注')
    note = db.Column(db.String(500), default='', comment='可选短备注')
    marked_by = db.Column(db.String(100), default='',
                          comment='标记人 username/claw_name')
    marked_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    topic = db.relationship('Topic', backref=db.backref(
        'node_marks', lazy='dynamic', cascade='all, delete-orphan'))

    __table_args__ = (
        db.UniqueConstraint('topic_id', 'node_key_hash',
                            name='uq_case_review_node_mark'),
        db.Index('ix_case_review_mark_topic', 'topic_id'),
    )

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.refresh_key_hash()

    @staticmethod
    def compute_key_hash(node_type, node_key):
        import hashlib
        raw = '%s:%s' % (node_type or '', node_key or '')
        return hashlib.sha256(raw.encode('utf-8')).hexdigest()

    def refresh_key_hash(self):
        self.node_key_hash = self.compute_key_hash(self.node_type, self.node_key)

    @property
    def node_id(self):
        """与脑图节点 id 对齐：`case:123` / `mod:登录模块`。"""
        prefix = 'case:' if self.node_type == 'case' else 'mod:'
        return prefix + (self.node_key or '')

    def to_dict(self):
        return {
            'id': self.id,
            'topic_id': self.topic_id,
            'node_id': self.node_id,
            'node_type': self.node_type,
            'node_key': self.node_key or '',
            'mark': self.mark,
            'note': self.note or '',
            'marked_by': self.marked_by or '',
            'marked_at': str(self.marked_at) if self.marked_at else None,
        }


# ============== 测试计划与任务模型 ==============

class TestIteration(db.Model):
    """测试迭代 — 代表一次外发版本，迭代下有多次转测（TestPlan）"""
    __tablename__ = 'test_iterations'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(200), nullable=False, comment='迭代名称')
    description = db.Column(db.Text, comment='迭代描述')

    # 版本信息
    version_name = db.Column(db.String(100), comment='版本号，如 v3.2.1')
    version_type = db.Column(db.String(20), default='regular',
                             comment='版本类型：regular=常规版本, resource=资源版本, hotfix=紧急补丁')

    # 排期
    start_date = db.Column(db.Date, comment='开始日期')
    end_date = db.Column(db.Date, comment='结束日期')

    # 项目关联
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'),
                           comment='关联项目ID')

    # TAPD 迭代关联（JSON数组，支持多选）—— 从 TestPlan 层上移到 Iteration 层
    tapd_iteration_ids = db.Column(db.JSON, comment='TAPD 迭代 ID 列表')
    tapd_iteration_names = db.Column(db.JSON, comment='TAPD 迭代名称列表（冗余）')
    tapd_workspace_id = db.Column(db.String(50), comment='TAPD workspace ID')

    # 状态
    status = db.Column(db.String(20), default='draft',
                       comment='状态：draft=草稿, active=进行中, completed=已完成, archived=已归档')

    # 进度统计（由子计划/任务汇总）
    total_plans = db.Column(db.Integer, default=0, comment='测试计划数')
    total_tasks = db.Column(db.Integer, default=0, comment='总任务数')
    completed_tasks = db.Column(db.Integer, default=0, comment='已完成任务数')
    total_bugs = db.Column(db.Integer, default=0, comment='Bug 总数')

    # 创建者
    created_by = db.Column(db.String(100), default='', comment='创建人')
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    # 关联
    project = db.relationship('Project', backref='test_iterations')
    plans = db.relationship('TestPlan', backref='iteration',
                            lazy='dynamic', cascade='all, delete-orphan')

    def to_dict(self, with_plans=False):
        data = {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'version_name': self.version_name,
            'version_type': self.version_type,
            'start_date': str(self.start_date) if self.start_date else None,
            'end_date': str(self.end_date) if self.end_date else None,
            'project_id': self.project_id,
            'project_name': self.project.name if self.project else None,
            'tapd_iteration_ids': self.tapd_iteration_ids or [],
            'tapd_iteration_names': self.tapd_iteration_names or [],
            'tapd_workspace_id': self.tapd_workspace_id,
            'status': self.status,
            'total_plans': self.total_plans,
            'total_tasks': self.total_tasks,
            'completed_tasks': self.completed_tasks,
            'total_bugs': self.total_bugs,
            'progress': round(self.completed_tasks / self.total_tasks * 100, 1) if self.total_tasks > 0 else 0,
            'created_by': self.created_by,
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }
        if with_plans:
            data['plans'] = [p.to_dict(with_tasks=True) for p in self.plans.order_by(TestPlan.created_at)]
        return data


class TestPlan(db.Model):
    """测试计划排期"""
    __tablename__ = 'test_plans'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    team_id = db.Column(db.Integer, db.ForeignKey('agent_teams.id'), index=True)
    name = db.Column(db.String(200), nullable=False, comment='计划名称')
    description = db.Column(db.Text, comment='计划描述')

    # 所属迭代（三级架构核心外键）
    iteration_id = db.Column(db.Integer, db.ForeignKey('test_iterations.id'),
                             comment='所属测试迭代ID')

    # 版本类型（保留，兼容旧数据；新建时从迭代继承）
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

    # Agent 编写的只读测试报告（前端只展示，写入走 API）
    report_content = db.Column(db.Text, comment='测试报告内容，支持 markdown/html')
    report_format = db.Column(db.String(20), default='markdown',
                              comment='报告格式：markdown/html')
    report_updated_by = db.Column(db.String(100), default='', comment='报告更新人')
    report_updated_at = db.Column(db.DateTime, comment='报告更新时间')

    # 创建者
    created_by = db.Column(db.String(100), default='', comment='创建人')
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    # 关联
    project = db.relationship('Project', backref='test_plans')
    agent_team = db.relationship('AgentTeam')
    tasks = db.relationship('TestTask', backref='plan',
                            lazy='dynamic', cascade='all, delete-orphan')

    def to_dict(self, with_tasks=False):
        data = {
            'id': self.id,
            'team_id': self.team_id,
            'team_name': self.agent_team.name if self.agent_team else None,
            'name': self.name,
            'description': self.description,
            'iteration_id': self.iteration_id,
            'iteration_name': self.iteration.name if self.iteration else None,
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
            'has_report': bool(self.report_content),
            'report_format': self.report_format or 'markdown',
            'report_updated_by': self.report_updated_by or '',
            'report_updated_at': str(self.report_updated_at) if self.report_updated_at else None,
            'progress': round(self.completed_tasks / self.total_tasks * 100, 1) if self.total_tasks > 0 else 0,
            'created_by': self.created_by,
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }
        if with_tasks:
            data['tasks'] = [t.to_dict() for t in self.tasks.order_by(TestTask.created_at)]
        return data


class TestPlanReport(db.Model):
    """测试计划报告（支持多份报告）"""
    __tablename__ = 'test_plan_reports'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    plan_id = db.Column(db.Integer, db.ForeignKey('test_plans.id'),
                        nullable=False, comment='所属测试计划ID')
    title = db.Column(db.String(200), nullable=False, comment='报告标题')
    content = db.Column(db.Text, comment='报告内容（markdown/html）')
    format = db.Column(db.String(20), default='markdown',
                       comment='格式：markdown/html')
    created_by = db.Column(db.String(100), default='', comment='创建人')
    # MEMORY #134：反向追溯到全局 TestReport，方便从旧 API 跳到新报告页
    linked_test_report_id = db.Column(db.Integer,
                                      db.ForeignKey('test_reports.id'),
                                      comment='对应全局 TestReport.id，可为空')
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    plan = db.relationship('TestPlan', backref=db.backref(
        'reports', lazy='dynamic', cascade='all, delete-orphan'))

    def to_dict(self):
        content = self.content or ''
        # 自动提取内容摘要：去掉 markdown 标签，取前 120 字符
        summary = content[:200]
        for tag in ('#', '*', '`', '\n', '---'):
            summary = summary.replace(tag, ' ')
        summary = ' '.join(summary.split())[:120]
        
        # 尝试从 created_by 查找对应的 OpenClaw 名称
        created_by_name = self.created_by or ''
        if self.created_by:
            # 尝试按 OpenClaw 名称查找
            claw = OpenClawInstance.query.filter_by(name=self.created_by).first()
            if claw:
                created_by_name = claw.name
            else:
                # 尝试按用户名查找
                user = User.query.filter(
                    (User.username == self.created_by) | 
                    (User.display_name == self.created_by)
                ).first()
                if user and user.bound_claw_id:
                    claw = OpenClawInstance.query.get(user.bound_claw_id)
                    if claw:
                        created_by_name = claw.name
        
        return {
            'id': self.id,
            'plan_id': self.plan_id,
            'linked_test_report_id': self.linked_test_report_id,
            'title': self.title,
            'content': content,
            'format': self.format or 'markdown',
            'summary': summary + ('...' if len(content) > 120 else ''),
            'created_by': self.created_by or '',
            'created_by_name': created_by_name,
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }


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

    # 执行人（OpenClaw 实例 或 直接指派用户）
    assignee_claw_id = db.Column(db.Integer, db.ForeignKey('openclaw_instances.id'),
                                  comment='指派的 OpenClaw ID（分配给Agent时）')
    assignee_username = db.Column(db.String(100), default='',
                                  comment='直接指派的用户名（分配给真人时）')

    # 排期
    start_date = db.Column(db.Date, comment='开始日期')
    end_date = db.Column(db.Date, comment='截止日期')
    # 新版团队计划调度合同。默认关闭以保持存量 TestTask/Stage 行为；只有
    # 用户或经理显式配置后，Hub 才按模板生成不可变 occurrence。
    schedule_enabled = db.Column(db.Boolean, nullable=False, default=False)
    recurrence_type = db.Column(
        db.String(16), nullable=False, default='once',
        comment='once/daily/weekly')
    recurrence_weekdays_json = db.Column(
        db.JSON, comment='weekly 使用，0=周一…6=周日')
    schedule_timezone = db.Column(
        db.String(64), nullable=False, default='Asia/Shanghai')
    not_before_time = db.Column(
        db.String(5), default='', comment='HH:MM，到点前不得派发')
    due_time = db.Column(
        db.String(5), default='', comment='HH:MM，当日建议截止时间')
    auto_dispatch = db.Column(
        db.Boolean, nullable=False, default=False,
        comment='到点后由 Hub 向固定执行人创建 AgentTask')
    execution_role = db.Column(
        db.String(32), nullable=False, default='member_work',
        comment='member_work/manager_work')

    # 优先级
    priority = db.Column(db.Enum('P0', 'P1', 'P2', 'P3'), default='P2', comment='优先级')

    # 关联用例库
    library_id = db.Column(db.Integer, db.ForeignKey('test_case_libraries.id'),
                           comment='关联的用例库ID')

    # 用例筛选条件（API 创建时更灵活）
    case_filter = db.Column(db.JSON, comment='用例筛选条件，如 {"module_paths":["登录模块"], "priorities":["P0"], "case_ids":[1,2,3]}')

    # 最近一次“同步最新用例库”前的任务用例快照。只保留一份，恢复成功后失效。
    case_sync_backup_json = db.Column(
        db.JSON, comment='最近一次用例库同步前的任务用例与执行结果备份')
    case_sync_backup_created_at = db.Column(
        db.DateTime, comment='同步备份创建时间')
    case_sync_backup_restored_at = db.Column(
        db.DateTime, comment='同步备份恢复时间；非空表示恢复机会已使用')

    # 进度和结果
    status = db.Column(db.Enum('assigned', 'pending', 'in_progress', 'completed', 'blocked', 'skipped'),
                       default='assigned',
                       comment='状态：assigned=新分配, pending=待开始, in_progress=进行中, '
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
        assignee_owner = self.assignee.owner if self.assignee else ''
        assignee_user = User.query.filter_by(username=assignee_owner).first() if assignee_owner else None

        # 统一显示逻辑：
        # 1. 分配给Agent → 显示Agent对应的owner用户名
        # 2. 直接分配给人 → 显示该人
        if self.assignee_claw_id and self.assignee:
            display_name = (assignee_user.display_name if assignee_user and assignee_user.display_name
                           else assignee_owner)
        elif self.assignee_username:
            direct_user = User.query.filter_by(username=self.assignee_username).first()
            display_name = (direct_user.display_name if direct_user and direct_user.display_name
                           else self.assignee_username)
        else:
            display_name = ''

        data = {
            'id': self.id,
            'plan_id': self.plan_id,
            'name': self.name,
            'description': self.description,
            'task_type': self.task_type,
            'assignee_claw_id': self.assignee_claw_id,
            'assignee_name': self.assignee.name if self.assignee else None,
            'assignee_username': self.assignee_username or '',
            'assignee_owner': assignee_owner or self.assignee_username or '',
            'assignee_owner_display_name': display_name,
            'assignee_display': display_name,
            'assignee_owner_user_id': assignee_user.id if assignee_user else None,
            'assignee_owner_wecom_userid': self.assignee.owner_wecom_userid if self.assignee else '',
            'start_date': str(self.start_date) if self.start_date else None,
            'end_date': str(self.end_date) if self.end_date else None,
            'schedule_enabled': bool(self.schedule_enabled),
            'recurrence_type': self.recurrence_type or 'once',
            'recurrence_weekdays': self.recurrence_weekdays_json or [],
            'schedule_timezone': self.schedule_timezone or 'Asia/Shanghai',
            'not_before_time': self.not_before_time or '',
            'due_time': self.due_time or '',
            'auto_dispatch': bool(self.auto_dispatch),
            'execution_role': self.execution_role or 'member_work',
            'priority': self.priority,
            'library_id': self.library_id,
            'library_name': self.library.name if self.library else None,
            'case_filter': self.case_filter,
            'case_sync_backup_available': bool(
                self.case_sync_backup_json
                and not self.case_sync_backup_restored_at),
            'case_sync_backup_created_at': (
                str(self.case_sync_backup_created_at)
                if self.case_sync_backup_created_at else None),
            'case_sync_backup_restored_at': (
                str(self.case_sync_backup_restored_at)
                if self.case_sync_backup_restored_at else None),
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
        if self.schedule_enabled:
            occurrence = self.occurrences.order_by(
                TestTaskOccurrence.occurrence_date.desc(),
                TestTaskOccurrence.id.desc()).first()
            data['current_occurrence'] = (
                occurrence.to_dict() if occurrence else None)
        if with_cases:
            data['task_cases'] = [tc.to_dict() for tc in self.task_cases]
        return data


class TestTaskOccurrence(db.Model):
    """One immutable scheduled execution of a TestTask template.

    A recurring TestTask is configuration.  Execution truth lives here so a
    previous day's terminal state/report can never be reused as today's work.
    """
    __tablename__ = 'test_task_occurrences'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    plan_id = db.Column(
        db.Integer, db.ForeignKey('test_plans.id'), nullable=False, index=True)
    test_task_id = db.Column(
        db.Integer, db.ForeignKey('test_tasks.id'), nullable=False, index=True)
    occurrence_date = db.Column(db.Date, nullable=False, index=True)
    timezone = db.Column(
        db.String(64), nullable=False, default='Asia/Shanghai')
    not_before_at = db.Column(db.DateTime, nullable=False, index=True)
    due_at = db.Column(db.DateTime)
    status = db.Column(
        db.String(24), nullable=False, default='scheduled', index=True)
    assignee_claw_id = db.Column(
        db.Integer, db.ForeignKey('openclaw_instances.id'), nullable=False,
        index=True)
    mission_stage_id = db.Column(
        db.Integer, db.ForeignKey('mission_stages.id'), index=True)
    agent_task_id = db.Column(
        db.Integer, db.ForeignKey('agent_tasks.id'), index=True)
    workflow_run_id = db.Column(
        db.Integer, db.ForeignKey('workflow_runs.id'), index=True)
    attempt_count = db.Column(db.Integer, nullable=False, default=0)
    next_action = db.Column(db.String(64), default='wait_not_before')
    next_check_at = db.Column(db.DateTime, index=True)
    last_heartbeat_at = db.Column(db.DateTime)
    result_summary = db.Column(db.Text)
    evidence_refs_json = db.Column(db.JSON)
    created_at = db.Column(db.DateTime, default=_now, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=_now, onupdate=_now, nullable=False)

    task = db.relationship('TestTask', backref=db.backref(
        'occurrences', lazy='dynamic', cascade='all, delete-orphan'))
    assignee = db.relationship('OpenClawInstance')
    agent_task = db.relationship('AgentTask')

    __table_args__ = (
        db.UniqueConstraint(
            'plan_id', 'test_task_id', 'occurrence_date',
            name='uq_test_task_occurrence_date'),
        db.Index(
            'ix_test_task_occurrence_dispatch',
            'plan_id', 'status', 'not_before_at'),
    )

    def to_dict(self):
        agent = self.agent_task
        lease_expired = bool(
            agent and agent.status == 'running'
            and agent.lease_expires_at
            and agent.lease_expires_at <= _now())
        return {
            'id': self.id,
            'plan_id': self.plan_id,
            'test_task_id': self.test_task_id,
            'occurrence_date': str(self.occurrence_date),
            'timezone': self.timezone or 'Asia/Shanghai',
            'not_before_at': (
                self.not_before_at.isoformat() + '+08:00'
                if self.not_before_at else None),
            'due_at': (
                self.due_at.isoformat() + '+08:00' if self.due_at else None),
            'status': self.status,
            'assignee_claw_id': self.assignee_claw_id,
            'mission_stage_id': self.mission_stage_id,
            'agent_task_id': self.agent_task_id,
            'workflow_run_id': self.workflow_run_id,
            'attempt_count': int(self.attempt_count or 0),
            'next_action': self.next_action or '',
            'next_check_at': (
                self.next_check_at.isoformat() + '+08:00'
                if self.next_check_at else None),
            'last_heartbeat_at': (
                self.last_heartbeat_at.isoformat() + '+08:00'
                if self.last_heartbeat_at else None),
            'result_summary': self.result_summary or '',
            'evidence_refs': self.evidence_refs_json or [],
            'execution': ({
                'agent_task_row_id': agent.id,
                'agent_task_id': agent.task_id,
                'status': agent.status,
                'attempt_no': int(agent.attempt_no or 0),
                'retry_count': int(agent.retry_count or 0),
                'retry_max': int(agent.retry_max or 0),
                'last_heartbeat_at': (
                    str(agent.last_heartbeat_at)
                    if agent.last_heartbeat_at else None),
                'lease_expires_at': (
                    str(agent.lease_expires_at)
                    if agent.lease_expires_at else None),
                'lease_expired': lease_expired,
                'terminal_reason': agent.terminal_reason or '',
            } if agent else None),
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }


class TestTaskReport(db.Model):
    """测试任务报告（支持多份报告，与测试计划报告类似）"""
    __tablename__ = 'test_task_reports'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    task_id = db.Column(db.Integer, db.ForeignKey('test_tasks.id'),
                        nullable=False, comment='所属测试任务ID')
    occurrence_id = db.Column(
        db.Integer, db.ForeignKey('test_task_occurrences.id'),
        nullable=True, index=True, comment='周期任务执行实例ID')
    title = db.Column(db.String(200), nullable=False, comment='报告标题')
    content = db.Column(db.Text, comment='报告内容（markdown/html）')
    format = db.Column(db.String(20), default='markdown',
                       comment='格式：markdown/html')
    created_by = db.Column(db.String(100), default='', comment='创建人')
    # MEMORY #134：反向追溯到全局 TestReport
    linked_test_report_id = db.Column(db.Integer,
                                      db.ForeignKey('test_reports.id'),
                                      comment='对应全局 TestReport.id，可为空')
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    task = db.relationship('TestTask', backref=db.backref(
        'reports', lazy='dynamic', cascade='all, delete-orphan'))

    def to_dict(self):
        content = self.content or ''
        summary = content[:200]
        for tag in ('#', '*', '`', '\n', '---'):
            summary = summary.replace(tag, ' ')
        summary = ' '.join(summary.split())[:120]

        created_by_name = self.created_by or ''
        if self.created_by:
            claw = OpenClawInstance.query.filter_by(name=self.created_by).first()
            if claw:
                created_by_name = claw.name
            else:
                user = User.query.filter(
                    (User.username == self.created_by) |
                    (User.display_name == self.created_by)
                ).first()
                if user and user.bound_claw_id:
                    claw2 = OpenClawInstance.query.get(user.bound_claw_id)
                    if claw2:
                        created_by_name = claw2.name

        return {
            'id': self.id,
            'task_id': self.task_id,
            'occurrence_id': self.occurrence_id,
            'linked_test_report_id': self.linked_test_report_id,
            'title': self.title,
            'content': content,
            'format': self.format or 'markdown',
            'summary': summary + ('...' if len(content) > 120 else ''),
            'created_by': self.created_by or '',
            'created_by_name': created_by_name,
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }


class TestTaskBugReport(db.Model):
    """测试任务Bug列表上报（支持多次上报）"""
    __tablename__ = 'test_task_bug_reports'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    task_id = db.Column(db.Integer, db.ForeignKey('test_tasks.id'),
                        nullable=False, comment='所属测试任务ID')
    total_bugs = db.Column(db.Integer, default=0, comment='Bug 总数')
    content = db.Column(db.Text, comment='Bug 详情内容（markdown/html 自由格式）')
    format = db.Column(db.String(20), default='markdown',
                       comment='格式：markdown/html')
    created_by = db.Column(db.String(100), default='', comment='上报人')
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    task = db.relationship('TestTask', backref=db.backref(
        'bug_reports', lazy='dynamic', cascade='all, delete-orphan'))

    def to_dict(self):
        content = self.content or ''
        summary = content[:200]
        for tag in ('#', '*', '`', '\n', '---'):
            summary = summary.replace(tag, ' ')
        summary = ' '.join(summary.split())[:100]

        return {
            'id': self.id,
            'task_id': self.task_id,
            'total_bugs': self.total_bugs,
            'content': content,
            'format': self.format or 'markdown',
            'summary': summary + ('...' if len(content) > 100 else ''),
            'created_by': self.created_by or '',
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }


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
    tapd_bug_url = db.Column(db.String(500), comment='关联的 TAPD Bug 链接')
    case_info_snapshot = db.Column(db.JSON, comment='同步到 Bug 的用例信息快照')
    case_source_snapshot = db.Column(
        db.JSON, comment='任务关联时的用例内容快照，用于识别用例库变更')
    bug_sync_status = db.Column(
        db.String(20), default='none',
        comment='Bug 用例信息同步状态：none/synced/failed')
    bug_sync_error = db.Column(db.Text, comment='Bug 用例信息同步错误')
    bug_synced_at = db.Column(db.DateTime, comment='最近一次同步成功时间')

    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    case = db.relationship('TestCase', backref='task_executions')

    __table_args__ = (
        db.UniqueConstraint('task_id', 'case_id', name='uq_task_case'),
    )

    def to_dict(self):
        d = {
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
            'tapd_bug_url': self.tapd_bug_url or '',
            'case_info_snapshot': self.case_info_snapshot,
            'bug_sync_status': self.bug_sync_status or 'none',
            'bug_sync_error': self.bug_sync_error or '',
            'bug_synced_at': str(self.bug_synced_at) if self.bug_synced_at else None,
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }
        if self.case:
            d['case_content'] = self.case.content
            d['case_type'] = self.case.type
            d['case_module_path'] = self.case.module_path or ''
        return d


# ==================== 任务链（Task Chain）====================
# 任务链是一连串有序任务，分配给多人或多 Agent，前置任务完成后自动触发下一步

class TestTaskChain(db.Model):
    """任务链：一组有序的步骤，前置步骤完成后自动推进"""
    __tablename__ = 'test_task_chains'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    plan_id = db.Column(db.Integer, db.ForeignKey('test_plans.id'),
                        nullable=False, comment='所属测试计划')
    name = db.Column(db.String(200), nullable=False, comment='任务链名称')
    description = db.Column(db.Text, comment='任务链描述/目标')

    # 状态
    status = db.Column(db.Enum('draft', 'active', 'completed', 'paused', 'cancelled'),
                       default='draft',
                       comment='draft=草稿, active=进行中, completed=已完成, '
                               'paused=暂停, cancelled=已取消')
    current_step = db.Column(db.Integer, default=0,
                             comment='当前进行到第几步（0=未开始，1=第一步）')
    total_steps = db.Column(db.Integer, default=0, comment='总步骤数')

    # 优先级
    priority = db.Column(db.Enum('P0', 'P1', 'P2', 'P3'), default='P1', comment='优先级')

    # 任务链执行结论（支持富文本）
    execution_conclusion = db.Column(db.Text, comment='任务链执行结论（markdown/html）')
    conclusion_format = db.Column(db.String(20), default='markdown', comment='结论格式：markdown/html')
    conclusion_updated_by = db.Column(db.String(100), default='', comment='结论提交人')
    conclusion_updated_at = db.Column(db.DateTime, comment='结论提交时间')

    # 创建者
    created_by = db.Column(db.String(100), default='', comment='创建人')
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)
    completed_at = db.Column(db.DateTime, comment='完成时间')

    # 关联
    plan = db.relationship('TestPlan', backref='task_chains')
    steps = db.relationship('TestTaskChainStep', backref='chain',
                            lazy='dynamic', cascade='all, delete-orphan',
                            order_by='TestTaskChainStep.step_order')

    def to_dict(self, with_steps=False):
        data = {
            'id': self.id,
            'plan_id': self.plan_id,
            'name': self.name,
            'description': self.description,
            'status': self.status,
            'current_step': self.current_step,
            'total_steps': self.total_steps,
            'priority': self.priority,
            'execution_conclusion': self.execution_conclusion or '',
            'conclusion_format': self.conclusion_format or 'markdown',
            'conclusion_updated_by': self.conclusion_updated_by or '',
            'conclusion_updated_at': str(self.conclusion_updated_at) if self.conclusion_updated_at else None,
            'created_by': self.created_by,
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
            'completed_at': str(self.completed_at) if self.completed_at else None,
        }
        if with_steps:
            data['steps'] = [s.to_dict() for s in self.steps.order_by(
                TestTaskChainStep.step_order).all()]
        return data


class TestTaskChainStep(db.Model):
    """任务链步骤：每一步是一个独立的子任务，分配给特定 Agent"""
    __tablename__ = 'test_task_chain_steps'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    chain_id = db.Column(db.Integer, db.ForeignKey('test_task_chains.id'),
                         nullable=False, comment='所属任务链')
    step_order = db.Column(db.Integer, nullable=False, comment='步骤顺序（从1开始）')

    name = db.Column(db.String(200), nullable=False, comment='步骤名称')
    description = db.Column(db.Text, comment='步骤描述/执行要求')
    task_type = db.Column(db.String(50), default='other',
                          comment='任务类型：与 TestTask.task_type 一致')

    # 执行人
    assignee_claw_id = db.Column(db.Integer, db.ForeignKey('openclaw_instances.id'),
                                  comment='指派的 OpenClaw ID')

    # 状态
    status = db.Column(db.Enum('waiting', 'pending', 'in_progress', 'completed', 'failed', 'skipped'),
                       default='waiting',
                       comment='waiting=等待前置完成, pending=待开始(已通知), '
                               'in_progress=进行中, completed=已完成, '
                               'failed=失败, skipped=跳过')

    # 结果
    result_summary = db.Column(db.Text, comment='步骤执行结果/产出摘要')
    output_data = db.Column(db.Text, comment='步骤产出数据JSON(传递给下一步)')

    # 时间
    started_at = db.Column(db.DateTime, comment='开始时间')
    completed_at = db.Column(db.DateTime, comment='完成时间')
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    # 关联
    assignee = db.relationship('OpenClawInstance', backref='chain_steps')

    def to_dict(self):
        assignee_owner = self.assignee.owner if self.assignee else ''
        assignee_user = User.query.filter_by(username=assignee_owner).first() if assignee_owner else None
        return {
            'id': self.id,
            'chain_id': self.chain_id,
            'step_order': self.step_order,
            'name': self.name,
            'description': self.description,
            'task_type': self.task_type,
            'assignee_claw_id': self.assignee_claw_id,
            'assignee_name': self.assignee.name if self.assignee else None,
            'assignee_owner': assignee_owner,
            'assignee_owner_display_name': (
                assignee_user.display_name if assignee_user and assignee_user.display_name else assignee_owner
            ),
            'status': self.status,
            'result_summary': self.result_summary,
            'output_data': json.loads(self.output_data) if self.output_data else None,
            'started_at': str(self.started_at) if self.started_at else None,
            'completed_at': str(self.completed_at) if self.completed_at else None,
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }


# ==================== 工程分析中心（Engineering Analysis Center）====================
# 详细设计参考：g:/RacingGoUnity/Documents/Testing/OpenClaw_Engineering_Analysis_Center_Design.md
# 落地原则：所有写操作复用 Hub 已有 projects / test_iterations / test_case_libraries / test_cases / knowledge_entries / audit_logs
# 仅新增 5 张本模块专属表，不修改 Hub 任何现有表/接口。

class EngineeringBaseline(db.Model):
    """工程分析基线 — 一个项目可有多份基线（不同分支或不同时点）"""
    __tablename__ = 'engineering_baselines'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'),
                           nullable=False, comment='关联 Hub 项目ID')
    name = db.Column(db.String(200), nullable=False,
                     comment='基线名称，如 RacingGoUnity-dev-baseline')
    repo_url = db.Column(db.String(500), nullable=False,
                         comment='Git 仓库地址')
    branch = db.Column(db.String(100), nullable=False, default='main',
                       comment='目标分支')
    baseline_commit = db.Column(db.String(64), comment='基线 commit hash')
    architecture_doc_url = db.Column(db.String(500),
                                     comment='基线说明文档 URL 或 knowledge_entries.id')
    module_mapping = db.Column(db.JSON,
                               comment='路径->模块映射规则，如 {"Assets/Scripts/Network/.*": "network"}')
    risk_rules = db.Column(db.JSON,
                           comment='风险评分规则覆盖（为空则用全局默认）')
    status = db.Column(db.Enum('active', 'inactive'),
                       default='active', comment='基线状态')
    created_by = db.Column(db.String(100), default='', comment='创建人')
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    project = db.relationship('Project', backref='engineering_baselines')

    def to_dict(self):
        return {
            'id': self.id,
            'project_id': self.project_id,
            'project_name': self.project.name if self.project else None,
            'name': self.name,
            'repo_url': self.repo_url,
            'branch': self.branch,
            'baseline_commit': self.baseline_commit,
            'architecture_doc_url': self.architecture_doc_url,
            'module_mapping': self.module_mapping or {},
            'risk_rules': self.risk_rules or {},
            'status': self.status,
            'created_by': self.created_by,
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }


class AnalysisRefreshBatch(db.Model):
    """工程分析增量刷新批次 — 一次 from_commit..to_commit 的分析"""
    __tablename__ = 'analysis_refresh_batches'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'),
                           nullable=False, comment='关联项目ID（冗余便于筛选）')
    baseline_id = db.Column(db.Integer,
                            db.ForeignKey('engineering_baselines.id'),
                            nullable=False, comment='所属基线')
    iteration_id = db.Column(db.Integer,
                             db.ForeignKey('test_iterations.id'),
                             comment='可选关联的测试迭代')
    refresh_type = db.Column(db.Enum('manual', 'scheduled', 'webhook', 'direct'),
                             default='manual',
                             comment='触发方式：manual/scheduled/webhook/direct（直接录入结果，跳过 Agent）')
    from_commit = db.Column(db.String(64), comment='起始 commit')
    to_commit = db.Column(db.String(64), comment='结束 commit')
    commit_count = db.Column(db.Integer, default=0, comment='区间内提交数')
    changed_file_count = db.Column(db.Integer, default=0,
                                   comment='变更文件数')
    summary = db.Column(db.Text, comment='AI 输出摘要')
    risk_level = db.Column(db.Enum('low', 'medium', 'high', 'critical'),
                           comment='整体风险等级')
    status = db.Column(db.Enum('queued', 'running', 'success', 'failed',
                               'approved', 'rejected'),
                       default='queued', comment='批次状态')
    assignee_claw_id = db.Column(db.Integer,
                                 db.ForeignKey('openclaw_instances.id'),
                                 comment='执行 Agent（可空）')
    result_payload = db.Column(db.JSON,
                               comment='Agent 回传的原始 JSON 结果')
    error_message = db.Column(db.Text, comment='失败时的错误信息')
    started_at = db.Column(db.DateTime)
    finished_at = db.Column(db.DateTime)
    triggered_by = db.Column(db.String(100), default='', comment='触发人')
    approved_by = db.Column(db.String(100), comment='审批人')
    approved_at = db.Column(db.DateTime, comment='审批时间')
    reject_reason = db.Column(db.Text, comment='驳回原因')
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    baseline = db.relationship('EngineeringBaseline', backref='batches')
    project = db.relationship('Project')
    assignee = db.relationship('OpenClawInstance')

    def to_dict(self, with_details=False):
        data = {
            'id': self.id,
            'project_id': self.project_id,
            'project_name': self.project.name if self.project else None,
            'baseline_id': self.baseline_id,
            'baseline_name': self.baseline.name if self.baseline else None,
            'iteration_id': self.iteration_id,
            'refresh_type': self.refresh_type,
            'from_commit': self.from_commit,
            'to_commit': self.to_commit,
            'commit_count': self.commit_count,
            'changed_file_count': self.changed_file_count,
            'summary': self.summary,
            'risk_level': self.risk_level,
            'status': self.status,
            'assignee_claw_id': self.assignee_claw_id,
            'assignee_claw_name': self.assignee.name if self.assignee else None,
            'error_message': self.error_message,
            'started_at': str(self.started_at) if self.started_at else None,
            'finished_at': str(self.finished_at) if self.finished_at else None,
            'triggered_by': self.triggered_by,
            'approved_by': self.approved_by,
            'approved_at': str(self.approved_at) if self.approved_at else None,
            'reject_reason': self.reject_reason,
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }
        if with_details:
            data['result_payload'] = self.result_payload
            data['change_count'] = EngineeringChangeItem.query.filter_by(
                batch_id=self.id).count()
            data['impact_count'] = EngineeringTestImpactItem.query.filter_by(
                batch_id=self.id).count()
        return data


class EngineeringChangeItem(db.Model):
    """单个文件变更项 — 一次刷新可有多条"""
    __tablename__ = 'engineering_change_items'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    batch_id = db.Column(db.Integer,
                         db.ForeignKey('analysis_refresh_batches.id'),
                         nullable=False)
    file_path = db.Column(db.String(500), nullable=False, comment='变更文件路径')
    change_type = db.Column(db.Enum('add', 'modify', 'delete', 'rename'),
                            default='modify')
    module_id = db.Column(db.Integer, db.ForeignKey('modules.id'),
                          comment='映射到的 Hub 模块ID（可空）')
    module_name = db.Column(db.String(100), default='',
                            comment='模块名（冗余便于筛选）')
    symbol_names = db.Column(db.JSON, comment='涉及的符号名 JSON 数组')
    impact_tags = db.Column(db.JSON,
                            comment='影响标签，如 ["network","login","perf"]')
    risk_score = db.Column(db.Integer, default=0, comment='风险评分 0-100')
    reason = db.Column(db.Text, comment='风险原因说明')
    tapd_story_ids = db.Column(db.JSON,
                               comment='自动解析的 TAPD 需求 ID 列表')

    created_at = db.Column(db.DateTime, default=_now)

    batch = db.relationship('AnalysisRefreshBatch', backref='change_items')
    module = db.relationship('Module')

    def to_dict(self):
        return {
            'id': self.id,
            'batch_id': self.batch_id,
            'file_path': self.file_path,
            'change_type': self.change_type,
            'module_id': self.module_id,
            'module_name': self.module_name or (
                self.module.name if self.module else ''),
            'symbol_names': self.symbol_names or [],
            'impact_tags': self.impact_tags or [],
            'risk_score': self.risk_score,
            'reason': self.reason,
            'tapd_story_ids': self.tapd_story_ids or [],
            'created_at': str(self.created_at) if self.created_at else None,
        }


class EngineeringTestImpactItem(db.Model):
    """用例变更建议 — 由 batch 自动生成，进入审批/认领闭环"""
    __tablename__ = 'engineering_test_impact_items'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    batch_id = db.Column(db.Integer,
                         db.ForeignKey('analysis_refresh_batches.id'),
                         nullable=False)
    library_id = db.Column(db.Integer,
                           db.ForeignKey('test_case_libraries.id'),
                           comment='建议归属用例库（可空）')
    module_id = db.Column(db.Integer, db.ForeignKey('modules.id'),
                          comment='所属模块ID（可空）')
    module_name = db.Column(db.String(100), default='', comment='模块名')
    feature_chain = db.Column(db.String(500), comment='功能链路')
    action_type = db.Column(db.Enum('add_case', 'update_case',
                                    'deprecate_case'),
                            default='update_case', comment='建议动作')
    priority = db.Column(db.Enum('P0', 'P1', 'P2', 'P3'),
                         default='P2', comment='建议优先级')
    suggestion = db.Column(db.Text, comment='建议描述')
    acceptance_criteria = db.Column(db.Text, comment='验收要点')
    owner = db.Column(db.String(100), default='', comment='认领人')
    status = db.Column(db.Enum('todo', 'in_progress', 'done', 'rejected'),
                       default='todo', comment='处理状态')
    linked_test_task_id = db.Column(db.Integer,
                                    db.ForeignKey('test_tasks.id'),
                                    comment='已写入的测试任务ID')
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    batch = db.relationship('AnalysisRefreshBatch', backref='impact_items')
    library = db.relationship('TestCaseLibrary')
    module = db.relationship('Module')

    def to_dict(self, with_links=False):
        data = {
            'id': self.id,
            'batch_id': self.batch_id,
            'library_id': self.library_id,
            'library_name': self.library.name if self.library else None,
            'module_id': self.module_id,
            'module_name': self.module_name or (
                self.module.name if self.module else ''),
            'feature_chain': self.feature_chain,
            'action_type': self.action_type,
            'priority': self.priority,
            'suggestion': self.suggestion,
            'acceptance_criteria': self.acceptance_criteria,
            'owner': self.owner,
            'status': self.status,
            'linked_test_task_id': self.linked_test_task_id,
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }
        if with_links:
            data['linked_cases'] = [
                l.to_dict() for l in EngineeringTestCaseLink.query
                .filter_by(impact_item_id=self.id).all()
            ]
        return data


class EngineeringTestCaseLink(db.Model):
    """用例建议 -> Hub 已有用例 关联"""
    __tablename__ = 'engineering_test_case_links'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    impact_item_id = db.Column(
        db.Integer, db.ForeignKey('engineering_test_impact_items.id'),
        nullable=False)
    test_case_id = db.Column(db.Integer, db.ForeignKey('test_cases.id'),
                             nullable=False)
    link_type = db.Column(db.Enum('affected', 'newly_created', 'replaced'),
                          default='affected',
                          comment='affected=已有用例受影响, newly_created=新建用例, '
                                  'replaced=被新用例替换')
    created_by = db.Column(db.String(100), default='')
    created_at = db.Column(db.DateTime, default=_now)

    impact_item = db.relationship('EngineeringTestImpactItem',
                                  backref='case_links')
    test_case = db.relationship('TestCase')

    __table_args__ = (
        db.UniqueConstraint('impact_item_id', 'test_case_id',
                            name='uq_impact_case_link'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'impact_item_id': self.impact_item_id,
            'test_case_id': self.test_case_id,
            'test_case_title': self.test_case.title if self.test_case else None,
            'test_case_priority': self.test_case.priority
                if self.test_case else None,
            'link_type': self.link_type,
            'created_by': self.created_by,
            'created_at': str(self.created_at) if self.created_at else None,
        }


class EngineeringArchitectureSnapshot(db.Model):
    """工程架构分析快照 — 每条 baseline LRU 保留最新 10 个，超出删最旧。

    支持两种分析方式：
      - scope='full'   全工程级总览（模块清单 / 通信机制 / 关键链路 / 全局架构图）
      - scope='module' 单模块深度分析（针对登录/网络/匹配等单模块出详细子架构 + 时序图）

    内容三件套：
      - structured  结构化 JSON（modules / communications / key_logic / risks / suggestions）
      - content_md  完整 Markdown（嵌入 ```mermaid 三要素图：架构 / 时序 / 流程）
      - summary     1-2 句概述
    """

    __tablename__ = 'engineering_architecture_snapshots'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    baseline_id = db.Column(db.Integer,
                            db.ForeignKey('engineering_baselines.id'),
                            nullable=False, index=True)
    # 冗余字段：避免每次鉴权都要 join engineering_baselines 拿项目归属。
    # 写入时由 baseline.project_id 同步；后台脚本一次性回填历史数据。
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'),
                           nullable=True, index=True,
                           comment='所属项目（冗余自 baseline，便于按项目鉴权/筛选）')

    scope = db.Column(db.Enum('full', 'module'), nullable=False,
                      default='full',
                      comment='full=全工程  module=单模块深度')
    target_module = db.Column(
        db.String(100), default='',
        comment='scope=module 时填模块名，如 "登录"/"网络通信"；'
                'scope=full 时必须为空字符串')

    title = db.Column(db.String(200),
                      comment='快照标题，如 "v1.2.0 全工程总览"')
    summary = db.Column(db.Text, comment='1-2 句概述')
    content_md = db.Column(db.Text,
                           comment='完整 markdown（嵌入 ```mermaid 三要素图）；'
                                   'MariaDB Text 上限约 64KB，超出请拆模块')
    structured = db.Column(
        db.JSON,
        comment='结构化字段：modules / communications / key_logic '
                '/ risks / suggestions')

    analyzed_commit = db.Column(db.String(64),
                                comment='本次分析对应的 commit hash')
    source_type = db.Column(
        db.Enum('agent', 'manual', 'memos_import'),
        default='agent',
        comment='生成来源：agent=AI 自动 / manual=人工录入 / memos_import=Memos 引入')
    triggered_by = db.Column(db.String(100), default='',
                             comment='触发人 / 提交 Agent 名')

    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    baseline = db.relationship('EngineeringBaseline',
                               backref='architecture_snapshots')

    def to_dict(self, with_content=False):
        data = {
            'id': self.id,
            'baseline_id': self.baseline_id,
            'baseline_name': self.baseline.name if self.baseline else None,
            'project_id': self.project_id or (
                self.baseline.project_id if self.baseline else None),
            'scope': self.scope,
            'target_module': self.target_module or '',
            'title': self.title or '',
            'summary': self.summary or '',
            'analyzed_commit': self.analyzed_commit,
            'source_type': self.source_type,
            'triggered_by': self.triggered_by,
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }
        if with_content:
            data['content_md'] = self.content_md or ''
            data['structured'] = self.structured or {}
        else:
            # 列表场景：仅给前 200 字预览，避免响应膨胀
            md = self.content_md or ''
            data['content_preview'] = md[:200] + ('...' if len(md) > 200 else '')
        return data


class EngineeringShare(db.Model):
    """工程分析结果共享授权（跨项目临时开放）。

    设计要点：
    - 一张表覆盖三类资源（baseline/batch/snapshot），通过 (resource_type, resource_id) 定位
    - share_type='public' → target_user_id / target_claw_id 都为空（全部登录用户/claw 可见）
    - share_type='user'   → 授权给某个用户；该用户名下的 OpenClaw 自动继承（_user_managed_claw_ids）
    - share_type='claw'   → 单独授权给某个 OpenClaw 实例
    - expires_at 可空：空 = 永久（手动撤销）；有值 = 到期自动失效（鉴权时过滤）
    - granted_by 记审计；note 记原因（如 "课题 #45 临时讨论"）
    """

    __tablename__ = 'engineering_shares'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    resource_type = db.Column(
        db.Enum('baseline', 'batch', 'snapshot'),
        nullable=False, comment='资源类型')
    resource_id = db.Column(db.Integer, nullable=False, comment='资源 ID')

    share_type = db.Column(
        db.Enum('user', 'claw', 'public'),
        nullable=False, comment='授权类型')
    target_user_id = db.Column(
        db.Integer, db.ForeignKey('users.id'),
        nullable=True, comment='被授权用户（share_type=user）')
    target_claw_id = db.Column(
        db.Integer, db.ForeignKey('openclaw_instances.id'),
        nullable=True, comment='被授权 OpenClaw（share_type=claw）')

    granted_by = db.Column(db.String(100), default='',
                           comment='授权人 username')
    note = db.Column(db.String(500), default='', comment='授权原因/备注')
    expires_at = db.Column(db.DateTime,
                           comment='过期时间；为空表示永久（手动撤销）')

    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    target_user = db.relationship('User', foreign_keys=[target_user_id])
    target_claw = db.relationship('OpenClawInstance',
                                  foreign_keys=[target_claw_id])

    __table_args__ = (
        db.Index('ix_share_resource', 'resource_type', 'resource_id'),
        db.Index('ix_share_user', 'share_type', 'target_user_id'),
        db.Index('ix_share_claw', 'share_type', 'target_claw_id'),
    )

    def is_active(self, now=None):
        if not self.expires_at:
            return True
        return (now or _now()) < self.expires_at

    def to_dict(self):
        return {
            'id': self.id,
            'resource_type': self.resource_type,
            'resource_id': self.resource_id,
            'share_type': self.share_type,
            'target_user_id': self.target_user_id,
            'target_user_name': (self.target_user.display_name
                                 or self.target_user.username)
                                 if self.target_user else None,
            'target_claw_id': self.target_claw_id,
            'target_claw_name': self.target_claw.name
                                if self.target_claw else None,
            'granted_by': self.granted_by,
            'note': self.note or '',
            'expires_at': str(self.expires_at) if self.expires_at else None,
            'is_active': self.is_active(),
            'created_at': str(self.created_at) if self.created_at else None,
        }


# ---------------------------------------------------------------------------
# 用例库共享授权 / 评审记录（参考 EngineeringShare 设计，资源固定为 library）
# ---------------------------------------------------------------------------


class TestCaseLibraryShare(db.Model):
    """用例库共享授权（"开放权限+邀请评审"）。

    设计要点：
    - 仅一种资源类型：用例库（test_case_libraries）→ 不引入 resource_type
    - share_type='public' → target_user_id / target_claw_id 都为空（全部登录用户/claw 可见）
    - share_type='user'   → 授权给某个用户；该用户名下的 OpenClaw 自动继承
    - share_type='claw'   → 单独授权给某个 OpenClaw 实例
    - permission='reviewer' → 在 readonly 之外可对评审发表评论/审批意见
                              （MVP-2 只读 + 评审参与；editor 留口子）
    - expires_at 可空：空 = 永久（手动撤销）
    """

    __tablename__ = 'test_case_library_shares'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    library_id = db.Column(db.Integer,
                           db.ForeignKey('test_case_libraries.id'),
                           nullable=False, comment='用例库 ID')
    share_type = db.Column(
        db.Enum('user', 'claw', 'public'),
        nullable=False, comment='授权类型')
    target_user_id = db.Column(
        db.Integer, db.ForeignKey('users.id'),
        nullable=True, comment='被授权用户（share_type=user）')
    target_claw_id = db.Column(
        db.Integer, db.ForeignKey('openclaw_instances.id'),
        nullable=True, comment='被授权 OpenClaw（share_type=claw）')
    permission = db.Column(
        db.String(20), default='reviewer',
        comment='权限：readonly=只读 / reviewer=只读+可评审 / editor=可编辑（预留）')
    granted_by = db.Column(db.String(100), default='',
                           comment='授权人 username')
    note = db.Column(db.String(500), default='', comment='授权原因/备注')
    expires_at = db.Column(db.DateTime,
                           comment='过期时间；为空表示永久（手动撤销）')

    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    library = db.relationship('TestCaseLibrary',
                              foreign_keys=[library_id])
    target_user = db.relationship('User', foreign_keys=[target_user_id])
    target_claw = db.relationship('OpenClawInstance',
                                  foreign_keys=[target_claw_id])

    __table_args__ = (
        db.Index('ix_tcl_share_lib', 'library_id'),
        db.Index('ix_tcl_share_user', 'share_type', 'target_user_id'),
        db.Index('ix_tcl_share_claw', 'share_type', 'target_claw_id'),
    )

    def is_active(self, now=None):
        if not self.expires_at:
            return True
        return (now or _now()) < self.expires_at

    def to_dict(self):
        return {
            'id': self.id,
            'library_id': self.library_id,
            'share_type': self.share_type,
            'target_user_id': self.target_user_id,
            'target_user_name': ((self.target_user.display_name
                                  or self.target_user.username)
                                 if self.target_user else None),
            'target_claw_id': self.target_claw_id,
            'target_claw_name': (self.target_claw.name
                                 if self.target_claw else None),
            'permission': self.permission or 'reviewer',
            'granted_by': self.granted_by,
            'note': self.note or '',
            'expires_at': str(self.expires_at) if self.expires_at else None,
            'is_active': self.is_active(),
            'created_at': str(self.created_at) if self.created_at else None,
        }


class TestCaseLibraryReview(db.Model):
    """用例库评审记录（一次发起 = 一条记录，全程留痕）。

    生命周期：
        submitted（pending_review） →
            approved   完成
            rejected   被驳回（可再次提交）
            withdrawn  发起方主动撤回
    """

    __tablename__ = 'test_case_library_reviews'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    library_id = db.Column(db.Integer,
                           db.ForeignKey('test_case_libraries.id'),
                           nullable=False, comment='用例库 ID')
    status = db.Column(
        db.String(20), default='submitted',
        comment='状态：submitted/approved/rejected/withdrawn')

    submitted_by = db.Column(db.String(100), default='',
                             comment='发起人 username/claw_name')
    submitted_at = db.Column(db.DateTime, default=_now,
                             comment='发起时间')
    submit_note = db.Column(db.Text, default='',
                            comment='发起说明（本次评审范围/重点）')
    scope_summary = db.Column(db.String(500), default='',
                              comment='评审范围简述（前端冗余展示）')
    # 评审范围（结构化）：library=整库 / module=某子目录（含子树）/ cases=多选用例（预留）
    scope_type = db.Column(db.String(20), default='library',
                           comment='评审范围类型：library/module/cases')
    scope_module_path = db.Column(
        db.String(500), default='',
        comment='评审范围模块路径（scope_type=module 时必填，按前缀匹配子树）')
    scope_case_count = db.Column(
        db.Integer, default=0,
        comment='本次评审覆盖的用例数（发起时快照）')
    invited_reviewers = db.Column(
        db.JSON,
        comment='本次邀请的评审人快照：[{type:user/claw, id, name}]')

    decided_by = db.Column(db.String(100),
                           comment='审批人 username/claw_name')
    decided_at = db.Column(db.DateTime,
                           comment='审批时间')
    decision_note = db.Column(db.Text, default='',
                              comment='审批意见')
    related_topic_id = db.Column(db.Integer,
                                 comment='关联的 case_review topic ID（可选）')

    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    library = db.relationship('TestCaseLibrary',
                              foreign_keys=[library_id])

    __table_args__ = (
        db.Index('ix_tcl_review_lib', 'library_id'),
        db.Index('ix_tcl_review_status', 'status'),
        db.Index('ix_tcl_review_lib_status', 'library_id', 'status'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'library_id': self.library_id,
            'status': self.status,
            'submitted_by': self.submitted_by or '',
            'submitted_at': str(self.submitted_at) if self.submitted_at else None,
            'submit_note': self.submit_note or '',
            'scope_summary': self.scope_summary or '',
            'scope_type': self.scope_type or 'library',
            'scope_module_path': self.scope_module_path or '',
            'scope_case_count': int(self.scope_case_count or 0),
            'invited_reviewers': self.invited_reviewers or [],
            'decided_by': self.decided_by or '',
            'decided_at': str(self.decided_at) if self.decided_at else None,
            'decision_note': self.decision_note or '',
            'related_topic_id': self.related_topic_id,
            'created_at': str(self.created_at) if self.created_at else None,
        }


# ---------------------------------------------------------------------------
# 测试账号管理（QQ / 微信 / ...）
# ---------------------------------------------------------------------------


class TestAccount(db.Model):
    """测试账号池：供 OpenClaw 调度领用 / 释放。

    设计要点：
    - 区分 platform（qq / wechat / 其他）+ account 唯一
    - 状态机：idle <-> in_use；任何时刻可被标记 abnormal
    - 领用时记录 current_user（claw 名或 web 用户名）+ current_purpose（备注）
    - 软删除：is_deleted=True 视为已下架
    """

    __tablename__ = 'test_accounts'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    platform = db.Column(db.Enum('qq', 'wechat', 'other'),
                         nullable=False, default='qq',
                         comment='账号平台：qq / wechat / other')
    account = db.Column(db.String(100), nullable=False, comment='账号')
    password = db.Column(db.String(255), nullable=False, comment='密码（明文）')
    status = db.Column(db.Enum('idle', 'in_use', 'abnormal'),
                       nullable=False, default='idle',
                       comment='状态：idle=空闲, in_use=使用中, abnormal=异常')
    holder_name = db.Column(db.String(100), default='',
                            comment='当前使用人（claw 名或 web 用户名）')
    current_claw_id = db.Column(db.Integer,
                                db.ForeignKey('openclaw_instances.id'),
                                comment='当前占用的 OpenClaw ID（如果是 claw 领用）')
    current_purpose = db.Column(db.String(500), default='',
                                comment='当前使用途径/备注（acquire 时上报）')
    last_login_at = db.Column(db.DateTime, comment='最近一次领用/登录时间')
    lease_ttl_seconds = db.Column(db.Integer, default=1800,
                                  comment='当前领用租期秒数')
    lease_expires_at = db.Column(db.DateTime, index=True,
                                 comment='当前领用到期时间')
    lease_heartbeat_at = db.Column(db.DateTime,
                                   comment='最近一次续租时间')
    workflow_run_id = db.Column(db.Integer, index=True,
                                comment='可选：关联 Workflow Run，不建外键以保留历史')
    controller_run_id = db.Column(db.String(160), index=True,
                                  comment='可选：Codex controller run ID')
    notes = db.Column(db.Text, default='', comment='账号备注（账号自身固有备注）')

    is_deleted = db.Column(db.Boolean, default=False,
                           comment='软删除标记：True=已删除（隐藏）')
    deleted_at = db.Column(db.DateTime, comment='软删除时间')
    deleted_by = db.Column(db.String(100), default='', comment='删除人')

    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)
    created_by = db.Column(db.String(100), default='', comment='创建人')

    __table_args__ = (
        db.UniqueConstraint('platform', 'account', name='uq_platform_account'),
    )

    def to_dict(self, include_password=False):
        data = {
            'id': self.id,
            'platform': self.platform,
            'account': self.account,
            'status': self.status,
            'current_user': self.holder_name or '',
            'holder_name': self.holder_name or '',
            'current_claw_id': self.current_claw_id,
            'current_purpose': self.current_purpose or '',
            'last_login_at': str(self.last_login_at) if self.last_login_at else None,
            'lease_ttl_seconds': self.lease_ttl_seconds,
            'lease_expires_at': (
                str(self.lease_expires_at) if self.lease_expires_at else None),
            'lease_heartbeat_at': (
                str(self.lease_heartbeat_at) if self.lease_heartbeat_at else None),
            'workflow_run_id': self.workflow_run_id,
            'controller_run_id': self.controller_run_id,
            'notes': self.notes or '',
            'created_by': self.created_by or '',
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
            'is_deleted': bool(self.is_deleted),
        }
        if include_password:
            data['password'] = self.password or ''
        else:
            data['password_present'] = bool(self.password)
        return data


class TestAccountUsageLog(db.Model):
    """测试账号使用流水 — 每次 acquire / release / mark_abnormal 留痕。"""

    __tablename__ = 'test_account_usage_logs'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    account_id = db.Column(db.Integer, db.ForeignKey('test_accounts.id'),
                           nullable=False, index=True)
    action = db.Column(db.Enum('acquire', 'renew', 'expire', 'release',
                               'mark_abnormal', 'recover', 'create', 'delete'),
                       nullable=False, comment='动作类型')
    actor_type = db.Column(db.Enum('claw', 'user', 'system'),
                           nullable=False, default='claw',
                           comment='动作发起方类型')
    actor_name = db.Column(db.String(100), default='', comment='发起方名（claw 名或用户名）')
    actor_claw_id = db.Column(db.Integer,
                              db.ForeignKey('openclaw_instances.id'),
                              comment='发起方 claw id（如果是 claw）')
    actor_user_id = db.Column(db.Integer,
                              db.ForeignKey('users.id'),
                              comment='发起方 user id（如果是 web user）')
    purpose = db.Column(db.String(500), default='',
                        comment='使用途径/备注（acquire 必填）')
    extra = db.Column(db.JSON, comment='附加信息：异常原因、释放摘要等')
    created_at = db.Column(db.DateTime, default=_now, index=True)

    account = db.relationship('TestAccount',
                              backref=db.backref(
                                  'usage_logs',
                                  lazy='dynamic',
                                  cascade='all, delete-orphan'))

    def to_dict(self):
        return {
            'id': self.id,
            'account_id': self.account_id,
            'action': self.action,
            'actor_type': self.actor_type,
            'actor_name': self.actor_name or '',
            'actor_claw_id': self.actor_claw_id,
            'actor_user_id': self.actor_user_id,
            'purpose': self.purpose or '',
            'extra': self.extra or {},
            'created_at': str(self.created_at) if self.created_at else None,
        }


# ============================================================
# 需求分析中心（Requirement Analysis Center）
# ============================================================
# 数据来源：OpenClaw Agent 通过 mcporter-internal 调用 TAPD MCP，
# 把需求快照 / 版本基线 / 字段映射 POST 到 Hub 落库。Hub 端零 TAPD 凭证。
#
# 字段对齐规范见 plan「七·B、TAPD 字段映射规范」与 skills/requirement-analysis/SKILL.md
# ============================================================


class RequirementItem(db.Model):
    """TAPD Story 本地快照（每条需求一行，与 TestIteration 强 1:1 关联）"""
    __tablename__ = 'requirement_items'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    iteration_id = db.Column(db.Integer,
                             db.ForeignKey('test_iterations.id'),
                             nullable=False, index=True,
                             comment='所属 Hub TestIteration')

    # TAPD 主键
    tapd_story_id = db.Column(db.String(64), nullable=False, index=True,
                              comment='TAPD Story.id')
    tapd_workspace_id = db.Column(db.String(50), index=True)
    tapd_iteration_id = db.Column(db.String(64), index=True,
                                  comment='TAPD Story.iteration_id')

    # 基础字段
    title = db.Column(db.String(500), default='', comment='TAPD Story.name')
    description = db.Column(db.Text, comment='TAPD Story.description（HTML）')
    description_text = db.Column(db.Text,
                                 comment='description 抽出的纯文本副本，给 LLM 用')
    status = db.Column(db.String(40), index=True,
                       comment='TAPD status key，如 status_3/status_22，UI 翻译用字典')
    priority_label = db.Column(db.String(40),
                               comment='High/Middle/Low/Nice To Have')
    priority_num = db.Column(db.Integer, default=0)

    # 人员
    owner = db.Column(db.String(500), default='',
                      comment='处理人，可能 ; 分隔多人')
    creator = db.Column(db.String(100), default='')
    developer = db.Column(db.String(500), default='')

    # 分类与模块
    category_id = db.Column(db.String(64), comment='TAPD 需求分类 ID')
    workitem_type_id = db.Column(db.String(64), comment='TAPD 需求类别')
    tapd_module = db.Column(db.String(200), default='',
                            comment='TAPD Story.module')
    feature = db.Column(db.String(200), default='',
                        comment='TAPD Story.feature')
    local_module_name = db.Column(db.String(200), default='',
                                  comment='映射后的 testcase-manager 一级模块')

    # 版本与基线
    tapd_version = db.Column(db.String(100), index=True,
                             comment='TAPD Story.version 字符串值，如 M1版本')
    tapd_release_id = db.Column(db.String(64), index=True,
                                comment='TAPD Story.release_id')
    tapd_baseline_id = db.Column(db.String(64), index=True,
                                 comment='所属转测基线（agent 推送时关联）')

    # 测试相关 custom_field
    acceptance_criteria = db.Column(db.Text,
                                    comment='custom_field_eight 测试验收')
    test_focus = db.Column(db.Text,
                           comment='test_focus + custom_field_three 测试执行')
    test_suggestions = db.Column(db.Text,
                                 comment='测试点建议（JSON 字符串）')
    test_result = db.Column(db.Text, comment='custom_field_six 测试结果')
    need_test = db.Column(db.String(40),
                          comment='custom_field_18 是否需要测试')
    review_progress = db.Column(db.String(40),
                                comment='custom_field_19 评审进度')

    # 层级
    parent_id = db.Column(db.String(64), comment='TAPD parent_id，0 表示无')
    children_id = db.Column(db.String(500), comment='TAPD children_id，| 分隔')
    tree_path = db.Column(db.String(500), comment='TAPD path')

    # 进度工时
    progress = db.Column(db.Integer, default=0)
    effort = db.Column(db.Float, default=0)
    effort_completed = db.Column(db.Float, default=0)
    remain = db.Column(db.Float, default=0)
    tech_risk = db.Column(db.String(200))

    # 时间（TAPD 原值）
    tapd_created_at = db.Column(db.DateTime, comment='TAPD created')
    tapd_modified_at = db.Column(db.DateTime, comment='TAPD modified')
    tapd_completed_at = db.Column(db.DateTime, comment='TAPD completed')
    tapd_begin = db.Column(db.Date)
    tapd_due = db.Column(db.Date)

    # 本地分析增强
    risk_score = db.Column(db.Integer, default=0,
                           comment='LLM 评估风险 0-100')
    risk_level = db.Column(db.String(20), default='low',
                           comment='low/medium/high/critical')
    local_test_status = db.Column(db.String(20), default='pending',
                                  comment='pending/case_designed/testing/passed/blocked')
    local_synced_at = db.Column(db.DateTime, default=_now,
                                comment='最近一次 agent 推送同步时间')

    # 实现状态（Agent 基于工程代码分析判断）
    impl_status = db.Column(db.String(20), default='unknown',
                            comment='未实现not_impl/实现中in_progress/已实现implemented/unknown')
    impl_remark = db.Column(db.String(500), default='',
                            comment='实现状态备注，如：服务端已完成，客户端还没完成')

    # 完整度（Agent 基于需求文档质量评估）
    completeness = db.Column(db.String(20), default='',
                             comment='完整度：极高/高/普通/较低')
    completeness_desc = db.Column(db.Text,
                                  comment='完整度说明（Markdown 格式）')

    # 兜底全字段
    raw_payload = db.Column(db.JSON,
                            comment='完整 TAPD Story JSON，便于扩展不 ALTER')

    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    iteration = db.relationship('TestIteration',
                                backref=db.backref('requirement_items',
                                                   lazy='dynamic',
                                                   cascade='all, delete-orphan'))

    __table_args__ = (
        db.UniqueConstraint('iteration_id', 'tapd_story_id',
                            name='uq_req_item_iter_story'),
    )

    def to_dict(self, with_raw=False):
        data = {
            'id': self.id,
            'iteration_id': self.iteration_id,
            'tapd_story_id': self.tapd_story_id,
            'tapd_workspace_id': self.tapd_workspace_id,
            'tapd_iteration_id': self.tapd_iteration_id,
            'title': self.title or '',
            'description': self.description,
            'description_text': self.description_text,
            'status': self.status,
            'priority_label': self.priority_label,
            'priority_num': self.priority_num or 0,
            'owner': self.owner or '',
            'creator': self.creator or '',
            'developer': self.developer or '',
            'category_id': self.category_id,
            'workitem_type_id': self.workitem_type_id,
            'tapd_module': self.tapd_module or '',
            'feature': self.feature or '',
            'local_module_name': self.local_module_name or '',
            'tapd_version': self.tapd_version,
            'tapd_release_id': self.tapd_release_id,
            'tapd_baseline_id': self.tapd_baseline_id,
            'acceptance_criteria': self.acceptance_criteria,
            'test_focus': self.test_focus,
            'test_suggestions': self.test_suggestions,
            'test_result': self.test_result,
            'need_test': self.need_test,
            'review_progress': self.review_progress,
            'parent_id': self.parent_id,
            'children_id': self.children_id,
            'tree_path': self.tree_path,
            'progress': self.progress or 0,
            'effort': self.effort or 0,
            'effort_completed': self.effort_completed or 0,
            'remain': self.remain or 0,
            'tech_risk': self.tech_risk,
            'tapd_created_at': str(self.tapd_created_at) if self.tapd_created_at else None,
            'tapd_modified_at': str(self.tapd_modified_at) if self.tapd_modified_at else None,
            'tapd_completed_at': str(self.tapd_completed_at) if self.tapd_completed_at else None,
            'tapd_begin': str(self.tapd_begin) if self.tapd_begin else None,
            'tapd_due': str(self.tapd_due) if self.tapd_due else None,
            'risk_score': self.risk_score or 0,
            'risk_level': self.risk_level or 'low',
            'local_test_status': self.local_test_status or 'pending',
            'impl_status': self.impl_status or 'unknown',
            'impl_remark': self.impl_remark or '',
            'completeness': self.completeness or '',
            'completeness_desc': self.completeness_desc or '',
            'local_synced_at': str(self.local_synced_at) if self.local_synced_at else None,
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }
        if with_raw:
            data['raw_payload'] = self.raw_payload or {}
        return data


class RequirementReviewVerdict(db.Model):
    """需求逐条评审结论：把需求分析岗的判断固化为可交接产物。"""
    __tablename__ = 'requirement_review_verdicts'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    requirement_item_id = db.Column(db.Integer,
                                    db.ForeignKey('requirement_items.id'),
                                    nullable=False, index=True)
    iteration_id = db.Column(db.Integer,
                             db.ForeignKey('test_iterations.id'),
                             nullable=False, index=True)
    review_key = db.Column(db.String(80), nullable=False, default='default',
                           comment='评审批次键：workflow run id 或 default')
    verdict = db.Column(db.String(20), nullable=False, default='pass',
                        comment='pass/problem/risk/not_testable')
    risk_level = db.Column(db.String(20), default='low',
                           comment='low/medium/high/critical')
    testability = db.Column(db.String(20), default='testable',
                            comment='testable/unclear/not_testable')
    issues_json = db.Column(db.JSON, comment='评审问题列表')
    summary = db.Column(db.Text)
    reviewer_name = db.Column(db.String(100), default='')
    reviewer_claw_id = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    item = db.relationship('RequirementItem', backref=db.backref(
        'review_verdicts', lazy='dynamic', cascade='all, delete-orphan'))
    iteration = db.relationship('TestIteration', backref=db.backref(
        'requirement_review_verdicts', lazy='dynamic', cascade='all, delete-orphan'))

    __table_args__ = (
        db.UniqueConstraint('requirement_item_id', 'review_key',
                            name='uq_req_review_verdict_item_key'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'requirement_item_id': self.requirement_item_id,
            'iteration_id': self.iteration_id,
            'review_key': self.review_key or 'default',
            'verdict': self.verdict or 'pass',
            'risk_level': self.risk_level or 'low',
            'testability': self.testability or 'testable',
            'issues': self.issues_json or [],
            'summary': self.summary or '',
            'reviewer_name': self.reviewer_name or '',
            'reviewer_claw_id': self.reviewer_claw_id,
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }


class RequirementChangeLog(db.Model):
    """需求字段级变更日志（每日 diff 自动产生）"""
    __tablename__ = 'requirement_change_logs'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    requirement_item_id = db.Column(db.Integer,
                                    db.ForeignKey('requirement_items.id'),
                                    nullable=False, index=True)
    iteration_id = db.Column(db.Integer,
                             db.ForeignKey('test_iterations.id'),
                             nullable=False, index=True,
                             comment='冗余迭代 ID 便于按迭代查')

    change_date = db.Column(db.Date, default=lambda: _now().date(), index=True)
    change_type = db.Column(db.String(20), nullable=False,
                            comment='added/modified/status_changed/removed')
    field_name = db.Column(db.String(80), default='',
                           comment='变更字段名，如 status/title/acceptance_criteria')
    old_value = db.Column(db.Text)
    new_value = db.Column(db.Text)
    diff_summary = db.Column(db.Text, comment='LLM 生成 1 句话摘要（可选）')
    impact_level = db.Column(db.String(10), default='low',
                             comment='low/medium/high，决定是否触发用例重评')
    processed = db.Column(db.Boolean, default=False,
                          comment='是否已被处理为用例变更建议')
    created_at = db.Column(db.DateTime, default=_now, index=True)

    item = db.relationship('RequirementItem',
                           backref=db.backref('change_logs',
                                              lazy='dynamic',
                                              cascade='all, delete-orphan'))

    __table_args__ = (
        db.UniqueConstraint('requirement_item_id', 'change_date',
                            'field_name', 'change_type',
                            name='uq_req_change_dedup'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'requirement_item_id': self.requirement_item_id,
            'iteration_id': self.iteration_id,
            'change_date': str(self.change_date) if self.change_date else None,
            'change_type': self.change_type,
            'field_name': self.field_name or '',
            'old_value': self.old_value,
            'new_value': self.new_value,
            'diff_summary': self.diff_summary,
            'impact_level': self.impact_level or 'low',
            'processed': bool(self.processed),
            'created_at': str(self.created_at) if self.created_at else None,
        }


class RequirementEngineeringLink(db.Model):
    """需求 ↔ 工程变更关联（自动 by tapd_story_id 或手动）"""
    __tablename__ = 'requirement_engineering_links'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    requirement_item_id = db.Column(db.Integer,
                                    db.ForeignKey('requirement_items.id'),
                                    nullable=False, index=True)
    change_item_id = db.Column(db.Integer,
                               db.ForeignKey('engineering_change_items.id'),
                               nullable=False, index=True)
    link_source = db.Column(db.String(20), default='auto_tapd_id',
                            comment='auto_tapd_id / manual / llm_inferred')
    confidence = db.Column(db.Integer, default=90,
                           comment='自动关联可信度 0-100')
    created_at = db.Column(db.DateTime, default=_now)

    item = db.relationship('RequirementItem',
                           backref=db.backref('engineering_links',
                                              lazy='dynamic',
                                              cascade='all, delete-orphan'))

    __table_args__ = (
        db.UniqueConstraint('requirement_item_id', 'change_item_id',
                            name='uq_req_eng_link'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'requirement_item_id': self.requirement_item_id,
            'change_item_id': self.change_item_id,
            'link_source': self.link_source or 'auto_tapd_id',
            'confidence': self.confidence or 0,
            'created_at': str(self.created_at) if self.created_at else None,
        }


class RequirementFunctionLink(db.Model):
    """需求 ↔ 函数/方法级工程关联（包含文件信息）"""
    __tablename__ = 'requirement_function_links'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    requirement_item_id = db.Column(db.Integer,
                                    db.ForeignKey('requirement_items.id'),
                                    nullable=False, index=True)
    change_item_id = db.Column(db.Integer,
                               db.ForeignKey('engineering_change_items.id'),
                               nullable=False, index=True)
    file_path = db.Column(db.String(500), nullable=False, default='',
                          comment='变更文件路径（冗余存储，便于查询）')
    symbol_name = db.Column(db.String(120), nullable=False, default='',
                            comment='函数/方法/符号名')
    start_line = db.Column(db.Integer, comment='函数起始行号（可空）')
    end_line = db.Column(db.Integer, comment='函数结束行号（可空）')
    link_source = db.Column(db.String(20), default='auto_symbol',
                            comment='auto_symbol/manual/llm_inferred')
    confidence = db.Column(db.Integer, default=90,
                           comment='自动关联可信度 0-100')
    created_at = db.Column(db.DateTime, default=_now)

    item = db.relationship('RequirementItem',
                           backref=db.backref('function_links',
                                              lazy='dynamic',
                                              cascade='all, delete-orphan'))

    __table_args__ = (
        db.UniqueConstraint('requirement_item_id', 'change_item_id', 'symbol_name',
                            name='uq_req_func_link'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'requirement_item_id': self.requirement_item_id,
            'change_item_id': self.change_item_id,
            'file_path': self.file_path or '',
            'symbol_name': self.symbol_name or '',
            'start_line': self.start_line,
            'end_line': self.end_line,
            'link_source': self.link_source or 'auto_symbol',
            'confidence': self.confidence or 0,
            'created_at': str(self.created_at) if self.created_at else None,
        }


class RequirementTestcaseLink(db.Model):
    """需求 ↔ 已有测试用例关联"""
    __tablename__ = 'requirement_testcase_links'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    requirement_item_id = db.Column(db.Integer,
                                    db.ForeignKey('requirement_items.id'),
                                    nullable=False, index=True)
    test_case_id = db.Column(db.Integer,
                             db.ForeignKey('test_cases.id'),
                             nullable=False, index=True)
    link_type = db.Column(db.String(20), default='covers',
                          comment='covers/partial/new_required/outdated')
    coverage_status = db.Column(db.String(20), default='covered',
                                comment='covered/gap/outdated')
    created_by = db.Column(db.String(100), default='')
    created_at = db.Column(db.DateTime, default=_now)

    item = db.relationship('RequirementItem',
                           backref=db.backref('testcase_links',
                                              lazy='dynamic',
                                              cascade='all, delete-orphan'))

    __table_args__ = (
        db.UniqueConstraint('requirement_item_id', 'test_case_id',
                            name='uq_req_case_link'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'requirement_item_id': self.requirement_item_id,
            'test_case_id': self.test_case_id,
            'link_type': self.link_type or 'covers',
            'coverage_status': self.coverage_status or 'covered',
            'created_by': self.created_by or '',
            'created_at': str(self.created_at) if self.created_at else None,
        }


class TapdVersion(db.Model):
    """TAPD 原生版本缓存（外发版本，对应"M1版本/M2版本"等）"""
    __tablename__ = 'tapd_versions'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'),
                           index=True)
    tapd_workspace_id = db.Column(db.String(50), nullable=False, index=True)
    tapd_version_id = db.Column(db.String(64), nullable=False,
                                comment='TAPD Version.id')

    name = db.Column(db.String(200), default='')
    description = db.Column(db.Text)
    status = db.Column(db.String(40), comment='Unclosed/Closed')
    version_type = db.Column(db.String(40), default='Normal version')

    start = db.Column(db.Date)
    due = db.Column(db.Date)
    realbegin = db.Column(db.Date)
    realend = db.Column(db.Date)
    testtime = db.Column(db.Date)
    releasetime = db.Column(db.Date)

    creator = db.Column(db.String(100), default='')
    owner = db.Column(db.String(500), default='')
    tapd_created_at = db.Column(db.DateTime)
    tapd_modified_at = db.Column(db.DateTime)
    local_synced_at = db.Column(db.DateTime, default=_now)

    __table_args__ = (
        db.UniqueConstraint('tapd_workspace_id', 'tapd_version_id',
                            name='uq_tapd_version'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'project_id': self.project_id,
            'tapd_workspace_id': self.tapd_workspace_id,
            'tapd_version_id': self.tapd_version_id,
            'name': self.name or '',
            'description': self.description,
            'status': self.status,
            'version_type': self.version_type or 'Normal version',
            'start': str(self.start) if self.start else None,
            'due': str(self.due) if self.due else None,
            'realbegin': str(self.realbegin) if self.realbegin else None,
            'realend': str(self.realend) if self.realend else None,
            'testtime': str(self.testtime) if self.testtime else None,
            'releasetime': str(self.releasetime) if self.releasetime else None,
            'creator': self.creator or '',
            'owner': self.owner or '',
            'tapd_created_at': str(self.tapd_created_at) if self.tapd_created_at else None,
            'tapd_modified_at': str(self.tapd_modified_at) if self.tapd_modified_at else None,
            'local_synced_at': str(self.local_synced_at) if self.local_synced_at else None,
        }


class TapdBaseline(db.Model):
    """TAPD 原生基线缓存（一次转测内容快照）"""
    __tablename__ = 'tapd_baselines'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    tapd_workspace_id = db.Column(db.String(50), nullable=False, index=True)
    tapd_baseline_id = db.Column(db.String(64), nullable=False,
                                 comment='TAPD Baseline.id')
    tapd_version_id_str = db.Column(db.String(64), index=True,
                                    comment='TAPD Baseline.version_id 原值')
    version_id = db.Column(db.Integer, db.ForeignKey('tapd_versions.id'),
                           comment='本地 tapd_versions FK，可空（version 还未同步时）')

    name = db.Column(db.String(200), default='')
    creator = db.Column(db.String(100), default='')
    tapd_created_at = db.Column(db.DateTime)

    story_count = db.Column(db.Integer, default=0,
                            comment='基线下需求数')
    stories_snapshot = db.Column(db.JSON,
                                 comment='["story_id1", ...] 这次转测包含的需求 ID 列表')
    local_synced_at = db.Column(db.DateTime, default=_now)

    version = db.relationship('TapdVersion',
                              backref=db.backref('baselines',
                                                 lazy='dynamic'))

    __table_args__ = (
        db.UniqueConstraint('tapd_workspace_id', 'tapd_baseline_id',
                            name='uq_tapd_baseline'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'tapd_workspace_id': self.tapd_workspace_id,
            'tapd_baseline_id': self.tapd_baseline_id,
            'tapd_version_id_str': self.tapd_version_id_str,
            'version_id': self.version_id,
            'name': self.name or '',
            'creator': self.creator or '',
            'tapd_created_at': str(self.tapd_created_at) if self.tapd_created_at else None,
            'story_count': self.story_count or 0,
            'stories_snapshot': self.stories_snapshot or [],
            'local_synced_at': str(self.local_synced_at) if self.local_synced_at else None,
        }


class TapdIterationsCache(db.Model):
    """TAPD 迭代下拉缓存（agent 推送维护，前端永远查这张表）"""
    __tablename__ = 'tapd_iterations_cache'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    tapd_workspace_id = db.Column(db.String(50), nullable=False, index=True)
    tapd_iteration_id = db.Column(db.String(64), nullable=False,
                                  comment='TAPD Iteration.id')

    name = db.Column(db.String(200), default='')
    status = db.Column(db.String(40), index=True,
                       comment='TAPD 原值，如 open/done')
    startdate = db.Column(db.Date)
    enddate = db.Column(db.Date)
    creator = db.Column(db.String(100), default='')
    description = db.Column(db.Text)
    parent_id = db.Column(db.String(64))

    last_synced_at = db.Column(db.DateTime, default=_now, index=True)
    cache_version = db.Column(db.Integer, default=1,
                              comment='每次 upsert +1，前端轮询用')

    __table_args__ = (
        db.UniqueConstraint('tapd_workspace_id', 'tapd_iteration_id',
                            name='uq_tapd_iter_cache'),
        db.Index('ix_tapd_iter_ws_status', 'tapd_workspace_id', 'status'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'tapd_workspace_id': self.tapd_workspace_id,
            'tapd_iteration_id': self.tapd_iteration_id,
            'name': self.name or '',
            'status': self.status,
            'startdate': str(self.startdate) if self.startdate else None,
            'enddate': str(self.enddate) if self.enddate else None,
            'creator': self.creator or '',
            'description': self.description,
            'parent_id': self.parent_id,
            'last_synced_at': str(self.last_synced_at) if self.last_synced_at else None,
            'cache_version': self.cache_version or 1,
        }


class TapdFieldMapCache(db.Model):
    """自定义字段中英映射缓存（不同 workspace custom_field_* 含义不同）"""
    __tablename__ = 'tapd_field_map_cache'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    tapd_workspace_id = db.Column(db.String(50), nullable=False, index=True)
    entity_type = db.Column(db.String(20), nullable=False,
                            default='story',
                            comment='story/bug/task')
    field_map = db.Column(db.JSON,
                          comment='{"custom_field_eight": "测试验收", ...}')
    last_synced_at = db.Column(db.DateTime, default=_now)

    __table_args__ = (
        db.UniqueConstraint('tapd_workspace_id', 'entity_type',
                            name='uq_tapd_field_map'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'tapd_workspace_id': self.tapd_workspace_id,
            'entity_type': self.entity_type or 'story',
            'field_map': self.field_map or {},
            'last_synced_at': str(self.last_synced_at) if self.last_synced_at else None,
        }


class TapdRefreshRequest(db.Model):
    """TAPD 实时刷新队列（R1 方案：agent 反向轮询消费）"""
    __tablename__ = 'tapd_refresh_requests'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    scope = db.Column(db.String(20), nullable=False, default='iterations',
                      comment='iterations/stories/all')
    tapd_workspace_id = db.Column(db.String(50), index=True)
    iteration_id = db.Column(db.Integer,
                             db.ForeignKey('test_iterations.id'),
                             comment='缩小范围到某个 Hub TestIteration')
    tapd_iteration_id = db.Column(db.String(64),
                                  comment='缩小范围到某个 TAPD 迭代')

    status = db.Column(db.String(20), default='pending', index=True,
                       comment='pending/picked/done/failed')
    requested_by = db.Column(db.String(100), default='',
                             comment='发起人（用户名 / system）')
    picked_by_claw_id = db.Column(db.Integer,
                                  db.ForeignKey('openclaw_instances.id'),
                                  comment='被哪个 agent 领走')
    picked_at = db.Column(db.DateTime)
    finished_at = db.Column(db.DateTime)
    error_message = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=_now, index=True)

    def to_dict(self):
        return {
            'id': self.id,
            'scope': self.scope,
            'tapd_workspace_id': self.tapd_workspace_id,
            'iteration_id': self.iteration_id,
            'tapd_iteration_id': self.tapd_iteration_id,
            'status': self.status or 'pending',
            'requested_by': self.requested_by or '',
            'picked_by_claw_id': self.picked_by_claw_id,
            'picked_at': str(self.picked_at) if self.picked_at else None,
            'finished_at': str(self.finished_at) if self.finished_at else None,
            'error_message': self.error_message,
            'created_at': str(self.created_at) if self.created_at else None,
        }


# ============================================================
# 评审中心 - 多轮评审记录（多态表，覆盖 Skill/Rule/Knowledge/TCL）
# ============================================================

class ReviewComment(db.Model):
    """统一评审记录表（每一次"提交/通过/整改/废弃/纯评论"都写一条，全程留痕）。

    多态：resource_type + resource_id 指向具体资源。不加真 FK，避免删除资源时级联。
    parent_review_id：仅 TCL 场景使用，关联 test_case_library_reviews.id。
    """

    __tablename__ = 'review_comments'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    resource_type = db.Column(
        db.Enum('skill', 'rule', 'knowledge', 'testcase_library'),
        nullable=False, index=True,
        comment='资源类型')
    resource_id = db.Column(db.Integer, nullable=False, index=True,
                            comment='目标资源 id（不加 FK 约束，应用层校验）')
    parent_review_id = db.Column(
        db.Integer, nullable=True,
        comment='TCL 时关联 test_case_library_reviews.id，其它为 NULL')

    action = db.Column(
        db.Enum('submit', 'approve', 'revise', 'reject',
                'withdraw', 'comment'),
        nullable=False, index=True,
        comment='动作类型：submit=提交/重新提交, approve=通过, '
                'revise=打回整改, reject=废弃, withdraw=撤回, comment=纯评论')
    from_status = db.Column(db.String(30), default='',
                            comment='状态机起点（如 pending）')
    to_status = db.Column(db.String(30), default='',
                          comment='状态机终点（如 revise）')
    content = db.Column(db.Text, default='',
                        comment='评审意见 / 整改要求 / 提交说明')

    author = db.Column(db.String(100), default='', index=True,
                       comment='username / claw_name')
    author_type = db.Column(
        db.Enum('user', 'openclaw', 'system'),
        default='user',
        comment='发起方类型')

    created_at = db.Column(db.DateTime, default=_now, index=True)

    __table_args__ = (
        db.Index('ix_rc_resource', 'resource_type', 'resource_id'),
        db.Index('ix_rc_resource_created',
                 'resource_type', 'resource_id', 'created_at'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'resource_type': self.resource_type,
            'resource_id': self.resource_id,
            'parent_review_id': self.parent_review_id,
            'action': self.action,
            'from_status': self.from_status or '',
            'to_status': self.to_status or '',
            'content': self.content or '',
            'author': self.author or '',
            'author_type': self.author_type or 'user',
            'created_at': str(self.created_at) if self.created_at else None,
        }


# ============================================================
# 需求分析图谱（Agent 关联分析结果）
# ============================================================

class RequirementDomainCluster(db.Model):
    """功能域聚类（需求图谱核心表1）"""
    __tablename__ = 'requirement_domain_clusters'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    iteration_id = db.Column(db.Integer,
                             db.ForeignKey('test_iterations.id'),
                             nullable=False, index=True,
                             comment='所属迭代')

    domain_name = db.Column(db.String(50), nullable=False,
                            comment='功能域名（关卡系统/3C_手势_镜头/...）')
    requirement_count = db.Column(db.Integer, default=0,
                                 comment='该域需求数')
    avg_score = db.Column(db.Float, default=0.0,
                          comment='该域平均质量分')

    top_issues = db.Column(db.Text, comment='该域TOP问题（JSON数组）')

    created_at = db.Column(db.DateTime, default=_now)

    __table_args__ = (
        db.UniqueConstraint('iteration_id', 'domain_name',
                            name='uq_req_domain_cluster'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'iteration_id': self.iteration_id,
            'domain_name': self.domain_name,
            'requirement_count': self.requirement_count or 0,
            'avg_score': self.avg_score or 0.0,
            'top_issues': self.top_issues or '[]',
            'created_at': str(self.created_at) if self.created_at else None,
        }


class RequirementConsistencyIssue(db.Model):
    """一致性问题（需求图谱核心表2）"""
    __tablename__ = 'requirement_consistency_issues'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    iteration_id = db.Column(db.Integer,
                             db.ForeignKey('test_iterations.id'),
                             nullable=False, index=True,
                             comment='所属迭代')

    issue_type = db.Column(db.String(30), nullable=False,
                           comment='复制粘贴/覆盖遗漏/差异化缺失/模板化严重/关联提醒')
    severity = db.Column(db.String(10), default='中',
                         comment='高/中/低')
    domain = db.Column(db.String(50), default='',
                       comment='所属功能域')

    requirement_ids = db.Column(db.Text, comment='涉及的需求ID列表（JSON数组）')
    detail = db.Column(db.Text, comment='问题描述')

    status = db.Column(db.String(20), default='open',
                       comment='open/closed（可标记已修复）')
    resolved_at = db.Column(db.DateTime, comment='修复时间')
    resolved_by = db.Column(db.String(100), comment='修复人')

    created_at = db.Column(db.DateTime, default=_now)

    def to_dict(self):
        return {
            'id': self.id,
            'iteration_id': self.iteration_id,
            'issue_type': self.issue_type,
            'severity': self.severity or '中',
            'domain': self.domain or '',
            'requirement_ids': self.requirement_ids or '[]',
            'detail': self.detail or '',
            'status': self.status or 'open',
            'resolved_at': str(self.resolved_at) if self.resolved_at else None,
            'resolved_by': self.resolved_by or '',
            'created_at': str(self.created_at) if self.created_at else None,
        }


class AgentRoleTemplate(db.Model):
    """Agent 身份模板：可复刻的配置组合（基础信息 + skills + rules + 定时任务）。"""
    __tablename__ = 'agent_role_templates'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    template_key = db.Column(db.String(80), unique=True, nullable=False, comment='模板唯一标识')
    name = db.Column(db.String(120), nullable=False, comment='模板名称')
    profile_name = db.Column(db.String(120), default='', comment='身份模板中的名字')
    role_name = db.Column(db.String(80), default='', comment='身份模板中的角色')
    main_responsibility = db.Column(db.Text, comment='主要职责（支持引用文件）')

    skills_summary = db.Column(db.Text, comment='技能页签简述')
    rules_summary = db.Column(db.Text, comment='规则页签简述')
    schedules_summary = db.Column(db.Text, comment='定时任务页签简述')

    installed_skill_ids = db.Column(db.JSON, comment='安装技能 ID 列表')
    rule_ids = db.Column(db.JSON, comment='规则 ID 列表')
    schedule_items = db.Column(db.JSON, comment='定时任务配置 JSON')

    owner_claw_id = db.Column(db.Integer, db.ForeignKey('openclaw_instances.id'),
                              nullable=True, comment='来源 OpenClaw（agent 提交时）')
    status = db.Column(db.Enum('draft', 'pending_review', 'approved', 'rejected', 'archived'),
                       default='draft', comment='模板状态')
    current_version = db.Column(db.Integer, default=1, comment='当前版本号')
    review_comment = db.Column(db.Text, comment='审核意见')

    created_by = db.Column(db.String(100), default='system')
    reviewed_by = db.Column(db.String(100), default='')
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    owner_claw = db.relationship('OpenClawInstance', backref='agent_role_templates')

    def to_dict(self):
        return {
            'id': self.id,
            'template_key': self.template_key,
            'name': self.name,
            'profile_name': self.profile_name or '',
            'role_name': self.role_name or '',
            'main_responsibility': self.main_responsibility or '',
            'skills_summary': self.skills_summary or '',
            'rules_summary': self.rules_summary or '',
            'schedules_summary': self.schedules_summary or '',
            'installed_skill_ids': self.installed_skill_ids or [],
            'rule_ids': self.rule_ids or [],
            'schedule_items': self.schedule_items or [],
            'owner_claw_id': self.owner_claw_id,
            'owner_claw_name': self.owner_claw.name if self.owner_claw else '',
            'status': self.status or 'draft',
            'current_version': self.current_version or 1,
            'review_comment': self.review_comment or '',
            'created_by': self.created_by or 'system',
            'reviewed_by': self.reviewed_by or '',
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }


class AgentRoleTemplateFile(db.Model):
    """Agent 身份模板文件索引。"""
    __tablename__ = 'agent_role_template_files'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    template_id = db.Column(db.Integer, db.ForeignKey('agent_role_templates.id'),
                            nullable=False, index=True)
    relative_path = db.Column(db.String(500), nullable=False, comment='模板根目录内相对路径')
    file_name = db.Column(db.String(255), nullable=False)
    mime_type = db.Column(db.String(120), default='')
    file_size = db.Column(db.Integer, default=0)
    sha256 = db.Column(db.String(64), default='')
    uploaded_by = db.Column(db.String(100), default='system')
    created_at = db.Column(db.DateTime, default=_now)

    template = db.relationship('AgentRoleTemplate', backref='files')

    __table_args__ = (
        db.UniqueConstraint('template_id', 'relative_path', name='uq_agent_template_file_path'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'template_id': self.template_id,
            'relative_path': self.relative_path,
            'file_name': self.file_name,
            'mime_type': self.mime_type or '',
            'file_size': self.file_size or 0,
            'sha256': self.sha256 or '',
            'uploaded_by': self.uploaded_by or 'system',
            'created_at': str(self.created_at) if self.created_at else None,
        }


class AgentRoleTemplateVersion(db.Model):
    """Agent 身份模板版本快照。"""
    __tablename__ = 'agent_role_template_versions'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    template_id = db.Column(db.Integer, db.ForeignKey('agent_role_templates.id'),
                            nullable=False, index=True)
    version_no = db.Column(db.Integer, nullable=False, comment='版本号（递增）')
    action = db.Column(db.String(30), default='save',
                       comment='save/review/submit/rollback/apply')
    change_note = db.Column(db.String(255), default='', comment='变更说明')
    snapshot_payload = db.Column(db.Text, nullable=False, comment='模板快照 JSON')
    created_by = db.Column(db.String(100), default='system')
    created_at = db.Column(db.DateTime, default=_now, index=True)

    template = db.relationship('AgentRoleTemplate', backref='versions')

    __table_args__ = (
        db.UniqueConstraint('template_id', 'version_no', name='uq_agent_template_version_no'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'template_id': self.template_id,
            'version_no': self.version_no,
            'action': self.action or 'save',
            'change_note': self.change_note or '',
            'created_by': self.created_by or 'system',
            'created_at': str(self.created_at) if self.created_at else None,
        }


# ============================================================
# 见闻分享（KM 文章 / 行业洞察等的轻量分享与评论）
# 与知识库（结构化沉淀）、课题讨论（聚焦议题）形成互补：
# 这里偏「转发 + 个人见解 + 评论交流」，不做强结构化、不强制评审。
# ============================================================

SHARED_ARTICLE_CATEGORIES = {
    'testing': '测试技能',
    'gaming': '游戏开发',
    'ai': 'AI 见闻',
    'work': '工作经验',
    'industry': '行业趣事',
    'other': '其他',
}


class SharedArticle(db.Model):
    """见闻分享文章：可由 Web 用户或 OpenClaw Agent 提交。"""
    __tablename__ = 'shared_articles'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    title = db.Column(db.String(200), nullable=False, comment='文章标题')
    summary = db.Column(db.String(500), default='',
                        comment='分享语 / 摘要，建议 80~200 字')
    content = db.Column(db.Text, comment='正文（Markdown，可选；若仅转发链接可留空）')
    source_url = db.Column(db.String(500), default='', comment='原文链接')
    source_name = db.Column(db.String(120), default='',
                            comment='来源（KM / 微信公众号 / 某博客等）')
    category = db.Column(db.String(30), default='other',
                         comment='分类 key（见 SHARED_ARTICLE_CATEGORIES）')
    tags = db.Column(db.JSON, default=list, comment='自由标签列表')

    sharer_type = db.Column(db.String(10), default='user',
                            comment='user / openclaw')
    sharer_user_id = db.Column(db.Integer, db.ForeignKey('users.id'),
                               nullable=True)
    sharer_claw_id = db.Column(db.Integer, db.ForeignKey('openclaw_instances.id'),
                               nullable=True)
    sharer_name = db.Column(db.String(80), nullable=False,
                            comment='展示用名字（user.display_name 或 claw.name）')

    view_count = db.Column(db.Integer, default=0)
    comment_count = db.Column(db.Integer, default=0)
    like_count = db.Column(db.Integer, default=0)

    is_deleted = db.Column(db.Boolean, default=False)
    deleted_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=_now, index=True)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    sharer_user = db.relationship('User', backref='shared_articles')
    sharer_claw = db.relationship('OpenClawInstance', backref='shared_articles')

    def to_dict(self, include_content=False):
        data = {
            'id': self.id,
            'title': self.title,
            'summary': self.summary or '',
            'source_url': self.source_url or '',
            'source_name': self.source_name or '',
            'category': self.category or 'other',
            'category_label': SHARED_ARTICLE_CATEGORIES.get(
                self.category or 'other', '其他'),
            'tags': self.tags or [],
            'sharer_type': self.sharer_type or 'user',
            'sharer_user_id': self.sharer_user_id,
            'sharer_claw_id': self.sharer_claw_id,
            'sharer_name': self.sharer_name,
            'view_count': self.view_count or 0,
            'comment_count': self.comment_count or 0,
            'like_count': self.like_count or 0,
            'is_deleted': bool(self.is_deleted),
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }
        if include_content:
            data['content'] = self.content or ''
        return data


class SharedArticleComment(db.Model):
    """见闻分享文章评论（支持单层引用，不做嵌套树展开）。"""
    __tablename__ = 'shared_article_comments'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    article_id = db.Column(db.Integer, db.ForeignKey('shared_articles.id'),
                           nullable=False, index=True)
    parent_id = db.Column(db.Integer, db.ForeignKey('shared_article_comments.id'),
                          nullable=True, comment='被回复的评论 id（可选）')
    content = db.Column(db.Text, nullable=False)

    commenter_type = db.Column(db.String(10), default='user',
                               comment='user / openclaw')
    commenter_user_id = db.Column(db.Integer, db.ForeignKey('users.id'),
                                  nullable=True)
    commenter_claw_id = db.Column(db.Integer, db.ForeignKey('openclaw_instances.id'),
                                  nullable=True)
    commenter_name = db.Column(db.String(80), nullable=False)

    status = db.Column(db.String(20), default='active',
                       comment='active / deleted')
    created_at = db.Column(db.DateTime, default=_now, index=True)

    article = db.relationship('SharedArticle', backref='comments')
    commenter_user = db.relationship('User', backref='shared_article_comments')
    commenter_claw = db.relationship('OpenClawInstance',
                                     backref='shared_article_comments')

    def to_dict(self):
        return {
            'id': self.id,
            'article_id': self.article_id,
            'parent_id': self.parent_id,
            'content': self.content or '',
            'commenter_type': self.commenter_type or 'user',
            'commenter_user_id': self.commenter_user_id,
            'commenter_claw_id': self.commenter_claw_id,
            'commenter_name': self.commenter_name,
            'status': self.status or 'active',
            'created_at': str(self.created_at) if self.created_at else None,
        }


# ============================================================
# 全局测试报告中心（Test Report Center） — MEMORY #134
# ============================================================
# 目标：把分散在 TestPlanReport / TestTaskReport / 需求分析 / 工程分析
# 几处的"报告"统一到全局表，支持：
#   - 6 种类型（版本计划 / 功能需求测试 / 专项测试 / 需求分析 / 工程分析 / 其他专项）
#   - 关联 TestIteration（版本）+ Project（项目内可见）
#   - 风险等级（high/medium/low/tbd）
#   - markdown / html 内容
#   - 附件（文件系统存储，单文件 10MB 上限）
#   - 分享外链（公开匿名只读；附件下载仍需登录）
#   - 通过 source_ref_type/source_ref_id 反向关联到旧的 TestPlanReport / TestTaskReport
# 旧表不下线（向后兼容），只是前端组件改读全局表。
# ============================================================

# 报告类型 → 中文显示名（前端 tab 与表单下拉用）
TEST_REPORT_TYPES = {
    'version_plan': '版本计划',
    'feature_test': '功能需求测试',
    'specialized_test': '专项测试',
    'requirement_analysis': '需求分析',
    'engineering_analysis': '工程分析',
    'other_specialized': '其他专项',
}

TEST_REPORT_RISK_LEVELS = {
    'high': '高',
    'medium': '中',
    'low': '低',
    'tbd': '评估中',  # 业务侧改名：早期叫"待定"，被认为不够积极，改成"评估中"
}

TEST_REPORT_SOURCE_REF_TYPES = (
    'manual',                   # 全局手工创建
    'test_plan',                # 来自测试计划报告（旧 TestPlanReport）
    'test_task',                # 来自测试任务报告（旧 TestTaskReport）
    'requirement_iteration',    # 关联需求分析的某迭代
    'engineering_batch',        # 关联工程分析批次
)

# 报告状态机（v2 引入，MEMORY #134.C）：
#   draft     刚提交，只有作者+所属用户+项目 admin+super_admin 可见可改
#   published 已发布，项目内所有人可见可读
#   revised   修改中，作者改 published 报告时手动切到此态，再次发布前隐藏
#   abandoned 已废弃，对所有非管理员隐藏（保留审计）
TEST_REPORT_STATUSES = {
    'draft': '草稿',
    'published': '已发布',
    'revised': '修改中',
    'abandoned': '已废弃',
}


class TestReport(db.Model):
    """全局测试报告。一份报告对应一个项目 + 可选迭代/版本，由 user 或 Agent 提交。"""
    __tablename__ = 'test_reports'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)

    # 基础元信息
    title = db.Column(db.String(200), nullable=False, comment='报告标题')
    report_type = db.Column(db.String(40), default='other_specialized',
                            nullable=False,
                            comment='version_plan/feature_test/specialized_test/'
                                    'requirement_analysis/engineering_analysis/'
                                    'other_specialized')
    custom_category_key = db.Column(db.String(120), default='', index=True,
                                    comment='自定义报告类别 key；为空保持固定 report_type 视图')
    remark = db.Column(db.String(500), default='', comment='简短备注/说明（一句话）')

    # 关联
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'),
                           nullable=False, index=True,
                           comment='所属项目（项目内默认可见可读）')
    iteration_id = db.Column(db.Integer, db.ForeignKey('test_iterations.id'),
                             index=True,
                             comment='关联测试迭代（版本）')
    version_name = db.Column(db.String(100), default='',
                             comment='冗余版本号字符串，便于列表/分享页展示')

    # 内容
    content = db.Column(db.Text, comment='报告正文 markdown/html')
    format = db.Column(db.String(20), default='markdown',
                       comment='markdown / html')
    risk_level = db.Column(db.String(20), default='tbd',
                           comment='high / medium / low / tbd')

    # 状态机（MEMORY #134.C）：draft / published / revised / abandoned
    # 默认草稿，作者主动发布；可任意切换；废弃 ≈ 软隐藏（保留审计）
    status = db.Column(db.String(20), default='draft', index=True,
                       comment='draft / published / revised / abandoned')

    # 来源追溯（旧入口写入时填）
    source_ref_type = db.Column(db.String(40), default='manual', index=True,
                                comment='manual/test_plan/test_task/'
                                        'requirement_iteration/engineering_batch')
    source_ref_id = db.Column(db.Integer, index=True,
                              comment='来源实体 ID')

    # 提交者（user 或 openclaw 二选一）
    submitter_type = db.Column(db.String(20), default='user',
                               comment='user / openclaw')
    submitter_user_id = db.Column(db.Integer, db.ForeignKey('users.id'),
                                  comment='user 提交时填')
    submitter_claw_id = db.Column(db.Integer,
                                  db.ForeignKey('openclaw_instances.id'),
                                  comment='openclaw 提交时填')
    submitter_name = db.Column(db.String(120), default='', comment='冗余显示名')

    # 分享外链
    is_shared = db.Column(db.Boolean, default=False, comment='是否已生成外链')
    share_token = db.Column(db.String(64), unique=True,
                            comment='分享 token，匿名访问 /r/<token>')
    shared_at = db.Column(db.DateTime, comment='首次/最近一次开启分享时间')

    # 隐藏（Web 列表不显示，但 agent 可分享链接访问）
    is_hidden = db.Column(db.Boolean, default=False, index=True,
                          comment='隐藏后 Web 列表不展示，分享链接仍有效')

    # 软删
    is_deleted = db.Column(db.Boolean, default=False, index=True)
    deleted_at = db.Column(db.DateTime)

    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    # 关联
    project = db.relationship('Project', backref='test_reports')
    iteration = db.relationship('TestIteration', backref='test_reports')
    submitter_user = db.relationship('User', foreign_keys=[submitter_user_id])
    submitter_claw = db.relationship('OpenClawInstance',
                                     foreign_keys=[submitter_claw_id])
    attachments = db.relationship(
        'TestReportAttachment',
        backref='report',
        lazy='dynamic',
        cascade='all, delete-orphan',
        order_by='TestReportAttachment.uploaded_at.asc()',
    )

    __table_args__ = (
        db.Index('ix_test_reports_proj_type', 'project_id', 'report_type'),
        db.Index('ix_test_reports_proj_iter', 'project_id', 'iteration_id'),
    )

    def generate_share_token(self) -> str:
        """生成一个唯一分享 token。已开启分享则保留原 token，再次开启不会换。"""
        if self.share_token:
            return self.share_token
        # 22 字符 URL-safe，碰撞概率忽略不计
        self.share_token = secrets.token_urlsafe(16)
        return self.share_token

    def to_dict(self, *, include_content=False, for_public=False,
                include_attachments=True):
        """
        - for_public=True：分享外链匿名访问场景，不返回内部 ID/敏感字段
        - include_content：列表场景默认 False 节省带宽
        - include_attachments：附件元数据
        """
        risk_label = TEST_REPORT_RISK_LEVELS.get(self.risk_level or 'tbd',
                                                 self.risk_level)
        type_label = TEST_REPORT_TYPES.get(self.report_type,
                                           self.report_type)
        status_value = self.status or 'draft'
        status_label = TEST_REPORT_STATUSES.get(status_value, status_value)
        data = {
            'id': None if for_public else self.id,
            'title': self.title,
            'report_type': self.report_type,
            'report_type_label': type_label,
            'custom_category_key': self.custom_category_key or '',
            'remark': self.remark or '',
            'risk_level': self.risk_level or 'tbd',
            'risk_level_label': risk_label,
            'status': status_value,
            'status_label': status_label,
            'format': self.format or 'markdown',
            'version_name': self.version_name or '',
            'iteration_id': None if for_public else self.iteration_id,
            'iteration_name': self.iteration.name if self.iteration else None,
            'project_id': None if for_public else self.project_id,
            'project_name': self.project.name if self.project else None,
            'submitter_type': self.submitter_type or 'user',
            'submitter_name': self.submitter_name or '',
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }
        if not for_public:
            data.update({
                'source_ref_type': self.source_ref_type or 'manual',
                'source_ref_id': self.source_ref_id,
                'submitter_user_id': self.submitter_user_id,
                'submitter_claw_id': self.submitter_claw_id,
                'is_shared': bool(self.is_shared),
                'share_token': self.share_token if self.is_shared else None,
                'shared_at': str(self.shared_at) if self.shared_at else None,
                'is_hidden': bool(self.is_hidden),
                'is_deleted': bool(self.is_deleted),
            })
        if include_content:
            data['content'] = self.content or ''
        if include_attachments:
            data['attachments'] = [
                a.to_dict(for_public=for_public)
                for a in self.attachments
                if not a.is_deleted
            ]
        return data


# ============== 游戏功能模块全景视图 ==============

class PanoramaWorkspace(db.Model):
    """功能全景工作区：一个项目下的一个唯一标题即一份功能全景。"""
    __tablename__ = 'panorama_workspaces'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=True, index=True)
    title = db.Column(db.String(180), nullable=False, comment='功能全景标题，如 程序功能/配置模块')
    description = db.Column(db.Text)
    is_default = db.Column(db.Boolean, default=False, index=True)
    created_by = db.Column(db.String(120))
    updated_by = db.Column(db.String(120))
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    project = db.relationship('Project', backref='panorama_workspaces')

    __table_args__ = (
        db.UniqueConstraint('project_id', 'title', name='uq_panorama_workspace_title'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'project_id': self.project_id,
            'project_name': self.project.name if self.project else None,
            'title': self.title,
            'description': self.description or '',
            'is_default': bool(self.is_default),
            'created_by': self.created_by,
            'updated_by': self.updated_by,
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }


class GameModulePanorama(db.Model):
    """游戏功能模块全景节点 — 由 Agent 建立和维护的功能模块树"""
    __tablename__ = 'game_module_panorama'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=True)
    workspace_id = db.Column(db.Integer, db.ForeignKey('panorama_workspaces.id'), nullable=True, index=True)
    parent_id = db.Column(db.Integer, db.ForeignKey('game_module_panorama.id'), nullable=True,
                          comment='父节点 ID，NULL 为顶层模块')
    name = db.Column(db.String(200), nullable=False, comment='模块名称，如"漂移系统"')
    path = db.Column(db.String(500), comment='模块全路径，如"赛车/漂移系统"')
    description = db.Column(db.Text, comment='模块功能描述')
    code_paths = db.Column(db.JSON, comment='关联的代码路径模式列表，JSON 数组')
    resource_paths = db.Column(db.JSON, comment='关联的资源路径模式列表，JSON 数组')
    test_focus = db.Column(db.Text, comment='测试重点说明')
    related_case_libraries = db.Column(db.JSON, comment='关联的用例库 ID 列表')
    status = db.Column(db.String(20), default='active', comment='active/deprecated/planned')
    risk_level = db.Column(db.String(20), default='normal', comment='low/normal/high/critical')
    last_change_summary = db.Column(db.Text, comment='最近一次变更摘要')
    last_changed_at = db.Column(db.DateTime, comment='最近变更时间')
    is_new = db.Column(db.Boolean, default=False, comment='是否新增模块，由 Agent 标记')
    highlight = db.Column(db.Boolean, default=False,
                          comment='本轮是否有变更（红点高亮），Agent 每次更新时设置；与 is_new 不同：is_new 表示首次创建，highlight 表示本轮被涉及')
    extra = db.Column(db.JSON, comment='扩展字段')
    created_by = db.Column(db.String(120), comment='创建者（agent 名或用户名）')
    updated_by = db.Column(db.String(120), comment='最后更新者')
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    children = db.relationship('GameModulePanorama', backref=db.backref('parent', remote_side='GameModulePanorama.id'), lazy='dynamic')
    workspace = db.relationship('PanoramaWorkspace', backref='modules')

    def to_dict(self, include_children=False):
        d = {
            'id': self.id,
            'project_id': self.project_id,
            'workspace_id': self.workspace_id,
            'parent_id': self.parent_id,
            'name': self.name,
            'path': self.path,
            'description': self.description,
            'code_paths': self.code_paths or [],
            'resource_paths': self.resource_paths or [],
            'test_focus': self.test_focus,
            'related_case_libraries': self.related_case_libraries or [],
            'status': self.status or 'active',
            'risk_level': self.risk_level or 'normal',
            'last_change_summary': self.last_change_summary,
            'last_changed_at': str(self.last_changed_at) if self.last_changed_at else None,
            'is_new': bool(self.is_new) if self.is_new else False,
            'highlight': bool(self.highlight) if self.highlight else False,
            'extra': self.extra,
            'created_by': self.created_by,
            'updated_by': self.updated_by,
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }
        metric = getattr(self, 'test_metric', None)
        if metric:
            d['test_metrics'] = metric.to_dict()
        if include_children:
            d['children'] = [c.to_dict(include_children=True) for c in self.children]
        return d


class GameModuleRelation(db.Model):
    """功能模块关系边 — 功能全景拓扑的正式边模型"""
    __tablename__ = 'game_module_relations'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=True, index=True)
    workspace_id = db.Column(db.Integer, db.ForeignKey('panorama_workspaces.id'), nullable=True, index=True)
    source_module_id = db.Column(db.Integer, db.ForeignKey('game_module_panorama.id'),
                                 nullable=False, index=True)
    target_module_id = db.Column(db.Integer, db.ForeignKey('game_module_panorama.id'),
                                 nullable=False, index=True)
    relation_type = db.Column(db.String(40), nullable=False, default='depends_on',
                              comment='child/depends_on/affects/shared_resource/api_flow/data_flow/test_overlap')
    confidence = db.Column(db.Float, default=1.0, comment='关系置信度 0-1')
    evidence = db.Column(db.JSON, comment='关系证据：reason/files/symbols/resources/case_library_ids')
    source = db.Column(db.String(50), default='manual', comment='manual/agent/code_index/migration')
    created_by = db.Column(db.String(120))
    updated_by = db.Column(db.String(120))
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    source_module = db.relationship(
        'GameModulePanorama',
        foreign_keys=[source_module_id],
        backref='outgoing_relations',
    )
    target_module = db.relationship(
        'GameModulePanorama',
        foreign_keys=[target_module_id],
        backref='incoming_relations',
    )

    __table_args__ = (
        db.UniqueConstraint('workspace_id', 'source_module_id', 'target_module_id',
                            'relation_type', name='uq_game_module_relation'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'project_id': self.project_id,
            'workspace_id': self.workspace_id,
            'source_module_id': self.source_module_id,
            'source_module_name': self.source_module.name if self.source_module else None,
            'target_module_id': self.target_module_id,
            'target_module_name': self.target_module.name if self.target_module else None,
            'relation_type': self.relation_type or 'depends_on',
            'confidence': float(self.confidence if self.confidence is not None else 1.0),
            'evidence': self.evidence or {},
            'source': self.source or 'manual',
            'created_by': self.created_by,
            'updated_by': self.updated_by,
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }


class PanoramaCodeEntity(db.Model):
    """功能全景轻量代码实体索引"""
    __tablename__ = 'panorama_code_entities'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=True, index=True)
    workspace_id = db.Column(db.Integer, db.ForeignKey('panorama_workspaces.id'), nullable=True, index=True)
    repo_key = db.Column(db.String(120), default='default', index=True)
    file_path = db.Column(db.String(1000), nullable=False, index=True)
    entity_type = db.Column(db.String(30), nullable=False, default='file',
                            comment='file/function/class/import/test/resource')
    symbol_name = db.Column(db.String(300), default='')
    language = db.Column(db.String(50), default='')
    start_line = db.Column(db.Integer, default=0)
    end_line = db.Column(db.Integer, default=0)
    content_hash = db.Column(db.String(120), default='')
    extra = db.Column(db.JSON)
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    __table_args__ = (
        db.UniqueConstraint('workspace_id', 'repo_key', 'file_path', 'entity_type',
                            'symbol_name', 'start_line', name='uq_panorama_code_entity'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'project_id': self.project_id,
            'workspace_id': self.workspace_id,
            'repo_key': self.repo_key or 'default',
            'file_path': self.file_path,
            'entity_type': self.entity_type or 'file',
            'symbol_name': self.symbol_name or '',
            'language': self.language or '',
            'start_line': self.start_line or 0,
            'end_line': self.end_line or 0,
            'content_hash': self.content_hash or '',
            'extra': self.extra or {},
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }


class PanoramaModuleCodeLink(db.Model):
    """功能模块与代码实体的关联"""
    __tablename__ = 'panorama_module_code_links'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=True, index=True)
    workspace_id = db.Column(db.Integer, db.ForeignKey('panorama_workspaces.id'), nullable=True, index=True)
    module_id = db.Column(db.Integer, db.ForeignKey('game_module_panorama.id'),
                          nullable=False, index=True)
    entity_id = db.Column(db.Integer, db.ForeignKey('panorama_code_entities.id'),
                          nullable=False, index=True)
    link_type = db.Column(db.String(30), nullable=False, default='owns',
                          comment='owns/uses/tests/configures/resource')
    confidence = db.Column(db.Float, default=1.0)
    evidence = db.Column(db.JSON)
    source = db.Column(db.String(50), default='agent')
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    module = db.relationship('GameModulePanorama', backref='code_links')
    entity = db.relationship('PanoramaCodeEntity', backref='module_links')

    __table_args__ = (
        db.UniqueConstraint('module_id', 'entity_id', 'link_type',
                            name='uq_panorama_module_code_link'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'project_id': self.project_id,
            'workspace_id': self.workspace_id,
            'module_id': self.module_id,
            'module_name': self.module.name if self.module else None,
            'entity_id': self.entity_id,
            'entity': self.entity.to_dict() if self.entity else None,
            'link_type': self.link_type or 'owns',
            'confidence': float(self.confidence if self.confidence is not None else 1.0),
            'evidence': self.evidence or {},
            'source': self.source or 'agent',
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }


class PanoramaImpactAnalysis(db.Model):
    """一次功能全景变更影响分析结果"""
    __tablename__ = 'panorama_impact_analyses'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=True, index=True)
    workspace_id = db.Column(db.Integer, db.ForeignKey('panorama_workspaces.id'), nullable=True, index=True)
    input_type = db.Column(db.String(30), default='changed_files')
    input_payload = db.Column(db.JSON)
    affected_modules = db.Column(db.JSON)
    affected_relations = db.Column(db.JSON)
    recommended_case_libraries = db.Column(db.JSON)
    risk_score = db.Column(db.Integer, default=0)
    risk_level = db.Column(db.String(20), default='low')
    test_context = db.Column(db.JSON)
    token_savings = db.Column(db.JSON)
    summary = db.Column(db.Text)
    created_by = db.Column(db.String(120))
    created_at = db.Column(db.DateTime, default=_now)

    def to_dict(self):
        return {
            'id': self.id,
            'project_id': self.project_id,
            'workspace_id': self.workspace_id,
            'input_type': self.input_type or 'changed_files',
            'input_payload': self.input_payload or {},
            'affected_modules': self.affected_modules or [],
            'affected_relations': self.affected_relations or [],
            'recommended_case_libraries': self.recommended_case_libraries or [],
            'risk_score': self.risk_score or 0,
            'risk_level': self.risk_level or 'low',
            'test_context': self.test_context or {},
            'token_savings': self.token_savings or {},
            'summary': self.summary or '',
            'created_by': self.created_by,
            'created_at': str(self.created_at) if self.created_at else None,
        }


class PanoramaSnapshot(db.Model):
    """功能全景快照元信息"""
    __tablename__ = 'panorama_snapshots'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=True, index=True)
    workspace_id = db.Column(db.Integer, db.ForeignKey('panorama_workspaces.id'), nullable=True, index=True)
    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    module_count = db.Column(db.Integer, default=0)
    relation_count = db.Column(db.Integer, default=0)
    code_entity_count = db.Column(db.Integer, default=0)
    created_by = db.Column(db.String(120))
    created_at = db.Column(db.DateTime, default=_now)
    # 快照捕获时，被捕获视图（模块/关系/实体）中的最新更新时间
    view_updated_at = db.Column(db.DateTime, nullable=True,
                                comment='快照对应视图内容的最新更新时间')

    items = db.relationship(
        'PanoramaSnapshotItem',
        backref='snapshot',
        lazy='dynamic',
        cascade='all, delete-orphan',
    )
    workspace = db.relationship('PanoramaWorkspace', backref='snapshots')

    def to_dict(self, *, include_items=False):
        d = {
            'id': self.id,
            'project_id': self.project_id,
            'workspace_id': self.workspace_id,
            'workspace_title': self.workspace.title if self.workspace else None,
            'name': self.name,
            'description': self.description,
            'module_count': self.module_count or 0,
            'relation_count': self.relation_count or 0,
            'code_entity_count': self.code_entity_count or 0,
            'created_by': self.created_by,
            'created_at': str(self.created_at) if self.created_at else None,
            'view_updated_at': (str(self.view_updated_at)
                                if self.view_updated_at else None),
        }
        if include_items:
            d['items'] = [item.to_dict() for item in self.items.order_by(PanoramaSnapshotItem.id).all()]
        return d


class PanoramaSnapshotItem(db.Model):
    """功能全景快照明细"""
    __tablename__ = 'panorama_snapshot_items'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    snapshot_id = db.Column(db.Integer, db.ForeignKey('panorama_snapshots.id'),
                           nullable=False, index=True)
    item_type = db.Column(db.String(30), nullable=False, comment='module/relation')
    item_key = db.Column(db.String(500), nullable=False)
    payload = db.Column(db.JSON)

    __table_args__ = (
        db.UniqueConstraint('snapshot_id', 'item_key', name='uq_panorama_snapshot_item'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'snapshot_id': self.snapshot_id,
            'item_type': self.item_type,
            'item_key': self.item_key,
            'payload': self.payload or {},
        }


class GameModuleChangeLog(db.Model):
    """功能模块变更记录 — Agent 每次更新全景视图时写入"""
    __tablename__ = 'game_module_change_logs'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    module_id = db.Column(db.Integer, db.ForeignKey('game_module_panorama.id'), nullable=False, index=True)
    change_type = db.Column(db.String(30), nullable=False,
                            comment='logic/resource/config/api/refactor/bugfix/feature')
    summary = db.Column(db.Text, nullable=False, comment='变更摘要')
    detail = db.Column(db.JSON, comment='变更详情：changed_files, commit_range, diff_highlights 等')
    affected_cases = db.Column(db.JSON, comment='受影响用例描述列表')
    test_suggestion = db.Column(db.Text, comment='测试建议')
    risk_level = db.Column(db.String(20), default='normal')
    source = db.Column(db.String(50), comment='变更来源：agent/manual/webhook')
    created_by = db.Column(db.String(120))
    created_at = db.Column(db.DateTime, default=_now)

    module = db.relationship('GameModulePanorama', backref='change_logs')

    def to_dict(self):
        return {
            'id': self.id,
            'module_id': self.module_id,
            'module_name': self.module.name if self.module else None,
            'module_path': self.module.path if self.module else None,
            'change_type': self.change_type,
            'summary': self.summary,
            'detail': self.detail,
            'affected_cases': self.affected_cases or [],
            'test_suggestion': self.test_suggestion,
            'risk_level': self.risk_level or 'normal',
            'source': self.source,
            'created_by': self.created_by,
            'created_at': str(self.created_at) if self.created_at else None,
        }


class TestReportCustomCategory(db.Model):
    """测试报告自定义类别；标题本身作为唯一 key。"""
    __tablename__ = 'test_report_custom_categories'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    title = db.Column(db.String(120), nullable=False, unique=True, index=True,
                      comment='类别标题，也是 API 查询 key')
    description = db.Column(db.String(500), default='')
    created_by = db.Column(db.String(120), default='')
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    def to_dict(self):
        return {
            'id': self.id,
            'title': self.title,
            'key': self.title,
            'description': self.description or '',
            'created_by': self.created_by or '',
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }


class TestReportAttachment(db.Model):
    """测试报告附件。文件存磁盘，DB 存元数据。单文件 10MB 上限。"""
    __tablename__ = 'test_report_attachments'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    report_id = db.Column(db.Integer, db.ForeignKey('test_reports.id'),
                          nullable=False, index=True)
    filename = db.Column(db.String(255), nullable=False,
                         comment='原始上传文件名（展示用）')
    stored_name = db.Column(db.String(255), nullable=False,
                            comment='磁盘上的唯一文件名（避免冲突）')
    size_bytes = db.Column(db.BigInteger, default=0, comment='字节数')
    content_type = db.Column(db.String(120), default='application/octet-stream')
    uploaded_by = db.Column(db.String(120), default='', comment='上传者名（冗余）')
    uploaded_by_user_id = db.Column(db.Integer)
    uploaded_by_claw_id = db.Column(db.Integer)
    is_deleted = db.Column(db.Boolean, default=False)
    uploaded_at = db.Column(db.DateTime, default=_now)

    def to_dict(self, *, for_public=False):
        return {
            'id': None if for_public else self.id,
            'filename': self.filename,
            'size_bytes': int(self.size_bytes or 0),
            'content_type': self.content_type or 'application/octet-stream',
            'uploaded_by': self.uploaded_by or '',
            'uploaded_at': str(self.uploaded_at) if self.uploaded_at else None,
            # 分享外链场景：仅显示文件名 + 大小，下载链接前端置灰提示"登录后下载"
            'download_url': (
                None if for_public
                else f'/api/v1/test-reports/{self.report_id}/attachments/{self.id}/download'
            ),
        }


# ============== Agent 考试系统 ==============

class ExamPaper(db.Model):
    """试卷：一组题目的集合"""
    __tablename__ = 'exam_papers'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(200), nullable=False, comment='试卷名称')
    description = db.Column(db.Text, comment='试卷描述/考核目的')
    category = db.Column(db.String(50), default='general',
                         comment='hub_ops/testcase/review/specialty/risk/general')
    difficulty = db.Column(db.String(20), default='normal',
                           comment='easy/normal/hard/expert')
    total_score = db.Column(db.Integer, default=100, comment='总分')
    pass_score = db.Column(db.Integer, default=60, comment='及格分')
    time_limit_min = db.Column(db.Integer, default=60,
                               comment='时长分钟，0 表示不限时')
    applicable_skill_ids = db.Column(db.JSON,
                                     comment='[skill_id, ...] 关联的考核能力点')
    remark = db.Column(db.Text, comment='试卷备注')
    status = db.Column(db.String(20), default='draft',
                       comment='draft/published/archived')
    is_deleted = db.Column(db.Boolean, default=False)
    created_by = db.Column(db.String(120))
    created_by_user_id = db.Column(db.Integer, comment='创建用户 ID')
    created_by_claw_id = db.Column(db.Integer, comment='创建 Agent ID')
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    def to_dict(self, include_questions=False, hide_answer=True):
        d = {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'category': self.category,
            'difficulty': self.difficulty,
            'total_score': self.total_score,
            'pass_score': self.pass_score,
            'time_limit_min': self.time_limit_min,
            'applicable_skill_ids': self.applicable_skill_ids or [],
            'remark': self.remark or '',
            'status': self.status,
            'is_deleted': bool(self.is_deleted),
            'created_by': self.created_by,
            'created_by_user_id': self.created_by_user_id,
            'created_by_claw_id': self.created_by_claw_id,
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
            'question_count': ExamQuestion.query.filter_by(
                paper_id=self.id, is_deleted=False).count(),
        }
        if include_questions:
            qs = ExamQuestion.query.filter_by(paper_id=self.id, is_deleted=False)\
                .order_by(ExamQuestion.order_index, ExamQuestion.id).all()
            d['questions'] = [q.to_dict(hide_answer=hide_answer) for q in qs]
        return d


class ExamCampaign(db.Model):
    """一次考试发起活动：指定试卷、范围和起止日期。"""
    __tablename__ = 'exam_campaigns'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    paper_id = db.Column(db.Integer, db.ForeignKey('exam_papers.id'),
                         nullable=False, index=True)
    name = db.Column(db.String(200), nullable=False, comment='考试名称')
    scope = db.Column(db.String(20), nullable=False,
                      comment='global/project/personal')
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'),
                           comment='项目级考试所属项目')
    target_claw_ids = db.Column(db.JSON,
                                comment='个人考试目标 Agent ID 列表')
    starts_at = db.Column(db.DateTime, comment='开始时间')
    ends_at = db.Column(db.DateTime, nullable=False, comment='截止时间')
    status = db.Column(db.String(20), default='scheduled',
                       comment='scheduled/active/ended/cancelled')
    remark = db.Column(db.Text, comment='考试备注')
    launched_by_user_id = db.Column(db.Integer, comment='发起用户 ID')
    launched_by_claw_id = db.Column(db.Integer, comment='发起 Agent ID')
    launched_by_display = db.Column(db.String(120), comment='发起人展示名')
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    paper = db.relationship('ExamPaper', backref=db.backref('campaigns', lazy='dynamic'))
    project = db.relationship('Project')

    def to_dict(self):
        now = _now()
        computed_status = self.status or 'scheduled'
        if computed_status not in ('cancelled', 'ended'):
            if self.ends_at and now > self.ends_at:
                computed_status = 'ended'
            elif self.starts_at and now < self.starts_at:
                computed_status = 'scheduled'
            else:
                computed_status = 'active'
        return {
            'id': self.id,
            'paper_id': self.paper_id,
            'paper_name': self.paper.name if self.paper else None,
            'name': self.name,
            'scope': self.scope,
            'project_id': self.project_id,
            'project_name': self.project.name if self.project else None,
            'target_claw_ids': self.target_claw_ids or [],
            'starts_at': str(self.starts_at) if self.starts_at else None,
            'ends_at': str(self.ends_at) if self.ends_at else None,
            'status': computed_status,
            'remark': self.remark or '',
            'launched_by_user_id': self.launched_by_user_id,
            'launched_by_claw_id': self.launched_by_claw_id,
            'launched_by_display': self.launched_by_display or '',
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
            'session_count': ExamSession.query.filter_by(
                campaign_id=self.id).count(),
        }


class ExamQuestion(db.Model):
    """题目：属于某试卷"""
    __tablename__ = 'exam_questions'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    paper_id = db.Column(db.Integer, db.ForeignKey('exam_papers.id'),
                         nullable=False, index=True)
    order_index = db.Column(db.Integer, default=0, comment='题目顺序')
    type = db.Column(db.String(30), nullable=False,
                     comment='single/multiple/judge/fill/essay/api_op/case_design/scenario')
    title = db.Column(db.String(500), nullable=False, comment='题目标题')
    description = db.Column(db.Text, comment='题干 markdown')
    options = db.Column(db.JSON,
                        comment='选项列表（单/多选用），[{"key":"A","text":"..."}]')
    points = db.Column(db.Integer, default=10, comment='本题分值')
    standard_answer = db.Column(db.JSON,
                                comment='标准答案（仅超管可见，单选="A"，多选=["A","B"]，开放题为评分要点列表）')
    grading_criteria = db.Column(db.Text,
                                 comment='评分细则 markdown，主观题用')
    auto_grade_script = db.Column(db.Text,
                                  comment='Python 表达式/脚本，可选，用于 api_op 题自动判分')
    skill_tag = db.Column(db.String(80), comment='对应的 skill 名（题目分类）')
    is_deleted = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    paper = db.relationship('ExamPaper', backref=db.backref('all_questions', lazy='dynamic'))

    def to_dict(self, hide_answer=True):
        d = {
            'id': self.id,
            'paper_id': self.paper_id,
            'order_index': self.order_index,
            'type': self.type,
            'title': self.title,
            'description': self.description,
            'options': self.options or [],
            'points': self.points,
            'skill_tag': self.skill_tag,
            'is_deleted': bool(self.is_deleted),
            'created_at': str(self.created_at) if self.created_at else None,
        }
        if not hide_answer:
            d['standard_answer'] = self.standard_answer
            d['grading_criteria'] = self.grading_criteria
            d['auto_grade_script'] = self.auto_grade_script
        return d


class ExamSession(db.Model):
    """一次考试：某 agent / 用户对某试卷的一次作答"""
    __tablename__ = 'exam_sessions'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    paper_id = db.Column(db.Integer, db.ForeignKey('exam_papers.id'),
                         nullable=False, index=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey('exam_campaigns.id'),
                            comment='所属考试场次；历史数据可为空')
    examinee_claw_id = db.Column(db.Integer, comment='应考的 OpenClaw id')
    examinee_user_id = db.Column(db.Integer, comment='应考的 User id（人考）')
    examinee_display = db.Column(db.String(120), comment='展示名（冗余）')
    started_at = db.Column(db.DateTime, default=_now)
    submitted_at = db.Column(db.DateTime)
    deadline_at = db.Column(db.DateTime, comment='截止时间（基于 time_limit_min）')
    status = db.Column(db.String(20), default='in_progress',
                       comment='in_progress/submitted/grading/completed/expired')
    auto_score = db.Column(db.Integer, default=0, comment='客观题自动得分')
    manual_score = db.Column(db.Integer, default=0, comment='主观题人工/同行评分')
    total_score = db.Column(db.Integer, default=0, comment='合计得分')
    passed = db.Column(db.Boolean, default=False)
    summary = db.Column(db.Text, comment='结果摘要/评语')

    paper = db.relationship('ExamPaper', backref=db.backref('sessions', lazy='dynamic'))
    campaign = db.relationship('ExamCampaign', backref=db.backref('sessions', lazy='dynamic'))

    def to_dict(self, include_answers=False):
        d = {
            'id': self.id,
            'paper_id': self.paper_id,
            'campaign_id': self.campaign_id,
            'campaign_name': self.campaign.name if self.campaign else None,
            'paper_name': self.paper.name if self.paper else None,
            'examinee_claw_id': self.examinee_claw_id,
            'examinee_user_id': self.examinee_user_id,
            'examinee_display': self.examinee_display,
            'started_at': str(self.started_at) if self.started_at else None,
            'submitted_at': str(self.submitted_at) if self.submitted_at else None,
            'deadline_at': str(self.deadline_at) if self.deadline_at else None,
            'status': self.status,
            'auto_score': self.auto_score or 0,
            'manual_score': self.manual_score or 0,
            'total_score': self.total_score or 0,
            'passed': bool(self.passed),
            'summary': self.summary or '',
        }
        if include_answers:
            ans = ExamAnswer.query.filter_by(session_id=self.id).all()
            d['answers'] = [a.to_dict() for a in ans]
        return d


class ExamAnswer(db.Model):
    """每道题的作答"""
    __tablename__ = 'exam_answers'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    session_id = db.Column(db.Integer, db.ForeignKey('exam_sessions.id'),
                           nullable=False, index=True)
    question_id = db.Column(db.Integer, db.ForeignKey('exam_questions.id'),
                            nullable=False)
    answer_content = db.Column(db.Text, comment='答题内容（JSON 字符串或文本）')
    auto_score = db.Column(db.Integer, comment='自动判分得分；NULL 表示未判分')
    peer_scores = db.Column(db.JSON,
                            comment='[{"reviewer_claw_id":N, "reviewer_display":"x", "score":8, "comment":"..."}, ...]')
    final_score = db.Column(db.Integer, default=0)
    grading_notes = db.Column(db.Text, comment='判分备注')
    answered_at = db.Column(db.DateTime, default=_now)

    session = db.relationship('ExamSession', backref=db.backref('answer_entries', lazy='dynamic'))
    question = db.relationship('ExamQuestion')

    def to_dict(self):
        return {
            'id': self.id,
            'session_id': self.session_id,
            'question_id': self.question_id,
            'question_title': self.question.title if self.question else None,
            'question_type': self.question.type if self.question else None,
            'question_points': self.question.points if self.question else None,
            'answer_content': self.answer_content or '',
            'auto_score': self.auto_score,
            'peer_scores': self.peer_scores or [],
            'final_score': self.final_score or 0,
            'grading_notes': self.grading_notes or '',
            'answered_at': str(self.answered_at) if self.answered_at else None,
        }


class ExamPeerReview(db.Model):
    """同行互评分配：哪个 reviewer 评哪份卷的哪些题"""
    __tablename__ = 'exam_peer_reviews'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    session_id = db.Column(db.Integer, db.ForeignKey('exam_sessions.id'),
                           nullable=False, index=True)
    reviewer_claw_id = db.Column(db.Integer)
    reviewer_user_id = db.Column(db.Integer)
    reviewer_display = db.Column(db.String(120))
    status = db.Column(db.String(20), default='pending',
                       comment='pending/in_progress/completed/skipped')
    invited_at = db.Column(db.DateTime, default=_now)
    completed_at = db.Column(db.DateTime)

    session = db.relationship('ExamSession', backref=db.backref('peer_reviews', lazy='dynamic'))

    def to_dict(self):
        return {
            'id': self.id,
            'session_id': self.session_id,
            'reviewer_claw_id': self.reviewer_claw_id,
            'reviewer_user_id': self.reviewer_user_id,
            'reviewer_display': self.reviewer_display,
            'status': self.status,
            'invited_at': str(self.invited_at) if self.invited_at else None,
            'completed_at': str(self.completed_at) if self.completed_at else None,
        }


# ============== Skill 密钥保险箱 ==============

class ClawSecret(db.Model):
    """Agent 加密密钥/Token 保险箱。

    Agent 把外部 API 凭据（Tavily Key、企微 Webhook 等）加密存到这里，
    Skill 内容里只写占位符 ${SECRET:key_name}，运行时由 Agent 自己取明文。
    数据用 _simple_encrypt（XOR+base64）加密存储，仅 owner 可解密读取。
    """
    __tablename__ = 'claw_secrets'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    owner_claw_id = db.Column(db.Integer, db.ForeignKey('openclaw_instances.id'),
                              comment='归属的 OpenClaw（按 claw 隔离）')
    owner_user_id = db.Column(db.Integer, db.ForeignKey('users.id'),
                              comment='归属的 Web 用户（人工存的）')
    key = db.Column(db.String(120), nullable=False,
                    comment='密钥名，例如 tavily_api_key / wecom_webhook_xx')
    encrypted_value = db.Column(db.Text, nullable=False, comment='加密后的值')
    description = db.Column(db.String(500), comment='用途说明')
    share_scope = db.Column(db.String(20), default='private',
                            comment='共享范围：private=仅owner / project=同项目claw / public=所有claw')
    share_project_id = db.Column(db.Integer, db.ForeignKey('projects.id'),
                                 comment='共享给哪个项目（share_scope=project 时有效）')
    last_used_at = db.Column(db.DateTime, comment='最后一次被读取的时间')
    use_count = db.Column(db.Integer, default=0, comment='被读取次数')
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    __table_args__ = (
        db.UniqueConstraint('owner_claw_id', 'owner_user_id', 'key', name='uq_claw_secret_owner_key'),
        db.Index('idx_claw_secret_owner', 'owner_claw_id', 'owner_user_id'),
    )

    def to_dict(self, include_value=False):
        # 解析 owner 显示信息（claw 名 / 用户名）
        owner_claw_name = ''
        owner_username = ''
        if self.owner_claw_id:
            claw = OpenClawInstance.query.get(self.owner_claw_id)
            if claw:
                owner_claw_name = claw.name
        if self.owner_user_id:
            user = User.query.get(self.owner_user_id)
            if user:
                owner_username = user.username

        # 共享项目名
        share_project_name = ''
        if self.share_project_id:
            from app.models import Project
            proj = Project.query.get(self.share_project_id)
            if proj:
                share_project_name = proj.name

        d = {
            'id': self.id,
            'owner_claw_id': self.owner_claw_id,
            'owner_claw_name': owner_claw_name,
            'owner_user_id': self.owner_user_id,
            'owner_username': owner_username,
            'owner_label': owner_claw_name and ('claw:' + owner_claw_name) or (owner_username and ('user:' + owner_username)) or '',
            'key': self.key,
            'description': self.description or '',
            'share_scope': self.share_scope or 'private',
            'share_project_id': self.share_project_id,
            'share_project_name': share_project_name,
            'has_value': bool(self.encrypted_value),
            'value_length': len(_simple_decrypt(self.encrypted_value)) if self.encrypted_value else 0,
            'last_used_at': str(self.last_used_at) if self.last_used_at else None,
            'use_count': self.use_count or 0,
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }
        if include_value:
            d['value'] = _simple_decrypt(self.encrypted_value) if self.encrypted_value else ''
        return d

    def set_value(self, plaintext):
        """加密存储明文值"""
        self.encrypted_value = _simple_encrypt(plaintext or '')

    def get_value(self):
        """获取明文值"""
        return _simple_decrypt(self.encrypted_value) if self.encrypted_value else ''


# ============== 测试左移 / Developer AI 协作（增量、默认不触发现有流程） ==============

class ShiftLeftAnalysisRun(db.Model):
    """一次精确基线的代码/需求增量分析。"""
    __tablename__ = 'shift_left_analysis_runs'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'),
                           nullable=False, index=True)
    iteration_id = db.Column(db.Integer, db.ForeignKey('test_iterations.id'),
                             index=True)
    workflow_run_id = db.Column(db.Integer, db.ForeignKey('workflow_runs.id'),
                                index=True)
    report_id = db.Column(db.Integer, db.ForeignKey('test_reports.id'), index=True)
    baseline_fingerprint = db.Column(db.String(64), nullable=False, index=True)
    rerun_sequence = db.Column(db.Integer, nullable=False, default=0)
    rerun_of_id = db.Column(db.Integer,
                            db.ForeignKey('shift_left_analysis_runs.id'), index=True)
    rerun_reason = db.Column(db.String(500), default='')
    status = db.Column(db.String(32), nullable=False, default='pending', index=True)
    baseline_json = db.Column(db.JSON, nullable=False)
    context_snapshot_json = db.Column(db.JSON)
    result_summary_json = db.Column(db.JSON)
    output_schema_version = db.Column(db.String(32), default='v1')
    created_by = db.Column(db.String(120), default='system')
    updated_by = db.Column(db.String(120), default='system')
    revision = db.Column(db.Integer, nullable=False, default=1)
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)
    finished_at = db.Column(db.DateTime)

    __table_args__ = (
        db.UniqueConstraint(
            'project_id', 'baseline_fingerprint', 'rerun_sequence',
            name='uq_shift_left_analysis_baseline_rerun'),
        db.Index('ix_shift_left_analysis_project_status', 'project_id', 'status'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'project_id': self.project_id,
            'iteration_id': self.iteration_id,
            'workflow_run_id': self.workflow_run_id,
            'report_id': self.report_id,
            'baseline_fingerprint': self.baseline_fingerprint,
            'rerun_sequence': self.rerun_sequence or 0,
            'rerun_of_id': self.rerun_of_id,
            'rerun_reason': self.rerun_reason or '',
            'status': self.status,
            'baseline': self.baseline_json or {},
            'context_snapshot': self.context_snapshot_json or {},
            'result_summary': self.result_summary_json or {},
            'output_schema_version': self.output_schema_version or 'v1',
            'created_by': self.created_by,
            'updated_by': self.updated_by,
            'revision': self.revision or 1,
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
            'finished_at': str(self.finished_at) if self.finished_at else None,
        }


class WorkflowEvidenceManifest(db.Model):
    """One revisioned evidence index and post-run verdict per Workflow Run."""
    __tablename__ = 'workflow_evidence_manifests'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'),
                           nullable=False, index=True)
    workflow_run_id = db.Column(
        db.Integer, db.ForeignKey('workflow_runs.id', ondelete='CASCADE'),
        nullable=False, unique=True, index=True)
    analysis_run_id = db.Column(
        db.Integer, db.ForeignKey('shift_left_analysis_runs.id'), index=True)
    coverage_json = db.Column(db.JSON, nullable=False)
    artifacts_json = db.Column(db.JSON, nullable=False)
    required_evidence_json = db.Column(db.JSON, nullable=False)
    completeness_status = db.Column(
        db.String(32), nullable=False, default='incomplete', index=True)
    missing_required_json = db.Column(db.JSON)
    classification = db.Column(db.String(40), index=True)
    analysis_summary_json = db.Column(db.JSON)
    finding_ids_json = db.Column(db.JSON)
    analyzed_by = db.Column(db.String(160), default='')
    analyzed_at = db.Column(db.DateTime)
    revision = db.Column(db.Integer, nullable=False, default=1)
    created_by = db.Column(db.String(160), default='system')
    updated_by = db.Column(db.String(160), default='system')
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now, index=True)

    __table_args__ = (
        db.Index(
            'ix_workflow_evidence_project_status',
            'project_id', 'completeness_status'),
        db.Index(
            'ix_workflow_evidence_project_updated',
            'project_id', 'updated_at'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'project_id': self.project_id,
            'workflow_run_id': self.workflow_run_id,
            'analysis_run_id': self.analysis_run_id,
            'coverage': self.coverage_json or {},
            'artifacts': self.artifacts_json or [],
            'required_evidence': self.required_evidence_json or [],
            'completeness_status': self.completeness_status or 'incomplete',
            'missing_required': self.missing_required_json or [],
            'classification': self.classification,
            'analysis_summary': self.analysis_summary_json or {},
            'finding_ids': self.finding_ids_json or [],
            'analyzed_by': self.analyzed_by or '',
            'analyzed_at': str(self.analyzed_at) if self.analyzed_at else None,
            'revision': self.revision or 1,
            'created_by': self.created_by or 'system',
            'updated_by': self.updated_by or 'system',
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }


class ShiftLeftFinding(db.Model):
    """跨 Analysis Run 持续存在的结构化风险对象。"""
    __tablename__ = 'shift_left_findings'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'),
                           nullable=False, index=True)
    analysis_run_id = db.Column(
        db.Integer, db.ForeignKey('shift_left_analysis_runs.id'),
        nullable=False, index=True)
    finding_key = db.Column(db.String(255), nullable=False)
    title = db.Column(db.String(300), nullable=False)
    module = db.Column(db.String(160), default='', index=True)
    severity = db.Column(db.String(20), nullable=False, default='medium', index=True)
    confidence = db.Column(db.Float, nullable=False, default=0.0)
    evidence_level = db.Column(db.String(32), nullable=False,
                               default='hypothesis', index=True)
    source_type = db.Column(db.String(40), nullable=False, default='agent')
    status = db.Column(db.String(32), nullable=False, default='new', index=True)
    owner = db.Column(db.String(120), default='', index=True)
    description = db.Column(db.Text)
    business_impact = db.Column(db.Text)
    context_quality = db.Column(db.String(20), default='fresh')
    code_locations_json = db.Column(db.JSON)
    associations_json = db.Column(db.JSON)
    fix_ref = db.Column(db.String(500), default='')
    verification_ref = db.Column(db.String(500), default='')
    fix_actor_key = db.Column(db.String(80), default='')
    verification_actor_key = db.Column(db.String(80), default='')
    status_reason = db.Column(db.Text)
    first_seen_at = db.Column(db.DateTime, default=_now)
    last_seen_at = db.Column(db.DateTime, default=_now, index=True)
    created_by = db.Column(db.String(120), default='system')
    created_by_actor_key = db.Column(db.String(80), default='system:0')
    updated_by = db.Column(db.String(120), default='system')
    revision = db.Column(db.Integer, nullable=False, default=1)
    is_archived = db.Column(db.Boolean, nullable=False, default=False, index=True)
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    __table_args__ = (
        db.UniqueConstraint('project_id', 'finding_key',
                            name='uq_shift_left_finding_project_key'),
        db.Index('ix_shift_left_findings_project_state',
                 'project_id', 'status', 'severity'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'project_id': self.project_id,
            'analysis_run_id': self.analysis_run_id,
            'finding_key': self.finding_key,
            'title': self.title,
            'module': self.module or '',
            'severity': self.severity,
            'confidence': self.confidence or 0.0,
            'evidence_level': self.evidence_level,
            'source_type': self.source_type,
            'status': self.status,
            'owner': self.owner or '',
            'description': self.description or '',
            'business_impact': self.business_impact or '',
            'context_quality': self.context_quality or 'fresh',
            'code_locations': self.code_locations_json or [],
            'associations': self.associations_json or {},
            'fix_ref': self.fix_ref or '',
            'verification_ref': self.verification_ref or '',
            'status_reason': self.status_reason or '',
            'first_seen_at': str(self.first_seen_at) if self.first_seen_at else None,
            'last_seen_at': str(self.last_seen_at) if self.last_seen_at else None,
            'created_by': self.created_by,
            'updated_by': self.updated_by,
            'revision': self.revision or 1,
            'is_archived': bool(self.is_archived),
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }


class ShiftLeftFindingEvent(db.Model):
    """Finding 的追加式证据、备注、评审结论和状态审计。"""
    __tablename__ = 'shift_left_finding_events'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    finding_id = db.Column(db.Integer, db.ForeignKey('shift_left_findings.id',
                                                      ondelete='CASCADE'),
                           nullable=False, index=True)
    event_type = db.Column(db.String(32), nullable=False, index=True)
    payload_json = db.Column(db.JSON, nullable=False)
    actor_type = db.Column(db.String(24), nullable=False)
    actor_id = db.Column(db.Integer, nullable=False)
    actor_key = db.Column(db.String(80), nullable=False)
    actor_name = db.Column(db.String(120), default='')
    idempotency_key = db.Column(db.String(128))
    created_at = db.Column(db.DateTime, default=_now, index=True)

    __table_args__ = (
        db.UniqueConstraint(
            'finding_id', 'actor_key', 'idempotency_key',
            name='uq_shift_left_finding_event_idempotency'),
        db.Index('ix_shift_left_finding_event_type', 'finding_id', 'event_type'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'finding_id': self.finding_id,
            'event_type': self.event_type,
            'payload': self.payload_json or {},
            'actor_type': self.actor_type,
            'actor_id': self.actor_id,
            'actor_name': self.actor_name or '',
            'created_at': str(self.created_at) if self.created_at else None,
        }


class ShiftLeftAnalysisFinding(db.Model):
    """Analysis Run 与稳定 Finding 的出现关系及当时快照。"""
    __tablename__ = 'shift_left_analysis_findings'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    analysis_run_id = db.Column(
        db.Integer, db.ForeignKey('shift_left_analysis_runs.id', ondelete='CASCADE'),
        nullable=False, index=True)
    finding_id = db.Column(
        db.Integer, db.ForeignKey('shift_left_findings.id', ondelete='CASCADE'),
        nullable=False, index=True)
    snapshot_json = db.Column(db.JSON, nullable=False)
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    __table_args__ = (
        db.UniqueConstraint('analysis_run_id', 'finding_id',
                            name='uq_shift_left_analysis_finding'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'analysis_run_id': self.analysis_run_id,
            'finding_id': self.finding_id,
            'snapshot': self.snapshot_json or {},
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }


class ShiftLeftFindingFeedback(db.Model):
    """Immutable normalized truth label attached to one Finding."""
    __tablename__ = 'shift_left_finding_feedback'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    project_id = db.Column(
        db.Integer, db.ForeignKey('projects.id'), nullable=False, index=True)
    finding_id = db.Column(
        db.Integer, db.ForeignKey('shift_left_findings.id', ondelete='CASCADE'),
        nullable=False, index=True)
    label = db.Column(db.String(48), nullable=False, index=True)
    note = db.Column(db.Text)
    evidence_refs_json = db.Column(db.JSON)
    source_type = db.Column(db.String(40), default='human')
    actor_type = db.Column(db.String(24), nullable=False)
    actor_id = db.Column(db.Integer, nullable=False)
    actor_key = db.Column(db.String(160), nullable=False)
    actor_name = db.Column(db.String(160), default='')
    idempotency_key = db.Column(db.String(128), nullable=False)
    request_hash = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, default=_now, index=True)

    finding = db.relationship(
        'ShiftLeftFinding', backref=db.backref(
            'feedback_items', cascade='all, delete-orphan',
            order_by='ShiftLeftFindingFeedback.id'))

    __table_args__ = (
        db.UniqueConstraint(
            'finding_id', 'actor_key', 'idempotency_key',
            name='uq_finding_feedback_actor_idempotency'),
        db.Index(
            'ix_finding_feedback_project_label', 'project_id', 'label'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'project_id': self.project_id,
            'finding_id': self.finding_id,
            'label': self.label,
            'note': self.note or '',
            'evidence_refs': self.evidence_refs_json or [],
            'source_type': self.source_type or 'human',
            'actor_type': self.actor_type,
            'actor_id': self.actor_id,
            'actor_name': self.actor_name or '',
            'created_at': str(self.created_at) if self.created_at else None,
        }


class AnalysisRule(db.Model):
    """Stable project-scoped identity for one learned analysis rule."""
    __tablename__ = 'analysis_rules'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    project_id = db.Column(
        db.Integer, db.ForeignKey('projects.id'), nullable=False, index=True)
    rule_key = db.Column(db.String(255), nullable=False)
    name = db.Column(db.String(300), nullable=False)
    description = db.Column(db.Text)
    status = db.Column(db.String(32), nullable=False, default='draft', index=True)
    active_version_id = db.Column(db.Integer, index=True)
    latest_version = db.Column(db.Integer, nullable=False, default=0)
    version = db.Column(db.Integer, nullable=False, default=1)
    created_by = db.Column(db.String(160), default='system')
    updated_by = db.Column(db.String(160), default='system')
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now, index=True)

    __table_args__ = (
        db.UniqueConstraint(
            'project_id', 'rule_key', name='uq_analysis_rule_project_key'),
        db.Index('ix_analysis_rule_project_status', 'project_id', 'status'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'project_id': self.project_id,
            'rule_key': self.rule_key,
            'name': self.name,
            'description': self.description or '',
            'status': self.status,
            'active_version_id': self.active_version_id,
            'latest_version': self.latest_version or 0,
            'version': self.version or 1,
            'created_by': self.created_by or 'system',
            'updated_by': self.updated_by or 'system',
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }


class AnalysisRuleVersion(db.Model):
    """Immutable rule definition promoted only through replay gates."""
    __tablename__ = 'analysis_rule_versions'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    rule_id = db.Column(
        db.Integer, db.ForeignKey('analysis_rules.id', ondelete='CASCADE'),
        nullable=False, index=True)
    version_no = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(32), nullable=False, default='draft', index=True)
    definition_json = db.Column(db.JSON, nullable=False)
    thresholds_json = db.Column(db.JSON, nullable=False)
    change_summary = db.Column(db.Text)
    based_on_version_id = db.Column(
        db.Integer, db.ForeignKey('analysis_rule_versions.id'), index=True)
    source_finding_ids_json = db.Column(db.JSON)
    definition_hash = db.Column(db.String(64), nullable=False, index=True)
    activated_by = db.Column(db.String(160), default='')
    activated_at = db.Column(db.DateTime)
    retired_by = db.Column(db.String(160), default='')
    retired_at = db.Column(db.DateTime)
    created_by = db.Column(db.String(160), default='system')
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    rule = db.relationship(
        'AnalysisRule', backref=db.backref(
            'versions', cascade='all, delete-orphan',
            order_by='AnalysisRuleVersion.version_no'))

    __table_args__ = (
        db.UniqueConstraint(
            'rule_id', 'version_no', name='uq_analysis_rule_version_no'),
        db.UniqueConstraint(
            'rule_id', 'definition_hash', name='uq_analysis_rule_definition_hash'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'rule_id': self.rule_id,
            'version_no': self.version_no,
            'status': self.status,
            'definition': self.definition_json or {},
            'thresholds': self.thresholds_json or {},
            'change_summary': self.change_summary or '',
            'based_on_version_id': self.based_on_version_id,
            'source_finding_ids': self.source_finding_ids_json or [],
            'definition_hash': self.definition_hash,
            'activated_by': self.activated_by or '',
            'activated_at': str(self.activated_at) if self.activated_at else None,
            'retired_by': self.retired_by or '',
            'retired_at': str(self.retired_at) if self.retired_at else None,
            'created_by': self.created_by or 'system',
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }


class AnalysisRuleReplay(db.Model):
    """Audited historical replay metrics for one immutable rule version."""
    __tablename__ = 'analysis_rule_replays'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    rule_version_id = db.Column(
        db.Integer, db.ForeignKey('analysis_rule_versions.id', ondelete='CASCADE'),
        nullable=False, index=True)
    workflow_run_id = db.Column(
        db.Integer, db.ForeignKey('workflow_runs.id'), index=True)
    dataset_snapshot_ref = db.Column(db.String(1000), nullable=False)
    dataset_hash = db.Column(db.String(64), nullable=False, index=True)
    metrics_json = db.Column(db.JSON, nullable=False)
    impact_json = db.Column(db.JSON)
    artifact_refs_json = db.Column(db.JSON)
    gate_passed = db.Column(db.Boolean, nullable=False, default=False, index=True)
    gate_reasons_json = db.Column(db.JSON)
    status = db.Column(db.String(32), nullable=False, default='completed')
    idempotency_key = db.Column(db.String(128), nullable=False)
    request_hash = db.Column(db.String(64), nullable=False)
    actor_type = db.Column(db.String(24), nullable=False)
    actor_id = db.Column(db.Integer, nullable=False)
    actor_name = db.Column(db.String(160), default='')
    created_at = db.Column(db.DateTime, default=_now, index=True)

    rule_version = db.relationship(
        'AnalysisRuleVersion', backref=db.backref(
            'replays', cascade='all, delete-orphan',
            order_by='AnalysisRuleReplay.id'))

    __table_args__ = (
        db.UniqueConstraint(
            'rule_version_id', 'actor_type', 'actor_id', 'idempotency_key',
            name='uq_analysis_rule_replay_actor_idempotency'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'rule_version_id': self.rule_version_id,
            'workflow_run_id': self.workflow_run_id,
            'dataset_snapshot_ref': self.dataset_snapshot_ref,
            'dataset_hash': self.dataset_hash,
            'metrics': self.metrics_json or {},
            'impact': self.impact_json or {},
            'artifact_refs': self.artifact_refs_json or [],
            'gate_passed': bool(self.gate_passed),
            'gate_reasons': self.gate_reasons_json or [],
            'status': self.status,
            'actor_type': self.actor_type,
            'actor_id': self.actor_id,
            'actor_name': self.actor_name or '',
            'created_at': str(self.created_at) if self.created_at else None,
        }


class CollaborationSession(db.Model):
    """可撤销邀请链接签发的任务级短期 API 会话。"""
    __tablename__ = 'collaboration_sessions'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'),
                           nullable=True, index=True)
    subject_type = db.Column(db.String(32), nullable=False, index=True)
    subject_id = db.Column(db.Integer, nullable=False, index=True)
    agent_identity = db.Column(db.String(160), nullable=False)
    scopes_json = db.Column(db.JSON, nullable=False)
    invitation_hash = db.Column(db.String(64), nullable=False, unique=True)
    access_token_hash = db.Column(db.String(64), unique=True)
    status = db.Column(db.String(20), nullable=False, default='pending', index=True)
    invitation_expires_at = db.Column(db.DateTime, nullable=False, index=True)
    token_expires_at = db.Column(db.DateTime, index=True)
    token_ttl_seconds = db.Column(db.Integer, nullable=False, default=7200)
    max_calls = db.Column(db.Integer, nullable=False, default=500)
    call_count = db.Column(db.Integer, nullable=False, default=0)
    created_by_type = db.Column(db.String(24), nullable=False)
    created_by_id = db.Column(db.Integer, nullable=False)
    created_by_name = db.Column(db.String(120), default='')
    creator_actor_key = db.Column(db.String(80), nullable=False)
    create_idempotency_key = db.Column(db.String(128), nullable=False)
    create_request_hash = db.Column(db.String(64), nullable=False)
    parent_invite_id = db.Column(
        db.Integer, nullable=True, index=True,
        comment='兑换该 Token 会话的根邀请 ID；NULL 表示邀请或旧版会话')
    exchanged_at = db.Column(db.DateTime)
    last_activity_at = db.Column(db.DateTime)
    revoked_at = db.Column(db.DateTime)
    revoked_by = db.Column(db.String(120), default='')
    revoke_reason = db.Column(db.String(500), default='')
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    __table_args__ = (
        db.Index('ix_collaboration_subject', 'subject_type', 'subject_id'),
        db.Index('ix_collaboration_project_status', 'project_id', 'status'),
        db.UniqueConstraint(
            'creator_actor_key', 'create_idempotency_key',
            name='uq_collaboration_creator_idempotency'),
    )

    def effective_status(self, now=None):
        now = now or _now()
        if self.status in ('revoked', 'completed'):
            return self.status
        if self.status == 'pending' and self.invitation_expires_at <= now:
            return 'expired'
        if (self.status == 'active' and self.parent_invite_id is None
                and not self.access_token_hash
                and self.invitation_expires_at <= now):
            return 'expired'
        if self.status == 'active' and self.token_expires_at and self.token_expires_at <= now:
            return 'expired'
        if self.max_calls and (self.call_count or 0) >= self.max_calls:
            return 'exhausted'
        return self.status

    def has_scope(self, scope):
        return scope in (self.scopes_json or [])

    def to_dict(self):
        return {
            'id': self.id,
            'project_id': self.project_id,
            'subject': {'type': self.subject_type, 'id': self.subject_id},
            'agent_identity': self.agent_identity,
            'invite_id': self.parent_invite_id or self.id,
            'participant_session': self.parent_invite_id is not None,
            'scopes': self.scopes_json or [],
            'status': self.effective_status(),
            'invitation_expires_at': str(self.invitation_expires_at),
            'token_expires_at': str(self.token_expires_at) if self.token_expires_at else None,
            'token_ttl_seconds': self.token_ttl_seconds or 7200,
            'max_calls': self.max_calls,
            'call_count': self.call_count or 0,
            'created_by': self.created_by_name,
            'exchanged_at': str(self.exchanged_at) if self.exchanged_at else None,
            'last_activity_at': str(self.last_activity_at) if self.last_activity_at else None,
            'revoked_at': str(self.revoked_at) if self.revoked_at else None,
            'revoked_by': self.revoked_by or '',
            'revoke_reason': self.revoke_reason or '',
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }


class CollaborationSessionEvent(db.Model):
    """临时协作会话生命周期审计（不保存邀请码或 Token 明文）。"""
    __tablename__ = 'collaboration_session_events'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    session_id = db.Column(db.Integer, db.ForeignKey('collaboration_sessions.id',
                                                      ondelete='CASCADE'),
                           nullable=False, index=True)
    event_type = db.Column(db.String(32), nullable=False, index=True)
    actor_type = db.Column(db.String(24), nullable=False)
    actor_id = db.Column(db.Integer, nullable=False)
    actor_name = db.Column(db.String(160), default='')
    detail_json = db.Column(db.JSON)
    created_at = db.Column(db.DateTime, default=_now, index=True)

    def to_dict(self):
        return {
            'id': self.id,
            'session_id': self.session_id,
            'event_type': self.event_type,
            'actor_type': self.actor_type,
            'actor_id': self.actor_id,
            'actor_name': self.actor_name or '',
            'detail': self.detail_json or {},
            'created_at': str(self.created_at) if self.created_at else None,
        }


# ============== Long-running Agent control plane pilot ==============


class AgentGoal(db.Model):
    """Durable objective that survives Worker and model-session restarts."""
    __tablename__ = 'agent_goals'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    project_id = db.Column(
        db.Integer, db.ForeignKey('projects.id'), nullable=False, index=True)
    title = db.Column(db.String(255), nullable=False)
    objective = db.Column(db.Text, nullable=False)
    scope_json = db.Column(db.JSON)
    non_goals_json = db.Column(db.JSON)
    authority_sources_json = db.Column(db.JSON)
    current_belief = db.Column(db.Text, default='')
    next_action = db.Column(db.Text, default='')
    status = db.Column(db.String(32), nullable=False, default='DRAFT', index=True)
    priority = db.Column(db.String(16), nullable=False, default='P1', index=True)
    compute_quota = db.Column(db.Integer, nullable=False, default=0)
    control_mode = db.Column(
        db.String(32), nullable=False, default='shadow', index=True)
    version = db.Column(db.Integer, nullable=False, default=1)
    created_by_type = db.Column(db.String(24), nullable=False, default='user')
    created_by_id = db.Column(db.Integer, nullable=False)
    created_by_name = db.Column(db.String(160), default='')
    created_at = db.Column(db.DateTime, default=_now, index=True)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    __table_args__ = (
        db.Index('ix_agent_goal_project_status', 'project_id', 'status'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'project_id': self.project_id,
            'title': self.title,
            'objective': self.objective,
            'scope': self.scope_json or {},
            'non_goals': self.non_goals_json or [],
            'authority_sources': self.authority_sources_json or [],
            'current_belief': self.current_belief or '',
            'next_action': self.next_action or '',
            'status': self.status,
            'priority': self.priority,
            'compute_quota': self.compute_quota or 0,
            'control_mode': self.control_mode or 'shadow',
            'version': self.version,
            'created_by': self.created_by_name or '',
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }


class GoalTodo(db.Model):
    """Versioned work item with a monotonic fencing generation."""
    __tablename__ = 'goal_todos'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    goal_id = db.Column(
        db.Integer, db.ForeignKey('agent_goals.id', ondelete='CASCADE'),
        nullable=False, index=True)
    title = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text, default='')
    task_class = db.Column(
        db.String(32), nullable=False, default='advancement_task', index=True)
    action_kind = db.Column(db.String(64), nullable=False, default='analyze')
    priority = db.Column(db.String(16), nullable=False, default='P1', index=True)
    status = db.Column(db.String(24), nullable=False, default='open', index=True)
    assigned_agent_id = db.Column(db.Integer, index=True)
    claimed_by_worker_id = db.Column(db.String(160), default='', index=True)
    claimed_claw_id = db.Column(db.Integer, index=True)
    claimed_at = db.Column(db.DateTime)
    claim_expires_at = db.Column(db.DateTime, index=True)
    claim_lease_seconds = db.Column(db.Integer, nullable=False, default=180)
    fencing_token = db.Column(db.Integer, nullable=False, default=0)
    authorization_envelope_json = db.Column(db.JSON)
    required_capabilities_json = db.Column(db.JSON)
    required_write_scopes_json = db.Column(db.JSON)
    resume_when_json = db.Column(db.JSON)
    completion_criteria_json = db.Column(db.JSON)
    verification_policy_json = db.Column(db.JSON)
    version = db.Column(db.Integer, nullable=False, default=1)
    created_at = db.Column(db.DateTime, default=_now, index=True)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    __table_args__ = (
        db.Index(
            'ix_goal_todo_frontier', 'goal_id', 'status', 'priority', 'id'),
        db.Index(
            'ix_goal_todo_claim_expiry', 'status', 'claim_expires_at'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'goal_id': self.goal_id,
            'title': self.title,
            'description': self.description or '',
            'task_class': self.task_class,
            'action_kind': self.action_kind,
            'priority': self.priority,
            'status': self.status,
            'assigned_agent_id': self.assigned_agent_id,
            'claimed_by_worker_id': self.claimed_by_worker_id or '',
            'claimed_claw_id': self.claimed_claw_id,
            'claimed_at': str(self.claimed_at) if self.claimed_at else None,
            'claim_expires_at': (
                str(self.claim_expires_at) if self.claim_expires_at else None),
            'claim_lease_seconds': self.claim_lease_seconds or 180,
            'fencing_token': self.fencing_token or 0,
            'authorization_envelope': self.authorization_envelope_json or {},
            'required_capabilities': self.required_capabilities_json or [],
            'required_write_scopes': self.required_write_scopes_json or [],
            'resume_when': self.resume_when_json or {},
            'completion_criteria': self.completion_criteria_json or {},
            'verification_policy': self.verification_policy_json or {},
            'version': self.version,
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }


class AgentControlGate(db.Model):
    """Auditable user/authority decision scoped to one lane or work item."""
    __tablename__ = 'agent_control_gates'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    goal_id = db.Column(
        db.Integer, db.ForeignKey('agent_goals.id', ondelete='CASCADE'),
        nullable=False, index=True)
    work_item_type = db.Column(db.String(32), nullable=False, default='goal_todo')
    work_item_id = db.Column(db.Integer, index=True)
    turn_id = db.Column(db.String(80), index=True)
    gate_type = db.Column(db.String(40), nullable=False, index=True)
    policy_level = db.Column(db.String(16), nullable=False, default='hard')
    status = db.Column(db.String(20), nullable=False, default='open', index=True)
    blocking_scope = db.Column(
        db.String(20), nullable=False, default='work_item')
    question = db.Column(db.Text, nullable=False)
    reason_code = db.Column(db.String(80), nullable=False)
    requested_scope_json = db.Column(db.JSON)
    safe_fallback_json = db.Column(db.JSON)
    decision_options_json = db.Column(db.JSON)
    requested_by = db.Column(db.String(160), nullable=False)
    resolved_by = db.Column(db.String(160), default='')
    resolution = db.Column(db.String(40), default='')
    resolution_note = db.Column(db.Text, default='')
    expires_at = db.Column(db.DateTime, index=True)
    version = db.Column(db.Integer, nullable=False, default=1)
    idempotency_key = db.Column(db.String(128), nullable=False)
    request_hash = db.Column(db.String(64), nullable=False)
    resolution_idempotency_key = db.Column(db.String(128))
    resolution_request_hash = db.Column(db.String(64))
    created_at = db.Column(db.DateTime, default=_now, index=True)
    resolved_at = db.Column(db.DateTime)

    __table_args__ = (
        db.UniqueConstraint(
            'goal_id', 'idempotency_key',
            name='uq_agent_control_gate_goal_idempotency'),
        db.Index(
            'ix_agent_control_gate_active', 'goal_id', 'status',
            'policy_level', 'work_item_id'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'goal_id': self.goal_id,
            'work_item_type': self.work_item_type,
            'work_item_id': self.work_item_id,
            'turn_id': self.turn_id,
            'gate_type': self.gate_type,
            'policy_level': self.policy_level,
            'status': self.status,
            'blocking_scope': self.blocking_scope,
            'question': self.question,
            'reason_code': self.reason_code,
            'requested_scope': self.requested_scope_json or {},
            'safe_fallback': self.safe_fallback_json or {},
            'decision_options': self.decision_options_json or [],
            'requested_by': self.requested_by,
            'resolved_by': self.resolved_by or '',
            'resolution': self.resolution or '',
            'resolution_note': self.resolution_note or '',
            'expires_at': str(self.expires_at) if self.expires_at else None,
            'version': self.version,
            'created_at': str(self.created_at) if self.created_at else None,
            'resolved_at': str(self.resolved_at) if self.resolved_at else None,
        }


class AgentTurn(db.Model):
    """One bounded dispatch result; unique per caller idempotency key."""
    __tablename__ = 'agent_turns'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    turn_id = db.Column(db.String(80), nullable=False, unique=True, index=True)
    goal_id = db.Column(
        db.Integer, db.ForeignKey('agent_goals.id', ondelete='CASCADE'),
        nullable=False, index=True)
    todo_id = db.Column(
        db.Integer, db.ForeignKey('goal_todos.id'), index=True)
    # Non-NULL only while the Turn is active. The unique slot closes the
    # cross-process should-run/claim race even when SELECT FOR UPDATE is weak.
    active_todo_slot = db.Column(db.String(80), unique=True, index=True)
    todo_version = db.Column(db.Integer)
    claw_id = db.Column(db.Integer, index=True)
    agent_id = db.Column(db.Integer, index=True)
    worker_id = db.Column(db.String(160), nullable=False, index=True)
    execution_mode = db.Column(
        db.String(32), nullable=False, default='legacy_unmanaged')
    route = db.Column(db.String(40), nullable=False)
    status = db.Column(db.String(24), nullable=False, default='dispatched', index=True)
    result_kind = db.Column(db.String(40), default='')
    fencing_token = db.Column(db.Integer, nullable=False, default=0)
    claim_expires_at = db.Column(db.DateTime)
    schedule_version = db.Column(db.Integer, nullable=False, default=1)
    idempotency_key = db.Column(db.String(128), nullable=False)
    request_hash = db.Column(db.String(64), nullable=False)
    envelope_json = db.Column(db.JSON, nullable=False)
    effect_manifest_json = db.Column(db.JSON)
    authorization_payload_json = db.Column(db.JSON)
    authorization_signature = db.Column(db.Text)
    authorization_key_id = db.Column(db.String(80), default='')
    authorization_idempotency_key = db.Column(db.String(128))
    authorization_request_hash = db.Column(db.String(64))
    quota_reserved = db.Column(db.Boolean, nullable=False, default=False)
    deadline_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=_now, index=True)
    completed_at = db.Column(db.DateTime)

    __table_args__ = (
        db.UniqueConstraint(
            'goal_id', 'worker_id', 'idempotency_key',
            name='uq_agent_turn_dispatch_idempotency'),
    )

    def to_dict(self):
        return {
            'turn_id': self.turn_id,
            'goal_id': self.goal_id,
            'todo_id': self.todo_id,
            'todo_version': self.todo_version,
            'claw_id': self.claw_id,
            'agent_id': self.agent_id,
            'worker_id': self.worker_id,
            'execution_mode': self.execution_mode or 'legacy_unmanaged',
            'route': self.route,
            'status': self.status,
            'result_kind': self.result_kind or '',
            'fencing_token': self.fencing_token or 0,
            'claim_expires_at': (
                str(self.claim_expires_at) if self.claim_expires_at else None),
            'schedule_version': self.schedule_version,
            'idempotency_key': self.idempotency_key,
            'envelope': self.envelope_json or {},
            'effect_manifest': self.effect_manifest_json or {},
            'authorization': self.authorization_payload_json or {},
            'authorization_signature': self.authorization_signature or '',
            'authorization_key_id': self.authorization_key_id or '',
            'effect_authorization_ready': bool(
                self.authorization_payload_json and self.authorization_signature),
            'quota_reserved': bool(self.quota_reserved),
            'deadline_at': str(self.deadline_at) if self.deadline_at else None,
            'created_at': str(self.created_at) if self.created_at else None,
            'completed_at': str(self.completed_at) if self.completed_at else None,
        }


class AgentTransitionReceipt(db.Model):
    """Append-only accepted state transition for one bounded turn."""
    __tablename__ = 'agent_transition_receipts'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    transition_id = db.Column(db.String(128), nullable=False, unique=True, index=True)
    request_hash = db.Column(db.String(64), nullable=False)
    turn_id = db.Column(
        db.String(80), db.ForeignKey('agent_turns.turn_id'),
        nullable=False, index=True)
    goal_id = db.Column(db.Integer, nullable=False, index=True)
    todo_id = db.Column(db.Integer, index=True)
    todo_version = db.Column(db.Integer)
    worker_id = db.Column(db.String(160), nullable=False)
    fencing_token = db.Column(db.Integer, nullable=False, default=0)
    from_state = db.Column(db.String(24), nullable=False)
    to_state = db.Column(db.String(24), nullable=False)
    result_kind = db.Column(db.String(40), nullable=False)
    summary = db.Column(db.Text, default='')
    verification_json = db.Column(db.JSON, nullable=False)
    evidence_json = db.Column(db.JSON)
    effect_receipts_json = db.Column(db.JSON)
    created_at = db.Column(db.DateTime, default=_now, index=True)

    def to_dict(self):
        return {
            'transition_id': self.transition_id,
            'turn_id': self.turn_id,
            'goal_id': self.goal_id,
            'todo_id': self.todo_id,
            'todo_version': self.todo_version,
            'worker_id': self.worker_id,
            'fencing_token': self.fencing_token,
            'from_state': self.from_state,
            'to_state': self.to_state,
            'result_kind': self.result_kind,
            'summary': self.summary or '',
            'verification': self.verification_json or {},
            'evidence': self.evidence_json or {},
            'effect_receipts': self.effect_receipts_json or [],
            'created_at': str(self.created_at) if self.created_at else None,
        }
