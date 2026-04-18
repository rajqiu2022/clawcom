"""
Memos 集成 API
- 经验沉淀（从日报/工作记录提取知识并写入 Memos）
- 知识搜索（从 Memos 检索知识）
- Memos 连接测试
"""
import re
from flask import request, jsonify, current_app
from app.api import api_bp
from app import memos_client


def _enrich_memo(memo):
    """给 memo 附加中文标签名"""
    content = memo.get('content', '')
    tags_found = []
    for tag_key, tag_label in memos_client.KNOWLEDGE_TAGS.items():
        if f'#openclaw/{tag_key}' in content:
            # 提取 scope
            match = re.search(rf'#openclaw/{tag_key}/(\S+)', content)
            scope = match.group(1) if match else 'general'
            tags_found.append({
                'key': tag_key,
                'label': tag_label,
                'scope': scope,
                'full_tag': f'openclaw/{tag_key}/{scope}',
            })
    memo['knowledge_tags'] = tags_found
    # 提取标题（第一行 # 开头的）
    for line in content.split('\n'):
        line = line.strip()
        if line.startswith('# '):
            memo['title'] = line[2:].strip()
            break
    return memo


@api_bp.route('/memos/tags', methods=['GET'])
def list_knowledge_tags():
    """获取知识标签列表（含中文名）"""
    tags = [{'key': k, 'label': v} for k, v in memos_client.KNOWLEDGE_TAGS.items()]
    return jsonify(tags)


@api_bp.route('/memos/test', methods=['GET'])
def test_memos_connection():
    """测试 Memos 连接"""
    try:
        memos = memos_client.search_memos(limit=1)
        return jsonify({'status': 'ok', 'message': 'Memos 连接正常', 'memo_count': len(memos)})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@api_bp.route('/memos/search', methods=['GET'])
def search_memos_api():
    """搜索 Memos（结果附加中文标签）"""
    tag = request.args.get('tag')
    keyword = request.args.get('keyword')
    limit = request.args.get('limit', 20, type=int)

    memos = memos_client.search_memos(tag=tag, keyword=keyword, limit=limit)
    enriched = [_enrich_memo(m) for m in memos]
    return jsonify({'memos': enriched, 'count': len(enriched)})


@api_bp.route('/memos/knowledge', methods=['GET'])
def list_knowledge_memos():
    """列出所有 openclaw 知识（附加中文标签）"""
    tag = request.args.get('tag', 'openclaw')
    limit = request.args.get('limit', 50, type=int)

    memos = memos_client.search_memos(tag=tag, limit=limit)
    enriched = [_enrich_memo(m) for m in memos]
    return jsonify({'memos': enriched, 'count': len(enriched), 'tags': memos_client.KNOWLEDGE_TAGS})


@api_bp.route('/memos/deposit', methods=['POST'])
def deposit_knowledge():
    """
    手动触发经验沉淀（从指定内容提取知识并存入 Memos）

    请求体：
    {
        "claw_id": 1,
        "content": "工作内容",
        "module": "core_gameplay",
        "project": "QQ飞车"
    }
    """
    data = request.get_json()
    if not data or not data.get('content'):
        return jsonify({'error': 'content 为必填项'}), 400

    claw_id = data.get('claw_id')
    content = data['content']
    module = data.get('module')
    project = data.get('project')

    claw_name = 'unknown'
    if claw_id:
        from app.models import OpenClawInstance
        claw = OpenClawInstance.query.get(claw_id)
        if claw:
            claw_name = claw.name
            if not module:
                module = claw.module_name
            if not project:
                project = claw.project_name

    deposited = memos_client.extract_and_deposit_knowledge(
        claw_id, claw_name, content, module=module, project=project
    )

    return jsonify({
        'message': f'提取并沉淀了 {len(deposited)} 条知识',
        'deposited': deposited,
    })


@api_bp.route('/memos/upsert', methods=['POST'])
def upsert_knowledge_memo():
    """
    直接写入/更新一条知识 Memo

    请求体：
    {
        "tag": "method",
        "scope_key": "core_gameplay",
        "title": "核心单局测试方法",
        "content": "Markdown 内容"
    }
    """
    data = request.get_json()
    if not data or not data.get('tag') or not data.get('content'):
        return jsonify({'error': 'tag 和 content 为必填项'}), 400

    tag_label = memos_client.KNOWLEDGE_TAGS.get(data['tag'], data['tag'])

    result = memos_client.upsert_knowledge(
        tag=data['tag'],
        scope_key=data.get('scope_key', 'general'),
        title=data.get('title', tag_label),
        content=data['content'],
    )
    result['tag_label'] = tag_label
    return jsonify(result)
