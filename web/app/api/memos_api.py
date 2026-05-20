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


@api_bp.route('/memos/memo/<path:memo_id>', methods=['GET'])
def get_memo_by_id(memo_id):
    """按 memo uid 直接读取（List 搜不到时可用，如历史 PRIVATE 笔记）"""
    memo = memos_client.get_memo(memo_id)
    if not memo:
        return jsonify({'error': 'memo 不存在或不可读'}), 404
    return jsonify(_enrich_memo(memo))


@api_bp.route('/memos/tags', methods=['GET'])
def list_knowledge_tags():
    """获取知识标签列表（含中文名）"""
    tags = [{'key': k, 'label': v} for k, v in memos_client.KNOWLEDGE_TAGS.items()]
    return jsonify(tags)


@api_bp.route('/memos/test', methods=['GET'])
def test_memos_connection():
    """测试 Memos 连接（需配置 MEMOS_API_KEY）"""
    result = memos_client.test_connection()
    if result.get('ok'):
        return jsonify({
            'status': 'ok',
            'message': result.get('message'),
            'memo_count': result.get('memo_count'),
            'url': result.get('url'),
        })
    return jsonify({'status': 'error', 'message': result.get('message')}), 503


@api_bp.route('/memos/search', methods=['GET'])
def search_memos_api():
    """搜索 Memos（结果附加中文标签）

    查询参数 claw_only=true 时，仅返回当前 Bearer Token 对应 Claw 的 #claw-{name} 笔记。
    """
    tag = request.args.get('tag')
    keyword = request.args.get('keyword')
    limit = request.args.get('limit', 20, type=int)
    claw_only = request.args.get('claw_only', '').lower() in ('1', 'true', 'yes')

    claw_name = None
    if claw_only:
        claw, _, _, _, submitter = _resolve_submitter()
        claw_name = (claw.name if claw else None) or submitter

    memos = memos_client.search_memos(
        tag=tag, keyword=keyword, limit=limit, claw_name=claw_name,
    )
    enriched = [_enrich_memo(m) for m in memos]
    return jsonify({'memos': enriched, 'count': len(enriched)})


@api_bp.route('/memos/knowledge', methods=['GET'])
def list_knowledge_memos():
    """列出 openclaw 标签的 Memos 笔记（附加中文标签）"""
    tag = request.args.get('tag', 'openclaw')
    limit = request.args.get('limit', 50, type=int)
    claw_only = request.args.get('claw_only', '').lower() in ('1', 'true', 'yes')
    claw_name = None
    if claw_only:
        claw, _, _, _, submitter = _resolve_submitter()
        claw_name = (claw.name if claw else None) or submitter

    memos = memos_client.search_memos(tag=tag, limit=limit, claw_name=claw_name)
    enriched = [_enrich_memo(m) for m in memos]
    return jsonify({'memos': enriched, 'count': len(enriched), 'tags': memos_client.KNOWLEDGE_TAGS})


def _resolve_submitter():
    """从 Bearer Token 或 Web 登录会话反推提交人。

    返回 (claw, user, source_openclaw_id, source_type, submitter_name)。
    若都没有，submitter_name=None；调用方决定是否拒绝匿名请求。

    历史教训（MEMORY #131）：早期 ``memos/deposit`` 和 ``memos/upsert`` 接口
    不校验 token、不反推 claw_id，导致一批"孤儿草稿"无法追溯提交人；
    现在统一在此函数收口。
    """
    claw = None
    user = None
    try:
        from app.api.knowledge import _get_current_openclaw, _get_current_user
        claw = _get_current_openclaw()
        user = _get_current_user()
    except Exception:
        pass

    if claw:
        return (claw, user, claw.id, 'openclaw',
                (claw.name or f'OpenClaw#{claw.id}'))
    if user:
        return (None, user, None, 'web',
                getattr(user, 'username', None) or 'web-user')
    return (None, None, None, 'manual', None)


@api_bp.route('/memos/deposit', methods=['POST'])
def deposit_knowledge():
    """手动触发经验沉淀（从指定内容提取知识并存入知识库）。

    认证（自 v2.1 MEMORY #131 起强制）：
      - Authorization: Bearer <OpenClaw token>  → 强制覆盖 claw_id
      - 或 Web 登录会话（任意角色）           → submitter=user.username
      - 都不提供且 strict 模式 → 401

    请求体：
    {
        "content":  "工作内容",            // 必填
        "module":   "core_gameplay",     // 可选
        "project":  "QQ飞车",            // 可选
        "claw_id":  1                    // 仅当未携带 token 且当前用户是 super_admin 时生效
    }
    """
    data = request.get_json() or {}
    if not data.get('content'):
        return jsonify({'error': 'content 为必填项'}), 400

    claw, user, claw_id, _src_type, submitter = _resolve_submitter()
    # 兼容 super_admin 显式传 claw_id 代写（运维/批量导入场景）
    if not claw_id and data.get('claw_id') and user and getattr(user, 'role', '') == 'super_admin':
        claw_id = data['claw_id']
        from app.models import OpenClawInstance
        forged = OpenClawInstance.query.get(claw_id)
        if forged:
            submitter = forged.name

    if not claw_id and not submitter:
        return jsonify({
            'error': '匿名提交已禁用，请在 Authorization Header 携带 OpenClaw Bearer Token，'
                     '或先登录 Web；如需运维代写请用 super_admin 登录后传 claw_id。',
        }), 401

    # 补默认 module/project（从 claw 拿）
    module = data.get('module')
    project = data.get('project')
    claw_name = submitter
    if claw_id and (not module or not project):
        from app.models import OpenClawInstance
        c = OpenClawInstance.query.get(claw_id)
        if c:
            module = module or c.module_name
            project = project or c.project_name
            claw_name = c.name

    deposited = memos_client.extract_and_deposit_knowledge(
        claw_id, claw_name, data['content'],
        module=module, project=project,
        created_by=submitter,
    )

    return jsonify({
        'message': f'提取并沉淀了 {len(deposited)} 条知识',
        'submitted_by': submitter,
        'source_openclaw_id': claw_id,
        'deposited': deposited,
    })


@api_bp.route('/memos/upsert', methods=['POST'])
def upsert_knowledge_memo():
    """直接写入/更新一条知识条目。

    认证：同 ``deposit_knowledge``（自 v2.1 起强制非匿名）。

    请求体：
    {
        "tag":       "method",          // 必填，作为 category
        "title":     "核心单局测试方法",
        "content":   "Markdown 内容",   // 必填
        "scope_key": "core_gameplay"
    }
    """
    data = request.get_json() or {}
    if not data.get('tag') or not data.get('content'):
        return jsonify({'error': 'tag 和 content 为必填项'}), 400

    claw, user, claw_id, source_type, submitter = _resolve_submitter()
    if not submitter:
        return jsonify({
            'error': '匿名提交已禁用，请在 Authorization Header 携带 OpenClaw Bearer Token，'
                     '或先登录 Web。',
        }), 401

    tag_label = memos_client.KNOWLEDGE_TAGS.get(data['tag'], data['tag'])

    try:
        result = memos_client.upsert_knowledge(
            tag=data['tag'],
            scope_key=data.get('scope_key', 'general'),
            title=data.get('title', tag_label),
            content=data['content'],
            source_openclaw_id=claw_id,
            source_type=source_type,
            created_by=submitter,
        )
    except memos_client.MemosNotConfiguredError as e:
        return jsonify({'error': str(e)}), 503
    except RuntimeError as e:
        current_app.logger.warning('Memos upsert 失败: %s', e)
        return jsonify({'error': str(e)}), 502

    result['tag_label'] = tag_label
    result['submitted_by'] = submitter
    return jsonify(result)
