# Linux Codex Provider 部署文档

## 1. 适用范围

Hub 统一部署器现在支持两种 Linux systemd provider：`hermes` 与 `codex`。Pi 已退役，部署请求传入 `agent_type=pi` 会直接失败，不能再作为默认值或 fallback。

Codex 路径只托管 `openclaw-sidecar-v2-claw-<id>.service`，不会安装或启动 Hermes Gateway。Sidecar 在进程内调用 Python Codex SDK；当前权限固定为 `repo_read`，实际设备、桌面或其他有副作用的动作仍由受控 Job Service 执行。

## 2. 运行环境与制品

目标机需要：

- Linux + systemd；
- Python 3.10 或更高版本，并能创建 venv；
- Codex CLI，建议安装到 `/usr/local/bin/codex`，或在部署请求中指定绝对路径；
- 目标工程目录已经存在；
- 离线 wheelhouse、带完整依赖哈希的 requirements 锁文件，以及锁文件自身的 SHA-256。

Codex CLI 可按官方 Linux 安装入口准备：

```bash
curl -fsSL https://chatgpt.com/codex/install.sh | sh
codex --version
```

Hub 的 systemd 服务不会继承管理员的交互式 `PATH`。安装后应把 CLI 放到所有服务用户可执行的稳定绝对路径（例如 `/usr/local/bin/codex`），或把 `command -v codex` 的结果填写到 `codex_cli`。

部署器只允许以下离线安装形式，不会临时访问 PyPI：

```bash
python -m pip install \
  --no-index \
  --find-links /opt/codex-runtime/wheelhouse \
  --only-binary=:all: \
  --require-hashes \
  -r /opt/codex-runtime/requirements-codex.lock
```

发布方应在受控构建机上锁定 `openai-codex` 及全部传递依赖，把 wheelhouse 和 requirements 一起交付，再计算：

```bash
sha256sum /opt/codex-runtime/requirements-codex.lock
```

## 3. 默认认证：ChatGPT/Codex 订阅账号

Linux Codex SDK 复用同一运行用户的 Codex CLI 登录状态。Hub 不接收 token，不读取或复制 `auth.json`，也不会把 `codex login status` 的账号输出写入部署日志。

每个 Agent 默认以 `oclaw_<id>` 用户运行，认证目录固定为：

```text
/opt/openclaw-agents/claw-<id>-<safe>/data/home/.codex
```

systemd 同时设置 `HOME=<data>/home` 和 `CODEX_HOME=<data>/home/.codex`，因此 CLI 与 Python SDK 使用同一份凭证。首次部署会先创建服务用户和私有目录；如果尚未登录，部署会安全失败并给出下一步命令。然后由管理员在目标机执行：

```bash
sudo -u oclaw_<id> env \
  HOME=/opt/openclaw-agents/claw-<id>-<safe>/data/home \
  CODEX_HOME=/opt/openclaw-agents/claw-<id>-<safe>/data/home/.codex \
  /usr/local/bin/codex login --device-auth

sudo -u oclaw_<id> env \
  HOME=/opt/openclaw-agents/claw-<id>-<safe>/data/home \
  CODEX_HOME=/opt/openclaw-agents/claw-<id>-<safe>/data/home/.codex \
  /usr/local/bin/codex login status
```

终端会显示网址和一次性验证码；在自己的电脑浏览器中用当前 Codex 账号确认即可。若所用 CLI 版本不支持 `--device-auth`，使用 `codex login` 的浏览器流程。登录完成后重新下发部署。

`auth.json` 等同密码文件：权限应保持 `0600`，认证目录保持 `0700`；不得提交代码库、发群、写日志或打进镜像。

## 4. 其他认证方式

API Key 适合长期无人值守但按 OpenAI Platform API 用量计费，不消耗 ChatGPT/Codex 订阅额度。仍应在目标机、以 systemd 运行用户通过 stdin 完成登录，Hub 请求和 `extra_env` 都不允许携带密钥：

```bash
sudo -u oclaw_<id> env HOME=<data>/home CODEX_HOME=<data>/home/.codex \
  sh -c 'printenv OPENAI_API_KEY | /usr/local/bin/codex login --with-api-key'
unset OPENAI_API_KEY
```

Enterprise Codex Access Token 只用于管理员已经开放该能力、且当前 CLI 明确支持对应登录参数的企业工作区。Hub 接受 `codex_auth_mode=enterprise_access_token` 作为运维标识，但不接收令牌；实际登录必须在目标机完成。

建议顺序：

1. 验证/个人 Worker：`chatgpt_subscription`（默认）。
2. 正式共享 Worker：企业管理员开放的 Access Token。
3. 无企业条件的无人值守 Worker：项目级 API Key。

## 5. Hub 部署请求

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
  "codex_requirements_sha256": "<64 位 SHA-256>",
  "codex_auth_mode": "chatgpt_subscription",
  "codex_model": "<可选的批准模型>"
}
```

SSH 凭据继续只从 Hub 密钥箱读取。Codex 认证信息不属于 SSH 凭据，也不属于 Hub 部署密钥。

部署器依次完成：制品校验、离线 SDK 安装、服务用户/目录初始化、CLI 登录状态检查、Sidecar/Provider 制品下载、Hub `sidecar-config` 注册、systemd 启用和 active 检查。

## 6. 验证与排障

```bash
systemctl status openclaw-sidecar-v2-claw-<id>.service
journalctl -u openclaw-sidecar-v2-claw-<id>.service -n 100 --no-pager
sudo -u oclaw_<id> env HOME=<data>/home CODEX_HOME=<data>/home/.codex \
  /usr/local/bin/codex login status
```

常见失败：

- `authentication required`：用服务用户完成登录后重新部署；
- requirements SHA 不匹配：停止部署，重新核对发布制品；
- `provider_unavailable`：检查 venv 中锁定的 `openai-codex` 安装；
- service 启动后退出：检查 Sidecar journal，但不要输出认证文件内容。

Docker 不是当前 Hub 正式部署合同。如果后续容器化，必须持久化挂载服务账号的 `.codex` 目录；否则容器重建后需要重新认证。
