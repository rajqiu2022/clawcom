from flask import request, jsonify, session
from datetime import datetime
from app import db
from app.models import Skill, OpenClawSkill, OpenClawInstance, Rule, User


def _get_current_user():
    """获取当前登录用户"""
    uid = session.get('user_id')
    if not uid:
        return None
    return User.query.get(uid)


def _can_edit(user, resource):
    """检查用户是否有权编辑资源（skill/rule）
    super_admin: 可以编辑一切
    admin（项目管理员）: 只能编辑自己负责项目内的 + 自己创建的
    user: 只能编辑自己创建的
    """
    if not user:
        return False
    if user.role == 'super_admin':
        return True
    created_by = getattr(resource, 'created_by', None) or ''
    if created_by == user.username:
        return True
    if user.role == 'admin':
        # 项目管理员：检查资源的适用项目是否在自己管理的项目中
        managed = user.managed_projects or []
        if not managed:
            return False
        res_projects = getattr(resource, 'applicable_projects', None) or []
        # 全局/无项目的资源，项目管理员不能改（除非自己创建的）
        if not res_projects:
            return False
        # 资源的适用项目和管理员的项目有交集
        if set(res_projects) & set(managed):
            return True
    return False
from app.api import api_bp


@api_bp.route('/skills', methods=['GET'])
def list_skills():
    """获取 Skills 列表（scope=admin 的仅管理员可见）"""
    category_filter = request.args.get('category')
    query = Skill.query
    if category_filter:
        query = query.filter(Skill.category == category_filter)

    # scope=admin 的 Skill 仅超级管理员和管理员可见
    user = _get_current_user()
    if not user or user.role not in ('super_admin', 'admin'):
        query = query.filter(Skill.scope != 'admin')

    skills = query.order_by(Skill.created_at.desc()).all()
    result = []
    for s in skills:
        d = s.to_dict()
        # 查出正在使用的 OpenClaw 名单
        users = (db.session.query(OpenClawInstance.name)
                 .join(OpenClawSkill)
                 .filter(OpenClawSkill.skill_id == s.id,
                         OpenClawSkill.enabled == True)
                 .all())
        d['used_by'] = [u[0] for u in users]
        d['used_by_count'] = len(d['used_by'])
        result.append(d)
    return jsonify(result)


@api_bp.route('/skills', methods=['POST'])
def create_skill():
    """创建新 Skill"""
    data = request.get_json()
    if not data or not data.get('name') or not data.get('display_name'):
        return jsonify({'error': '标识名和显示名称为必填项'}), 400

    if Skill.query.filter_by(name=data['name']).first():
        return jsonify({'error': f'Skill "{data["name"]}" 已存在'}), 409

    skill = Skill(
        name=data['name'],
        display_name=data['display_name'],
        description=data.get('description'),
        category=data.get('category', 'custom'),
        trigger_phrase=data.get('trigger_phrase'),
        template_content=data.get('template_content'),
        scope=data.get('scope', 'global'),
        applicable_projects=data.get('applicable_projects'),
        applicable_modules=data.get('applicable_modules'),
        created_by=data.get('created_by', 'system'),
        is_standard=bool(data.get('is_standard', False)),
    )

    # 进化技能额外字段
    if data.get('category') == 'evolved':
        skill.evolve_source = data.get('evolve_source')
        skill.success_rate = data.get('success_rate', 0)
        skill.total_runs = data.get('total_runs', 0)
        skill.error_count = data.get('error_count', 0)
        skill.last_evolved_at = datetime.utcnow()
        skill.evolve_history = [{
            'action': 'created',
            'timestamp': datetime.utcnow().isoformat(),
            'detail': f'由 OpenSpace 从 {data.get("evolve_source", "未知")} 进化而来',
        }]

    db.session.add(skill)
    db.session.commit()
    return jsonify(skill.to_dict()), 201


@api_bp.route('/skills/batch-evolved', methods=['POST'])
def batch_import_evolved():
    """批量导入 OpenSpace 自动进化的技能"""
    data = request.get_json()
    skills = data.get('skills', [])
    source = data.get('source', 'OpenSpace')

    if not skills:
        return jsonify({'error': 'skills 数组不能为空'}), 400

    created = 0
    updated = 0

    for s_data in skills:
        if not s_data.get('name') or not s_data.get('display_name'):
            continue

        existing = Skill.query.filter_by(name=s_data['name']).first()
        if existing:
            # 更新已有技能
            if s_data.get('description'):
                existing.description = s_data['description']
            if s_data.get('template_content'):
                existing.template_content = s_data['template_content']
            if s_data.get('trigger_phrase'):
                existing.trigger_phrase = s_data['trigger_phrase']
            existing.success_rate = s_data.get('success_rate', existing.success_rate)
            existing.total_runs = s_data.get('total_runs', existing.total_runs)
            existing.error_count = s_data.get('error_count', existing.error_count)
            existing.last_evolved_at = datetime.utcnow()
            history = existing.evolve_history or []
            history.append({
                'action': 'evolved',
                'timestamp': datetime.utcnow().isoformat(),
                'detail': f'OpenSpace 自动进化',
            })
            existing.evolve_history = history[-20:]  # 保留最近20条
            updated += 1
        else:
            # 创建新技能
            skill = Skill(
                name=s_data['name'],
                display_name=s_data['display_name'],
                description=s_data.get('description'),
                category='evolved',
                trigger_phrase=s_data.get('trigger_phrase'),
                template_content=s_data.get('template_content'),
                scope=s_data.get('scope', 'global'),
                evolve_source=source,
                success_rate=s_data.get('success_rate', 0),
                total_runs=s_data.get('total_runs', 0),
                error_count=s_data.get('error_count', 0),
                last_evolved_at=datetime.utcnow(),
                evolve_history=[{
                    'action': 'created',
                    'timestamp': datetime.utcnow().isoformat(),
                    'detail': f'由 OpenSpace 从 {source} 进化而来',
                }],
            )
            db.session.add(skill)
            created += 1

    db.session.commit()

    return jsonify({
        'message': f'导入完成：新建 {created} 个，更新 {updated} 个',
        'created': created,
        'updated': updated,
    })


@api_bp.route('/skills/<int:skill_id>', methods=['PUT'])
def update_skill(skill_id):
    """更新 Skill"""
    skill = Skill.query.get_or_404(skill_id)

    user = _get_current_user()
    if not _can_edit(user, skill):
        return jsonify({'error': '无权修改此 Skill，只有超级管理员、管理员或提交人可编辑'}), 403

    data = request.get_json()

    for field in ['display_name', 'description', 'trigger_phrase',
                  'template_content', 'category', 'scope',
                  'applicable_projects', 'applicable_modules']:
        if field in data:
            setattr(skill, field, data[field])

    if 'is_standard' in data:
        skill.is_standard = bool(data['is_standard'])

    # review_status 只有 admin 以上可改
    if 'review_status' in data and data['review_status'] in ('approved', 'pending', 'rejected'):
        if user and user.role in ('super_admin', 'admin'):
            skill.review_status = data['review_status']
        else:
            return jsonify({'error': '只有管理员可以修改审核状态'}), 403

    db.session.commit()
    return jsonify(skill.to_dict())


@api_bp.route('/skills/<int:skill_id>', methods=['DELETE'])
def delete_skill(skill_id):
    """删除 Skill（管理员权限）

    删除后，所有安装过此 Skill 的 OpenClaw 的关联记录会被级联删除，
    OpenClaw 自行在下次同步时感知到 Skill 已不存在。
    """
    skill = Skill.query.get_or_404(skill_id)

    user = _get_current_user()
    if not user or user.role not in ('super_admin', 'admin'):
        return jsonify({'error': '只有管理员可以删除 Skill'}), 403

    # 级联删除所有安装关联（OpenClawSkill）
    OpenClawSkill.query.filter_by(skill_id=skill_id).delete()

    # 删除 Skill 本体
    db.session.delete(skill)
    db.session.commit()
    return jsonify({'message': f'Skill "{skill.name}" 已删除'})


def _notify_claw_sync(claw_id, action, detail):
    """给 OpenClaw 发送配置同步通知（轮询模式下次心跳会收到）"""
    from app.models import ClawMessage
    msg = ClawMessage(
        claw_id=claw_id,
        sender_name='Hub',
        content=f'[{action}] {detail}，请同步配置',
        msg_type='sync_config',
        direction='to_claw',
        status='pending',
    )
    db.session.add(msg)


@api_bp.route('/openclaws/<int:claw_id>/skills', methods=['POST'])
def install_skill(claw_id):
    """为 OpenClaw 安装 Skill

    1. 写入 OpenClawSkill 关联记录
    2. 下发待办任务（interrupt 级别），OpenClaw 心跳时感知并拉取安装
    """
    claw = OpenClawInstance.query.get_or_404(claw_id)
    data = request.get_json()
    skill_id = data.get('skill_id')

    if not skill_id:
        return jsonify({'error': 'skill_id 为必填项'}), 400

    skill = Skill.query.get_or_404(skill_id)

    existing = OpenClawSkill.query.filter_by(
        openclaw_id=claw_id, skill_id=skill_id
    ).first()

    if existing:
        existing.enabled = True
    else:
        existing = OpenClawSkill(
            openclaw_id=claw_id, skill_id=skill_id, enabled=True
        )
        db.session.add(existing)

    # 下发安装待办（interrupt 级别，心跳时立即感知）
    from app.models import ClawTodo
    todo = ClawTodo(
        openclaw_id=claw_id,
        title=f'安装 Skill：{skill.display_name}',
        description=(
            f'Hub 已分配 Skill「{skill.display_name}」(id={skill.id})，请拉取并安装到本地。\n\n'
            f'执行步骤：\n'
            f'1. GET /api/v1/openclaws/{claw_id}/assigned-skills 获取最新 Skills 列表\n'
            f'2. 找到 name="{skill.name}" 的 Skill\n'
            f'3. GET /api/v1/skills/{skill.id}/files 获取文档包文件清单\n'
            f'4. 逐个拉取文件写入 ~/.qclaw/skills/{skill.name}/\n'
            f'5. 完成后上报 POST /todos/{{todo_id}}/complete'
        ),
        schedule_type='once',
        urgency_level='interrupt',
        priority='P0',
        task_category='routine',
        verification_target=f'install-skill:{skill.name}',
        enabled=True,
        created_by='hub',
    )
    db.session.add(todo)

    _notify_claw_sync(claw_id, '分配 Skill', skill.display_name)
    db.session.commit()
    return jsonify({'message': f'已为 {claw.name} 分配 {skill.display_name}'}), 201


@api_bp.route('/openclaws/<int:claw_id>/skills/<int:skill_id>',
              methods=['DELETE'])
def uninstall_skill(claw_id, skill_id):
    """移除 Skill"""
    link = OpenClawSkill.query.filter_by(
        openclaw_id=claw_id, skill_id=skill_id
    ).first_or_404()

    skill_name = link.skill.display_name if link.skill else str(skill_id)
    skill_ident = link.skill.name if link.skill else str(skill_id)
    link.enabled = False

    # 下发卸载待办
    from app.models import ClawTodo
    todo = ClawTodo(
        openclaw_id=claw_id,
        title=f'卸载 Skill：{skill_name}',
        description=f'Hub 已移除 Skill「{skill_name}」，请从本地删除 ~/.qclaw/skills/{skill_ident}/ 目录。',
        schedule_type='once',
        urgency_level='flexible',
        priority='P1',
        task_category='routine',
        verification_target=f'uninstall-skill:{skill_ident}',
        enabled=True,
        created_by='hub',
    )
    db.session.add(todo)

    _notify_claw_sync(claw_id, '移除 Skill', skill_name)
    db.session.commit()
    return jsonify({'message': '已移除'})


# ==================== 标准化 Skills 管理 ====================

@api_bp.route('/skills/standard', methods=['GET'])
def list_standard_skills():
    """获取所有标准化 Skills"""
    skills = Skill.query.filter_by(is_standard=True).order_by(Skill.name).all()
    return jsonify([s.to_dict() for s in skills])


@api_bp.route('/skills/standard', methods=['PUT'])
def batch_update_standard_skills():
    """
    批量更新 Skills 的标准化标记

    请求体：
    {
        "skill_ids": [1, 2, 3],  // 要标记为标准化的 skill_ids
        "standard": true         // true=标记为标准化，false=取消标准化
    }
    """
    data = request.get_json()
    if not data or 'skill_ids' not in data:
        return jsonify({'error': 'skill_ids 为必填项'}), 400

    skill_ids = data['skill_ids']
    is_standard = data.get('standard', True)

    updated = 0
    for skill_id in skill_ids:
        skill = Skill.query.get(skill_id)
        if skill:
            skill.is_standard = is_standard
            updated += 1

    db.session.commit()

    action = "已标记为标准化" if is_standard else "已取消标准化"
    return jsonify({
        'message': f'{updated} 个 Skills {action}',
        'updated': updated,
    })


@api_bp.route('/skills/registration-skill', methods=['GET'])
def get_registration_skill():
    """
    获取注册 Skill（注册时使用的标准化 Skill）
    注册 Skill 包含：
    1. 注册到 Hub 的 API 调用
    2. 需要安装的标准化 Rules 列表
    3. 需要安装的标准化 Skills 列表
    """
    # 查找注册 Skill（名称为 registration-skill）
    skill = Skill.query.filter_by(name='registration-skill').first()

    if not skill:
        return jsonify({'error': '注册 Skill 未找到'}), 404

    # 获取标准化 Rules
    standard_rules = Rule.query.filter_by(is_standard=True).all()

    # 获取标准化 Skills（排除注册 Skill 本身）
    standard_skills = Skill.query.filter(
        Skill.is_standard == True,
        Skill.name != 'registration-skill'
    ).all()

    result = skill.to_dict()
    result['standard_rules'] = [r.to_dict() for r in standard_rules]
    result['standard_skills'] = [s.to_dict() for s in standard_skills]
    result['standard_rule_ids'] = [r.id for r in standard_rules]
    result['standard_skill_ids'] = [s.id for s in standard_skills]

    return jsonify(result)


@api_bp.route('/skills/registration-skill', methods=['POST'])
def create_registration_skill():
    """
    创建/更新注册 Skill 模板

    请求体：
    {
        "display_name": "注册技能",
        "description": "标准化注册流程，包含 Hub 注册、Rules/Skills 安装",
        "template_content": "...",  // 注册流程的具体执行内容
    }
    """
    data = request.get_json()

    # 查找或创建注册 Skill
    skill = Skill.query.filter_by(name='registration-skill').first()

    if skill:
        # 更新已有
        for field in ['display_name', 'description', 'template_content']:
            if field in data:
                setattr(skill, field, data[field])
    else:
        # 创建新的
        skill = Skill(
            name='registration-skill',
            display_name=data.get('display_name', '注册技能'),
            description=data.get('description', '标准化注册流程'),
            template_content=data.get('template_content', ''),
            category='standard',
            is_standard=True,
        )
        db.session.add(skill)

    db.session.commit()
    return jsonify(skill.to_dict()), 201


# ==================== Skill/Rule 原始内容链接 ====================

from flask import Response

@api_bp.route('/skills/<int:skill_id>/raw', methods=['GET'])
def get_skill_raw(skill_id):
    """获取 Skill 原始内容（Markdown），可通过链接直接拉取

    用法：curl http://hub:8088/api/v1/skills/3/raw
    不需要登录，公开访问（方便 OpenClaw 拉取）
    """
    skill = Skill.query.get_or_404(skill_id)
    content = skill.template_content or ''
    md = f"# {skill.display_name}\n\n"
    if skill.description:
        md += f"> {skill.description}\n\n"
    if skill.trigger_phrase:
        md += f"**触发短语**：{skill.trigger_phrase}\n\n---\n\n"
    md += content
    return Response(md, mimetype='text/markdown; charset=utf-8',
                    headers={'Content-Disposition': f'inline; filename="skill-{skill.id}.md"'})


@api_bp.route('/rules/<int:rule_id>/raw', methods=['GET'])
def get_rule_raw(rule_id):
    """获取 Rule 原始内容（Markdown）"""
    from app.models import Rule
    rule = Rule.query.get_or_404(rule_id)
    content = rule.content_template or ''
    md = f"# {rule.display_name}\n\n"
    if rule.description:
        md += f"> {rule.description}\n\n---\n\n"
    md += content
    return Response(md, mimetype='text/markdown; charset=utf-8',
                    headers={'Content-Disposition': f'inline; filename="rule-{rule.id}.md"'})


# ==================== Skill 文档包 API（MySQL 存储） ====================

from app.models import SkillFile
import os

HUB_STORE_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'hub-store')


def _sync_to_disk(skill, filename, content):
    """同步写磁盘缓存（可选）"""
    try:
        pack_dir = os.path.join(HUB_STORE_ROOT, 'skills', skill.name)
        os.makedirs(pack_dir, exist_ok=True)
        with open(os.path.join(pack_dir, filename), 'w', encoding='utf-8') as f:
            f.write(content or '')
    except Exception:
        pass


def _remove_from_disk(skill, filename):
    """从磁盘缓存删除"""
    try:
        fpath = os.path.join(HUB_STORE_ROOT, 'skills', skill.name, filename)
        if os.path.isfile(fpath):
            os.remove(fpath)
    except Exception:
        pass


@api_bp.route('/skills/<int:skill_id>/files', methods=['GET'])
def list_skill_files(skill_id):
    """列出 Skill 文档包所有文件"""
    skill = Skill.query.get_or_404(skill_id)
    files = SkillFile.query.filter_by(skill_id=skill_id).order_by(SkillFile.filename).all()
    return jsonify({
        'skill_id': skill_id,
        'skill_name': skill.name,
        'files': [f.to_dict() for f in files],
    })


@api_bp.route('/skills/<int:skill_id>/files/<path:filename>', methods=['GET'])
def get_skill_file(skill_id, filename):
    """获取文档包某个文件内容"""
    Skill.query.get_or_404(skill_id)
    sf = SkillFile.query.filter_by(skill_id=skill_id, filename=filename).first()
    if not sf:
        return jsonify({'error': f'文件 {filename} 不存在'}), 404

    fmt = request.args.get('format', 'raw')
    if fmt == 'json':
        return jsonify(sf.to_dict())

    return Response(sf.content or '', mimetype='text/markdown; charset=utf-8',
                    headers={'Content-Disposition': f'inline; filename="{filename}"'})


@api_bp.route('/skills/<int:skill_id>/files/<path:filename>', methods=['PUT'])
def save_skill_file(skill_id, filename):
    """保存/更新文档包文件（存 MySQL + 同步磁盘缓存）"""
    skill = Skill.query.get_or_404(skill_id)
    data = request.get_json()
    content = data.get('content', '')
    description = data.get('description')

    sf = SkillFile.query.filter_by(skill_id=skill_id, filename=filename).first()
    if sf:
        sf.content = content
        if description is not None:
            sf.description = description
    else:
        sf = SkillFile(skill_id=skill_id, filename=filename, content=content,
                       description=description,
                       file_type='markdown' if filename.endswith('.md') else 'text')
        db.session.add(sf)

    # SKILL.md 同步到 template_content
    if filename == 'SKILL.md':
        skill.template_content = content
        skill.pack_path = f'hub-store/skills/{skill.name}'

    # 更新文件清单
    db.session.flush()
    all_files = SkillFile.query.filter_by(skill_id=skill_id).all()
    skill.files = [{'name': f.filename, 'size': len(f.content or '')} for f in all_files]
    db.session.commit()

    # 同步磁盘缓存
    _sync_to_disk(skill, filename, content)

    return jsonify({'message': f'{filename} 已保存', 'file': sf.to_dict()})


@api_bp.route('/skills/<int:skill_id>/files/<path:filename>', methods=['DELETE'])
def delete_skill_file(skill_id, filename):
    """删除文档包文件"""
    skill = Skill.query.get_or_404(skill_id)

    if filename == 'SKILL.md':
        return jsonify({'error': '不能删除主文件 SKILL.md'}), 400

    sf = SkillFile.query.filter_by(skill_id=skill_id, filename=filename).first()
    if sf:
        db.session.delete(sf)
        # 更新清单
        remaining = SkillFile.query.filter_by(skill_id=skill_id).filter(
            SkillFile.filename != filename).all()
        skill.files = [{'name': f.filename, 'size': len(f.content or '')} for f in remaining]
        db.session.commit()
        _remove_from_disk(skill, filename)

    return jsonify({'message': f'{filename} 已删除'})


@api_bp.route('/skills/<int:skill_id>/pack', methods=['GET'])
def download_skill_pack(skill_id):
    """打包下载整个文档包（ZIP，从 MySQL 读取）"""
    import zipfile, io
    skill = Skill.query.get_or_404(skill_id)
    files = SkillFile.query.filter_by(skill_id=skill_id).all()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.writestr(f'{skill.name}/{f.filename}', f.content or '')

    buf.seek(0)
    return Response(buf.getvalue(),
                    mimetype='application/zip',
                    headers={'Content-Disposition': f'attachment; filename="{skill.name}.zip"'})
