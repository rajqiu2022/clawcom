from datetime import datetime
from flask import request, jsonify
from app import db
from app.models import KnowledgeEntry, KnowledgeDistribution
from app.api import api_bp


@api_bp.route('/knowledge', methods=['GET'])
def list_knowledge():
    """查询知识（支持 scope/category/project/module 筛选）"""
    query = KnowledgeEntry.query

    scope = request.args.get('scope')
    category = request.args.get('category')
    project = request.args.get('project')
    module = request.args.get('module')
    status = request.args.get('status')

    if scope:
        query = query.filter(KnowledgeEntry.scope == scope)
    if category:
        query = query.filter(KnowledgeEntry.category == category)
    if project:
        query = query.filter(KnowledgeEntry.project_name == project)
    if module:
        query = query.filter(KnowledgeEntry.module_name == module)
    if status:
        query = query.filter(KnowledgeEntry.status == status)

    entries = query.order_by(
        KnowledgeEntry.created_at.desc()
    ).limit(100).all()
    return jsonify([e.to_dict() for e in entries])


@api_bp.route('/knowledge', methods=['POST'])
def create_knowledge():
    """创建知识条目"""
    data = request.get_json()
    if not data or not data.get('title') or not data.get('content'):
        return jsonify({'error': '标题和内容为必填项'}), 400

    entry = KnowledgeEntry(
        memos_id=data.get('memos_id'),
        title=data['title'],
        content=data['content'],
        category=data.get('category', 'general'),
        scope=data.get('scope', 'global'),
        project_name=data.get('project_name'),
        module_name=data.get('module_name'),
        source_openclaw_id=data.get('source_openclaw_id'),
        status=data.get('status', 'draft'),
    )
    db.session.add(entry)
    db.session.commit()
    return jsonify(entry.to_dict()), 201


@api_bp.route('/knowledge/<int:entry_id>', methods=['PUT'])
def update_knowledge(entry_id):
    """更新知识"""
    entry = KnowledgeEntry.query.get_or_404(entry_id)
    data = request.get_json()

    for field in ['title', 'content', 'category', 'scope',
                  'project_name', 'module_name', 'status']:
        if field in data:
            setattr(entry, field, data[field])

    db.session.commit()
    return jsonify(entry.to_dict())


@api_bp.route('/knowledge/pending', methods=['GET'])
def pending_reviews():
    """获取待审核列表"""
    entries = KnowledgeEntry.query.filter_by(
        status='pending_review'
    ).order_by(KnowledgeEntry.created_at.desc()).all()
    return jsonify([e.to_dict() for e in entries])


@api_bp.route('/knowledge/<int:entry_id>/review', methods=['POST'])
def review_knowledge(entry_id):
    """审核知识（通过/拒绝）"""
    entry = KnowledgeEntry.query.get_or_404(entry_id)
    data = request.get_json()

    action = data.get('action')  # 'approve' or 'reject'
    if action not in ('approve', 'reject'):
        return jsonify({'error': 'action 必须是 approve 或 reject'}), 400

    entry.reviewer_notes = data.get('notes', '')
    entry.approved_by = data.get('reviewer', 'admin')

    if action == 'approve':
        entry.status = 'approved'
        entry.approved_at = datetime.utcnow()
    else:
        entry.status = 'rejected'

    db.session.commit()
    return jsonify(entry.to_dict())


@api_bp.route('/knowledge/<int:entry_id>/distribute', methods=['POST'])
def distribute_knowledge(entry_id):
    """共享知识到指定范围"""
    entry = KnowledgeEntry.query.get_or_404(entry_id)

    if entry.status != 'approved':
        return jsonify({'error': '只有已审核通过的知识才能共享'}), 400

    data = request.get_json()
    dist = KnowledgeDistribution(
        knowledge_id=entry_id,
        target_scope=data.get('target_scope', 'all'),
        target_project=data.get('target_project'),
        target_module=data.get('target_module'),
        distributed_by=data.get('distributed_by', 'admin'),
    )
    db.session.add(dist)
    db.session.commit()
    return jsonify({'message': '知识已共享', 'distribution_id': dist.id}), 201
