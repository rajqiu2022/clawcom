"""Team-local observations. Self reports never mutate Run, Task or Claw state."""
import hashlib
import json
import re

from app import db
from app.models import AgentTeamMemberStatus, AgentTeamMemberTask, AgentTeamMemberReport, _now
from app.services.agent_teams import TeamError, integer

STALE_SECONDS = 180
TASK_STATES = {'working', 'blocked', 'completed', 'failed', 'cancelled'}
TERMINAL = {'completed', 'failed', 'cancelled'}


def roster(team):
    people = {}
    def add(claw_id, role, specialties=()):
        if claw_id is None:
            return
        entry = people.setdefault(claw_id, {'claw_id': claw_id, 'roles': [], 'specialties': []})
        if role not in entry['roles']:
            entry['roles'].append(role)
        entry['specialties'] = sorted(set(entry['specialties']) | set(specialties))
    add(team.primary_manager_claw_id, 'primary_manager')
    add(team.backup_manager_claw_id, 'backup_manager')
    for member in team.members:
        add(member.claw_id, member.role_key, member.specialties_json or [])
    return people


def member_summary(person, claw, status, task, now=None):
    now = now or _now()
    age = max(0, int((now - status.reported_at).total_seconds())) if status else None
    last_seen = claw.last_activity
    connection_age = max(0, int((now - last_seen).total_seconds())) if last_seen else None
    # Claw.status uses human activity labels too; only heartbeat recency is evidence of contact.
    connection = 'recently_seen' if connection_age is not None and connection_age <= STALE_SECONDS and claw.status not in ('offline', 'deleted') else 'not_recently_seen'
    return dict(person, name=claw.name, connection=connection,
                connection_label='近期有连接活动' if connection == 'recently_seen' else '暂无近期连接活动',
                last_seen_at=str(last_seen) if last_seen else None,
                reported_state=status.state if status else None,
                effective_state=('stale' if age >= STALE_SECONDS else status.state) if status else 'unknown',
                summary=status.summary if status else '', version=status.version if status else 0,
                reported_at=str(status.reported_at) if status else None,
                report_age_seconds=age, stale_after_seconds=STALE_SECONDS,
                current_task=task.to_dict() if task else None, source='agent_self_report')


def _text(value, field, maximum, required=False):
    if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()):
        raise TeamError('TEAM_ACTIVITY_INVALID', field + ' 格式或长度不合法', 400)
    return value.strip()


def _key(value, field):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,96}', value):
        raise TeamError('TEAM_ACTIVITY_INVALID', field + ' 须为 1–96 位字母、数字或 ._:-', 400)
    return value


def ingest(team, claw_id, data):
    """Caller must hold the Team row lock, including first-report inserts."""
    allowed = {'event_id', 'expected_version', 'state', 'summary', 'task'}
    if set(data) - allowed:
        raise TeamError('TEAM_ACTIVITY_INVALID', '未知上报字段', 400)
    event_id = _key(data.get('event_id'), 'event_id')
    expected = integer(data.get('expected_version'), 'expected_version', 0)
    try:
        encoded = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    except (ValueError, TypeError):
        raise TeamError('TEAM_ACTIVITY_INVALID', '上报数据须为有效 JSON', 400)
    if len(encoded.encode('utf-8')) > 12000:
        raise TeamError('TEAM_ACTIVITY_INVALID', '上报内容过大', 400)
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    receipt = AgentTeamMemberReport.query.filter_by(team_id=team.id, claw_id=claw_id, event_id=event_id).first()
    if receipt:
        if receipt.request_sha256 != digest:
            raise TeamError('TEAM_ACTIVITY_EVENT_CONFLICT', '相同 event_id 不得改变请求内容')
        return dict(receipt.response_json, replayed=True)
    status = AgentTeamMemberStatus.query.filter_by(team_id=team.id, claw_id=claw_id).first()
    if expected != (status.version if status else 0):
        raise TeamError('TEAM_ACTIVITY_VERSION_CONFLICT', '状态版本已变化，请回读后上报；勿重放旧状态')
    state = data.get('state')
    if not isinstance(state, str) or state not in ('idle', 'working', 'blocked'):
        raise TeamError('TEAM_ACTIVITY_INVALID', 'state 仅支持 idle/working/blocked', 400)
    summary = _text(data.get('summary', ''), 'summary', 500)
    now = _now()
    current = db.session.get(AgentTeamMemberTask, status.current_task_id) if status and status.current_task_id else None
    payload = data.get('task')
    task = None
    if payload is not None:
        if not isinstance(payload, dict) or set(payload) - {'task_key','title','task_type','reference','status','progress_percent','progress_message'}:
            raise TeamError('TEAM_ACTIVITY_INVALID', '无效的 task 字段', 400)
        key = _key(payload.get('task_key'), 'task_key')
        title = _text(payload.get('title'), 'title', 240, True)
        kind = payload.get('task_type')
        task_state = payload.get('status')
        if not isinstance(kind, str) or not isinstance(task_state, str) or kind not in ('flow', 'bug_regression', 'code_analysis', 'other') or task_state not in TASK_STATES:
            raise TeamError('TEAM_ACTIVITY_INVALID', '无效的任务类型/状态', 400)
        reference = _text(payload.get('reference', ''), 'reference', 240)
        progress = payload.get('progress_percent')
        if progress is not None:
            integer(progress, 'progress_percent', 0, 100)
        message = _text(payload.get('progress_message', ''), 'progress_message', 4000)
        if (task_state in TERMINAL and state != 'idle') or (task_state not in TERMINAL and state != task_state):
            raise TeamError('TEAM_ACTIVITY_INVALID', '任务进行中时成员状态须一致；结束任务时须上报 idle', 400)
        if current and current.task_key != key:
            raise TeamError('TEAM_ACTIVITY_TASK_ACTIVE', '先结束当前任务，再上报新任务')
        task = AgentTeamMemberTask.query.filter_by(team_id=team.id, claw_id=claw_id, task_key=key).first()
        if task and task.status in TERMINAL:
            raise TeamError('TEAM_ACTIVITY_TASK_FINISHED', '已结束任务不可改写；重跑请使用新 task_key')
        if not task:
            if task_state in TERMINAL:
                raise TeamError('TEAM_ACTIVITY_TASK_NOT_STARTED', '须先上报任务开始，再上报结束', 400)
            task = AgentTeamMemberTask(team_id=team.id, claw_id=claw_id, task_key=key, title=title,
                                       task_type=kind, reference=reference, started_at=now)
            db.session.add(task)
        elif (title, kind, reference) != (task.title, task.task_type, task.reference):
            raise TeamError('TEAM_ACTIVITY_TASK_IDENTITY_CHANGED', '任务标题、类型和关联信息不可变；进展写入 progress_message')
        task.status, task.progress_percent, task.progress_message = task_state, progress, message
        task.updated_at = now
        task.finished_at = now if task_state in TERMINAL else None
        db.session.flush()
    elif state != 'idle' or current:
        raise TeamError('TEAM_ACTIVITY_TASK_REQUIRED', '工作/阻塞状态须带任务；空闲前须显式结束当前任务', 400)
    if not status:
        status = AgentTeamMemberStatus(team_id=team.id, claw_id=claw_id)
        db.session.add(status)
    status.state, status.summary = state, summary
    status.current_task_id = task.id if task and task.status not in TERMINAL else None
    status.reported_at, status.version = now, expected + 1
    result = {'team_id':team.id, 'claw_id':claw_id, 'version':status.version, 'state':state,
              'summary':summary, 'task':task.to_dict() if task else None, 'reported_at':str(now),
              'event_id':event_id, 'source':'agent_self_report'}
    db.session.add(AgentTeamMemberReport(team_id=team.id, claw_id=claw_id, event_id=event_id,
        request_sha256=digest, task_id=task.id if task else None, response_json=result, created_at=now))
    db.session.commit()
    return dict(result, replayed=False)
