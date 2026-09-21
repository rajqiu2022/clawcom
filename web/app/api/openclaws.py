import os
import uuid
import logging
import json
from datetime import datetime, date, time
from functools import wraps
from flask import request, jsonify, session
from app import db
from app.hermes_models import (
    DEFAULT_HERMES_LLM_PROVIDER,
    DEFAULT_HERMES_LLM_MODEL,
    default_hermes_model,
    normalize_hermes_model,
    normalize_timiai_model_for_project,
    default_timiai_model_for_project,
    normalize_hermes_provider,
)
from app.services.hermes_timiai_projects import normalize_timiai_project
from app.models import (OpenClawInstance, DailyReport, Project, Rule,
                        OpenClawRule, OpenClawSkill, Skill, ClawMessage,
                        ClawTodo, ClawTodoLog, User, AgentDeployment,
                        generate_api_token, hash_token, _simple_encrypt)
from app.api import api_bp
from app.services.todo_schedule import (
    TERMINAL_TODO_STATUSES,
    TODO_TIMEZONE,
    cst_iso,
    cst_now_naive,
    todo_schedule_state,
)

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


def _ensure_sidecar_v2_enabled(claw):
    """确保注册链接/一键脚本走 #143 sidecar v2，并把连接模式切到 SSE。"""
    changed = False
    if (claw.connection_mode or '').lower() != 'sse':
        claw.connection_mode = 'sse'
        changed = True

    skill = Skill.query.get(143) or Skill.query.filter_by(
        name='hub-sse-sidecar-v2').first()
    if skill:
        rel = OpenClawSkill.query.filter_by(
            openclaw_id=claw.id, skill_id=skill.id).first()
        if rel:
            if not rel.enabled:
                rel.enabled = True
                changed = True
        else:
            db.session.add(OpenClawSkill(
                openclaw_id=claw.id, skill_id=skill.id, enabled=True))
            changed = True
    if changed:
        db.session.commit()
    return skill


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


def _parse_llm_fields(data, claw=None):
    """Parse model selection from top-level or deploy payload."""
    deploy = data.get('deploy') if isinstance(data.get('deploy'), dict) else {}
    raw_provider = (data.get('llm_provider') or deploy.get('llm_provider')
                    or getattr(claw, 'llm_provider', None)
                    or DEFAULT_HERMES_LLM_PROVIDER)
    provider = normalize_hermes_provider(raw_provider)
    raw_model = data.get('llm_model') or deploy.get('llm_model')
    current_provider = str(getattr(claw, 'llm_provider', '') or '').strip()
    if not raw_model and current_provider == provider:
        raw_model = getattr(claw, 'llm_model', None)
    if provider == 'timiai':
        project = _parse_timiai_project(data, claw)
        raw_model = raw_model or default_timiai_model_for_project(project)
        return provider, normalize_timiai_model_for_project(
            raw_model, project)
    return provider, normalize_hermes_model(
        raw_model or default_hermes_model(provider), provider)


def _parse_timiai_project(data, claw=None):
    deploy = data.get('deploy') if isinstance(data.get('deploy'), dict) else {}
    return normalize_timiai_project(
        data.get('timiai_project') or deploy.get('timiai_project')
        or getattr(claw, 'timiai_project', None))


def _parse_work_dirs(data):
    from app.services.agent_deployer import normalize_agent_work_dirs
    return normalize_agent_work_dirs(data.get('work_dirs'))


def _sync_sidecar_llm_config(claw, actor_name):
    """Keep sidecar-config version moving when card model changes."""
    try:
        from app.models import ClawSidecarConfig
        cfg = ClawSidecarConfig.query.get(claw.id)
        if not cfg:
            return
        cfg.llm_provider = claw.llm_provider or DEFAULT_HERMES_LLM_PROVIDER
        cfg.llm_model = claw.llm_model or DEFAULT_HERMES_LLM_MODEL
        cfg.config_version = (cfg.config_version or 0) + 1
        cfg.updated_by = actor_name
    except Exception:
        logger.exception('sync sidecar llm config failed for claw %s', claw.id)


def _has_registered_hermes_agent(claw):
    """是否通过 Hub 代建过 Hermes Agent（用于卡片设置页签开关）。"""
    dep = (AgentDeployment.query
           .filter_by(openclaw_id=claw.id, agent_type='hermes')
           .order_by(AgentDeployment.created_at.desc())
           .first())
    return bool(dep and dep.deploy_method == 'systemd' and dep.status == 'success')


def _has_registered_codex_agent(claw):
    """是否通过 Hub 代建过 Linux Codex provider。"""
    dep = (AgentDeployment.query
           .filter_by(openclaw_id=claw.id, agent_type='codex')
           .order_by(AgentDeployment.created_at.desc())
           .first())
    return bool(dep and dep.deploy_method == 'systemd' and dep.status == 'success')


def _worker_runtime_payload(claw, cfg=None, *, query_if_missing=True):
    """Expose runtime identity independently from Hub deployment history."""
    from app.models import ClawSidecarConfig
    from app.services.worker_runtime import runtime_summary

    if cfg is None and query_if_missing:
        cfg = ClawSidecarConfig.query.get(claw.id)
    summary = runtime_summary(
        cfg.runtime_config_json if cfg else None,
        cfg.config_owner if cfg else 'hub',
    )
    summary['runtime_reported_at'] = (
        str(cfg.runtime_reported_at)
        if cfg and cfg.runtime_reported_at else None)
    return summary


def _record_failed_agent_deployment(claw, error_message, actor_name, deploy_opts=None):
    """部署请求无法下发时也要落库，前端才能锁定卡片并允许重试。"""
    try:
        from app.services.agent_deployer import (
            build_codex_unit_name,
            build_default_systemd_data_dir,
            build_systemd_unit_name,
        )
        agent_type = 'hermes'
        host = ''
        ssh_user = ''
        if isinstance(deploy_opts, dict):
            agent_type = (deploy_opts.get('agent_type') or 'hermes').strip().lower()
            if agent_type not in ('hermes', 'codex'):
                agent_type = 'hermes'
            host = (deploy_opts.get('host') or '').strip()
            if host and deploy_opts.get('ssh_port'):
                host = f"{host}:{deploy_opts.get('ssh_port')}"
            ssh_user = (deploy_opts.get('ssh_user') or '').strip()
        dep = AgentDeployment(
            openclaw_id=claw.id,
            agent_type=agent_type,
            deploy_method='systemd',
            host=host,
            ssh_user=ssh_user,
            remote_base_dir=build_default_systemd_data_dir(
                claw.id, claw.name or f'claw-{claw.id}', safe_name=claw.safe_name or ''),
            container_name=(build_codex_unit_name(claw.id) if agent_type == 'codex'
                            else build_systemd_unit_name(claw.id)),
            image='',
            status='failed',
            error_message=error_message,
            log_tail=error_message,
            finished_at=datetime.now(),
            triggered_by=actor_name,
        )
        db.session.add(dep)
        db.session.commit()
        return dep
    except Exception:
        db.session.rollback()
        logger.exception('record failed agent deployment failed for claw %s', getattr(claw, 'id', '?'))
        return None


def _sync_sidecar_wecom_config(claw, actor_name):
    """企微绑定变更后推进 sidecar 配置版本。"""
    try:
        from app.models import ClawSidecarConfig
        cfg = ClawSidecarConfig.query.get(claw.id)
        if not cfg:
            return
        cfg.wecom_enabled = bool(claw.wecom_bot_id and claw.wecom_bot_secret)
        cfg.config_version = (cfg.config_version or 0) + 1
        cfg.updated_by = actor_name
    except Exception:
        logger.exception('sync sidecar wecom config failed for claw %s', claw.id)


def require_claw_token(f):
    """OpenClaw API Token 认证装饰器 — 统一版本（防越权）"""
    from app.api.auth_utils import require_claw_token as _unified
    return _unified(f)


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
    claw_ids = [c.id for c in claws]
    deployments = (AgentDeployment.query
                   .filter(AgentDeployment.openclaw_id.in_(claw_ids))
                   .order_by(AgentDeployment.created_at.desc())
                   .all()) if claw_ids else []
    latest_deployment_by_claw = {}
    managed_hermes_ids = set()
    managed_codex_ids = set()
    for deployment in deployments:
        latest_deployment_by_claw.setdefault(
            deployment.openclaw_id, deployment)
        if deployment.deploy_method == 'systemd' and deployment.status == 'success':
            if deployment.agent_type == 'hermes':
                managed_hermes_ids.add(deployment.openclaw_id)
            elif deployment.agent_type == 'codex':
                managed_codex_ids.add(deployment.openclaw_id)
    from app.models import ClawSidecarConfig
    runtime_cfg_by_claw = {
        item.claw_id: item
        for item in (ClawSidecarConfig.query
                     .filter(ClawSidecarConfig.claw_id.in_(claw_ids)).all())
    } if claw_ids else {}
    today = cst_now_naive().date()
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
        latest_dep = latest_deployment_by_claw.get(c.id)
        d['has_hermes_agent'] = c.id in managed_hermes_ids
        d['has_codex_agent'] = c.id in managed_codex_ids
        runtime_payload = _worker_runtime_payload(
            c, runtime_cfg_by_claw.get(c.id), query_if_missing=False)
        d.update(runtime_payload)
        d['agent_type'] = (
            runtime_payload['runtime_provider']
            if runtime_payload['has_worker_runtime']
            else (latest_dep.agent_type if latest_dep else ''))
        d['agent_deployment_status'] = latest_dep.status if latest_dep else ''
        d['agent_deployment_id'] = latest_dep.id if latest_dep else None
        d['agent_deployment_error'] = latest_dep.error_message if latest_dep else ''
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
    data = request.get_json(silent=True) or {}

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
                try:
                    claw.llm_provider, claw.llm_model = _parse_llm_fields(data)
                    claw.timiai_project = _parse_timiai_project(data)
                except ValueError as e:
                    return jsonify({'error': str(e)}), 400
                claw.last_modified_by = _actor_display_name(user)
                db.session.commit()
                _ensure_sidecar_v2_enabled(claw)

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
            try:
                existing.llm_provider, existing.llm_model = _parse_llm_fields(data)
                existing.timiai_project = _parse_timiai_project(data)
            except ValueError as e:
                return jsonify({'error': str(e)}), 400
            existing.last_modified_by = _actor_display_name(user)
            db.session.commit()
            _ensure_sidecar_v2_enabled(existing)

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

    try:
        llm_provider, llm_model = _parse_llm_fields(data)
        timiai_project = _parse_timiai_project(data)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    wecom_bot_id = (data.get('wecom_bot_id') or '').strip()
    wecom_bot_secret = (data.get('wecom_bot_secret') or '').strip()
    if bool(wecom_bot_id) != bool(wecom_bot_secret):
        return jsonify({'error': '企微 Bot ID / Key 和 Secret 必须同时填写'}), 400

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
        llm_provider=llm_provider,
        llm_model=llm_model,
        timiai_project=timiai_project,
        wecom_bot_id=wecom_bot_id,
        wecom_bot_secret=(_simple_encrypt(wecom_bot_secret)
                          if wecom_bot_secret else ''),
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

    _ensure_sidecar_v2_enabled(claw)

    # === 可选：Hub 代建 Hermes/Codex Agent（仅 super_admin） ===
    # 注意：部署失败不会回滚 OpenClaw 注册；状态写入 agent_deployments 表，
    # 前端通过 /openclaws/<id>/agent-deployments/latest 轮询。
    # 高危操作（远端 SSH + Docker 起容器）只允许超级管理员触发，
    # 项目管理员即使能创建 OpenClaw 也不能代建 agent。
    deployment_payload = None
    create_agent = bool(data.get('create_agent'))
    deploy_opts_raw = data.get('deploy') if isinstance(data.get('deploy'), dict) else None
    if create_agent and deploy_opts_raw and user and user.role != 'super_admin':
        err = '仅超级管理员可代建 Agent，已忽略部署请求。'
        dep = _record_failed_agent_deployment(
            claw, err, _actor_display_name(user), deploy_opts_raw)
        deployment_payload = dep.to_dict() if dep else {
            'status': 'failed',
            'error_message': err,
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
            err = f'部署参数无效：{ve}'
            dep = _record_failed_agent_deployment(
                claw, err, _actor_display_name(user), deploy_opts_raw)
            deployment_payload = dep.to_dict() if dep else {
                'status': 'failed',
                'error_message': err,
            }
            logger.warning('agent deploy validation failed for claw %d: %s', claw.id, ve)
        except Exception as e:
            err = f'部署任务下发失败：{e}'
            dep = _record_failed_agent_deployment(
                claw, err, _actor_display_name(user), deploy_opts_raw)
            deployment_payload = dep.to_dict() if dep else {
                'status': 'failed',
                'error_message': err,
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
    payload['has_hermes_agent'] = _has_registered_hermes_agent(claw)
    payload['has_codex_agent'] = _has_registered_codex_agent(claw)
    payload.update(_worker_runtime_payload(claw))
    return jsonify(payload)


@api_bp.route('/openclaws/<int:claw_id>', methods=['PUT'])
def update_openclaw(claw_id):
    """更新 OpenClaw 信息"""
    claw = OpenClawInstance.query.get_or_404(claw_id)
    user = _get_user()
    data = request.get_json(silent=True) or {}

    llm_keys = {'llm_provider', 'llm_model', 'timiai_project'}
    wecom_keys = {'wecom_bot_id', 'wecom_bot_secret', 'clear_wecom_bot_secret'}
    work_dir_keys = {'work_dirs'}
    wants_llm_update = bool(llm_keys & set(data.keys()))
    wants_wecom_update = bool(wecom_keys & set(data.keys()))
    wants_work_dir_update = bool(work_dir_keys & set(data.keys()))
    non_self_settings_update = bool(set(data.keys()) - llm_keys - wecom_keys - work_dir_keys)
    if non_self_settings_update and not _can_manage_claw(user, claw):
        return jsonify({'error': '无权修改该 OpenClaw'}), 403
    if wants_llm_update and not _can_own_claw(user, claw):
        return jsonify({'error': '仅本人 / 绑定 OpenClaw / 超级管理员可修改大模型'}), 403
    if wants_llm_update and not _has_registered_hermes_agent(claw):
        return jsonify({'error': '仅通过 Hub 注册部署成功的 Hermes Agent 可修改大模型'}), 403
    if wants_wecom_update and not _can_own_claw(user, claw):
        return jsonify({'error': '仅本人 / 绑定 OpenClaw / 超级管理员可绑定企微机器人'}), 403
    if wants_wecom_update and not _has_registered_hermes_agent(claw):
        return jsonify({'error': '仅通过 Hub 注册部署的 Hermes Agent 可绑定企微机器人'}), 403
    if wants_work_dir_update and not _can_own_claw(user, claw):
        return jsonify({'error': '仅本人 / 绑定 OpenClaw / 超级管理员可修改工作目录'}), 403
    if wants_work_dir_update and not _has_registered_hermes_agent(claw):
        return jsonify({'error': '仅通过 Hub 注册部署成功的 Hermes Agent 可设置工作目录'}), 403

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

    if wants_llm_update:
        try:
            claw.llm_provider, claw.llm_model = _parse_llm_fields(data, claw)
            claw.timiai_project = _parse_timiai_project(data, claw)
        except ValueError as e:
            return jsonify({'error': str(e)}), 400

    if wants_wecom_update:
        if 'wecom_bot_id' in data:
            claw.wecom_bot_id = (data.get('wecom_bot_id') or '').strip()
        if data.get('clear_wecom_bot_secret'):
            claw.wecom_bot_secret = ''
        secret = (data.get('wecom_bot_secret') or '').strip()
        if secret:
            claw.wecom_bot_secret = _simple_encrypt(secret)

    if wants_work_dir_update:
        try:
            claw.work_dirs = _parse_work_dirs(data)
        except ValueError as e:
            return jsonify({'error': str(e)}), 400

    # 处理 project_id（支持 '__global__' = NULL）
    if 'project_id' in data:
        claw.project_id = _parse_project_id(data.get('project_id'))

    actor_name = _actor_display_name(user)
    claw.last_modified_by = actor_name
    if wants_llm_update:
        _sync_sidecar_llm_config(claw, actor_name)
    if wants_wecom_update:
        _sync_sidecar_wecom_config(claw, actor_name)
    db.session.commit()
    payload = claw.to_dict()
    payload['has_hermes_agent'] = _has_registered_hermes_agent(claw)
    payload['has_codex_agent'] = _has_registered_codex_agent(claw)
    payload.update(_worker_runtime_payload(claw))
    return jsonify(payload)


@api_bp.route('/openclaws/<int:claw_id>/worker-runtime',
              methods=['GET', 'PUT'])
def manage_worker_runtime(claw_id):
    """Manage the secret-free Claw Worker runtime compatibility contract.

    This endpoint is needed for already-running Workers that predate structured
    runtime reporting.  Only operators can change config ownership; a Worker
    heartbeat can refresh runtime facts but cannot claim that authority.
    """
    from app.models import AuditLog, ClawSidecarConfig
    from app.services.worker_runtime import (
        normalize_config_owner,
        runtime_summary,
        validate_worker_runtime,
    )

    claw = OpenClawInstance.query.get_or_404(claw_id)
    user = _get_user()
    if not _is_global_actor(user):
        return jsonify({'error': '仅超级管理员可配置 Worker 运行时'}), 403

    cfg = ClawSidecarConfig.query.get(claw_id)
    if request.method == 'GET':
        result = runtime_summary(
            cfg.runtime_config_json if cfg else None,
            cfg.config_owner if cfg else 'hub',
        )
        result.update({
            'claw_id': claw_id,
            'agent_type': cfg.agent_type if cfg else '',
            'config_version': cfg.config_version if cfg else None,
            'runtime_reported_at': (
                str(cfg.runtime_reported_at)
                if cfg and cfg.runtime_reported_at else None),
        })
        return jsonify(result)

    data = request.get_json(silent=True)
    if not isinstance(data, dict) or set(data) != {'config_owner', 'runtime'}:
        return jsonify({
            'error': '请求体必须且只能包含 config_owner、runtime 字段',
        }), 400
    try:
        owner = normalize_config_owner(data.get('config_owner'))
        runtime = validate_worker_runtime(data.get('runtime'))
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    if runtime['kind'] != 'claw_worker':
        return jsonify({'error': '该接口仅登记 kind=claw_worker'}), 400

    if cfg is None:
        cfg = ClawSidecarConfig(
            claw_id=claw_id,
            agent_type=runtime['provider'],
            llm_provider=claw.llm_provider or 'venus',
            llm_model=claw.llm_model or 'venus',
            wecom_enabled=bool(claw.wecom_bot_id and claw.wecom_bot_secret),
            enabled=True,
            config_version=1,
            config_owner=owner,
        )
        db.session.add(cfg)

    before = {
        'config_owner': cfg.config_owner or 'hub',
        'runtime': cfg.runtime_config_json,
        'agent_type': cfg.agent_type or '',
    }
    after = {
        'config_owner': owner,
        'runtime': runtime,
        'agent_type': runtime['provider'],
    }
    changed = before != after
    if changed:
        actor_name = _actor_display_name(user)
        cfg.config_owner = owner
        cfg.runtime_config_json = runtime
        cfg.agent_type = runtime['provider']
        cfg.config_version = int(cfg.config_version or 0) + 1
        cfg.updated_by = actor_name
        db.session.add(AuditLog(
            action='update',
            resource_type='claw_worker_runtime',
            resource_id=claw_id,
            resource_name=claw.name,
            operator=actor_name,
            ip_address=request.remote_addr,
            detail=json.dumps(
                {'before': before, 'after': after},
                ensure_ascii=False, sort_keys=True),
        ))
    db.session.commit()

    result = runtime_summary(cfg.runtime_config_json, cfg.config_owner)
    result.update({
        'claw_id': claw_id,
        'agent_type': cfg.agent_type,
        'config_version': cfg.config_version,
        'changed': changed,
        'runtime_reported_at': (
            str(cfg.runtime_reported_at) if cfg.runtime_reported_at else None),
    })
    return jsonify(result)


@api_bp.route('/openclaws/<int:claw_id>/system-context-policy',
              methods=['GET', 'PUT'])
def manage_system_context_policy(claw_id):
    """Manage the per-Worker trusted policy used by Codex sidecars.

    This is an operator control-plane endpoint. The stored value is never
    returned by ``sidecar-config`` directly; that endpoint validates it again
    and intersects its Flow IDs with the live Workflow execute ACL.
    """

    from app.models import AuditLog, ClawSidecarConfig
    from app.services.agent_system_context import validate_system_context_policy

    claw = OpenClawInstance.query.get_or_404(claw_id)
    user = _get_user()
    if not _is_global_actor(user):
        return jsonify({'error': '仅超级管理员可配置 Worker 系统策略'}), 403

    cfg = ClawSidecarConfig.query.get(claw_id)
    if request.method == 'GET':
        return jsonify({
            'claw_id': claw_id,
            'agent_type': cfg.agent_type if cfg else '',
            'config_version': cfg.config_version if cfg else None,
            'policy': cfg.system_context_policy_json if cfg else None,
        })

    data = request.get_json(silent=True)
    if not isinstance(data, dict) or set(data) != {'policy'}:
        return jsonify({'error': '请求体仅允许 policy 字段'}), 400

    if not ((cfg and (cfg.agent_type or '').lower() == 'codex') or
            _has_registered_codex_agent(claw)):
        return jsonify({'error': '仅已注册的 Codex Worker 可启用该策略'}), 400

    raw_policy = data.get('policy')
    try:
        canonical_policy = (
            None if raw_policy is None
            else validate_system_context_policy(raw_policy)
        )
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400

    if cfg is None:
        cfg = ClawSidecarConfig(
            claw_id=claw_id,
            agent_type='codex',
            llm_provider=claw.llm_provider or 'venus',
            llm_model=claw.llm_model or 'venus',
            wecom_enabled=bool(claw.wecom_bot_id and claw.wecom_bot_secret),
            enabled=True,
            config_version=1,
        )
        db.session.add(cfg)

    before = cfg.system_context_policy_json
    changed = before != canonical_policy
    if changed:
        actor_name = _actor_display_name(user)
        cfg.system_context_policy_json = canonical_policy
        cfg.config_version = int(cfg.config_version or 0) + 1
        cfg.updated_by = actor_name
        db.session.add(AuditLog(
            action='update',
            resource_type='sidecar_system_context_policy',
            resource_id=claw_id,
            resource_name=claw.name,
            operator=actor_name,
            ip_address=request.remote_addr,
            detail=json.dumps({
                'before': before,
                'after': canonical_policy,
                'config_version': cfg.config_version,
            }, ensure_ascii=False, sort_keys=True),
        ))
    db.session.commit()

    return jsonify({
        'claw_id': claw_id,
        'agent_type': cfg.agent_type,
        'config_version': cfg.config_version,
        'changed': changed,
        'policy': cfg.system_context_policy_json,
    })


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
        build_bootstrap_command,
        build_bootstrap_markdown,
        build_mcp_config_snippets,
        _bootstrap_sh_url,
    )

    claw = OpenClawInstance.query.get_or_404(claw_id)
    _ensure_sidecar_v2_enabled(claw)
    token = claw.get_token_plain() or ''
    # 历史教训 #126b：与 bootstrap.sh 接口保持一致，不用 request.host_url，强制走 env / 固定 fallback。
    # 详见 get_bootstrap_script_for_claw 注释。
    hub_url = (os.environ.get('HUB_PUBLIC_URL')
               or 'https://clawteam.woa.com:18800').rstrip('/')
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
            'bootstrap_sh_url': _bootstrap_sh_url(hub_url, claw.id, token),
            'bootstrap_command': build_bootstrap_command(hub_url, claw.id, token),
            'skill_content': md,
            'skill_links': skill_links,
            'rule_links': rule_links,
            'mcp_config_snippets': build_mcp_config_snippets(
                hub_url, claw.id, token,
            ),
            'mode': 'bootstrap-v2',
        })

    return Response(
        md,
        mimetype='text/markdown; charset=utf-8',
        headers={
            'Content-Disposition': f'inline; filename="registration-claw-{claw.id}.md"',
            'Cache-Control': 'no-store',
        },
    )


@api_bp.route('/openclaws/<int:claw_id>/bootstrap.sh', methods=['GET'])
def get_bootstrap_script_for_claw(claw_id):
    """返回该 OpenClaw 的一键自部署 shell 脚本。

    访问方式：
      bash <(curl -fsSL '.../bootstrap.sh?token=oc_tk_xxx')

    token 参数必须匹配该 claw；Web owner/super_admin 登录态也可访问。
    """
    from flask import Response
    from app.api.registration_bootstrap import build_bootstrap_script

    claw = OpenClawInstance.query.get_or_404(claw_id)
    token = (request.args.get('token') or request.args.get('claw_token')
             or request.args.get('api_token') or '').strip()
    user = _get_user()
    token_ok = bool(token and claw.verify_token(token))
    if not token_ok and not _can_own_claw(user, claw):
        return Response(
            '#!/usr/bin/env bash\necho "bootstrap token 无效或无权限" >&2\nexit 1\n',
            status=403,
            mimetype='text/x-shellscript; charset=utf-8',
            headers={'Cache-Control': 'no-store'},
        )

    _ensure_sidecar_v2_enabled(claw)
    raw_token = token if token_ok else (claw.get_token_plain() or '')
    if not raw_token:
        return Response(
            '#!/usr/bin/env bash\necho "该 OpenClaw 尚未生成 API Token" >&2\nexit 1\n',
            status=404,
            mimetype='text/x-shellscript; charset=utf-8',
            headers={'Cache-Control': 'no-store'},
        )

    # 历史教训 #126b：
    # - 千万不能用 request.host_url！它随请求来源变（curl 127.0.0.1 时拿到 http://127.0.0.1:18800/，
    #   浏览器走 NGN 时拿到 https://clawteam.woa.com/），结果发给 claw 全不通。
    # - https://clawteam.woa.com (443) 在 testserver 上是 lampp/Apache 占的（SLB 默认 vhost 404 真凶），
    #   不是 Hub 真身。Hub 真身只在 :18800（gunicorn）。
    # - claw 实际可达的稳定 URL = http://clawteam.woa.com:18800（直连 Flask，绕开 Apache/SLB）。
    hub_url = (os.environ.get('HUB_PUBLIC_URL')
               or 'https://clawteam.woa.com:18800').rstrip('/')
    project_name = claw.project.name if claw.project else (claw.project_name or '未指定')
    script = build_bootstrap_script(
        hub_url=hub_url,
        claw_id=claw.id,
        claw_name=claw.name or '',
        role=claw.role or 'test_member',
        project_name=project_name,
        api_token=raw_token,
    )
    return Response(
        script,
        mimetype='text/x-shellscript; charset=utf-8',
        headers={
            'Content-Disposition': f'inline; filename="openclaw-bootstrap-{claw.id}.sh"',
            'Cache-Control': 'no-store',
        },
    )


@api_bp.route('/openclaws/<int:claw_id>/offline-install-bundle.sh', methods=['GET'])
def get_offline_install_bundle_for_claw(claw_id):
    """生成 self-contained 离线安装脚本（绕过 NGN/SLB 限制场景）。

    背景：`clawteam.woa.com` 走 NGN 网关；目标机器不在公司信任网段
    （非 21.x.x.x / 10.x.x.x 等，如外网 / AI Sandbox / Mac 桌面 / 海外节点）时，
    SLB 会把无 SSO cookie 的请求路由到默认 Apache 占位（server header
    `Apache/2.4.37 (Unix) PHP/5.6.39`），返 404 Error 页。导致：
      ① curl bootstrap.sh 拿到 404 错误页；
      ② 即使脚本被复制过去，内部 `curl $HUB_URL/static/skills/...` 也 404。

    本接口在浏览器（已认证）端被调用，返回 self-contained 脚本：
      - bootstrap.sh / install_v2.sh / sidecar_v2.py / cleanup_v1.sh 四份资源 base64 内联
      - 启动时自动改写 bootstrap.sh 里 3 处 curl 为 cp 本地文件
      - 用户 scp 这一个文件到任何机器 bash 即可装好 sidecar
    """
    from flask import Response
    import base64
    from app.api.registration_bootstrap import build_bootstrap_script

    claw = OpenClawInstance.query.get_or_404(claw_id)
    token = (request.args.get('token') or request.args.get('claw_token')
             or request.args.get('api_token') or '').strip()
    user = _get_user()
    token_ok = bool(token and claw.verify_token(token))
    if not token_ok and not _can_own_claw(user, claw):
        return Response('#!/usr/bin/env bash\necho "offline-bundle 鉴权失败" >&2\nexit 1\n',
                        status=403, mimetype='text/x-shellscript; charset=utf-8')

    _ensure_sidecar_v2_enabled(claw)
    raw_token = token if token_ok else (claw.get_token_plain() or '')
    if not raw_token:
        return Response('#!/usr/bin/env bash\necho "该 OpenClaw 尚未生成 API Token" >&2\nexit 1\n',
                        status=404, mimetype='text/x-shellscript; charset=utf-8')

    hub_url = (os.environ.get('HUB_PUBLIC_URL')
               or request.host_url.rstrip('/')
               or 'https://clawteam.woa.com').rstrip('/')
    project_name = claw.project.name if claw.project else (claw.project_name or '未指定')
    bootstrap_sh = build_bootstrap_script(
        hub_url=hub_url, claw_id=claw.id, claw_name=claw.name or '',
        role=claw.role or 'test_member', project_name=project_name,
        api_token=raw_token,
    )

    static_root = os.path.abspath(os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        '..', 'static', 'skills', 'hub-sse-sidecar-v2'))

    def _read_or_empty(path):
        try:
            with open(path, 'rb') as f:
                return f.read()
        except (FileNotFoundError, OSError) as e:
            logger.warning(f'[offline-bundle] 读取 {path} 失败: {e}')
            return b''

    install_v2_sh = _read_or_empty(os.path.join(static_root, 'install_v2.sh'))
    sidecar_v2_py = _read_or_empty(os.path.join(static_root, 'scripts', 'sidecar_v2.py'))
    cleanup_v1_sh = _read_or_empty(os.path.join(static_root, 'scripts', 'cleanup_v1.sh'))

    if not install_v2_sh or not sidecar_v2_py:
        return Response(
            f'#!/usr/bin/env bash\necho "[ERROR] Hub 端缺失 v2 静态资源（{static_root}）" >&2\nexit 1\n',
            status=500, mimetype='text/x-shellscript; charset=utf-8')

    def _b64(data):
        return base64.b64encode(data).decode('ascii')

    bundle = '#!/usr/bin/env bash\n'
    bundle += f'# OpenClaw 离线安装包 (claw_id={claw.id}, name={claw.name or ""})\n'
    bundle += '# 适用：目标机器不在公司信任网段、无法 curl $HUB_URL/static/... 的场景。\n'
    bundle += '#\n# 用法：\n'
    bundle += f'#   scp openclaw-{claw.id}-offline-install.sh user@target:~/\n'
    bundle += f'#   ssh user@target \'bash ~/openclaw-{claw.id}-offline-install.sh\'\n'
    bundle += '#\n# 已内联 4 份资源（base64）：bootstrap.sh / install_v2.sh / sidecar_v2.py / cleanup_v1.sh\n'
    bundle += 'set -euo pipefail\n\n'
    bundle += f'WORK_DIR="$(mktemp -d -t openclaw-offline-{claw.id}-XXXXXX)"\n'
    bundle += "trap 'rm -rf \"$WORK_DIR\"' EXIT\n"
    bundle += 'mkdir -p "$WORK_DIR/scripts"\n\n'
    bundle += 'echo "[offline-install] 解码内联资源 -> $WORK_DIR"\n'
    bundle += "base64 -d > \"$WORK_DIR/bootstrap.sh\" <<'__BOOT_B64__'\n"
    bundle += _b64(bootstrap_sh.encode('utf-8')) + '\n__BOOT_B64__\n'
    bundle += "base64 -d > \"$WORK_DIR/install_v2.sh\" <<'__INST_B64__'\n"
    bundle += _b64(install_v2_sh) + '\n__INST_B64__\n'
    bundle += "base64 -d > \"$WORK_DIR/scripts/sidecar_v2.py\" <<'__SIDE_B64__'\n"
    bundle += _b64(sidecar_v2_py) + '\n__SIDE_B64__\n'
    bundle += "base64 -d > \"$WORK_DIR/scripts/cleanup_v1.sh\" <<'__CLEAN_B64__'\n"
    bundle += _b64(cleanup_v1_sh or b'#!/usr/bin/env bash\nexit 0\n') + '\n__CLEAN_B64__\n'
    bundle += 'chmod +x "$WORK_DIR/bootstrap.sh" "$WORK_DIR/install_v2.sh" \\\n'
    bundle += '         "$WORK_DIR/scripts/sidecar_v2.py" "$WORK_DIR/scripts/cleanup_v1.sh"\n\n'
    bundle += '# 改写 bootstrap.sh 里 3 处 curl $HUB_URL/static/... 为 cp 本地文件\n'
    bundle += 'echo "[offline-install] 重写 bootstrap.sh：远程 curl -> 本地 cp"\n'
    bundle += "export WORK_DIR\npython3 - <<'__PY__'\n"
    bundle += 'import os, pathlib, re\n'
    bundle += "work = os.environ['WORK_DIR']\n"
    bundle += "p = pathlib.Path(work) / 'bootstrap.sh'\n"
    bundle += 's = p.read_text()\n'
    bundle += "for fname, target in [('install_v2.sh', f'{work}/install_v2.sh'),\n"
    bundle += "                       ('sidecar_v2.py', f'{work}/scripts/sidecar_v2.py'),\n"
    bundle += "                       ('cleanup_v1.sh', f'{work}/scripts/cleanup_v1.sh')]:\n"
    bundle += "    pat = r'curl -fsSL -o \"\\$TMP_DIR[^\"]*' + re.escape(fname) + r'\" \\\\\\n\\s*\"\\$HUB_URL/static/skills/hub-sse-sidecar-v2/[^\"]+\"( \\|\\| true)?'\n"
    bundle += "    def _repl(m, t=target, fn=fname):\n"
    bundle += "        sub_dir = 'scripts/' if fn.endswith('.py') or fn == 'cleanup_v1.sh' else ''\n"
    bundle += "        tail = ' || true' if (m.group(1) or '') else ''\n"
    bundle += "        return f'cp \"{t}\" \"$TMP_DIR/{sub_dir}{fn}\"' + tail\n"
    bundle += "    s = re.sub(pat, _repl, s)\n"
    bundle += "p.write_text(s)\nprint('[offline-install] bootstrap.sh 重写完成')\n__PY__\n\n"
    bundle += 'echo "[offline-install] 执行 bootstrap.sh"\n'
    bundle += 'bash "$WORK_DIR/bootstrap.sh" "$@"\n'

    return Response(
        bundle,
        mimetype='text/x-shellscript; charset=utf-8',
        headers={
            'Content-Disposition': f'attachment; filename="openclaw-{claw.id}-offline-install.sh"',
            'Cache-Control': 'no-store',
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
        'llm_provider': claw.llm_provider or DEFAULT_HERMES_LLM_PROVIDER,
        'llm_model': claw.llm_model or DEFAULT_HERMES_LLM_MODEL,
        'wecom': {
            'bot_id': claw.wecom_bot_id or '',
            'secret': claw.get_wecom_bot_secret_plain() or '',
            'enabled': bool(claw.wecom_bot_id and claw.wecom_bot_secret),
        },
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
    claw.last_activity = cst_now_naive()
    db.session.commit()

    today = cst_now_naive().date()

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
    pending_titles = []     # 今日待完成标题（用于分层记忆注入）

    for t in all_todos:
        log = today_logs.get(t.id)
        state = todo_schedule_state(t, today_log=log)
        if log and log.status in TERMINAL_TODO_STATUSES:
            todos_done += 1
        elif state['is_due']:
            todos_pending += 1
            if t.title:
                pending_titles.append(t.title)
            if t.task_category == 'init':
                init_pending += 1
            if t.urgency_level == 'interrupt' and t.schedule_time:
                interrupt_pending.append({
                    'id': t.id,
                    'title': t.title,
                    'time': t.schedule_time,
                    'due_at': state['due_at'],
                    'timezone': state['timezone'],
                })

    # 分层记忆注入（P2）：按今日待办标题聚合命中的公共经验，心跳时提醒
    memory_inject = {'pitfall_alerts': [], 'unread_pitfall_count': 0}
    if pending_titles:
        try:
            from app.services.task_context import build_heartbeat_memory_inject
            memory_inject = build_heartbeat_memory_inject(
                pending_titles[:20], claw=claw, limit=3)
        except Exception:
            pass

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
        'memory_inject': memory_inject,
        'server_time': cst_iso(),
        'timezone': TODO_TIMEZONE,
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
    from app.services.skill_installation import installation_view
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
                'installation': installation_view(s),
            })
    return jsonify({'skills': skills})


def _skill_delivery_task_ref():
    ref_type = request.args.get('ref_type')
    raw_ref_id = request.args.get('ref_id')
    if (ref_type is None) != (raw_ref_id is None):
        from app.services.skill_delivery import SkillDeliveryError
        raise SkillDeliveryError(
            'invalid_task_ref',
            'ref_type 与 ref_id 必须同时提供',
            400,
        )
    if raw_ref_id is None:
        return None, None
    try:
        return ref_type, int(raw_ref_id)
    except (TypeError, ValueError):
        from app.services.skill_delivery import SkillDeliveryError
        raise SkillDeliveryError(
            'invalid_ref_id',
            'ref_id 必须是整数',
            400,
        )


def _skill_delivery_error(exc):
    return jsonify({'error': exc.message, 'code': exc.code}), exc.status_code


def _skill_download_headers(sha256, content_version):
    return {
        'ETag': f'"{sha256}"',
        'X-Skill-Content-Version': content_version,
        'Cache-Control': 'private, max-age=60',
    }


@api_bp.route('/openclaws/<int:claw_id>/skill-manifest', methods=['GET'])
@require_claw_token
def get_skill_manifest(claw_id, claw=None):
    from app.services.skill_delivery import (
        SkillDeliveryError,
        build_skill_manifest,
    )
    try:
        ref_type, ref_id = _skill_delivery_task_ref()
        return jsonify(build_skill_manifest(
            claw,
            ref_type=ref_type,
            ref_id=ref_id,
        ))
    except SkillDeliveryError as exc:
        return _skill_delivery_error(exc)


@api_bp.route('/openclaws/<int:claw_id>/skills/<int:skill_id>/installation-receipts', methods=['POST'])
@require_claw_token
def submit_skill_installation_receipt(claw_id, skill_id, claw=None):
    from app.services.skill_installation import accept_receipt
    from app.services.skill_delivery import SkillDeliveryError
    try:
        result = accept_receipt(claw, skill_id, request.get_json(silent=True))
        db.session.commit()
        return jsonify(result)
    except SkillDeliveryError as exc:
        db.session.rollback()
        return _skill_delivery_error(exc)


@api_bp.route(
    '/openclaws/<int:claw_id>/skills/<int:skill_id>/files/<path:filename>',
    methods=['GET'],
)
@require_claw_token
def download_claw_skill_file(claw_id, skill_id, filename, claw=None):
    from flask import Response
    from app.services.skill_delivery import (
        SkillDeliveryError,
        get_authorized_skill_bundle,
    )
    try:
        ref_type, ref_id = _skill_delivery_task_ref()
        bundle = get_authorized_skill_bundle(
            claw,
            skill_id,
            ref_type=ref_type,
            ref_id=ref_id,
        )
    except SkillDeliveryError as exc:
        return _skill_delivery_error(exc)

    normalized = str(filename or '').replace('\\', '/').lstrip('/')
    item = next(
        (value for value in bundle['files'] if value['path'] == normalized),
        None,
    )
    if item is None:
        return jsonify({
            'error': f'文件 {filename} 不存在',
            'code': 'skill_file_not_found',
        }), 404

    headers = _skill_download_headers(
        item['sha256'],
        bundle['content_version'],
    )
    if request.if_none_match.contains(item['sha256']):
        return Response(status=304, headers=headers)
    suffix = item['path'].lower()
    if suffix.endswith('.md'):
        content_type = 'text/markdown; charset=utf-8'
    elif suffix.endswith('.json'):
        content_type = 'application/json; charset=utf-8'
    else:
        content_type = 'text/plain; charset=utf-8'
    from app.services.skill_usage import record_skill_content_access
    record_skill_content_access(skill_id, 'agent_file')
    return Response(
        item['content'],
        content_type=content_type,
        headers=headers,
    )


@api_bp.route(
    '/openclaws/<int:claw_id>/skills/<int:skill_id>/pack',
    methods=['GET'],
)
@require_claw_token
def download_claw_skill_pack(claw_id, skill_id, claw=None):
    import io
    import zipfile
    from flask import Response
    from werkzeug.utils import secure_filename
    from app.services.skill_delivery import (
        SkillDeliveryError,
        get_authorized_skill_bundle,
    )
    try:
        ref_type, ref_id = _skill_delivery_task_ref()
        bundle = get_authorized_skill_bundle(
            claw,
            skill_id,
            ref_type=ref_type,
            ref_id=ref_id,
        )
    except SkillDeliveryError as exc:
        return _skill_delivery_error(exc)

    headers = _skill_download_headers(
        bundle['sha256'],
        bundle['content_version'],
    )
    if request.if_none_match.contains(bundle['sha256']):
        return Response(status=304, headers=headers)

    skill = bundle['skill']
    root = secure_filename(skill.name) or f'skill-{skill.id}'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for item in bundle['files']:
            archive.writestr(f"{root}/{item['path']}", item['content'])
    headers['Content-Disposition'] = (
        f'attachment; filename="{root}.zip"'
    )
    from app.services.skill_usage import record_skill_content_access
    record_skill_content_access(skill_id, 'agent_pack')
    return Response(
        buffer.getvalue(),
        content_type='application/zip',
        headers=headers,
    )


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
