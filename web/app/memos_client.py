"""
Memos 客户端（Hub 知识库：落库到 KnowledgeEntry）

说明
----
- 读接口（search）与写接口（deposit / upsert）均走 MySQL ``knowledge_entries``，
  不再直连外部 Memos 服务；与 ``memos_api.py`` 的调用约定必须一致。
- 历史上 ``extract_and_deposit_knowledge`` / ``upsert_knowledge`` 曾与
  ``memos_api`` 参数不一致，会导致 POST 500；已对齐。
"""

KNOWLEDGE_TAGS = {
    'bug-experience': '缺陷经验',
    'test-method': '测试方法',
    'tool-usage': '工具使用',
    'project-knowledge': '项目知识',
    'performance': '性能相关',
    'compatibility': '兼容性',
    'security': '安全相关',
    'automation': '自动化',
    'general': '通用知识',
}


def search_memos(tag=None, keyword=None, limit=50):
    """搜索知识条目（从数据库）"""
    from app.models import KnowledgeEntry
    query = KnowledgeEntry.query
    if tag:
        query = query.filter(KnowledgeEntry.category == tag)
    if keyword:
        query = query.filter(KnowledgeEntry.title.like(f'%{keyword}%'))
    return [e.to_dict() for e in query.order_by(
        KnowledgeEntry.created_at.desc()).limit(limit).all()]


def extract_and_deposit_knowledge(claw_id, claw_name, content, module=None, project=None):
    """从工作记录沉淀一条知识（与 ``memos_api.deposit_knowledge`` 参数一致）。

    返回写入条目的 ``to_dict()`` 列表（当前实现为单条），便于接口统计
    ``len(deposited)``。
    """
    from app import db
    from app.models import KnowledgeEntry

    text = (content or '').strip()
    if not text:
        return []

    # 标题：首行非空，否则截取正文
    first_line = ''
    for line in text.split('\n'):
        s = line.strip()
        if s:
            first_line = s
            break
    title = (first_line[:250] or '工作记录沉淀')[:255]

    cat = module if module and module in KNOWLEDGE_TAGS else 'general'
    scope_val = 'project' if (project and str(project).strip()) else 'global'
    mod_name = None
    if cat == 'general' and module and module not in KNOWLEDGE_TAGS:
        mod_name = str(module)[:100]

    entry = KnowledgeEntry(
        title=title,
        content=text,
        category=cat,
        scope=scope_val,
        project_name=(str(project)[:100] if project else None),
        module_name=mod_name,
        source_openclaw_id=claw_id,
        source_type='openclaw',
        status='draft',
    )
    db.session.add(entry)
    db.session.commit()
    return [entry.to_dict()]


def upsert_knowledge(tag, content, title=None, scope_key='general',
                     project_name=None, module_name=None):
    """创建或更新知识条目（与 ``memos_api.upsert_knowledge_memo`` 请求体一致）。

    - ``tag``：对应 ``KnowledgeEntry.category``（建议在 ``KNOWLEDGE_TAGS`` 内）。
    - ``scope_key``：非 ``general`` 时记为 ``scope=module`` 并写入 ``module_name``。
    """
    from app import db
    from app.models import KnowledgeEntry

    t = (title or tag or '知识').strip()[:255] or '知识'
    cat = (tag or 'general').strip()[:50] or 'general'

    sk = (scope_key or 'general')
    if isinstance(sk, str):
        sk = sk.strip()
    if sk and sk != 'general':
        scope_val = 'module'
        mod = sk[:100]
    else:
        scope_val = 'global'
        mod = (module_name or None)
        if mod:
            mod = str(mod)[:100]

    existing = KnowledgeEntry.query.filter_by(title=t, category=cat).first()
    if existing:
        existing.content = content
        existing.scope = scope_val
        existing.module_name = mod
        if project_name:
            existing.project_name = str(project_name)[:100]
        db.session.commit()
        return existing.to_dict()

    entry = KnowledgeEntry(
        title=t,
        content=content,
        category=cat,
        scope=scope_val,
        project_name=(str(project_name)[:100] if project_name else None),
        module_name=mod,
        source_type='manual',
        status='draft',
    )
    db.session.add(entry)
    db.session.commit()
    return entry.to_dict()
