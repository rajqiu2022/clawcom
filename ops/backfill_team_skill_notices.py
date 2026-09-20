"""Explicitly scoped, idempotent backfill of durable Team Skill join messages.

Default: inspect enabled non-archived teams. --apply requires exact team ID and
expected member IDs from inspection. No installation, task creation or restart.
"""
import argparse
import json
import uuid
import paramiko
from deploy_agent_teams import ENV_SOURCE, command, run_python

SCRIPT = r'''
from app import create_app, db
from app.models import AgentTeam, AuditLog, ClawMessage, User
from app.services.agent_team_activity import roster
from app.services.agent_teams import require_team_project, scoped_claw
app = create_app('production')
with app.app_context():
    if team_id is None:
        items = []
        for team in AgentTeam.query.filter(AgentTeam.status != 'archived').all():
            try:
                require_team_project(team.project_id)
            except ValueError:
                continue
            items.append({'team_id':team.id,'name':team.name,'version':team.version,
                          'project_id':team.project_id,'member_ids':sorted(roster(team))})
        db.session.remove()
        print('TEAM_RELEASE '+json.dumps({'teams':items},ensure_ascii=False))
    else:
        team = AgentTeam.query.filter_by(id=team_id).with_for_update().first()
        assert team and team.status != 'archived'
        require_team_project(team.project_id)
        people = roster(team)
        assert sorted(people) == expected_members, 'Roster changed; inspect again'
        for cid in people:
            scoped_claw(cid, team.project_id)
        if apply_changes:
            from app.services.agent_team_onboarding import queue_missing_join_notifications
            admin = User.query.filter_by(role='super_admin').first()
            assert admin
            notified = queue_missing_join_notifications(team)
            if notified:
                db.session.add(AuditLog(action='skill_onboarding_backfill', resource_type='agent_team',
                    resource_id=team.id, resource_name=team.name, operator=admin.username,
                    detail=json.dumps({'claw_ids':notified,'team_version':team.version,
                                       'source':'explicit_operator_backfill'},ensure_ascii=False)))
            db.session.commit()
        else:
            notified = []
            db.session.rollback()
        prefix = '[Agent 团队入队通知]\n你已加入团队 #%s：' % team_id
        rows = ClawMessage.query.filter(ClawMessage.sender_name=='Hub Agent Teams',
            ClawMessage.direction=='to_claw',ClawMessage.claw_id.in_(expected_members),
            ClawMessage.content.startswith(prefix)).order_by(ClawMessage.id).all()
        result = {'team_id':team_id,'new_notice_claw_ids':notified,
                  'messages':[{'message_id':m.id,'claw_id':m.claw_id,'status':m.status,
                               'created_at':str(m.created_at)} for m in rows],
                  'skills_installed':False,'runs_started':False}
        db.session.remove()
        print('TEAM_RELEASE '+json.dumps(result,ensure_ascii=False))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--key', required=True)
    parser.add_argument('--team-id', type=int)
    parser.add_argument('--expected-members', help='comma-separated Claw IDs from inspection')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if args.apply and not args.team_id:
        parser.error('--apply requires --team-id')
    if args.team_id and not args.expected_members:
        parser.error('--team-id requires --expected-members')
    ids = sorted({int(x) for x in args.expected_members.split(',')}) if args.expected_members else []
    source = ENV_SOURCE + '\nteam_id=%r\nexpected_members=%r\napply_changes=%r\n' % (args.team_id, ids, args.apply) + SCRIPT
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    client.connect('9.134.11.169', port=36000, username='root', key_filename=args.key, timeout=20)
    try:
        stage = '/tmp/team-notices-' + uuid.uuid4().hex
        command(client, 'mkdir -m 700 ' + stage)
        with client.open_sftp() as sftp:
            run_python(client, sftp, stage, 'backfill', source)
    finally:
        client.close()


if __name__ == '__main__':
    main()
