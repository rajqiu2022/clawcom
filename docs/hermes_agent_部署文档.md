# Hermes Agent 部署指南（Venus 大模型配置）

> 适用版本：Hermes Agent 最新版  
> 最后更新：2026-04-27  
> 适用平台：Linux (CentOS/Ubuntu)  
> ⚠️ 公司内网部署，严禁数据出境

---

## 一、前置条件

### 1.1 系统要求
- **操作系统**：Linux x86_64（CentOS 7+/Ubuntu 20.04+）
- **内存**：≥ 4GB 可用
- **磁盘**：≥ 10GB 可用空间
- **网络**：需访问内网 Venus API (v2.open.venus.oa.com)

### 1.2 依赖环境
```bash
# Python 3.8+
python3 --version

# Docker（推荐方式部署）
docker --version
docker-compose --version

# 或 Python 虚拟环境
which python3
```

### 1.3 权限准备
- 服务器 root 或 sudo 权限
- Venus 平台 API Key

---

## 二、部署步骤

### 方式一：Docker 部署（推荐）

#### 步骤 1：拉取镜像
```bash
# 从公司镜像仓库拉取
docker pull ccr.ccs.tencentyun.com/hermes/hermes-agent:latest

# 或指定版本
docker pull ccr.ccs.tencentyun.com/hermes/hermes-agent:v1.2.3
```

#### 步骤 2：创建工作目录
```bash
# 创建配置目录和数据目录
mkdir -p /opt/hermes-agent/{config,sessions,logs}
chmod 755 /opt/hermes-agent/{config,sessions,logs}
```

#### 步骤 3：准备配置文件
创建 `/opt/hermes-agent/config/config.yaml`：

```yaml
model:
  default: "glm-5.1"                    # 默认主模型
  provider: "venus"                     # Venus 平台
  api_mode: "chat_completions"          # Venus 使用 chat_completions

providers:
  venus:
    type: "openai_compatible"
    base_url: "http://v2.open.venus.oa.com/llmproxy"
    api_key: "${VENUS_API_KEY}"         # 从环境变量读取
    default_model: "glm-5.1"
    api_mode: "chat_completions"
    context_length: 128000
    
  # 可选：备用配置（其他平台）
  dashscope:
    type: "openai_compatible"
    base_url: "https://coding.dashscope.aliyuncs.com/v1"
    api_key: "${DASHSCOPE_API_KEY}"
    default_model: "qwen3.5-plus"
    api_mode: "chat_completions"
    context_length: 200000
    
  timiai:
    type: "openai_compatible"
    base_url: "http://api.timiai.woa.com/ai_api_manage/llmproxy"
    api_key: "${TIMIAI_API_KEY}"
    default_model: "claude-sonnet-4.6"
    api_mode: "codex_responses"         # timiai 必须用这个
    context_length: 128000

auxiliary:
  vision:
    model: "glm-5.1"                    # 图片识别模型
    provider: "venus"
    api_mode: "chat_completions"

# 其他全局配置
enable_tools: true
enable_vision: true
log_level: "INFO"
```

#### 步骤 4：创建环境变量文件
创建 `/opt/hermes-agent/config/.env`：

```bash
# Venus 平台（当前使用）
VENUS_API_KEY=SELWHFXdTCIc8abHcfjvXpFC@4991

# 备用平台配置
DASHSCOPE_API_KEY=sk-sp-26203a91b43c4ed9ac1e2c072ebeca14
TIMIAI_API_KEY=aeHnpV6GS5Yk1fA6cu4MG5Fc3uSXuT9OnkImbcqZ

# Hermes 全局配置
HERMES_CODEX_STREAMING=false           # ⚠️ 必须禁用 streaming！
HERMES_LOG_LEVEL=INFO
HERMES_SESSION_DIR=/app/sessions
HERMES_CONFIG_PATH=/app/config/config.yaml
```

#### 步骤 5：启动容器
```bash
docker run -d \
  --name hermes-agent \
  --restart always \
  --network host \
  -v /opt/hermes-agent/config:/app/config:ro \
  -v /opt/hermes-agent/sessions:/app/sessions \
  -v /opt/hermes-agent/logs:/app/logs \
  --env-file /opt/hermes-agent/config/.env \
  -e HERMES_CODEX_STREAMING=false \
  ccr.ccs.tencentyun.com/hermes/hermes-agent:latest

# 查看日志
docker logs -f hermes-agent
```

### 方式二：二进制/源码部署

#### 步骤 1：下载二进制或源码
```bash
# 创建安装目录
mkdir -p /opt/hermes-agent && cd /opt/hermes-agent

# 方式 A：下载二进制（如果有发布）
# wget https://.../hermes-agent-linux-amd64 -O hermes-agent
# chmod +x hermes-agent

# 方式 B：源码安装（需 Python 3.8+）
git clone https://git.woa.com/hermes/hermes-agent.git
```

#### 步骤 2：创建虚拟环境
```bash
cd /opt/hermes-agent
python3 -m venv venv
source venv/bin/activate

# 安装依赖
pip install -r requirements.txt
```

#### 步骤 3：配置文件
同 Docker 方式，创建 `config/config.yaml` 和 `.env`

#### 步骤 4：启动
```bash
# 直接运行 (调试用)
python -m hermes_agent

# 或使用 supervisord/systemd 托管
```

---

## 三、Venus 大模型配置详解

### 3.1 支持的模型列表（Venus 平台）

| 模型名称 | 上下文长度 | 特点 | 适用场景 |
|---------|----------|------|---------|
| `glm-5.1` | 128K | 通用能力强 | 日常对话、代码、分析 |
| `kimi-k2.6` | 200K | 长上下文 | 长文档分析、多轮对话 |
| `qwen3.5-plus` | 200K | 阿里通义千问 | 中文场景优化 |
| `deepseek-r1` | 128K | 推理能力强 | 复杂问题推理 |

### 3.2 config.yaml 关键字段说明

```yaml
model:
  default: "glm-5.1"      # 默认使用的主模型名称
  provider: "venus"       # 对应 providers 下的键名
  api_mode: "chat_completions"  # API 调用模式

providers:
  venus:
    type: "openai_compatible"    # 接口类型
    base_url: "..."              # API 基础地址
    api_key: "..."               # 认证密钥
    default_model: "glm-5.1"     # 默认模型（必须设置）
    api_mode: "chat_completions" # 调用模式
    context_length: 128000       # 上下文窗口大小
```

### 3.3 api_mode 说明

| api_mode | 适用平台 | 说明 |
|---------|---------|------|
| `chat_completions` | Venus, DashScope | OpenAI 标准 Chat API |
| `codex_responses` | timiai | OpenAI Responses API |

⚠️ **重要**：timiai 平台**只支持** `codex_responses` 模式！

---

## 四、企微机器人与图片/文件收发配置

### 4.1 配置企微机器人

在 `config/config.yaml` 中新增 `wecom` 节点：

```yaml
wecom:
  key: "aibGfulxda08hC1y1N6P1Gl8xD1tNbP-tqI"      # 企微机器人 ID
  secret: "9ojl8EYLPRAKimHiDK9AiapvzhKpouKGHS2FbnGArde"  # 企微机器人 Secret
```

> 📌 **获取方式**：在企微群 → 添加群机器人 → 复制 Webhook 中的 `key`。  
> 若机器人类型为自定义机器人，`secret` 可在企微管理后台查看。

### 4.2 图片与文件收发

Hermes Agent 支持通过企微接收和发送**图片**与**文件**。

#### 前提条件
1. `config.yaml` 中已配置 `wecom.key` 和 `wecom.secret`
2. **`wecom.py` 需包含以下修复**（确保代码版本 >= 2026-04-21）：
   - `_cache_media` 正确处理媒体缓存，避免返回 `None`
   - `_derive_message_type` 正确识别消息类型（`image` / `file`）
3. 模型需支持 Vision 能力（见 [4.3 Vision 模型配置](#43-vision模型配置)）

#### 验证图片接收
发送图片到企微群，观察 Agent 日志：
```bash
# 应看到类似日志
cat /var/log/hermes-agent.log
# [receive]<hermes.core.message_handlers.wecom_handler - Event: image received> ...
```

若日志出现 `[DEBUG_EXTRACT_MEDIA] _cache_media returned None for kind=image`，说明 `wecom.py` 未修复，需更新代码。

#### 发送图片/文件
Agent 通过 Vision 模型识别图片内容后，会以文本形式回复分析结果。  
如需发送文件，可通过工具调用（MCP）或自定义 Action 实现。

### 4.3 Vision 模型配置

图片识别依赖 `auxiliary.vision` 配置，需指定支持多模态的模型：

```yaml
auxiliary:
  vision:
    model: "glm-5.1"                    # 图片识别模型（需支持 Vision）
    provider: "venus"
    api_mode: "chat_completions"
```

#### 各平台 Vision 模型推荐

| 平台 | 推荐模型 | 说明 |
|------|---------|------|
| Venus | `glm-5.1` | 通用视觉能力 |
| timiai | `gemini-3.1-pro-preview` | 多模态能力强 |
| DashScope | `qwen3.5-plus` | 中文视觉场景优化 |

#### 注意事项
- Vision 模型和主模型**可以是不同平台**，例如主模型用 Venus 的 `glm-5.1`，Vision 模型用 timiai 的 `gemini-3.1-pro-preview`
- 切换 Vision 模型后，**需要重启服务**生效（无需清理会话）
- 若 Vision 模型配置错误，图片消息会 fallback 为普通文本处理，或提示"无法识别图片"

---

> 📌 **Hub 系统接入**：与 Hub（DeepFlow 2.0）的对接（创建 OpenClaw、获取 token、SSE 客户端部署等）由用户在 Hub 侧自行完成，不在本文档范围内。

---

## 五、模型切换操作

### 5.1 切换到 Venus 平台（从其他平台）

```bash
# 1. 备份当前配置
cp config/config.yaml config/config.yaml.bak.$(date +%Y%m%d)

# 2. 修改 config.yaml
# model.default = "glm-5.1"
# model.provider = "venus"
# model.api_mode = "chat_completions"

# 3. 清理旧的会话（重要！）
echo '{}' > sessions/sessions.json

# 4. 重启服务
# Docker 方式
docker restart hermes-agent

# 或 systemd 方式
systemctl restart hermes-agent

# 5. 验证
docker logs -f hermes-agent  # 查看启动日志
```

### 5.2 在 Venus 平台切换不同模型

```bash
# 只需修改 config.yaml 中的 model.default
# glm-5.1 -> kimi-k2.6 -> qwen3.5-plus

# 修改后清理会话并重启
echo '{}' > sessions/sessions.json
docker restart hermes-agent
```

---

## 六、常见问题与排查

### 6.1 服务启动失败

**现象**：容器反复重启  
**排查**：
```bash
docker logs hermes-agent --tail 100
# 检查：
# 1. config.yaml 格式是否正确（缩进必须为2空格）
# 2. api_key 是否有效
# 3. 网络能否访问 base_url
```

### 6.2 模型不响应/403 错误

**现象**：发送消息无响应，日志显示 403  
**排查**：
```bash
# 检查 API Key 是否过期
curl -H "Authorization: Bearer $VENUS_API_KEY" \
  http://v2.open.venus.oa.com/llmproxy/models

# 检查 api_mode 是否正确
# Venus 必须用 chat_completions
```

### 6.3 会话异常/历史消息混乱

**现象**：模型回复异常，或返回旧会话内容  
**解决**：
```bash
# 清空会话缓存
echo '{}' > sessions/sessions.json
# 重启服务
docker restart hermes-agent
```

### 6.4 Streaming 相关错误

**现象**：TypeError: sequence item 0: expected str instance, NoneType found  
**解决**：
```bash
# 必须禁用 streaming
docker run ... -e HERMES_CODEX_STREAMING=false ...
# 或在 .env 中设置 HERMES_CODEX_STREAMING=false
```

### 6.5 图片无法接收/识别

**现象**：发送图片后 Agent 无响应，或提示无法识别  
**排查**：
```bash
# 1. 检查 wecom.py 是否包含 _cache_media 修复
grep -n "_cache_media" /path/to/wecom.py

# 2. 检查 Vision 模型配置
cat config/config.yaml | grep -A 5 "auxiliary:"

# 3. 查看图片相关日志
grep "_cache_media\|DEBUG_EXTRACT_MEDIA\|image received" /var/log/hermes-agent.log
```

---

## 七、Systemd 服务配置（非 Docker 环境）

创建 `/etc/systemd/system/hermes-agent.service`：

```ini
[Unit]
Description=Hermes Agent Service
After=network.target

[Service]
Type=simple
User=hermes
Group=hermes
WorkingDirectory=/opt/hermes-agent

# 关键环境变量
Environment="VENUS_API_KEY=SELWHFXdTCIc8abHcfjvXpFC@4991"
Environment="HERMES_CODEX_STREAMING=false"
Environment="HERMES_LOG_LEVEL=INFO"

# 启动命令
ExecStart=/opt/hermes-agent/venv/bin/python -m hermes_agent
ExecStartPre=/bin/bash -c 'echo "{}" > /opt/hermes-agent/sessions/sessions.json'

# 重启策略
Restart=always
RestartSec=10
StartLimitInterval=60
StartLimitBurst=3

# 资源限制
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
```

启用服务：
```bash
systemctl daemon-reload
systemctl enable hermes-agent --now
systemctl status hermes-agent
```

---

## 八、附件：参考配置速查

### Venus 平台完整配置
```yaml
model:
  default: "glm-5.1"
  provider: "venus"
  api_mode: "chat_completions"

providers:
  venus:
    type: "openai_compatible"
    base_url: "http://v2.open.venus.oa.com/llmproxy"
    api_key: "${VENUS_API_KEY}"
    default_model: "glm-5.1"
    api_mode: "chat_completions"
    context_length: 128000

auxiliary:
  vision:
    model: "glm-5.1"
    provider: "venus"
    api_mode: "chat_completions"

wecom:
  key: "your-wecom-key"
  secret: "your-wecom-secret"
```

### 环境变量汇总
```bash
# 必需
VENUS_API_KEY=<your-key>
HERMES_CODEX_STREAMING=false

# 企微机器人
WECOM_KEY=your-wecom-key
WECOM_SECRET=your-wecom-secret

# 可选
HERMES_LOG_LEVEL=INFO
HERMES_SESSION_DIR=/app/sessions
HERMES_CONFIG_PATH=/app/config/config.yaml
```

---

## 九、安全合规提醒

1. **严禁数据出境**：所有 API 调用仅限内网 Venus 平台
2. **API Key 保管**：不要硬编码在代码中，使用环境变量或密钥管理服务
3. **访问控制**：限制服务器的 SSH 访问权限
4. **日志脱敏**：确保日志中不记录 API Key、Token 等敏感信息
5. **定期轮换**：建议定期更换 API Token

---

> 📌 **踩坑经验**：
> - 切换模型后**必须**清理 `sessions.json`，否则旧会话格式可能导致 API 错误
> - `default_model` 字段**必须**设置，否则 provider 返回 model=None
> - `HERMES_CODEX_STREAMING` **必须**设为 `false`（timiai 和 Venus 都会出问题）
> - timiai 平台**只支持** `codex_responses` 模式
> - 图片收发依赖 `wecom.py` 的 `_cache_media` 修复，旧版本会返回 `None` 导致图片丢失
> - Vision 模型和主模型**可以不同平台**，但切换后需重启服务

如有问题，请查看容器日志或联系 Hermes Agent 维护团队。