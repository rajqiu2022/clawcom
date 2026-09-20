"""Durable join notifications; never assign/install skills or start work."""
from flask import current_app

from app import db
from app.models import ClawMessage, Skill, OpenClawInstance
from app.services.agent_team_activity import roster
from app.services.agent_teams import EXECUTOR_SPECIALTIES, TEAM_ROLES


TEAM_SKILLS = (
    ('agent-team-collaboration', 'Agent 团队协作与经理调度'),
    ('agent-team-member-activity', 'Agent 团队成员动态上报'),
)


def queue_missing_join_notifications(team):
    """Explicit operator backfill only; caller must hold the Team row lock.

    The automatic join path deliberately does not use this historical check:
    removing and then rejoining a team should send a fresh invitation.
    """
    people = roster(team)
    prefix = '[Agent 团队入队通知]\n你已加入团队 #%s：' % team.id
    existing = {row.claw_id for row in ClawMessage.query.filter(
        ClawMessage.sender_name == 'Hub Agent Teams',
        ClawMessage.direction == 'to_claw',
        ClawMessage.claw_id.in_(list(people)),
        ClawMessage.content.startswith(prefix)).all()}
    return queue_join_notifications(team, existing)


def queue_join_notifications(team, previous_member_ids=()):
    """Called under the Team write transaction. Return Claw IDs to wake AFTER commit.

    Updates hold the existing Team row lock and check expected_version. Comparing
    unique Claw IDs prevents role overlap or an unchanged save from sending twice.
    A failed transaction rolls back both membership and its notification rows.
    """
    people = roster(team)
    added = sorted(set(people) - set(previous_member_ids))
    if not added:
        return []
    skills = {s.name: s for s in Skill.query.filter(
        Skill.name.in_([name for name, _ in TEAM_SKILLS]),
        Skill.review_status == 'approved', Skill.visibility == 'public',
        Skill.scope == 'global', Skill.is_deleted.is_(False)).all()}
    instructions = []
    for name, title in TEAM_SKILLS:
        skill = skills.get(name)
        if skill:
            instructions.append('- %s（Skill #%s，%s）：\n'
                                '  GET /api/v1/skills/%s/raw\n'
                                '  文档包：GET /api/v1/skills/%s/pack' % (
                                    title, skill.id, name, skill.id, skill.id))
        else:
            instructions.append('- %s（%s）：当前暂无可用的公开已审核版本，请联系团队管理员发布。' % (title, name))
    labels = dict(TEAM_ROLES, primary_manager='测试经理（主）', backup_manager='测试经理（备用）')
    names = dict(OpenClawInstance.query.with_entities(
        OpenClawInstance.id, OpenClawInstance.name).filter(
            OpenClawInstance.id.in_(list(people))).all())
    roster_text = '\n'.join(
        '- #%s %s：%s' % (cid, names.get(cid, ''),
            '、'.join(labels.get(role, role) for role in person['roles']))
        for cid, person in sorted(people.items()))
    for claw_id in added:
        person = people[claw_id]
        roles = '、'.join(labels.get(role, role) for role in person['roles'])
        specialties = '、'.join(EXECUTOR_SPECIALTIES.get(s, s) for s in person['specialties'])
        content = (
            '[Agent 团队入队通知]\n'
            '你已加入团队 #%s：%s（project_id=%s，配置版本=%s）。\n'
            '你的角色：%s%s。\n'
            '本通知接收身份：claw_id=%s。团队成员（以团队角色而非显示名为准）：\n%s\n'
            '团队入口：/agent-teams?project_id=%s\n'
            '请在参与团队任务前读取并安装以下两份 Skill：\n%s\n\n'
            '使用当前 Agent 已授权的 Hub 客户端读取，不索取或输出凭据。'
            '身份不一致或工具不可用时停止并报告，不读取共享 ~/.qclaw 或其他实例配置寻找凭据。'
            '若本机已安装，请核对最新内容，无需重复安装；有受控本地 Skill 安装工具时按其规则安装，'
            '没有安装工具或权限时向团队管理员请求分配，不自行调用管理员安装接口或扩大权限。\n'
            '安装后请确认安装结果；按状态上报 Skill 先回读本团队当前任务，再如实报告工作/空闲/阻塞。'
            '已有 Worker 自动上报时复用其队列，勿启第二个写入者。'
            '未完成安装或上报不得声称已完成。\n'
            '这是入队与安装提醒，不是任务派发，不要求抢占正在运行的任务。'
            '不因此启动 Flow、领取经理任期、修改 ACL 或升级 Worker。'
            '处理前请 GET /api/v1/agent-teams/%s 核实最新成员关系；若已退队，不再执行本通知。'
        ) % (team.id, team.name, team.project_id, team.version, roles,
             ('；细分：' + specialties) if specialties else '', claw_id, roster_text, team.project_id,
             '\n'.join(instructions), team.id)
        db.session.add(ClawMessage(
            claw_id=claw_id, sender_name='Hub Agent Teams', content=content,
            msg_type='text', direction='to_claw', status='pending'))
    return added


def wake_join_recipients(claw_ids):
    """Best-effort SSE hint; persisted pending messages survive offline clients."""
    from app.api.agent_client import notify_claw
    for claw_id in claw_ids:
        try:
            notify_claw(claw_id)
        except Exception:
            # No exception body: transport diagnostics may include credentials.
            current_app.logger.warning('Team join notice pending; wake failed for claw_id=%s', claw_id)
