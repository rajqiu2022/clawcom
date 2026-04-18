import os
import uuid
from datetime import datetime
from flask import request, jsonify, current_app, send_from_directory
from app import db
from app.models import KnowledgeEntry, KnowledgeDistribution
from app.api import api_bp


def _get_current_user():
    """获取当前用户（支持 Web session 和 OpenClaw Bearer Token）"""
    from flask import session
    from app.models import User, OpenClawInstance
    uid = session.get('user_id')
    if uid:
        return User.query.get(uid)

    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        token = auth[7:]
        for claw in OpenClawInstance.query.filter(OpenClawInstance.status != 'deleted').all():
            if claw.verify_token(token):
                if claw.role == 'admin':
                    class _AdminProxy:
                        role = 'super_admin'
                        username = claw.name
                        managed_projects = []
                    return _AdminProxy()
                owner = User.query.filter_by(username=claw.owner).first()
                if owner:
                    owner._claw_name = claw.name
                    return owner
    return None


UPLOAD_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                          'static', 'uploads')


@api_bp.route('/upload/image', methods=['POST'])
def upload_image():
    """上传图片，返回 Markdown 可用的 URL"""
    if 'image' not in request.files:
        return jsonify({'error': '未选择文件'}), 400

    f = request.files['image']
    if not f.filename:
        return jsonify({'error': '文件名为空'}), 400

    # 校验文件类型
    allowed = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.svg'}
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in allowed:
        return jsonify({'error': f'不支持的格式: {ext}'}), 400

    # 限制 10MB
    f.seek(0, 2)
    size = f.tell()
    f.seek(0)
    if size > 10 * 1024 * 1024:
        return jsonify({'error': '图片不能超过 10MB'}), 400

    os.makedirs(UPLOAD_DIR, exist_ok=True)

    # 生成唯一文件名
    filename = f'{uuid.uuid4().hex[:12]}{ext}'
    filepath = os.path.join(UPLOAD_DIR, filename)
    f.save(filepath)

    url = f'/static/uploads/{filename}'
    return jsonify({
        'url': url,
        'filename': filename,
        # Vditor 需要的格式
        'data': {'url': url},
    })


@api_bp.route('/knowledge', methods=['GET'])
def list_knowledge():
    """查询知识（支持 scope/category/project/source_type 筛选）"""
    query = KnowledgeEntry.query

    scope = request.args.get('scope')
    category = request.args.get('category')
    project = request.args.get('project')
    module = request.args.get('module')
    status = request.args.get('status')
    source_type = request.args.get('source_type')

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
    if source_type:
        query = query.filter(KnowledgeEntry.source_type == source_type)

    # 全文搜索
    search = request.args.get('search')
    if search:
        search_term = f'%{search}%'
        query = query.filter(
            db.or_(
                KnowledgeEntry.title.like(search_term),
                KnowledgeEntry.content.like(search_term),
            )
        )

    entries = query.order_by(
        KnowledgeEntry.created_at.desc()
    ).limit(200).all()
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
        source_type=data.get('source_type', 'manual'),
        status=data.get('status', 'draft'),
    )
    db.session.add(entry)
    db.session.commit()
    return jsonify(entry.to_dict()), 201


@api_bp.route('/knowledge/batch-import', methods=['POST'])
def batch_import_knowledge():
    """批量导入知识条目（来自 OpenSpace 或 Agent 上报）"""
    data = request.get_json()
    entries = data.get('entries', [])
    source_type = data.get('source_type', 'openspace')

    if not entries:
        return jsonify({'error': 'entries 数组不能为空'}), 400

    if source_type not in ('openclaw', 'openspace'):
        return jsonify({'error': 'source_type 必须是 openclaw 或 openspace'}), 400

    created = 0
    for e_data in entries:
        if not e_data.get('title') or not e_data.get('content'):
            continue

        entry = KnowledgeEntry(
            memos_id=e_data.get('memos_id'),
            title=e_data['title'],
            content=e_data['content'],
            category=e_data.get('category', 'general'),
            scope=e_data.get('scope', 'global'),
            project_name=e_data.get('project_name'),
            module_name=e_data.get('module_name'),
            source_openclaw_id=e_data.get('source_openclaw_id'),
            source_type=source_type,
            status=e_data.get('status', 'pending_review'),
        )
        db.session.add(entry)
        created += 1

    db.session.commit()

    return jsonify({
        'message': f'导入 {created} 条知识（待审核）',
        'created': created,
    })


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
    """审核知识（通过/拒绝）— 仅超级管理员（龙虾王 Token 认证映射为 super_admin）"""
    user = _get_current_user()
    if not user or user.role not in ('super_admin', 'admin'):
        return jsonify({'error': '仅管理员可审核知识'}), 403

    entry = KnowledgeEntry.query.get_or_404(entry_id)
    data = request.get_json()

    action = data.get('action')  # 'approve' or 'reject'
    if action not in ('approve', 'reject'):
        return jsonify({'error': 'action 必须是 approve 或 reject'}), 400

    entry.reviewer_notes = data.get('notes', '')
    entry.approved_by = data.get('reviewer', 'admin')

    if action == 'approve':
        entry.status = 'approved'
        entry.approved_at = datetime.now()
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


@api_bp.route('/knowledge/<int:entry_id>', methods=['DELETE'])
def delete_knowledge(entry_id):
    """删除知识条目"""
    entry = KnowledgeEntry.query.get_or_404(entry_id)

    # 先删关联的分发记录
    KnowledgeDistribution.query.filter_by(knowledge_id=entry_id).delete()
    db.session.delete(entry)
    db.session.commit()
    return jsonify({'message': '已删除'})
