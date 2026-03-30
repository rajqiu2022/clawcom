"""系统配置 API（LLM 配置等）"""
from flask import request, jsonify
from app import db
from app.api import api_bp

import sqlalchemy as sa
from sqlalchemy import text


@api_bp.route('/system/config', methods=['GET'])
def get_system_config():
    """获取所有系统配置"""
    rows = db.session.execute(
        text("SELECT config_key, config_value FROM system_config")
    ).fetchall()
    return jsonify({r.config_key: r.config_value for r in rows})


@api_bp.route('/system/config', methods=['PUT'])
def update_system_config():
    """批量更新系统配置"""
    data = request.get_json()
    if not data:
        return jsonify({'error': '数据为空'}), 400

    conn = db.engine.connect()
    for key, value in data.items():
        # 兼容 MySQL 和 SQLite
        existing = conn.execute(text(
            "SELECT 1 FROM system_config WHERE config_key=:k"
        ), {'k': key}).fetchone()
        if existing:
            conn.execute(text(
                "UPDATE system_config SET config_value=:v, updated_at=CURRENT_TIMESTAMP WHERE config_key=:k"
            ), {'k': key, 'v': value})
        else:
            conn.execute(text(
                "INSERT INTO system_config (config_key, config_value, updated_at) VALUES (:k, :v, CURRENT_TIMESTAMP)"
            ), {'k': key, 'v': value})
    conn.commit()
    conn.close()
    return jsonify({'message': '配置已更新'})


@api_bp.route('/system/llm/generate-spec', methods=['POST'])
def generate_spec():
    """调用 LLM 生成工作规范 MD 文件内容"""
    data = request.get_json()
    description = (data.get('description') or '').strip()
    project_name = (data.get('project_name') or '').strip()
    module_name = (data.get('module_name') or '').strip()
    claw_role = (data.get('claw_role') or 'test_member').strip()

    if not description:
        return jsonify({'error': '描述不能为空'}), 400

    # 读取 LLM 配置
    rows = dict(db.session.execute(
        text("SELECT config_key, config_value FROM system_config")
    ).fetchall())

    provider = rows.get('llm_provider', 'doubao')
    model = rows.get('llm_model', 'doubao-pro-32k')
    api_base = rows.get('llm_api_base', 'https://ark.cn-beijing.volces.com/api/v3')
    api_key = rows.get('llm_api_key', '')

    if not api_key:
        return jsonify({'error': 'LLM API Key 未配置，请先在系统设置中配置'}), 400

    # 构造 prompt
    role_labels = {
        'admin': '管理员（总览全局）',
        'test_manager': '测试经理（负责规划与审核）',
        'test_member': '测试成员（负责执行测试）',
        'test_executor': '测试执行（专注于测试执行与记录）',
    }
    role_label = role_labels.get(claw_role, '测试成员')

    prompt = f"""你是一个专业的游戏测试经理 AI（OpenClaw），擅长制定测试工作规范。

请为以下 OpenClaw 生成一份工作规范文档（Markdown 格式）：

- 角色类型：{role_label}
- 所属项目：{project_name or '通用项目'}
- 所属模块：{module_name or '通用模块'}
- 功能描述：{description}

规范要求：
1. 使用中文输出
2. 采用 Markdown 格式，结构清晰
3. 包含以下章节：
   - 角色定位（说明该角色的职责范围）
   - 核心能力（该角色应具备的技能）
   - 日常工作流程（按测试阶段：计划→设计→执行→报告）
   - 汇报机制（日报、周报格式要求）
   - 知识库规范（提交知识到 Memos 的标签、格式要求）
   - 协作规范（与其他角色/系统的配合要求）
4. 规范要具体、可执行，避免空洞描述
5. 输出纯 Markdown 内容，不要加 ```markdown 外层包裹

请直接输出规范内容："""

    # 调用 LLM
    try:
        spec_result = _call_llm(prompt, provider, model, api_base, api_key)
        return jsonify({'spec': spec_result})
    except Exception as e:
        return jsonify({'error': f'LLM 调用失败: {str(e)}'}), 500


@api_bp.route('/system/llm/generate', methods=['POST'])
def llm_generate():
    """通用 AI 生成接口，支持生成 Skill / Rule 等内容"""
    data = request.get_json()
    gen_type = data.get('type', 'skill')  # skill / rule
    description = (data.get('description') or '').strip()

    if not description:
        return jsonify({'error': '描述不能为空'}), 400

    # 读取 LLM 配置
    rows = dict(db.session.execute(
        text("SELECT config_key, config_value FROM system_config")
    ).fetchall())

    provider = rows.get('llm_provider', 'doubao')
    model = rows.get('llm_model', 'doubao-pro-32k')
    api_base = rows.get('llm_api_base', 'https://ark.cn-beijing.volces.com/api/v3')
    api_key = rows.get('llm_api_key', '')

    if not api_key:
        return jsonify({'error': 'LLM API Key 未配置，请先在系统设置中配置'}), 400

    if gen_type == 'skill':
        prompt = f"""你是一个 OpenClaw AI Agent 技能设计专家。

根据以下描述生成一个 Skill（技能），要求输出严格的 JSON 格式（不要 markdown 包裹）：

描述：{description}

JSON 格式要求：
{{
  "name": "skill-identifier (英文短横线格式)",
  "display_name": "中文显示名称",
  "description": "功能描述（1-2句话）",
  "trigger_phrase": "触发短语",
  "template_content": "Skill 的详细执行模板（Markdown 格式，包含步骤、输入输出要求等）"
}}

直接输出 JSON："""
    elif gen_type == 'rule':
        prompt = f"""你是一个 OpenClaw AI Agent 工作规范设计专家。

根据以下描述生成一个工作规范 Rule，要求输出严格的 JSON 格式（不要 markdown 包裹）：

描述：{description}

JSON 格式要求：
{{
  "name": "rule-identifier (英文短横线格式)",
  "display_name": "中文显示名称",
  "description": "功能描述（1-2句话）",
  "content_template": "规范的详细内容（Markdown 格式，包含角色定义、工作流程、检查要点等）"
}}

直接输出 JSON："""
    else:
        return jsonify({'error': f'不支持的生成类型: {gen_type}'}), 400

    try:
        result = _call_llm(prompt, provider, model, api_base, api_key)
        # 尝试解析 JSON
        import json
        # 去掉可能的 markdown 代码块包裹
        cleaned = result.strip()
        if cleaned.startswith('```'):
            cleaned = cleaned.split('\n', 1)[1] if '\n' in cleaned else cleaned[3:]
        if cleaned.endswith('```'):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

        parsed = json.loads(cleaned)
        return jsonify({'result': parsed, 'raw': result})
    except json.JSONDecodeError:
        return jsonify({'result': None, 'raw': result, 'error': 'AI 输出格式不标准，请手动调整'})
    except Exception as e:
        return jsonify({'error': f'LLM 调用失败: {str(e)}'}), 500


def _call_llm(prompt, provider, model, api_base, api_key):
    """统一 LLM 调用"""
    if provider == 'doubao':
        return _call_doubao(prompt, model, api_base, api_key)
    elif provider == 'ernie':
        return _call_ernie(prompt, model, api_key)
    elif provider == 'tongyi':
        return _call_tongyi(prompt, model, api_key)
    elif provider == 'zhipu':
        return _call_zhipu(prompt, model, api_key)
    elif provider == 'deepseek':
        return _call_deepseek(prompt, model, api_key)
    else:
        raise ValueError(f'不支持的 LLM 提供商: {provider}')


def _call_doubao(prompt, model, api_base, api_key):
    import requests
    url = f"{api_base.rstrip('/')}/chat/completions"
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json',
    }
    payload = {
        'model': model,
        'messages': [{'role': 'user', 'content': prompt}],
        'temperature': 0.7,
    }
    resp = requests.post(url, json=payload, headers=headers, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    return data['choices'][0]['message']['content']


def _call_ernie(prompt, model, api_key):
    import requests
    # 百度 ERNIE 使用不同的 API 格式
    url = f"https://aip.baidubce.com/rpc/2.0/ai_custom/v1/wenxinworkshop/chat/{model}?access_token={_get_ernie_token(api_key)}"
    payload = {
        'messages': [{'role': 'user', 'content': prompt}],
        'temperature': 0.7,
    }
    resp = requests.post(url, json=payload, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    return data['result']


def _get_ernie_token(api_key):
    # 简化处理，实际使用时需要用 AK/SK 获取 access_token
    return api_key


def _call_tongyi(prompt, model, api_key):
    import requests
    url = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json',
    }
    payload = {
        'model': model,
        'messages': [{'role': 'user', 'content': prompt}],
        'temperature': 0.7,
    }
    resp = requests.post(url, json=payload, headers=headers, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    return data['choices'][0]['message']['content']


def _call_zhipu(prompt, model, api_key):
    import requests
    url = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json',
    }
    payload = {
        'model': model,
        'messages': [{'role': 'user', 'content': prompt}],
        'temperature': 0.7,
    }
    resp = requests.post(url, json=payload, headers=headers, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    return data['choices'][0]['message']['content']


def _call_deepseek(prompt, model, api_key):
    import requests
    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json',
    }
    payload = {
        'model': model,
        'messages': [{'role': 'user', 'content': prompt}],
        'temperature': 0.7,
    }
    resp = requests.post(url, json=payload, headers=headers, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    return data['choices'][0]['message']['content']
