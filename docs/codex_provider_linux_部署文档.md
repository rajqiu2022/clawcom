# Linux Codex Provider 部署文档

## 1. 正式支持范围

Hub 统一部署器支持 Linux systemd `hermes` 与 `codex` 两条独立路径。Pi 已退役；
`agent_type=pi` 会直接失败，不能作为默认值或 fallback。

Codex 部署只托管 `openclaw-sidecar-v2-claw-<id>.service`。Sidecar 通过 Python
Codex SDK 处理 `message`、`todo`、`workflow` 和企微消息，权限固定为
`cognitive_only/repo_read`；有副作用的动作继续交给受控 Job Service。

Codex turn 会进入单独的 Linux 进程组，并占用 Sidecar 全局执行槽。达到
`agent_timeout` 后 Sidecar 会终止 SDK 及其 app-server 整棵进程树。子进程只继承
`HOME`、`CODEX_HOME`、PATH、语言、代理和 CA 等白名单变量，不继承 `CLAW_TOKEN`、
企微 secret、`OPENAI_API_KEY` 或 `CODEX_ACCESS_TOKEN`。

## 2. 制作锁版本离线运行包

仓库固定的 Linux x86_64 基线见：

```text
ops/codex-runtime-linux-x86_64.json
```

当前批准版本为 `openai-codex==0.144.4`，目标为 Python 3.10+ / glibc x86_64；
依赖下载固定为 `abi3`，避免把运行包误锁到某一个 CPython 小版本。
在能访问 PyPI 的受控 Linux 构建机执行：

```bash
python3.10 ops/codex_runtime_release.py build \
  --output /tmp/codex-runtime-linux-x86_64

python3.10 ops/codex_runtime_release.py verify \
  --runtime /tmp/codex-runtime-linux-x86_64
```

构建工具调用 `pip download --only-binary=:all:` 解析全部传递依赖，然后从每个 wheel
的 `METADATA` 生成精确版本及 SHA-256 锁。它还会强制 SDK 与
`openai-codex-cli-bin` 同版本，并生成：

```text
codex-runtime-linux-x86_64/
├── codex-runtime-manifest.json
├── requirements-codex.lock
└── wheelhouse/
    └── *.whl
```

`verify` 会拒绝多余/缺失/重复/非 wheel 文件、wheel 被篡改、锁文件被篡改、SDK 与
CLI runtime 版本漂移。将整个目录原样发布到目标机 `/opt/codex-runtime`，不要只复制
requirements。部署请求中的 `codex_requirements_sha256` 取 manifest 的同名字段：

```bash
python3 - <<'PY'
import json
print(json.load(open('/opt/codex-runtime/codex-runtime-manifest.json'))
      ['requirements_sha256'])
PY
```

Hub 部署器只允许以下离线安装，不会临时访问包索引：

```bash
python -m pip install \
  --no-index \
  --find-links /opt/codex-runtime/wheelhouse \
  --only-binary=:all: \
  --require-hashes \
  -r /opt/codex-runtime/requirements-codex.lock
```

升级 SDK 时先修改批准配置、重新构建并验证完整目录，再评审 config、manifest 和
测试结果；禁止把 `<approved-version>` 或 `latest` 带入正式部署。

## 3. 默认认证：ChatGPT/Codex 订阅账号

Linux SDK 复用服务用户的 Codex CLI 登录状态。每个 Agent 以独立的 `oclaw_<id>`
运行，并使用：

```text
HOME=/opt/openclaw-agents/claw-<id>-<safe>/data/home
CODEX_HOME=/opt/openclaw-agents/claw-<id>-<safe>/data/home/.codex
```

首次部署会创建用户和私有目录；尚未登录时会安全失败并返回下一步命令。管理员应在
目标机以该服务用户执行无界面设备码登录：

```bash
sudo -u oclaw_<id> env \
  HOME=<data>/home \
  CODEX_HOME=<data>/home/.codex \
  /usr/local/bin/codex login --device-auth

sudo -u oclaw_<id> env \
  HOME=<data>/home \
  CODEX_HOME=<data>/home/.codex \
  /usr/local/bin/codex login status
```

设备码不可用时，使用同一服务用户执行普通 `codex login` 浏览器流程。Hub 不接收、
读取或复制 `auth.json`。认证目录保持 `0700`、认证文件保持 `0600`；不得提交仓库、
发群、打镜像或写入日志。

### 其他认证方式

API Key 适合无人值守 Worker，但按 OpenAI Platform API 用量计费，不消耗订阅额度。
仍应在目标机通过 stdin 登录，随后清除临时环境变量：

```bash
sudo -u oclaw_<id> env HOME=<data>/home CODEX_HOME=<data>/home/.codex \
  sh -c 'printenv OPENAI_API_KEY | /usr/local/bin/codex login --with-api-key'
unset OPENAI_API_KEY
```

Enterprise Codex Access Token 仅在企业管理员已开放且当前 CLI 明确支持时使用。Hub
只保存 `codex_auth_mode` 运维标识，不接收 token。正式建议顺序为：个人验证使用订阅
账号；共享 Worker 优先 Enterprise Access Token；没有企业条件时再使用项目级 API Key。

## 4. Hub 部署请求

```json
{
  "agent_type": "codex",
  "deploy_method": "systemd",
  "host": "linux-worker.example",
  "ssh_user": "root",
  "codex_runtime_dir": "/opt/codex-runtime",
  "codex_data_dir": "/opt/openclaw-agents/claw-17-codex-worker/data",
  "codex_cli": "/usr/local/bin/codex",
  "codex_workspace": "/srv/projects/example",
  "codex_requirements": "/opt/codex-runtime/requirements-codex.lock",
  "codex_wheelhouse": "/opt/codex-runtime/wheelhouse",
  "codex_requirements_sha256": "<manifest 中的 64 位 SHA-256>",
  "codex_auth_mode": "chatgpt_subscription",
  "codex_model": "<可选的批准模型>",
  "wecom_node_bin": "/usr/bin/node",
  "wecom_sdk_root": "/opt/wecom-runtime/node_modules/@wecom/aibot-node-sdk"
}
```

目标机还需要 Python 3.10、venv、systemd、已存在的 workspace，以及稳定绝对路径的
Codex CLI。Hub 依次完成运行包 SHA 校验、离线安装、服务用户/目录初始化、CLI 登录
状态检查、Sidecar/Provider 文件下载、配置预检、systemd 启用和 active 检查。

## 5. Codex 与企微

启用企微后链路为：

```text
企微官方 Node SDK → Sidecar 鉴权/白名单/去重 → Codex SDK
                → Sidecar 持久化最终结果 → 企微回复
```

bot ID/secret 只写入服务用户可读的 `<data>/wecom-credentials.json`（`0600`）。它们
不会进入 Codex prompt、SDK 参数、子进程环境或日志。`wecom-state.db` 在调用 Codex
前原子占位并保存最终结果，正常重投不会重复消耗额度；过期的 processing 租约才允许
重领。

## 6. 验证与排障

```bash
python3 ops/codex_runtime_release.py verify --runtime /opt/codex-runtime
systemctl status openclaw-sidecar-v2-claw-<id>.service
journalctl -u openclaw-sidecar-v2-claw-<id>.service -n 100 --no-pager
sudo -u oclaw_<id> env HOME=<data>/home CODEX_HOME=<data>/home/.codex \
  /usr/local/bin/codex login status
```

常见错误：

- `authentication_required`：以服务用户完成登录后重新部署；
- `provider_config_invalid`：检查 systemd 中实例独立的 `HOME/CODEX_HOME`；
- `provider_busy`：全局 Codex 执行槽已满，按可重试错误处理；
- `timeout`：SDK/app-server 进程组已被终止，可安全重试；
- `provider_unavailable`：检查锁版本 venv 与部署的 Provider 文件；
- requirements SHA 不匹配：停止部署，重新验证并发布完整 runtime 目录。

Docker 不是当前正式托管合同。后续容器化时必须持久挂载服务账号的 `.codex` 目录，
否则容器重建后需要重新认证。

SDK 的安装、认证复用与接口语义以官方
[Codex SDK](https://learn.chatgpt.com/docs/codex-sdk) 和
[Codex authentication](https://learn.chatgpt.com/docs/auth) 文档为准。
