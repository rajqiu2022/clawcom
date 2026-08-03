"""Requirement-to-testcase coverage helpers."""


COVERED = {'covered', 'covers'}
PARTIAL = {'partial'}
GAP = {'gap', 'new_required', ''}
OUTDATED = {'outdated'}


def _req_id(req):
    return req.get('id') if isinstance(req, dict) else getattr(req, 'id', None)


def _req_payload(req):
    if isinstance(req, dict):
        return {
            'id': req.get('id'),
            'tapd_story_id': req.get('tapd_story_id') or '',
            'title': req.get('title') or '',
        }
    return {
        'id': getattr(req, 'id', None),
        'tapd_story_id': getattr(req, 'tapd_story_id', '') or '',
        'title': getattr(req, 'title', '') or '',
    }


def _link_req_id(link):
    return (
        link.get('requirement_item_id')
        if isinstance(link, dict)
        else getattr(link, 'requirement_item_id', None)
    )


def _link_status(link):
    raw = (
        link.get('coverage_status')
        if isinstance(link, dict)
        else getattr(link, 'coverage_status', '')
    )
    return str(raw or '').strip()


def _best_status(statuses):
    status_set = set(statuses or [])
    if status_set & COVERED:
        return 'covered'
    if status_set & PARTIAL:
        return 'partial'
    if status_set & OUTDATED:
        return 'outdated'
    return 'gap'


def summarize_requirement_coverage(requirements, links):
    """Summarize whether every requirement has an effective testcase link."""
    reqs = list(requirements or [])
    links_by_req = {}
    for link in links or []:
        links_by_req.setdefault(_link_req_id(link), []).append(_link_status(link))

    covered = 0
    partial = []
    gaps = []
    outdated = []
    for req in reqs:
        rid = _req_id(req)
        status = _best_status(links_by_req.get(rid, ['gap']))
        payload = _req_payload(req)
        if status == 'covered':
            covered += 1
        elif status == 'partial':
            partial.append(payload)
        elif status == 'outdated':
            outdated.append(payload)
        else:
            gaps.append(payload)

    total = len(reqs)
    return {
        'total_requirements': total,
        'covered_requirements': covered,
        'partial_requirements': len(partial),
        'partial_requirement_items': partial,
        'gap_requirements': gaps,
        'outdated_requirements': outdated,
        'coverage_rate': round(covered / total, 4) if total else 0,
        'passed': bool(total and not partial and not gaps and not outdated),
    }
