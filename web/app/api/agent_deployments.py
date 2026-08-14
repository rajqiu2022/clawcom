"""Agent 部署相关 API

提供 Hub 代建 Hermes Agent 的状态查询与重试入口。注意：

- 本接口与 ``openclaws.create_openclaw`` 共享同一套部署器
  （:mod:`app.services.agent_deployer`），只在 OpenClaw 已存在时使用。
- 高危操作（重试部署 / 查看部署日志）权限对齐 ``_can_own_claw``：
  只有 super_admin / 绑定者 / owner 才能触发，避免项目管理员替别人重装 agent。
- SSH 凭据 / Venus API Key 永远只活在本次请求里，不落库。
"""

from __future__ import annotations

import logging
import os
import re
import shlex

from flask import current_app, jsonify, request

from app import db
from app.api import api_bp
from app.api.openclaws import (
    _actor_display_name,
    _can_own_claw,
    _get_user,
)
from app.models import AgentDeployment, OpenClawInstance
from app.hermes_models import (
    DEFAULT_HERMES_LLM_PROVIDER,
    DEFAULT_HERMES_LLM_MODEL,
    DEFAULT_TIMIAI_LLM_MODEL,
    normalize_hermes_model,
    normalize_hermes_provider,
)
from app.services.hermes_timiai_projects import (
    TIMIAI_PROJECTS,
    normalize_timiai_project,
    timiai_project_label,
)
from app.services.deployment_secrets import get_deployment_secret
from app.services.agent_deployer import (
    AGENT_SERVICE_GROUP,
    AGENT_ROOT_DIR,
    DeployRequest,
    _SSHRunner,
    _render_config_yaml,
    _render_env_file,
    _render_sidecar_env,
    _render_sidecar_unit,
    _render_sidecar_wrapper,
    _render_systemd_unit,
    _work_dir_setup_cmd,
    build_container_name,
    build_default_systemd_data_dir,
    build_remote_base_dir,
    build_systemd_unit_name,
    normalize_agent_work_dirs,
    spawn_deploy_async,
)

logger = logging.getLogger(__name__)
_SYSTEMD_USER_RE = re.compile(r'^[a-z_][a-z0-9_-]{0,30}$')
_SENSITIVE_DEPLOY_ENV_RE = re.compile(
    r'(?:API_?KEY|TOKEN|SECRET|PASSWORD|AUTHORIZATION|PRIVATE_?KEY)',
    re.IGNORECASE,
)
DEFAULT_HERMES_INSTALL_DIR = '/opt/hermes-runtime'
DEFAULT_CODEX_RUNTIME_DIR = '/opt/codex-runtime'
_SHA256_RE = re.compile(r'^[0-9a-fA-F]{64}$')


def _system_config_values(keys) -> dict:
    """Read selected system_config keys. Keys are code constants, not user input."""
    if not keys:
        return {}
    try:
        from sqlalchemy import text
        placeholders = ','.join([f':k{i}' for i, _ in enumerate(keys)])
        params = {f'k{i}': key for i, key in enumerate(keys)}
        rows = db.session.execute(text(
            f"SELECT config_key, value FROM system_config WHERE config_key IN ({placeholders})"
        ), params).fetchall()
        return {k: v for k, v in rows}
    except Exception:
        logger.exception('读取 system_config 配置失败: %s', keys)
        return {}


def _configured_timiai_api_key(project: str = '') -> str:
    """从部署密钥箱读取按项目区分的 Hermes TimiAI API Key。"""
    project_key = normalize_timiai_project(project)
    meta = TIMIAI_PROJECTS[project_key]
    keys = list(meta['config_keys'])
    if project_key in ('gbt', 'qqspeed_pc'):
        keys.extend((
            'deploy_timiai_api_key',
            'hermes_timiai_api_key',
            'timiai_api_key',
        ))
    return get_deployment_secret(keys)


def _configured_venus_api_key() -> str:
    """从部署密钥箱读取共享的 Hermes Venus API Key。"""
    return get_deployment_secret((
        'deploy_venus_api_key',
        'hermes_venus_api_key',
        'venus_api_key',
    ))


def _default_hermes_install_dir() -> str:
    rows = _system_config_values((
        'deploy_default_hermes_install_dir',
        'hermes_install_dir',
    ))
    return (
        os.getenv('DEPLOY_DEFAULT_HERMES_INSTALL_DIR')
        or os.getenv('HERMES_INSTALL_DIR')
        or rows.get('deploy_default_hermes_install_dir')
        or rows.get('hermes_install_dir')
        or DEFAULT_HERMES_INSTALL_DIR
    ).strip().rstrip('/')


def _build_remote_base_dir_compat(claw: OpenClawInstance, safe_name: str = '') -> str:
    """兼容旧版 agent_deployer（无 safe_name 参数）。"""
    try:
        return build_remote_base_dir(
            claw.id, claw.name or f'claw-{claw.id}', safe_name=safe_name
        )
    except TypeError:
        return build_remote_base_dir(claw.id, claw.name or f'claw-{claw.id}')


def _build_default_data_dir_compat(claw: OpenClawInstance, safe_name: str = '') -> str:
    """兼容旧版 build_default_systemd_data_dir（无 safe_name 参数）。"""
    try:
        return build_default_systemd_data_dir(
            claw.id, claw.name or f'claw-{claw.id}', safe_name=safe_name
        )
    except TypeError:
        return build_default_systemd_data_dir(claw.id, claw.name or f'claw-{claw.id}')


def _deployment_defaults() -> dict:
    rows = _system_config_values((
        'deploy_default_host',
        'deploy_default_ssh_port',
        'deploy_default_ssh_user',
    ))
    return {
        'host': (os.getenv('DEPLOY_DEFAULT_HOST') or rows.get('deploy_default_host') or '').strip(),
        'ssh_port': (os.getenv('DEPLOY_DEFAULT_SSH_PORT') or rows.get('deploy_default_ssh_port') or '').strip(),
        'ssh_user': (os.getenv('DEPLOY_DEFAULT_SSH_USER') or rows.get('deploy_default_ssh_user') or 'root').strip(),
        'ssh_password': get_deployment_secret((
            'deploy_default_ssh_password',
            'deploy_ssh_password',
        )) or None,
        'ssh_private_key': get_deployment_secret((
            'deploy_default_ssh_key',
            'deploy_ssh_private_key',
        )) or None,
        'ssh_key_passphrase': get_deployment_secret((
            'deploy_default_ssh_key_passphrase',
            'deploy_ssh_key_passphrase',
        )) or None,
    }


def _deployment_host_port(dep: AgentDeployment, defaults: dict) -> tuple[str, int]:
    raw_host = (dep.host or defaults.get('host') or '').strip()
    raw_port = defaults.get('ssh_port') or ''
    if ':' in raw_host:
        host_part, port_part = raw_host.rsplit(':', 1)
        if port_part.isdigit():
            return host_part.strip(), int(port_part)
    try:
        port = int(raw_port or 22)
    except (TypeError, ValueError):
        port = 22
    return raw_host, port


def _restart_systemd_agent(claw: OpenClawInstance, actor_name: str) -> dict:
    """Restart the latest successful Hub-managed systemd provider."""
    dep = (AgentDeployment.query
           .filter_by(openclaw_id=claw.id, deploy_method='systemd', status='success')
           .order_by(AgentDeployment.created_at.desc())
           .first())
    if not dep:
        raise ValueError('该 OpenClaw 没有可重启的 Hub 代建 Agent')

    defaults = _deployment_defaults()
    host, ssh_port = _deployment_host_port(dep, defaults)
    ssh_user = (dep.ssh_user or defaults.get('ssh_user') or 'root').strip()
    ssh_password = defaults.get('ssh_password')
    ssh_private_key = defaults.get('ssh_private_key')
    if not host:
        raise ValueError('重启失败：未配置部署目标机')
    if not ssh_password and not ssh_private_key:
        raise ValueError('重启失败：未配置 SSH 凭据')

    unit = dep.container_name or build_systemd_unit_name(claw.id)
    req = DeployRequest(
        openclaw_id=claw.id,
        claw_name=claw.name or f'claw-{claw.id}',
        claw_token='',
        hub_url='',
        host=host,
        ssh_user=ssh_user,
        ssh_port=ssh_port,
        ssh_password=ssh_password,
        ssh_private_key=ssh_private_key,
        ssh_key_passphrase=defaults.get('ssh_key_passphrase'),
        deploy_method='systemd',
        agent_type=dep.agent_type or 'hermes',
        triggered_by=actor_name,
    )

    if dep.agent_type == 'codex':
        with _SSHRunner(req) as ssh:
            restart = ssh.run(
                f"systemctl restart {shlex.quote(unit)} && "
                f"systemctl is-active {shlex.quote(unit)}",
                timeout=60, name='restart codex sidecar')
            if not restart.ok or restart.stdout.strip() != 'active':
                journal = ssh.run(
                    f"journalctl -u {shlex.quote(unit)} --no-pager -n 80",
                    timeout=30, name='journalctl codex sidecar')
                raise RuntimeError(
                    f'Codex sidecar 重启失败：{(journal.stdout or journal.stderr)[-1200:]}')
        return {
            'unit': unit,
            'host': f'{host}:{ssh_port}' if ssh_port != 22 else host,
            'status': 'active',
            'agent_type': 'codex',
            'config_synced': False,
        }

    with _SSHRunner(req) as ssh:
        deploy_req = _parse_deploy_options(
            {
                'deploy_method': 'systemd',
                'llm_provider': claw.llm_provider or DEFAULT_HERMES_LLM_PROVIDER,
                'llm_model': claw.llm_model or DEFAULT_HERMES_LLM_MODEL,
                'work_dirs': claw.work_dirs or [],
                'hermes_start_mode': 'gateway',
            },
            claw=claw,
            claw_token=claw.get_token_plain() or '',
            hub_url=request.host_url.rstrip('/'),
            actor_name=actor_name,
        )
        data = deploy_req.systemd_data_dir()
        cfg_path = f'{data}/config.yaml' if deploy_req.systemd_split_layout() else f'{data}/config/config.yaml'
        env_path = f'{data}/.env' if deploy_req.systemd_split_layout() else f'{data}/config/.env'
        wrapper_path = f'{data}/scripts/hermes_sidecar_wrapper.sh'
        sidecar_script = f'{data}/scripts/sidecar_v2.py'
        sidecar_env_path = f'{data}/scripts/sidecar.env'
        service_user = deploy_req.systemd_service_user()
        sidecar_unit = f'openclaw-sidecar-v2-claw-{int(claw.id)}.service'
        sidecar_unit_path = f'/etc/systemd/system/{sidecar_unit}'
        unit_path = f'/etc/systemd/system/{unit}'

        # 先修复运行目录权限，避免 ExecStartPre 在 data/sessions|logs 上权限失败。
        runtime_dirs = [data, f'{data}/home', f'{data}/sessions', f'{data}/logs', f'{data}/scripts']
        runtime_fix_cmd = (
            f"mkdir -p {' '.join(shlex.quote(p) for p in runtime_dirs)} && "
            f"chown -R {shlex.quote(service_user)}:{shlex.quote(AGENT_SERVICE_GROUP)} {shlex.quote(data)} && "
            f"chmod 0700 {shlex.quote(data)} {shlex.quote(data + '/home')} "
            f"{shlex.quote(data + '/sessions')} {shlex.quote(data + '/logs')} {shlex.quote(data + '/scripts')}"
        )
        runtime_fix = ssh.run(runtime_fix_cmd, timeout=30, name='fix hermes runtime dir ownership')
        if not runtime_fix.ok:
            raise RuntimeError(f'修复运行目录权限失败：{(runtime_fix.stderr or runtime_fix.stdout)[-800:]}')

        # sidecar v2 脚本/环境文件在 restart 时也要补齐，避免 "Failed to load environment files"。
        sidecar_url = (
            f"{(deploy_req.hub_url or '').rstrip('/')}"
            "/static/skills/hub-sse-sidecar-v2/scripts/sidecar_v2.py"
        )
        download_cmd = (
            f"SIDECAR_URL={shlex.quote(sidecar_url)} "
            f"SIDECAR_PATH={shlex.quote(sidecar_script)} "
            "python3 - <<'PY'\n"
            "import os, urllib.request\n"
            "url = os.environ['SIDECAR_URL']\n"
            "path = os.environ['SIDECAR_PATH']\n"
            "with urllib.request.urlopen(url, timeout=30) as r:\n"
            "    data = r.read()\n"
            "if not data.startswith(b'#!/usr/bin/env python3'):\n"
            "    raise SystemExit('unexpected sidecar content')\n"
            "with open(path, 'wb') as f:\n"
            "    f.write(data)\n"
            "PY\n"
            f"chmod 0755 {shlex.quote(sidecar_script)}"
        )
        sidecar_download = ssh.run(download_cmd, timeout=45, name='download sidecar_v2.py')
        if not sidecar_download.ok:
            raise RuntimeError(f'下载 sidecar_v2.py 失败：{(sidecar_download.stderr or sidecar_download.stdout)[-800:]}')

        for content, path, mode, name in (
                (_render_config_yaml(deploy_req), cfg_path, '0644', 'sync config.yaml'),
                (_render_env_file(deploy_req), env_path, '0600', 'sync .env'),
                (_render_sidecar_wrapper(deploy_req), wrapper_path, '0755', 'sync sidecar wrapper'),
                (_render_sidecar_env(deploy_req), sidecar_env_path, '0600', 'sync sidecar env'),
                (_render_systemd_unit(deploy_req), unit_path, '0644', 'sync hermes unit'),
                (_render_sidecar_unit(deploy_req), sidecar_unit_path, '0644', 'sync sidecar unit')):
            sr = ssh.put_text(content, path, mode=mode, name=name)
            if not sr.ok:
                raise RuntimeError(f'{name} 失败：{(sr.stderr or sr.stdout)[-800:]}')

        work_dirs = ssh.run(
            _work_dir_setup_cmd(deploy_req),
            timeout=30, name='sync agent work dirs')
        if not work_dirs.ok:
            raise RuntimeError(f'同步工作目录权限失败：{(work_dirs.stderr or work_dirs.stdout)[-800:]}')

        sync = ssh.run(
            f"chown {shlex.quote(service_user)}:openclaw_agents "
            f"{shlex.quote(cfg_path)} {shlex.quote(env_path)} {shlex.quote(wrapper_path)} "
            f"{shlex.quote(sidecar_env_path)} {shlex.quote(sidecar_script)} && "
            f"chmod 0644 {shlex.quote(cfg_path)} && "
            f"chmod 0600 {shlex.quote(env_path)} && "
            f"chmod 0755 {shlex.quote(wrapper_path)} {shlex.quote(sidecar_script)} && "
            f"chmod 0600 {shlex.quote(sidecar_env_path)}",
            timeout=20, name='chown synced hermes files')
        if not sync.ok:
            raise RuntimeError(f'同步配置权限失败：{(sync.stderr or sync.stdout)[-800:]}')

        reload_sr = ssh.run('systemctl daemon-reload', timeout=30, name='systemctl daemon-reload')
        if not reload_sr.ok:
            raise RuntimeError(f'daemon-reload 失败：{(reload_sr.stderr or reload_sr.stdout)[-800:]}')

        restart = ssh.run(
            f"systemctl restart {shlex.quote(unit)}",
            timeout=45, name='systemctl restart hermes unit')
        sidecar_restart = ssh.run(
            f"systemctl restart {shlex.quote(sidecar_unit)}",
            timeout=45, name='systemctl restart sidecar unit')
        active = ssh.run(
            f"systemctl is-active {shlex.quote(unit)}",
            timeout=20, name='systemctl is-active hermes unit')
        sidecar_active = ssh.run(
            f"systemctl is-active {shlex.quote(sidecar_unit)}",
            timeout=20, name='systemctl is-active sidecar unit')
        if (not restart.ok or not sidecar_restart.ok or
                not active.ok or active.stdout.strip() != 'active' or
                not sidecar_active.ok or sidecar_active.stdout.strip() != 'active'):
            journal = ssh.run(
                f"journalctl -u {shlex.quote(unit)} --no-pager -n 80",
                timeout=30, name='journalctl hermes unit')
            raise RuntimeError(
                'Hermes Agent 重启失败：'
                f"restart={restart.exit_code}, active={active.stdout.strip() or active.stderr.strip()}; "
                f"{(journal.stdout or journal.stderr)[-1200:]}"
            )

    return {
        'unit': unit,
        'sidecar_unit': f'openclaw-sidecar-v2-claw-{int(claw.id)}.service',
        'host': f'{host}:{ssh_port}' if ssh_port != 22 else host,
        'status': 'active',
        'config_synced': True,
        'llm_provider': normalize_hermes_provider(claw.llm_provider or DEFAULT_HERMES_LLM_PROVIDER),
        'llm_model': normalize_hermes_model(
            claw.llm_model or DEFAULT_HERMES_LLM_MODEL,
            claw.llm_provider or DEFAULT_HERMES_LLM_PROVIDER),
    }


def _path_in_agent_root(path: str, claw: OpenClawInstance) -> bool:
    """检查路径是否落在本 claw 的私有 /opt/openclaw-agents 子树内。"""
    if not path:
        return False
    # 优先使用 claw.safe_name（数据库中存储的，第一次创建时生成，后续不变）
    safe = claw.safe_name or ''
    base = _build_remote_base_dir_compat(claw, safe_name=safe).rstrip('/')
    cleaned = path.rstrip('/')
    if cleaned == base or cleaned.startswith(base + '/'):
        return True
    # 兼容历史目录：claw 名称变更后，旧目录可能仍为 claw-{id}-old-safe。
    legacy_prefix = f"{AGENT_ROOT_DIR}/claw-{int(claw.id)}-"
    return cleaned.startswith(legacy_prefix)


def _parse_deploy_options(data: dict, claw: OpenClawInstance,
                          claw_token: str, hub_url: str,
                          actor_name: str) -> DeployRequest:
    """把前端传过来的 deploy 子对象解析成 :class:`DeployRequest`。

    通用必填：``host``、``ssh_user``、(``ssh_password`` 或 ``ssh_private_key``)。
    ``venus_api_key`` 默认从 Hub 服务端配置读取，不要求前端提交。

    Hub 代建 Hermes Agent 统一使用 ``deploy_method='systemd'``：

    - ``systemd``：与《Hermes Agent 标准化部署指南》v2 + OS 写入隔离一致——
      推荐同时传 ``hermes_install_dir``（已准备好的 venv/源码，只读使用）与
      ``hermes_data_dir``（必须位于本 claw 的 ``/opt/openclaw-agents/...`` 子目录）；
      或仅传 ``hermes_home`` 单目录兼容旧布局。可选 ``hermes_start_mode``：
      ``gateway``（默认，拆分目录时）/ ``module``（``python -m hermes_module``，
      单目录默认）。另有 ``hermes_python`` / ``hermes_module`` / ``systemd_user``。
    """
    if not isinstance(data, dict):
        raise ValueError('deploy 参数必须是 JSON 对象')

    deploy_method = (data.get('deploy_method') or 'systemd').strip().lower()
    if deploy_method != 'systemd':
        raise ValueError('Hub 统一部署器当前仅支持 Linux systemd 隔离部署')
    agent_type = (data.get('agent_type') or 'hermes').strip().lower()
    if agent_type == 'pi':
        raise ValueError('Pi provider 已退役；请选择 hermes 或 codex')
    if agent_type not in ('hermes', 'codex'):
        raise ValueError('agent_type 仅支持 hermes 或 codex')

    # 支持从环境变量 / system_config 读取默认部署配置（前端简化后不提交 host/ssh 信息）
    defaults = _deployment_defaults()
    host = (data.get('host') or defaults['host']).strip()
    ssh_user = (data.get('ssh_user') or defaults['ssh_user'] or 'root').strip()
    ssh_password = defaults['ssh_password'] or None
    ssh_private_key = defaults['ssh_private_key'] or None
    ssh_key_passphrase = defaults['ssh_key_passphrase'] or None
    venus_api_key = _configured_venus_api_key() or None
    llm_provider = normalize_hermes_provider(
        data.get('llm_provider') or getattr(claw, 'llm_provider', None)
        or DEFAULT_HERMES_LLM_PROVIDER)
    timiai_project = normalize_timiai_project(
        data.get('timiai_project') or getattr(claw, 'timiai_project', None))
    timiai_api_key = _configured_timiai_api_key(timiai_project) or None
    default_model = (DEFAULT_HERMES_LLM_MODEL if llm_provider == 'venus'
                     else DEFAULT_TIMIAI_LLM_MODEL)
    llm_model = normalize_hermes_model(
        data.get('llm_model') or getattr(claw, 'llm_model', None) or default_model,
        llm_provider)
    image = (data.get('image') or '').strip() \
        or 'ccr.ccs.tencentyun.com/hermes/hermes-agent:latest'

    if not host:
        raise ValueError('host 必填；可在服务端配置 DEPLOY_DEFAULT_HOST')
    if not ssh_password and not ssh_private_key:
        raise ValueError('SSH 凭据未配置：请在 Hub 密钥箱配置 deploy_default_ssh_password 或 deploy_default_ssh_key')
    if agent_type == 'hermes' and llm_provider == 'venus' and not venus_api_key:
        raise ValueError(
            'Venus API Key 未配置：请在 Hub 密钥箱配置 deploy_venus_api_key')
    if agent_type == 'hermes' and llm_provider == 'timiai' and not timiai_api_key:
        raise ValueError(
            'TimiAI API Key 未配置：请在 Hub 密钥箱配置项目 %s 对应的密钥' %
            timiai_project_label(timiai_project))

    hermes_home = (data.get('hermes_home') or '').strip().rstrip('/') or None
    hermes_install_dir = (data.get('hermes_install_dir') or '').strip().rstrip('/') or None
    hermes_data_dir = (data.get('hermes_data_dir') or '').strip().rstrip('/') or None
    hermes_python = (data.get('hermes_python') or '').strip() or None
    hermes_module = (data.get('hermes_module') or '').strip() or 'hermes_agent'
    raw_systemd_user = (data.get('systemd_user') or '').strip()
    systemd_user = raw_systemd_user if raw_systemd_user and raw_systemd_user != 'root' else ''
    if systemd_user and not _SYSTEMD_USER_RE.match(systemd_user):
        raise ValueError('systemd_user 只能包含小写字母、数字、下划线或短横线，且不能以数字开头')
    raw_start = (data.get('hermes_start_mode') or '').strip().lower()
    work_dirs = normalize_agent_work_dirs(
        data.get('work_dirs', getattr(claw, 'work_dirs', None) or []))

    safe = claw.safe_name or ''
    codex_runtime_dir = (data.get('codex_runtime_dir') or DEFAULT_CODEX_RUNTIME_DIR).strip().rstrip('/')
    codex_data_dir = (data.get('codex_data_dir') or
                      f"{_build_remote_base_dir_compat(claw, safe_name=safe)}/data").strip().rstrip('/')
    codex_python = (data.get('codex_python') or '').strip() or None
    codex_cli = (data.get('codex_cli') or '/usr/local/bin/codex').strip()
    codex_workspace = (data.get('codex_workspace') or '').strip().rstrip('/') or None
    codex_model = (data.get('codex_model') or '').strip()
    codex_requirements = (data.get('codex_requirements') or
                          f'{codex_runtime_dir}/requirements-codex.lock').strip()
    codex_wheelhouse = (data.get('codex_wheelhouse') or
                        f'{codex_runtime_dir}/wheelhouse').strip().rstrip('/')
    codex_requirements_sha256 = (data.get('codex_requirements_sha256') or '').strip().lower()
    codex_auth_mode = (data.get('codex_auth_mode') or 'chatgpt_subscription').strip().lower()

    if deploy_method == 'systemd' and agent_type == 'codex':
        if not codex_workspace:
            raise ValueError('codex_workspace 必填，且必须是目标 Linux 主机上的工程绝对路径')
        for path, label in (
                (codex_runtime_dir, 'codex_runtime_dir'),
                (codex_data_dir, 'codex_data_dir'),
                (codex_cli, 'codex_cli'),
                (codex_workspace, 'codex_workspace'),
                (codex_requirements, 'codex_requirements'),
                (codex_wheelhouse, 'codex_wheelhouse')):
            if not path.startswith('/') or any(ch.isspace() for ch in path) or '%' in path:
                raise ValueError(f'{label} 必须是不含空格或 % 的 Linux 绝对路径')
        if codex_python and not codex_python.startswith('/'):
            raise ValueError('codex_python 必须为 Linux 绝对路径')
        if codex_model and not re.fullmatch(r'[A-Za-z0-9._:/-]{1,128}', codex_model):
            raise ValueError('codex_model 只能包含字母、数字、点、下划线、冒号、斜杠或短横线')
        if not _path_in_agent_root(codex_data_dir, claw):
            raise ValueError(
                f'codex_data_dir 必须位于 {_build_remote_base_dir_compat(claw, safe_name=safe)}/ 下')
        normalize_agent_work_dirs([codex_workspace])
        if not _SHA256_RE.fullmatch(codex_requirements_sha256):
            raise ValueError('codex_requirements_sha256 必填，且必须是 64 位十六进制 SHA-256')
        if codex_auth_mode not in (
                'chatgpt_subscription', 'api_key', 'enterprise_access_token'):
            raise ValueError(
                'codex_auth_mode 仅支持 chatgpt_subscription、api_key、enterprise_access_token')
    elif deploy_method == 'systemd':
        if not hermes_home and not hermes_install_dir:
            hermes_install_dir = _default_hermes_install_dir()
        # 优先使用已有的 hermes_data_dir（从数据库读取），避免重新生成导致目录名变化
        if not hermes_home and not hermes_data_dir:
            # 检查是否已有部署记录，且 hermes_data_dir 不为空
            existing_dep = AgentDeployment.query.filter_by(
                openclaw_id=claw.id
            ).order_by(AgentDeployment.id.desc()).first()
            if existing_dep and existing_dep.remote_base_dir:
                # 从 remote_base_dir 提取 data 目录（格式：/opt/openclaw-agents/claw-{id}-xxx/data (venv:...)）
                import re
                m = re.match(r'^(/\S+?/data)', existing_dep.remote_base_dir)
                if m:
                    hermes_data_dir = m.group(1)
                else:
                    # 优先使用 claw.safe_name（数据库中存储的，第一次创建时生成，后续不变）
                    safe = claw.safe_name or ''
                    hermes_data_dir = _build_default_data_dir_compat(claw, safe_name=safe)
            else:
                # 优先使用 claw.safe_name
                safe = claw.safe_name or ''
                hermes_data_dir = _build_default_data_dir_compat(claw, safe_name=safe)
        if hermes_home and (hermes_install_dir or hermes_data_dir):
            raise ValueError('请勿同时填写 hermes_home 与 hermes_install_dir/hermes_data_dir')
        if hermes_install_dir and not hermes_data_dir:
            raise ValueError('拆分布局需同时填写 hermes_data_dir（HERMES_HOME，必须在 /opt/openclaw-agents/<claw>/ 下）')
        if hermes_data_dir and not hermes_install_dir:
            raise ValueError('拆分布局需同时填写 hermes_install_dir（venv 根目录，如 /opt/hermes-xiaohe）')
        if not hermes_home and not (hermes_install_dir and hermes_data_dir):
            raise ValueError(
                'systemd：请填写「安装目录 + 数据目录」（推荐，见 Hermes 部署指南 v2），'
                '或仅填 hermes_home（单目录兼容旧机）'
            )
        for path, label in (
                (hermes_home, 'hermes_home'),
                (hermes_install_dir, 'hermes_install_dir'),
                (hermes_data_dir, 'hermes_data_dir')):
            if path and not path.startswith('/'):
                raise ValueError(f'{label} 必须为绝对路径')
        work_dir = hermes_home or hermes_data_dir
        if not _path_in_agent_root(work_dir, claw):
            # 优先使用 claw.safe_name（数据库中存储的，第一次创建时生成，后续不变）
            safe = claw.safe_name or ''
            private_base = _build_remote_base_dir_compat(claw, safe_name=safe)
            raise ValueError(
                f'agent 私有工作目录必须位于 {private_base}/ 下；'
                f'即 {AGENT_ROOT_DIR} 的本 claw 子目录。共享目录固定为 /opt/agent_share，'
                f'不能使用 /root 或其他系统目录'
            )

    if raw_start in ('gateway', 'module'):
        hermes_start_mode = raw_start
    elif deploy_method == 'systemd' and hermes_install_dir and hermes_data_dir:
        hermes_start_mode = 'gateway'
    elif deploy_method == 'systemd':
        hermes_start_mode = 'module'
    else:
        hermes_start_mode = 'gateway'

    try:
        ssh_port = int(data.get('ssh_port') or defaults['ssh_port'] or 22)
    except (TypeError, ValueError):
        ssh_port = 22

    extra_env = data.get('extra_env') or {}
    if not isinstance(extra_env, dict):
        extra_env = {}
    blocked_env = sorted(
        str(key) for key in extra_env if _SENSITIVE_DEPLOY_ENV_RE.search(str(key))
    )
    if blocked_env:
        raise ValueError(
            'extra_env 不允许传入敏感字段，请改存 Hub 密钥箱：%s' %
            ', '.join(blocked_env)
        )

    return DeployRequest(
        openclaw_id=claw.id,
        claw_name=claw.name or f'claw-{claw.id}',
        claw_token=claw_token or '',
        hub_url=hub_url,
        host=host,
        ssh_user=ssh_user,
        ssh_port=ssh_port,
        ssh_password=ssh_password,
        ssh_private_key=ssh_private_key,
        ssh_key_passphrase=ssh_key_passphrase,
        deploy_method=deploy_method,
        agent_type=agent_type,
        image=image,
        hermes_home=hermes_home,
        hermes_install_dir=hermes_install_dir,
        hermes_data_dir=hermes_data_dir,
        hermes_python=hermes_python,
        hermes_module=hermes_module,
        hermes_start_mode=hermes_start_mode,
        systemd_user=systemd_user,
        codex_runtime_dir=codex_runtime_dir,
        codex_data_dir=codex_data_dir,
        codex_python=codex_python,
        codex_cli=codex_cli,
        codex_workspace=codex_workspace,
        codex_model=codex_model,
        codex_requirements=codex_requirements,
        codex_wheelhouse=codex_wheelhouse,
        codex_requirements_sha256=codex_requirements_sha256,
        codex_auth_mode=codex_auth_mode,
        venus_api_key=venus_api_key,
        timiai_api_key=timiai_api_key,
        timiai_project=timiai_project,
        llm_provider=llm_provider,
        llm_model=llm_model,
        wecom_bot_id=claw.wecom_bot_id or '',
        wecom_bot_secret=claw.get_wecom_bot_secret_plain() or '',
        owner_wecom_userid=(claw.owner_wecom_userid or claw.owner or '').strip(),
        work_dirs=work_dirs,
        extra_env=extra_env,
        triggered_by=actor_name,
    )


def create_deployment_record(claw: OpenClawInstance, req: DeployRequest) -> AgentDeployment:
    """根据 :class:`DeployRequest` 建一条 ``AgentDeployment`` 记录。

    敏感字段（密码 / 私钥 / Venus key）**不会** 写到表里，只把 host / 目录 /
    容器名（或 systemd unit） / 镜像 / 触发人留痕。

    docker 模式：``container_name`` 写容器名，``image`` 写镜像，``remote_base_dir``
    走 ``/opt/openclaw-agents/claw-<id>-<safe>``。

    systemd 模式：``container_name`` 复用为 ``hermes-gateway-claw-<id>.service``，
    ``image`` 留空，``remote_base_dir`` 记数据目录及安装目录摘要。
    """
    if req.deploy_method == 'systemd':
        data = req.systemd_data_dir()
        inst = req.systemd_install_dir()
        remote_base = f"{data} (venv:{inst})"
        if len(remote_base) > 500:
            remote_base = remote_base[:497] + '...'
        container = req.container_name()
        image = ''
    else:
        # 优先使用 claw.safe_name（数据库中存储的，第一次创建时生成，后续不变）
        safe = claw.safe_name or ''
        remote_base = _build_remote_base_dir_compat(claw, safe_name=safe)
        container = build_container_name(claw.id)
        image = req.image

    dep = AgentDeployment(
        openclaw_id=claw.id,
        agent_type=req.agent_type,
        deploy_method=req.deploy_method,
        host=f'{req.host}:{req.ssh_port}' if req.ssh_port and req.ssh_port != 22 else req.host,
        ssh_user=req.ssh_user,
        remote_base_dir=remote_base,
        container_name=container,
        image=image,
        status='pending',
        triggered_by=req.triggered_by,
    )
    db.session.add(dep)
    db.session.commit()
    return dep


def trigger_async_deployment(claw: OpenClawInstance, req: DeployRequest) -> AgentDeployment:
    """对外的"创建记录 + 异步执行"封装。OpenClaw 注册流程也调它。"""
    dep = create_deployment_record(claw, req)
    spawn_deploy_async(current_app._get_current_object(), dep.id, req)
    return dep


# ---------- HTTP 接口 ----------


@api_bp.route('/openclaws/<int:claw_id>/agent-deployments', methods=['POST'])
def create_agent_deployment(claw_id: int):
    """重新部署一个 OpenClaw 对应的 Hermes/Codex Agent。

    OpenClaw 注册时如果已经选择了 ``create_agent``，第一次部署会从
    ``create_openclaw`` 内部直接触发；本接口主要用在「失败后重试」 /
    「换台机器重装」的场景。

    权限：**仅 super_admin**。代建 Agent 涉及对远端机器的 SSH + Docker 操作，
    属于高危运维动作，项目管理员/绑定者/owner 都不能触发，
    避免误装到错误目标机或踩别人 claw 的容器名。
    """
    claw = OpenClawInstance.query.get_or_404(claw_id)
    user = _get_user()
    if not user or user.role != 'super_admin':
        return jsonify({'error': '仅超级管理员可代建 / 重新部署 Agent'}), 403

    data = request.get_json(silent=True) or {}
    deploy_opts = data.get('deploy') if isinstance(data.get('deploy'), dict) else data

    hub_url = (data.get('hub_url') or request.host_url.rstrip('/')).rstrip('/')

    try:
        req = _parse_deploy_options(
            deploy_opts,
            claw=claw,
            claw_token=claw.get_token_plain() or '',
            hub_url=hub_url,
            actor_name=_actor_display_name(user),
        )
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    dep = trigger_async_deployment(claw, req)
    return jsonify({
        'message': '部署任务已下发，请稍后查询状态',
        'deployment': dep.to_dict(),
    }), 202


@api_bp.route('/openclaws/<int:claw_id>/agent/restart', methods=['POST'])
def restart_openclaw_agent(claw_id: int):
    """重启 Hub 代建的 Hermes/Codex systemd 服务。"""
    claw = OpenClawInstance.query.get_or_404(claw_id)
    user = _get_user()
    if not _can_own_claw(user, claw):
        return jsonify({'error': '仅本人 / 绑定 OpenClaw / 超级管理员可重启 Agent'}), 403
    has_managed = (AgentDeployment.query
                   .filter_by(openclaw_id=claw.id, deploy_method='systemd', status='success')
                   .first())
    if not has_managed:
        return jsonify({'error': '仅通过 Hub 注册部署成功的 Agent 可重启'}), 403

    try:
        result = _restart_systemd_agent(claw, _actor_display_name(user))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logger.exception('restart managed agent failed for claw %s', claw_id)
        return jsonify({'error': str(e)}), 500

    return jsonify({
        'message': 'Agent 已重启',
        'restart': result,
    }), 200


@api_bp.route('/openclaws/<int:claw_id>/agent-deployments/latest', methods=['GET'])
def get_latest_agent_deployment(claw_id: int):
    """查询某 OpenClaw 的最近一次部署记录（用于前端轮询）。"""
    claw = OpenClawInstance.query.get_or_404(claw_id)
    user = _get_user()
    if not _can_own_claw(user, claw):
        return jsonify({'error': '仅本 OpenClaw 的绑定者 / 创建者 / 超级管理员可查看部署详情'}), 403

    dep = (AgentDeployment.query
           .filter_by(openclaw_id=claw.id)
           .order_by(AgentDeployment.created_at.desc())
           .first())
    if not dep:
        return jsonify({'deployment': None}), 200
    return jsonify({'deployment': dep.to_dict()}), 200


@api_bp.route('/openclaws/<int:claw_id>/agent-deployments', methods=['GET'])
def list_agent_deployments(claw_id: int):
    """查询某 OpenClaw 的全部部署历史（按时间倒序）。"""
    claw = OpenClawInstance.query.get_or_404(claw_id)
    user = _get_user()
    if not _can_own_claw(user, claw):
        return jsonify({'error': '仅本 OpenClaw 的绑定者 / 创建者 / 超级管理员可查看部署详情'}), 403

    limit = max(1, min(int(request.args.get('limit') or 20), 100))
    deps = (AgentDeployment.query
            .filter_by(openclaw_id=claw.id)
            .order_by(AgentDeployment.created_at.desc())
            .limit(limit)
            .all())
    return jsonify({'deployments': [d.to_dict() for d in deps]}), 200
