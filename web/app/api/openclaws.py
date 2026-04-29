import uuid
import logging
from datetime import datetime, date, time
from functools import wraps
from flask import request, jsonify, session
from app import db
from app.models import (OpenClawInstance, DailyReport, Project, Rule,
                        OpenClawRule, OpenClawSkill, Skill, ClawMessage,
                        ClawTodo, ClawTodoLog, User,
                        generate_api_token, hash_token, _simple_encrypt)
from app.api import api_bp

logger = logging.getLogger(__name__)


def _get_user():
    from app.api.skills import _get_current_user
    return _get_current_user()


def _actor_display_name(user):
    """统一返回操作者展示名：OpenClaw 名 优先，其次 display_name / username。"""
    if not user:
        return 'system'
    # _ClawAdminProxy（admin claw）和 owner（被附加 _claw_name 的 User）都可能带 claw 名
    claw_name = getattr(user, '_claw_name', None)
    if claw_name:
        return claw_name
    bound_id = getattr(user, 'bound_claw_id', None)
    if bound_id:
        try:
            c = OpenClawInstance.query.get(bound_id)
            if c and c.name:
                return c.name
        except Exception:
            pass
    return (getattr(user, 'display_name', None)
            or getattr(user, 'username', None)
            or 'system')


def _user_project_ids(user):
    ids = set()
    if not user:
        return ids
    for pid in (getattr(user, 'managed_projects', None) or []):
        try:
            ids.add(int(pid))
        except Exception:
            continue
    bound_claw_id = getattr(user, 'bound_claw_id', None)
    if bound_claw_id:
        claw = OpenClawInstance.query.get(bound_claw_id)
        if claw and claw.project_id:
            ids.add(int(claw.project_id))
    return ids


def _is_global_actor(user):
    """是否拥有全平台权限：super_admin 用户，或 project_id IS NULL 的 admin claw（如龙虾王）。"""
    if not user:
        return False
    if user.role == 'super_admin':
        return True
    return bool(getattr(user, 'is_global', False))


def _can_read_claw(user, claw):
    if not user:
        return False
    if _is_global_actor(user):
        return True
    if claw.role == 'admin':
        return False
    if getattr(user, 'bound_claw_id', None) == claw.id:
        return True
    if user.role == 'admin':
        return claw.project_id in _user_project_ids(user)
    return claw.project_id in _user_project_ids(user)


def _can_manage_claw(user, claw):
    if not _can_read_claw(user, claw):
        return False
    if _is_global_actor(user):
        return True
    if user.role == 'admin':
        return claw.project_id in _user_project_ids(user)
    return getattr(user, 'bound_claw_id', None) == claw.id


def _can_own_claw(user, claw):
    """高危操作（删除 / 查看 Token / 重置 Token）的权限。
    仅以下身份允许，项目管理员对非自己的 claw 一律拒绝：
      1) super_admin（含全平台 admin claw 如龙虾王）
      2) 绑定者：user.bound_claw_id == claw.id
      3) Owner：claw.owner 等于 user.username 或 user.display_name
         （历史数据 owner 字段两种值都存在，比如 'condihuang' 或 '文敏'）
    """
    if not user:
        return False
    if _is_global_actor(user):
        return True
    if getattr(user, 'bound_claw_id', None) == claw.id:
        return True
    owner = (getattr(claw, 'owner', None) or '').strip()
    if owner:
        username = (getattr(user, 'username', None) or '').strip()
        display = (getattr(user, 'display_name', None) or '').strip()
        if (username and owner == username) or (display and owner == display):
            return True
    return False


def _parse_project_id(raw):
    """解析项目 id：'__global__' / 'global' / 'all' / 空 / 0 / None → None（"全平台"或未关联），其它正整数 → int。"""
    if raw is None or raw == '':
        return None
    if isinstance(raw, str):
        s = raw.strip().lower()
        if s in ('__global__', 'global', 'all', '__all__'):
            return None
        try:
            v = int(s)
        except ValueError:
            return None
    else:
        try:
            v = int(raw)
        except (TypeError, ValueError):
            return None
    return v if v > 0 else None


def require_claw_token(f):
    """OpenClaw API Token 认证装饰器

    用于 OpenClaw 自身调用的接口（heartbeat、report、config）。
    优先验证 URL 中 claw_id 对应的 Token；
    如果不匹配，回退遍历所有 OpenClawInstance 验证（支持 Token 与 claw_id 不一致的场景）。
    """
    @wraps(f)
    def decorated(claw_id, *args, **kwargs):
        claw = OpenClawInstance.query.get_or_404(claw_id)

        auth_header = request.headers.get('Authorization', '')
        if not auth_header.startswith('Bearer '):
            return jsonify({'error': '缺少认证 Token'}), 401

        token = auth_header[7:]  # 去掉 "Bearer "
        # 优先匹配 claw_id 对应的 Token
        if claw.verify_token(token):
            return f(claw_id, claw=claw, *args, **kwargs)
        # 回退：遍历所有非删除的 OpenClawInstance 验证 Token
        for c in OpenClawInstance.query.filter(OpenClawInstance.status != 'deleted').all():
            if c.verify_token(token):
                return f(claw_id, claw=c, *args, **kwargs)
        return jsonify({'error': 'Token 无效或不匹配'}), 403
    return decorated


@api_bp.route('/openclaws', methods=['GET'])
def list_openclaws():
    """获取所有 OpenClaw 列表（默认不含已删除的，?include_deleted=true 可查看）
    非 super_admin/admin 看不到 admin 角色（龙虾王）
    """
    include_deleted = request.args.get('include_deleted', 'false').lower() == 'true'
    query = OpenClawInstance.query
    if not include_deleted:
        query = query.filter(OpenClawInstance.status != 'deleted')

    user = _get_user()
    if not _is_global_actor(user):
        # 非全平台：隐藏 admin 角色（龙虾王等）
        query = query.filter(OpenClawInstance.role != 'admin')
        # 非全平台：仅可见自己项目（统一 project_id）
        if user:
            proj_ids = list(_user_project_ids(user))
            if proj_ids:
                query = query.filter(OpenClawInstance.project_id.in_(proj_ids))
            elif getattr(user, 'bound_claw_id', None):
                query = query.filter(OpenClawInstance.id == int(user.bound_claw_id))
            else:
                query = query.filter(OpenClawInstance.id == -1)
        else:
            query = query.filter(OpenClawInstance.id == -1)

    claws = query.order_by(OpenClawInstance.created_at.desc()).all()
    today = date.today()
    result = []
    for c in claws:
        d = c.to_dict(brief=True)
        # 添加今日待办统计
        total_todos = ClawTodo.query.filter_by(openclaw_id=c.id, enabled=True).count()
        today_logs = ClawTodoLog.query.filter_by(openclaw_id=c.id, log_date=today).all()
        today_submitted = sum(1 for l in today_logs if l.status == 'submitted')
        today_approved = sum(1 for l in today_logs if l.status in ('approved', 'completed'))
        d['total_todos'] = total_todos
        d['today_submitted'] = today_submitted
        d['today_approved'] = today_approved
        # 高危操作（删除 / 看 Token / 重置 Token）的权限：仅 owner（super_admin / 绑定者 / 创建者）
        d['can_own'] = _can_own_claw(user, c)
        result.append(d)
    return jsonify(result)


@api_bp.route('/openclaws', methods=['POST'])
def create_openclaw():
    """注册新 OpenClaw 或通过旧 Token 恢复已归档的实例"""
    user = _get_user()
    if not user or user.role not in ('super_admin', 'admin'):
        return jsonify({'error': '需要管理员权限'}), 403
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
                if 'project_id' in data:
                    claw.project_id = _parse_project_id(data.get('project_id'))
                claw.last_modified_by = _actor_display_name(user)
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
            if 'project_id' in data:
                existing.project_id = _parse_project_id(data.get('project_id'))
            # 生成新 Token
            raw_token = generate_api_token()
            existing.api_token_hash = hash_token(raw_token)
            existing.api_token_plain = _simple_encrypt(raw_token)
            existing.last_modified_by = _actor_display_name(user)
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

    # 处理 project_id（支持 '__global__' = NULL，仅 admin 角色才有"全平台"语义）
    project_id = _parse_project_id(data.get('project_id'))

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
        last_modified_by=_actor_display_name(user),
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

    # === 可选：Hub 代建 Hermes Agent（仅 super_admin） ===
    # 注意：部署失败不会回滚 OpenClaw 注册；状态写入 agent_deployments 表，
    # 前端通过 /openclaws/<id>/agent-deployments/latest 轮询。
    # 高危操作（远端 SSH + Docker 起容器）只允许超级管理员触发，
    # 项目管理员即使能创建 OpenClaw 也不能代建 agent。
    deployment_payload = None
    create_agent = bool(data.get('create_agent'))
    deploy_opts_raw = data.get('deploy') if isinstance(data.get('deploy'), dict) else None
    if create_agent and deploy_opts_raw and user and user.role != 'super_admin':
        deployment_payload = {
            'status': 'failed',
            'error_message': '仅超级管理员可代建 Hermes Agent，已忽略部署请求。',
        }
        logger.warning('agent deploy denied for claw %d: user=%s role=%s 非 super_admin',
                       claw.id, getattr(user, 'username', '?'), getattr(user, 'role', '?'))
    elif create_agent and deploy_opts_raw:
        try:
            from app.api.agent_deployments import (
                _parse_deploy_options,
                trigger_async_deployment,
            )
            hub_url = (data.get('hub_url') or request.host_url.rstrip('/')).rstrip('/')
            deploy_req = _parse_deploy_options(
                deploy_opts_raw,
                claw=claw,
                claw_token=raw_token,
                hub_url=hub_url,
                actor_name=_actor_display_name(user),
            )
            dep = trigger_async_deployment(claw, deploy_req)
            deployment_payload = dep.to_dict()
        except ValueError as ve:
            deployment_payload = {
                'status': 'failed',
                'error_message': f'部署参数无效：{ve}',
            }
            logger.warning('agent deploy validation failed for claw %d: %s', claw.id, ve)
        except Exception as e:
            deployment_payload = {
                'status': 'failed',
                'error_message': f'部署任务下发失败：{e}',
            }
            logger.exception('agent deploy spawn failed for claw %d', claw.id)

    # 返回完整 Token（注册时必须返回，否则 OpenClaw 无法连接 SSE）
    result = claw.to_dict()
    result['api_token'] = raw_token
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
    if deployment_payload is not None:
        result['agent_deployment'] = deployment_payload
    return jsonify(result), 201


@api_bp.route('/openclaws/<int:claw_id>', methods=['GET'])
def get_openclaw(claw_id):
    """获取 OpenClaw 详情"""
    claw = OpenClawInstance.query.get_or_404(claw_id)
    user = _get_user()
    if not _can_read_claw(user, claw):
        return jsonify({'error': '无权访问该 OpenClaw'}), 403
    payload = claw.to_dict()
    payload['can_own'] = _can_own_claw(user, claw)
    return jsonify(payload)


@api_bp.route('/openclaws/<int:claw_id>', methods=['PUT'])
def update_openclaw(claw_id):
    """更新 OpenClaw 信息"""
    claw = OpenClawInstance.query.get_or_404(claw_id)
    user = _get_user()
    if not _can_manage_claw(user, claw):
        return jsonify({'error': '无权修改该 OpenClaw'}), 403
    data = request.get_json()

    # 只有 super_admin 可将 OpenClaw 设置为 admin（龙虾王）
    if 'role' in data and data.get('role') == 'admin' and user.role != 'super_admin':
        return jsonify({'error': '仅超级管理员可设置为 admin 角色'}), 403

    updatable_fields = [
        'name', 'role', 'role_title', 'responsibilities', 'project_name',
        'module_name', 'avatar', 'soul_config', 'workflow_config',
        'report_schedule', 'connection_mode', 'web_system_url', 'status'
    ]
    for field in updatable_fields:
        if field in data:
            setattr(claw, field, data[field])

    # 处理 project_id（支持 '__global__' = NULL）
    if 'project_id' in data:
        claw.project_id = _parse_project_id(data.get('project_id'))

    claw.last_modified_by = _actor_display_name(user)
    db.session.commit()
    return jsonify(claw.to_dict())


@api_bp.route('/openclaws/<int:claw_id>/regenerate-token', methods=['POST'])
def regenerate_token(claw_id):
    """重新生成 API Token（旧 Token 立即失效）"""
    claw = OpenClawInstance.query.get_or_404(claw_id)
    user = _get_user()
    if not _can_own_claw(user, claw):
        return jsonify({'error': '仅本 OpenClaw 的绑定者 / 创建者 / 超级管理员可重置 Token'}), 403

    raw_token = generate_api_token()
    claw.api_token_hash = hash_token(raw_token)
    claw.api_token_plain = _simple_encrypt(raw_token)
    claw.last_modified_by = _actor_display_name(user)
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
    user = _get_user()
    if not _can_own_claw(user, claw):
        return jsonify({'error': '仅本 OpenClaw 的绑定者 / 创建者 / 超级管理员可查看完整 Token'}), 403

    return jsonify({
        'id': claw.id,
        'name': claw.name,
        'api_token': claw.get_token_plain(),
        'api_token_preview': claw.get_token_preview(),
    })


@api_bp.route('/openclaws/<int:claw_id>', methods=['DELETE'])
def delete_openclaw(claw_id):
    """软删除 OpenClaw（保留所有配置、记忆和关联数据，支持后续恢复）。

    权限：仅本 claw 的绑定者 / 创建者 / 超级管理员可删除；
    项目管理员对项目内非自己的 claw 没有删除权限。
    """
    claw = OpenClawInstance.query.get_or_404(claw_id)
    user = _get_user()
    if not _can_own_claw(user, claw):
        return jsonify({'error': '仅本 OpenClaw 的绑定者 / 创建者 / 超级管理员可删除'}), 403
    if claw.role == 'admin' and not _is_global_actor(user):
        return jsonify({'error': '仅超级管理员可删除 admin OpenClaw'}), 403

    claw.status = 'deleted'
    claw.deleted_at = datetime.now()
    db.session.commit()

    return jsonify({
        'message': f'OpenClaw "{claw.name}" 已归档（数据已保留，可通过旧 Token 重新激活）',
        'claw_id': claw.id,
    })


@api_bp.route('/openclaws/<int:claw_id>/registration-skill', methods=['GET'])
def get_registration_skill_for_claw(claw_id):
    """生成该 OpenClaw 专属的"完整自部署"Markdown / JSON

    默认（``?format=markdown`` 或不带参数）：返回一份覆盖 7 步自部署
    流程的 Markdown 文档，AI 拿到即可在 shell 里跑完所有安装步骤。
    流程见 :mod:`app.api.registration_bootstrap`。

    ``?format=json`` ：除了完整 Markdown，再附带：

    - ``api_token`` / ``hub_url`` / ``claw_id`` / ``claw_name``
    - ``mcp_config_snippets``：三种 host 的 mcp.json 片段
    - ``skill_links`` / ``rule_links``：已分配 skill / rule 的直链

    已移除 legacy 回退逻辑，统一走 sidecar-only 的 bootstrap 文档。
    """
    from flask import Response
    from app.api.registration_bootstrap import (
        build_bootstrap_markdown,
        build_mcp_config_snippets,
    )

    claw = OpenClawInstance.query.get_or_404(claw_id)
    token = claw.get_token_plain() or ''
    hub_url = 'http://your-hub-host:8088'
    project_name = claw.project.name if claw.project else (claw.project_name or '未指定')

    skill_links = [
        f"{hub_url}/api/v1/skills/{s.skill.id}/raw"
        for s in claw.skills if s.enabled and s.skill
    ]
    rule_links = [
        f"{hub_url}/api/v1/rules/{r.rule.id}/raw"
        for r in OpenClawRule.query.filter_by(
            openclaw_id=claw_id, enabled=True).all()
        if r.rule
    ]

    # 统一新逻辑：完整 7 步自部署文档
    md = build_bootstrap_markdown(
        hub_url=hub_url,
        claw_id=claw.id,
        claw_name=claw.name or '',
        role=claw.role or 'test_member',
        project_name=project_name,
        api_token=token,
        skill_links=skill_links,
        rule_links=rule_links,
    )

    fmt = request.args.get('format', 'markdown')
    if fmt == 'json':
        return jsonify({
            'claw_id': claw.id,
            'claw_name': claw.name,
            'hub_url': hub_url,
            'api_token': token,
            'skill_content': md,
            'skill_links': skill_links,
            'rule_links': rule_links,
            'mcp_config_snippets': build_mcp_config_snippets(
                hub_url, claw.id, token,
            ),
            'mode': 'bootstrap',
        })

    return Response(
        md,
        mimetype='text/markdown; charset=utf-8',
        headers={
            'Content-Disposition': f'inline; filename="registration-claw-{claw.id}.md"',
        },
    )


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

    客户端可携带 status 字段上报当前状态（工作/学习/摸鱼/休息），
    Hub 只记录，不硬编码。
    返回：当前状态 + 消息 + 待办统计
    OpenClaw 每 30 秒调用一次，根据返回决定下一步动作
    """
    from app.models import ClawTodo, ClawTodoLog

    data = request.get_json(silent=True) or {}
    # 客户端上报状态，不硬编码
    client_status = data.get('status')
    valid_statuses = ('工作', '学习', '摸鱼', '休息')
    if client_status and client_status in valid_statuses:
        claw.status = client_status
    elif not claw.status or claw.status == 'offline':
        # 无上报且当前离线，默认设为工作
        claw.status = '工作'
    claw.last_activity = datetime.now()
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
        'server_time': datetime.now().isoformat(),
    })


@api_bp.route('/openclaws/<int:claw_id>/report', methods=['POST'])
@require_claw_token
def submit_report(claw_id, claw=None):
    """工作日报上报（需 Token 认证）"""
    data = request.get_json()

    if not data:
        return jsonify({'error': '上报数据为空'}), 400

    # 兼容：如果传了 content 但没传 tasks_completed，自动映射
    if data.get('content') and not data.get('tasks_completed'):
        data['tasks_completed'] = data['content']

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
        # 更新已有日报（非 None 的字段才覆盖，避免清空已有内容）
        for field in ['tasks_completed', 'knowledge_recorded', 'experience_shared', 'knowledge_learned', 'ai_summary']:
            val = data.get(field)
            if val is not None:
                setattr(report, field, val)
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

    task_id = f"task_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}"
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
    claw.last_modified_by = _actor_display_name(_get_user())
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
        msg.delivered_at = datetime.now()
    db.session.commit()

    return jsonify({
        'messages': [m.to_dict() for m in pending],
        'count': len(pending),
    })


@api_bp.route('/openclaws/<int:claw_id>/messages/<int:msg_id>/read', methods=['PUT'])
@require_claw_token
def mark_claw_message_read(claw_id, msg_id, claw=None):
    """标记消息为已读，支持附带回复"""
    msg = ClawMessage.query.get_or_404(msg_id)

    if msg.claw_id != claw_id:
        return jsonify({'error': '无权操作'}), 403

    msg.status = 'read'
    msg.read_at = datetime.now()

    # 如果附带了 reply，自动创建一条回复消息
    reply_msg = None
    data = request.get_json(silent=True) or {}
    reply_content = data.get('reply', '').strip()
    if reply_content:
        reply_msg = ClawMessage(
            claw_id=claw_id,
            sender_name=claw.name,
            content=reply_content,
            msg_type='text',
            direction='from_claw',
            reply_to=msg_id,
            status='delivered',
            delivered_at=datetime.now(),
        )
        db.session.add(reply_msg)

    db.session.commit()

    result = {'message': '已标记为已读'}
    if reply_msg:
        result['reply'] = reply_msg.to_dict()
    return jsonify(result)
