from __future__ import annotations

from datetime import date
from typing import Any


def build_experience_draft(
    task_title: str,
    project: str,
    module: str,
    failure: str,
    fix: str,
    verification: str,
    owner: str,
) -> dict[str, Any]:
    today = date.today().isoformat()
    slug = task_title.lower().replace(' ', '-')[:40]
    return {
        'id': f'pitfall-draft-{slug}',
        'title': task_title,
        'project': project,
        'module': module,
        'type': 'pitfall',
        'symptom': failure,
        'root_cause': '',
        'solution': fix,
        'verified_steps': [verification],
        'related_tools': [module],
        'keywords': [project, module],
        'status': 'active',
        'owner': owner,
        'created_at': today,
        'updated_at': today,
    }


def should_offer_draft(
    *,
    had_error_fix: bool = False,
    retry_count: int = 0,
    used_complex_tooling: bool = False,
    user_asked_record: bool = False,
) -> bool:
    return any([
        had_error_fix,
        retry_count > 2,
        used_complex_tooling,
        user_asked_record,
    ])
