from datetime import datetime


CAMPAIGN_SCOPES = {'global', 'project', 'personal'}


def _project_ids(user):
    ids = set()
    for pid in (getattr(user, 'managed_projects', None) or []):
        try:
            ids.add(int(pid))
        except Exception:
            continue
    return ids


def can_launch_campaign(user, scope, project_id=None):
    """判断用户/Agent 是否能发起指定范围的考试。"""
    if not user or scope not in CAMPAIGN_SCOPES:
        return False
    role = getattr(user, 'role', None)
    if scope == 'global':
        return role == 'super_admin' or bool(getattr(user, 'is_global', False))
    if scope == 'project':
        if role == 'super_admin' or getattr(user, 'is_global', False):
            return project_id is not None
        if role != 'admin' or project_id is None:
            return False
        try:
            return int(project_id) in _project_ids(user)
        except Exception:
            return False
    return True


def agent_can_participate(campaign, agent):
    """判断 agent 是否在考试场次范围内。"""
    if not campaign or not agent:
        return False
    scope = getattr(campaign, 'scope', None)
    if scope == 'global':
        return True
    if scope == 'project':
        return (getattr(campaign, 'project_id', None) is not None
                and getattr(agent, 'project_id', None) == getattr(campaign, 'project_id', None))
    if scope == 'personal':
        return getattr(agent, 'id', None) in (getattr(campaign, 'target_claw_ids', None) or [])
    return False


def campaign_is_open(campaign, now=None):
    """考试场次当前是否允许开始/答题/提交。"""
    if not campaign or getattr(campaign, 'status', None) in ('cancelled', 'ended'):
        return False
    now = now or datetime.now()
    starts_at = getattr(campaign, 'starts_at', None)
    ends_at = getattr(campaign, 'ends_at', None)
    if starts_at and now < starts_at:
        return False
    if ends_at and now > ends_at:
        return False
    return True
