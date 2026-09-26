"""Reconcile one active team's durable Plan Supervisor and Mission."""

from __future__ import print_function

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT if (ROOT / 'app').is_dir() else ROOT / 'web'
if str(WEB) not in sys.path:
    sys.path.insert(0, str(WEB))

from app import create_app, db  # noqa: E402
from app.models import WorkflowMission  # noqa: E402
from app.services import plan_supervision as supervision  # noqa: E402


def snapshot(plan_id):
    sup = supervision.locked(plan_id)
    if not sup:
        raise RuntimeError('Plan supervisor does not exist')
    mission = (db.session.get(WorkflowMission, sup.mission_id)
               if sup.mission_id else None)
    return sup, mission, {
        'plan_id': sup.plan_id,
        'team_id': sup.team_id,
        'orchestrator_claw_id': sup.orchestrator_claw_id,
        'supervisor_status': sup.status,
        'next_check_at': str(sup.next_check_at) if sup.next_check_at else None,
        'mission_id': mission.id if mission else None,
        'mission_status': mission.status if mission else None,
        'mission_effective_status': (
            mission.effective_status() if mission else None),
        'mission_main_claw_id': mission.main_claw_id if mission else None,
        'mission_expires_at': str(mission.expires_at) if mission else None,
        'allowed_worker_claw_ids': (
            mission.allowed_worker_claw_ids_json if mission else []),
    }


def apply(plan_id):
    sup, _mission, before = snapshot(plan_id)
    closeout = supervision.close_expired_mission_supervision(sup)
    if not closeout:
        supervision.ensure_manager_tenure(sup)
        supervision.ensure_team_mission(sup)
        supervision.enqueue_schedule_ticks(sup)
    db.session.commit()
    _sup, _mission, after = snapshot(plan_id)
    return {
        'changed': before != after,
        'before': before,
        'after': after,
        'closeout_receipt': closeout,
    }


def main():
    parser = argparse.ArgumentParser()
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument('--plan-id', type=int)
    target.add_argument('--all-expired', action='store_true')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    app = create_app()
    with app.app_context():
        if args.all_expired:
            if args.apply:
                closeouts = supervision.close_all_expired_missions()
                db.session.commit()
                result = {
                    'changed': bool(closeouts),
                    'closeout_count': len(closeouts),
                    'closeouts': closeouts,
                }
            else:
                rows = WorkflowMission.query.filter(
                    WorkflowMission.status.in_(('active', 'expired')),
                    WorkflowMission.expires_at <= supervision._now(),
                ).order_by(WorkflowMission.id).all()
                result = {
                    'changed': False,
                    'dry_run': True,
                    'expired_missions': [{
                        'mission_id': row.id,
                        'status': row.status,
                        'effective_status': row.effective_status(),
                        'expires_at': str(row.expires_at),
                        'has_v2_closeout': bool(
                            isinstance(row.context_json, dict)
                            and (row.context_json.get('lifecycle_closeout') or {}).get(
                                'contract') == supervision.MISSION_CLOSEOUT_CONTRACT),
                    } for row in rows],
                }
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return
        if args.apply:
            result = apply(args.plan_id)
        else:
            _sup, _mission, current = snapshot(args.plan_id)
            result = {'changed': False, 'dry_run': True, 'current': current}
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == '__main__':
    main()
