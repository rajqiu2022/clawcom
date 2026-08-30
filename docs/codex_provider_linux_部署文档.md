# Linux Codex Provider 部署文档

## 1. 正式支持范围

Hub 统一部署器支持 Linux systemd `hermes` 与 `codex` 两条独立路径。Pi 已退役；
`agent_type=pi` 会直接失败，不能作为默认值或 fallback。

新建 Claw 的 `codex` 路径现统一称为 **Claw Worker（Codex SDK）**，部署源改为 Worker
仓库提交的不可变 Linux release，不再由 Hub 从文档重建安装步骤，也不再从
`/static/skills/hub-sse-sidecar-v2/scripts` 拼装 Worker：

```text
Worker dist/worker-releases/index.json
→ Hub 刷新并校验 index/release/platform/package/artifact SHA
→ 超级管理员显式批准候选版本
→ 新建 Claw 固定 worker_release_record_id
→ Hub 上传已批准 tar.gz
→ 执行包内 scripts/install-linux.sh --no-start
→ Hub 注入实例 Token/企微/TimiAI 凭据并启动服务
```

未签名候选版本必须显式提交 `confirm_unsigned=true` 才能批准；刷新版本不会自动升级任何
已部署实例。部署记录保存 release ID、源码 commit 和 artifact SHA，便于审计与回滚。

Claw Worker 部署托管 `claw-worker-codex-<id>.service`。Worker 通过 Python
Codex SDK 处理 `message`、`todo`、`workflow`、企微消息和 Workflow Cycle 复盘。
Codex 是主执行者：默认可读写 `codex_workspace` 与部署请求中的 `work_dirs`，可以通过
`hub_api` 查询/操作 Hub，并可通过 `wecom_reply` 回复已绑定的企微 Owner。Worker 负责
凭据隔离、状态持久化、幂等、超时、权限审批和结果回读，不把模型限制为只读复盘器。

待办以 `task_kind=todo` 和独立 `todo:<id>` 会话调用 Codex；Hub 消息、Workflow 节点和
企微会话也使用稳定的逻辑 session key，真实 `thread_id` 只保存在 Worker 本地的
`codex-state.db`。Sidecar 使用本地凭据调用 Hub，Hub URL、Claw ID、令牌和企微 secret
都不会进入 prompt 或 Codex 子进程环境。

默认工作目录以外的精确目录、网络域名或 Hub 写操作，由 Codex 返回结构化
`permission_request`。已绑定企微 Owner 可选择一次授权或永久授权；Worker 校验请求、
恢复同一 Codex 会话并只为该次调用注入精确权限。禁止授权磁盘根目录、用户根目录、
凭据目录或任意通配资源。

Codex turn 会进入单独的 Linux 进程组，并占用 Sidecar 全局执行槽。达到
`agent_timeout` 后 Sidecar 会终止 SDK 及其 app-server 整棵进程树。子进程只继承
`HOME`、`CODEX_HOME`、PATH、语言、代理和 CA 等白名单变量，不继承 `CLAW_TOKEN`、
企微 secret、`OPENAI_API_KEY` 或 `CODEX_ACCESS_TOKEN`。

## 2. 制作锁版本离线运行包

仓库固定的 Linux x86_64 基线见：

```text
ops/codex-runtime-linux-x86_64.json
```

当前批准版本为 `openai-codex==0.144.4`，统一部署目标为 CPython 3.11 / glibc x86_64；
依赖下载固定为 `cp311`。这是因为 `pydantic-core` 含 CPython ABI wheel，不能把整套依赖错误标记为通用 `abi3`。
在能访问 PyPI 的受控 Linux 构建机执行：

```bash
python3.11 ops/codex_runtime_release.py build \
  --output /tmp/codex-runtime-linux-x86_64

python3.11 ops/codex_runtime_release.py verify \
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

## 3. 模型认证：Codex 订阅或 TimiAI

Hub「新建 OpenClaw → 同时部署 Agent → Claw Worker（Codex SDK）」首期支持两种认证：

- `chatgpt_subscription`（默认）：复用实例服务用户自己的 Codex CLI 登录状态；
- `timiai`：使用 Codex 自定义 `model_providers.timiai`，协议固定为 `responses`，API Key
  从 Hub 密钥箱按项目读取。

TimiAI Key 不写入页面、`agent_deployments`、Sidecar 环境或 Codex SDK 子进程环境。Hub 将
Key 写入实例私有 `0600` 凭据文件，并在 `$CODEX_HOME/config.toml` 配置命令式认证助手；
Codex permission profile 将整个实例目录列为 protected root。该实现对齐
[Codex advanced configuration](https://developers.openai.com/codex/config-advanced/) 的
custom model provider / command-backed auth 合同。

### 默认：ChatGPT/Codex 订阅账号

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
  /opt/codex-runtime/venv/bin/codex login --device-auth

sudo -u oclaw_<id> env \
  HOME=<data>/home \
  CODEX_HOME=<data>/home/.codex \
  /opt/codex-runtime/venv/bin/codex login status
```

设备码不可用时，使用同一服务用户执行普通 `codex login` 浏览器流程。Hub 不接收、
读取或复制 `auth.json`。认证目录保持 `0700`、认证文件保持 `0600`；不得提交仓库、
发群、打镜像或写入日志。

### 其他认证方式

API Key 适合无人值守 Worker，但按 OpenAI Platform API 用量计费，不消耗订阅额度。
仍应在目标机通过 stdin 登录，随后清除临时环境变量：

```bash
sudo -u oclaw_<id> env HOME=<data>/home CODEX_HOME=<data>/home/.codex \
  sh -c 'printenv OPENAI_API_KEY | /opt/codex-runtime/venv/bin/codex login --with-api-key'
unset OPENAI_API_KEY
```

Enterprise Codex Access Token 仅在企业管理员已开放且当前 CLI 明确支持时使用。Hub
只保存 `codex_auth_mode` 运维标识，不接收 token。正式建议顺序为：个人验证使用订阅
账号；共享 Worker 优先 Enterprise Access Token；没有企业条件时再使用项目级 API Key。

### TimiAI 接口

创建页选择 TimiAI 后还需选择 TimiAI 项目与模型。部署器会强制：

```toml
model_provider = "timiai"
model = "deepseek-v4-pro-r1"

[model_providers.timiai]
base_url = "http://api.timiai.woa.com/ai_api_manage/llmproxy"
wire_api = "responses"

[model_providers.timiai.auth]
command = "/opt/codex-runtime/venv/bin/python"
args = ["<instance>/scripts/codex_timiai_auth.py"]
```

认证助手只读取同一实例目录内的 `credentials/timiai-api-key`，并校验它是当前服务用户
拥有的普通文件且权限不宽于 `0600`。切回订阅认证时，部署器会清理该实例旧的 TimiAI
凭据与助手，避免认证模式漂移。

## 4. Hub 部署请求

```json
{
  "agent_type": "codex",
  "worker_release_record_id": 7,
  "deploy_method": "systemd",
  "host": "linux-worker.example",
  "ssh_user": "root",
  "codex_runtime_dir": "/opt/codex-runtime",
  "codex_data_dir": "/opt/openclaw-agents/claw-17-codex-worker/data",
  "codex_cli": "/opt/codex-runtime/venv/bin/codex",
  "codex_workspace": "/srv/projects/example",
  "work_dirs": [
    "/srv/projects/example",
    "/srv/projects/shared-automation"
  ],
  "codex_requirements": "/opt/codex-runtime/requirements-codex.lock",
  "codex_wheelhouse": "/opt/codex-runtime/wheelhouse",
  "codex_requirements_sha256": "<manifest 中的 64 位 SHA-256>",
  "codex_auth_mode": "chatgpt_subscription | timiai",
  "codex_model": "<可选的批准模型>",
  "wecom_node_bin": "/usr/bin/node",
  "wecom_sdk_root": "/opt/wecom-runtime/node_modules/@wecom/aibot-node-sdk"
}
```

`worker_release_record_id` 必须指向 `linux-x86_64 + approved` 的 Hub release 记录；新建页只
展示已批准版本，默认选中当前 default。版本来源配置：

- `WORKER_RELEASE_REPOSITORY_PATH`：Hub 可直接读取的只读 Worker Git checkout；
- 或 `WORKER_RELEASE_REPOSITORY_URL` + `WORKER_RELEASE_SOURCE_REF`：Hub 管理自己的 checkout
  并在“刷新版本”时执行 fetch；
- `WORKER_RELEASE_STORE_ROOT`：Hub 私有发布包存储目录。

URL 不能携带用户名/密码；私有仓库认证由 Hub 主机的只读 Git/SSH 运行身份提供，不进入
API 参数、数据库或日志。

创建页不要求操作者填写 runtime SHA。Hub 从环境变量
`DEPLOY_CODEX_REQUIREMENTS_SHA256` 或 `system_config.deploy_codex_requirements_sha256`
读取统一部署环境的固定值；runtime 根目录可由 `DEPLOY_CODEX_RUNTIME_DIR` 或
`system_config.deploy_codex_runtime_dir` 配置，默认 `/opt/codex-runtime`。

目标机还需要 Python 3.11、venv、systemd、已存在的 workspace。Hub 会从锁定的
`openai-codex-cli-bin` wheel 建立 runtime 内部 CLI 入口，然后依次完成运行包 SHA
校验、离线安装、服务用户/目录初始化、CLI 登录状态检查、Sidecar/Provider/MCP/
权限模块下载、配置预检、systemd 启用和 active 检查。`codex_workspace` 会自动加入
`work_dirs`；systemd 的 `ReadWritePaths` 与 `PROVIDER_ALLOWED_DIRS` 使用同一组规范化
目录，避免“系统服务可写但 Codex SDK 仍只读”或反向越权。

## 5. Hub 工具、权限审批与监督闭环

每个 Codex 回合会创建短期 `claw_hub` MCP。模型需要先从延迟工具列表发现：

- `mcp__claw_hub__hub_api`：经 Worker 本地代理调用 Hub；创建 Workflow Run 时继续执行
  definition 白名单、参数校验和写后回读；
- `mcp__claw_hub__wecom_reply`：只允许发给已绑定 Owner，模型不能指定任意接收人。

MCP 凭据由 Sidecar 持有并在回合结束后销毁。受控的两个 MCP 工具在 Codex SDK 层直接
批准，真实授权仍由本地代理执行，避免 `approval_policy=never` 与 MCP 交互审批互相
冲突。

缺少额外权限时，Owner 会收到带请求编号的企微消息，可回复：

```text
/codex-allow-once <request_id>
/codex-allow-always <request_id>
/codex-deny <request_id>
/codex-revoke <request_id>
```

一次授权仅用于恢复当前调用；永久授权按规范化请求指纹保存在实例私有 SQLite 中，
仍受 protected roots、精确 hostname/path/method 和最大权限链约束。没有绑定企微
Owner 时不会静默放宽权限。

Hub 下发的 `system_context.policy.codex_orchestrator` 可启用 Workflow Cycle 监督闭环：

```json
{
  "enabled": true,
  "session_key": "racinggo:flow-orchestrator",
  "resume_on": ["blocked", "failed", "timeout"],
  "allowed_next_flows": [12, 25, 26],
  "max_retries": 2,
  "review_success": true
}
```

Worker 监听 Run 终态并唤醒同一逻辑 Codex 会话。异常链路为
`Review → Repair → 修复回执校验 → 创建/回读下一 Run`，不会再丢弃
`repair_operation` 后直接原样重启；整个 Cycle 的最终用户结论由 Codex 收口。Worker
继续掌握幂等、重试上限、Flow 白名单和 Hub 凭据。未下发该配置时保持兼容行为。

## 6. Codex 与企微

启用企微后链路为：

```text
企微官方 Node SDK → Sidecar 鉴权/白名单/去重 → Codex SDK
                → Sidecar 持久化最终结果 → 企微回复
```

bot ID/secret 只写入服务用户可读的 `<data>/wecom-credentials.json`（`0600`）。它们
不会进入 Codex prompt、SDK 参数、子进程环境或日志。`wecom-state.db` 在调用 Codex
前原子占位并保存最终结果，正常重投不会重复消耗额度；过期的 processing 租约才允许
重领。除最终回复外，Codex 还可主动调用 `wecom_reply` 发送关键进度、权限申请和复盘
结论；接收人固定为 Hub 已绑定 Owner。

## 7. 验证与排障

```bash
python3 ops/codex_runtime_release.py verify --runtime /opt/codex-runtime
systemctl status claw-worker-codex-<id>.service
journalctl -u claw-worker-codex-<id>.service -n 100 --no-pager
sudo -u oclaw_<id> env HOME=<data>/home CODEX_HOME=<data>/home/.codex \
  /opt/codex-runtime/venv/bin/codex login status
```

常见错误：

- `authentication_required`：以服务用户完成登录后重新部署；
- `provider_config_invalid`：检查 systemd 中实例独立的 `HOME/CODEX_HOME`；
- `provider_busy`：全局 Codex 执行槽已满，按可重试错误处理；
- `timeout`：SDK/app-server 进程组已被终止，可安全重试；
- `permission_approval_timeout`：企微 Owner 未在时限内处理权限申请；
- `permission_request_invalid`：Codex 请求了通配、根目录、凭据目录等不可授权资源；
- Hub MCP 写操作在 SDK 本地被拒绝：检查部署文件是否包含新版
  `codex_sdk_worker.py`、`hub_plugin.py`、`hub_proxy.py` 与 `claw_hub_mcp.py`；
- `provider_unavailable`：检查锁版本 venv 与部署的 Provider 文件；
- requirements SHA 不匹配：停止部署，重新验证并发布完整 runtime 目录。

Docker 不是当前正式托管合同。后续容器化时必须持久挂载服务账号的 `.codex` 目录，
否则容器重建后需要重新认证。

SDK 的安装、认证复用与接口语义以官方
[Codex SDK](https://learn.chatgpt.com/docs/codex-sdk) 和
[Codex authentication](https://learn.chatgpt.com/docs/auth) 文档为准。
