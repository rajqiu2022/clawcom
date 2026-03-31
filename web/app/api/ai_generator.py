"""
AI 生成器 API
用于通过大模型生成 Rules、Skills 和用例
"""

import json
import re
from flask import request, jsonify
from app import db
from app.models import Rule, Skill, TestCaseLibrary, TestCase
from app.api import api_bp


# ==================== 通用 AI 调用 ====================

def call_llm(prompt, system_prompt=None, model=None):
    """
    调用大模型（统一入口）
    从 system_config 表读取 LLM 配置，调用对应的大模型 API
    """
    from sqlalchemy import text

    try:
        rows = dict(db.session.execute(
            text("SELECT config_key, config_value FROM system_config")
        ).fetchall())

        provider = rows.get('llm_provider', 'doubao')
        llm_model = model or rows.get('llm_model', 'doubao-pro-32k')
        api_base = rows.get('llm_api_base', 'https://ark.cn-beijing.volces.com/api/v3')
        api_key = rows.get('llm_api_key', '')

        if not api_key:
            print("LLM API Key 未配置")
            return None

        # 构造完整 prompt（含 system_prompt）
        full_prompt = prompt
        if system_prompt:
            full_prompt = f"{system_prompt}\n\n{prompt}"

        # 调用 system.py 中的统一 LLM 函数
        from app.api.system import _call_llm as sys_call_llm
        return sys_call_llm(full_prompt, provider, llm_model, api_base, api_key)
    except Exception as e:
        print(f"AI 调用失败: {e}")
        import traceback
        traceback.print_exc()
        return None


# ==================== Rules 生成器 ====================

RULES_GENERATOR_SYSTEM = """你是一个测试管理工作规范专家，精通编写 OpenClaw Agent 的工作规范文件。

工作规范（Rule）是用于定义 OpenClaw Agent 身份角色和行为准则的 Markdown 配置文件。

请根据用户需求生成符合以下格式的规范内容：

# 规范标题

## 角色定义
描述该规范定义的 Agent 角色

## 职责范围
- 职责1
- 职责2

## 工作准则
### 准则1
具体描述...

### 准则2
具体描述...

## 行为规范
- 规范1
- 规范2

## 输出要求
- 输出1
- 输出2

请用中文输出，格式规范，内容专业。"""


@api_bp.route('/ai/generate-rule', methods=['POST'])
def generate_rule():
    """
    AI 生成 Rule

    请求体：
    {
        "description": "用户的需求描述，如：测试用例评审规范",
        "scope": "global/project/module",
        "name": "可选，rule 名称"
    }
    """
    data = request.get_json()

    if not data or not data.get('description'):
        return jsonify({'error': 'description 为必填项'}), 400

    description = data['description']
    scope = data.get('scope', 'global')

    # 生成 name
    name = data.get('name')
    if not name:
        # 从描述中提取关键词生成 name
        name = re.sub(r'[^a-zA-Z0-9\u4e00-\u9fa5]', '_', description)[:50]
        name = f"rule_{name.lower()}"

    # 调用 AI 生成
    prompt = f"""请为以下需求生成一个 OpenClaw 工作规范：

需求描述：{description}
适用范围：{scope}

请生成规范的完整内容，包括角色定义、职责范围、工作准则、行为规范等。"""

    content = call_llm(prompt, system_prompt=RULES_GENERATOR_SYSTEM)

    if not content:
        return jsonify({'error': 'AI 生成失败，请稍后重试'}), 500

    # 创建 Rule
    rule = Rule(
        name=name,
        display_name=description[:50],
        description=description,
        category='ai_generated',
        scope=scope,
        content_template=content,
    )
    db.session.add(rule)
    db.session.commit()

    return jsonify({
        'id': rule.id,
        'name': rule.name,
        'display_name': rule.display_name,
        'content': content,
        'message': 'Rule 生成成功'
    }), 201


# ==================== Skills 生成器 ====================

SKILLS_GENERATOR_SYSTEM = """你是一个 OpenClaw Agent 技能开发专家，精通编写 OpenClaw 技能配置文件。

技能（Skill）是 OpenClaw Agent 的能力扩展，通过触发短语激活，执行特定任务。

请根据用户需求生成符合以下格式的技能配置：

## 技能名称
[显示名称]

## 功能描述
[简要描述技能功能]

## 触发短语
- phrase1
- phrase2

## 技能模板
```
[技能的完整模板内容，支持变量替换]
```

## 使用场景
[何时使用该技能]

## 注意事项
[使用时需要注意的事项]

请用中文输出，格式规范，内容实用。"""


@api_bp.route('/ai/generate-skill', methods=['POST'])
def generate_skill():
    """
    AI 生成 Skill

    请求体：
    {
        "description": "用户的需求描述，如：代码审查技能",
        "scope": "global/project/module",
        "name": "可选，skill 名称"
    }
    """
    data = request.get_json()

    if not data or not data.get('description'):
        return jsonify({'error': 'description 为必填项'}), 400

    description = data['description']
    scope = data.get('scope', 'global')

    # 生成 name
    name = data.get('name')
    if not name:
        name = re.sub(r'[^a-zA-Z0-9\u4e00-\u9fa5]', '_', description)[:50]
        name = f"skill_{name.lower()}"

    # 调用 AI 生成
    prompt = f"""请为以下需求生成一个 OpenClaw 技能：

需求描述：{description}
适用范围：{scope}

请生成技能的完整配置，包括触发短语、技能模板、使用场景等。"""

    content = call_llm(prompt, system_prompt=SKILLS_GENERATOR_SYSTEM)

    if not content:
        return jsonify({'error': 'AI 生成失败，请稍后重试'}), 500

    # 创建 Skill
    skill = Skill(
        name=name,
        display_name=description[:50],
        description=description,
        category='ai_generated',
        scope=scope,
        template_content=content,
    )
    db.session.add(skill)
    db.session.commit()

    return jsonify({
        'id': skill.id,
        'name': skill.name,
        'display_name': skill.display_name,
        'content': content,
        'message': 'Skill 生成成功'
    }), 201


# ==================== 用例库 AI 对话 ====================

TESTCASE_GENERATOR_SYSTEM = """你是一个测试用例设计专家，精通编写各种类型的测试用例。

你管理一个测试用例库，可以：
1. 添加用例 - 根据需求生成新用例
2. 修改用例 - 优化现有用例
3. 删除用例 - 移除无效用例
4. 查询用例 - 根据条件查找用例
5. 生成脑图结构 - 组织用例的层级关系

测试用例格式：
{
    "case_id": "TC_001",
    "title": "用例标题",
    "priority": "P0/P1/P2/P3",
    "type": "functional/interface/performance/security",
    "preconditions": "前置条件",
    "steps": ["步骤1", "步骤2"],
    "expected_results": ["预期结果1", "预期结果2"],
    "tags": ["标签1", "标签2"]
}

请根据用户需求执行相应操作，并以 JSON 格式返回结果。"""


@api_bp.route('/ai/testcases/chat', methods=['POST'])
def testcases_ai_chat():
    """
    用例库 AI 对话

    请求体：
    {
        "library_id": 1,  // 用例库 ID
        "message": "用户消息",
        "context": {}  // 可选，上下文
    }
    """
    data = request.get_json()

    if not data or not data.get('library_id') or not data.get('message'):
        return jsonify({'error': 'library_id 和 message 为必填项'}), 400

    library_id = data['library_id']
    message = data['message']

    # 获取用例库
    library = TestCaseLibrary.query.get(library_id)
    if not library:
        return jsonify({'error': '用例库不存在'}), 404

    # 获取现有用例上下文
    existing_cases = [c.to_dict() for c in library.cases.limit(50).all()]

    prompt = f"""用例库：{library.name}
描述：{library.description}

现有用例数量：{library.cases.count()}
前50条用例：
{json.dumps(existing_cases, ensure_ascii=False, indent=2)}

用户消息：{message}

请分析用户需求，执行相应操作并返回结果。"""

    response = call_llm(prompt, system_prompt=TESTCASE_GENERATOR_SYSTEM)

    if not response:
        return jsonify({'error': 'AI 生成失败，请稍后重试'}), 500

    # 解析 AI 响应，尝试提取 JSON
    try:
        # 尝试从响应中提取 JSON
        json_match = re.search(r'\{[\s\S]*\}', response)
        if json_match:
            result = json.loads(json_match.group())
        else:
            result = {"response": response}
    except json.JSONDecodeError:
        result = {"response": response}

    # 如果 AI 返回了要添加/修改的用例，执行操作
    if 'actions' in result:
        actions = result['actions']
        for action in actions:
            if action.get('type') == 'add_case':
                case_data = action.get('data', {})
                case = TestCase(
                    library_id=library_id,
                    case_id=case_data.get('case_id', f"TC_{library.cases.count() + 1:03d}"),
                    title=case_data.get('title', '未命名用例'),
                    priority=case_data.get('priority', 'P2'),
                    type=case_data.get('type', 'functional'),
                    content=case_data.get('content', {}),
                    ai_generated=True,
                    ai_prompt=message,
                )
                db.session.add(case)

        db.session.commit()
        result['cases_updated'] = len(actions)

    return jsonify(result)


@api_bp.route('/ai/testcases/generate', methods=['POST'])
def generate_testcases():
    """
    批量生成测试用例

    请求体：
    {
        "library_id": 1,
        "requirement": "需求描述",
        "count": 10,  // 生成数量
        "type": "functional"  // 用例类型
    }
    """
    data = request.get_json()

    if not data or not data.get('library_id') or not data.get('requirement'):
        return jsonify({'error': 'library_id 和 requirement 为必填项'}), 400

    library_id = data['library_id']
    requirement = data['requirement']
    count = data.get('count', 10)
    case_type = data.get('type', 'functional')

    library = TestCaseLibrary.query.get(library_id)
    if not library:
        return jsonify({'error': '用例库不存在'}), 404

    prompt = f"""请为以下需求生成 {count} 个测试用例：

需求：{requirement}
用例类型：{case_type}

现有用例库中用例编号最大为：TC_{library.cases.count():03d}

请生成 {count} 个测试用例，返回 JSON 数组格式：
[
  {{
    "case_id": "TC_XXX",
    "title": "用例标题",
    "priority": "P0/P1/P2/P3",
    "preconditions": "前置条件",
    "steps": ["步骤1", "步骤2"],
    "expected_results": ["预期结果1", "预期结果2"],
    "tags": ["标签1"]
  }},
  ...
]"""

    response = call_llm(prompt, system_prompt=TESTCASE_GENERATOR_SYSTEM)

    if not response:
        return jsonify({'error': 'AI 生成失败，请稍后重试'}), 500

    # 解析响应，提取 JSON
    generated_cases = []
    try:
        json_match = re.search(r'\[[\s\S]*\]', response)
        if json_match:
            cases_data = json.loads(json_match.group())
            for case_data in cases_data:
                case = TestCase(
                    library_id=library_id,
                    case_id=case_data.get('case_id', f"TC_{library.cases.count() + 1:03d}"),
                    title=case_data.get('title', '未命名用例'),
                    priority=case_data.get('priority', 'P2'),
                    type=case_type,
                    content={
                        'preconditions': case_data.get('preconditions', ''),
                        'steps': case_data.get('steps', []),
                        'expected_results': case_data.get('expected_results', []),
                    },
                    tags=case_data.get('tags', []),
                    ai_generated=True,
                    ai_prompt=requirement,
                )
                db.session.add(case)
                generated_cases.append(case)

            db.session.commit()
    except json.JSONDecodeError as e:
        return jsonify({'error': f'解析 AI 响应失败: {str(e)}', 'raw': response}), 500

    return jsonify({
        'message': f'成功生成 {len(generated_cases)} 个用例',
        'cases': [c.to_dict() for c in generated_cases],
    }), 201
