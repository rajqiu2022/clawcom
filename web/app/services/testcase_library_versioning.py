"""Canonical testcase-library revisions and candidate-to-case mapping."""

from datetime import datetime
import hashlib
import json

from app import db
from app.models import (
    TestCase,
    TestCaseLibraryRevision,
)


SNAPSHOT_SCHEMA = 'testcase-library-revision-v1'
PRIORITIES = {'P0', 'P1', 'P2', 'P3'}
CASE_TYPES = {'functional', 'interface', 'performance', 'security'}


def _iso(value):
    # Production columns are MariaDB DATETIME (second precision).  Python ORM
    # objects still retain microseconds until they are expired after commit;
    # hashing those transient digits makes the just-published snapshot differ
    # from the same row reloaded from MySQL and creates a false legacy_sync.
    return value.replace(microsecond=0).isoformat() if value else None


def parse_snapshot_datetime(value):
    if not value:
        return None
    return datetime.fromisoformat(str(value))


def canonical_case(case):
    """Return every persisted testcase content field in a JSON-safe form."""
    return {
        'id': case.id,
        'case_id': case.case_id,
        'title': case.title,
        'priority': case.priority,
        'type': case.type,
        'content': case.content,
        'mindmap_node_id': case.mindmap_node_id,
        'ai_generated': bool(case.ai_generated),
        'ai_prompt': case.ai_prompt,
        'tags': case.tags or [],
        'module_path': case.module_path or '',
        'is_placeholder': bool(case.is_placeholder),
        'created_by': case.created_by or '',
        'updated_by': case.updated_by or '',
        'tapd_story_url': case.tapd_story_url,
        'tapd_story_title': case.tapd_story_title,
        'created_at': _iso(case.created_at),
        'updated_at': _iso(case.updated_at),
    }


def canonical_library_snapshot(library, cases=None):
    """Create deterministic full content; library administrative fields stay live."""
    rows = cases
    if rows is None:
        rows = TestCase.query.filter_by(library_id=library.id).order_by(
            TestCase.id.asc()).all()
    return {
        'schema': SNAPSHOT_SCHEMA,
        'library_id': library.id,
        'mindmap': library.mindmap,
        'cases': [canonical_case(case) for case in rows],
    }


def canonical_hash(value):
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), default=str,
    ).encode('utf-8')
    return 'sha256:' + hashlib.sha256(raw).hexdigest()


def request_hash(value):
    return canonical_hash(value)


def ensure_library_revision(library, actor_name='system'):
    """Ensure live content is represented by an immutable revision.

    Existing CRUD is intentionally not intercepted. If it changed content since
    the last formal revision, record that live state as ``legacy_sync`` and bump
    the optimistic-lock revision before accepting another formal mutation.
    """
    db.session.flush()
    snapshot = canonical_library_snapshot(library)
    live_hash = canonical_hash(snapshot)
    current_revision = int(library.revision or 0)
    # This must be a current/locking read. Under MySQL REPEATABLE READ a normal
    # snapshot read can miss the revision committed by a writer that released
    # the library-row lock moments ago, then attempt the same unique revision
    # and surface a 500 duplicate-key error.
    row = (TestCaseLibraryRevision.query.filter_by(
        library_id=library.id, revision=current_revision)
        .with_for_update().first())
    if row is None:
        row = TestCaseLibraryRevision(
            library_id=library.id,
            revision=current_revision,
            content_hash=live_hash,
            case_count=len(snapshot['cases']),
            snapshot_json=snapshot,
            source_type='baseline',
            message='Initial formal revision from current library content',
            created_by=actor_name,
        )
        library.content_hash = live_hash
        db.session.add(row)
        db.session.flush()
        return row, False
    if row.content_hash != live_hash:
        current_revision += 1
        row = TestCaseLibraryRevision(
            library_id=library.id,
            revision=current_revision,
            content_hash=live_hash,
            case_count=len(snapshot['cases']),
            snapshot_json=snapshot,
            source_type='legacy_sync',
            message='Captured testcase changes made through legacy CRUD',
            created_by=actor_name,
        )
        library.revision = current_revision
        library.content_hash = live_hash
        db.session.add(row)
        db.session.flush()
        return row, True
    library.content_hash = live_hash
    return row, False


def candidate_case_values(candidate, actor_name):
    """Map a qualified candidate draft onto one production testcase."""
    draft = dict(candidate.case_draft_json or {})
    priority = str(draft.get('priority') or 'P2').upper()
    if priority not in PRIORITIES:
        raise ValueError(
            f'candidate {candidate.id}: priority must be one of '
            f'{", ".join(sorted(PRIORITIES))}')
    case_type = str(draft.get('type') or 'functional').lower()
    if case_type not in CASE_TYPES:
        raise ValueError(
            f'candidate {candidate.id}: type must be one of '
            f'{", ".join(sorted(CASE_TYPES))}')
    tags = draft.get('tags') or []
    if not isinstance(tags, list):
        raise ValueError(f'candidate {candidate.id}: tags must be an array')
    marker = f'automation_candidate:{candidate.id}'
    tags = [str(tag)[:100] for tag in tags if str(tag).strip()]
    if marker not in tags:
        tags.append(marker)
    content = draft.get('content')
    if content is None:
        content = {
            'preconditions': draft.get('preconditions') or [],
            'steps': draft.get('steps') or [],
            'expected_results': draft.get('expected_results') or [],
            'automation': draft.get('automation') or {},
        }
    if not isinstance(content, dict):
        raise ValueError(f'candidate {candidate.id}: content must be an object')
    module_path = str(
        draft.get('module_path') or
        str(candidate.module_key or '').replace('.', '/')
    )[:500]
    return {
        'case_id': str(
            draft.get('case_id') or f'AUTO-CAND-{candidate.id:06d}')[:100],
        'title': str(draft.get('title') or candidate.title)[:255],
        'priority': priority,
        'type': case_type,
        'content': content,
        'mindmap_node_id': str(
            draft.get('mindmap_node_id') or f'candidate_{candidate.id}')[:50],
        'ai_generated': True,
        'ai_prompt': json.dumps({
            'source_type': candidate.source_type,
            'source_refs': candidate.source_refs_json or [],
            'candidate_id': candidate.id,
        }, ensure_ascii=False, sort_keys=True),
        'tags': tags,
        'module_path': module_path,
        'is_placeholder': False,
        'updated_by': actor_name,
        'tapd_story_url': str(draft.get('tapd_story_url') or '')[:500] or None,
        'tapd_story_title': str(
            draft.get('tapd_story_title') or '')[:255] or None,
    }


def snapshot_case_values(snapshot_case):
    """Validate and map an immutable snapshot row back to the ORM model."""
    priority = str(snapshot_case.get('priority') or 'P2')
    case_type = str(snapshot_case.get('type') or 'functional')
    if priority not in PRIORITIES or case_type not in CASE_TYPES:
        raise ValueError('Historical revision contains an unsupported enum value')
    return {
        'case_id': snapshot_case.get('case_id'),
        'title': str(snapshot_case.get('title') or '')[:255],
        'priority': priority,
        'type': case_type,
        'content': snapshot_case.get('content'),
        'mindmap_node_id': snapshot_case.get('mindmap_node_id'),
        'ai_generated': bool(snapshot_case.get('ai_generated')),
        'ai_prompt': snapshot_case.get('ai_prompt'),
        'tags': snapshot_case.get('tags') or [],
        'module_path': snapshot_case.get('module_path') or '',
        'is_placeholder': bool(snapshot_case.get('is_placeholder')),
        'created_by': snapshot_case.get('created_by') or '',
        'updated_by': snapshot_case.get('updated_by') or '',
        'tapd_story_url': snapshot_case.get('tapd_story_url'),
        'tapd_story_title': snapshot_case.get('tapd_story_title'),
        'created_at': parse_snapshot_datetime(snapshot_case.get('created_at')),
        'updated_at': parse_snapshot_datetime(snapshot_case.get('updated_at')),
    }
