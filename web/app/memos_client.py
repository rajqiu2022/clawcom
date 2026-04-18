"""
Memos 客户端（兼容占位）

原始 Memos 集成已迁移到知识库模块。
此文件保留 KNOWLEDGE_TAGS 和基本接口，供 memos_api.py 引用。
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


def extract_and_deposit_knowledge(content, tag, source_claw_id=None):
    """提取并存储知识"""
    from app import db
    from app.models import KnowledgeEntry
    entry = KnowledgeEntry(
        title=content[:100],
        content=content,
        category=tag,
        scope='global',
        source_openclaw_id=source_claw_id,
        source_type='openclaw',
        status='draft',
    )
    db.session.add(entry)
    db.session.commit()
    return entry.to_dict()


def upsert_knowledge(title, content, tag, scope='global',
                     project_name=None, module_name=None):
    """创建或更新知识条目"""
    from app import db
    from app.models import KnowledgeEntry
    existing = KnowledgeEntry.query.filter_by(title=title, category=tag).first()
    if existing:
        existing.content = content
        db.session.commit()
        return existing.to_dict()
    entry = KnowledgeEntry(
        title=title, content=content, category=tag,
        scope=scope, project_name=project_name,
        module_name=module_name, source_type='manual',
        status='draft',
    )
    db.session.add(entry)
    db.session.commit()
    return entry.to_dict()
