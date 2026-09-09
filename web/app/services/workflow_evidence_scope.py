"""Compute evidence coverage without allowing another case to fill a gap."""


def evidence_outcome_values(value, path='result'):
    """Read explicit verdicts, including nested case/phase results."""
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            child = '%s.%s' % (path, key)
            if key in ('evidence_outcome', 'evidence_ingest_status'):
                status = str(item or '').upper()
                if status in ('ANALYSIS_INCOMPLETE', 'EVIDENCE_INGEST_INCOMPLETE'):
                    found.append({'path': child, 'status': 'ANALYSIS_INCOMPLETE'})
                elif status in ('COMPLETE', 'EVIDENCE_INGEST_COMPLETE'):
                    found.append({'path': child, 'status': 'COMPLETE'})
            elif isinstance(item, (dict, list)):
                found.extend(evidence_outcome_values(item, child))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(evidence_outcome_values(item, '%s[%s]' % (path, index)))
    return found


def normalize_evidence_requirements(value):
    if not isinstance(value, list) or len(value) > 500:
        raise ValueError('evidence_requirements must be an array of at most 500 scopes')
    result = []
    seen = set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError('evidence requirement must be an object')
        entry = {}
        for key in ('case_id', 'stage', 'channel'):
            raw = item.get(key)
            if isinstance(raw, bool) or not isinstance(raw, (str, int)) or not str(raw).strip():
                raise ValueError('evidence requirement needs case_id, stage and channel')
            entry[key] = str(raw).strip()
            if len(entry[key]) > 200:
                raise ValueError('evidence scope field exceeds 200 characters')
        identity = tuple(entry.values())
        if identity in seen:
            raise ValueError('duplicate evidence requirement')
        seen.add(identity)
        entry['allow_not_applicable'] = item.get('allow_not_applicable') is True
        result.append(entry)
    return result


def scoped_evidence_missing(requirements, artifacts):
    """Only exact case + stage + channel receipts satisfy an expectation."""
    missing = []
    for expected in requirements:
        matched = []
        for artifact in artifacts:
            meta = artifact.get('metadata') or {}
            if ('step_id' in expected and (
                    meta.get('step_id') != expected['step_id']
                    or meta.get('attempt_no') != expected['attempt_no'])):
                continue
            if (str(artifact.get('case_id', meta.get('case_id', ''))) == expected['case_id']
                    and str(artifact.get('stage', meta.get('stage', ''))) == expected['stage']
                    and str(artifact.get('type', '')) == expected['channel']):
                matched.append(artifact)
        complete = any(
            str((item.get('metadata') or {}).get('status', 'complete')).lower() == 'complete'
            and bool(item.get('uri'))
            and not str(item.get('uri')).startswith('artifact://workflow-run/')
            for item in matched)
        not_applicable = any(
            (item.get('metadata') or {}).get('status') == 'not_applicable'
            and (item.get('metadata') or {}).get('reason') for item in matched)
        if not complete and not (expected.get('allow_not_applicable') and not_applicable):
            missing.append(dict(expected, status='missing'))
    return missing
