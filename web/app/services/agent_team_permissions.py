"""Live, revocable Flow grants derived from administrator-managed teams.

These grants never mutate a Definition ACL or a persisted Sidecar ceiling.
Manual grants therefore survive team changes, while team-only grants do not.
"""
from sqlalchemy.orm import selectinload
from flask import has_request_context, request

from app import db
from app.models import AgentTeam, OpenClawInstance, WorkflowDefinition
from app.services.agent_teams import TeamError, require_team_project


def active_teams(claw):
    if not claw or claw.status == 'deleted' or not claw.project_id:
        return []
    try:
        require_team_project(claw.project_id)
    except TeamError:
        return []
    # A definition listing calls this for many Flows. Cache only within this
    # request (never between refreshes, where revocation must take effect).
    cache = request.environ.setdefault('hub.team_flow_grants', {}) if has_request_context() else {}
    key = (claw.id, claw.project_id)
    if key in cache:
        return cache[key]
    teams = AgentTeam.query.filter(
        AgentTeam.project_id == claw.project_id, AgentTeam.status == 'active',
        (AgentTeam.primary_manager_claw_id == claw.id)
        | (AgentTeam.backup_manager_claw_id == claw.id)
        | AgentTeam.members.any(claw_id=claw.id),
    ).options(selectinload(AgentTeam.members)).all()
    cache[key] = teams
    return teams


def _includes(team, definition_id):
    return definition_id in (team.policy_json or {}).get('allowed_definition_ids', [])


def flow_grants(claw, definitions=None):
    """Managers may start/dispatch; analysts and executors may execute."""
    teams = active_teams(claw)
    if not teams:
        return []
    if definitions is None:
        definitions = WorkflowDefinition.query.filter_by(
            project_id=claw.project_id, status='active').all()
    return sorted({definition.id for definition in definitions
                   if definition.project_id == claw.project_id
                   and definition.status == 'active'
                   and any(_includes(team, definition.id) for team in teams)})


def can_execute(claw_id, definition):
    claw = db.session.get(OpenClawInstance, int(claw_id))
    return definition.id in flow_grants(claw, [definition])


def can_dispatch_to(claw_id, worker_claw_id, definition):
    """Delegation is limited to working members of the SAME granting team."""
    claw = db.session.get(OpenClawInstance, int(claw_id))
    worker = db.session.get(OpenClawInstance, int(worker_claw_id))
    if (not claw or not worker or worker.status == 'deleted'
            or worker.project_id != claw.project_id
            or definition.project_id != claw.project_id
            or definition.status != 'active'):
        return False
    return any(
        claw.id in (team.primary_manager_claw_id, team.backup_manager_claw_id)
        and _includes(team, definition.id)
        and any(member.claw_id == worker.id
                and member.role_key in ('code_analyst', 'test_executor')
                for member in team.members)
        for team in active_teams(claw))
