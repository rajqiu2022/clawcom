"""Hub-host ops tool: temporary handover of a team's primary test manager.

Dry-run by default; every write needs an explicit ``--apply``. Run it on the Hub
host (or through the same remote-exec harness the other ops scripts use):

    python ops/manager_delegation.py --team-id 1 --status
    python ops/manager_delegation.py --migrate --apply
    python ops/manager_delegation.py --team-id 1 --delegate-claw-id 61 \
        --reason "小策 Provider 额度耗尽" --apply
    python ops/manager_delegation.py --team-id 1 --revoke --apply
    python ops/manager_delegation.py --sweep-expired --apply

Why this exists: ``primary_manager_claw_id`` alone cannot be swapped. Plan
supervision freezes the manager identity into ``PlanSupervisor.start_hash``, so
the handover must also move every supervisor's orchestrator, hash, fence and
Mission owner in one transaction. See
``app/services/agent_team_manager_delegation.py``.
"""
from __future__ import print_function

import argparse
import json
import os
import subprocess
import sys

REMOTE = '/opt/openclaw-web'
ENV_FILE = REMOTE + '/.env'


def load_service_env():
    """Inherit the running service's env so DB credentials match production."""
    try:
        pid = subprocess.check_output(
            ['systemctl', 'show', 'openclaw-web', '-p', 'MainPID']
        ).decode().strip().split('=', 1)[1]
        with open('/proc/' + pid + '/environ', 'rb') as stream:
            for item in stream.read().split(b'\0'):
                if b'=' in item:
                    key, value = item.split(b'=', 1)
                    os.environ[key.decode()] = value.decode()
    except Exception:
        pass
    os.environ['SKIP_AUTO_MIGRATE'] = '1'
    if REMOTE not in sys.path:
        sys.path.insert(0, REMOTE)
    try:
        from dotenv import load_dotenv
        load_dotenv(ENV_FILE)
    except Exception:
        pass


def emit(payload):
    print('MANAGER_DELEGATION ' + json.dumps(payload, ensure_ascii=False, sort_keys=True))


def parse_args(argv):
    parser = argparse.ArgumentParser(description='Team primary-manager handover')
    parser.add_argument('--team-id', type=int)
    parser.add_argument('--delegate-claw-id', type=int)
    parser.add_argument('--revoke', action='store_true')
    parser.add_argument('--sweep-expired', action='store_true')
    parser.add_argument('--migrate', action='store_true')
    parser.add_argument('--reason', default='')
    parser.add_argument('--expires-at', default=None)
    parser.add_argument('--actor', default='ops:manager-delegation')
    parser.add_argument('--status', action='store_true')
    parser.add_argument('--apply', action='store_true',
                        help='禁止隐式执行：不带此参数只做只读预演')
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    load_service_env()

    from sqlalchemy import inspect, text

    from app import create_app, db
    from app.models import AgentTeam
    from app.services import agent_team_manager_delegation as delegation
    from app.services import plan_supervision

    app = create_app('production')
    with app.app_context():
        columns = {row['name'] for row in inspect(db.engine).get_columns('agent_teams')}
        has_column = 'manager_delegation_json' in columns
        gate = {
            'PLAN_MANAGER_DELEGATION_ENABLED': bool(delegation.enabled()),
            'AGENT_TEAMS_ENABLED': str(app.config.get('AGENT_TEAMS_ENABLED')),
            'AGENT_TEAMS_PROJECT_IDS': str(app.config.get('AGENT_TEAMS_PROJECT_IDS')),
        }
        if args.migrate:
            if has_column:
                emit({'action': 'migrate', 'applied': False, 'reason': 'column_present'})
                return 0
            if not args.apply:
                emit({'action': 'migrate', 'applied': False, 'dry_run': True,
                      'ddl': 'ALTER TABLE agent_teams ADD COLUMN '
                             'manager_delegation_json LONGTEXT DEFAULT NULL'})
                return 0
            with db.engine.begin() as conn:
                conn.execute(text(
                    'ALTER TABLE agent_teams ADD COLUMN '
                    'manager_delegation_json LONGTEXT DEFAULT NULL'))
            emit({'action': 'migrate', 'applied': True})
            return 0

        if args.sweep_expired:
            due = delegation.due_for_revoke()
            report = {'action': 'sweep_expired', 'dry_run': not args.apply,
                      'due_team_ids': [team.id for team in due], 'revoked': []}
            if args.apply:
                for team in due:
                    try:
                        result = delegation.revoke(
                            team.id, args.actor, 'ops', '转正截止时间已到自动回退')
                        db.session.commit()
                        if result['wake_claw_id']:
                            plan_supervision.wake(result['wake_claw_id'])
                        report['revoked'].append({
                            'team_id': team.id,
                            'restored_primary_claw_id':
                                result['team']['primary_manager_claw_id'],
                            'new_backup_claw_id':
                                result['team']['backup_manager_claw_id']})
                    except Exception as error:
                        db.session.rollback()
                        report['revoked'].append({
                            'team_id': team.id,
                            'error': '%s: %s' % (type(error).__name__, error)})
            emit(report)
            return 0

        if not args.team_id:
            emit({'error': 'need_team_id', 'hint': '--team-id 或 --sweep-expired'})
            return 2
        team = db.session.get(AgentTeam, args.team_id)
        if not team:
            emit({'error': 'team_not_found', 'team_id': args.team_id})
            return 2

        if args.status or (not args.delegate_claw_id and not args.revoke):
            emit({
                'action': 'status',
                'team': {
                    'id': team.id, 'name': team.name, 'status': team.status,
                    'version': team.version,
                    'primary_manager_claw_id': team.primary_manager_claw_id,
                    'backup_manager_claw_id': team.backup_manager_claw_id,
                    'manager_epoch': team.manager_epoch,
                    'manager_session_id': team.manager_session_id,
                    'active_manager_claw_id': team.active_manager_claw_id,
                },
                'manager_delegation': delegation.state(team),
                'column_present': has_column,
                'gates': gate,
                'dry_run': True,
            })
            return 0

        if not has_column:
            emit({'error': 'column_missing', 'hint': '先执行 --migrate --apply'})
            return 3

        if args.revoke:
            result = delegation.revoke(
                team.id, args.actor, 'ops', args.reason)
            if not args.apply:
                db.session.rollback()
                emit({'action': 'revoke', 'dry_run': True,
                      'team_id': team.id,
                      'restored_primary_claw_id':
                          result['team']['primary_manager_claw_id'],
                      'new_backup_claw_id':
                          result['team']['backup_manager_claw_id'],
                      'supervisors': result['supervisors']})
                return 0
            db.session.commit()
            if result['wake_claw_id']:
                plan_supervision.wake(result['wake_claw_id'])
            emit({'action': 'revoke', 'applied': True, 'team_id': team.id,
                  'team': result['team'],
                  'manager_delegation': result['delegation'],
                  'supervisors': result['supervisors']})
            return 0

        result = delegation.delegate(
            team.id, args.delegate_claw_id, args.actor, 'ops', args.reason,
            args.expires_at)
        if not args.apply:
            db.session.rollback()
            emit({'action': 'delegate', 'dry_run': True, 'team_id': team.id,
                  'delegate_claw_id': args.delegate_claw_id,
                  'planned_primary_manager_claw_id':
                      result['team']['primary_manager_claw_id'],
                  'planned_backup_manager_claw_id':
                      result['team']['backup_manager_claw_id'],
                  'planned_manager_epoch': result['team']['manager_epoch'],
                  'supervisors': result['supervisors']})
            return 0
        db.session.commit()
        if result['wake_claw_id']:
            plan_supervision.wake(result['wake_claw_id'])
        emit({'action': 'delegate', 'applied': True, 'team_id': team.id,
              'team': result['team'],
              'manager_delegation': result['delegation'],
              'supervisors': result['supervisors']})
        return 0


if __name__ == '__main__':
    sys.exit(main())
