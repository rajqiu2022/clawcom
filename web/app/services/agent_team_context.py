"""Non-secret, team-scoped identity snapshots for authenticated Sidecar config."""
from sqlalchemy.orm import selectinload

from app.models import AgentTeam, OpenClawInstance
from app.services.agent_team_activity import roster
from app.services.agent_teams import TeamError, require_team_project


def team_snapshot(team, claw_id, names):
    people = roster(team)
    if claw_id not in people:
        return None
    ids = sorted(people)
    # Keep self even for a large team; full roster is available at the API.
    selected = sorted(set(ids[:49]) | {claw_id})
    return {
        'team_id': team.id, 'name': team.name, 'project_id': team.project_id,
        'version': team.version, 'status': team.status,
        'self': dict(people[claw_id], name=names.get(claw_id, '')),
        'primary_manager_claw_id': team.primary_manager_claw_id,
        'backup_manager_claw_id': team.backup_manager_claw_id,
        'members': [dict(people[cid], name=names.get(cid, '')) for cid in selected],
        'members_truncated': len(selected) < len(ids),
        'definition_api': '/api/v1/agent-teams/%s' % team.id,
        'activity_api': '/api/v1/agent-teams/%s/members/activity' % team.id,
    }


def build_team_context(claw):
    try:
        require_team_project(claw.project_id)
    except TeamError:
        return []
    teams = AgentTeam.query.filter(
        AgentTeam.project_id == claw.project_id,
        (AgentTeam.primary_manager_claw_id == claw.id)
        | (AgentTeam.backup_manager_claw_id == claw.id)
        | AgentTeam.members.any(claw_id=claw.id),
    ).options(selectinload(AgentTeam.members)).order_by(AgentTeam.id).all()
    ids = {cid for team in teams for cid in roster(team)}
    names = dict(OpenClawInstance.query.with_entities(
        OpenClawInstance.id, OpenClawInstance.name).filter(
            OpenClawInstance.id.in_(ids),
            OpenClawInstance.project_id == claw.project_id).all()) if ids else {}
    return [team_snapshot(team, claw.id, names) for team in teams]
