"""
OpenSpace 自动进化 API
采集团队工作数据 → AI 分析 → 提炼可复用 Skill 和知识
"""
import json
from datetime import datetime, date, timedelta
from flask import request, jsonify
from sqlalchemy import text
from app import db
from app.models import (OpenClawInstance, DailyReport, KnowledgeEntry,
                        Skill, AuditLog)
from app.api import api_bp
from app.api.audit import log_action


def _get_sys_config(key, default=''):
    try:
        row = db.session.execute(
            text("SELECT value FROM system_config WHERE config_key = :k"),
            {'k': key}
        ).fetchone()
        return row[0] if row else default
    except Exception:
        return default


@api_bp.route('/openspace/analyze', methods=['POST'])
def openspace_analyze():
    """执行 OpenSpace 进化分析

    需要 admin 权限（龙虾王调用）。
    可选参数：
    - dry_run: true 时只分析不写入
    - days: 分析最近几天（默认 7）
    """
    data = request.get_json(silent=True) or {}
    dry_run = data.get('dry_run', False)
    days = data.get('days', 7)
    cutoff = date.today() - timedelta(days=days)

    # 1. 采集近 N 天日报
    reports = DailyReport.query.filter(
        DailyReport.report_date >= cutoff
    ).all()

    report_texts = []
    for r in reports:
        claw = OpenClawInstance.query.get(r.openclaw_id)
        claw_name = claw.name if claw else '未知'
        tasks = r.tasks_completed or []
        knowledge = r.knowledge_recorded or []
        report_texts.append(
            f"[{claw_name} {r.report_date}] 任务: {json.dumps(tasks, ensure_ascii=False)[:200]}, "
            f"知识: {json.dumps(knowledge, ensure_ascii=False)[:200]}"
        )

    # 2. 采集近期知识
    recent_knowledge = KnowledgeEntry.query.filter(
        KnowledgeEntry.created_at >= datetime.now() - timedelta(days=days),
        KnowledgeEntry.status == 'approved'
    ).limit(50).all()
    knowledge_texts = [f"[{k.category}] {k.title}: {k.content[:100]}" for k in recent_knowledge]

    # 3. 采集审计日志中的高频操作
    audit_logs = AuditLog.query.filter(
        AuditLog.created_at >= datetime.now() - timedelta(days=days),
        AuditLog.resource_type.in_(['skill', 'rule', 'knowledge'])
    ).limit(50).all()
    audit_texts = [f"[{a.action}] {a.resource_type}: {a.resource_name}" for a in audit_logs]

    # 4. 构造 LLM prompt
    prompt = f"""你是一个 AI 团队管理助手，负责分析团队工作模式并提炼可复用的技能和知识。

以下是团队近 {days} 天的工作数据：

## 日报摘要（{len(report_texts)} 条）
{chr(10).join(report_texts[:20]) if report_texts else '暂无日报'}

## 已有知识（{len(knowledge_texts)} 条）
{chr(10).join(knowledge_texts[:15]) if knowledge_texts else '暂无'}

## 操作日志（{len(audit_texts)} 条）
{chr(10).join(audit_texts[:15]) if audit_texts else '暂无'}

请分析以上数据，输出严格 JSON 格式（不要 markdown 包裹）：
{{
  "evolved_skills": [
    {{
      "name": "skill-identifier",
      "display_name": "中文名",
      "description": "描述",
      "template_content": "Skill 执行模板",
      "trigger_phrase": "触发词"
    }}
  ],
  "knowledge_entries": [
    {{
      "title": "知识标题",
      "content": "知识内容",
      "category": "分类标签"
    }}
  ],
  "summary": "整体分析摘要"
}}

如果数据不足无法提炼，返回空数组即可。直接输出 JSON："""

    # 5. 调用 LLM
    from app.api.system import _call_llm
    rows = dict(db.session.execute(
        text("SELECT config_key, value FROM system_config")
    ).fetchall())

    provider = rows.get('llm_provider', 'doubao')
    model = rows.get('llm_model', 'doubao-pro-32k')
    api_base = rows.get('llm_api_base', 'https://ark.cn-beijing.volces.com/api/v3')
    api_key = rows.get('llm_api_key', '')

    if not api_key:
        return jsonify({'error': 'LLM API Key 未配置'}), 400

    try:
        raw = _call_llm(prompt, provider, model, api_base, api_key)
        cleaned = raw.strip()
        if cleaned.startswith('```'):
            cleaned = cleaned.split('\n', 1)[1] if '\n' in cleaned else cleaned[3:]
        if cleaned.endswith('```'):
            cleaned = cleaned[:-3]
        result = json.loads(cleaned.strip())
    except Exception as e:
        return jsonify({'error': f'LLM 分析失败: {str(e)}', 'raw': raw if 'raw' in dir() else ''}), 500

    if dry_run:
        return jsonify({'dry_run': True, 'result': result})

    # 6. 写入 evolved Skills
    created_skills = 0
    for s in result.get('evolved_skills', []):
        if not s.get('name'):
            continue
        existing = Skill.query.filter_by(name=s['name']).first()
        if existing:
            continue
        skill = Skill(
            name=s['name'],
            display_name=s.get('display_name', s['name']),
            description=s.get('description', ''),
            category='evolved',
            trigger_phrase=s.get('trigger_phrase', ''),
            template_content=s.get('template_content', ''),
            scope='global',
            created_by='OpenSpace',
            review_status='pending',
            evolve_source='OpenSpace',
            last_evolved_at=datetime.now(),
        )
        db.session.add(skill)
        created_skills += 1

    # 7. 写入 openspace 知识
    created_knowledge = 0
    for k in result.get('knowledge_entries', []):
        if not k.get('title'):
            continue
        entry = KnowledgeEntry(
            title=k['title'],
            content=k.get('content', ''),
            category=k.get('category', 'general'),
            scope='global',
            source_type='openspace',
            status='pending_review',
        )
        db.session.add(entry)
        created_knowledge += 1

    db.session.commit()

    log_action('analyze', 'openspace', detail=
               f'进化分析完成: {created_skills} 个 Skill, {created_knowledge} 条知识',
               operator='OpenSpace')

    return jsonify({
        'message': '进化分析完成',
        'created_skills': created_skills,
        'created_knowledge': created_knowledge,
        'summary': result.get('summary', ''),
    })


@api_bp.route('/openspace/history', methods=['GET'])
def openspace_history():
    """查看进化历史"""
    logs = AuditLog.query.filter(
        AuditLog.resource_type == 'openspace'
    ).order_by(AuditLog.created_at.desc()).limit(20).all()
    return jsonify([l.to_dict() for l in logs])
