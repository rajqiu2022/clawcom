"""Hub 代建 Hermes Agent 远端部署器

职责
----
- **Systemd**：对齐《Hermes Agent 标准化部署指南》v2——拆分目录时
  ``hermes_install_dir``（venv+源码）+ ``hermes_data_dir``（HERMES_HOME，
  根目录 ``config.yaml``）；unit ``hermes-gateway-claw-<id>.service``；
  默认 ``hermes_cli.main gateway run --replace``；兼容单目录 ``hermes_home``。
- 状态与日志摘要回写 :class:`AgentDeployment`。

设计取舍
--------
- SSH 凭据（密码 / 私钥）只活在 ``DeployRequest`` 实例里，绝不落库；
  只有 host / user / 目录 / 容器名 / 状态会写入 ``agent_deployments`` 表。
- 部署是 fire-and-forget 的后台线程；OpenClaw 注册流程不会因为部署失败而回滚。
- 重试只支持「重新输入凭据 + 调相同接口」，不支持自动重跑。
- 同机多 claw 隔离：Systemd 按 unit 名 ``hermes-gateway-claw-<id>``，并用
  ``oclaw_<id>`` 用户 + ``ReadWritePaths`` 限定写入边界。

入口
----
- :func:`spawn_deploy_async` —— 在后台线程跑一次部署，写状态到 ``AgentDeployment``。
- :func:`build_remote_base_dir` / :func:`build_container_name` / :func:`build_systemd_unit_name`
  —— 命名规则的单一来源。
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from io import StringIO
from typing import Optional

from app.hermes_models import (
    DEFAULT_HERMES_LLM_PROVIDER,
    DEFAULT_HERMES_LLM_MODEL,
    HERMES_LLM_PROVIDERS,
    hermes_config_model,
    hermes_context_length,
    hermes_provider_api_mode,
    hermes_vision_config,
    normalize_hermes_model,
    normalize_hermes_provider,
)

logger = logging.getLogger(__name__)


# ---------- 命名规范 ----------

_SAFE_NAME_RE = re.compile(r'[^A-Za-z0-9_]+')
_SHA256_RE = re.compile(r'^[0-9a-fA-F]{64}$')
AGENT_ROOT_DIR = "/opt/openclaw-agents"
AGENT_SHARE_DIR = "/opt/agent_share"
AGENT_SERVICE_GROUP = "openclaw_agents"
_BLOCKED_WORK_DIR_PREFIXES = (
    '/', '/bin', '/boot', '/dev', '/etc', '/lib', '/lib64', '/proc', '/root',
    '/run', '/sbin', '/sys', '/usr', '/var/lib/mysql', '/opt/openclaw-web',
)


def _safe_name(raw: str) -> str:
    """把 OpenClaw 名字（可能含中文 / 空格 / 特殊符号）转成路径友好的 ASCII 标识。

    规则：
    1. 中文 / 非 ASCII 字符直接去掉（不保留）
    2. 非 ``[A-Za-z0-9_]`` 的 ASCII 字符全部折叠成单个 ``_``
    3. 转小写（避免大小写敏感问题）
    4. 前后下划线去掉，截断到 30 字符；空串兜底为 ``unnamed``。
    """
    # 先去掉所有非 ASCII 字符（包括中文）
    s_ascii = (raw or '').encode('ascii', errors='ignore').decode('ascii')
    # 替换非 [A-Za-z0-9_] 的字符为 _
    s = _SAFE_NAME_RE.sub('_', s_ascii.strip()).lower().strip('_')
    if len(s) > 30:
        s = s[:30].rstrip('_')
    return s or 'unnamed'


def build_remote_base_dir(claw_id: int, claw_name: str = '', safe_name: str = '') -> str:
    """统一的远端 per-claw 工作目录命名：``/opt/openclaw-agents/claw-<id>-<safe_name>/``

    仅 docker 模式使用；systemd 模式直接复用调用方传入的 ``hermes_home``。
    
    :param safe_name: 优先使用（从数据库读取，第一次创建时生成并存储）
    :param claw_name: 备用（当 safe_name 为空时，从 claw.name 生成）
    """
    name = safe_name or _safe_name(claw_name or f'claw-{claw_id}')
    return f"{AGENT_ROOT_DIR}/claw-{int(claw_id)}-{name}"


def build_default_systemd_data_dir(claw_id: int, claw_name: str = '', safe_name: str = '') -> str:
    """systemd 默认 HERMES_HOME：per-agent 私有数据目录。"""
    return f"{build_remote_base_dir(claw_id, claw_name, safe_name)}/data"


def build_default_systemd_user(claw_id: int) -> str:
    """每个 agent 独立 Linux 服务用户。"""
    return f"oclaw_{int(claw_id)}"


def build_container_name(claw_id: int) -> str:
    """统一的容器名：``hermes-agent-claw-<id>``"""
    return f"hermes-agent-claw-{int(claw_id)}"


def build_systemd_unit_name(claw_id: int) -> str:
    """Systemd unit 名：``hermes-gateway-claw-<id>.service``

    与《Hermes Agent 标准化部署指南》v2 的 ``hermes-gateway-{name}`` 命名一致，
    用 ``claw-<id>`` 保证同机多 OpenClaw 不冲突。
    """
    return f"hermes-gateway-claw-{int(claw_id)}.service"


def build_codex_unit_name(claw_id: int) -> str:
    """Linux Codex provider 只托管 Hub Sidecar，不额外启动 Gateway。"""
    return f"openclaw-sidecar-v2-claw-{int(claw_id)}.service"


def normalize_agent_work_dirs(raw) -> list[str]:
    """Validate extra writable directories for a systemd-deployed Hermes Agent."""
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            raw_items = parsed if isinstance(parsed, list) else raw.splitlines()
        except Exception:
            raw_items = raw.splitlines()
    elif isinstance(raw, (list, tuple)):
        raw_items = raw
    else:
        raw_items = []

    result = []
    seen = set()
    for item in raw_items:
        path = str(item or '').strip().rstrip('/')
        if not path:
            continue
        if not path.startswith('/'):
            raise ValueError('工作目录必须为绝对路径')
        if any(ch.isspace() for ch in path):
            raise ValueError('工作目录暂不支持包含空格或换行的路径')
        if '%' in path:
            raise ValueError('工作目录暂不支持包含 % 的路径')
        if len(path) > 500:
            raise ValueError('工作目录路径过长')
        for blocked in _BLOCKED_WORK_DIR_PREFIXES:
            if path == blocked or (blocked != '/' and path.startswith(blocked.rstrip('/') + '/')):
                raise ValueError(f'工作目录不能设置为系统目录：{path}')
        if path not in seen:
            result.append(path)
            seen.add(path)
    if len(result) > 20:
        raise ValueError('工作目录最多配置 20 个')
    return result


# ---------- 请求 / 结果 ----------


@dataclass
class DeployRequest:
    """一次远端部署所需的全部参数。

    Hub 代建 Hermes Agent 当前统一使用 ``deploy_method='systemd'``：

    - ``deploy_method='systemd'``：与《Hermes Agent 标准化部署指南》v2 对齐。
      **推荐（拆分目录）**：``hermes_install_dir`` 指向已准备好的 venv/源码
      （只读使用），``hermes_data_dir`` 必须位于
      ``/opt/openclaw-agents/claw-<id>-<safe>/`` 下作为 ``HERMES_HOME``。
      **兼容（单目录）**：仅填 ``hermes_home``，也必须位于该 claw 私有目录下。
      启动默认 ``hermes_cli.main gateway run --replace``，可用 ``hermes_start_mode``
      切回 ``python -m hermes_agent`` 等旧形态。

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

    deploy_method: str = 'systemd'
    agent_type: str = 'hermes'

    # docker 专用
    image: str = 'ccr.ccs.tencentyun.com/hermes/hermes-agent:latest'

    # systemd 专用（与部署指南 v2：INSTALL_DIR + DATA_DIR 对齐）
    hermes_home: Optional[str] = None  # 兼容：单根目录时 install=data=hermes_home
    hermes_install_dir: Optional[str] = None  # /opt/hermes-{name}/ 含 venv
    hermes_data_dir: Optional[str] = None  # /root/.hermes-{name}/ 即 HERMES_HOME
    hermes_python: Optional[str] = None  # 默认 ${install}/venv/bin/python
    hermes_module: str = 'hermes_agent'  # hermes_start_mode=module 时使用
    hermes_start_mode: str = 'gateway'  # 'gateway' | 'module'
    systemd_user: str = ''  # 空值时自动使用 oclaw_<claw_id>

    # Linux Codex provider（SDK + CLI 共享同一运行用户的 CODEX_HOME）
    codex_runtime_dir: str = '/opt/codex-runtime'
    codex_data_dir: Optional[str] = None
    codex_python: Optional[str] = None
    codex_cli: str = '/usr/local/bin/codex'
    codex_workspace: Optional[str] = None
    codex_model: str = ''
    codex_requirements: Optional[str] = None
    codex_wheelhouse: Optional[str] = None
    codex_requirements_sha256: str = ''
    codex_auth_mode: str = 'chatgpt_subscription'

    venus_api_key: Optional[str] = None
    timiai_api_key: Optional[str] = None
    timiai_project: str = 'gbt'
    llm_provider: str = DEFAULT_HERMES_LLM_PROVIDER
    llm_model: str = DEFAULT_HERMES_LLM_MODEL
    wecom_bot_id: str = ''
    wecom_bot_secret: str = ''
    owner_wecom_userid: str = ''
    safe_name: str = ''  # 目录名安全版本（从数据库读取，第一次创建时生成，后续不变）
    work_dirs: list[str] = field(default_factory=list)
    extra_env: dict = field(default_factory=dict)

    triggered_by: str = 'system'

    def systemd_install_dir(self) -> str:
        """systemd：venv / 源码根目录。"""
        if self.agent_type == 'codex':
            return (self.codex_runtime_dir or '').rstrip('/')
        if self.hermes_install_dir:
            return self.hermes_install_dir.rstrip('/')
        return (self.hermes_home or '').rstrip('/')

    def systemd_data_dir(self) -> str:
        """systemd：HERMES_HOME（数据、config.yaml 根目录）。"""
        if self.agent_type == 'codex':
            return (self.codex_data_dir or '').rstrip('/')
        if self.hermes_data_dir:
            return self.hermes_data_dir.rstrip('/')
        return (self.hermes_home or '').rstrip('/')

    def systemd_split_layout(self) -> bool:
        """是否采用指南 v2 的拆分目录（install 与 data 为不同路径）。"""
        return bool(self.hermes_install_dir and self.hermes_data_dir)

    def codex_home_dir(self) -> str:
        """CLI 与 SDK 共用的私有认证目录；Hub 永不读取其中内容。"""
        return f"{self.systemd_data_dir()}/home/.codex"

    def systemd_service_user(self) -> str:
        """systemd 运行用户；root 会被收敛到 per-agent 用户。"""
        user = (self.systemd_user or '').strip()
        if not user or user == 'root':
            return build_default_systemd_user(self.openclaw_id)
        return user

    def remote_base_dir(self) -> str:
        """docker：per-claw 根目录；systemd：对外展示用数据目录（HERMES_HOME）。"""
        if self.deploy_method == 'systemd':
            return self.systemd_data_dir()
        # 优先使用 safe_name（数据库中存储的，第一次创建时生成，后续不变）
        safe = self.safe_name or ''
        return build_remote_base_dir(self.openclaw_id, self.claw_name, safe_name=safe)

    def container_name(self) -> str:
        """docker 模式返回容器名；systemd 模式返回 ``hermes-gateway-claw-<id>.service``。

        模型里的 ``container_name`` 字段在两种模式下复用，UI 据 ``deploy_method``
        切换 "容器" / "Unit" 的展示文案。
        """
        if self.deploy_method == 'systemd':
            if self.agent_type == 'codex':
                return build_codex_unit_name(self.openclaw_id)
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


def _yaml_quote(s: str) -> str:
    """双引号 YAML 字符串转义。"""
    return json.dumps(s or '', ensure_ascii=False)


def _render_config_yaml(req: DeployRequest) -> str:
    """生成 Hermes ``config.yaml``。

    Venus API Key 通过环境变量 ``${VENUS_API_KEY}`` 占位（与 systemd
    ``EnvironmentFile`` / docker ``--env-file`` 一致）。

    若 ``req`` 带 ``hub_url`` + ``claw_token``，写入 ``hub:`` 节点（与《Hermes Agent
    标准化部署指南》v2 一致），便于 Gateway 直连 Hub。
    """
    provider = normalize_hermes_provider(req.llm_provider)
    selected_model = normalize_hermes_model(req.llm_model, provider)
    config_model = hermes_config_model(selected_model, provider)
    context_length = hermes_context_length(selected_model, provider)
    api_mode = hermes_provider_api_mode(provider)
    provider_meta = HERMES_LLM_PROVIDERS[provider]
    vision = hermes_vision_config(provider)
    vision_api_mode = hermes_provider_api_mode(vision["provider"])

    lines = [
        "# Hermes Agent 配置（由 Hub 代建生成）",
        f"# Hub 选择：{provider} / {selected_model}",
        "model:",
        f"  default: {_yaml_quote(config_model)}",
        f"  provider: {_yaml_quote(provider)}",
        f"  api_mode: {_yaml_quote(api_mode)}",
        "",
        "providers:",
        f"  {provider}:",
        "    type: \"openai_compatible\"",
        f"    base_url: {_yaml_quote(provider_meta['base_url'])}",
        f"    api_key: \"${{{provider_meta['api_key_env']}}}\"",
        f"    default_model: {_yaml_quote(config_model)}",
        f"    api_mode: {_yaml_quote(api_mode)}",
        f"    context_length: {context_length}",
        "",
        "auxiliary:",
        "  vision:",
        f"    model: {_yaml_quote(vision['model'])}",
        f"    provider: {_yaml_quote(vision['provider'])}",
        f"    api_mode: {_yaml_quote(vision_api_mode)}",
        "",
        "enable_tools: true",
        "enable_vision: true",
        "log_level: \"INFO\"",
    ]
    if req.wecom_bot_id and req.wecom_bot_secret:
        lines.extend([
            "",
            "wecom:",
            f"  key: {_yaml_quote(req.wecom_bot_id)}",
            f"  secret: {_yaml_quote(req.wecom_bot_secret)}",
        ])
    hub_base = (req.hub_url or '').rstrip('/')
    if hub_base and (req.claw_token or '').strip():
        lines.extend([
            "",
            "hub:",
            f"  base_url: {_yaml_quote(hub_base)}",
            f"  api_token: {_yaml_quote(req.claw_token)}",
            f"  claw_id: {int(req.openclaw_id)}",
            "  enabled: true",
        ])
    return '\n'.join(lines) + '\n'


def _render_env_file(req: DeployRequest) -> str:
    """生成 Hermes 进程的环境变量文件。

    docker 模式下路径走容器内的 ``/app/...``；systemd 模式走数据目录
    （``HERMES_HOME``）：拆分布局时 ``config.yaml`` 在数据根目录，兼容布局在
    ``{HERMES_HOME}/config/config.yaml``。
    """
    if req.deploy_method == 'systemd':
        data = req.systemd_data_dir()
        if req.systemd_split_layout():
            session_dir = f"{data}/sessions"
            config_path = f"{data}/config.yaml"
            log_dir = f"{data}/logs"
        else:
            session_dir = f"{data}/sessions"
            config_path = f"{data}/config/config.yaml"
            log_dir = f"{data}/logs"
    else:
        session_dir = "/app/sessions"
        config_path = "/app/config/config.yaml"
        log_dir = "/app/logs"
    lines = [
        "# Hermes 环境变量（由 Hub 代建生成）",
        f"VENUS_API_KEY={req.venus_api_key or ''}",
        f"TIMIAI_API_KEY={req.timiai_api_key or ''}",
        "HERMES_CODEX_STREAMING=false",
        "HERMES_LOG_LEVEL=INFO",
        f"HERMES_SESSION_DIR={session_dir}",
        f"HERMES_CONFIG_PATH={config_path}",
        f"HERMES_LOG_DIR={log_dir}",
        f"OPENCLAW_HUB_URL={req.hub_url.rstrip('/')}",
        f"OPENCLAW_CLAW_ID={req.openclaw_id}",
        f"OPENCLAW_TOKEN={req.claw_token}",
        f"HERMES_LLM_PROVIDER={normalize_hermes_provider(req.llm_provider)}",
        f"HERMES_LLM_MODEL={normalize_hermes_model(req.llm_model, req.llm_provider)}",
        f"HERMES_TIMIAI_PROJECT={req.timiai_project or 'gbt'}",
        f"AGENT_WORK_DIRS={json.dumps(req.work_dirs or [], ensure_ascii=False)}",
    ]
    if req.wecom_bot_id and req.wecom_bot_secret:
        lines.extend([
            f"WECOM_KEY={req.wecom_bot_id}",
            f"WECOM_BOT_ID={req.wecom_bot_id}",
            f"WECOM_SECRET={req.wecom_bot_secret}",
            "WECOM_ALLOW_ALL_USERS=true",
            "WECOM_DM_POLICY=open",
        ])
        if req.owner_wecom_userid:
            lines.extend([
                f"WECOM_HOME_CHANNEL={req.owner_wecom_userid}",
                f"WECOM_HOME_CHANNEL_NAME={req.owner_wecom_userid}",
            ])
    for k, v in (req.extra_env or {}).items():
        if k and v is not None:
            lines.append(f"{k}={v}")
    return '\n'.join(lines) + '\n'


def _render_systemd_unit(req: DeployRequest) -> str:
    """生成 per-claw 的 systemd unit（对齐《Hermes Agent 标准化部署指南》v2）。

    - 拆分目录：``VIRTUAL_ENV``/``PATH`` 指向 install_dir，``HERMES_HOME`` 指向 data_dir
    - 默认 ``hermes_cli.main gateway run --replace``；``hermes_start_mode=module`` 时用
      ``python -m {hermes_module}``
    - ``EnvironmentFile`` 指向数据目录下 ``.env``（拆分）或 ``config/.env``（兼容）
    - 日志走 journal（与指南一致），便于 ``journalctl -u`` 排障
    """
    inst = req.systemd_install_dir()
    data = req.systemd_data_dir()
    py = req.hermes_python or f"{inst}/venv/bin/python"
    venv_bin = f"{inst}/venv/bin"
    user = req.systemd_service_user()
    home_env = f"{data}/home"

    if (req.hermes_start_mode or 'gateway').lower() == 'module':
        mod = req.hermes_module or 'hermes_agent'
        exec_start = f"{py} -m {mod}"
        exec_reload = ''
    else:
        exec_start = f"{py} -m hermes_cli.main gateway run --replace"
        exec_reload = "ExecReload=/bin/kill -USR1 $MAINPID\n"

    if req.systemd_split_layout():
        env_file = f"{data}/.env"
        _pre_script = (
            f"mkdir -p {data}/sessions {data}/logs {data}/scripts && "
            f"([ -s {data}/sessions/sessions.json ] || echo '{{}}' > {data}/sessions/sessions.json)"
        )
    else:
        env_file = f"{data}/config/.env"
        _pre_script = (
            f"mkdir -p {data}/config {data}/sessions {data}/logs && "
            f"([ -s {data}/sessions/sessions.json ] || echo '{{}}' > {data}/sessions/sessions.json)"
        )
    pre = f"/bin/sh -c {shlex.quote(_pre_script)}"
    read_only_paths = f"ReadOnlyPaths={inst}\n" if inst != data else ""
    read_write_paths = ' '.join([data, AGENT_SHARE_DIR] + (req.work_dirs or []))

    return (
        "[Unit]\n"
        f"Description=Hermes Gateway (Hub claw {req.openclaw_id} {req.claw_name})\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "StartLimitIntervalSec=600\n"
        "StartLimitBurst=5\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"User={user}\n"
        f"Group={AGENT_SERVICE_GROUP}\n"
        f"WorkingDirectory={data}\n"
        f"Environment=\"PATH={venv_bin}:/usr/local/sbin:/usr/local/bin:"
        f"/usr/sbin:/usr/bin:/sbin:/bin\"\n"
        f"Environment=\"VIRTUAL_ENV={inst}/venv\"\n"
        f"Environment=\"HERMES_HOME={data}\"\n"
        f"Environment=\"AGENT_PRIVATE_DIR={data}\"\n"
        f"Environment=\"AGENT_SHARE_DIR={AGENT_SHARE_DIR}\"\n"
        "Environment=\"HERMES_CODEX_STREAMING=false\"\n"
        f"Environment=\"HOME={home_env}\"\n"
        "Environment=\"HERMES_LOG_LEVEL=INFO\"\n"
        f"EnvironmentFile={env_file}\n"
        f"ExecStartPre={pre}\n"
        f"ExecStart={exec_start}\n"
        f"{exec_reload}"
        "UMask=0077\n"
        "NoNewPrivileges=true\n"
        "PrivateTmp=true\n"
        "ProtectSystem=strict\n"
        "ProtectHome=true\n"
        "ProtectControlGroups=true\n"
        "ProtectKernelModules=true\n"
        "ProtectKernelTunables=true\n"
        "RestrictSUIDSGID=true\n"
        "LockPersonality=true\n"
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6\n"
        f"ReadWritePaths={read_write_paths}\n"
        f"{read_only_paths}"
        "Restart=on-failure\n"
        "RestartSec=30\n"
        "TimeoutStopSec=60\n"
        "LimitNOFILE=65536\n"
        "StandardOutput=journal\n"
        "StandardError=journal\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def _sidecar_unit_name(req: DeployRequest) -> str:
    return f"openclaw-sidecar-v2-claw-{int(req.openclaw_id)}.service"


def _render_sidecar_wrapper(req: DeployRequest) -> str:
    data = req.systemd_data_dir()
    home = f"{data}/home"
    py = req.hermes_python or f"{req.systemd_install_dir()}/venv/bin/python"
    config_path = f"{data}/config.yaml" if req.systemd_split_layout() else f"{data}/config/config.yaml"
    env_path = f"{data}/.env" if req.systemd_split_layout() else f"{data}/config/.env"
    return (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "MSG=\"\"\n"
        "TIMEOUT=300\n"
        "while [ $# -gt 0 ]; do\n"
        "  case \"$1\" in\n"
        "    --message) MSG=\"${2:-}\"; shift 2 ;;\n"
        "    --timeout) TIMEOUT=\"${2:-300}\"; shift 2 ;;\n"
        "    *) shift ;;\n"
        "  esac\n"
        "done\n"
        "if [ -z \"$MSG\" ]; then\n"
        "  echo \"missing --message\" >&2\n"
        "  exit 2\n"
        "fi\n"
        f"cd {shlex.quote(data)}\n"
        f"export HERMES_HOME={shlex.quote(data)}\n"
        f"export HOME={shlex.quote(home)}\n"
        f"export HERMES_CONFIG_PATH={shlex.quote(config_path)}\n"
        "export HERMES_CODEX_STREAMING=false\n"
        "mkdir -p \"$HOME/.hermes\" \"$HOME/.hermes/data\" \"$HOME/.hermes/sessions\"\n"
        f"if [ -r {shlex.quote(env_path)} ]; then\n"
        "  set -a\n"
        f"  . {shlex.quote(env_path)}\n"
        "  set +a\n"
        "fi\n"
        f"exec {shlex.quote(py)} -m hermes_cli.main chat -Q -t hermes-wecom "
        "--max-turns 30 --source hub-sidecar -q \"$MSG\"\n"
    )


def _render_sidecar_env(req: DeployRequest) -> str:
    lines = [
        "# hub-sse-sidecar v2 environment（由 Hub 代建生成）\n"
        f"HUB_URL={(req.hub_url or '').rstrip('/')}\n"
        f"CLAW_ID={int(req.openclaw_id)}\n"
        f"CLAW_TOKEN={req.claw_token or ''}\n"
        "SSE_RECONNECT_SEC=5\n"
        "CONFIG_REFRESH_SEC=60\n"
        "TODO_LOOP_SEC=120\n"
        "PYTHONUNBUFFERED=1\n"
    ]
    lines.append(f"AGENT_TYPE={req.agent_type}\n")
    if req.agent_type == 'codex':
        lines.extend([
            "CODEX_PROVIDER_ENABLED=true\n",
            f"CODEX_HOME={req.codex_home_dir()}\n",
            f"CODEX_WORKSPACE={req.codex_workspace or ''}\n",
            f"CODEX_MODEL={req.codex_model or ''}\n",
            f"CODEX_AUTH_MODE={req.codex_auth_mode}\n",
        ])
    return ''.join(lines)


def _render_sidecar_unit(req: DeployRequest) -> str:
    data = req.systemd_data_dir()
    home = f"{data}/home"
    user = req.systemd_service_user()
    env_path = f"{data}/scripts/sidecar.env"
    script_path = f"{data}/scripts/sidecar_v2.py"
    read_write_paths = ' '.join([data, AGENT_SHARE_DIR] + (req.work_dirs or []))
    read_only_paths = (
        f"ReadOnlyPaths={req.systemd_install_dir()}\n"
        if req.systemd_install_dir() != data else ""
    )


def _render_codex_sidecar_unit(req: DeployRequest) -> str:
    """Render the Linux Codex provider service under its authenticated user."""
    data = req.systemd_data_dir()
    runtime = req.systemd_install_dir()
    home = f"{data}/home"
    codex_home = req.codex_home_dir()
    workspace = req.codex_workspace or data
    user = req.systemd_service_user()
    py = req.codex_python or f"{runtime}/venv/bin/python"
    env_path = f"{data}/scripts/sidecar.env"
    script_path = f"{data}/scripts/sidecar_v2.py"
    read_write_paths = ' '.join([data, AGENT_SHARE_DIR] + (req.work_dirs or []))
    read_only = ' '.join(dict.fromkeys([runtime, workspace]))
    return (
        "[Unit]\n"
        f"Description=OpenClaw Codex Provider Sidecar (claw {int(req.openclaw_id)})\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"User={user}\n"
        f"Group={AGENT_SERVICE_GROUP}\n"
        f"WorkingDirectory={workspace}\n"
        f"Environment=\"HOME={home}\"\n"
        f"Environment=\"CODEX_HOME={codex_home}\"\n"
        f"EnvironmentFile={env_path}\n"
        f"ExecStart={py} -u {script_path}\n"
        "Restart=always\n"
        "RestartSec=10\n"
        "NoNewPrivileges=true\n"
        "PrivateTmp=true\n"
        "ProtectSystem=strict\n"
        f"ReadWritePaths={read_write_paths}\n"
        f"ReadOnlyPaths={read_only}\n"
        "StandardOutput=journal\n"
        "StandardError=journal\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )
    return (
        "[Unit]\n"
        f"Description=OpenClaw Hub SSE Sidecar v2 (claw {int(req.openclaw_id)})\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"User={user}\n"
        f"Group={AGENT_SERVICE_GROUP}\n"
        f"WorkingDirectory={data}\n"
        f"Environment=\"HOME={home}\"\n"
        f"EnvironmentFile={env_path}\n"
        f"ExecStart=/usr/bin/python3 -u {script_path}\n"
        "Restart=always\n"
        "RestartSec=10\n"
        "NoNewPrivileges=true\n"
        "PrivateTmp=true\n"
        "ProtectSystem=strict\n"
        f"ReadWritePaths={read_write_paths}\n"
        f"{read_only_paths}"
        "StandardOutput=journal\n"
        "StandardError=journal\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def _work_dir_setup_cmd(req: DeployRequest) -> str:
    """Create configured work dirs and make their top-level dirs writable."""
    if not req.work_dirs:
        return "true"
    user = req.systemd_service_user()
    parts = []
    for path in req.work_dirs:
        q = shlex.quote(path)
        parts.append(
            f"mkdir -p {q} && "
            f"chown {shlex.quote(user)}:{shlex.quote(AGENT_SERVICE_GROUP)} {q} && "
            f"chmod 2770 {q}"
        )
    return " && ".join(parts)


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

    def _create_admin_deploy_notify_todos(dep) -> None:
        """部署结束后让龙虾王代发企微通知给 owner 用户。"""
        try:
            from app.models import ClawTodo, OpenClawInstance
            claw = OpenClawInstance.query.get(dep.openclaw_id)
            if not claw:
                return
            admin_claws = OpenClawInstance.query.filter(
                OpenClawInstance.role == 'admin',
                OpenClawInstance.status != 'deleted',
            ).all()
            if not admin_claws:
                logger.warning('agent deployment %s finished but no admin claw found', dep.id)
                return
            status_text = '成功' if dep.status == 'success' else '失败'
            target_user = (claw.owner or '').strip() or '未配置 owner'
            provider_name = 'Codex' if dep.agent_type == 'codex' else 'Hermes'
            desc_lines = [
                f'OpenClaw「{claw.name}」的 {provider_name} Agent 部署已{status_text}。',
                '',
                f'请给用户 `{target_user}` 发送企微通知。',
                '',
                f'- OpenClaw ID：{claw.id}',
                f'- OpenClaw 名称：{claw.name}',
                f'- 所属用户：{target_user}',
                f'- 部署状态：{dep.status}',
                f'- 目标机：{dep.ssh_user or ""}@{dep.host or ""}',
                f'- Systemd unit：{dep.container_name or ""}',
                f'- 目录：{dep.remote_base_dir or ""}',
            ]
            if dep.error_message:
                desc_lines.extend(['', f'失败原因：{dep.error_message}'])
            desc_lines.extend([
                '',
                '通知建议文案：',
                f'{provider_name} Agent 部署已{status_text}：{claw.name}（OpenClaw #{claw.id}）。',
            ])
            for admin in admin_claws:
                exists = ClawTodo.query.filter_by(
                    openclaw_id=admin.id,
                    verification_target=f'agent-deploy-notify:{dep.id}',
                    enabled=True,
                ).first()
                if exists:
                    continue
                db.session.add(ClawTodo(
                    openclaw_id=admin.id,
                    title=f'通知用户 {provider_name} Agent 部署{status_text}：{claw.name}',
                    description='\n'.join(desc_lines),
                    schedule_type='once',
                    urgency_level='interrupt',
                    priority='P0',
                    task_category='routine',
                    verification_target=f'agent-deploy-notify:{dep.id}',
                    enabled=True,
                    created_by='Hub',
                ))
            db.session.commit()
            try:
                from app.api.agent_client import notify_claw
                for admin in admin_claws:
                    notify_claw(admin.id)
            except Exception:
                logger.exception('notify admin claw failed for deployment %s', dep.id)
        except Exception:
            db.session.rollback()
            logger.exception('create admin deploy notify todo failed for deployment %s', dep.id)

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
            if status in ('success', 'failed'):
                _create_admin_deploy_notify_todos(dep)

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
                if req.agent_type == 'codex':
                    _deploy_codex_systemd(ssh, req, steps, _flush)
                else:
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

    # 1) 创建隔离目录骨架：/opt/openclaw-agents/<claw>/ 私有，/opt/agent_share 共享
    mkdir_cmd = (
        f"mkdir -p "
        f"{shlex.quote(AGENT_ROOT_DIR)} "
        f"{shlex.quote(AGENT_SHARE_DIR)} "
        f"{shlex.quote(base + '/hermes/config')} "
        f"{shlex.quote(base + '/hermes/sessions')} "
        f"{shlex.quote(base + '/hermes/logs')} && "
        f"chmod 755 {shlex.quote(AGENT_ROOT_DIR)} && "
        f"chmod 1777 {shlex.quote(AGENT_SHARE_DIR)} && "
        f"chmod 700 {shlex.quote(base)} && "
        f"chmod 700 {shlex.quote(base + '/hermes')} && "
        f"chmod 700 {shlex.quote(base + '/hermes/sessions')} "
        f"{shlex.quote(base + '/hermes/logs')} && "
        f"chmod 500 {shlex.quote(base + '/hermes/config')}"
    )
    sr = ssh.run(mkdir_cmd, timeout=30, name='mkdir per-claw dirs')
    _record_step(steps, sr)
    if not sr.ok:
        _flush('failed', f'创建目录失败：{_tail(sr.stderr, 400)}')
        return

    # 2) 写 config.yaml / .env（敏感值经 base64 中转，避免被 shell 展开）
    sr = ssh.put_text(_render_config_yaml(req),
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
        f'--read-only '
        f'--tmpfs /tmp:rw,noexec,nosuid,size=128m '
        f'--tmpfs /run:rw,noexec,nosuid,size=64m '
        f'--cap-drop ALL '
        f'--security-opt no-new-privileges:true '
        f'-v {shlex.quote(base + "/hermes/config")}:/app/config:ro '
        f'-v {shlex.quote(base + "/hermes/sessions")}:/app/sessions '
        f'-v {shlex.quote(base + "/hermes/logs")}:/app/logs '
        f'-v {shlex.quote(AGENT_SHARE_DIR)}:/agent_share '
        f'--env-file {shlex.quote(base + "/hermes/config/.env")} '
        f'-e HERMES_CODEX_STREAMING=false '
        f'-e AGENT_PRIVATE_DIR=/app/sessions '
        f'-e AGENT_SHARE_DIR=/agent_share '
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


def _deploy_codex_systemd(ssh: '_SSHRunner', req: DeployRequest, steps: list, _flush) -> None:
    """Install and supervise the Linux Codex SDK provider with systemd.

    Authentication is deliberately out-of-band: the CLI and SDK reuse the
    service user's ``HOME/.codex`` state. Hub only checks ``codex login status``
    and never reads, copies, or logs credential files.
    """
    runtime = req.systemd_install_dir()
    data = req.systemd_data_dir()
    workspace = req.codex_workspace or ''
    requirements = req.codex_requirements or f"{runtime}/requirements-codex.lock"
    wheelhouse = req.codex_wheelhouse or f"{runtime}/wheelhouse"
    expected_sha = (req.codex_requirements_sha256 or '').lower()
    py = req.codex_python or f"{runtime}/venv/bin/python"
    cli = req.codex_cli or '/usr/local/bin/codex'
    user = req.systemd_service_user()
    home = f"{data}/home"
    codex_home = req.codex_home_dir()
    unit_name = req.container_name()
    unit_path = f"/etc/systemd/system/{unit_name}"
    sidecar_dir = f"{data}/scripts"
    sidecar_script = f"{sidecar_dir}/sidecar_v2.py"
    sidecar_env_path = f"{sidecar_dir}/sidecar.env"

    if not all((runtime, data, workspace, requirements, wheelhouse, expected_sha)):
        _flush('failed', 'Codex systemd 部署缺少 runtime/data/workspace/离线依赖锁文件参数')
        return
    if not _SHA256_RE.fullmatch(expected_sha):
        _flush('failed', 'codex_requirements_sha256 必须是 64 位十六进制 SHA-256')
        return

    sr = ssh.run('command -v systemctl && systemctl --version | head -n1',
                 timeout=20, name='systemctl --version')
    _record_step(steps, sr)
    if not sr.ok:
        _flush('failed', '目标机没有 systemctl，无法部署 Linux Codex provider。')
        return

    bootstrap = ssh.run(
        "python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' "
        "&& python3 -V",
        timeout=20, name='python >= 3.10')
    _record_step(steps, bootstrap)
    if not bootstrap.ok:
        _flush('failed', 'Linux Codex provider 需要 Python 3.10 或更高版本。')
        return

    artifact_check = ssh.run(
        f"test -f {shlex.quote(requirements)} && "
        f"test -d {shlex.quote(wheelhouse)} && "
        f"test \"$(sha256sum {shlex.quote(requirements)} | awk '{{print $1}}')\" = "
        f"{shlex.quote(expected_sha)}",
        timeout=30, name='verify codex offline artifacts')
    _record_step(steps, artifact_check)
    if not artifact_check.ok:
        _flush('failed', 'Codex 离线依赖制品不存在或 requirements SHA-256 不匹配；拒绝联网临时安装。')
        return

    install = ssh.run(
        f"mkdir -p {shlex.quote(runtime)} && "
        f"python3 -m venv {shlex.quote(runtime + '/venv')} && "
        f"{shlex.quote(py)} -m pip install --disable-pip-version-check "
        f"--no-index --find-links {shlex.quote(wheelhouse)} --only-binary=:all: "
        f"--require-hashes -r {shlex.quote(requirements)} && "
        f"{shlex.quote(py)} -c 'import openai_codex'",
        timeout=600, name='install codex sdk from wheelhouse')
    _record_step(steps, install)
    if not install.ok:
        _flush('failed', f'Codex SDK 离线安装失败：{_tail(install.stderr or install.stdout, 800)}')
        return

    setup = ssh.run(
        f"groupadd -r {shlex.quote(AGENT_SERVICE_GROUP)} 2>/dev/null || true; "
        f"id -u {shlex.quote(user)} >/dev/null 2>&1 || "
        f"useradd -r -g {shlex.quote(AGENT_SERVICE_GROUP)} -d {shlex.quote(home)} "
        f"-s /sbin/nologin {shlex.quote(user)}; "
        f"mkdir -p {shlex.quote(AGENT_ROOT_DIR)} {shlex.quote(AGENT_SHARE_DIR)} "
        f"{shlex.quote(data)} {shlex.quote(home)} {shlex.quote(codex_home)} "
        f"{shlex.quote(sidecar_dir)}; "
        f"test -d {shlex.quote(workspace)}; "
        f"chown root:{shlex.quote(AGENT_SERVICE_GROUP)} {shlex.quote(AGENT_ROOT_DIR)} "
        f"{shlex.quote(AGENT_SHARE_DIR)}; "
        f"chmod 0755 {shlex.quote(AGENT_ROOT_DIR)}; "
        f"chmod 2770 {shlex.quote(AGENT_SHARE_DIR)}; "
        f"chown -R {shlex.quote(user)}:{shlex.quote(AGENT_SERVICE_GROUP)} {shlex.quote(data)}; "
        f"chmod 0700 {shlex.quote(data)} {shlex.quote(home)} {shlex.quote(codex_home)} "
        f"{shlex.quote(sidecar_dir)}",
        timeout=60, name='create codex service user and private dirs')
    _record_step(steps, setup)
    if not setup.ok:
        _flush('failed', f'创建 Codex 服务用户/目录失败：{_tail(setup.stderr, 500)}')
        return

    sr = ssh.run(_work_dir_setup_cmd(req), timeout=60, name='mkdir/chown work dirs')
    _record_step(steps, sr)
    if not sr.ok:
        _flush('failed', f'工作目录授权失败：{_tail(sr.stderr or sr.stdout, 500)}')
        return

    cli_check = ssh.run(f"test -x {shlex.quote(cli)} && {shlex.quote(cli)} --version",
                        timeout=30, name='codex cli --version')
    _record_step(steps, cli_check)
    if not cli_check.ok:
        _flush('failed', f'未找到 Codex CLI：{cli}；请先安装 CLI，或显式传 codex_cli 绝对路径。')
        return

    auth_cmd = (
        f"runuser -u {shlex.quote(user)} -- env HOME={shlex.quote(home)} "
        f"CODEX_HOME={shlex.quote(codex_home)} {shlex.quote(cli)} login status"
    )
    auth = ssh.run(auth_cmd, timeout=30, name='codex login status')
    _record_step(steps, StepResult(
        name='codex login status', exit_code=auth.exit_code,
        stdout='authenticated' if auth.ok else '',
        stderr='' if auth.ok else 'authentication required'))
    if not auth.ok:
        if req.codex_auth_mode == 'api_key':
            action = (
                f"以 {user} 用户设置临时 OPENAI_API_KEY，再通过 stdin 执行 "
                f"{cli} login --with-api-key；完成后立即 unset"
            )
        elif req.codex_auth_mode == 'enterprise_access_token':
            action = (
                f"确认企业工作区和当前 CLI 已开放 Access Token 登录后，以 {user} 用户完成登录"
            )
        else:
            action = (
                f"执行 sudo -u {user} env HOME={home} CODEX_HOME={codex_home} "
                f"{cli} login --device-auth，然后重新部署"
            )
        _flush('failed', f'Codex 运行用户尚未认证。{action}。Hub 不会接收或复制 auth.json。')
        return

    base_url = (req.hub_url or '').rstrip('/')
    if not (base_url and req.claw_token):
        _flush('failed', 'Codex provider 必须提供 hub_url 与 claw_token 才能部署 Sidecar。')
        return

    asset_root = f"{base_url}/static/skills/hub-sse-sidecar-v2/scripts"
    download_cmd = (
        f"ASSET_ROOT={shlex.quote(asset_root)} ASSET_DIR={shlex.quote(sidecar_dir)} "
        f"{shlex.quote(py)} - <<'PY'\n"
        "import os, urllib.request\n"
        "root = os.environ['ASSET_ROOT'].rstrip('/')\n"
        "dest = os.environ['ASSET_DIR']\n"
        "checks = {\n"
        "    'sidecar_v2.py': b'#!/usr/bin/env python3',\n"
        "    'provider_runtime.py': b'class ProviderInvocation',\n"
        "    'codex_sdk_provider.py': b'class CodexSdkProvider',\n"
        "}\n"
        "for name, marker in checks.items():\n"
        "    with urllib.request.urlopen(root + '/' + name, timeout=30) as response:\n"
        "        content = response.read()\n"
        "    if marker not in content:\n"
        "        raise SystemExit('unexpected provider asset: ' + name)\n"
        "    with open(os.path.join(dest, name), 'wb') as handle:\n"
        "        handle.write(content)\n"
        "PY"
    )
    sr = ssh.run(download_cmd, timeout=90, name='download codex provider assets')
    _record_step(steps, sr)
    if not sr.ok:
        _flush('failed', f'下载 Codex provider 制品失败：{_tail(sr.stderr, 600)}')
        return

    sr = ssh.put_text(_render_sidecar_env(req), sidecar_env_path,
                      mode='0600', name='write codex sidecar.env')
    _record_step(steps, sr)
    if not sr.ok:
        _flush('failed', '写 Codex sidecar.env 失败')
        return

    permissions = ssh.run(
        f"chown -R {shlex.quote(user)}:{shlex.quote(AGENT_SERVICE_GROUP)} "
        f"{shlex.quote(sidecar_dir)} && "
        f"chmod 0700 {shlex.quote(sidecar_dir)} && "
        f"chmod 0755 {shlex.quote(sidecar_script)} && "
        f"chmod 0644 {shlex.quote(sidecar_dir + '/provider_runtime.py')} "
        f"{shlex.quote(sidecar_dir + '/codex_sdk_provider.py')} && "
        f"chmod 0600 {shlex.quote(sidecar_env_path)}",
        timeout=30, name='secure codex provider assets')
    _record_step(steps, permissions)
    if not permissions.ok:
        _flush('failed', f'Codex provider 文件权限设置失败：{_tail(permissions.stderr, 400)}')
        return

    import_check = ssh.run(
        f"runuser -u {shlex.quote(user)} -- env HOME={shlex.quote(home)} "
        f"CODEX_HOME={shlex.quote(codex_home)} PYTHONPATH={shlex.quote(sidecar_dir)} "
        f"{shlex.quote(py)} -c 'from codex_sdk_provider import CodexSdkProvider; "
        "assert CodexSdkProvider().capabilities().readiness != \"unavailable\"'",
        timeout=30, name='import codex provider')
    _record_step(steps, import_check)
    if not import_check.ok:
        _flush('failed', f'Codex provider 自检失败：{_tail(import_check.stderr, 600)}')
        return

    preflight_url = f"{base_url}/api/openclaws/{int(req.openclaw_id)}/sidecar-config?sidecar_version=2.6.0"
    sr = ssh.run(
        f"curl -fsS -H {shlex.quote('Authorization: Bearer ' + req.claw_token)} "
        f"{shlex.quote(preflight_url)} >/tmp/openclaw-sidecar-preflight-{int(req.openclaw_id)}.json",
        timeout=30, name='codex Hub config preflight')
    _record_step(steps, sr)
    if not sr.ok:
        _flush('failed', f'Codex sidecar Hub 连通/鉴权预检失败：{_tail(sr.stderr, 500)}')
        return

    sr = ssh.put_text(_render_codex_sidecar_unit(req), unit_path,
                      mode='0644', name=f'write {unit_name}')
    _record_step(steps, sr)
    if not sr.ok:
        _flush('failed', f'写 Codex systemd unit 失败：{_tail(sr.stderr, 400)}')
        return

    sr = ssh.run(
        f"systemctl daemon-reload && systemctl enable {shlex.quote(unit_name)} && "
        f"systemctl restart {shlex.quote(unit_name)}",
        timeout=90, name='enable/restart codex sidecar')
    _record_step(steps, sr)
    if not sr.ok:
        _flush('failed', f'Codex sidecar 启动失败：{_tail(sr.stderr, 600)}')
        return

    time.sleep(3)
    sr = ssh.run(f"systemctl is-active {shlex.quote(unit_name)}",
                 timeout=20, name='systemctl is-active codex sidecar')
    _record_step(steps, sr)
    if not (sr.ok and sr.stdout.strip() == 'active'):
        logs = ssh.run(f"journalctl -u {shlex.quote(unit_name)} -n 80 --no-pager",
                       timeout=30, name='journalctl codex sidecar')
        _record_step(steps, logs)
        _flush('failed', f'Codex sidecar 启动后未保持 active：{_tail(logs.stdout or logs.stderr, 800)}')
        return

    # Only activate the Hub-side provider identity after the local service is
    # proven active, so a failed migration cannot disrupt a live Hermes worker.
    selfcheck_url = (
        f"{base_url}/api/openclaws/{int(req.openclaw_id)}/sidecar-config"
        f"?sidecar_version=2.6.0&agent_type=codex"
        f"&agent_name=main&agent_timeout=300"
    )
    sr = ssh.run(
        f"curl -fsS -H {shlex.quote('Authorization: Bearer ' + req.claw_token)} "
        f"{shlex.quote(selfcheck_url)} >/tmp/openclaw-sidecar-selfcheck-{int(req.openclaw_id)}.json",
        timeout=30, name='activate codex sidecar config')
    _record_step(steps, sr)
    if not sr.ok:
        _flush('failed', f'Codex sidecar 已启动但 Hub provider 激活失败：{_tail(sr.stderr, 500)}')
        return
    _flush('success')


def _deploy_systemd(ssh: '_SSHRunner', req: DeployRequest, steps: list, _flush) -> None:
    """systemd：按《Hermes Agent 标准化部署指南》v2 写数据目录 + unit。

    **拆分目录**：``install_dir`` = 源码+venv，``data_dir`` = HERMES_HOME（根目录
    ``config.yaml``）。**单目录兼容**：仅 ``hermes_home``，配置仍在 ``config/`` 子目录。

    Hub 不远程 git clone / pip install；venv 与源码须已就绪。
    """
    inst = req.systemd_install_dir()
    data = req.systemd_data_dir()
    unit_name = req.container_name()
    unit_path = f"/etc/systemd/system/{unit_name}"
    py = req.hermes_python or f"{inst}/venv/bin/python"
    split = req.systemd_split_layout()
    start_mode = (req.hermes_start_mode or 'gateway').lower()
    service_user = req.systemd_service_user()

    if not inst or not data:
        _flush('failed',
               'systemd 请填写「安装目录 + 数据目录」或单独填写「单目录 hermes_home」'
               '（参见部署指南 v2）')
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
               f'未在目标机找到可执行 Python：{py}。'
               f'请确认 venv 在 {shlex.quote(inst + "/venv")}，或显式传 hermes_python。')
        return

    if start_mode == 'gateway':
        sr = ssh.run(f"{shlex.quote(py)} -c 'import hermes_cli' 2>&1",
                     timeout=30, name='import hermes_cli')
        _record_step(steps, sr)
        if not sr.ok:
            _flush('failed',
                   'venv 无法 import hermes_cli（Gateway 模式）。'
                   '请升级 Hermes 源码，或在部署参数中将 hermes_start_mode 设为 module。')
            return
    else:
        mod = req.hermes_module or 'hermes_agent'
        sr = ssh.run(f"{shlex.quote(py)} -c 'import {mod}' 2>&1",
                     timeout=30, name=f'import {mod}')
        _record_step(steps, sr)
        if not sr.ok:
            _flush('failed',
                   f'venv 无法 import {mod}，请确认源码已安装到 {inst}。')
            return

    # 1) 隔离目录、共享目录、独立服务用户
    if split:
        private_dirs = [data, f'{data}/home', f'{data}/sessions', f'{data}/logs', f'{data}/scripts']
    else:
        private_dirs = [data, f'{data}/home', f'{data}/config', f'{data}/sessions', f'{data}/logs']
    mkdir_cmd = (
        f"groupadd -r {shlex.quote(AGENT_SERVICE_GROUP)} 2>/dev/null || true; "
        f"id -u {shlex.quote(service_user)} >/dev/null 2>&1 || "
        f"useradd -r -g {shlex.quote(AGENT_SERVICE_GROUP)} "
        f"-d {shlex.quote(data + '/home')} -s /sbin/nologin "
        f"{shlex.quote(service_user)}; "
        f"usermod -a -G {shlex.quote(AGENT_SERVICE_GROUP)} {shlex.quote(service_user)} 2>/dev/null || true; "
        f"mkdir -p {shlex.quote(AGENT_ROOT_DIR)} {shlex.quote(AGENT_SHARE_DIR)} "
        + ' '.join(shlex.quote(p) for p in private_dirs) + "; "
        f"chown root:{shlex.quote(AGENT_SERVICE_GROUP)} {shlex.quote(AGENT_ROOT_DIR)} "
        f"{shlex.quote(AGENT_SHARE_DIR)}; "
        f"chmod 0755 {shlex.quote(AGENT_ROOT_DIR)}; "
        f"chmod 2770 {shlex.quote(AGENT_SHARE_DIR)}; "
        f"chown -R {shlex.quote(service_user)}:{shlex.quote(AGENT_SERVICE_GROUP)} {shlex.quote(data)}; "
        f"chmod 0700 {shlex.quote(data)} {shlex.quote(data + '/home')}; "
        f"chmod 0700 {shlex.quote(data + '/sessions')} {shlex.quote(data + '/logs')}"
    )
    sr = ssh.run(mkdir_cmd, timeout=30, name='mkdir hermes data dirs')
    _record_step(steps, sr)
    if not sr.ok:
        _flush('failed', f'mkdir 失败：{_tail(sr.stderr, 300)}')
        return

    sr = ssh.run(_work_dir_setup_cmd(req), timeout=60, name='mkdir/chown work dirs')
    _record_step(steps, sr)
    if not sr.ok:
        _flush('failed', f'工作目录授权失败：{_tail(sr.stderr or sr.stdout, 500)}')
        return

    # 清理 #135/旧 manager-hub 时代遗留的 SSE 客户端，避免与 v2 sidecar 抢消息。
    cleanup_old_sidecar_cmd = (
        "for p in $(pgrep -f 'sse_client.py$|hub_sse_client_daemon.py$' 2>/dev/null || true); do "
        "  kill -TERM \"$p\" 2>/dev/null || true; "
        "done; "
        f"ts=$(date +%Y%m%d%H%M%S); "
        f"for f in {shlex.quote(data + '/scripts/sse_client.py')} "
        f"{shlex.quote(data + '/scripts/hub_sse_client_daemon.py')}; do "
        f"  [ -f \"$f\" ] && mv \"$f\" \"$f.disabled.$ts\" || true; "
        f"done"
    )
    sr = ssh.run(cleanup_old_sidecar_cmd, timeout=20, name='cleanup legacy sidecar clients')
    _record_step(steps, sr)
    if not sr.ok:
        _flush('failed', f'清理旧 sidecar 客户端失败：{_tail(sr.stderr, 300)}')
        return

    # 2) config.yaml / .env
    if split:
        cfg_path = f'{data}/config.yaml'
        env_path = f'{data}/.env'
    else:
        cfg_path = f'{data}/config/config.yaml'
        env_path = f'{data}/config/.env'

    sr = ssh.put_text(_render_config_yaml(req), cfg_path,
                      mode='0644', name='write config.yaml')
    _record_step(steps, sr)
    if not sr.ok:
        _flush('failed', '写 config.yaml 失败')
        return

    sr = ssh.put_text(_render_env_file(req), env_path,
                      mode='0600', name='write .env')
    _record_step(steps, sr)
    if not sr.ok:
        _flush('failed', '写 .env 失败')
        return

    # put_text runs as SSH user (usually root). The systemd service runs as the
    # isolated oclaw_<id> user, so the EnvironmentFile must be owned by it.
    sr = ssh.run(
        f"chown {shlex.quote(service_user)}:{shlex.quote(AGENT_SERVICE_GROUP)} "
        f"{shlex.quote(cfg_path)} {shlex.quote(env_path)} && "
        f"chmod 0644 {shlex.quote(cfg_path)} && "
        f"chmod 0600 {shlex.quote(env_path)}",
        timeout=20, name='chown hermes config files')
    _record_step(steps, sr)
    if not sr.ok:
        _flush('failed', f'调整配置文件权限失败：{_tail(sr.stderr, 300)}')
        return

    verify_cmd = (
        f"test -r {shlex.quote(cfg_path)} && "
        f"test -r {shlex.quote(env_path)} && "
        f"grep -q '^VENUS_API_KEY=' {shlex.quote(env_path)} && "
        f"grep -q '^HERMES_CONFIG_PATH=' {shlex.quote(env_path)} && "
        f"grep -q '^model:' {shlex.quote(cfg_path)}"
    )
    if req.wecom_bot_id and req.wecom_bot_secret:
        verify_cmd += (
            f" && grep -q '^WECOM_KEY=' {shlex.quote(env_path)}"
            f" && grep -q '^WECOM_SECRET=' {shlex.quote(env_path)}"
            f" && grep -q '^WECOM_ALLOW_ALL_USERS=true$' {shlex.quote(env_path)}"
            f" && grep -q '^wecom:' {shlex.quote(cfg_path)}"
        )
    sr = ssh.run(verify_cmd, timeout=20, name='verify hermes config/env')
    _record_step(steps, sr)
    if not sr.ok:
        _flush('failed', 'Hermes config.yaml/.env 自检失败：缺少 Venus 或 WeCom 必需配置')
        return

    # 3) systemd unit
    sr = ssh.put_text(_render_systemd_unit(req), unit_path, mode='0644',
                      name=f'write {unit_name}')
    _record_step(steps, sr)
    if not sr.ok:
        _flush('failed', f'写 systemd unit 失败：{_tail(sr.stderr, 400)}')
        return

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

    time.sleep(5)
    sr = ssh.run(f'systemctl is-active {shlex.quote(unit_name)}',
                 timeout=20, name=f'systemctl is-active {unit_name}')
    _record_step(steps, sr)
    is_active_ok = sr.ok and 'active' in sr.stdout.strip()

    log_sr = ssh.run(
        f'journalctl -u {shlex.quote(unit_name)} -n 80 --no-pager',
        timeout=30, name='journalctl hermes-gateway')
    _record_step(steps, log_sr)

    if not is_active_ok:
        _flush('failed', f'systemd 服务启动后未保持 active：{_tail(sr.stdout, 200)}')
        return

    # 5) Hub SSE sidecar v2：Hermes Gateway 只负责消息平台/cron，
    # Hub 通信中心的消息/待办需要 sidecar 消费 SSE 并调用 Hermes CLI。
    # 历史教训：曾经写成 `if req.hub_url and req.claw_token`，当
    # `system_config.deploy_default_hub_url` 漏配 / token 拿不到时，会静默跳过
    # sidecar 但部署仍标记 success，导致 agent online 却收不到消息。
    # 现在改为硬校验：缺一即 failed，不再静默跳过。
    if not (req.hub_url and req.claw_token):
        _flush('failed',
               'Hub 代建 Hermes Agent 必须提供 hub_url 与 claw_token 才能部署 SSE sidecar，'
               '当前两者至少有一项缺失（system_config 的 deploy_default_hub_url 是否配置？'
               'OpenClaw.api_token 是否生成？）')
        return

    if True:
        sidecar_unit = _sidecar_unit_name(req)
        sidecar_unit_path = f"/etc/systemd/system/{sidecar_unit}"
        sidecar_script = f"{data}/scripts/sidecar_v2.py"
        wrapper_path = f"{data}/scripts/hermes_sidecar_wrapper.sh"
        sidecar_env_path = f"{data}/scripts/sidecar.env"
        sidecar_url = (
            f"{(req.hub_url or '').rstrip('/')}"
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
        sr = ssh.run(download_cmd, timeout=45, name='download sidecar_v2.py')
        _record_step(steps, sr)
        if not sr.ok:
            _flush('failed', f'下载 sidecar_v2.py 失败：{_tail(sr.stderr, 500)}')
            return

        sr = ssh.put_text(_render_sidecar_wrapper(req), wrapper_path,
                          mode='0755', name='write hermes sidecar wrapper')
        _record_step(steps, sr)
        if not sr.ok:
            _flush('failed', '写 Hermes sidecar wrapper 失败')
            return

        sr = ssh.put_text(_render_sidecar_env(req), sidecar_env_path,
                          mode='0600', name='write sidecar.env')
        _record_step(steps, sr)
        if not sr.ok:
            _flush('failed', '写 sidecar.env 失败')
            return

        sr = ssh.run(
            f"chown -R {shlex.quote(service_user)}:{shlex.quote(AGENT_SERVICE_GROUP)} "
            f"{shlex.quote(data + '/scripts')} && "
            f"chmod 0700 {shlex.quote(data + '/scripts')} && "
            f"chmod 0755 {shlex.quote(sidecar_script)} {shlex.quote(wrapper_path)} && "
            f"chmod 0600 {shlex.quote(sidecar_env_path)}",
            timeout=20, name='chown sidecar files')
        _record_step(steps, sr)
        if not sr.ok:
            _flush('failed', f'调整 sidecar 文件权限失败：{_tail(sr.stderr, 300)}')
            return

        # 初始化/刷新 Hub 侧 sidecar 配置中心，确保 v2 知道要调用 Hermes wrapper。
        selfcheck_url = (
            f"{(req.hub_url or '').rstrip('/')}/api/openclaws/{int(req.openclaw_id)}/sidecar-config"
            f"?sidecar_version=2.0.1&agent_type=hermes"
            f"&openclaw_bin={urllib.parse.quote(wrapper_path, safe='')}"
            f"&hermes_home={urllib.parse.quote(data, safe='')}"
            f"&agent_name=main&agent_timeout=300"
        )
        selfcheck_cmd = (
            f"curl -fsS -H {shlex.quote('Authorization: Bearer ' + (req.claw_token or ''))} "
            f"{shlex.quote(selfcheck_url)} >/tmp/openclaw-sidecar-selfcheck-{int(req.openclaw_id)}.json"
        )
        sr = ssh.run(selfcheck_cmd, timeout=30, name='sidecar-config selfcheck')
        _record_step(steps, sr)
        if not sr.ok:
            _flush('failed', f'sidecar 配置自检失败：{_tail(sr.stderr, 500)}')
            return

        sr = ssh.put_text(_render_sidecar_unit(req), sidecar_unit_path, mode='0644',
                          name=f'write {sidecar_unit}')
        _record_step(steps, sr)
        if not sr.ok:
            _flush('failed', f'写 sidecar systemd unit 失败：{_tail(sr.stderr, 400)}')
            return

        sr = ssh.run('systemctl daemon-reload', timeout=30,
                     name='systemctl daemon-reload sidecar')
        _record_step(steps, sr)
        if not sr.ok:
            _flush('failed', f'sidecar daemon-reload 失败：{_tail(sr.stderr, 300)}')
            return

        sr = ssh.run(f'systemctl enable {shlex.quote(sidecar_unit)}',
                     timeout=30, name=f'systemctl enable {sidecar_unit}')
        _record_step(steps, sr)

        sr = ssh.run(f'systemctl restart {shlex.quote(sidecar_unit)}',
                     timeout=60, name=f'systemctl restart {sidecar_unit}')
        _record_step(steps, sr)
        if not sr.ok:
            _flush('failed', f'sidecar restart 失败：{_tail(sr.stderr, 600)}')
            return

        time.sleep(3)
        sr = ssh.run(f'systemctl is-active {shlex.quote(sidecar_unit)}',
                     timeout=20, name=f'systemctl is-active {sidecar_unit}')
        _record_step(steps, sr)
        if not (sr.ok and 'active' in sr.stdout.strip()):
            log_sr = ssh.run(
                f'journalctl -u {shlex.quote(sidecar_unit)} -n 80 --no-pager',
                timeout=30, name='journalctl sidecar')
            _record_step(steps, log_sr)
            _flush('failed', f'sidecar 启动后未保持 active：{_tail(sr.stdout, 200)}')
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
