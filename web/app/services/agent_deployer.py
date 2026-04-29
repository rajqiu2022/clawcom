"""Hub 代建 Hermes Agent 远端部署器

职责
----
- 通过 Paramiko SSH 在远端目标机上：
  1) 准备 per-claw 工作目录 ``/opt/openclaw-agents/claw-<id>-<safe_name>/``，
  2) 写入 ``hermes/config/config.yaml`` 与 ``hermes/config/.env``，
  3) ``docker pull`` 指定镜像，
  4) ``docker run -d --name hermes-agent-claw-<id> ...`` 启动隔离容器，
  5) 把状态、日志摘要、错误信息回写到 :class:`AgentDeployment`。

设计取舍
--------
- SSH 凭据（密码 / 私钥）只活在 ``DeployRequest`` 实例里，绝不落库；
  只有 host / user / 目录 / 容器名 / 状态会写入 ``agent_deployments`` 表。
- 部署是 fire-and-forget 的后台线程；OpenClaw 注册流程不会因为部署失败而回滚。
- 重试只支持「重新输入凭据 + 调相同接口」，不支持自动重跑。
- 同机多 claw 隔离：目录 / 容器名 / 镜像 mount 都按 ``claw_id`` 区分，
  绝不共享 ``/opt/hermes-agent`` 这种全局路径。

入口
----
- :func:`spawn_deploy_async` —— 在后台线程跑一次部署，写状态到 ``AgentDeployment``。
- :func:`build_remote_base_dir` / :func:`build_container_name` —— 命名规则的单一来源。
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from io import StringIO
from typing import Optional

logger = logging.getLogger(__name__)


# ---------- 命名规范 ----------

_SAFE_NAME_RE = re.compile(r'[^A-Za-z0-9_]+')


def _safe_name(raw: str) -> str:
    """把 OpenClaw 名字（可能含中文 / 空格 / 特殊符号）转成路径友好的 ASCII 标识。

    规则：非 ``[A-Za-z0-9_]`` 的字符全部折叠成单个 ``_``，前后下划线去掉，
    截断到 30 字符；空串兜底为 ``unnamed``。
    """
    s = _SAFE_NAME_RE.sub('_', (raw or '').strip()).strip('_')
    if len(s) > 30:
        s = s[:30].rstrip('_')
    return s or 'unnamed'


def build_remote_base_dir(claw_id: int, claw_name: str) -> str:
    """统一的远端 per-claw 工作目录命名：``/opt/openclaw-agents/claw-<id>-<safe_name>/``

    仅 docker 模式使用；systemd 模式直接复用调用方传入的 ``hermes_home``。
    """
    return f"/opt/openclaw-agents/claw-{int(claw_id)}-{_safe_name(claw_name)}"


def build_container_name(claw_id: int) -> str:
    """统一的容器名：``hermes-agent-claw-<id>``"""
    return f"hermes-agent-claw-{int(claw_id)}"


def build_systemd_unit_name(claw_id: int) -> str:
    """统一的 systemd unit 名：``hermes-agent-claw-<id>.service``

    同机多 claw 隔离：每个 claw 各自一个 unit 文件，互不影响。
    """
    return f"hermes-agent-claw-{int(claw_id)}.service"


# ---------- 请求 / 结果 ----------


@dataclass
class DeployRequest:
    """一次远端部署所需的全部参数。

    支持两种部署方式：

    - ``deploy_method='docker'``（默认）：Hub 在远端用 ``docker run`` 起一个独立
      per-claw 容器，需要目标机有 Docker 18+ 且能拉到镜像。``image`` 字段必填。
    - ``deploy_method='systemd'``：适合 Docker 不可用的老机器（如 CentOS 7 +
      Docker 1.13），假设目标机 ``hermes_home`` 下已经装好 Hermes（venv +
      ``hermes_agent`` 模块），Hub 只写 config/.env 并生成 per-claw systemd unit。
      ``hermes_home`` 字段必填。

    敏感字段（``ssh_password`` / ``ssh_private_key`` / ``venus_api_key`` 等）
    只活在请求生命周期内，不落库。
    """
    openclaw_id: int
    claw_name: str
    claw_token: str
    hub_url: str

    host: str
    ssh_user: str
    ssh_port: int = 22
    ssh_password: Optional[str] = None
    ssh_private_key: Optional[str] = None  # PEM 文本（不写盘）
    ssh_key_passphrase: Optional[str] = None

    deploy_method: str = 'docker'  # 'docker' | 'systemd'

    # docker 专用
    image: str = 'ccr.ccs.tencentyun.com/hermes/hermes-agent:latest'

    # systemd 专用
    hermes_home: Optional[str] = None  # 目标机已装好的 Hermes 根目录，如 /opt/hermes-xiaohe
    hermes_python: Optional[str] = None  # 默认 ${hermes_home}/venv/bin/python
    hermes_module: str = 'hermes_agent'  # 默认 python -m hermes_agent
    systemd_user: str = 'root'  # systemd unit User=

    venus_api_key: Optional[str] = None
    extra_env: dict = field(default_factory=dict)

    triggered_by: str = 'system'

    def remote_base_dir(self) -> str:
        """docker 模式的远端 per-claw 根目录；systemd 模式直接返回 hermes_home。"""
        if self.deploy_method == 'systemd':
            return (self.hermes_home or '').rstrip('/')
        return build_remote_base_dir(self.openclaw_id, self.claw_name)

    def container_name(self) -> str:
        """docker 模式返回容器名；systemd 模式返回 unit 名。

        模型里的 ``container_name`` 字段在两种模式下复用，UI 据 ``deploy_method``
        切换 "容器" / "Unit" 的展示文案。
        """
        if self.deploy_method == 'systemd':
            return build_systemd_unit_name(self.openclaw_id)
        return build_container_name(self.openclaw_id)


@dataclass
class StepResult:
    name: str
    exit_code: int
    stdout: str = ''
    stderr: str = ''

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


# ---------- SSH 客户端封装 ----------


class _SSHRunner:
    """对 paramiko.SSHClient 的薄包装，提供 ``run`` / ``put_text`` 两个原语。

    所有命令都收集 stdout/stderr 拼成 :class:`StepResult`；
    远端写文件统一走 ``cat <<'__EOF__' > <path>`` 而不是 SFTP，避开 SFTP 子系统未开的环境。
    """

    def __init__(self, req: DeployRequest):
        self.req = req
        self.client = None

    def __enter__(self):
        import paramiko  # 延迟导入，避免没装的环境也能 import 模块
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        connect_kwargs = {
            'hostname': self.req.host,
            'port': int(self.req.ssh_port or 22),
            'username': self.req.ssh_user,
            'timeout': 20,
            'banner_timeout': 30,
            'auth_timeout': 30,
            'allow_agent': False,
            'look_for_keys': False,
        }

        if self.req.ssh_private_key:
            pkey = self._load_pkey(self.req.ssh_private_key,
                                   self.req.ssh_key_passphrase)
            connect_kwargs['pkey'] = pkey
        elif self.req.ssh_password:
            connect_kwargs['password'] = self.req.ssh_password
        else:
            raise RuntimeError('SSH 凭据缺失：必须提供 ssh_password 或 ssh_private_key')

        client.connect(**connect_kwargs)
        self.client = client
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if self.client:
                self.client.close()
        except Exception:
            pass

    @staticmethod
    def _load_pkey(pem_text: str, passphrase: Optional[str]):
        import paramiko
        from paramiko.ssh_exception import SSHException

        last_err = None
        for cls in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey, paramiko.DSSKey):
            try:
                buf = StringIO(pem_text)
                if passphrase:
                    return cls.from_private_key(buf, password=passphrase)
                return cls.from_private_key(buf)
            except SSHException as e:
                last_err = e
            except Exception as e:
                last_err = e
        raise RuntimeError(f'无法解析 SSH 私钥：{last_err}')

    def run(self, cmd: str, timeout: int = 120, name: str = '') -> StepResult:
        assert self.client is not None
        stdin, stdout, stderr = self.client.exec_command(cmd, timeout=timeout)
        stdin.close()
        out = stdout.read().decode('utf-8', 'replace')
        err = stderr.read().decode('utf-8', 'replace')
        code = stdout.channel.recv_exit_status()
        return StepResult(name=name or cmd[:40], exit_code=code, stdout=out, stderr=err)

    def put_text(self, content: str, remote_path: str, mode: str = '0644',
                 name: str = '') -> StepResult:
        """通过 stdin 把文本写到远端文件，避开 SFTP 子系统。"""
        assert self.client is not None
        # 用 base64 传输，规避 heredoc 的转义陷阱（${VAR} 会被 shell 展开）
        import base64
        b64 = base64.b64encode(content.encode('utf-8')).decode('ascii')
        # mkdir 父目录 → 写入 → chmod
        parent = remote_path.rsplit('/', 1)[0]
        cmd = (
            f"set -e; "
            f"mkdir -p {shlex.quote(parent)}; "
            f"printf %s {shlex.quote(b64)} | base64 -d > {shlex.quote(remote_path)}; "
            f"chmod {mode} {shlex.quote(remote_path)}"
        )
        return self.run(cmd, timeout=60,
                        name=name or f'put_text {remote_path}')


# ---------- Hermes 配置生成 ----------


def _render_config_yaml() -> str:
    """生成 Hermes ``config.yaml``。

    全部敏感字段（API Key）通过环境变量 ``${VENUS_API_KEY}`` 注入容器，
    yaml 里只放占位符，避免凭据落到磁盘。
    """
    return (
        "# Hermes Agent 配置（由 Hub 代建生成）\n"
        "model:\n"
        "  default: \"glm-5.1\"\n"
        "  provider: \"venus\"\n"
        "  api_mode: \"chat_completions\"\n"
        "\n"
        "providers:\n"
        "  venus:\n"
        "    type: \"openai_compatible\"\n"
        "    base_url: \"http://v2.open.venus.oa.com/llmproxy\"\n"
        "    api_key: \"${VENUS_API_KEY}\"\n"
        "    default_model: \"glm-5.1\"\n"
        "    api_mode: \"chat_completions\"\n"
        "    context_length: 128000\n"
        "\n"
        "auxiliary:\n"
        "  vision:\n"
        "    model: \"glm-5.1\"\n"
        "    provider: \"venus\"\n"
        "    api_mode: \"chat_completions\"\n"
        "\n"
        "enable_tools: true\n"
        "enable_vision: true\n"
        "log_level: \"INFO\"\n"
    )


def _render_env_file(req: DeployRequest) -> str:
    """生成 Hermes 进程的环境变量文件。

    docker 模式下路径走容器内的 ``/app/...``；systemd 模式走 ``$HERMES_HOME/...``，
    需要让 Hermes 能在宿主机正确寻路。
    """
    if req.deploy_method == 'systemd':
        home = (req.hermes_home or '').rstrip('/')
        session_dir = f"{home}/sessions"
        config_path = f"{home}/config/config.yaml"
        log_dir = f"{home}/logs"
    else:
        session_dir = "/app/sessions"
        config_path = "/app/config/config.yaml"
        log_dir = "/app/logs"
    lines = [
        "# Hermes 环境变量（由 Hub 代建生成）",
        f"VENUS_API_KEY={req.venus_api_key or ''}",
        "HERMES_CODEX_STREAMING=false",
        "HERMES_LOG_LEVEL=INFO",
        f"HERMES_SESSION_DIR={session_dir}",
        f"HERMES_CONFIG_PATH={config_path}",
        f"HERMES_LOG_DIR={log_dir}",
        f"OPENCLAW_HUB_URL={req.hub_url.rstrip('/')}",
        f"OPENCLAW_CLAW_ID={req.openclaw_id}",
        f"OPENCLAW_TOKEN={req.claw_token}",
    ]
    for k, v in (req.extra_env or {}).items():
        if k and v is not None:
            lines.append(f"{k}={v}")
    return '\n'.join(lines) + '\n'


def _render_systemd_unit(req: DeployRequest) -> str:
    """生成 per-claw 的 systemd unit 文件。

    设计要点：
    - ``EnvironmentFile=`` 指向 per-claw ``.env``，VENUS_API_KEY 等敏感值不裸露在 unit 文件里
    - ``WorkingDirectory`` 与 ``ExecStart`` 都基于 ``hermes_home``，不假设全局路径
    - ``Restart=always`` + ``StartLimitBurst`` 防 crash-loop
    """
    home = (req.hermes_home or '').rstrip('/')
    py = req.hermes_python or f"{home}/venv/bin/python"
    module = req.hermes_module or 'hermes_agent'
    return (
        "[Unit]\n"
        f"Description=Hermes Agent (Hub-managed, claw {req.openclaw_id} {req.claw_name})\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"User={req.systemd_user or 'root'}\n"
        f"WorkingDirectory={home}\n"
        f"EnvironmentFile={home}/config/.env\n"
        f"ExecStartPre=/bin/bash -c 'mkdir -p {home}/sessions {home}/logs && "
        f"[ -s {home}/sessions/sessions.json ] || echo \"{{}}\" > {home}/sessions/sessions.json'\n"
        f"ExecStart={py} -m {module}\n"
        f"StandardOutput=append:{home}/logs/hermes-agent.log\n"
        f"StandardError=append:{home}/logs/hermes-agent.log\n"
        "Restart=always\n"
        "RestartSec=10\n"
        "StartLimitBurst=5\n"
        "StartLimitIntervalSec=300\n"
        "LimitNOFILE=65536\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


# ---------- 部署主流程 ----------


def _tail(text: str, max_chars: int = 4000) -> str:
    if not text:
        return ''
    if len(text) <= max_chars:
        return text
    return '...\n' + text[-max_chars:]


def _record_step(steps: list, sr: StepResult) -> None:
    steps.append(
        f"[{sr.name}] exit={sr.exit_code}\n"
        f"--- stdout ---\n{_tail(sr.stdout, 800)}\n"
        f"--- stderr ---\n{_tail(sr.stderr, 800)}"
    )


def _run_deploy(deployment_id: int, req: DeployRequest, app) -> None:
    """实际跑部署。在后台线程里调用。

    - ``app`` 是 Flask app，用来推 app_context 后访问 ``db.session``。
    - 任何异常都被捕获、回写到 ``AgentDeployment.status='failed'``。
    """
    from app import db
    from app.models import AgentDeployment

    steps: list = []

    def _flush(status: str, error: Optional[str] = None) -> None:
        with app.app_context():
            dep = db.session.get(AgentDeployment, deployment_id)
            if not dep:
                return
            dep.status = status
            dep.log_tail = _tail('\n\n'.join(steps), 6000)
            if error:
                dep.error_message = error[:2000]
            if status in ('success', 'failed') and not dep.finished_at:
                dep.finished_at = datetime.now()
            db.session.commit()

    try:
        with app.app_context():
            dep = db.session.get(AgentDeployment, deployment_id)
            if not dep:
                logger.error('agent deployment %s 不存在', deployment_id)
                return
            dep.status = 'in_progress'
            dep.started_at = datetime.now()
            db.session.commit()

        with _SSHRunner(req) as ssh:
            if req.deploy_method == 'systemd':
                _deploy_systemd(ssh, req, steps, _flush)
            else:
                _deploy_docker(ssh, req, steps, _flush)
    except Exception as e:
        logger.exception('agent deployment %s failed: %s', deployment_id, e)
        steps.append(f"[fatal] 部署异常：{e}")
        _flush('failed', f'{type(e).__name__}: {e}')


def _deploy_docker(ssh: '_SSHRunner', req: DeployRequest, steps: list, _flush) -> None:
    """Docker 模式：拉镜像 → docker run -d --name hermes-agent-claw-<id>。"""
    base = req.remote_base_dir()
    cname = req.container_name()

    # 0) 连通性 & docker 是否可用
    sr = ssh.run('docker --version', timeout=20, name='docker --version')
    _record_step(steps, sr)
    if not sr.ok:
        _flush('failed',
               'docker 不可用：请确认目标机已安装 Docker 并允许该用户调用，'
               '或改用 deploy_method=systemd。')
        return

    # 1) 创建 per-claw 目录骨架
    mkdir_cmd = (
        f"mkdir -p "
        f"{shlex.quote(base + '/hermes/config')} "
        f"{shlex.quote(base + '/hermes/sessions')} "
        f"{shlex.quote(base + '/hermes/logs')} && "
        f"chmod 755 {shlex.quote(base)} {shlex.quote(base + '/hermes')}"
    )
    sr = ssh.run(mkdir_cmd, timeout=30, name='mkdir per-claw dirs')
    _record_step(steps, sr)
    if not sr.ok:
        _flush('failed', f'创建目录失败：{_tail(sr.stderr, 400)}')
        return

    # 2) 写 config.yaml / .env（敏感值经 base64 中转，避免被 shell 展开）
    sr = ssh.put_text(_render_config_yaml(),
                      f'{base}/hermes/config/config.yaml',
                      mode='0644', name='write config.yaml')
    _record_step(steps, sr)
    if not sr.ok:
        _flush('failed', '写 config.yaml 失败')
        return

    sr = ssh.put_text(_render_env_file(req),
                      f'{base}/hermes/config/.env',
                      mode='0600', name='write .env')
    _record_step(steps, sr)
    if not sr.ok:
        _flush('failed', '写 .env 失败')
        return

    # 3) docker pull（允许失败回退到本地已缓存镜像，但要给出明确日志）
    sr = ssh.run(f'docker pull {shlex.quote(req.image)}',
                 timeout=600, name='docker pull')
    _record_step(steps, sr)
    if not sr.ok:
        steps.append(f"[warn] docker pull 失败，尝试用本地已缓存的 {req.image} 启动")

    # 4) 清理同名容器（只清本 claw 的）
    sr = ssh.run(
        f'docker rm -f {shlex.quote(cname)} 2>/dev/null || true',
        timeout=60, name='docker rm previous')
    _record_step(steps, sr)

    # 5) docker run
    run_cmd = (
        f'docker run -d '
        f'--name {shlex.quote(cname)} '
        f'--restart always '
        f'--network host '
        f'-v {shlex.quote(base + "/hermes/config")}:/app/config:ro '
        f'-v {shlex.quote(base + "/hermes/sessions")}:/app/sessions '
        f'-v {shlex.quote(base + "/hermes/logs")}:/app/logs '
        f'--env-file {shlex.quote(base + "/hermes/config/.env")} '
        f'-e HERMES_CODEX_STREAMING=false '
        f'{shlex.quote(req.image)}'
    )
    sr = ssh.run(run_cmd, timeout=120, name='docker run')
    _record_step(steps, sr)
    if not sr.ok:
        _flush('failed', f'docker run 失败：{_tail(sr.stderr, 600)}')
        return

    # 6) 给容器 5 秒缓冲，再 docker ps 确认确实在跑
    time.sleep(5)
    sr = ssh.run(
        f'docker ps --filter name=^{cname}$ --format "{{{{.Names}}}}\t{{{{.Status}}}}"',
        timeout=30, name='docker ps verify')
    _record_step(steps, sr)
    if not sr.ok or cname not in sr.stdout:
        logs_sr = ssh.run(f'docker logs --tail 60 {shlex.quote(cname)}',
                          timeout=30, name='docker logs')
        _record_step(steps, logs_sr)
        _flush('failed', f'容器启动后未保持运行：{_tail(sr.stdout, 200)}')
        return

    _flush('success')


def _deploy_systemd(ssh: '_SSHRunner', req: DeployRequest, steps: list, _flush) -> None:
    """systemd 模式：复用目标机已装好的 Hermes，只刷 config/.env + per-claw unit。

    前置条件：目标机 ``$HERMES_HOME/venv/bin/python`` 必须已存在并能 ``import
    hermes_agent``。Hub 不会替你装 Hermes 源码（避免外网依赖、git 凭据这些麻烦）。
    """
    home = req.remote_base_dir()  # systemd 模式下就是 hermes_home
    unit_name = req.container_name()  # systemd 模式下就是 unit 名
    unit_path = f"/etc/systemd/system/{unit_name}"
    py = req.hermes_python or f"{home}/venv/bin/python"

    if not home:
        _flush('failed', 'systemd 模式必须提供 hermes_home（如 /opt/hermes-xiaohe）')
        return

    # 0) 基本环境探测
    sr = ssh.run('command -v systemctl && systemctl --version | head -n1',
                 timeout=20, name='systemctl --version')
    _record_step(steps, sr)
    if not sr.ok:
        _flush('failed', '目标机没有 systemctl，无法用 systemd 模式部署。')
        return

    sr = ssh.run(f"test -x {shlex.quote(py)} && {shlex.quote(py)} -V",
                 timeout=20, name='hermes python -V')
    _record_step(steps, sr)
    if not sr.ok:
        _flush('failed',
               f'未在目标机找到可执行的 Hermes python：{py}。'
               f'请先在 {home} 下装好 venv（HERMES_HOME/venv/bin/python），或显式传 hermes_python。')
        return

    sr = ssh.run(
        f"{shlex.quote(py)} -c 'import {req.hermes_module}' 2>&1",
        timeout=30, name=f'import {req.hermes_module}')
    _record_step(steps, sr)
    if not sr.ok:
        _flush('failed',
               f'目标机的 Hermes venv 无法 import {req.hermes_module}：'
               f'请确认 hermes_agent 源码已装到 {home} 且依赖完整。')
        return

    # 1) 创建 per-claw 目录骨架（沿用 hermes_home 下的 config/sessions/logs）
    mkdir_cmd = (
        f"mkdir -p {shlex.quote(home + '/config')} "
        f"{shlex.quote(home + '/sessions')} "
        f"{shlex.quote(home + '/logs')}"
    )
    sr = ssh.run(mkdir_cmd, timeout=30, name='mkdir hermes dirs')
    _record_step(steps, sr)
    if not sr.ok:
        _flush('failed', f'mkdir 失败：{_tail(sr.stderr, 300)}')
        return

    # 2) 写 config.yaml / .env
    sr = ssh.put_text(_render_config_yaml(),
                      f'{home}/config/config.yaml',
                      mode='0644', name='write config.yaml')
    _record_step(steps, sr)
    if not sr.ok:
        _flush('failed', '写 config.yaml 失败')
        return

    sr = ssh.put_text(_render_env_file(req),
                      f'{home}/config/.env',
                      mode='0600', name='write .env')
    _record_step(steps, sr)
    if not sr.ok:
        _flush('failed', '写 .env 失败')
        return

    # 3) 写 per-claw systemd unit 文件
    sr = ssh.put_text(_render_systemd_unit(req),
                      unit_path, mode='0644',
                      name=f'write {unit_name}')
    _record_step(steps, sr)
    if not sr.ok:
        _flush('failed', f'写 systemd unit 失败：{_tail(sr.stderr, 400)}')
        return

    # 4) reload + enable + restart（用 restart 兼容首次安装与覆盖更新）
    sr = ssh.run('systemctl daemon-reload', timeout=30, name='systemctl daemon-reload')
    _record_step(steps, sr)
    if not sr.ok:
        _flush('failed', f'daemon-reload 失败：{_tail(sr.stderr, 300)}')
        return

    sr = ssh.run(f'systemctl enable {shlex.quote(unit_name)}',
                 timeout=30, name=f'systemctl enable {unit_name}')
    _record_step(steps, sr)

    sr = ssh.run(f'systemctl restart {shlex.quote(unit_name)}',
                 timeout=60, name=f'systemctl restart {unit_name}')
    _record_step(steps, sr)
    if not sr.ok:
        _flush('failed', f'systemctl restart 失败：{_tail(sr.stderr, 600)}')
        return

    # 5) 5 秒缓冲，再 is-active 确认
    time.sleep(5)
    sr = ssh.run(f'systemctl is-active {shlex.quote(unit_name)}',
                 timeout=20, name=f'systemctl is-active {unit_name}')
    _record_step(steps, sr)
    is_active_ok = sr.ok and 'active' in sr.stdout.strip()

    # 不论成败都拉一段最近日志写回，便于排查
    log_sr = ssh.run(
        f'tail -n 60 {shlex.quote(home + "/logs/hermes-agent.log")} 2>/dev/null '
        f'|| journalctl -u {shlex.quote(unit_name)} -n 60 --no-pager',
        timeout=30, name='hermes recent logs')
    _record_step(steps, log_sr)

    if not is_active_ok:
        _flush('failed', f'systemd 服务启动后未保持 active：{_tail(sr.stdout, 200)}')
        return

    _flush('success')


def spawn_deploy_async(app, deployment_id: int, req: DeployRequest) -> None:
    """启动后台线程跑部署，立即返回。

    Args:
        app: Flask app（用于子线程 push app_context）
        deployment_id: ``AgentDeployment`` 主键
        req: 完整请求；调用方应在调用后立即丢弃 ``ssh_password`` /
             ``ssh_private_key`` / ``venus_api_key`` 等敏感字段。
    """
    t = threading.Thread(target=_run_deploy,
                         args=(deployment_id, req, app),
                         name=f'agent-deploy-{deployment_id}',
                         daemon=True)
    t.start()
