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

from flask import current_app, jsonify, request

from app import db
from app.api import api_bp
from app.api.openclaws import _actor_display_name, _can_own_claw, _get_user
from app.models import AgentDeployment, OpenClawInstance
from app.services.agent_deployer import (
    DeployRequest,
    build_container_name,
    build_remote_base_dir,
    build_systemd_unit_name,
    spawn_deploy_async,
)

logger = logging.getLogger(__name__)


def _parse_deploy_options(data: dict, claw: OpenClawInstance,
                          claw_token: str, hub_url: str,
                          actor_name: str) -> DeployRequest:
    """把前端传过来的 deploy 子对象解析成 :class:`DeployRequest`。

    通用必填：``host``、``ssh_user``、(``ssh_password`` 或 ``ssh_private_key``)、
    ``venus_api_key``。

    按 ``deploy_method`` 还有额外要求：

    - ``docker``（默认）：可选 ``image``，缺省走 hermes-agent:latest
    - ``systemd``：必填 ``hermes_home``（目标机已装好 Hermes 的根目录），
      可选 ``hermes_python`` / ``hermes_module`` / ``systemd_user``
    """
    if not isinstance(data, dict):
        raise ValueError('deploy 参数必须是 JSON 对象')

    deploy_method = (data.get('deploy_method') or 'docker').strip().lower()
    if deploy_method not in ('docker', 'systemd'):
        raise ValueError(f'不支持的 deploy_method：{deploy_method}（仅支持 docker / systemd）')

    host = (data.get('host') or '').strip()
    ssh_user = (data.get('ssh_user') or 'root').strip()
    ssh_password = data.get('ssh_password') or None
    ssh_private_key = data.get('ssh_private_key') or None
    ssh_key_passphrase = data.get('ssh_key_passphrase') or None
    venus_api_key = (data.get('venus_api_key') or '').strip() or None
    image = (data.get('image') or '').strip() \
        or 'ccr.ccs.tencentyun.com/hermes/hermes-agent:latest'

    if not host:
        raise ValueError('host 必填')
    if not ssh_password and not ssh_private_key:
        raise ValueError('SSH 凭据必填：ssh_password 或 ssh_private_key 至少提供一个')
    if not venus_api_key:
        raise ValueError('venus_api_key 必填')

    hermes_home = (data.get('hermes_home') or '').strip().rstrip('/') or None
    hermes_python = (data.get('hermes_python') or '').strip() or None
    hermes_module = (data.get('hermes_module') or '').strip() or 'hermes_agent'
    systemd_user = (data.get('systemd_user') or '').strip() or 'root'

    if deploy_method == 'systemd' and not hermes_home:
        raise ValueError('systemd 模式必须提供 hermes_home（如 /opt/hermes-xiaohe）')
    if deploy_method == 'systemd' and not hermes_home.startswith('/'):
        raise ValueError('hermes_home 必须是绝对路径')

    try:
        ssh_port = int(data.get('ssh_port') or 22)
    except (TypeError, ValueError):
        ssh_port = 22

    extra_env = data.get('extra_env') or {}
    if not isinstance(extra_env, dict):
        extra_env = {}

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
        image=image,
        hermes_home=hermes_home,
        hermes_python=hermes_python,
        hermes_module=hermes_module,
        systemd_user=systemd_user,
        venus_api_key=venus_api_key,
        extra_env=extra_env,
        triggered_by=actor_name,
    )


def create_deployment_record(claw: OpenClawInstance, req: DeployRequest) -> AgentDeployment:
    """根据 :class:`DeployRequest` 建一条 ``AgentDeployment`` 记录。

    敏感字段（密码 / 私钥 / Venus key）**不会** 写到表里，只把 host / 目录 /
    容器名（或 systemd unit） / 镜像 / 触发人留痕。

    docker 模式：``container_name`` 写容器名，``image`` 写镜像，``remote_base_dir``
    走 ``/opt/openclaw-agents/claw-<id>-<safe>``。

    systemd 模式：``container_name`` 字段复用为 ``hermes-agent-claw-<id>.service``
    unit 名（前端会按 ``deploy_method`` 切换标签），``image`` 留空，
    ``remote_base_dir`` 写传入的 ``hermes_home``。
    """
    if req.deploy_method == 'systemd':
        remote_base = (req.hermes_home or '').rstrip('/')
        container = build_systemd_unit_name(claw.id)
        image = ''
    else:
        remote_base = build_remote_base_dir(claw.id, claw.name or f'claw-{claw.id}')
        container = build_container_name(claw.id)
        image = req.image

    dep = AgentDeployment(
        openclaw_id=claw.id,
        agent_type='hermes',
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
    """重新部署一个 OpenClaw 对应的 Hermes Agent。

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
        return jsonify({'error': '仅超级管理员可代建 / 重新部署 Hermes Agent'}), 403

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
