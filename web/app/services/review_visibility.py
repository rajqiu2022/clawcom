"""课题（含用例评审）可见性判定。

四档可见性：

- ``public_all``  完全公开：已登录的用户或 Agent 均可查看与参与，**不校验项目权限**
- ``public``      Hub 用户或 Agent：沿用历史语义，登录 + 有项目权限
- ``project``     项目内用户或 Agent：调用者项目需命中课题项目
- ``assigned``    指定用户或 Agent：需命中 ``topic_grants`` 授权

设计约束：本模块顶层只依赖标准库，纯判定函数可脱离 Flask 应用上下文单测；
需要查库的辅助函数在函数体内部再 import ``app``，与仓库既有 service 写法一致。
"""

VISIBILITY_PUBLIC_ALL = 'public_all'
VISIBILITY_PUBLIC = 'public'
VISIBILITY_PROJECT = 'project'
VISIBILITY_ASSIGNED = 'assigned'

VALID_VISIBILITIES = (
    VISIBILITY_PUBLIC_ALL,
    VISIBILITY_PUBLIC,
    VISIBILITY_PROJECT,
    VISIBILITY_ASSIGNED,
)

DEFAULT_VISIBILITY = VISIBILITY_PUBLIC


# 允许无项目权限用户访问的评审路径。
# 只放开"公开评审必需"的读接口与回复接口；发起课题、用例库接口一律不豁免。
# 逐条可见性判定不受影响，无项目用户依然只能看到 public_all 课题。
_EXEMPT_EXACT = {
    ('GET', '/api/v1/topics'),
    ('GET', '/api/v1/topics/boards'),
    ('GET', '/api/v1/topics/visibilities'),
}

_EXEMPT_TOPIC_SUFFIXES = {
    ('GET', ''),                  # GET /api/v1/topics/<id>
    ('POST', '/replies'),         # 公开评审需要能评论
    ('GET', '/review-mindmap'),
    ('GET', '/review-marks'),
    ('PUT', '/review-marks'),
}

# 带动态段的豁免路径，尾段必须是纯数字，避免顺带放开别的子路径
_EXEMPT_TOPIC_PREFIXES = {
    ('GET', '/review-cases/'),    # 脑图就地展开用例的前提/步骤/预期
}


def normalize_visibility(value):
    """把入参归一化为合法可见性，非法值退回默认。"""
    value = str(value or '').strip()
    return value if value in VALID_VISIBILITIES else DEFAULT_VISIBILITY


def _caller_project_ids(caller):
    ids = set()
    for pid in ((caller or {}).get('project_ids') or []):
        try:
            ids.add(int(pid))
        except (TypeError, ValueError):
            continue
    return ids


def _topic_visibility(topic):
    return normalize_visibility(getattr(topic, 'visibility', None))


def is_project_exempt_review_path(path, method):
    """该请求是否豁免"必须先开通项目"这道粗门禁。

    仅按路径 + 方法做白名单，真正的可见性判定仍由课题接口逐条执行。
    """
    method = str(method or 'GET').upper()
    path = str(path or '').split('?', 1)[0].rstrip('/') or '/'

    if (method, path) in _EXEMPT_EXACT:
        return True

    if not path.startswith('/api/v1/topics/'):
        return False

    remainder = path[len('/api/v1/topics/'):]
    if not remainder:
        return False

    topic_seg, _, suffix = remainder.partition('/')
    if not topic_seg.isdigit():
        return False

    suffix_key = '/' + suffix if suffix else ''
    if (method, suffix_key) in _EXEMPT_TOPIC_SUFFIXES:
        return True

    for exempt_method, prefix in _EXEMPT_TOPIC_PREFIXES:
        if method == exempt_method and suffix_key.startswith(prefix):
            return suffix_key[len(prefix):].isdigit()
    return False


def can_view_topic(caller, topic, topic_project_id=None, granted=False,
                   caller_has_project_access=True):
    """是否可以查看课题。

    ``granted``                   调用者是否命中该课题的定向授权（由调用方查库后传入）
    ``caller_has_project_access`` 调用者是否拥有任何项目权限
    """
    if not caller:
        return False

    visibility = _topic_visibility(topic)

    if visibility == VISIBILITY_PUBLIC_ALL:
        return True

    if caller.get('role') == 'super_admin':
        return True

    if _is_topic_author(caller, topic):
        return True

    if granted:
        return True

    if visibility == VISIBILITY_PUBLIC:
        return bool(caller_has_project_access)

    if visibility == VISIBILITY_PROJECT:
        if topic_project_id is None:
            # 课题没绑定项目：退化为 Hub 内可见，避免历史数据不可访问
            return bool(caller_has_project_access)
        return int(topic_project_id) in _caller_project_ids(caller)

    # assigned：未命中授权 → 不可见
    return False


def can_comment_topic(caller, topic, topic_project_id=None, granted=False,
                      caller_has_project_access=True):
    """是否可以在课题下发表回复/评审意见。

    参与门槛与查看一致；课题是否 open 由调用方单独判断。
    """
    return can_view_topic(
        caller, topic,
        topic_project_id=topic_project_id,
        granted=granted,
        caller_has_project_access=caller_has_project_access,
    )


def can_mark_review_nodes(caller, topic, topic_project_id=None, granted=False,
                          caller_has_project_access=True):
    """是否可以在评审脑图上打标记。

    标记只存在于评审镜像层、不回写用例库，因此门槛与"能评论"一致。
    """
    return can_comment_topic(
        caller, topic,
        topic_project_id=topic_project_id,
        granted=granted,
        caller_has_project_access=caller_has_project_access,
    )


def _is_topic_author(caller, topic):
    caller_user_id = (caller or {}).get('user_id')
    caller_claw_id = (caller or {}).get('claw_id')
    author_user_id = getattr(topic, 'author_user_id', None)
    author_claw_id = getattr(topic, 'author_claw_id', None)
    if caller_user_id and author_user_id and caller_user_id == author_user_id:
        return True
    return bool(caller_claw_id and author_claw_id and caller_claw_id == author_claw_id)


# --------------------------------------------------------------------------
# 以下函数需要数据库，内部延迟 import
# --------------------------------------------------------------------------

def granted_topic_ids(caller):
    """调用者通过 topic_grants 拿到授权的课题 ID 集合。

    授权给用户时，其名下 Agent 自动继承；反之 Agent 被直接授权也生效。
    """
    if not caller:
        return set()

    from datetime import datetime
    from app import db
    from app.models import OpenClawInstance, TopicGrant, User

    user_id = caller.get('user_id')
    claw_id = caller.get('claw_id')

    claw_ids = set()
    if claw_id:
        claw_ids.add(int(claw_id))

    user_ids = set()
    if user_id:
        user_ids.add(int(user_id))

    # Agent 调用：把它的 owner 也算进来，让"授权给人"覆盖其 Agent
    if claw_id and not user_id:
        claw = OpenClawInstance.query.get(claw_id)
        owner = (getattr(claw, 'owner', None) or '').strip()
        if owner:
            owner_user = User.query.filter_by(username=owner).first()
            if owner_user:
                user_ids.add(int(owner_user.id))

    # 用户调用：把它名下所有 Agent 也算进来
    if user_id:
        user = User.query.get(user_id)
        username = (getattr(user, 'username', None) or '').strip()
        if username:
            owned = OpenClawInstance.query.filter(
                OpenClawInstance.owner == username,
                OpenClawInstance.status != 'deleted',
            ).all()
            claw_ids.update(int(c.id) for c in owned)

    if not user_ids and not claw_ids:
        return set()

    conditions = []
    if user_ids:
        conditions.append(TopicGrant.target_user_id.in_(list(user_ids)))
    if claw_ids:
        conditions.append(TopicGrant.target_claw_id.in_(list(claw_ids)))

    now = datetime.now()
    rows = TopicGrant.query.filter(
        db.or_(*conditions),
        db.or_(TopicGrant.expires_at.is_(None), TopicGrant.expires_at > now),
    ).with_entities(TopicGrant.topic_id).all()

    return {int(row[0]) for row in rows}


def visible_topic_filter(caller, caller_has_project_access=True,
                         granted_ids=None):
    """构造课题列表/计数共用的可见性过滤条件。

    返回 SQLAlchemy 条件对象；``caller`` 为空时只放 public_all。
    """
    from app import db
    from app.models import Project, Topic

    always_visible = Topic.visibility == VISIBILITY_PUBLIC_ALL

    if not caller:
        return always_visible

    if caller.get('role') == 'super_admin':
        return db.true()

    clauses = [always_visible]

    # 自己发起的课题永远可见
    author_clauses = []
    if caller.get('user_id'):
        author_clauses.append(Topic.author_user_id == caller['user_id'])
    if caller.get('claw_id'):
        author_clauses.append(Topic.author_claw_id == caller['claw_id'])
    if author_clauses:
        clauses.append(db.or_(*author_clauses))

    if granted_ids is None:
        granted_ids = granted_topic_ids(caller)
    if granted_ids:
        clauses.append(Topic.id.in_(list(granted_ids)))

    if caller_has_project_access:
        clauses.append(Topic.visibility == VISIBILITY_PUBLIC)

    project_ids = _caller_project_ids(caller)
    if project_ids:
        project_names = [
            p.name for p in Project.query.filter(Project.id.in_(list(project_ids))).all()
        ]
        if project_names:
            clauses.append(db.and_(
                Topic.visibility == VISIBILITY_PROJECT,
                Topic.project_name.in_(project_names),
            ))

    return db.or_(*clauses)


def caller_has_any_project(caller):
    """调用者是否拥有项目权限（admin 视为有）。"""
    if not caller:
        return False
    if caller.get('role') in ('super_admin', 'admin'):
        return True
    return bool(_caller_project_ids(caller))
