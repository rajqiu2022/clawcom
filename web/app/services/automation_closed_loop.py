"""Read-only project aggregate for the automation closed-loop dashboard (P0-J)."""

from collections import Counter
from datetime import datetime, timedelta, timezone

from app import db
from app.models import (
    AnalysisRule,
    AnalysisRuleReplay,
    AnalysisRuleVersion,
    AutomationCaseCandidate,
    CapabilityGap,
    ResourceLease,
    ResourceLeaseEvent,
    ShiftLeftAnalysisRun,
    ShiftLeftFinding,
    ShiftLeftFindingFeedback,
    TestCaseLibrary,
    TestCaseLibraryPromotion,
    WorkflowEvidenceManifest,
    WorkflowRun,
    WorkflowRunLibrarySnapshot,
)
from app.services.automation_candidates import CANDIDATE_STATES


_CST = timezone(timedelta(hours=8))
_TERMINAL_CANDIDATE_STATES = {'ACTIVE', 'RETIRED'}


def now_cst_naive():
    return datetime.now(_CST).replace(tzinfo=None)


def _iso(value):
    return value.isoformat(sep=' ', timespec='seconds') if value else None


def _hours_since(value, now):
    if not value:
        return None
    return round(max(0.0, (now - value).total_seconds() / 3600), 1)


def _count_by(rows, field):
    counts = Counter(str(getattr(row, field) or 'unclassified') for row in rows)
    return dict(sorted(counts.items()))


def _candidate_item(row, now):
    return {
        'id': row.id,
        'title': row.title,
        'module_key': row.module_key or '',
        'state': row.state,
        'age_hours': _hours_since(row.updated_at or row.created_at, now),
        'capability_gap_id': row.capability_gap_id,
        'production_library_id': row.production_library_id,
        'production_case_id': row.production_case_id,
        'updated_at': _iso(row.updated_at),
        'trace_api': (
            '/api/v1/entity-relations/trace?project_id={project_id}'
            '&entity_type=automation_case_candidate&entity_id={candidate_id}'
            '&direction=both&max_depth=5'
        ).format(project_id=row.project_id, candidate_id=row.id),
    }


def _candidate_section(project_id, now, stale_hours, limit):
    rows = (AutomationCaseCandidate.query
            .filter_by(project_id=project_id)
            .order_by(AutomationCaseCandidate.updated_at.desc(),
                      AutomationCaseCandidate.id.desc())
            .all())
    state_counts = Counter(row.state for row in rows)
    state_aging = []
    for state in sorted(CANDIDATE_STATES):
        state_rows = [row for row in rows if row.state == state]
        if not state_rows:
            continue
        ages = [
            _hours_since(row.updated_at or row.created_at, now) or 0
            for row in state_rows
        ]
        state_aging.append({
            'state': state,
            'count': len(state_rows),
            'oldest_age_hours': max(ages),
            'average_age_hours': round(sum(ages) / len(ages), 1),
        })
    cutoff = now - timedelta(hours=stale_hours)
    stale = [
        row for row in rows
        if row.state not in _TERMINAL_CANDIDATE_STATES
        and (row.updated_at or row.created_at)
        and (row.updated_at or row.created_at) <= cutoff
    ]

    def queue(*states):
        return [
            _candidate_item(row, now) for row in rows if row.state in states
        ][:limit]

    publish_exceptions = queue('QUARANTINED')

    return {
        'total': len(rows),
        'state_counts': {
            state: state_counts.get(state, 0) for state in sorted(CANDIDATE_STATES)
        },
        'state_aging': state_aging,
        'stale_hours': stale_hours,
        'stale_count': len(stale),
        'queues': {
            'waiting_capability': queue('WAITING_CAPABILITY'),
            'pending_qualification': queue('READY_FOR_CANARY'),
            'pending_publish': queue('QUALIFIED', 'PENDING_PUBLISH'),
            # Promotion does not persist failed attempts. QUARANTINED is the
            # durable operator queue for publication/production exceptions.
            'publish_failures': publish_exceptions,
            'publish_exceptions': publish_exceptions,
        },
        'queue_semantics': {
            'publish_failures': (
                'QUARANTINED candidates are used as the durable P0 '
                'publication-exception queue; legacy failed attempts are not '
                'persisted'),
        },
        'recent_items': [_candidate_item(row, now) for row in rows[:limit]],
    }


def _capability_gap_section(project_id, limit):
    rows = (CapabilityGap.query.filter_by(project_id=project_id)
            .order_by(CapabilityGap.updated_at.desc(), CapabilityGap.id.desc())
            .all())
    waiting_counts = Counter(
        candidate.capability_gap_id
        for candidate in AutomationCaseCandidate.query.filter_by(
            project_id=project_id, state='WAITING_CAPABILITY').all()
        if candidate.capability_gap_id
    )
    items = []
    for row in rows[:limit]:
        item = row.to_dict()
        item['waiting_candidate_count'] = waiting_counts.get(row.id, 0)
        items.append(item)
    return {
        'total': len(rows),
        'status_counts': _count_by(rows, 'status'),
        'requeue_pending_count': sum(
            1 for row in rows if row.candidate_requeue_pending),
        'waiting_candidate_count': sum(waiting_counts.values()),
        'items': items,
    }


def _resource_lease_project(row, project_id, project_run_ids,
                            project_controller_ids):
    metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
    try:
        if int(metadata.get('project_id')) == int(project_id):
            return True
    except (TypeError, ValueError):
        pass
    if row.controller_run_id and row.controller_run_id in project_controller_ids:
        return True
    if row.owner_type in {'workflow_run', 'run'}:
        try:
            return int(row.owner_id) in project_run_ids
        except (TypeError, ValueError):
            return False
    return False


def _resource_lease_section(project_id, now, limit):
    project_runs = WorkflowRun.query.filter_by(project_id=project_id).all()
    run_ids = {row.id for row in project_runs}
    controller_ids = {
        row.controller_run_id for row in project_runs if row.controller_run_id
    }
    all_active = (ResourceLease.query.filter_by(status='active')
                  .order_by(ResourceLease.expires_at.asc()).all())
    scoped = [
        row for row in all_active
        if _resource_lease_project(row, project_id, run_ids, controller_ids)
    ]
    current = [row for row in scoped if row.expires_at and row.expires_at > now]
    expired_pending = [
        row for row in scoped if not row.expires_at or row.expires_at <= now
    ]
    scoped_ids = [row.id for row in scoped]
    conflicts = []
    if scoped_ids:
        conflicts = (ResourceLeaseEvent.query
                     .filter(ResourceLeaseEvent.lease_id.in_(scoped_ids),
                             ResourceLeaseEvent.event_type == 'conflict')
                     .order_by(ResourceLeaseEvent.created_at.desc())
                     .limit(limit).all())
    return {
        'active_count': len(current),
        'active_group_count': len({row.lease_group_id for row in current}),
        'expired_pending_cleanup_count': len(expired_pending),
        'conflict_count': len(conflicts),
        'conflict_tracking': (
            'conflict events are shown when producers persist event_type=conflict; '
            'legacy acquire conflicts are response-only'),
        'items': [row.to_dict() for row in current[:limit]],
        'conflicts': [row.to_dict() for row in conflicts],
    }


def _library_section(project, library_id, limit):
    library = db.session.get(TestCaseLibrary, library_id) if library_id else None
    if (library and library.project_name
            and library.project_name.strip().casefold()
            != project.name.strip().casefold()):
        library = None
    if not library:
        return {
            'library_id': library_id,
            'found': False,
            'current': None,
            'recent_operations': [],
            'promotion_count': 0,
            'rollback_count': 0,
        }
    operations = (TestCaseLibraryPromotion.query.filter_by(library_id=library.id)
                  .order_by(TestCaseLibraryPromotion.created_at.desc(),
                            TestCaseLibraryPromotion.id.desc())
                  .limit(limit).all())
    return {
        'library_id': library.id,
        'found': True,
        'current': library.to_dict(with_cases=False, with_mindmap=False),
        'recent_operations': [row.to_dict() for row in operations],
        'promotion_count': sum(
            1 for row in operations if row.operation_type == 'promotion'),
        'rollback_count': sum(
            1 for row in operations if row.operation_type == 'rollback'),
    }


def _workflow_section(project_id, definition_id, limit):
    query = WorkflowRun.query.filter_by(project_id=project_id)
    if definition_id:
        query = query.filter_by(definition_id=definition_id)
    runs = (query.order_by(WorkflowRun.created_at.desc(), WorkflowRun.id.desc())
            .limit(limit).all())
    snapshots = {}
    if runs:
        snapshots = {
            row.workflow_run_id: row
            for row in WorkflowRunLibrarySnapshot.query.filter(
                WorkflowRunLibrarySnapshot.workflow_run_id.in_(
                    [run.id for run in runs])).all()
        }
    items = []
    for run in runs:
        snapshot = snapshots.get(run.id)
        items.append({
            'id': run.id,
            'definition_id': run.definition_id,
            'workflow_key': run.definition.workflow_key if run.definition else '',
            'run_name': run.run_name,
            'status': run.status,
            'summary': run.summary or '',
            'controller_run_id': run.controller_run_id,
            'correlation_id': run.correlation_id,
            'created_at': _iso(run.created_at),
            'finished_at': _iso(run.finished_at),
            'library_snapshot': snapshot.to_dict() if snapshot else None,
            'run_url': f'/workflows?run_id={run.id}',
        })
    return {
        'definition_id': definition_id,
        'total_recent': len(items),
        'status_counts': _count_by(runs, 'status'),
        'snapshot_missing_count': sum(
            1 for run in runs if run.id not in snapshots),
        'items': items,
    }


def _evidence_section(project_id, limit):
    rows = (WorkflowEvidenceManifest.query.filter_by(project_id=project_id)
            .order_by(WorkflowEvidenceManifest.updated_at.desc(),
                      WorkflowEvidenceManifest.id.desc()).all())
    complete = sum(1 for row in rows if row.completeness_status == 'complete')
    items = []
    for row in rows[:limit]:
        item = row.to_dict()
        item['run_url'] = f'/workflows?run_id={row.workflow_run_id}'
        items.append(item)
    return {
        'total': len(rows),
        'complete_count': complete,
        'completeness_rate': round(complete / len(rows), 4) if rows else None,
        'completeness_counts': _count_by(rows, 'completeness_status'),
        'classification_counts': _count_by(rows, 'classification'),
        'items': items,
    }


def _finding_section(project_id, limit):
    rows = (ShiftLeftFinding.query.filter_by(
        project_id=project_id, is_archived=False)
        .order_by(ShiftLeftFinding.updated_at.desc(), ShiftLeftFinding.id.desc())
        .all())
    feedback = ShiftLeftFindingFeedback.query.filter_by(project_id=project_id).all()
    analysis_ids = {row.analysis_run_id for row in rows if row.analysis_run_id}
    report_by_analysis = {
        row.id: row.report_id
        for row in (ShiftLeftAnalysisRun.query
                    .filter(ShiftLeftAnalysisRun.id.in_(analysis_ids)).all()
                    if analysis_ids else [])
    }
    return {
        'total': len(rows),
        'status_counts': _count_by(rows, 'status'),
        'severity_counts': _count_by(rows, 'severity'),
        'feedback_counts': _count_by(feedback, 'label'),
        'items': [{
            **row.to_dict(),
            'detail_url': (
                f'/test-reports/{report_by_analysis[row.analysis_run_id]}'
                if report_by_analysis.get(row.analysis_run_id)
                else '/test-reports'),
        } for row in rows[:limit]],
    }


def _rule_section(project_id, limit):
    rules = (AnalysisRule.query.filter_by(project_id=project_id)
             .order_by(AnalysisRule.updated_at.desc(), AnalysisRule.id.desc())
             .all())
    rule_ids = [row.id for row in rules]
    versions = (AnalysisRuleVersion.query
                .filter(AnalysisRuleVersion.rule_id.in_(rule_ids)).all()
                if rule_ids else [])
    version_ids = [row.id for row in versions]
    replays = (AnalysisRuleReplay.query
               .filter(AnalysisRuleReplay.rule_version_id.in_(version_ids))
               .order_by(AnalysisRuleReplay.created_at.desc(),
                         AnalysisRuleReplay.id.desc()).all()
               if version_ids else [])
    version_rule = {row.id: row.rule_id for row in versions}
    return {
        'total': len(rules),
        'status_counts': _count_by(rules, 'status'),
        'version_status_counts': _count_by(versions, 'status'),
        'replay_counts': {
            'total': len(replays),
            'passed': sum(1 for row in replays if row.gate_passed),
            'failed': sum(1 for row in replays if not row.gate_passed),
        },
        'recent_replays': [{
            **row.to_dict(),
            'rule_id': version_rule.get(row.rule_version_id),
        } for row in replays[:limit]],
        'items': [row.to_dict() for row in rules[:limit]],
    }


def build_automation_closed_loop_overview(
        project, library_id=33, workflow_definition_id=12,
        stale_hours=24, limit=12, now=None):
    """Return the P0 dashboard payload without mutating any source object."""
    now = now or now_cst_naive()
    return {
        'generated_at': _iso(now),
        'project': project.to_dict(),
        'filters': {
            'library_id': library_id,
            'workflow_definition_id': workflow_definition_id,
            'stale_hours': stale_hours,
            'limit': limit,
        },
        'candidates': _candidate_section(
            project.id, now, stale_hours, limit),
        'capability_gaps': _capability_gap_section(project.id, limit),
        'resource_leases': _resource_lease_section(project.id, now, limit),
        'testcase_library': _library_section(project, library_id, limit),
        'workflow_runs': _workflow_section(
            project.id, workflow_definition_id, limit),
        'evidence': _evidence_section(project.id, limit),
        'findings': _finding_section(project.id, limit),
        'analysis_rules': _rule_section(project.id, limit),
    }
