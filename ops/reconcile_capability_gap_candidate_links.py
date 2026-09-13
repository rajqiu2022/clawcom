#!/usr/bin/env python3
"""Safely repair legacy Gap evidence links that missed the candidate FK.

Dry-run is the default.  ``--apply`` updates only same-project
WAITING_CAPABILITY candidates whose required capability set is covered by the
Gap's declared missing capabilities/operations.  No capability is marked
available and no candidate is requeued by this script.
"""

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = ROOT if (ROOT / 'app').is_dir() else ROOT / 'web'
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from app import create_app, db  # noqa: E402
from app.models import (  # noqa: E402
    AutomationCaseCandidate,
    AutomationCaseCandidateEvent,
    CapabilityGap,
)


def plan_links(project_id=None):
    query = CapabilityGap.query.filter(
        CapabilityGap.status.in_(('open', 'in_progress')))
    if project_id:
        query = query.filter_by(project_id=project_id)
    planned = []
    skipped = []
    for gap in query.order_by(CapabilityGap.id.asc()).all():
        evidence = gap.evidence_json if isinstance(gap.evidence_json, dict) else {}
        raw_candidate_id = evidence.get('candidate_id')
        try:
            candidate_id = int(raw_candidate_id)
        except (TypeError, ValueError):
            continue
        candidate = db.session.get(AutomationCaseCandidate, candidate_id)
        gap_tokens = set(gap.missing_capabilities_json or []) | set(
            gap.required_operations_json or [])
        required = set(candidate.required_capabilities_json or []) if candidate else set()
        reason = ''
        if candidate is None:
            reason = 'candidate_not_found'
        elif int(candidate.project_id) != int(gap.project_id):
            reason = 'project_mismatch'
        elif candidate.state != 'WAITING_CAPABILITY':
            reason = 'candidate_not_waiting_capability'
        elif candidate.capability_gap_id not in (None, gap.id):
            reason = 'candidate_linked_to_other_gap'
        elif not required or not required.issubset(gap_tokens):
            reason = 'capability_set_mismatch'
        elif candidate.capability_gap_id == gap.id:
            reason = 'already_linked'
        item = {
            'gap_id': gap.id,
            'candidate_id': candidate_id,
            'reason': reason,
        }
        (skipped if reason else planned).append(item)
    return planned, skipped


def apply_links(planned, actor, actor_id=0):
    for item in planned:
        candidate = db.session.get(
            AutomationCaseCandidate, item['candidate_id'])
        version_before = int(candidate.version or 1)
        candidate.capability_gap_id = item['gap_id']
        candidate.version = version_before + 1
        candidate.updated_by = actor
        db.session.add(AutomationCaseCandidateEvent(
            candidate_id=candidate.id,
            event_type='capability_gap_link_reconciled',
            from_state=candidate.state,
            to_state=candidate.state,
            version_before=version_before,
            version_after=candidate.version,
            payload_json={'capability_gap_id': item['gap_id']},
            actor_type='system',
            actor_id=actor_id,
            actor_name=actor,
            request_id='capability-gap-link-reconciliation',
        ))
    db.session.commit()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--project-id', type=int)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--actor', default='hub-capability-reconciliation')
    parser.add_argument(
        '--actor-id', type=int, default=0,
        help='Numeric audit actor id; 0 is the reserved system actor')
    args = parser.parse_args()
    app = create_app()
    with app.app_context():
        planned, skipped = plan_links(args.project_id)
        if args.apply and planned:
            apply_links(planned, args.actor, args.actor_id)
        print(json.dumps({
            'mode': 'apply' if args.apply else 'dry-run',
            'planned': planned,
            'skipped': skipped,
            'updated_count': len(planned) if args.apply else 0,
        }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
