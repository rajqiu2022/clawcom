import uuid
import logging
from datetime import datetime, date, time
from functools import wraps
from flask import request, jsonify
from app import db
from app.models import (OpenClawInstance, DailyReport, Project, Rule,
                        OpenClawRule, OpenClawSkill, Skill, ClawMessage,
                        generate_api_token, hash_token, _simple_encrypt)
from app.api import api_bp

logger = logging.getLogger(__name__)


def require_claw_token(f):
    """OpenClaw API Token 认证装饰器
    
    用于 OpenClaw 自身调用的接口（heartbeat、report、config）。
    验证 Authorization: Bearer <token> 是否匹配该 OpenClaw 的 token。
    """
    @wraps(f)
    def decorated(claw_id, *args, **kwargs):
        claw = OpenClawInstance.query.get_or_404(claw_id)

        auth_header = request.headers.get('Authorization', '')
        if not auth_header.startswith('Bearer '):
            return jsonify({'error': '缺少认证 Token'}), 401

        token = auth_header[7:]  # 去掉 "Bearer "
        if not claw.verify_token(token):
            return jsonify({'error': 'Token 无效或不匹配'}), 403

        return f(claw_id, claw=claw, *args, **kwargs)
    return decorated


@api_bp.route('/openclaws', methods=['GET'])
def list_openclaws():
    """获取所有 OpenClaw 列表（默认不含已删除的，?include_deleted=true 可查看）"""
    include_deleted = request.args.get('include_deleted', 'false').lower() == 'true'
    query = OpenClawInstance.query
    if not include_deleted:
        query = query.filter(OpenClawInstance.status != 'deleted')
    claws = query.order_by(OpenClawInstance.created_at.desc()).all()
    return jsonify([c.to_dict(brief=True) for c in claws])


@api_bp.route('/openclaws', methods=['POST'])
def create_openclaw():
    """注册新 OpenClaw 或通过旧 Token 恢复已归档的实例"""
    data = request.get_json()

    # === 模式1：通过旧 Token 恢复 ===
    restore_token = data.get('restore_token')
    if restore_token:
        # 在所有实例（含已删除的）中查找匹配的 Token
        for claw in OpenClawInstance.query.all():
            if claw.verify_token(restore_token):
                # 找到了，恢复
                claw.status = 'offline'
                claw.deleted_at = None
                # 允许更新部分字段
                for field in ['name', 'owner', 'role_title', 'responsibilities',
                              'project_name', 'module_name', 'connection_mode',
                              'report_schedule', 'web_system_url']:
                    if field in data:
                        setattr(claw, field, data[field])
                if 'project_id' in data and data['project_id']:
                    claw.project_id = int(data['project_id'])
                db.session.commit()

                result = claw.to_dict()
                result['restored'] = True
                result['api_token_preview'] = claw.get_token_preview()
                result['has_token'] = True
                # 返回已有的 skills 和 rules
                installed_skills = [s.skill.name for s in claw.skills if s.enabled and s.skill]
                installed_rules = [r.rule.name for r in OpenClawRule.query.filter_by(
                    openclaw_id=claw.id, enabled=True).all() if r.rule]
                result['auto_installed'] = {
                    'skills': installed_skills,
                    'rules': installed_rules,
                }
                return jsonify(result), 200

        return jsonify({'error': 'Token 无效，未找到匹配的 OpenClaw 实例'}), 404

    # === 模式2：全新注册 ===
    if not data.get('name') or not data.get('owner'):
        return jsonify({'error': '名称和所属用户为必填项'}), 400

    # 自动生成 claw_tag
    claw_tag = data.get('claw_tag', f"claw-{data['name']}")

    # 检查唯一性（排除已删除的同名标签，如果有则复用）
    existing = OpenClawInstance.query.filter_by(claw_tag=claw_tag).first()
    if existing:
        if existing.status == 'deleted':
            # 同标签的已删除实例，恢复它
            existing.status = 'offline'
            existing.deleted_at = None
            existing.name = data['name']
            existing.owner = data['owner']
            for field in ['role_title', 'responsibilities', 'project_name',
                          'module_name', 'connection_mode', 'report_schedule',
                          'web_system_url', 'soul_config', 'workflow_config']:
                if field in data:
                    setattr(existing, field, data[field])
            if 'project_id' in data and data['project_id']:
                existing.project_id = int(data['project_id'])
            # 生成新 Token
            raw_token = generate_api_token()
            existing.api_token_hash = hash_token(raw_token)
            existing.api_token_plain = _simple_encrypt(raw_token)
            db.session.commit()

            result = existing.to_dict()
            result['restored'] = True
            result['api_token'] = raw_token
            result['api_token_preview'] = existing.get_token_preview()
            result['has_token'] = True
            installed_skills = [s.skill.name for s in existing.skills if s.enabled and s.skill]
            installed_rules = [r.rule.name for r in OpenClawRule.query.filter_by(
                openclaw_id=existing.id, enabled=True).all() if r.rule]
            result['auto_installed'] = {
                'skills': installed_skills,
                'rules': installed_rules,
            }
            return jsonify(result), 200
        else:
            return jsonify({'error': f'标签 {claw_tag} 已存在'}), 409

    # 生成 API Token
    raw_token = generate_api_token()
    token_hash = hash_token(raw_token)
    token_encrypted = _simple_encrypt(raw_token)

    # 处理 project_id
    project_id = data.get('project_id')
    if project_id:
        project_id = int(project_id)

    claw = OpenClawInstance(
        name=data['name'],
        claw_tag=claw_tag,
        owner=data['owner'],
        role=data.get('role', 'test_member'),
        role_title=data.get('role_title'),
        responsibilities=data.get('responsibilities'),
        project_id=project_id,
        project_name=data.get('project_name'),
        module_name=data.get('module_name'),
        avatar=data.get('avatar'),
        soul_config=data.get('soul_config'),
        workflow_config=data.get('workflow_config'),
        report_schedule=data.get('report_schedule', '15:00,21:00'),
        connection_mode=data.get('connection_mode', 'sse'),
        web_system_url=data.get('web_system_url'),
        api_token_hash=token_hash,
        api_token_plain=token_encrypted,
    )
    db.session.add(claw)
    db.session.commit()

    # === 自动安装：标准包 + is_standard 双机制 ===

    # 1) 通过激活的标准包下发
    try:
        from app.models import StandardPack
        from app.api.packs import _apply_pack_to_claw
        active_packs = StandardPack.query.filter_by(is_active=True).all()
        for pack in active_packs:
            try:
                _apply_pack_to_claw(pack, claw)
            except Exception as pack_err:
                import logging
                logging.getLogger(__name__).warning(
                    'Pack %s apply failed for claw %d: %s', pack.name, claw.id, pack_err)
                db.session.rollback()
    except Exception:
        pass

    # 2) 兼容旧逻辑：is_standard=True 但不在任何标准包内的，也自动安装
    try:
        standard_skills = Skill.query.filter_by(is_standard=True).all()
        for skill in standard_skills:
            if skill.name == 'registration-skill':
                continue
            existing = OpenClawSkill.query.filter_by(
                openclaw_id=claw.id, skill_id=skill.id
            ).first()
            if not existing:
                db.session.add(OpenClawSkill(
                    openclaw_id=claw.id, skill_id=skill.id, enabled=True
                ))

        standard_rules = Rule.query.filter_by(is_standard=True).all()
        for rule in standard_rules:
            existing = OpenClawRule.query.filter_by(
                openclaw_id=claw.id, rule_id=rule.id
            ).first()
            if not existing:
                db.session.add(OpenClawRule(
                    openclaw_id=claw.id, rule_id=rule.id, enabled=True
                ))

        db.session.commit()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            'Standard install failed for claw %d: %s', claw.id, e)
        db.session.rollback()

    # === 下发初始化验证任务（不阻塞注册） ===
    try:
        from app.seed import create_init_tasks_for_claw
        init_task_count = create_init_tasks_for_claw(claw.id)
        db.session.commit()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            'Init tasks failed for claw %d: %s', claw.id, e)
        init_task_count = 0
        db.session.rollback()

    # 返回 Token 预览（不返回完整明文）
    result = claw.to_dict()
    result['api_token_preview'] = claw.get_token_preview()
    result['has_token'] = True
    # 收集实际安装结果
    installed_skills = [s.skill.name for s in claw.skills if s.enabled and s.skill]
    installed_rules = [r.rule.name for r in OpenClawRule.query.filter_by(
        openclaw_id=claw.id, enabled=True).all() if r.rule]
    result['auto_installed'] = {
        'skills': installed_skills,
        'rules': installed_rules,
        'packs': [p.name for p in active_packs],
    }
    result['init_tasks'] = init_task_count
    return jsonify(result), 201


@api_bp.route('/openclaws/<int:claw_id>', methods=['GET'])
def get_openclaw(claw_id):
    """获取 OpenClaw 详情"""
    claw = OpenClawInstance.query.get_or_404(claw_id)
    return jsonify(claw.to_dict())


@api_bp.route('/openclaws/<int:claw_id>', methods=['PUT'])
def update_openclaw(claw_id):
    """更新 OpenClaw 信息"""
    claw = OpenClawInstance.query.get_or_404(claw_id)
    data = request.get_json()

    updatable_fields = [
        'name', 'role', 'role_title', 'responsibilities', 'project_name',
        'module_name', 'avatar', 'soul_config', 'workflow_config',
        'report_schedule', 'connection_mode', 'web_system_url'
    ]
    for field in updatable_fields:
        if field in data:
            setattr(claw, field, data[field])

    # 处理 project_id
    if 'project_id' in data:
        claw.project_id = int(data['project_id']) if data['project_id'] else None

    db.session.commit()
    return jsonify(claw.to_dict())


@api_bp.route('/openclaws/<int:claw_id>/regenerate-token', methods=['POST'])
def regenerate_token(claw_id):
    """重新生成 API Token（旧 Token 立即失效）"""
    claw = OpenClawInstance.query.get_or_404(claw_id)

    raw_token = generate_api_token()
    claw.api_token_hash = hash_token(raw_token)
    claw.api_token_plain = _simple_encrypt(raw_token)
    db.session.commit()

    return jsonify({
        'id': claw.id,
        'name': claw.name,
        'api_token': raw_token,
        'api_token_preview': claw.get_token_preview(),
        'has_token': True,
        'message': '新 Token 已生成，旧 Token 已失效。',
    })


@api_bp.route('/openclaws/<int:claw_id>/token', methods=['GET'])
def get_claw_token(claw_id):
    """
    获取 OpenClaw 的完整 Token（用于弹窗显示和复制）
    注意：需要管理员权限或验证操作者身份
    """
    claw = OpenClawInstance.query.get_or_404(claw_id)

    return jsonify({
        'id': claw.id,
        'name': claw.name,
        'api_token': claw.get_token_plain(),
        'api_token_preview': claw.get_token_preview(),
    })


@api_bp.route('/openclaws/<int:claw_id>', methods=['DELETE'])
def delete_openclaw(claw_id):
    """软删除 OpenClaw（保留所有配置、记忆和关联数据，支持后续恢复）"""
    claw = OpenClawInstance.query.get_or_404(claw_id)

    claw.status = 'deleted'
    claw.deleted_at = datetime.utcnow()
    db.session.commit()

    return jsonify({
        'message': f'OpenClaw "{claw.name}" 已归档（数据已保留，可通过旧 Token 重新激活）',
        'claw_id': claw.id,
    })


@api_bp.route('/openclaws/<int:claw_id>/registration-skill', methods=['GET'])
def get_registration_skill_for_claw(claw_id):
    """生成该 OpenClaw 专属的注册 Skill（已填好 CLAW_ID、TOKEN、Hub 地址）

    管理员注册 OpenClaw 后，把这个链接发给用户，用户直接保存为 SKILL.md 即可使用。
    用法：curl http://hub:8088/api/v1/openclaws/5/registration-skill
    """
    from flask import Response
    claw = OpenClawInstance.query.get_or_404(claw_id)
    token = claw.get_token_plain()
    hub_url = 'http://your-hub-host:8088'

    # 获取已安装的标准 Skills 链接
    skill_links = []
    for s in claw.skills:
        if s.enabled and s.skill:
            skill_links.append(f"- [{s.skill.display_name}]({hub_url}/api/v1/skills/{s.skill.id}/raw)")

    # 获取已安装的标准 Rules 链接
    rule_links = []
    for r in OpenClawRule.query.filter_by(openclaw_id=claw_id, enabled=True).all():
        if r.rule:
            rule_links.append(f"- [{r.rule.display_name}]({hub_url}/api/v1/rules/{r.rule.id}/raw)")

    md = f"""# OpenClaw 注册配置 - {claw.name}

> 此文件由 Hub 自动生成，已配置好连接信息。将此文件保存到你的 OpenClaw Skills 目录即可完成接入。

---

## 连接信息

```
Hub 地址: {hub_url}
CLAW_ID: {claw.id}
HUB_API_TOKEN: {token}
OpenClaw 名称: {claw.name}
所属项目: {claw.project.name if claw.project else claw.project_name or '未指定'}
所属模块: {claw.module_name or '未指定'}
角色: {claw.role or 'test_member'}
日报时间: {claw.report_schedule or '15:00,21:00'}
```

---

## 快速开始

### 1. 验证连接

```bash
curl -H "Authorization: Bearer {token}" \\
  {hub_url}/api/v1/openclaws/{claw.id}/config
```

### 2. 全量初始化（拉取所有已分配的 Skills 和 Rules）

```bash
# 获取已分配的 Skills
curl -H "Authorization: Bearer {token}" \\
  {hub_url}/api/v1/openclaws/{claw.id}/assigned-skills

# 获取已分配的 Rules
curl -H "Authorization: Bearer {token}" \\
  {hub_url}/api/v1/openclaws/{claw.id}/assigned-rules
```

### 3. 建立通信（SSE 长连接）

```bash
curl -H "Authorization: Bearer {token}" \\
  -H "Accept: text/event-stream" \\
  {hub_url}/api/openclaws/{claw.id}/events
```

### 4. 心跳保活（每 30 秒）

```bash
curl -X POST -H "Authorization: Bearer {token}" \\
  {hub_url}/api/v1/openclaws/{claw.id}/heartbeat
```

---

## 已安装的 Skills（可通过链接直接获取内容）

{chr(10).join(skill_links) if skill_links else '暂无'}

## 已安装的 Rules

{chr(10).join(rule_links) if rule_links else '暂无'}

---

## 增量模式（按需安装更多）

### 浏览 Skills 市场

```bash
curl -H "Authorization: Bearer {token}" \\
  {hub_url}/api/v1/skills
```

### 安装指定 Skill

```bash
curl -X POST -H "Authorization: Bearer {token}" \\
  -H "Content-Type: application/json" \\
  -d '{{"skill_id": 3}}' \\
  {hub_url}/api/v1/openclaws/{claw.id}/skills
```

### 浏览并安装 Rules

```bash
# 浏览
curl -H "Authorization: Bearer {token}" {hub_url}/api/v1/rules

# 批量安装
curl -X POST -H "Authorization: Bearer {token}" \\
  -H "Content-Type: application/json" \\
  -d '{{"rule_ids": [1, 3, 5]}}' \\
  {hub_url}/api/v1/openclaws/{claw.id}/rules
```

---

## 日常使用

- **提交日报**：`POST /api/v1/openclaws/{claw.id}/report`
- **发送消息**：`POST /api/openclaws/{claw.id}/messages`
- **获取消息**：`GET /api/openclaws/{claw.id}/messages`
- **同步更新**：每小时调用 `GET /assigned-skills` 和 `GET /assigned-rules`

---

## 触发词

- "连接 Hub"
- "同步配置"
- "安装 Skill"
- "查看市场"
- "提交日报"
"""

    fmt = request.args.get('format', 'markdown')
    if fmt == 'json':
        return jsonify({
            'claw_id': claw.id,
            'claw_name': claw.name,
            'hub_url': hub_url,
            'api_token': token,
            'skill_content': md,
            'skill_links': [f"{hub_url}/api/v1/skills/{s.skill.id}/raw" for s in claw.skills if s.enabled and s.skill],
            'rule_links': [f"{hub_url}/api/v1/rules/{r.rule.id}/raw" for r in OpenClawRule.query.filter_by(openclaw_id=claw_id, enabled=True).all() if r.rule],
        })

    return Response(md, mimetype='text/markdown; charset=utf-8',
                    headers={'Content-Disposition': f'inline; filename="registration-claw-{claw.id}.md"'})


@api_bp.route('/openclaws/<int:claw_id>/config', methods=['GET'])
@require_claw_token
def get_openclaw_config(claw_id, claw=None):
    """OpenClaw 拉取自己的配置（需 Token 认证）"""
    # 获取已安装的 Skills（跳过已删除的）
    installed_skills = [s.skill.to_dict() for s in claw.skills if s.enabled and s.skill]

    # 获取已安装的 Rules（跳过已删除的）
    installed_rules = []
    for r in OpenClawRule.query.filter_by(openclaw_id=claw_id, enabled=True).all():
        if not r.rule:
            continue
        installed_rules.append({
            'id': r.rule.id,
            'name': r.rule.name,
            'display_name': r.rule.display_name,
            'description': r.rule.description,
            'content_template': r.rule.content_template,
        })

    return jsonify({
        'id': claw.id,
        'name': claw.name,
        'claw_tag': claw.claw_tag,
        'project_name': claw.project.name if claw.project else claw.project_name,
        'module_name': claw.module_name,
        'report_schedule': claw.report_schedule,
        'web_system_url': claw.web_system_url,
        'skills': installed_skills,
        'rules': installed_rules,
    })


@api_bp.route('/openclaws/<int:claw_id>/heartbeat', methods=['POST'])
@require_claw_token
def heartbeat(claw_id, claw=None):
    """心跳上报（轮询模式核心接口）

    返回：当前状态 + 消息 + 待办统计
    OpenClaw 每 30 秒调用一次，根据返回决定下一步动作
    """
    from app.models import ClawTodo, ClawTodoLog

    claw.status = 'online'
    claw.last_heartbeat = datetime.utcnow()
    db.session.commit()

    today = date.today()

    # 消息统计
    pending_messages = ClawMessage.query.filter_by(
        claw_id=claw_id, status='pending').count()
    urgent = ClawMessage.query.filter_by(
        claw_id=claw_id, status='pending', msg_type='sync_config'
    ).first()

    # 待办统计
    all_todos = ClawTodo.query.filter_by(openclaw_id=claw_id, enabled=True).all()
    today_logs = {l.todo_id: l for l in ClawTodoLog.query.filter_by(
        openclaw_id=claw_id, log_date=today).all()}

    # 分类统计
    interrupt_pending = []  # 需要立即中断执行的
    todos_pending = 0       # 今日待完成总数
    todos_done = 0
    init_pending = 0        # 初始化任务未完成

    for t in all_todos:
        log = today_logs.get(t.id)
        is_done = log and log.status == 'completed'

        # 判断今天是否需要执行
        need_today = False
        if t.schedule_type == 'once':
            need_today = not is_done
        elif t.schedule_type == 'daily':
            need_today = True
        elif t.schedule_type == 'weekly' and t.schedule_day:
            need_today = today.isoweekday() == t.schedule_day
        elif t.schedule_type == 'monthly' and t.schedule_day:
            need_today = today.day == t.schedule_day

        if need_today:
            if is_done:
                todos_done += 1
            else:
                todos_pending += 1
                if t.task_category == 'init':
                    init_pending += 1
                if t.urgency_level == 'interrupt' and t.schedule_time:
                    interrupt_pending.append({
                        'id': t.id,
                        'title': t.title,
                        'time': t.schedule_time,
                    })

    return jsonify({
        'status': 'ok',
        'pending_messages': pending_messages,
        'has_urgent': urgent is not None,
        'todos': {
            'pending': todos_pending,
            'done': todos_done,
            'init_pending': init_pending,
            'interrupt': interrupt_pending,
        },
        'server_time': datetime.utcnow().isoformat(),
    })


@api_bp.route('/openclaws/<int:claw_id>/report', methods=['POST'])
@require_claw_token
def submit_report(claw_id, claw=None):
    """工作日报上报（需 Token 认证）"""
    data = request.get_json()

    if not data:
        return jsonify({'error': '上报数据为空'}), 400

    # 解析日期和时间
    report_date = date.fromisoformat(data.get('report_date',
                                              date.today().isoformat()))
    report_time_str = data.get('report_time',
                               datetime.now().strftime('%H:%M'))
    hour, minute = map(int, report_time_str.split(':'))
    report_time = time(hour, minute)

    # 查找或创建日报（同一时间段去重）
    report = DailyReport.query.filter_by(
        openclaw_id=claw_id,
        report_date=report_date,
        report_time=report_time,
    ).first()

    if report:
        # 更新已有日报
        report.tasks_completed = data.get('tasks_completed')
        report.knowledge_recorded = data.get('knowledge_recorded')
        report.experience_shared = data.get('experience_shared')
        report.knowledge_learned = data.get('knowledge_learned')
        report.ai_summary = data.get('ai_summary')
    else:
        report = DailyReport(
            openclaw_id=claw_id,
            report_date=report_date,
            report_time=report_time,
            tasks_completed=data.get('tasks_completed'),
            knowledge_recorded=data.get('knowledge_recorded'),
            experience_shared=data.get('experience_shared'),
            knowledge_learned=data.get('knowledge_learned'),
            ai_summary=data.get('ai_summary'),
        )
        db.session.add(report)

    db.session.commit()
    return jsonify(report.to_dict()), 201


@api_bp.route('/openclaws/<int:claw_id>/reports', methods=['GET'])
def list_reports(claw_id):
    """获取日报历史"""
    claw = OpenClawInstance.query.get_or_404(claw_id)

    # 支持日期筛选
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')

    query = DailyReport.query.filter_by(openclaw_id=claw_id)
    if start_date:
        query = query.filter(DailyReport.report_date >= start_date)
    if end_date:
        query = query.filter(DailyReport.report_date <= end_date)

    reports = query.order_by(
        DailyReport.report_date.desc(),
        DailyReport.report_time.desc()
    ).limit(50).all()

    return jsonify([r.to_dict() for r in reports])


@api_bp.route('/openclaws/<int:claw_id>/assigned-skills', methods=['GET'])
@require_claw_token
def get_assigned_skills(claw_id, claw=None):
    """OpenClaw 获取分配给自己的 Skills（需 Token 认证）"""
    skills = []
    for s in claw.skills:
        if s.enabled and s.skill:
            skills.append({
                'id': s.skill.id,
                'name': s.skill.name,
                'display_name': s.skill.display_name,
                'description': s.skill.description,
                'template_content': s.skill.template_content,
                'trigger_phrase': s.skill.trigger_phrase,
            })
    return jsonify({'skills': skills})


@api_bp.route('/openclaws/<int:claw_id>/assigned-rules', methods=['GET'])
def get_assigned_rules(claw_id):
    """获取已分配的 Rules（Web 端和 OpenClaw 均可调用）"""
    OpenClawInstance.query.get_or_404(claw_id)
    rules = []
    for r in OpenClawRule.query.filter_by(openclaw_id=claw_id, enabled=True).all():
        if r.rule:
            rules.append({
                'id': r.rule.id,
                'name': r.rule.name,
                'display_name': r.rule.display_name,
                'description': r.rule.description,
                'content_template': r.rule.content_template,
            })
    return jsonify({'rules': rules})


@api_bp.route('/openclaws/<int:claw_id>/dispatch', methods=['POST'])
@require_claw_token
def dispatch_task_to_claw(claw_id, claw=None):
    """
    向 OpenClaw 下发任务（通过 WebSocket 推送）

    请求体：
    {
        "task_type": "sync_skills" | "sync_rules" | "get_status" | ...",
        "payload": {...}  // 任务参数
    }
    """
    data = request.get_json()
    if not data or not data.get('task_type'):
        return jsonify({'error': 'task_type 为必填项'}), 400

    task_id = f"task_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}"
    task_type = data.get('task_type')
    payload = data.get('payload', {})

    # 构造任务数据
    task_data = {
        'task_id': task_id,
        'task_type': task_type,
        'payload': payload,
    }

    # 通过 WebSocket 推送给 OpenClaw
    from app.api.gateway_ws import push_event, conn_manager

    # 找到该 claw 对应的 session
    session_id = None
    for sid, info in conn_manager.list_connections().items():
        if info.get('claw_id') == claw_id:
            session_id = sid
            break

    if session_id:
        push_event(session_id, 'task', task_data)
        logger.info(f"任务已推送给 OpenClaw {claw_id}: {task_type}")
        return jsonify({
            'message': '任务已推送',
            'task_id': task_id,
            'task_type': task_type,
            'status': 'pushed',
        })
    else:
        # OpenClaw 不在线，存入待处理队列（简化处理，后续可扩展）
        logger.warning(f"OpenClaw {claw_id} 不在线，任务已忽略")
        return jsonify({
            'message': 'OpenClaw 不在线，任务推送失败',
            'task_id': task_id,
            'task_type': task_type,
            'status': 'offline',
        }), 503


@api_bp.route('/openclaws/<int:claw_id>/report-schedule', methods=['PUT'])
def update_report_schedule(claw_id):
    """修改上报频率配置"""
    claw = OpenClawInstance.query.get_or_404(claw_id)
    data = request.get_json()

    if 'report_schedule' not in data:
        return jsonify({'error': 'report_schedule 为必填项'}), 400

    claw.report_schedule = data['report_schedule']
    db.session.commit()
    return jsonify({
        'id': claw.id,
        'name': claw.name,
        'report_schedule': claw.report_schedule,
    })


# ============== OpenClaw 消息接口 ==============

@api_bp.route('/openclaws/<int:claw_id>/messages', methods=['GET'])
@require_claw_token
def get_claw_messages(claw_id, claw=None):
    """
    OpenClaw 获取发给自己的消息（需 Token 认证）
    用于轮询模式的客户端
    """
    # 获取未送达的消息
    pending = ClawMessage.query.filter_by(
        claw_id=claw_id,
        status='pending'
    ).order_by(ClawMessage.created_at.asc()).all()

    # 将待送达消息标记为已送达
    for msg in pending:
        msg.status = 'delivered'
        msg.delivered_at = datetime.utcnow()
    db.session.commit()

    return jsonify({
        'messages': [m.to_dict() for m in pending],
        'count': len(pending),
    })


@api_bp.route('/openclaws/<int:claw_id>/messages/<int:msg_id>/read', methods=['PUT'])
@require_claw_token
def mark_claw_message_read(claw_id, msg_id, claw=None):
    """标记消息为已读"""
    msg = ClawMessage.query.get_or_404(msg_id)

    if msg.claw_id != claw_id:
        return jsonify({'error': '无权操作'}), 403

    msg.status = 'read'
    msg.read_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'message': '已标记为已读'})
