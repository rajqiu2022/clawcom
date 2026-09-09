"""Hub-owned ingestion for Workflow evidence manifests and structured findings."""

from datetime import datetime
import hashlib
import json

from app import db
from app.models import (
    AnalysisRuleReplay,
    AutomationCaseCandidate,
    CollaborationSession,
    CollaborationSessionEvent,
    EntityRelation,
    ShiftLeftAnalysisFinding,
    ShiftLeftAnalysisRun,
    ShiftLeftFinding,
    ShiftLeftFindingEvent,
    ShiftLeftFindingFeedback,
    TestAccount,
    WorkflowEvidenceManifest,
    WorkflowRunStep,
)
from app.services.entity_relations import best_effort_upsert_relations
from app.services.evidence_manifests import normalize_manifest
from app.services.workflow_evidence_scope import (
    evidence_outcome_values, scoped_evidence_missing,
)


def _first_dict(*values):
    return next((value for value in values if isinstance(value, dict)), {})


def _finding_list(*values):
    for value in values:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError):
                continue
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            for key in ('findings', 'bugs', 'items'):
                if isinstance(value.get(key), list):
                    return value[key]
    return []


def _artifact_items(evidence, run_id, step_id):
    items = []
    for raw_type, raw_value in (evidence or {}).items():
        values = raw_value if isinstance(raw_value, list) else [raw_value]
        for index, value in enumerate(values):
            if value in (None, ''):
                continue
            metadata = {'step_id': step_id, 'source_key': str(raw_type)}
            if isinstance(value, dict):
                uri = str(value.get('uri') or value.get('url') or value.get('path') or '')
                metadata.update(value.get('metadata') or {})
                for key in ('stage', 'case_id', 'snapshot_id', 'ui_tree_id',
                            'console_id', 'related_ids', 'status', 'code',
                            'reason'):
                    if value.get(key) not in (None, ''):
                        metadata[key] = value.get(key)
            else:
                uri = str(value)
            if '://' not in uri:
                metadata['original_ref'] = (
                    uri or str(metadata.get('status') or ''))
                uri = 'artifact://workflow-run/%s/%s/%s/%s' % (
                    run_id, step_id, raw_type, index)
            artifact_type = str(raw_type or 'artifact').strip().lower().replace('-', '_')
            artifact_type = ''.join(
                ch if ch.isalnum() or ch == '_' else '_' for ch in artifact_type)
            if not artifact_type or not artifact_type[0].isalpha():
                artifact_type = 'artifact'
            item = {
                'type': artifact_type[:64],
                'uri': uri[:2048],
                'step_id': str(step_id),
                'metadata': metadata,
            }
            items.append(item)
    return items


def _coverage_from_artifacts(artifacts, has_findings=False):
    def channel_state(aliases):
        matched = [
            item for item in artifacts
            if str(item.get('type') or '') in aliases]
        if not matched:
            return 'missing'
        statuses = {
            str((item.get('metadata') or {}).get('status')
                or (item.get('metadata') or {}).get('original_ref')
                or '').strip().lower()
            for item in matched
        }
        if statuses and statuses <= {'not_applicable'}:
            return 'not_applicable'
        return 'complete'

    return {
        'case_result': ('complete' if has_findings else channel_state({
            'case_result', 'case_results', 'case_summary', 'settlement_report',
            'result', 'results'})),
        'ui_snapshot': channel_state({
            'ui_snapshot', 'snapshot', 'snapshots', 'ui_tree'}),
        'console': channel_state({
            'console', 'console_log', 'console_logs', 'stdout_tail',
            'unity_log'}),
        'screenshots': channel_state({
            'screenshot', 'screenshots', 'image', 'images'}),
    }


def _not_applicable_is_complete(run):
    definition = getattr(run, 'definition', None)
    payload = getattr(definition, 'definition_json', None)
    context = payload.get('context') if isinstance(payload, dict) else {}
    profile = (
        context.get('evidence_profile')
        if isinstance(context, dict) else {})
    return bool(
        isinstance(profile, dict)
        and profile.get('not_applicable_is_complete') is True)


def _analysis_run(run, actor):
    row = ShiftLeftAnalysisRun.query.filter_by(workflow_run_id=run.id).order_by(
        ShiftLeftAnalysisRun.id.desc()).first()
    if row:
        return row
    fingerprint = hashlib.sha256(
        ('workflow-run:%s' % run.id).encode('utf-8')).hexdigest()
    row = ShiftLeftAnalysisRun(
        project_id=run.project_id,
        workflow_run_id=run.id,
        baseline_fingerprint=fingerprint,
        rerun_sequence=0,
        status='completed',
        baseline_json={'workflow_run_id': run.id},
        context_snapshot_json=run.context_json or {},
        result_summary_json={},
        created_by=actor,
        updated_by=actor,
        finished_at=datetime.now(),
    )
    db.session.add(row)
    db.session.flush()
    return row


def _finding_key(run_id, raw, index):
    value = str(raw.get('finding_key') or raw.get('key') or raw.get('id') or '').strip()
    if value:
        return value[:255]
    canonical = json.dumps(raw, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:20]
    return 'flow%s-%s-%s' % (run_id, index + 1, digest)


def _upsert_findings(run, analysis, findings, actor, manifest_id=None):
    result = []
    now = datetime.now()
    valid_severity = {'critical', 'high', 'medium', 'low', 'info'}
    valid_evidence = {'hypothesis', 'code_supported', 'runtime_supported', 'confirmed'}
    for index, raw in enumerate(findings or []):
        if not isinstance(raw, dict):
            continue
        key = _finding_key(run.id, raw, index)
        title = str(raw.get('title') or raw.get('summary') or raw.get('description')
                    or ('Workflow Finding %s' % (index + 1))).strip()[:300]
        row = ShiftLeftFinding.query.filter_by(
            project_id=run.project_id, finding_key=key).first()
        created = row is None
        if created:
            row = ShiftLeftFinding(
                project_id=run.project_id,
                analysis_run_id=analysis.id,
                finding_key=key,
                title=title,
                first_seen_at=now,
                created_by=actor,
                created_by_actor_key='workflow:%s' % run.id,
            )
            db.session.add(row)
        row.analysis_run_id = analysis.id
        row.title = title
        row.module = str(raw.get('module') or raw.get('component') or '')[:160]
        severity = str(raw.get('severity') or 'medium').lower()
        row.severity = severity if severity in valid_severity else 'medium'
        try:
            row.confidence = max(0.0, min(float(raw.get('confidence') or 0), 1.0))
        except (TypeError, ValueError):
            row.confidence = 0.0
        evidence_level = str(raw.get('evidence_level') or 'runtime_supported').lower()
        row.evidence_level = (
            evidence_level if evidence_level in valid_evidence else 'runtime_supported')
        row.source_type = str(raw.get('source_type') or 'workflow')[:40]
        row.description = raw.get('description') or raw.get('detail') or ''
        row.business_impact = raw.get('business_impact') or raw.get('impact') or ''
        row.code_locations_json = raw.get('code_locations') or []
        associations = raw.get('associations') if isinstance(raw.get('associations'), dict) else {}
        if manifest_id:
            associations = dict(associations, evidence_manifest_id=manifest_id)
        row.associations_json = associations
        row.last_seen_at = now
        row.updated_by = actor
        if not created:
            row.revision = int(row.revision or 1) + 1
        db.session.flush()
        occurrence = ShiftLeftAnalysisFinding.query.filter_by(
            analysis_run_id=analysis.id, finding_id=row.id).first()
        snapshot = {
            'title': row.title,
            'severity': row.severity,
            'evidence_level': row.evidence_level,
            'module': row.module,
            'evidence_manifest_id': manifest_id,
        }
        if occurrence:
            occurrence.snapshot_json = snapshot
        else:
            db.session.add(ShiftLeftAnalysisFinding(
                analysis_run_id=analysis.id,
                finding_id=row.id,
                snapshot_json=snapshot,
            ))
        result.append(row)
    return result


def ingest_workflow_result(run, step, data, actor='workflow'):
    """Ingest worker evidence/findings in the same transaction as result writeback."""
    data = data if isinstance(data, dict) else {}
    outputs = data.get('outputs') if isinstance(data.get('outputs'), dict) else {}
    evidence = data.get('evidence') if isinstance(data.get('evidence'), dict) else {}
    manifest_payload = _first_dict(
        data.get('evidence_manifest'), outputs.get('evidence_manifest'))
    findings = _finding_list(
        data.get('findings'), data.get('flow12_bugs'), data.get('flow12_bugs_json'),
        outputs.get('findings'), outputs.get('flow12_bugs'),
        outputs.get('flow12_bugs_json'))
    expects_evidence = bool(
        evidence or manifest_payload or findings
        or evidence_outcome_values(data)
        or (step.step_config_json or {}).get('evidence_requirements')
        or (step.step_config_json or {}).get('required_evidence'))
    if not expects_evidence:
        return {'status': '', 'manifest_id': None, 'finding_ids': []}
    if not run.project_id:
        run.evidence_ingest_status = 'EVIDENCE_INGEST_INCOMPLETE'
        return {
            'status': run.evidence_ingest_status,
            'manifest_id': None,
            'finding_ids': [],
            'error': 'workflow_run_project_required',
        }

    analysis = _analysis_run(run, actor)
    manifest = WorkflowEvidenceManifest.query.filter_by(
        workflow_run_id=run.id).first()
    artifacts = list(manifest.artifacts_json or []) if manifest else []
    incoming = list(manifest_payload.get('artifacts') or [])
    incoming.extend(_artifact_items(evidence, run.id, step.step_id))
    for item in incoming:
        if isinstance(item, dict):
            item = dict(item, step_id=step.step_id, metadata=dict(
                item.get('metadata') or {}, step_id=step.step_id,
                attempt_no=step.attempt_no or 1))
        artifacts.append(item)
    deduped_artifacts = []
    seen_artifacts = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        meta = artifact.get('metadata') or {}
        key = (str(artifact.get('type') or ''), str(artifact.get('uri') or ''),
               str(artifact.get('case_id', meta.get('case_id', ''))),
               str(meta.get('stage', '')), str(meta.get('step_id', '')),
               str(meta.get('attempt_no', '')), str(meta.get('status', '')))
        if key in seen_artifacts:
            continue
        seen_artifacts.add(key)
        deduped_artifacts.append(artifact)
    artifacts = deduped_artifacts
    coverage = manifest_payload.get('coverage')
    if not isinstance(coverage, dict):
        coverage = _coverage_from_artifacts(artifacts, bool(findings))
    if manifest:
        previous_coverage = manifest.coverage_json or {}
        coverage = {
            key: value
            for key, value in coverage.items()
        }
        for key, value in previous_coverage.items():
            coverage.setdefault(key, value)
    required_evidence = manifest_payload.get('required_evidence')
    if required_evidence is None and manifest:
        required_evidence = manifest.required_evidence_json
    normalized = normalize_manifest({
        'coverage': coverage,
        'artifacts': artifacts,
        'required_evidence': required_evidence,
    }, not_applicable_is_complete=_not_applicable_is_complete(run))
    scopes = []
    explicit_missing = []
    for row in WorkflowRunStep.query.filter_by(run_id=run.id).all():
        scopes.extend(dict(scope, step_id=row.step_id, attempt_no=row.attempt_no or 1)
                      for scope in (row.step_config_json or {}).get('evidence_requirements') or [])
        row_result = (dict(data) if row.step_id == step.step_id else {
            'outputs': row.outputs_json or {}, 'metrics': row.metrics_json or {},
            'evidence': row.evidence_json or {},
            **((row.contract_result_json or {}).get('outcome_input') or {}),
        })
        explicit_missing.extend(dict(item, step_id=row.step_id)
                                for item in evidence_outcome_values(row_result)
                                if item['status'] == 'ANALYSIS_INCOMPLETE')
    scope_missing = scoped_evidence_missing(scopes, normalized['artifacts'])
    if explicit_missing or scope_missing:
        normalized['completeness_status'] = 'incomplete'
        normalized['missing_required'].extend(scope_missing + explicit_missing)
    if manifest is None:
        manifest = WorkflowEvidenceManifest(
            project_id=run.project_id,
            workflow_run_id=run.id,
            revision=1,
            created_by=actor,
        )
        db.session.add(manifest)
    else:
        manifest.revision = int(manifest.revision or 1) + 1
    manifest.analysis_run_id = analysis.id
    manifest.coverage_json = normalized['coverage']
    manifest.artifacts_json = normalized['artifacts']
    manifest.required_evidence_json = normalized['required_evidence']
    manifest.completeness_status = normalized['completeness_status']
    manifest.missing_required_json = normalized['missing_required']
    manifest.updated_by = actor
    db.session.flush()

    finding_rows = _upsert_findings(
        run, analysis, findings, actor, manifest_id=manifest.id)
    previous_finding_ids = list(manifest.finding_ids_json or [])
    manifest.finding_ids_json = list(dict.fromkeys(
        previous_finding_ids + [row.id for row in finding_rows]))
    if manifest.finding_ids_json:
        manifest.classification = str(
            manifest_payload.get('classification')
            or manifest.classification or 'CONFIRMED_ANOMALY').upper()
    elif manifest.completeness_status != 'complete':
        manifest.classification = 'ANALYSIS_INCOMPLETE'
    manifest.analysis_summary_json = manifest_payload.get('analysis_summary') or {
        'ingested_step_id': step.step_id,
        'finding_count': len(finding_rows),
    }
    analysis.result_summary_json = manifest.analysis_summary_json
    run.evidence_ingest_status = (
        'EVIDENCE_INGEST_COMPLETE'
        if manifest.completeness_status == 'complete'
        else 'EVIDENCE_INGEST_INCOMPLETE')

    relations = [{
        'from_type': 'workflow_run', 'from_id': str(run.id),
        'relation_type': 'has_evidence',
        'to_type': 'evidence_manifest', 'to_id': str(manifest.id),
        'metadata': {'completeness_status': manifest.completeness_status},
    }]
    for row in finding_rows:
        relations.append({
            'from_type': 'finding', 'from_id': str(row.id),
            'relation_type': 'supported_by',
            'to_type': 'evidence_manifest', 'to_id': str(manifest.id),
            'metadata': {'workflow_run_id': run.id},
        })
    best_effort_upsert_relations(run.project_id, relations, actor)
    return {
        'status': run.evidence_ingest_status,
        'manifest_id': manifest.id,
        'finding_ids': [row.id for row in finding_rows],
        'completeness_status': manifest.completeness_status,
    }


def link_report_to_evidence(run, report, actor='workflow'):
    manifest = WorkflowEvidenceManifest.query.filter_by(
        workflow_run_id=run.id).first()
    if not manifest or not report or not run.project_id:
        return False
    best_effort_upsert_relations(run.project_id, [{
        'from_type': 'test_report', 'from_id': str(report.id),
        'relation_type': 'supported_by',
        'to_type': 'evidence_manifest', 'to_id': str(manifest.id),
        'metadata': {
            'workflow_run_id': run.id,
            'completeness_status': manifest.completeness_status,
        },
    }], actor)
    return True


def cleanup_workflow_result_records(run_id):
    """Remove or detach Shift Left records before deleting a Workflow Run.

    The caller owns the transaction. Stable findings shared by another
    analysis run are preserved and re-pointed to a remaining occurrence.
    """
    manifests = WorkflowEvidenceManifest.query.filter_by(
        workflow_run_id=run_id).all()
    manifest_ids = {row.id for row in manifests}
    analysis_rows = ShiftLeftAnalysisRun.query.filter_by(
        workflow_run_id=run_id).all()
    analysis_by_id = {row.id: row for row in analysis_rows}
    for row in manifests:
        if row.analysis_run_id:
            linked = db.session.get(ShiftLeftAnalysisRun, row.analysis_run_id)
            if linked:
                analysis_by_id.setdefault(linked.id, linked)
    analysis_ids = set(analysis_by_id)

    finding_ids = set()
    if analysis_ids:
        finding_ids.update(
            row[0] for row in db.session.query(
                ShiftLeftAnalysisFinding.finding_id).filter(
                    ShiftLeftAnalysisFinding.analysis_run_id.in_(analysis_ids)).all()
        )
        finding_ids.update(
            row[0] for row in db.session.query(ShiftLeftFinding.id).filter(
                ShiftLeftFinding.analysis_run_id.in_(analysis_ids)).all()
        )
    for manifest in manifests:
        finding_ids.update(
            int(value) for value in (manifest.finding_ids_json or [])
            if str(value).isdigit()
        )

    deleted_relations = 0
    relation_targets = (
        [('workflow_run', str(run_id))]
        + [('evidence_manifest', str(value)) for value in manifest_ids]
    )
    for entity_type, entity_id in relation_targets:
        deleted_relations += EntityRelation.query.filter(db.or_(
            db.and_(EntityRelation.from_type == entity_type,
                    EntityRelation.from_id == entity_id),
            db.and_(EntityRelation.to_type == entity_type,
                    EntityRelation.to_id == entity_id),
        )).delete(synchronize_session=False)

    deleted_manifests = WorkflowEvidenceManifest.query.filter_by(
        workflow_run_id=run_id).delete(synchronize_session=False)
    detached_candidates = AutomationCaseCandidate.query.filter_by(
        qualification_run_id=run_id).update(
            {'qualification_run_id': None}, synchronize_session=False)
    detached_accounts = TestAccount.query.filter_by(workflow_run_id=run_id).update(
        {'workflow_run_id': None}, synchronize_session=False)
    deleted_replays = AnalysisRuleReplay.query.filter_by(
        workflow_run_id=run_id).delete(synchronize_session=False)

    deleted_findings = 0
    preserved_findings = 0
    if analysis_ids:
        ShiftLeftAnalysisRun.query.filter(
            ShiftLeftAnalysisRun.rerun_of_id.in_(analysis_ids),
            ShiftLeftAnalysisRun.id.notin_(analysis_ids),
        ).update({'rerun_of_id': None}, synchronize_session=False)
        ShiftLeftAnalysisFinding.query.filter(
            ShiftLeftAnalysisFinding.analysis_run_id.in_(analysis_ids)
        ).delete(synchronize_session=False)
        db.session.flush()

        for finding_id in finding_ids:
            finding = db.session.get(ShiftLeftFinding, finding_id)
            if not finding:
                continue
            remaining = ShiftLeftAnalysisFinding.query.filter_by(
                finding_id=finding_id).order_by(
                    ShiftLeftAnalysisFinding.analysis_run_id.desc()).first()
            if remaining:
                if finding.analysis_run_id in analysis_ids:
                    finding.analysis_run_id = remaining.analysis_run_id
                preserved_findings += 1
                continue
            session_ids = [row[0] for row in db.session.query(
                CollaborationSession.id).filter_by(
                    subject_type='finding', subject_id=finding_id).all()]
            if session_ids:
                CollaborationSessionEvent.query.filter(
                    CollaborationSessionEvent.session_id.in_(session_ids)
                ).delete(synchronize_session=False)
                CollaborationSession.query.filter(
                    CollaborationSession.id.in_(session_ids)
                ).delete(synchronize_session=False)
            ShiftLeftFindingFeedback.query.filter_by(
                finding_id=finding_id).delete(synchronize_session=False)
            ShiftLeftFindingEvent.query.filter_by(
                finding_id=finding_id).delete(synchronize_session=False)
            deleted_relations += EntityRelation.query.filter(db.or_(
                db.and_(EntityRelation.from_type == 'finding',
                        EntityRelation.from_id == str(finding_id)),
                db.and_(EntityRelation.to_type == 'finding',
                        EntityRelation.to_id == str(finding_id)),
            )).delete(synchronize_session=False)
            db.session.delete(finding)
            deleted_findings += 1

        ShiftLeftAnalysisRun.query.filter(
            ShiftLeftAnalysisRun.id.in_(analysis_ids)
        ).delete(synchronize_session=False)

    return {
        'manifests': deleted_manifests,
        'analysis_runs': len(analysis_ids),
        'findings': deleted_findings,
        'preserved_findings': preserved_findings,
        'relations': deleted_relations,
        'rule_replays': deleted_replays,
        'detached_candidates': detached_candidates,
        'detached_test_accounts': detached_accounts,
    }
