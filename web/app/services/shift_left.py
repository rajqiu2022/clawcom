"""Shift-left domain helpers.

This module deliberately contains no workflow dispatch side effects.  The first
increment only defines canonical values, fingerprints and transition rules so
the new API can coexist with current Hub flows without changing them.
"""

import hashlib
import json
import re
import secrets
from datetime import datetime, timedelta, timezone


FINDING_SEVERITIES = ('critical', 'high', 'medium', 'low', 'info')
EVIDENCE_LEVELS = (
    'hypothesis',
    'contract_confirmed',
    'runtime_confirmed',
    'human_confirmed',
    'fixed_verified',
)
EVIDENCE_RANK = {value: index for index, value in enumerate(EVIDENCE_LEVELS)}

FINDING_STATES = (
    'new', 'triaged', 'validation_planned', 'validating', 'needs_human',
    'confirmed', 'dev_acknowledged', 'fixing', 'fix_ready', 'reverifying',
    'closed', 'rejected', 'blocked', 'reopened',
)

FINDING_FEEDBACK_LABELS = (
    'true_positive',
    'false_positive',
    'duplicate_bug',
    'automation_unsupported',
    'human_confirmed',
    'human_rejected',
    'fixed_before_validation',
)

FINDING_TRANSITIONS = {
    'triage': ({'new', 'reopened'}, 'triaged'),
    'plan_validation': ({'triaged', 'needs_human'}, 'validation_planned'),
    'start_validation': ({'validation_planned'}, 'validating'),
    'needs_human': ({'triaged', 'validation_planned', 'validating'}, 'needs_human'),
    'confirm': ({'triaged', 'validating', 'needs_human'}, 'confirmed'),
    'acknowledge': ({'confirmed'}, 'dev_acknowledged'),
    'start_fix': ({'dev_acknowledged'}, 'fixing'),
    'submit_fix': ({'fixing'}, 'fix_ready'),
    'start_reverify': ({'fix_ready'}, 'reverifying'),
    'close': ({'reverifying'}, 'closed'),
    'reject': ({'new', 'triaged', 'validating'}, 'rejected'),
    'block': ({
        'new', 'triaged', 'validation_planned', 'validating', 'needs_human',
        'confirmed', 'dev_acknowledged', 'fixing', 'fix_ready', 'reverifying',
        'reopened',
    }, 'blocked'),
    'reopen': ({'closed', 'rejected', 'blocked'}, 'reopened'),
}

COLLABORATION_SUBJECT_TYPES = (
    'analysis_report', 'analysis_run', 'finding', 'case_review', 'topic')
COLLABORATION_SCOPES = {
    'report:read',
    'finding:read',
    'finding:comment',
    'finding:review',
    'finding:transition:acknowledge',
    'finding:transition:start_fix',
    'finding:transition:submit_fix',
    'case_review:read',
    'case_review:comment',
    'case_review:mark',
    'case_review:decision',
    'topic:read',
    'topic:reply',
}
DEFAULT_REVIEW_SCOPES = (
    'report:read',
    'finding:read',
    'finding:comment',
    'finding:review',
    'finding:transition:acknowledge',
    'finding:transition:start_fix',
    'finding:transition:submit_fix',
)
DEFAULT_CASE_REVIEW_SCOPES = (
    'case_review:read',
    'case_review:comment',
    'case_review:mark',
)
DEFAULT_TOPIC_SCOPES = (
    'topic:read',
    'topic:reply',
)

_COLLABORATION_PATHS = (
    (re.compile(r'^/api/v1/test-reports/\d+/(analysis-context|findings)/?$'), {'GET'}),
    (re.compile(r'^/api/v1/shift-left/findings/?$'), {'GET'}),
    (re.compile(r'^/api/v1/shift-left/findings/\d+/?$'), {'GET'}),
    (re.compile(r'^/api/v1/shift-left/findings/\d+/evidence/?$'), {'GET'}),
    (re.compile(r'^/api/v1/shift-left/findings/\d+/feedback/?$'), {'GET', 'POST'}),
    (re.compile(r'^/api/v1/shift-left/findings/\d+/comments/?$'), {'GET', 'POST'}),
    (re.compile(r'^/api/v1/shift-left/findings/\d+/(review-decisions|transitions)/?$'), {'POST'}),
    (re.compile(r'^/api/v1/shift-left/case-reviews/\d+/(context|cases|marks)/?$'),
     {'GET'}),
    (re.compile(r'^/api/v1/shift-left/case-reviews/\d+/comments/?$'), {'POST'}),
    (re.compile(r'^/api/v1/shift-left/case-reviews/\d+/reviews/?$'),
     {'GET', 'POST'}),
    (re.compile(r'^/api/v1/shift-left/case-reviews/\d+/reviews/\d+/?$'),
     {'PATCH', 'PUT', 'DELETE'}),
    (re.compile(r'^/api/v1/shift-left/case-reviews/\d+/marks/?$'), {'PUT'}),
    (re.compile(r'^/api/v1/topics/\d+/?$'), {'GET'}),
    (re.compile(r'^/api/v1/topics/\d+/replies/?$'), {'POST'}),
    (re.compile(r'^/api/v1/topics/\d+/replies/\d+/?$'),
     {'PATCH', 'PUT', 'DELETE'}),
)


def now_cst_naive():
    """Match the existing model convention (naive UTC+8 datetimes)."""
    return (datetime.now(timezone.utc) + timedelta(hours=8)).replace(tzinfo=None)


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), default=str)


def payload_hash(value):
    return hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()


def secret_hash(value):
    return hashlib.sha256(str(value or '').encode('utf-8')).hexdigest()


def generate_invitation_code():
    return 'hub_ci_' + secrets.token_urlsafe(32)


def generate_access_token():
    return 'hub_cs_' + secrets.token_urlsafe(40)


def collaboration_submission_identity(session_row, data, max_length=160):
    """Resolve a display-only identity from one external submission.

    Participant ownership is always the access-token session id. Identity is
    deliberately not part of the invitation or authorization boundary. This
    remains mandatory even for legacy tokens so old invitation labels cannot
    silently become the author of a new submission.
    """
    data = data if isinstance(data, dict) else {}
    value = str(
        data.get('identity')
        or data.get('agent_identity')
        or data.get('author_name')
        or '').strip()
    if not value:
        raise ValueError('identity 必填；该字段只用于本次提交的显示身份')
    if len(value) > int(max_length):
        raise ValueError('identity 不能超过 %s 字符' % int(max_length))
    return value


def normalize_scopes(raw_scopes, default_scopes=None):
    defaults = default_scopes or DEFAULT_REVIEW_SCOPES
    scopes = raw_scopes if isinstance(raw_scopes, list) else list(defaults)
    normalized = []
    for value in scopes:
        value = str(value or '').strip()
        if not value or value in normalized:
            continue
        if value not in COLLABORATION_SCOPES:
            raise ValueError('不支持的协作 scope: %s' % value)
        normalized.append(value)
    if not normalized:
        raise ValueError('协作 scope 不能为空')
    return normalized


def collaboration_path_allowed(path, method):
    method = str(method or '').upper()
    for pattern, methods in _COLLABORATION_PATHS:
        if method in methods and pattern.match(path or ''):
            return True
    return False


def find_active_collaboration_session(access_token):
    """Resolve a short-lived collaboration token without mutating the row."""
    if not str(access_token or '').startswith('hub_cs_'):
        return None
    from app.models import CollaborationSession
    row = CollaborationSession.query.filter_by(
        access_token_hash=secret_hash(access_token)).first()
    if not row or row.effective_status(now_cst_naive()) != 'active':
        return None
    return row


def build_baseline_fingerprint(data):
    baseline = {
        'project_id': data.get('project_id'),
        'iteration_id': data.get('iteration_id'),
        'client_repo': data.get('client_repo') or '',
        'client_base_sha': data.get('client_base_sha') or '',
        'client_target_sha': data.get('client_target_sha') or '',
        'server_repo': data.get('server_repo') or '',
        'server_base_sha': data.get('server_base_sha') or '',
        'server_target_sha': data.get('server_target_sha') or '',
        'config_digest': data.get('config_digest') or '',
        'proto_digest': data.get('proto_digest') or '',
        'requirement_revision': data.get('requirement_revision') or '',
        'testcase_revisions': data.get('testcase_revisions') or {},
        'knowledge_snapshots': data.get('knowledge_snapshots') or {},
        'analysis_profile_version': data.get('analysis_profile_version') or 'v1',
        'rule_snapshot_id': data.get('rule_snapshot_id'),
        'output_schema_version': data.get('output_schema_version') or 'v1',
    }
    return payload_hash(baseline), baseline


def transition_target(current_status, action):
    rule = FINDING_TRANSITIONS.get(action)
    if not rule:
        raise ValueError('不支持的 Finding action: %s' % action)
    sources, target = rule
    if current_status not in sources:
        raise ValueError('Finding 当前状态 %s 不允许执行 %s' % (current_status, action))
    return target


def validate_transition_preconditions(finding, action, data):
    if action == 'confirm':
        level = getattr(finding, 'evidence_level', 'hypothesis') or 'hypothesis'
        if EVIDENCE_RANK.get(level, 0) < EVIDENCE_RANK['contract_confirmed']:
            return 'confirmed 至少需要 contract_confirmed 证据'
    if action == 'reject' and not str(data.get('counter_evidence') or '').strip():
        return 'reject 必须提供 counter_evidence'
    if action == 'block' and not str(data.get('blocker') or '').strip():
        return 'block 必须提供 blocker'
    if action == 'submit_fix' and not str(data.get('fix_ref') or '').strip():
        return 'submit_fix 必须提供 fix_ref'
    if action == 'close':
        if not (str(data.get('fix_ref') or getattr(finding, 'fix_ref', '') or '').strip()
                and str(data.get('verification_ref') or '').strip()):
            return 'close 必须提供 fix_ref 和独立 verification_ref'
    if action == 'reopen' and not str(data.get('reason') or '').strip():
        return 'reopen 必须提供 reason'
    return None
