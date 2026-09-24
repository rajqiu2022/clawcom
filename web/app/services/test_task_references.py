"""Project-scoped reference bindings for test-plan tasks.

Task rows store canonical IDs only.  Execution payloads expose small metadata
manifests with authenticated read URLs so Providers can pull the latest source
on demand without copying large Skill/knowledge/report bodies into AgentTask.
"""
from app import db
from app.models import (
    AgentTeamKnowledgeResource,
    AgentTeamSkillResource,
    KnowledgeEntry,
    Skill,
    TestReport,
)


REFERENCE_FIELDS = {
    'reference_skill_ids': 'reference_skill_ids_json',
    'reference_knowledge_ids': 'reference_knowledge_ids_json',
    'reference_report_ids': 'reference_report_ids_json',
}


def _ids(value, field):
    if value is None:
        return []
    if (not isinstance(value, list) or len(value) > 50
            or any(isinstance(item, bool) for item in value)):
        raise ValueError('%s 必须为不超过 50 项的正整数数组' % field)
    try:
        result = sorted(set(int(item) for item in value))
    except (TypeError, ValueError):
        raise ValueError('%s 必须为不超过 50 项的正整数数组' % field)
    if any(item <= 0 for item in result):
        raise ValueError('%s 必须为不超过 50 项的正整数数组' % field)
    return result


def normalize_task_references(data, plan, current=None):
    """Validate changed reference arrays and preserve omitted values."""
    values = {}
    changed = set()
    for api_name, model_name in REFERENCE_FIELDS.items():
        fallback = getattr(current, model_name, []) if current else []
        values[model_name] = _ids(data.get(api_name, fallback), api_name)
        if api_name in data:
            changed.add(api_name)

    skill_ids = values['reference_skill_ids_json']
    knowledge_ids = values['reference_knowledge_ids_json']
    report_ids = values['reference_report_ids_json']

    if plan.team_id and 'reference_skill_ids' in changed:
        linked_skills = {
            row.skill_id for row in AgentTeamSkillResource.query.filter(
                AgentTeamSkillResource.team_id == plan.team_id,
                AgentTeamSkillResource.skill_id.in_(skill_ids or [-1]),
            ).all()
        }
        if linked_skills != set(skill_ids):
            raise ValueError('reference_skill_ids 只能绑定当前团队共享 Skill')
    elif not plan.team_id and 'reference_skill_ids' in changed:
        skills = Skill.query.filter(
            Skill.id.in_(skill_ids or [-1]),
            db.or_(Skill.is_deleted.is_(False), Skill.is_deleted.is_(None)),
            db.or_(Skill.review_status != 'rejected',
                   Skill.review_status.is_(None)),
        ).all()
        visible_skills = set()
        for skill in skills:
            projects = {
                int(value) for value in (skill.applicable_projects or [])
                if str(value).isdigit()
            }
            if skill.visibility != 'private' and (
                    not projects or plan.project_id in projects):
                visible_skills.add(skill.id)
        if visible_skills != set(skill_ids):
            raise ValueError('reference_skill_ids 包含不可用于当前项目的 Skill')

    if plan.team_id and 'reference_knowledge_ids' in changed:
        linked_knowledge = {
            row.knowledge_id for row in AgentTeamKnowledgeResource.query.filter(
                AgentTeamKnowledgeResource.team_id == plan.team_id,
                AgentTeamKnowledgeResource.knowledge_id.in_(
                    knowledge_ids or [-1]),
            ).all()
        }
        if linked_knowledge != set(knowledge_ids):
            raise ValueError('reference_knowledge_ids 只能绑定当前团队共享知识库')
    elif not plan.team_id and 'reference_knowledge_ids' in changed:
        knowledge = KnowledgeEntry.query.filter(
            KnowledgeEntry.id.in_(knowledge_ids or [-1]),
            KnowledgeEntry.project_id == plan.project_id,
            KnowledgeEntry.archived_at.is_(None),
        ).all()
        if {row.id for row in knowledge} != set(knowledge_ids):
            raise ValueError('reference_knowledge_ids 包含非本项目或已归档知识')

    if 'reference_report_ids' in changed:
        reports = TestReport.query.filter(
            TestReport.id.in_(report_ids or [-1]),
            TestReport.project_id == plan.project_id,
            TestReport.is_deleted.is_(False),
            TestReport.is_hidden.is_(False),
            TestReport.status != 'abandoned',
        ).all()
        if {row.id for row in reports} != set(report_ids):
            raise ValueError('reference_report_ids 包含非本项目、隐藏、已删除或已废弃报告')
    return values


def _skill_payload(skill):
    return {
        'id': skill.id,
        'kind': 'skill',
        'title': skill.display_name,
        'name': skill.name,
        'category': skill.category,
        'detail_api': '/api/v1/skills/%s' % skill.id,
        'pull_url': '/api/v1/skills/%s/pack' % skill.id,
        'web_url': '/skills?skill_id=%s' % skill.id,
    }


def _knowledge_payload(entry):
    is_wiki = (entry.entry_type or 'article') == 'test_journal'
    return {
        'id': entry.id,
        'kind': 'knowledge',
        'title': entry.title,
        'entry_type': entry.entry_type or 'article',
        'revision': int(entry.current_revision or 0),
        'detail_api': (
            '/api/v1/knowledge/journal-pages/%s' % entry.id
            if is_wiki else '/api/v1/knowledge/%s' % entry.id),
        'pull_url': '/api/v1/knowledge/%s/export.md' % entry.id,
        'web_url': (
            '/knowledge/wiki/%s' % entry.id
            if is_wiki else '/knowledge?entry_id=%s' % entry.id),
    }


def _report_payload(report):
    return {
        'id': report.id,
        'kind': 'report',
        'title': report.title,
        'report_type': report.report_type,
        'status': report.status or 'draft',
        'risk_level': report.risk_level or 'tbd',
        'detail_api': '/api/v1/test-reports/%s' % report.id,
        'web_url': '/test-reports/%s' % report.id,
    }


def serialize_reference_ids(skill_ids=None, knowledge_ids=None,
                            report_ids=None):
    """Return stable ordered metadata for a frozen set of reference IDs."""
    skill_ids = _ids(skill_ids or [], 'reference_skill_ids')
    knowledge_ids = _ids(knowledge_ids or [], 'reference_knowledge_ids')
    report_ids = _ids(report_ids or [], 'reference_report_ids')

    skills = ({row.id: row for row in Skill.query.filter(
        Skill.id.in_(skill_ids)).all()} if skill_ids else {})
    knowledge = ({row.id: row for row in KnowledgeEntry.query.filter(
        KnowledgeEntry.id.in_(knowledge_ids)).all()} if knowledge_ids else {})
    reports = ({row.id: row for row in TestReport.query.filter(
        TestReport.id.in_(report_ids)).all()} if report_ids else {})
    return {
        'skills': [_skill_payload(skills[item]) for item in skill_ids
                   if item in skills],
        'knowledge': [_knowledge_payload(knowledge[item])
                      for item in knowledge_ids if item in knowledge],
        'reports': [_report_payload(reports[item]) for item in report_ids
                    if item in reports],
        'read_policy': {
            'mode': 'on_demand',
            'required_before_execution': True,
            'fail_closed_when_unreadable': True,
            'note': '执行前通过 detail_api/pull_url 读取；关联 Skill 是任务参考，不等同于安装或扩大权限。',
        },
    }


def serialize_task_references(task, occurrence=None):
    source = occurrence or task
    return serialize_reference_ids(
        getattr(source, 'reference_skill_ids_json', None) or [],
        getattr(source, 'reference_knowledge_ids_json', None) or [],
        getattr(source, 'reference_report_ids_json', None) or [],
    )


def task_reference_options(plan):
    """Return only resources every member of this task's project can read."""
    if plan.team_id:
        skill_rows = (AgentTeamSkillResource.query
                      .join(Skill, Skill.id == AgentTeamSkillResource.skill_id)
                      .filter(
                          AgentTeamSkillResource.team_id == plan.team_id,
                          db.or_(Skill.is_deleted.is_(False),
                                 Skill.is_deleted.is_(None)),
                          db.or_(Skill.review_status != 'rejected',
                                 Skill.review_status.is_(None)))
                      .order_by(Skill.display_name, Skill.id).all())
        knowledge_rows = (AgentTeamKnowledgeResource.query
                          .join(KnowledgeEntry,
                                KnowledgeEntry.id ==
                                AgentTeamKnowledgeResource.knowledge_id)
                          .filter(
                              AgentTeamKnowledgeResource.team_id == plan.team_id,
                              KnowledgeEntry.archived_at.is_(None))
                          .order_by(KnowledgeEntry.title,
                                    KnowledgeEntry.id).all())
        skills = [row.skill for row in skill_rows]
        knowledge = [row.knowledge for row in knowledge_rows]
    else:
        skills = []
        for skill in Skill.query.filter(
                db.or_(Skill.is_deleted.is_(False),
                       Skill.is_deleted.is_(None)),
                db.or_(Skill.review_status != 'rejected',
                       Skill.review_status.is_(None)),
                Skill.visibility != 'private').order_by(
                    Skill.display_name, Skill.id).limit(500).all():
            projects = {
                int(value) for value in (skill.applicable_projects or [])
                if str(value).isdigit()
            }
            if not projects or plan.project_id in projects:
                skills.append(skill)
        knowledge = KnowledgeEntry.query.filter(
            KnowledgeEntry.project_id == plan.project_id,
            KnowledgeEntry.archived_at.is_(None),
        ).order_by(KnowledgeEntry.title, KnowledgeEntry.id).limit(500).all()

    reports = TestReport.query.filter(
        TestReport.project_id == plan.project_id,
        TestReport.is_deleted.is_(False),
        TestReport.is_hidden.is_(False),
        TestReport.status != 'abandoned',
    ).order_by(TestReport.updated_at.desc(), TestReport.id.desc()).limit(500).all()
    return {
        'skills': [_skill_payload(row) for row in skills],
        'knowledge': [_knowledge_payload(row) for row in knowledge],
        'reports': [_report_payload(row) for row in reports],
    }
