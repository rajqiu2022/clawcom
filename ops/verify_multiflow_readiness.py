"""Read-only deployment readiness checks for the Hub multi-Flow closed loop."""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path


REQUIRED_MIGRATIONS = (
    '20260804_workflow_operation_idempotency.sql',
    '20260810_shift_left_mvp_a.sql',
    '20260812_workflow_run_control_plane.sql',
    '20260812_automation_case_candidates.sql',
    '20260812_testcase_library_promotions.sql',
    '20260812_workflow_run_library_snapshots.sql',
    '20260812_resource_leases_and_test_account_ttl.sql',
    '20260812_entity_relations.sql',
    '20260812_workflow_evidence_manifests.sql',
    '20260812_capability_gap_lifecycle.sql',
    '20260812_finding_feedback_analysis_rules.sql',
    '20260813_automation_capabilities.sql',
    '20260813_terminal_run_gap_reconciliation.sql',
)


REQUIRED_TABLE_COLUMNS = {
    'workflow_runs': {
        'idempotency_key', 'idempotency_request_hash', 'controller_run_id',
        'correlation_id', 'trigger_source',
    },
    'workflow_operation_idempotencies': {
        'actor_type', 'actor_id', 'idempotency_key', 'request_hash',
        'response_status',
    },
    'shift_left_analysis_runs': {
        'project_id', 'workflow_run_id', 'baseline_fingerprint', 'status',
        'revision',
    },
    'shift_left_findings': {
        'project_id', 'analysis_run_id', 'finding_key', 'severity', 'status',
        'revision',
    },
    'automation_case_candidates': {
        'project_id', 'state', 'qualification_outcome', 'capability_gap_id',
        'production_library_id', 'production_case_id', 'version',
    },
    'automation_case_candidate_events': {
        'candidate_id', 'event_type', 'version_before', 'version_after',
        'request_id',
    },
    'capability_gaps': {
        'project_id', 'gap_key', 'status', 'candidate_requeue_pending',
        'development_requirement_ref', 'development_requirement_url',
        'development_requirement_status', 'version',
    },
    'capability_gap_events': {
        'capability_gap_id', 'event_type', 'version_before', 'version_after',
        'request_id',
    },
    'automation_capabilities': {
        'project_id', 'capability_key', 'operations_json', 'observables_json',
        'reset_hooks_json', 'platforms_json', 'status',
        'implementation_version', 'health_checked_at', 'version',
    },
    'test_case_libraries': {'revision', 'content_hash'},
    'testcase_library_revisions': {
        'library_id', 'revision', 'content_hash', 'snapshot_json',
        'source_type',
    },
    'testcase_library_promotions': {
        'library_id', 'operation_type', 'idempotency_key', 'before_revision',
        'after_revision', 'content_hash', 'response_json',
    },
    'workflow_run_library_snapshots': {
        'workflow_run_id', 'library_id', 'library_revision', 'content_hash',
        'case_count', 'snapshot_json',
    },
    'resource_leases': {
        'lease_group_id', 'resource_key', 'active_slot', 'owner_type',
        'owner_id', 'controller_run_id', 'status', 'expires_at',
    },
    'resource_lease_events': {
        'lease_id', 'lease_group_id', 'event_type', 'created_at',
    },
    'entity_relations': {
        'project_id', 'from_type', 'from_id', 'relation_type', 'to_type',
        'to_id', 'relation_key',
    },
    'workflow_evidence_manifests': {
        'project_id', 'workflow_run_id', 'coverage_json',
        'completeness_status', 'classification', 'revision',
    },
    'shift_left_finding_feedback': {
        'project_id', 'finding_id', 'label', 'actor_key', 'idempotency_key',
    },
    'analysis_rules': {
        'project_id', 'rule_key', 'status', 'active_version_id',
        'latest_version', 'version',
    },
    'analysis_rule_versions': {
        'rule_id', 'version_no', 'status', 'definition_json',
        'thresholds_json', 'definition_hash',
    },
    'analysis_rule_replays': {
        'rule_version_id', 'dataset_hash', 'metrics_json', 'gate_passed',
        'idempotency_key',
    },
}


def _check(name, status, message, details=None):
    return {
        'name': name,
        'status': status,
        'message': message,
        'details': details or {},
    }


def check_migration_assets(repo_root, required=None):
    required = tuple(required or REQUIRED_MIGRATIONS)
    migration_dir = Path(repo_root) / 'ops' / 'migrations'
    missing = [name for name in required if not (migration_dir / name).is_file()]
    if missing:
        return _check(
            'migration_assets', 'fail',
            f'{len(missing)} required migration files are missing',
            {'missing': missing})
    return _check(
        'migration_assets', 'pass',
        f'all {len(required)} required migration files are present')


def check_database_schema(inspector, requirements=None):
    requirements = requirements or REQUIRED_TABLE_COLUMNS
    actual_tables = set(inspector.get_table_names())
    missing_tables = sorted(set(requirements) - actual_tables)
    missing_columns = {}
    for table, required_columns in requirements.items():
        if table not in actual_tables:
            continue
        actual_columns = {
            column['name'] for column in inspector.get_columns(table)
        }
        missing = sorted(set(required_columns) - actual_columns)
        if missing:
            missing_columns[table] = missing
    if missing_tables or missing_columns:
        return _check(
            'database_schema', 'fail',
            'database schema is not ready for the multi-Flow closed loop',
            {
                'missing_tables': missing_tables,
                'missing_columns': missing_columns,
            })
    return _check(
        'database_schema', 'pass',
        f'all {len(requirements)} required tables and columns are present')


def _enabled(value):
    if isinstance(value, str):
        return value.strip().lower() in {'1', 'true', 'yes', 'on'}
    return bool(value)


def check_runtime_config(config, require_enabled=False):
    if _enabled(config.get('SHIFT_LEFT_ENABLED', False)):
        return _check(
            'runtime_config', 'pass', 'SHIFT_LEFT_ENABLED is enabled')
    if require_enabled:
        return _check(
            'runtime_config', 'fail',
            'SHIFT_LEFT_ENABLED is disabled but --require-shift-left was set')
    return _check(
        'runtime_config', 'warn',
        'SHIFT_LEFT_ENABLED is disabled; existing Flow execution remains safe, '
        'but Evidence/Finding collaboration APIs will be unavailable')


def check_seed_objects(connection, project_id, library_id, definition_id):
    from sqlalchemy import text

    details = {
        'project_id': project_id,
        'library_id': library_id,
        'workflow_definition_id': definition_id,
    }
    project = (connection.execute(text(
        'SELECT id, name FROM projects WHERE id = :id'),
        {'id': project_id}).mappings().first() if project_id else None)
    library = connection.execute(text(
        'SELECT id, name, project_name, revision, content_hash '
        'FROM test_case_libraries WHERE id = :id'),
        {'id': library_id}).mappings().first()
    definition = connection.execute(text(
        'SELECT id, project_id, status FROM workflow_definitions '
        'WHERE id = :id'), {'id': definition_id}).mappings().first()
    failures = []
    warnings = []
    if project_id and not project:
        failures.append(f'project #{project_id} was not found')
    if not library:
        failures.append(f'testcase library #{library_id} was not found')
    if not definition:
        failures.append(f'workflow definition #{definition_id} was not found')
    if definition and definition['status'] != 'active':
        warnings.append(
            f'workflow definition #{definition_id} status is '
            f"{definition['status']}")
    if (project and definition
            and definition['project_id'] not in (None, project['id'])):
        failures.append(
            f'workflow definition #{definition_id} belongs to project '
            f"#{definition['project_id']}, not #{project['id']}")
    if project and library and library['project_name'] and (
            library['project_name'].strip().casefold()
            != project['name'].strip().casefold()):
        failures.append(
            f'testcase library #{library_id} project_name does not match '
            f"project #{project['id']}")
    if library:
        current_revision = int(library['revision'] or 0)
        revision = connection.execute(text(
            'SELECT revision, content_hash FROM testcase_library_revisions '
            'WHERE library_id = :library_id AND revision = :revision'), {
                'library_id': library['id'],
                'revision': current_revision,
            }).mappings().first()
        if not revision:
            failures.append(
                f"testcase library #{library['id']} current revision "
                f'{current_revision} has no formal snapshot')
        elif revision['content_hash'] != (library['content_hash'] or ''):
            failures.append(
                f"testcase library #{library['id']} revision/hash "
                'readback mismatch')
        else:
            details['library_revision'] = current_revision
            details['library_content_hash'] = library['content_hash'] or ''
    if failures:
        return _check(
            'seed_objects', 'fail', '; '.join(failures), details)
    if warnings:
        return _check(
            'seed_objects', 'warn', '; '.join(warnings), details)
    return _check(
        'seed_objects', 'pass',
        'project, production library baseline and Flow definition are ready',
        details)


def check_expired_active_leases(connection):
    from sqlalchemy import text

    count = connection.execute(text(
        'SELECT COUNT(*) FROM resource_leases '
        'WHERE status = :status AND expires_at <= :now'), {
            'status': 'active', 'now': datetime.now(),
        }).scalar_one()
    if count:
        return _check(
            'expired_active_leases', 'warn',
            f'{count} expired active lease rows await lazy reclamation',
            {'count': count})
    return _check(
        'expired_active_leases', 'pass',
        'no expired active lease rows are waiting for reclamation')


def finalize_report(checks):
    counts = {
        status: sum(1 for item in checks if item['status'] == status)
        for status in ('pass', 'warn', 'fail')
    }
    return {
        'ready': counts['fail'] == 0,
        'summary': counts,
        'checks': checks,
    }


def _print_human(report):
    labels = {'pass': 'PASS', 'warn': 'WARN', 'fail': 'FAIL'}
    for item in report['checks']:
        print(f"[{labels[item['status']]}] {item['name']}: {item['message']}")
        if item['details'] and item['status'] != 'pass':
            print('  ' + json.dumps(item['details'], ensure_ascii=False))
    summary = report['summary']
    verdict = 'READY' if report['ready'] else 'NOT READY'
    print(
        f"{verdict}: {summary['pass']} pass, {summary['warn']} warn, "
        f"{summary['fail']} fail")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Read-only Hub multi-Flow deployment readiness check')
    parser.add_argument('--config', default=None)
    parser.add_argument('--project-id', type=int)
    parser.add_argument('--library-id', type=int, default=33)
    parser.add_argument('--workflow-definition-id', type=int, default=12)
    parser.add_argument('--require-shift-left', action='store_true')
    parser.add_argument('--json', action='store_true', dest='as_json')
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parents[1]
    web_root = repo_root / 'web'
    if str(web_root) not in sys.path:
        sys.path.insert(0, str(web_root))
    checks = [check_migration_assets(repo_root)]
    try:
        from sqlalchemy import create_engine, inspect
        from sqlalchemy.engine import make_url
        from config import config

        config_name = args.config or os.getenv('FLASK_ENV', 'default')
        config_class = config.get(config_name)
        if not config_class:
            raise ValueError(f'unknown config: {config_name}')
        database_url = make_url(config_class.SQLALCHEMY_DATABASE_URI)
        if database_url.drivername == 'sqlite' and database_url.database:
            database_path = Path(database_url.database)
            if not database_path.is_absolute():
                database_url = database_url.set(
                    database=str(web_root / 'instance' / database_path))
        engine = create_engine(database_url, pool_pre_ping=True)
        checks.append(check_database_schema(inspect(engine)))
        checks.append(check_runtime_config({
            'SHIFT_LEFT_ENABLED': config_class.SHIFT_LEFT_ENABLED,
        }, require_enabled=args.require_shift_left))
        if checks[-2]['status'] != 'fail':
            with engine.connect() as connection:
                checks.append(check_seed_objects(
                    connection, args.project_id, args.library_id,
                    args.workflow_definition_id))
                checks.append(check_expired_active_leases(connection))
        engine.dispose()
    except Exception as exc:
        checks.append(_check(
            'database_connection', 'fail',
            f'{type(exc).__name__}: {exc}'))
    report = finalize_report(checks)
    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_human(report)
    return 0 if report['ready'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
