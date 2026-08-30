"""Best-effort immutable testcase-library snapshots for Workflow Runs."""

import copy

from app import db
from app.models import (
    TestCaseLibrary,
    WorkflowRunLibrarySnapshot,
)
from app.services.testcase_library_versioning import ensure_library_revision
from app.services.entity_relations import best_effort_upsert_relations


LIBRARY_ID_ALIASES = (
    'test_case_library_id',
    'testcase_library_id',
    'library_id',
)

# Compatibility binding for the existing RacingGO Flow #12.  The binding is
# keyed by the stable workflow key (not the deployment-specific definition
# id), so cloning/recreating definitions does not silently change behaviour.
# Explicit Run input always wins.
DEFAULT_WORKFLOW_LIBRARY_BINDINGS = {
    'racinggo_editor_smoke_report_only': 33,
}


def resolve_workflow_library_id(data, context, workflow_key=None,
                                default_bindings=None):
    """Resolve compatible Run inputs and report ambiguity without blocking."""
    data = data if isinstance(data, dict) else {}
    context = context if isinstance(context, dict) else {}
    start_vars = (
        context.get('start_vars')
        if isinstance(context.get('start_vars'), dict)
        else {}
    )
    candidates = []
    for source_name, source in (
        ('request', data), ('start_vars', start_vars), ('context', context),
    ):
        for alias in LIBRARY_ID_ALIASES:
            raw = source.get(alias)
            if raw in (None, ''):
                continue
            try:
                value = int(raw)
            except (TypeError, ValueError):
                return None, {
                    'code': 'INVALID_TESTCASE_LIBRARY_ID',
                    'message': f'{source_name}.{alias} must be an integer',
                    'details': {'field': f'{source_name}.{alias}', 'value': raw},
                }
            if value <= 0:
                return None, {
                    'code': 'INVALID_TESTCASE_LIBRARY_ID',
                    'message': f'{source_name}.{alias} must be positive',
                    'details': {'field': f'{source_name}.{alias}', 'value': raw},
                }
            candidates.append((source_name, alias, value))
    values = sorted({item[2] for item in candidates})
    if not values:
        bindings = (
            default_bindings
            if isinstance(default_bindings, dict)
            else DEFAULT_WORKFLOW_LIBRARY_BINDINGS
        )
        raw_default = bindings.get(str(workflow_key or '').strip())
        if raw_default in (None, ''):
            return None, None
        try:
            default_library_id = int(raw_default)
        except (TypeError, ValueError):
            return None, {
                'code': 'INVALID_DEFAULT_TESTCASE_LIBRARY_ID',
                'message': 'Workflow default testcase library ID must be an integer',
                'details': {
                    'workflow_key': str(workflow_key or ''),
                    'value': raw_default,
                },
            }
        if default_library_id <= 0:
            return None, {
                'code': 'INVALID_DEFAULT_TESTCASE_LIBRARY_ID',
                'message': 'Workflow default testcase library ID must be positive',
                'details': {
                    'workflow_key': str(workflow_key or ''),
                    'value': raw_default,
                },
            }
        return default_library_id, None
    if len(values) > 1:
        return None, {
            'code': 'TESTCASE_LIBRARY_ID_CONFLICT',
            'message': 'Workflow Run contains conflicting testcase library IDs',
            'details': {
                'values': [
                    {'source': source, 'field': alias, 'value': value}
                    for source, alias, value in candidates
                ],
            },
        }
    return values[0], None


def freeze_workflow_run_library_snapshot(run, library_id, actor_name='system'):
    """Freeze the current formal revision for ``run`` in its own full copy.

    The caller owns the transaction/savepoint and converts any raised database
    exception into a non-blocking Run warning.
    """
    existing = WorkflowRunLibrarySnapshot.query.filter_by(
        workflow_run_id=run.id).first()
    if existing:
        return existing, None
    library = (TestCaseLibrary.query.filter_by(id=int(library_id))
               .with_for_update().first())
    if library is None:
        return None, {
            'code': 'TESTCASE_LIBRARY_SNAPSHOT_NOT_FOUND',
            'message': 'Configured testcase library was not found; Run continues',
            'details': {'library_id': int(library_id)},
        }
    if (run.project is not None and library.project_name and
            library.project_name != run.project.name):
        return None, {
            'code': 'TESTCASE_LIBRARY_PROJECT_MISMATCH',
            'message': 'Testcase library belongs to another project; Run continues',
            'details': {
                'library_id': library.id,
                'library_project': library.project_name,
                'run_project': run.project.name,
            },
        }
    revision, _ = ensure_library_revision(library, actor_name)
    snapshot = copy.deepcopy(revision.snapshot_json or {})
    executable_case_ids = [
        int(case['id']) for case in (snapshot.get('cases') or [])
        if case.get('id') is not None and not case.get('is_placeholder')
    ]
    row = WorkflowRunLibrarySnapshot(
        workflow_run_id=run.id,
        library_id=library.id,
        library_revision=revision.revision,
        snapshot_version=revision.revision,
        content_hash=revision.content_hash,
        case_ids_json=executable_case_ids,
        case_count=len(executable_case_ids),
        snapshot_json=snapshot,
        frozen_by=actor_name,
    )
    db.session.add(row)
    db.session.flush()
    if run.project_id:
        relations = [{
            'from_type': 'testcase',
            'from_id': str(case_id),
            'relation_type': 'consumed_by',
            'to_type': 'workflow_run',
            'to_id': str(run.id),
            'metadata': {
                'library_id': library.id,
                'library_revision': revision.revision,
                'content_hash': revision.content_hash,
                'workflow_run_library_snapshot_id': row.id,
            },
        } for case_id in executable_case_ids]
        relations.append({
            'from_type': 'testcase_library_revision',
            'from_id': f'{library.id}:{revision.revision}',
            'relation_type': 'consumed_by',
            'to_type': 'workflow_run',
            'to_id': str(run.id),
            'metadata': {
                'content_hash': revision.content_hash,
                'case_count': len(executable_case_ids),
            },
        })
        best_effort_upsert_relations(
            run.project_id, relations, actor_name)
    return row, None


def append_workflow_snapshot_warning(context, warning):
    """Return a copied Run context with one de-duplicated structured warning."""
    result = copy.deepcopy(context) if isinstance(context, dict) else {}
    existing_warnings = result.get('warnings')
    warnings = list(existing_warnings) if isinstance(existing_warnings, list) else []
    warning = dict(warning or {})
    warning['scope'] = 'testcase_library_snapshot'
    signature = (
        warning.get('code'),
        str((warning.get('details') or {}).get('library_id') or ''),
    )
    if not any((item.get('code'), str(
            (item.get('details') or {}).get('library_id') or '')) == signature
               for item in warnings if isinstance(item, dict)):
        warnings.append(warning)
    result['warnings'] = warnings
    return result
