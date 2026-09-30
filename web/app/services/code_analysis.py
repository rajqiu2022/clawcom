"""Plan-scoped code review and feedback learning on existing Workflow/knowledge."""
import hashlib
import json
from urllib.parse import urlsplit

from app import db
from app.models import (CodeAnalysisProject, KnowledgeEntry, Project,
                        SharedResourcePolicy, SkillFile, WorkflowDefinition)
from app.services import resource_sharing as sharing


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                   separators=(',', ':'), default=str).encode()).hexdigest()


def repository(value):
    """Only references, never userinfo/query credentials or shell commands."""
    value = str(value or '').strip()
    parsed = urlsplit(value)
    if (not value or len(value) > 1000 or parsed.scheme not in ('https', 'ssh')
            or not parsed.hostname or parsed.password or parsed.query or parsed.fragment
            or (parsed.scheme == 'https' and parsed.username)
            or any(c.isspace() or ord(c) < 32 for c in value)):
        raise ValueError('仓库必须为不含凭据的 HTTPS/SSH URL；凭据使用 Worker 受控配置')
    return value


def git_ref(value):
    value = str(value or '').strip()
    if (not value or len(value) > 255 or value.startswith('-') or '..' in value
            or '@{' in value or value.endswith(('/', '.'))
            or any(c.isspace() or c in '~^:?*[\\' or ord(c) < 32 for c in value)):
        raise ValueError('分支或基线引用无效')
    return value


def initialize(project_id, actor):
    from app.api.knowledge_notebooks import _create_revision
    # The caller holds a common project-row mutex across all project inits.
    # The category marker locates the existing canonical global resource.
    global_page = KnowledgeEntry.query.filter_by(
        category='code_analysis_general', entry_type='test_journal', project_id=None).first()
    if not global_page:
        global_page = KnowledgeEntry(title='通用代码分析经验库', content='',
            category='code_analysis_general', entry_type='test_journal',
            scope='global', status='approved', created_by=actor['name'])
        db.session.add(global_page)
        db.session.flush()
        db.session.add(SharedResourcePolicy(resource_kind='knowledge',
            resource_id=global_page.id, scope='all', created_by=actor['name']))
        _create_revision(global_page, actor, global_page.title, '', '创建空白通用经验库',
                         0, 'code-analysis-general-init:%s' % global_page.id)
    config = db.session.get(CodeAnalysisProject, project_id)
    if not config:
        project = db.session.get(Project, project_id)
        page = KnowledgeEntry(title='%s · 代码分析经验库' % project.name, content='',
            project_id=project_id, project_name=project.name, category='code_analysis_project',
            entry_type='test_journal', scope='project', status='approved',
            created_by=actor['name'])
        db.session.add(page)
        db.session.flush()
        db.session.add(SharedResourcePolicy(resource_kind='knowledge',
            resource_id=page.id, owner_project_id=project_id, scope='project',
            created_by=actor['name']))
        _create_revision(page, actor, page.title, '', '创建空白项目经验库',
                         0, 'code-analysis-project-init:%s' % project_id)
        config = CodeAnalysisProject(project_id=project_id, knowledge_id=page.id)
        db.session.add(config)
    sharing.invalidate()
    return config, global_page


PROMPT = '''读取 start_vars.code_analysis 中的冻结合同。
所有 result_api 回写携带当前 Workflow run_id 作为 workflow_run_id，以及当前 step attempt_no，不能使用旧 attempt。
operation=analyze 时：
按初始 Skill 的方法，在独立只读代码工作区分析固定 commit；禁止修改业务源码、合并或推送分支。
先校验 Skill 内容/文件摘要和知识版本，解析 source_ref 与 baseline_ref 为真实 commit，记录 diff 范围。
结合冻结测试计划与历史人工反馈；待确认/风险接受不等于误报；经验只是线索，必须给本项目证据。
向 result_api 提交基线、全部结构化候选 findings、完整 report_id。报告先发布到 Hub，不能给本地路径。
候选只能建议 submit/confirm/ignore，不代替人工标注，不自行创建 TAPD Bug。
operation=learn 时：对照人工反馈和原分析证据，总结有效方法/误判原因。
向 result_api 提交 expected_revision、project_content（项目经验库更新后的完整 Markdown）、summary；
业务无关的经验提交 general_proposal，不自动覆盖通用知识或 Skill。保留其他协作者内容；冲突先重读再合并。
这不是模型权重训练。只有知识版本和学习回执保存成功才算学习完成；仅回复“已学习”不算完成。'''


def default_definition(project_id, actor, executor_id):
    from app.services.workflows import normalize_workflow_definition
    key = 'code-analysis-project-%s' % project_id
    row = WorkflowDefinition.query.filter_by(workflow_key=key).first()
    if row and (row.project_id != project_id or
            (row.definition_json or {}).get('context', {}).get('code_analysis_template') != 1):
        raise ValueError('代码分析模板标识已被其他定义占用，请由维护者核验')
    if not row:
        definition = normalize_workflow_definition({
            'key': key, 'name': '项目代码分析与反馈学习', 'version': 1,
            'context': {'code_analysis_template': 1},
            'steps': [{'id': 'analyze', 'name': '分析 / 反馈学习', 'type': 'agent_task',
                       'prompt': PROMPT, 'target_claw_id_var': 'executor_claw_id'}]})
        row = WorkflowDefinition(workflow_key=key, name=definition['name'],
            project_id=project_id, definition_json=definition, owner_type=actor['type'],
            owner_id=actor['id'], visibility_scope='project', executor_acl_json={})
        db.session.add(row)
    # Only our project-local template receives project-authorized launchers.
    if (row.definition_json or {}).get('context', {}).get('code_analysis_template') == 1:
        acl = dict(row.executor_acl_json or {})
        field = 'claw_ids' if actor['type'] == 'claw' else 'user_ids'
        acl[field] = sorted(set(acl.get(field) or []) | {actor['id']})
        acl['claw_ids'] = sorted(set(acl.get('claw_ids') or []) | {executor_id})
        row.executor_acl_json = acl
    return row


def skill_snapshot(skill):
    files = SkillFile.query.filter_by(skill_id=skill.id).order_by(SkillFile.id).all()
    body = {'id': skill.id, 'name': skill.name, 'content': skill.template_content or '',
            'files': [{'filename': f.filename, 'content': f.content or ''} for f in files],
            'attachments': [{'filename': a.filename, 'sha256': a.sha256}
                            for a in skill.attachment_entries.order_by('id').all()]}
    body['sha256'] = digest(body)
    body['pull_url'] = '/api/v1/skills/%s/pack' % skill.id
    return body


def knowledge_snapshot(entry):
    return {'id': entry.id, 'title': entry.title, 'content': entry.content or '',
            'revision': int(entry.current_revision or 0), 'sha256': digest(entry.content or ''),
            'project_id': entry.project_id}


def decision_payload(row):
    return {'id': row.id, 'finding_id': row.finding_id, 'analysis_run_id': row.analysis_run_id,
            'action': row.action, 'truth_label': row.truth_label, 'reason': row.reason,
            'actor_name': row.actor_name, 'submission_status': row.submission_status,
            'bug_id': row.bug_id or '', 'learning_status': row.learning_status,
            'learning_run_id': row.learning_run_id, 'learned_revision': row.learned_revision,
            'learning_summary': row.learning_summary or '',
            'general_published_revision': row.general_published_revision,
            'general_proposal': row.general_proposal or '', 'created_at': str(row.created_at)}
