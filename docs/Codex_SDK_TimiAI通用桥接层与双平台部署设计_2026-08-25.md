# Codex SDK × TimiAI 通用桥接层与 Linux / Windows 部署设计

日期：2026-08-25  
状态：交付 Worker 开发评审（先实施 Hermes 能力对齐 MVP）  
主要实现方：Claw Worker  
配合方：Hub  

## 0. 范围优先级

本文件同时保留近期 MVP 和后续完整能力路线。若后文章节与本节冲突，首期开发、验收和排期
以本节为准。

首期目标不是完整复刻 Codex 官方 Provider 能力，而是让“Codex SDK + TimiAI Bridge”达到
当前 Hermes 在 Claw 体系中的核心业务能力，并同时可在 Windows 与 Linux 部署：

1. 接收 Hub 消息、读取 Hub 托管上下文，并通过现有 `hub_api` MCP 完成获准的 Hub 读写；
2. 接收企微消息并回传最终结果；首期不要求模型主动调用 `wecom_reply` 发送中间消息；
3. 执行 Hub Workflow AgentTask，支持 heartbeat/progress、严格结果回写和上游 `outputs`；
4. 在本机批准的 workspace/allowed dirs 内读取、修改文件，执行 shell、Git 和测试；
5. 共用现有 Sidecar、Provider、Hub MCP、权限、记忆和 Job Service，不新建业务 Agent；
6. Windows 与 Linux 使用同一 Bridge 核心、协议测试向量和错误合同，并可升级、回滚和诊断。

首期固定约束：

- 只启用一个已批准的 TimiAI 项目和一个已通过工具 canary 的模型；
- `CODEX_PERMISSION_MODE` 支持 `trusted_host|full_access`；默认`trusted_host`，`full_access`只能由
  本机部署参数显式选择并显示高风险提示；需要把TimiAI Key隔离出Worker用户时再启用独立Bridge身份；
- 只实现固定 Codex SDK 版本实际使用的 Responses 字段和 SSE 事件，不建设通用 Responses 网关；
- 工具调用先支持串行 function/MCP/local tool，`parallel_tool_calls` 延后；
- structured output 可由 Worker 现有严格结果校验兜底，不要求上游模型原生支持；
- Bridge 故障必须 fail closed，不自动切换订阅账号、项目、模型或计费来源；
- Hub 普通任务不能热切换 auth mode、上游地址、项目、Key或模型。

以下能力延后，不作为 Hermes 对齐 MVP 的交付门禁：

- 多项目/多模型动态切换；
- 完整 Responses 事件全集与未知客户端兼容；
- 并行工具调用；
- reasoning summary 精细映射；
- subscription 与 TimiAI 的自动故障转移；
- 独立 Bridge OS 身份、跨服务隔离等进一步纵深防御；
- 不属于 Hub通信、企微通信、Flow执行和本机工程操作的 Codex 专属能力。

## 1. 决策摘要

本方案在 Claw Worker 本机增加一个只监听回环地址的 Model Bridge，对 Codex 暴露 OpenAI Responses API，对内适配 TimiAI 实际协议。

```text
Hub
  |
  v
Claw Worker / CodexSdkProvider
  |
  v
Codex SDK
  |-- auth_mode=subscription --> OpenAI 内置 Provider
  |
  `-- auth_mode=timiai_bridge --> 127.0.0.1 Model Bridge --> TimiAI
```

核心决策：

1. Bridge 是 Codex 的“模型传输兼容层”，不是新的 Agent Provider。
2. Worker 现有 `AgentProvider`、`ProviderInvocation`、`ProviderResult` 和 Job Service 合同保持不变。
3. `subscription` 继续走 Codex 内置 OpenAI Provider，不经过 Bridge。
4. `timiai_bridge` 才启动本地 Bridge，并把 Codex 自定义 Provider 的 `base_url` 指向回环地址。
5. Linux 和 Windows 共用同一 Python Bridge 包、协议转换、状态库和测试向量；仅凭据存储、ACL、进程树监管和安装脚本不同。
6. TimiAI Key 只进入 Bridge，不进入 Codex、Sidecar、Hub任务、Prompt、日志或 `config.toml`。
7. Hub 不实现模型协议转换。Hub只负责保存经审批的部署期望、审批 Worker Release、密钥箱和审计；
   实际模式、项目和模型必须通过 Worker 部署事务写入本机受保护配置，不能由普通运行时任务切换。

## 2. 官方约束与设计依据

OpenAI 官方文档确认：

- Codex SDK 用于程序化启动、继续和恢复本地 Codex Thread；
- Codex自定义 `model_providers` 支持 `base_url`、命令式认证、环境 Header、重试和流超时；
- 自定义 Provider 的 `wire_api` 当前只支持 `responses`。

参考：

- [Codex SDK](https://learn.chatgpt.com/docs/codex-sdk)
- [Codex Configuration Reference](https://learn.chatgpt.com/docs/config-file/config-reference)
- [Responses API](https://developers.openai.com/api/reference/cli/resources/responses/methods/create)

因此不能要求 Codex SDK 直接使用只支持 Chat Completions 的上游；兼容层必须在本地表现为 Responses API。

## 3. 当前工程状态

### 3.1 Worker 已有能力

当前 Worker 已具备：

- `src/provider_runtime.py`：唯一 `AgentProvider` / `ProviderResult` 合同；
- `src/codex_sdk_provider.py`：Codex SDK Adapter；
- `src/codex_sdk_worker.py`：单回合 Codex执行；
- `src/sidecar_v2.py`：Hub传输、Provider生命周期、权限和会话串行；
- Windows Job Object / Linux进程组回收；
- Windows `scripts/install.ps1` 和 Linux `scripts/install-linux.py`；
- 实例独立 `CODEX_HOME`、workspace和本地状态库；
- Hub MCP短期代理与长期凭据隔离。

这些能力应复用，不另建第二套 Worker。

### 3.2 Hub 当前临时方案

Hub `web/app/services/agent_deployer.py::_render_codex_config()` 当前在 TimiAI 模式下生成：

```toml
model_provider = "timiai"

[model_providers.timiai]
base_url = "<TimiAI base>"
wire_api = "responses"

[model_providers.timiai.auth]
command = "<credential helper>"
```

当前方案存在两个前提：

1. 假设目标 TimiAI 项目完整兼容 Codex需要的 Responses请求和 SSE事件；
2. 认证助手把 TimiAI上游 Key直接交给 Codex Provider。

Bridge 上线后应替换这两点：

- `base_url` 改成本地 Bridge；
- Codex认证助手只返回本地 Bridge Token；
- TimiAI Key 改为 Bridge 专属凭据。

## 4. 目标与非目标

### 4.1 首期目标：对齐 Hermes 核心业务能力

- 支持 Codex SDK通过 TimiAI模型完成Hub普通消息、Todo、Workflow和企微回合；
- 支持满足上述场景所需的流式文本、串行函数工具、MCP工具和多轮 Thread；
- 支持批准项目当前实际使用的 Chat Completions转换；只有通过完整conformance的项目才启用Responses透传；
- 支持Codex在批准工作区内读写文件、执行shell/Git/测试，并继续使用现有Job Service执行typed action；
- Linux和Windows行为一致；
- 认证、限流、额度、模型不存在、HTTP 200错误体等异常映射稳定；
- Bridge崩溃后无孤儿进程，Worker能够熔断并等待经审批的配置回滚；不得自动切换Provider；
- 凭据扫描和日志检查零泄漏；
- 发布包可离线、固定版本、完整哈希和可回滚。

完整Responses事件、并行工具、多项目模型切换、reasoning summary和其它Codex专属能力属于后续增强，
不阻塞Hermes能力对齐MVP。

### 4.2 非目标

- 不在 Bridge中执行 Hub业务API、企微通知、Git、Unity、ADB或桌面操作；
- 不把 Bridge注册为第三个 `AgentProvider`；
- 不把 TimiAI Key 放进任务 payload、Prompt、Codex环境或 Thread；
- 不让模型或普通 Hub任务动态切换 TimiAI项目、Key、上游地址或模型白名单；
- 不为不支持工具调用的模型伪造工具能力；
- 不让 Bridge成为任意目标可访问的通用 HTTP代理；
- 不代理 ChatGPT/Codex订阅认证。

## 5. 组件边界

### 5.1 Worker Runtime

负责：

- 读取本机受保护的 Provider配置；
- 选择 `subscription` 或 `timiai_bridge`；
- 启动、监管和停止 Bridge；
- Bridge readiness通过后注册 Codex Provider；
- 保持每个 `CODEX_HOME` 串行；
- lease丢失、企微抢占、超时或停机时取消 Codex和Bridge请求；
- 将 Codex结果转换为既有 `ProviderResult`；
- 统一向 Hub回写结果，不允许 Bridge直接回写。

### 5.2 Model Bridge

负责：

- 校验本地 Bridge Token；
- 校验 Responses请求合同；
- 根据受保护配置选择 TimiAI Upstream Adapter；
- 转换请求、SSE事件、工具调用、usage和错误；
- 保存 Chat Completions模式下必要的 `previous_response_id` 状态；
- 限制并发、请求体、链深、超时和输出大小；
- 输出脱敏指标和诊断。

Bridge不得读取：

- `CLAW_TOKEN` / Hub Token；
- 企微、TAPD、测试账号或设备凭据；
- Provider workspace源码；
- Codex `auth.json`；
- Worker任务数据库。

### 5.3 Hub

负责：

- 选择 `codex_auth_mode=subscription|timiai_bridge`；
- 选择 `timiai_project` 和允许的模型；
- 从密钥箱解析对应项目 Key，并安全部署到目标机；
- 固定 Worker / Bridge Release、manifest和SHA-256；
- 保存部署、配置变更、灰度和回滚审计；
- 展示 Bridge版本、健康、上游项目和实际模型；
- 不参与每次模型请求，不保存 Bridge会话。

### 5.4 DeepFlow / Job Service

不需要因 Bridge改造而变化。确定性系统、Unity、ADB、设备和Git受控操作仍走 typed runner / Job Service。

## 6. 进程与网络模型

`trusted_host`模式推荐由 Worker 主进程监管 Bridge子进程，不分别维护 Windows Service和Linux
systemd业务实现。`full_access`模式下Codex拥有当前Worker运行用户的完整文件和进程权限，同用户
Bridge无法构成TimiAI Key隔离边界；该模式仍可运行，但必须明确展示风险。需要隔离TimiAI Key时，
使用独立Bridge OS身份或等效独立服务边界：

- Linux：独立`clawbridge_<id>`用户和`claw-model-bridge-<id>.service`；
- Windows：独立低权限服务/任务身份，或仅Bridge身份可解密的凭据代理；
- Codex可获得本地Bridge Token，这是自定义Provider认证的必要条件，但不得获得TimiAI Key；
- Bridge仍只允许固定项目、模型、上游地址和单实例并发，不能因`full_access`变成通用代理。

```text
Worker process
  |-- Sidecar / Hub transport
  |-- Codex invocation subprocess
  `-- Model Bridge subprocess
```

共同规则：

- Bridge只监听 `127.0.0.1`；
- 安装时选择一个稳定、未占用的实例端口，例如 `19100-19999`；
- 端口写入实例受保护配置，任务不能修改；
- Bridge启动后 Worker轮询 `/readyz`；
- readiness未通过时 Codex Provider报告 `provider_unavailable`；
- Bridge连续崩溃达到阈值后熔断，不无限重启；
- Worker退出、升级或取消时回收 Bridge进程树；
- 不开放局域网监听，不支持转发任意 Host。

平台监管：

- Windows `trusted_host`：Bridge加入 Worker Job Object，`kill-on-close`；
- Linux `trusted_host`：Bridge加入独立 process group，运行在 Worker systemd cgroup内；
- Windows/Linux `full_access`：Bridge由独立服务身份监管，Worker通过受控服务管理接口协调启停，
  不能让Codex取得Bridge服务控制权或TimiAI凭据读取权；
- 两个平台都必须能在升级、超时和异常下证明无孤儿 Bridge / Codex进程。

## 7. 本地 HTTP 合同

### 7.1 接口

```http
GET  /healthz
GET  /readyz
GET  /v1/models
POST /v1/responses
POST /responses
```

`/responses` 是兼容别名。Bridge需要记录 Codex runtime 实际请求路径，稳定后可只保留必要路径。

### 7.2 本地认证

所有模型请求必须携带：

```http
Authorization: Bearer <per-instance bridge token>
```

Bridge Token：

- 每个 Worker实例独立生成；
- 只用于 Codex到本地 Bridge；
- 不等于 TimiAI Key；
- 不由 Hub任务或模型决定；
- 轮换时原子写入并重启 Bridge / Codex；
- 日志只记录 token fingerprint，不记录值。

### 7.3 限制

建议初始值：

| 项目 | 建议值 |
|---|---:|
| 请求体 | 8 MiB |
| 单次输出 | 8 MiB |
| 单实例并发 | 1，与 `CODEX_HOME` 串行一致 |
| SSE idle timeout | 300 秒 |
| 总请求 timeout | 3600 秒，受 invocation更小值约束 |
| response chain | 128 |
| state DB | 512 MiB / 30天TTL，可配置 |
| 单条日志 | 4 KiB脱敏摘要 |

Bridge遇到超限请求应返回稳定错误，不截断后继续生成。

## 8. Upstream Adapter

```python
class TimiaiUpstream(Protocol):
    async def create_response(
        self,
        request: ResponseRequest,
        cancel: CancellationToken,
    ) -> AsyncIterator[ResponseEvent]: ...
```

首期实现两个 Adapter：

```text
TimiaiResponsesAdapter
TimiaiChatCompletionsAdapter
```

配置显式指定上游协议，不做每次请求自动猜测：

```json
{
  "upstream": {
    "base_url": "http://api.timiai.woa.com/ai_api_manage/llmproxy",
    "protocol": "chat_completions",
    "project": "gbt",
    "credential_ref": "timiai:gbt"
  }
}
```

若某个项目已通过完整 Responses conformance测试，可改为：

```json
{"protocol": "responses"}
```

禁止仅通过“返回过一次文本”就认定 Responses兼容。

## 9. Responses 到 Chat Completions 转换

### 9.1 请求映射

| Responses 字段 | Chat Completions 映射 |
|---|---|
| `model` | 经批准的 `upstream_model` |
| `instructions` | system/developer message |
| `input` 文本 | user message |
| `input` message items | messages |
| function tools | tools/functions |
| `tool_choice` | tool_choice；不支持时拒绝 |
| tool output item | role=tool + call_id |
| `parallel_tool_calls` | 仅能力清单允许时透传 |
| `reasoning.effort` | 按模型能力映射，否则忽略并产生 warning |
| structured text format | response_format；不支持时拒绝或走 Worker结果校验 |
| `previous_response_id` | 从本地state store重建历史 messages |
| `stream=true` | 上游stream并转换成Responses SSE |

Bridge不能信任请求中的项目、上游 URL、Key或任意模型名。模型必须命中本机批准的映射。

### 9.2 SSE事件映射

至少覆盖：

```text
response.created
response.in_progress
response.output_item.added
response.content_part.added
response.output_text.delta
response.output_text.done
response.function_call_arguments.delta
response.function_call_arguments.done
response.output_item.done
response.completed
response.failed
```

Chat chunk映射规则：

- assistant text delta → `response.output_text.delta`；
- tool call name / id → function call output item；
- tool arguments delta → `response.function_call_arguments.delta`；
- finish_reason=tool_calls → function item done，不得错误完成整个 Response；
- finish_reason=stop → text item done + response completed；
- usage → completed response 的 usage；
- 上游中途错误 → `response.failed`，关闭 SSE；
- 已输出任何事件后禁止自动重放整个请求。

### 9.3 工具语义

Codex依赖工具调用完成 MCP、shell、apply patch和其它本地能力。Bridge必须保持：

- tool name；
- call id；
- JSON argument字节顺序和完整性；
- tool output与call id对应；
- 多个并行call的索引；
- 跨 `previous_response_id` 的tool状态。

任何模型不支持工具调用或返回不可解析 arguments 时，返回 `tool_protocol_invalid`，不能把工具JSON当普通文本交给 Codex。

### 9.4 Reasoning

TimiAI不同模型可能使用不同字段，例如 `reasoning_content`。Bridge只能依据批准的模型能力清单映射：

- 支持并验证过：转换为允许的 reasoning summary事件；
- 不支持：不透出原始思维链，可只记录 `reasoning_available=false`；
- 未知字段：丢弃并记录脱敏 warning；
- 禁止把完整 reasoning写入日志、Hub progress或最终报告。

## 10. Response状态存储

若上游 Chat Completions不支持 `previous_response_id`，Bridge使用实例独立SQLite存储最小协议状态：

```text
responses
response_items
tool_calls
usage
```

要求：

- response id使用 `resp_claw_<random>`；
- 保存重建消息链所需数据，不保存TimiAI Key和Hub凭据；
- 数据库目录只能由 Worker服务用户访问；
- 有schema version、WAL、容量上限、TTL和完整性检查；
- 未完成stream标记failed，重启后不能误当completed；
- chain缺失、损坏或越界时返回 `response_state_unavailable`；
- 不跨 Worker实例或项目复用；
- Codex Thread映射仍由 Worker维护，Bridge不替代它。

## 11. 错误归一化与重试

TimiAI可能用 HTTP 200返回业务错误。Adapter必须先解析body，再决定成功或失败。

建议错误码：

| 条件 | Bridge HTTP | Worker错误 | retryable |
|---|---:|---|---|
| TimiAI Key无效 | 401 | `upstream_authentication_required` | 否 |
| 模型/项目无权限 | 403 | `provider_permission_denied` | 否 |
| 模型不存在 | 400 | `provider_config_invalid` | 否 |
| 额度耗尽 | 429 | `quota_exhausted` | 否 |
| 上游限流/繁忙 | 429/503 | `rate_limited` | 是 |
| 首字节超时 | 504 | `upstream_ttfb_timeout` | 是 |
| stream idle | 504 | `upstream_stream_timeout` | 仅未输出事件时 |
| 非JSON/合同错误 | 502 | `upstream_protocol_invalid` | 受限重试 |
| Bridge不可用 | 503 | `provider_unavailable` | 是 |
| 本地状态缺失 | 409 | `response_state_unavailable` | 否 |
| 工具arguments非法 | 502 | `tool_protocol_invalid` | 否 |

重试规则：

- Codex自定义 Provider已有请求重试；Bridge内部默认最多重试1次；
- 一旦向 Codex输出任意 SSE字节，不自动重放整个上游请求；
- 同一请求保留稳定 bridge request id；
- 不对工具已产生副作用的轮次自动重做；
- 错误body进入日志前做Secret、Header、路径和Prompt脱敏。

`authentication_required`只用于ChatGPT/Codex订阅登录失效；不得用于TimiAI Key。Worker收到
`upstream_authentication_required`时应通知管理员轮换TimiAI凭据或重新部署，不能提示用户执行
`codex login`或网页登录。

## 12. 模型能力清单

模型能力必须按 TimiAI项目隔离，不能用一份全局列表：

```json
{
  "project": "gbt",
  "models": {
    "deepseek-v4-pro-r1": {
      "upstream_model": "deepseek-v4-pro-r1",
      "context_window": 131072,
      "supports_stream": true,
      "supports_tools": true,
      "supports_parallel_tools": false,
      "supports_structured_output": false,
      "supports_reasoning": true
    }
  }
}
```

要求：

- 能力清单由发布配置或 Hub批准配置生成；
- Key实际授权由独立 canary验证；
- 不在生产启动时遍历探测所有模型，避免消耗额度和触发限流；
- 模型别名在Bridge入口归一化后，只发送批准的真实upstream model；
- Key决定实际TimiAI项目和计费归属，`project`字段只用于匹配和审计，不能覆盖Key归属。

## 13. 凭据设计

### 13.1 两层凭据

```text
Codex -- local bridge token --> Bridge -- TimiAI Key --> TimiAI
```

Bridge Token和TimiAI Key必须独立轮换。

`trusted_host`模式允许Bridge与Worker使用同一服务身份，Codex permission profile必须排除实例
目录、Bridge目录和credentials目录。该方案依赖Codex sandbox，不构成独立OS安全边界。

`full_access`模式也属于首期支持范围。默认同用户部署保持与现有Codex full access相同的高风险
边界：Key不注入Prompt/环境/Thread，但同一Windows/Linux用户理论上可读取或调试本机凭据和进程。
如果部署要求TimiAI Key对Codex用户不可读，则必须将Bridge/TimiAI Key放入独立安全身份：TimiAI Key
文件、DPAPI blob和Bridge进程只允许Bridge身份与系统管理员访问，Worker/Codex运行身份不得读取或
解密。Bridge Token会由Codex command auth取得，它不是TimiAI Key；即使Codex直接调用本地Bridge，
仍只能使用本机固定的项目、模型、限流和并发合同。安装器不能静默把`full_access`降级为
`trusted_host`；是否启用独立Bridge身份由显式部署参数决定。

独立Bridge身份只保护TimiAI Key，不会把Codex `full_access`变成低风险模式。Codex仍拥有Worker
运行用户本来能够访问的工程、进程和其它本机资料；因此该模式只用于隔离开发机或明确接受风险的
专用实例，安装与Hub UI必须沿用现有高风险告警和显式选择。

### 13.2 Linux

优先级：

1. systemd credential；
2. 实例credential目录中的普通文件。

文件方式要求：

```text
/var/lib/claw-worker/<instance>/credentials/timiai-api-key
/var/lib/claw-worker/<instance>/credentials/model-bridge-token
```

- 目录 `0700`；
- 文件 `0600`；
- owner为Worker服务用户；
- 拒绝symlink、hardlink、owner不匹配或group/other可读；
- Hub通过SFTP上传临时文件，目标机原子rename；
- 禁止把Key放进SSH命令、systemd unit、argv或journal；
- 写入后只做长度、权限和fingerprint自检，不输出值。

`full_access`时上述owner改为专用Bridge用户，Worker/Codex用户不得加入可读group；Bridge由独立
systemd unit托管，Worker只通过loopback health/model接口交互。

### 13.3 Windows

推荐：

- TimiAI Key和Bridge Token用 DPAPI 加密；
- 优先 CurrentUser；Hub全自动部署无法进入用户会话时，可使用 LocalMachine DPAPI + 严格NTFS ACL；
- 凭据blob仅允许Worker任务用户和SYSTEM访问；
- Worker读取后只传给Bridge内存，不写环境变量；
- 安装日志只显示fingerprint；
- 删除或轮换时安全替换blob并重启Bridge。

`full_access`时TimiAI Key blob不得授权Worker任务用户，必须只允许Bridge服务身份、SYSTEM和
Administrators，并由Bridge身份完成解密。Bridge Token仍可授权Worker任务用户，供Codex command
auth读取。

建议路径：

```text
<instance>\credentials\timiai-api-key.dpapi
<instance>\credentials\model-bridge-token.dpapi
```

禁止：

- 把Key写进 `sidecar.env`；
- 把Key写进 `CODEX_HOME/config.toml`；
- 把Key作为PowerShell命令参数；
- 把Key放进计划任务XML；
- 把Key返回Hub部署日志。

### 13.4 Codex认证助手

Codex自定义 Provider 使用官方支持的 command auth。助手只输出本地 Bridge Token：

```toml
[model_providers.claw_timiai_bridge.auth]
command = "<instance python>"
args = ["<bridge credential helper>", "print-local-token"]
timeout_ms = 5000
refresh_interval_ms = 0
```

助手要求：

- 校验文件/DPAPI blob的owner和ACL；
- stdout仅输出token本体；
- stderr只输出稳定错误码；
- 不接受任意路径参数；
- 不读取TimiAI Key。

## 14. 通用包结构

建议在 Worker仓库增加：

```text
src/model_bridge/
  __init__.py
  cli.py
  server.py
  config.py
  auth.py
  health.py
  responses_contract.py
  state_store.py
  errors.py
  redaction.py
  upstream/
    base.py
    timiai_responses.py
    timiai_chat.py
  translation/
    request.py
    stream.py
    tools.py
    usage.py
  platform/
    base.py
    linux_credentials.py
    windows_dpapi.py

tests/model_bridge/
  fixtures/
  golden/
  test_contract.py
  test_stream_translation.py
  test_tools.py
  test_state_store.py
  test_credentials.py
  test_errors.py
```

入口：

```bash
python -m src.model_bridge.cli serve --config <instance bridge.json>
python -m src.model_bridge.cli health --config <instance bridge.json>
python -m src.model_bridge.cli credential print-local-token --instance <id>
```

业务逻辑不得在PowerShell或shell脚本中再实现一份。

## 15. Bridge配置

配置文件不包含Secret：

```json
{
  "schema": 1,
  "instance_id": "claw-12",
  "listen": {
    "host": "127.0.0.1",
    "port": 19112
  },
  "upstream": {
    "provider": "timiai",
    "base_url": "http://api.timiai.woa.com/ai_api_manage/llmproxy",
    "protocol": "chat_completions",
    "project": "gbt",
    "credential_ref": "timiai:gbt"
  },
  "model": {
    "public_name": "deepseek-v4-pro-r1",
    "upstream_name": "deepseek-v4-pro-r1",
    "context_window": 131072,
    "supports_stream": true,
    "supports_tools": true,
    "supports_parallel_tools": false,
    "supports_structured_output": false,
    "supports_reasoning": true
  },
  "limits": {
    "body_bytes": 8388608,
    "output_bytes": 8388608,
    "stream_idle_seconds": 300,
    "request_timeout_seconds": 3600,
    "max_chain_depth": 128,
    "state_max_bytes": 536870912,
    "state_ttl_days": 30
  },
  "observability": {
    "log_level": "INFO",
    "include_prompt": false,
    "include_tool_arguments": false
  }
}
```

配置必须有canonical JSON和SHA-256；Worker启动时记录配置hash，不记录Secret hash与配置内容全文。

## 16. Codex配置渲染

### 16.1 Subscription

```toml
# Managed by Claw Worker
model_provider = "openai"
model = "<approved model>"
```

- Bridge不启动；
- 复用实例独立 `CODEX_HOME/auth.json`；
- 认证由目标用户完成；
- Hub不得读取订阅认证材料。

### 16.2 TimiAI Bridge

```toml
# Managed by Claw Worker
model_provider = "claw_timiai_bridge"
model = "deepseek-v4-pro-r1"
model_context_window = 131072
web_search = "disabled"

[model_providers.claw_timiai_bridge]
name = "Claw TimiAI Bridge"
base_url = "http://127.0.0.1:19112/v1"
wire_api = "responses"
request_max_retries = 1
stream_max_retries = 2
stream_idle_timeout_ms = 300000
supports_websockets = false
supports_standalone_web_search = false

[model_providers.claw_timiai_bridge.auth]
command = "<instance python>"
args = ["<credential helper>", "print-local-token"]
timeout_ms = 5000
refresh_interval_ms = 0
```

每个实例配置独立，不允许多个Claw共享同一个Bridge Token、state DB或端口配置。

## 17. Worker集成

### 17.1 Provider配置

新增本机受保护字段：

```text
CODEX_AUTH_MODE=subscription|timiai_bridge
CODEX_MODEL_BRIDGE_CONFIG=<absolute path>
CODEX_MODEL_BRIDGE_ENABLED=true|false
```

`CODEX_AUTH_MODE`、Bridge配置路径和启用状态属于本机受保护部署配置。Hub可以展示期望值和实际值，
但运行时`sidecar-config`不能直接覆盖这些字段。

不要新增：

```text
TIMIAI_API_KEY=<secret>
```

到Worker公共环境。

### 17.2 启动顺序

```text
validate release + metadata
  -> validate provider workspace / CODEX_HOME
  -> validate bridge config + credentials
  -> start bridge
  -> wait /readyz
  -> validate managed Codex config
  -> register CodexSdkProvider
  -> start Hub transport
```

任一步失败：

- 不启动Codex TimiAI Provider；
- 不回退为未授权Provider；
- health报告稳定错误码；
- subscription配置不受TimiAI Bridge故障影响。

### 17.3 取消与停止

- invocation取消：取消当前上游HTTP和Codex子进程；Bridge守护进程保持；
- Worker停止：停止接收新请求，等待或取消当前stream，再关闭Bridge；
- 配置切换：停止新请求，回收当前Codex，关闭Bridge，原子替换配置后重启；
- lease丢失：停止Codex调用；Bridge结果不得继续进入Hub final。

## 18. Linux部署方法

### 18.1 发布输入

Worker Release包含：

- Bridge源码/模块；
- 固定版本wheelhouse；
- `--require-hashes` requirements；
- package manifest；
- Bridge schema与模型能力manifest；
- 完整SHA-256；
- 单元和fake集成测试结果。

目标机不得在线安装“最新”依赖。

### 18.2 目录

沿用实例目录，例如：

```text
/opt/claw-worker/<instance>/app/
/var/lib/claw-worker/<instance>/
  bridge/
    bridge.json
    state.sqlite3
    logs/
  credentials/
    timiai-api-key
    model-bridge-token
  home/.codex/
```

权限：

- app由root管理、服务用户只读；
- data / bridge / credentials / CODEX_HOME归服务用户；
- credentials目录0700、文件0600；
- 拒绝workspace、bridge目录和凭据目录为symlink或逃逸路径。

### 18.3 安装流程

建议扩展 `scripts/install-linux.py`：

1. 验证Release manifest、artifact hash和platform；
2. 解包到staging目录；
3. 离线创建/更新venv；
4. 校验Bridge模块可导入；
5. 选择稳定回环端口并验证未被占用；
6. 写不含Secret的 `bridge.json`；
7. 通过SFTP写TimiAI Key临时文件，校验后原子rename；
8. 在目标机生成Bridge Token；
9. 生成只输出Bridge Token的auth helper；
10. 渲染 `CODEX_HOME/config.toml` 指向本地Bridge；
11. 原子切换app软链接/目录；
12. 重启Worker systemd unit；
13. 验证Bridge `/readyz`；
14. 用fake/no-billing诊断检查配置；
15. 只有显式canary步骤才发起真实TimiAI模型请求；
16. 写install metadata、config hash、Bridge版本和回滚点。

### 18.4 systemd

`trusted_host`推荐Bridge仍由Worker拉起，因此无需第二个常驻unit。Worker unit应保持：

- `NoNewPrivileges=true`；
- `PrivateTmp=true`；
- `ProtectSystem=strict`；
- 仅实例data和workspace按权限可写；
- 服务用户无login shell；
- EnvironmentFile不含TimiAI Key；
- Bridge进程处于相同cgroup，Worker停止后无残留。

`full_access`需要隔离TimiAI Key时，使用`--model-bridge-external`拆分unit和服务用户：

```text
claw-model-bridge-<id>.service   User=clawbridge_<id>
claw-worker-<id>.service        Requires/After bridge service
```

但业务实现仍复用同一Python包。

### 18.5 Linux诊断

```bash
sudo -u <service-user> \
  HOME=<service-home> CODEX_HOME=<codex-home> \
  <venv-python> -m src.model_bridge.cli health --config <bridge.json>
```

诊断输出只允许：

- Bridge版本和artifact hash；
- config hash；
- listen地址；
- provider/project/model；
- credential present / ACL valid / fingerprint；
- state DB schema；
- readiness和稳定错误码。

## 19. Windows部署方法

### 19.1 发布输入

与Linux使用相同Bridge代码和Python依赖锁；Windows artifact单独生成platform manifest与hash。

目标机不在线 `pip install`，使用Worker release内wheelhouse。

### 19.2 目录

沿用当前实例目录，例如：

```text
<instance>\app\
<instance>\data\bridge\
  bridge.json
  state.sqlite3
  logs\
<instance>\data\credentials\
  timiai-api-key.dpapi
  model-bridge-token.dpapi
<instance>\data\home\.codex\
```

ACL：

- app：Administrators写，Worker任务用户读/执行；
- data / CODEX_HOME / state DB：Worker任务用户和Administrators；
- credential blob：Worker任务用户、SYSTEM、Administrators；
- 拒绝Everyone/Users写权限、reparse point和UNC/device path。

### 19.3 安装流程

建议扩展 `scripts/install.ps1`：

1. 验证Release、Authenticode（如适用）、manifest和SHA；
2. 解包到staging目录；
3. 离线安装固定Python依赖；
4. 验证Bridge模块；
5. 选择稳定回环端口；
6. 写 `bridge.json`；
7. TimiAI Key通过受保护临时文件进入目标机；
8. 使用DPAPI加密为实例credential blob；
9. 删除临时明文文件并验证不存在；
10. 在目标机生成并DPAPI加密Bridge Token；
11. 生成auth helper；
12. 渲染实例独立 `CODEX_HOME/config.toml`；
13. 更新受保护 `sidecar.env`，只写Bridge配置路径，不写Key；
14. 原子切换app目录；
15. 重新注册/启动当前计划任务；
16. Worker启动Bridge并等待readiness；
17. 做无计费诊断；
18. 显式canary才调用真实TimiAI；
19. 保存回滚目录、install metadata和hash证据。

### 19.4 Worker启动

`trusted_host`不增加第二个计划任务。现有 Worker计划任务启动后：

- 读取DPAPI凭据；
- 创建Bridge子进程；
- 把子进程加入Job Object；
- readiness后再启动Codex任务处理；
- Worker退出关闭Job Object，Bridge和Codex一起回收。

`full_access`默认可沿用同用户子进程方式，但安装/UI必须明确提示该用户可访问本机凭据。需要隔离
TimiAI Key时，改用独立Bridge服务/任务身份，DPAPI/ACL只授予该身份；Worker计划任务只持有Bridge
Token并通过loopback检查readiness。升级和停止脚本必须同时验证两个进程身份、版本、配置hash与
ownership receipt，且不得按进程名批量结束其它实例。

### 19.5 Windows诊断

```powershell
& <instance-python> -m src.model_bridge.cli health `
  --config <instance>\data\bridge\bridge.json
```

诊断不得解密并显示凭据；只显示present、ACL、scope和fingerprint。

## 20. Hub部署合同

### 20.1 创建/部署参数

Hub部署API建议使用：

```json
{
  "agent_type": "codex",
  "codex_auth_mode": "timiai_bridge",
  "timiai_project": "gbt",
  "llm_model": "deepseek-v4-pro-r1",
  "worker_release_id": "<approved immutable release>",
  "codex_workspace": "<local absolute path>"
}
```

兼容期可接受旧值：

```text
codex_auth_mode=timiai -> timiai_bridge
```

但数据库和实际下发应规范化为 `timiai_bridge`。

### 20.2 Hub下发内容

非Secret配置：

- auth mode；
- project；
- model；
- Bridge config schema；
- Worker Release和Bridge artifact hash；
- workspace和allowed dirs；
-健康/灰度策略。

Secret：

- Hub从对应项目密钥箱解析Key；
- 仅部署阶段通过SFTP/受保护通道写入目标credential store；
- 不进入 `sidecar-config` JSON；
- 不进入部署日志和数据库deployment log_tail；
- Hub只保存secret key名称/fingerprint，不保存部署回显。

### 20.3 sidecar-config

运行时最多下发只读期望状态，用于比较和提示需要重新部署：

```json
{
  "model_bridge": {
    "mode": "timiai_bridge",
    "project": "gbt",
    "model": "deepseek-v4-pro-r1",
    "config_version": 3,
    "restart_required": true
  }
}
```

不能下发Key、Bridge Token或任意上游URL。URL来自Worker批准的本机配置模板。Sidecar收到
`restart_required=true`只能上报配置漂移/待部署状态，不能热改本机Provider或自行重启切换。

## 21. Release与供应链

Worker Release manifest增加：

```json
{
  "model_bridge": {
    "schema": 1,
    "version": "1.0.0",
    "artifact": "claw-model-bridge.whl",
    "sha256": "<64 hex>",
    "supported_platforms": ["windows-x86_64", "linux-x86_64"],
    "protocols": ["responses", "chat_completions"]
  }
}
```

要求：

- 依赖全部固定版本和hash；
- 无运行时在线更新；
- Hub只部署approved Release；
- Worker启动时重验Bridge artifact；
- Bridge version、config hash、model manifest hash进入诊断；
- Windows artifact签名策略沿用Worker发布规范；
- 回滚不删除state DB和诊断证据，但凭据问题时立即轮换/失效。

## 22. 观测与脱敏

允许记录：

- request id、invocation id的不可逆hash；
- Bridge / Worker版本；
- TimiAI项目和模型名；
- latency、TTFB、stream duration；
- HTTP状态、稳定错误码；
- token usage；
-重试次数、熔断状态；
-配置和artifact hash。

禁止记录：

- Authorization Header；
- TimiAI Key / Bridge Token；
- Prompt全文；
-完整tool arguments / output；
-Hub Token；
-Codex auth.json；
-用户私有绝对路径；
-原生异常中可能包含Secret的message。

日志发送Hub前再次脱敏并限长。

## 23. 测试计划

### 23.1 H0：Codex协议捕获

开发转换器前，先让当前固定Codex SDK指向fake Responses server，捕获且脱敏：

-实际path；
-request fields；
-SSE事件顺序；
-Thread续接；
-function/MCP工具调用；
-structured result；
-取消和错误行为。

形成版本化golden traces。没有这一步不得宣称转换合同完整。

Hermes能力对齐MVP只需覆盖固定SDK在四类目标任务中实际出现的字段与事件：Hub消息、企微消息、
Workflow AgentTask和本机workspace工具调用。Golden之外的Responses字段一律fail closed，不以
静默忽略方式伪装兼容。

### 23.2 单元测试

- Responses schema合法/非法；
-普通文本和多段文本；
-stream拆包、合包、半包；
-函数名、call id、arguments delta；
-并行工具调用；
-tool output续接；
-`previous_response_id`；
-usage；
-reasoning字段；
-structured output；
-HTTP 200错误体；
-401/403/429/435/5xx；
-TTFB、idle和总超时；
-取消；
-state DB损坏和恢复；
-Key/Token/Prompt脱敏；
-模型白名单和项目隔离。

### 23.3 集成测试

```text
fake Codex client -> real Bridge -> fake TimiAI
real Codex SDK -> real Bridge -> fake TimiAI
fake Worker -> real Bridge -> fake TimiAI
```

Linux和Windows运行相同golden测试集。

### 23.4 真实canary

每个项目/模型至少验证：

1. 单轮文本；
2. 多轮Thread；
3. function tool；
4. MCP只读；
5. repo_read；
6. repo_write隔离工作区；
7. Workflow严格JSON；
8. 中文和长输入；
9. 限流、额度耗尽和模型无权限；
10. Worker/Bridge重启；
11.取消和无孤儿进程；
12.凭据扫描。

真实canary必须记录：Release、commit、Bridge版本、平台、时间、项目、模型、配置hash和脱敏证据路径。

## 24. 验收门禁

### 24.1 Hermes能力对齐MVP门禁

进入首期灰度前要求：

- Hub消息能够经Codex/TimiAI生成回复并由Sidecar完成原消息闭环；
- 企微Owner消息能够进入同一Codex Provider并收到最终回复；
- Workflow AgentTask能够完成heartbeat/progress和严格JSON result回写；
- `trusted_host`下Codex能够在批准workspace内读写文件、执行shell/Git/聚焦测试，且不能访问
  credentials目录；
- `full_access`下Codex能够执行当前Worker用户有权执行的本机操作；同用户Bridge按高风险模式验收，
  独立Bridge身份部署还必须证明Codex不能读取TimiAI Key；两种方式都必须在隔离开发机显式canary；
- 串行function/MCP工具call id与tool output配对100%；
- Windows与Linux相同golden测试100%一致；
- 未授权项目、模型、URL和权限模式切换0次；
- Bridge/Codex孤儿进程0；
- 任何SSE已输出后重复模型请求0次；
- subscription实例在Bridge完全不可用时仍可独立运行；
- 每个平台至少20次连续真实readonly canary无失败，并完成1次受控repo_write canary；
- Hub展示实际Provider=`codex`、auth mode、项目、模型和Bridge版本，但不展示Key。

### 24.2 后续完整能力门禁

并行工具、多项目/多模型、reasoning和更完整Responses兼容进入后续生产前再要求：

- Linux与Windows协议golden测试100%一致；
- Workflow结构化结果合规率100%；
-工具call id / output配对100%；
-无Secret泄漏；
-未授权模型/项目切换0次；
-Bridge/Codex孤儿进程0；
-任意SSE已输出后重复模型请求0次；
-lease丢失后stale final result 0次；
-至少一次quota、rate-limit、stream断开、Bridge崩溃和回滚演练；

## 25. 灰度计划

### Phase 0：开发和fake测试

- 不连接真实TimiAI；
- 不发企微；
- 不访问真实Hub业务数据；
-完成协议捕获、转换器、凭据和双平台安装测试。

### Phase 1：只读canary

- 选择1个Linux Claw和1个Windows Claw；
-只允许message/readonly Workflow；
-关闭外部通知；
-不允许repo_write和设备操作。

### Phase 2：影子对比

- subscription/现有稳定Provider实际执行；
-TimiAI Bridge读取相同脱敏上下文，只生成判断；
-比较结果、工具计划、错误分类和成本；
-不重复有副作用动作。

### Phase 3：受控repo_write

-限定工作区；
-写后必须验证；
-Git/设备/Unity仍受既有策略限制；
-先完成`trusted_host`受控repo_write；随后在隔离开发机执行显式`full_access` canary，验证独立
 Bridge身份、TimiAI Key不可读、固定项目/模型和回滚。未通过前不对生产实例开放。

### Phase 4：按Claw启用

-Hub部署页开放 `timiai_bridge`；
-只对批准的Worker Release启用；
-保留一键切回subscription或上一稳定Provider配置。

## 26. 回滚

优先配置级回滚：

1. 停止给TimiAI Bridge分配新invocation；
2. 等待或取消当前Codex请求；
3. 关闭Bridge；
4. 恢复上一份 `CODEX_HOME/config.toml`；
5. 切回subscription或上一稳定Provider；
6. 重启Worker并验证；
7. 保留state DB、错误指标和脱敏日志；
8. 若涉及凭据，轮换Bridge Token和TimiAI Key；
9. 不重放已产生工具副作用的任务；
10. Hub部署记录标记实际回滚Release和原因。

升级失败时使用Worker现有 `.app.previous` / release回滚机制，不能只回滚Bridge文件而留下不匹配的Codex配置。

## 27. Worker开发任务拆分

### H0：Hermes能力对齐协议捕获

- 固定Codex SDK的fake Responses capture server；
- Hub消息、企微、Workflow、本机工具四类golden traces；
- 确认TimiAI批准模型支持stream和串行tools；
- 明确最小Responses/SSE字段集和稳定错误码。

### H1：最小Bridge与Chat Completions转换

- Bridge config/schema、health/ready和本地认证；
- 最小Responses请求校验；
- Chat Completions messages、stream、usage；
- 串行tool/function call及tool output续接；
- 必要的previous response状态；
- fake TimiAI集成测试。

### H2：Worker业务能力接入

- 默认Sidecar与V4共用Bridge supervisor；
- Hub MCP、企微最终回复、Workflow严格结果；
- trusted_host workspace文件/shell/Git/测试；
- full_access本机操作与独立Bridge凭据身份；
- subscription与timiai_bridge模式隔离；
- auth mode感知的健康检查和企微错误提示。

### H3：跨平台部署与发布

-Linux credential store；
-Windows DPAPI；
-Worker supervisor；
-Job Object/process group；
-安装、诊断、回滚。

### H4：Hub集成与真实canary

-新auth mode合同；
-部署配置和密钥箱；
-release approval；
-健康和审计展示；
-旧 `timiai` 配置迁移。
-Linux/Windows各一个实例；
-首期批准的单项目/单模型；
-故障注入；
-安全扫描；
-灰度和回滚演练。

### F1：后续完整Codex能力

-Responses透传Adapter；
-并行tool calls；
-多项目/多模型批准切换；
-reasoning/structured output原生映射；
-Bridge额外硬化（更细沙箱、网络策略和审计）；
-扩展golden与真实canary矩阵。

## 28. Worker首期交付物

- Bridge核心源码；
-Responses合同与Chat转换器；
-TimiAI Chat Completions Upstream Adapter；
-Linux/Windows credential store；
-Worker Bridge supervisor；
-state DB schema与迁移；
-config schema与模型能力manifest；
-Linux/Windows安装和诊断；
-固定依赖、wheelhouse、release manifest和hash；
-单元、fake集成、协议golden测试；
-真实canary记录；
-升级、回滚和故障排查文档。

最终状态必须区分：

- 已编码；
- 单元测试通过；
- fake集成通过；
- Linux实机通过；
- Windows实机通过；
- 指定项目/模型真实canary通过；
- 可灰度；
- 可生产。

不能把“Bridge进程启动”“返回过一次文本”或“Codex能连接本地端口”写成生产完成。

## 29. Hub后续改造清单

待Worker交付并发布approved Release后，Hub再做：

1. 部署选项 `codex_auth_mode` 增加/规范化为 `timiai_bridge`；
2. 不再把TimiAI上游Key作为Codex command auth结果；
3. 根据Worker manifest渲染本地Bridge配置；
4. 通过SFTP/目标机credential helper写入Key；
5. 保存Bridge版本、artifact hash、config hash和credential fingerprint；
6. sidecar-config只展示非Secret期望状态和`restart_required`，不负责应用模式变更；
7. UI展示subscription / TimiAI Bridge、项目和模型；
8. canary通过前不设为全局默认；
9. 保留旧配置回滚和审计；
10. 不在Hub实现第二份协议转换器。

## 30. 开放问题

Worker开发开始前需要通过M0证据确认：

1. 当前固定Codex SDK实际发送的Responses字段和事件全集；
2. TimiAI各项目是否存在真正完整的Responses入口；
3. 各模型工具调用、并行工具、reasoning和structured output能力；
4. TimiAI错误码与重试语义，尤其HTTP 200错误体；
5. Codex是否在目标版本使用 `previous_response_id`、完整input或conversation；
6. Windows Worker任务身份适合CurrentUser还是LocalMachine DPAPI；
7. Bridge稳定端口分配是否由安装器或Hub部署记录管理；
8. state DB保留周期和容量上限；
9. 真实额度、并发和计费项目归属；
10. Worker公开Python SDK与当前内部固定包的发布支持边界。

任何未确认项都应作为 capability / canary门禁，不允许用静默fallback掩盖。
