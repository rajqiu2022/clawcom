# 自研定制 Agent 接入 Hub 改造说明

> 用途：交给 Windows / Linux 定制 Agent 项目的开发 AI，作为实现依据。  
> 目标：把定制 Agent 改造成 Hub 可编排、可下发岗位配置、可调用 MCP、可回写结构化结果的认知执行器。  
> Hub 仓库：`claw_team`  
> 更新时间：2026-08-02

## 1. 总体定位

定制 Agent 不重新实现一套 Hub 通信协议，推荐采用：

```text
Hub
  → SSE sidecar
  → custom-agent 可执行程序
  → LLM / Skills / MCP / Hub API
```

各组件职责：

| 组件 | 职责 |
| --- | --- |
| Hub | 身份、工位、Profile、Workflow、门禁、状态、产物和审计 |
| SSE sidecar | SSE 长连接、任务接收、心跳、processing/done/failed 回写、Prompt 拼装 |
| custom-agent | 理解任务、加载 Skill、调用 MCP/Hub API、生成结构化结果 |
| workflow-worker | Unity、ADB、构包、自动化脚本等确定性命令执行 |

`custom-agent` 是“认知执行器”，`workflow-worker` 是“确定性执行器”，不要合并成一个无限膨胀的进程。

## 2. Hub 侧已经具备的能力

定制 Agent 不需要重复实现：

- `CLAW_ID + CLAW_TOKEN` 身份体系。
- SSE `/api/openclaws/<id>/events` 长连接。
- message、todo、workflow agent task 的接收与状态回写。
- Workflow 心跳与 progress 上报。
- Workflow 超时、重试、阻断和通知。
- `task_context` 注入：required skills、preflight、pitfall、references。
- Agent Profile 下发：工位、岗位职责、工作规范、必装 Skill、Profile 版本。

对应代码：

- `openclaw-agent/skills/hub-sse-sidecar/scripts/sidecar_v2.py`
- `web/app/api/agent_client.py`
- `web/app/services/task_context.py`
- `web/app/api/workflows.py`

## 3. 第一阶段必须实现：统一 CLI 契约

### 3.1 当前真实调用格式

当前 `sidecar_v2.py` 的 custom 分支实际调用：

```shell
custom-agent --message "<完整 Prompt>" --timeout 300
```

配置：

```ini
AGENT_TYPE=custom
CUSTOM_AGENT_BIN=C:\Agent\custom-agent.exe
```

Linux 示例：

```ini
AGENT_TYPE=custom
CUSTOM_AGENT_BIN=/opt/custom-agent/custom-agent
```

### 3.2 已知文档差异

旧版 `hub-sse-sidecar/SKILL.md` 和 `install.sh` 的部分注释仍写成：

```shell
custom-agent agent --message "<Prompt>" --timeout 300
```

但当前生产代码没有 `agent` 子命令。

开发时必须以当前代码为准。为兼容新旧 sidecar，建议定制 Agent 同时接受：

```shell
custom-agent --message "..." --timeout 300
custom-agent agent --message "..." --timeout 300
```

两种形式进入同一处理函数。

### 3.3 CLI 行为要求

定制 Agent 必须满足：

1. `--message`：UTF-8 完整 Prompt。
2. `--timeout`：最大执行秒数。
3. 非交互执行，不能等待终端输入。
4. 成功退出码为 `0`。
5. 失败退出码非 `0`。
6. stdout 只输出最终响应；日志写 stderr 或独立日志文件。
7. Windows 与 Linux 的参数语义、输出格式和退出码一致。
8. 收到超时/终止信号时停止子进程和工具调用，不遗留后台进程。

建议入口：

```python
def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        response = run_agent(message=args.message, timeout=args.timeout)
        sys.stdout.write(response)
        return 0
    except Exception as exc:
        print(format_error(exc), file=sys.stderr)
        return 1
```

## 4. Prompt 中需要识别的内容

sidecar 已经把以下信息拼进 Prompt，定制 Agent 不需要额外拉取才能开始执行：

### 4.1 岗位 Profile

格式示意：

```text
=== Hub Agent 岗位说明书（必须遵守）===
- 工位：需求分析岗
- Profile：需求分析 Agent v2
- 必装/必读 Skills：agent-operating-protocol, requirement-analysis

岗位职责：
...

工作规范：
...
=== 岗位说明书结束 ===
```

处理规则：

- Profile 的岗位约束优先于普通任务文本。
- 本地用户配置不能覆盖 Hub 下发的安全规则和产出契约。
- Profile 版本必须记录到本次执行日志。
- 多个 Profile 同时存在时，sidecar 下发的 `active_agent_profile` 为当前主 Profile。

### 4.2 任务上下文

可能包含：

```json
{
  "project": "RacingGO",
  "required_skills": [
    "requirement-analysis",
    "basic-operations-preflight"
  ],
  "primary_skill": "requirement-analysis",
  "preflight_checklist": [],
  "top_pitfalls": [],
  "references": []
}
```

Agent 必须：

1. 先加载 required skills。
2. 读取 top pitfalls，避免重复踩坑。
3. 写操作前执行 preflight。
4. references 标记为“务必查阅”时必须读取。
5. 写操作完成后使用独立 GET 或 Hub `/ops/verify` 回读确认。

### 4.3 Workflow 任务信息

Workflow AgentTask Prompt 包含：

- Run ID、Step ID、Runner。
- Progress API、Result API。
- 固定 inputs。
- 变量 input_vars。
- 前序步骤 outputs。
- Run context / start_vars。

Agent 不能只回复“收到”，必须真实执行并返回结构化结果。

## 5. Workflow 结构化结果契约

### 5.1 正常完成

最终 stdout 只输出一个 JSON 对象：

```json
{
  "status": "passed",
  "summary": "完成 24 条需求分析，发现 3 个高风险问题",
  "metrics": {
    "total_requirements": 24,
    "reviewed_requirements": 24,
    "high_risk_issue_count": 3
  },
  "outputs": {
    "review_verdict_count": 24,
    "high_risk_issue_ids": [101, 102, 103]
  },
  "evidence": {
    "hub_resources": [
      {
        "type": "requirement_review_verdict",
        "id": 501
      }
    ]
  },
  "logs": {},
  "blocker": {}
}
```

### 5.2 业务阻断

无法继续，但不是 Agent 运行时崩溃：

```json
{
  "status": "blocked",
  "summary": "需求验收标准缺失，无法形成可测试用例",
  "metrics": {
    "missing_acceptance_criteria": 4
  },
  "outputs": {},
  "evidence": {},
  "logs": {},
  "blocker": {
    "type": "missing_acceptance_criteria",
    "message": "4 条需求没有测试验收标准",
    "suggested_action": "需求负责人补齐验收标准后重试"
  }
}
```

### 5.3 运行失败

工具崩溃、模型不可用、MCP 鉴权失败等运行时故障：

```json
{
  "status": "failed",
  "summary": "TAPD MCP 调用失败",
  "metrics": {},
  "outputs": {},
  "evidence": {},
  "logs": {
    "error_code": "MCP_AUTH_FAILED"
  },
  "blocker": {
    "type": "mcp_auth_failed",
    "message": "无法访问 TAPD MCP",
    "suggested_action": "检查 Taihu Token 和 MCP 服务状态"
  }
}
```

合法状态：

```text
passed / blocked / failed / skipped
```

注意：

- `metrics` 给当前步骤门禁判断使用。
- `outputs` 给后续步骤使用。
- `evidence` 必须指向可复查证据，不能只写自然语言“已完成”。
- 不要在 JSON 前后输出 Markdown 代码块或解释文本。

## 6. Progress 与长任务

sidecar 会独立发送 Workflow heartbeat，定制 Agent 无需自己保持 SSE。

但长耗时任务需要调用 Prompt 中给出的 Progress API，建议阶段：

```text
prepare / fetch / analyze / write / verify / summarize
```

示例：

```http
POST /api/v1/workflow-runs/123/steps/requirement_review/progress
Authorization: Bearer <CLAW_TOKEN>
Content-Type: application/json

{
  "worker_id": "custom-agent:27",
  "phase": "analyze",
  "message": "已分析 15/24 条需求",
  "percent": 62,
  "heartbeat": true
}
```

建议至少在以下时机上报：

- 开始拉数据。
- 数据拉取完成。
- 分析完成。
- Hub 写入完成。
- 回读验证完成。

## 7. Skill 加载器改造

定制 Agent 必须具备 Skill 加载器，不能只把 Skill 名字显示给模型。

### 7.1 加载流程

```text
读取 required_skills
  → 查本地 Skill 缓存
  → 校验版本/更新时间
  → 缺失或过期时从 Hub 获取
  → 加载 SKILL.md
  → 按需加载 references/scripts
  → 记录本次实际加载版本
```

### 7.2 第一阶段必支持

- `agent-operating-protocol`
- `basic-operations-preflight`
- `requirement-analysis`
- `engineering-analysis`
- `testcase-manager`
- `case-review`
- `knowledge-manager`
- `workflow-manager`

### 7.3 加载约束

- 不允许只加载摘要，必须读取完整 `SKILL.md`。
- references/scripts 只在 Skill 明确要求时加载，避免上下文爆炸。
- Skill 缺失时不能假装执行，应返回 `blocked`。
- Profile 要求的 Skill 版本不满足时，应返回 `blocked` 并给出实际版本。

## 8. 工具与 MCP 能力

首个闭环是：

```text
需求评审
→ 工程影响分析
→ 用例设计
→ 独立评审
→ 人工一次确认
```

定制 Agent 至少需要以下工具：

### 8.1 TAPD MCP

- 查询需求、迭代、附件和评论。
- 创建/更新评论。
- 回写需求评审进度。
- 后续阶段创建 Bug、建立需求与 Bug 关联。

鉴权使用运行环境提供的 MCP / 太湖 Token，不把凭证写进代码、日志、Git 或 Prompt。

### 8.2 Hub API

至少支持：

```text
GET/POST /api/v1/requirements/*
GET/POST /api/v1/testcase-libraries/*
GET/POST /api/v1/topics/*
GET/POST /api/v1/workflow-runs/*
GET      /api/v1/tasks/{ref_type}/{ref_id}/context
POST     /api/v1/ops/verify
```

### 8.3 工程工具

- Git status / diff / log。
- 文件与符号检索。
- 只读代码分析。
- 必要时运行项目已有静态分析脚本。

### 8.4 工具执行规则

- 每个工具调用有 timeout。
- 同一失败最多重试一次；第二次仍失败就 blocked。
- 写操作必须有幂等键或先查后写。
- 写后必须回读验证。
- 所有外部副作用记录操作目标、响应 ID 和验证结果。

## 9. 运行时架构建议

建议目录：

```text
custom-agent/
├── cli.py
├── runtime/
│   ├── agent_runtime.py
│   ├── prompt_parser.py
│   ├── result_normalizer.py
│   ├── timeout.py
│   └── cancellation.py
├── profiles/
│   └── cache.py
├── skills/
│   ├── loader.py
│   ├── cache.py
│   └── hub_client.py
├── tools/
│   ├── hub_api.py
│   ├── mcp_client.py
│   ├── filesystem.py
│   └── shell.py
├── providers/
│   ├── openclaw.py
│   ├── hermes.py
│   └── openai_compatible.py
├── audit/
│   └── execution_log.py
└── tests/
```

核心接口建议：

```python
class BrainProvider:
    def run(self, prompt: str, tools: list, timeout: int) -> str:
        ...


class SkillLoader:
    def load_required(self, skill_names: list[str]) -> list:
        ...


class AgentRuntime:
    def execute(self, message: str, timeout: int) -> str:
        ...
```

Provider 可插拔，但 Runtime、Skill、工具审计和结果规范化必须共用，避免不同模型各实现一套行为。

## 10. Windows / Linux 一致性

必须避免只在 Windows 可用：

- 不硬编码盘符。
- 路径统一使用语言标准库。
- Shell 命令区分 PowerShell / bash，禁止字符串拼接注入。
- 子进程创建新进程组，超时时杀死整组。
- UTF-8 输入输出。
- 日志文件支持滚动。
- 配置优先环境变量，其次配置文件，不从源码读取秘密。

Windows 特别注意：

- `.exe` / `.cmd` / `.ps1` 的启动方式不同。
- `subprocess` 参数必须使用 argv 数组，不拼接 command string。
- 终止任务时清理子孙进程。
- 不能依赖 `fcntl`、systemd、Unix signal。

Linux 特别注意：

- systemd service 的 WorkingDirectory、EnvironmentFile 和用户权限。
- 非 root 运行。
- MCP/项目目录通过明确的 allowlist 授权。

## 11. workflow-worker 边界

现有 `workflow_worker.py` 用于：

- Unity 自动化。
- ADB 操作。
- 构包、下载、安装。
- 固定脚本和确定性 runner。

它不是完整认知 Agent，不要把 LLM、多轮工具规划、Skill 加载全部塞进去。

推荐部署：

```text
custom-agent service
  负责认知任务，由 sidecar 调用

workflow-worker service
  负责 worker_task，轮询/claim/执行本地命令/回写结果
```

两者可以共用同一个 `CLAW_ID / CLAW_TOKEN`，但使用不同日志和进程。

## 12. 安全要求

- Token、MCP 凭证、LLM API Key 禁止写入 stdout。
- Prompt 日志需要脱敏。
- Hub Profile 不允许直接执行任意代码。
- Skill scripts 必须来自已审核 Skill，或经过 allowlist。
- Shell 工具默认关闭，按 Profile/工位开启。
- 文件系统默认只读，写目录使用 allowlist。
- 对外写 TAPD、发企微、提交 Bug 等动作必须保留证据 ID。
- Agent 不得自行修改本地 Profile 来绕过 Hub 版本和考试门禁。

## 13. 测试清单

### 13.1 CLI

- [ ] `custom-agent --message "hello" --timeout 30` 正常返回。
- [ ] 兼容 `custom-agent agent --message ...`。
- [ ] 中文 Prompt 不乱码。
- [ ] 成功退出码为 0。
- [ ] 模型异常时退出码非 0，stderr 有错误。
- [ ] 超时后无残留子进程。
- [ ] stdout 不混入日志。

### 13.2 Profile

- [ ] 能识别并遵守岗位说明书。
- [ ] 能记录 Profile key/version。
- [ ] required Skill 缺失时 blocked。
- [ ] Profile 更新后下一次任务使用新版本。

### 13.3 Workflow

- [ ] 输出合法 JSON。
- [ ] `metrics/outputs/evidence/blocker` 类型正确。
- [ ] 上游 outputs 能被读取和使用。
- [ ] 长任务能上报 progress。
- [ ] blocked 与 failed 能正确区分。
- [ ] 不输出“收到”后立即成功。

### 13.4 工具

- [ ] TAPD MCP 查询成功。
- [ ] Hub API GET/POST 成功。
- [ ] 写操作后有独立回读。
- [ ] 凭证不出现在日志。
- [ ] 同一失败不会无限重试。

### 13.5 跨平台

- [ ] Windows 运行通过。
- [ ] Linux 运行通过。
- [ ] 相同 Profile 和测试输入下，结果 JSON 结构一致。

## 14. 分阶段交付

### M1：最小接入

- CLI 契约。
- 非交互运行。
- stdout/stderr/退出码规范。
- Workflow JSON 结果。
- Windows 可执行程序。

完成标准：sidecar 能成功调用 custom Agent，Workflow 节点从 pending 到 passed/blocked。

### M2：可完成首个业务闭环

- Hub API 工具。
- TAPD MCP。
- required Skill 加载。
- Profile 识别。
- Progress 上报。
- 写后回读验证。

完成标准：能独立执行需求分析岗或用例设计岗任务。

### M3：团队化

- Profile 本地缓存和版本上报。
- 工位准入考试状态展示。
- 多 Provider。
- Windows/Linux 双平台。
- 统一审计日志。

完成标准：同岗位替换 Agent 实例后，输出结构和质量门禁仍一致。

### M4：稳定性

- 并发任务隔离。
- 取消与超时。
- 崩溃恢复。
- 工具限流。
- 指标和告警。

## 15. 给开发 AI 的执行要求

开发 AI 开始修改定制 Agent 仓库前必须先回答：

1. 当前 CLI 入口文件在哪里？
2. 当前是否支持非交互 `--message`？
3. 当前日志是否混入 stdout？
4. 当前有哪些 Brain Provider？
5. 当前 MCP 客户端在哪里？
6. 当前 Skill 如何加载？
7. 当前 Windows 子进程超时如何清理？
8. 当前测试框架和 CI 命令是什么？

然后按以下顺序实施：

```text
CLI 契约测试
→ CLI 最小实现
→ Workflow JSON 测试
→ 结果规范化
→ Profile/Skill 加载
→ Hub API/MCP 工具
→ Windows 超时与子进程清理
→ Linux 兼容
→ 端到端接 Hub 验证
```

禁止事项：

- 不得重写 Hub SSE 协议。
- 不得让 custom Agent 自己轮询 Workflow。
- 不得在第一阶段引入另一个任务数据库。
- 不得把 workflow-worker 和认知 Runtime 强行合并。
- 不得在没有回读验证时声称写入成功。
- 不得只完成 Demo 而没有自动化测试。

## 16. 联调验收场景

最终使用 Hub 内置 Workflow：

```text
agent_team_req_to_case_loop
```

至少完成一次真实迭代联调：

1. Hub 把 `requirement_review` 节点派给需求分析岗。
2. custom Agent 读取 Profile、required Skills、迭代 ID 和历史 pitfall。
3. 从 TAPD/Hub 拉需求。
4. 写入逐条 `RequirementReviewVerdict`。
5. 回读确认评审数量等于需求数量。
6. stdout 输出结构化 JSON。
7. Hub 门禁通过并派发下一工位。
8. 替换另一台同岗位 Agent 重跑，输出 JSON 结构保持一致。

验收指标：

- 人工介入不超过一次。
- 无未解释的需求覆盖缺口。
- 每步能追溯 Agent、Profile 版本、Skill 版本和证据。
- 运行失败不会被误报为 passed。

## 17. `claw-worker-windows-v2` 现状审查（2026-08-02）

### 17.1 审查范围与结论

审查仓库：`F:\Code\claw-worker-windows-v2`。

当前工程并不是本文前面设想的、可被 Hub sidecar 通过 `CUSTOM_AGENT_BIN` 直接调用的独立认知执行器。其真实架构是：

```text
Hub SSE
  → Windows Worker 内置 sidecar_v2.py
  → CustomAgentBridge
  → custom-agent Python 薄封装
  → Pi CLI
```

因此需要区分两种部署模式：

1. **完整 Windows Worker 模式**：部署整个 `claw-worker-windows-v2`，由其内置 sidecar 连接 Hub。这是当前仓库的实际设计和近期推荐路径。
2. **Hub `CUSTOM_AGENT_BIN` 直调模式**：只把 custom-agent 作为外部可执行程序交给 Hub sidecar。当前工程不满足该契约，必须先完成 17.6 的 P0 改造。

不要把两种模式混为一谈。完整 Worker 模式下，生产配置实际是 `AGENT_TYPE=pi + CUSTOM_AGENT_ENTRY`；它不会走 Hub sidecar 的 `AGENT_TYPE=custom + CUSTOM_AGENT_BIN` 分支。

### 17.2 当前工程结构

| 模块 | 主要路径 | 当前职责 |
| --- | --- | --- |
| 安装入口 | `Setup.ps1`、`scripts/install.ps1` | 安装 Python 源码、生成配置、创建 Windows 计划任务 |
| Worker 启动 | `scripts/start.ps1` | 启动内置 sidecar |
| Hub 通信 | `src/sidecar_v2.py` | SSE、Todo、Workflow、消息、心跳、Hub proxy |
| Agent 桥接 | `src/custom_agent_bridge.py` | 写 context 文件、调用 custom-agent、处理超时与清理 |
| CLI 入口 | `src/custom_agent_entry.py`、`packages/custom-agent/src/claw_custom_agent/cli.py` | 解析参数并调用 Provider |
| Brain Provider | `packages/custom-agent/src/claw_custom_agent/providers.py` | 当前只实现 `PiBrainProvider` |
| 子进程治理 | `packages/custom-agent/src/claw_custom_agent/process.py` | Windows Job Object、POSIX 进程组和超时清理 |
| Workflow 结果 | `src/agent_protocol.py` | 解析、补齐和规范化步骤结果 |
| Prompt 与记忆 | `src/context_builder.py`、`src/local_memory.py` | Prompt 分段、本地 SQLite 记忆 |
| Pi 会话 | `src/pi_session.py` | Pi session 文件和租约 |
| Pi 工具扩展 | `src/pi-extensions/` | Hub API 代理、路径策略和命令保护 |

当前技术栈为 Python 3.10+、TypeScript Pi extension、PowerShell 5.1 和 Windows 计划任务。没有 MSI/EXE/PyInstaller 成品，部署仍依赖 Python 安装树和 Pi CLI。

### 17.3 已具备能力

以下能力已经具备或基本可用：

- CLI 已解析 `--message`、`--timeout`，并兼容可选的 `agent` 子命令。
- 退出码有基本区分：成功、参数/context 错误、Provider 错误和超时。
- 成功结果写 stdout，错误写 stderr。
- Windows 使用 Job Object 清理子孙进程，POSIX 分支使用进程组清理。
- 内置 sidecar 已支持 Hub SSE、Todo、Workflow、心跳、进度和结果回写。
- 已有 Pi session、本地 SQLite 记忆和 Hub API loopback proxy。
- context 文件、环境变量和日志具有一定脱敏与敏感字段保护。
- Pi extension 提供路径 allowlist 和命令保护。
- 已有 Python、集成测试和 PowerShell 测试，但未发现固定 CI 流水线。

### 17.4 与本文目标的主要差距

| 要求 | 状态 | 审查结论 |
| --- | --- | --- |
| `custom-agent --message ... --timeout ...` 可独立执行 | 缺失 | CLI 虽能解析参数，但没有 `--context-file` 时直接返回 `provider not configured`，退出码为 2 |
| 兼容 `agent` 子命令 | 已具备 | `cli.py` 会剥掉首个 `agent` 参数 |
| Profile 进入最终 LLM Prompt | 缺失 | Profile 只进入 context JSON 元数据，`PiBrainProvider` 只读取 `prompt.rendered`，实际模型看不到岗位说明书 |
| Workflow stdout 严格结构化 | 部分具备 | 规范化发生在 sidecar，不在 custom-agent；Pi 可以输出自然语言 |
| 运行失败不误报 passed | 存在风险 | `src/agent_protocol.py` 对非 JSON 文本存在默认归为 `passed` 的行为 |
| required Skill 真正加载 | 缺失 | 当前只把 Skill 名称写进 Prompt，没有读取 `SKILL.md`、版本校验或缓存 |
| Skill 缺失时 blocked | 缺失 | 没有 Skill Loader，也没有对应门禁 |
| TAPD MCP | 缺失 | 未实现 MCP 客户端 |
| Hub API | 部分具备 | Pi 可通过 sidecar 的 `hub_api` 代理调用，但 Agent Runtime 没有统一客户端和写后回读框架 |
| 长任务 Progress | 部分具备 | sidecar 有 heartbeat，Prompt 也要求模型调用 Progress API，但没有 Runtime 级阶段上报 |
| 多 Provider | 缺失 | custom-agent 当前只有 Pi Provider |
| Windows 独立可执行文件 | 缺失 | 依赖 Python 源码安装树 |
| Linux 生产部署 | 缺失 | 核心进程代码有 POSIX 分支，但没有 Linux 安装脚本和 systemd 服务 |
| 统一审计 | 部分具备 | 有日志和脱敏，但缺 Profile/Skill/工具调用版本化审计 |

### 17.5 关键接口冲突

Hub 当前 custom 分支调用：

```shell
CUSTOM_AGENT_BIN --message "<完整 Prompt>" --timeout 300
```

但 Windows 工程的有效调用实际依赖 bridge 生成的 context 文件：

```shell
python custom_agent_entry.py \
  --message "[context-file]" \
  --timeout 300 \
  --context-file <path>
```

`packages/custom-agent/src/claw_custom_agent/cli.py` 在没有 `--context-file` 时不会使用 `--message` 驱动 Provider，而是返回错误。因此：

- 把该工程入口直接配置到 Hub 的 `CUSTOM_AGENT_BIN`，当前一定不能工作。
- 完整 Windows Worker 仍可以通过内置 `CustomAgentBridge` 工作。
- 若决定长期采用完整 Worker 模式，应该让 Windows sidecar 与 Hub sidecar 的 Profile、Workflow 和 task_context 行为持续收敛。
- 若决定采用统一 `CUSTOM_AGENT_BIN` 模式，则必须新增 standalone runtime，并让结果规范化、Skill、MCP 和审计都落在 custom-agent 内。

### 17.6 改造优先级

#### P0：先保证接入和结果可信

1. **明确唯一主部署路径**
   - 近期建议采用完整 Windows Worker 模式。
   - 文档、安装配置和联调脚本必须明确，不再混用 `AGENT_TYPE=pi + CUSTOM_AGENT_ENTRY` 与 `AGENT_TYPE=custom + CUSTOM_AGENT_BIN`。

2. **把 Hub Profile 注入最终 Prompt**
   - 修改 `src/sidecar_v2.py`，移植 Hub sidecar 的 `agent_profile_lines()` 逻辑。
   - 验证 Pi 实际收到工位、Profile 名称/版本、职责、工作规范和 required Skills。

3. **禁止非 JSON Workflow 结果默认 passed**
   - 修改 `src/agent_protocol.py`。
   - 空输出、纯自然语言、JSON 解析失败应返回 `failed` 或明确的 `blocked`，不能默认为 `passed`。

4. **如果必须支持 `CUSTOM_AGENT_BIN`，新增 standalone 模式**
   - 修改 `packages/custom-agent/src/claw_custom_agent/cli.py`。
   - 新增 `runtime/standalone.py` 或等价实现。
   - 无 `--context-file` 时，必须用 `--message` 构造最小执行上下文并真实调用 Provider。

P0 自动化测试至少覆盖：

- `custom-agent --message "hello" --timeout 30`。
- `custom-agent agent --message "你好" --timeout 30`。
- Profile 内容确实进入 Provider 收到的 Prompt。
- 空输出、普通文本和损坏 JSON 都不会变成 `passed`。
- 超时后没有 Pi 子孙进程残留。

#### P1：完成首个业务闭环

1. 在 `packages/custom-agent/src/claw_custom_agent/skills/` 增加 Skill Loader、缓存、版本校验和 Hub 拉取。
2. 增加统一 Hub API 客户端，覆盖需求、用例、课题、Workflow 和 `/ops/verify`。
3. 增加 TAPD MCP 客户端，支持查询需求/迭代、评论和后续 Bug 操作。
4. 增加 Workflow JSON schema 校验和结果规范化。
5. 增加 Agent Runtime 级 Progress 上报。
6. 如果走直调模式，提供稳定的 `.cmd`、`.exe` 或其他可被 `CUSTOM_AGENT_BIN` 调用的启动包装。

#### P2：跨平台和团队化

- Linux 安装脚本、systemd、非 root 运行和目录授权。
- Hermes/OpenAI-compatible 等多 Provider。
- Profile、Skill、工具调用和证据 ID 的统一审计。
- 并发隔离、限流、取消、指标和告警。
- 收敛 Hub sidecar 与 Windows sidecar 的重复实现，避免长期协议漂移。

### 17.7 当前建议的联调路径

第一阶段按以下链路联调：

```text
Hub
  → claw-worker-windows-v2 内置 sidecar
  → CustomAgentBridge
  → custom-agent
  → Pi
```

完成 P0 后，再用 `agent_team_req_to_case_loop` 做真实迭代验收。验收时必须确认：

1. Pi 收到的 Prompt 中包含正确的 Profile key/version 和岗位约束。
2. required Skills 不只是名字，实际加载了对应 `SKILL.md`。
3. Workflow 普通文本、空结果和异常不会被判为 `passed`。
4. 需求评审写入后通过 Hub API 或 `/ops/verify` 回读。
5. 结果包含可复查的 Hub/TAPD 资源 ID。
6. Windows 超时或取消后不存在残留 Pi 进程。
7. 替换同岗位 Agent 后，结果 JSON 结构和门禁语义保持一致。

## 18. Hub Skill 下发端点契约（2026-08-02 已实现）

### 18.1 Loader 入口

新 Loader 不再自行组合旧 `/skills/{id}/raw|files|pack` 接口，统一从 Agent 专用 Manifest 开始：

```http
GET /api/v1/openclaws/{claw_id}/skill-manifest
Authorization: Bearer <CLAW_TOKEN>
```

需要合并任务级 required Skills 时：

```http
GET /api/v1/openclaws/{claw_id}/skill-manifest?ref_type=workflow_step&ref_id=501
Authorization: Bearer <CLAW_TOKEN>
```

合法 `ref_type`：

```text
todo / agent_task / workflow_step
```

`ref_type` 与 `ref_id` 必须同时提供。Hub 会重新读取任务并确认它属于当前 Agent；不能借其他 Agent 的任务获得 Skill 下载权限。

### 18.2 授权来源

Manifest 合并三种来源：

1. `assigned`：已通过 `openclaw_skills` 显式分配并启用。
2. `profile`：当前 active Profile 的 `required_skills`，无需另行分配。
3. `task_context`：指定任务上下文重新计算出的 `required_skills`，无需另行分配。

Profile/task_context 自动授权仍受以下约束：

- Skill 必须存在。
- `review_status` 必须为 `approved`。
- Skill 不能被软删除。
- `applicable_projects` 为空时视为全局；非空时必须包含当前 Agent 的 `project_id`。
- 私有 Skill 只能由 `owner_claw_id` 对应的 Agent 获取。

客户端不得提交任意 Skill 名称让 Hub 下载；required Skills 必须由 Hub 已保存的 Profile 或任务上下文推导。

### 18.3 Manifest 响应

```json
{
  "manifest_version": 1,
  "claw_id": 27,
  "generated_at": "2026-08-02T17:00:00+00:00",
  "task_ref": {
    "ref_type": "workflow_step",
    "ref_id": 501
  },
  "skills": [
    {
      "id": 143,
      "name": "workflow-manager",
      "display_name": "Workflow 管理",
      "content_version": "2026-08-02T10:00:00",
      "sha256": "bundle-sha256",
      "sources": ["assigned", "profile", "task_context"],
      "files": [
        {
          "path": "SKILL.md",
          "size": 12345,
          "sha256": "file-sha256",
          "updated_at": "2026-08-02T10:00:00",
          "download_url": "/api/v1/openclaws/27/skills/143/files/SKILL.md?ref_type=workflow_step&ref_id=501"
        }
      ],
      "pack_url": "/api/v1/openclaws/27/skills/143/pack?ref_type=workflow_step&ref_id=501"
    }
  ],
  "missing_skills": [
    {
      "name": "requirement-analysis",
      "sources": ["profile"],
      "reason": "not_found"
    }
  ]
}
```

`missing_skills.reason`：

```text
not_found
not_approved
deleted
project_forbidden
private_forbidden
```

Manifest 返回 200 并包含 `missing_skills` 时，Loader 必须停止认知任务并返回 Workflow `blocked`，不能忽略缺失项继续执行。

### 18.4 Agent 专用下载

单文件：

```http
GET /api/v1/openclaws/{claw_id}/skills/{skill_id}/files/{filename}
Authorization: Bearer <CLAW_TOKEN>
```

完整 ZIP：

```http
GET /api/v1/openclaws/{claw_id}/skills/{skill_id}/pack
Authorization: Bearer <CLAW_TOKEN>
```

如果 Manifest URL 带 `ref_type/ref_id`，下载时必须保留这些参数。服务端会再次计算授权；Manifest 中出现过的 URL 不是永久授权凭证。

下载响应：

```text
ETag: "<sha256>"
X-Skill-Content-Version: <content_version>
Cache-Control: private, max-age=60
```

Loader 应把本地 SHA256 作为 `If-None-Match`：

```http
If-None-Match: "<sha256>"
```

Hub 返回 `304 Not Modified` 时复用本地缓存；返回 200 时必须先写临时目录、校验每个文件和 Bundle SHA256，再原子替换旧目录。

### 18.5 文档包兼容与安全

- Hub 以 `SkillFile` 为文档包来源。
- 旧 Skill 没有 `SkillFile("SKILL.md")` 时，自动用 `Skill.template_content` 合成主文件。
- 文件路径统一为 `/`。
- Manifest 中的文件 URL 已按路径分段编码，可支持中文和空格。
- 绝对路径、Windows 盘符、`..` 和规范化后重复路径会被拒绝。
- Manifest 不返回正文，避免配置响应膨胀。
- ZIP 内统一放在 `<skill-name>/` 目录下。

### 18.6 Loader 推荐流程

```text
读取 Prompt 中 ref_type/ref_id
→ GET skill-manifest
→ 检查 missing_skills（非空则 blocked）
→ 对比本地 Bundle SHA256
→ 需要更新时下载 pack 或逐文件下载
→ 校验 SHA256
→ 临时目录解压
→ 路径安全检查
→ 原子替换 Skill 缓存
→ 完整读取 required Skill 的 SKILL.md
→ 按 Skill 指示按需读取 references/scripts
→ 执行任务并记录实际 Skill SHA256/content_version
```

Hub 实现位置：

- `web/app/services/skill_delivery.py`
- `web/app/api/openclaws.py`
- `tests/test_skill_delivery.py`
- `tests/test_skill_delivery_api.py`

旧 `/api/v1/skills/{id}/raw|files|pack` 目前为兼容保留，不是新定制 Agent Loader 的正式契约。

### 18.7 线上部署状态

2026-08-02 已部署到 Hub 生产环境：

```text
主机：9.134.11.169:36000
应用：/opt/openclaw-web
服务：openclaw-web
端口：18800
```

本次线上新增：

- `app/services/skill_delivery.py`
- `app/api/openclaws.py` 中的 Manifest、单文件下载和 ZIP 下载路由
- `app/models.py` 中的 `AgentProfile/AgentPost/AgentPostAssignment`
- `agent_profiles/agent_posts/agent_post_assignments` 三张表

线上 MariaDB 版本不支持原生 `JSON` 列，因此 Profile 的 `required_skills_json` 和 `contract_json` 使用 `LONGTEXT` 存储；SQLAlchemy `db.JSON` 仍负责 Python 对象序列化/反序列化。迁移脚本已同步改为旧 MariaDB 兼容写法。

线上验证结果：

```text
openclaw-web = active
HTTP / = 200
Manifest 鉴权请求 = 200
文件下载 = 200
If-None-Match 命中 = 304
ZIP 下载 = 200
Profile 自动授权 = 通过
task_context 自动授权 = 通过
动态测试数据回滚后残留 = 0
```

部署备份：

```text
/opt/openclaw-web/app/api/openclaws.py.bak_skill_delivery_20260802_221447
/opt/openclaw-web/app/models.py.bak_skill_delivery_20260802_221447
```

定制 Agent 工程可以从本章契约开始实现阶段 C Skill Loader，不再等待 Hub 端接口。

## 19. Windows Worker C2 Hub 写后验证契约审查（Commit `1b01025`）

### 19.1 审查结论

审查时间：2026-08-03。

结论：**Request Changes，C2 当前不能按生产契约验收。**

阻断问题：

1. 生产 Hub 尚未部署 `/requirements/items/{id}/review-verdict` 和 `RequirementReviewVerdict` 模型。
2. Worker 把 review verdict 的响应 `id` 当成 `AgentTask.id`，并使用 `resource_type=agent_task`，资源类型错误。
3. Workflow Step 写入值位于 GET Run 的 `steps[]`，Worker 当前只比较 Run 顶层字段。
4. `/api/v1/tasks/{id}` 不存在，Worker 为不存在的路径配置了读写验证策略。
5. Hub 没有实现这些路由的通用 `Idempotency-Key`；Worker 自行生成该 Header 不能保证请求只执行一次。
6. Worker 强制 `verify.expected == 完整写 body`，但 Hub 会重命名、归一化和忽略字段，契约不兼容。

审查基线：

```text
Worker 仓库：F:\Code\claw-worker-windows-v2
Worker Commit：1b0102561e8b952b45e6ab8369346cb651ffe09d
Hub 本地仓库：F:\Code\claw_team
Hub 生产环境：9.134.11.169:36000 / /opt/openclaw-web
```

### 19.2 生产路由支持矩阵

```text
POST /api/v1/requirements/items/{id}/review-verdict
  本地代码：支持
  生产线上：不支持
  生产探测：路由不存在；RequirementReviewVerdict 模型不存在

POST /api/v1/ops/verify
  生产线上：支持

GET/POST /api/v1/workflow-runs/{id}/...
  生产线上：支持主要 Run/Step 路由

POST /api/v1/topics
GET  /api/v1/topics/{id}
  生产线上：支持

GET/写 /api/v1/tasks/{id}
  生产线上：不支持

GET /api/v1/tasks/{ref_type}/{ref_id}/context
  生产线上：支持，只读
```

### 19.3 Requirement Review Verdict

本地目标接口：

```http
POST /api/v1/requirements/items/501/review-verdict
Authorization: Bearer <CLAW_TOKEN>
Content-Type: application/json
```

请求：

```json
{
  "review_key": "45",
  "verdict": "risk",
  "risk_level": "high",
  "testability": "unclear",
  "issues": [
    {
      "type": "ambiguity",
      "detail": "验收标准缺失"
    }
  ],
  "summary": "建议补充边界条件"
}
```

本地目标响应：新建为 HTTP 201，更新为 HTTP 200；顶层 `id` 是整数。

```json
{
  "id": 128,
  "requirement_item_id": 501,
  "iteration_id": 12,
  "review_key": "45",
  "verdict": "risk",
  "risk_level": "high",
  "testability": "unclear",
  "issues": [
    {
      "type": "ambiguity",
      "detail": "验收标准缺失"
    }
  ],
  "summary": "建议补充边界条件",
  "reviewer_name": "小安",
  "reviewer_claw_id": 11
}
```

正确资源 ID：

```text
响应 id = RequirementReviewVerdict.id
不是 RequirementItem.id
不是 AgentTask.id
```

因此 Worker 当前配置：

```text
resource_type = agent_task
resource_id = review-verdict 响应 id
```

是错误的。`/ops/verify` 会拿该 ID 查询 `AgentTask`，无法验证 Verdict。

Hub 后续必须二选一：

1. `/ops/verify` 新增 `requirement_review_verdict` 类型，并支持 Verdict 字段映射。
2. 新增单条 Verdict GET 接口，让 Worker 使用独立 GET 回读。

在其中一个方案上线前，Worker 必须把该写操作标记为 unsupported。

字段转换：

- `review_key` 优先取请求 `review_key/workflow_run_id/run_id`，最终截断为 80 字符。
- `workflow_run_id`、`run_id` 不会原名出现在响应中。
- `issues` 落库字段为 `issues_json`，响应再输出为 `issues`。
- 非法 `risk_level` 静默改为 `low`。
- 非法 `testability` 静默改为 `testable`。
- 未声明字段被忽略。

所以不能用完整原始请求 body 做通用 ORM 字段验证。

### 19.4 `/api/v1/ops/verify`

生产支持的 `resource_type`：

```text
agent_task
knowledge
test_report
todo
workflow_step
```

请求格式：

```http
POST /api/v1/ops/verify
Authorization: Bearer <CLAW_TOKEN>
Content-Type: application/json
```

```json
{
  "resource_type": "agent_task",
  "resource_id": 123,
  "expected": {
    "status": "reviewed"
  },
  "token": "operation-id"
}
```

这个格式只有在以下条件下才正确：

- `resource_id=123` 是真实 `AgentTask.id`。
- `expected` 中的键是 `AgentTask` 的真实 ORM 字段。
- `status=reviewed` 是实际落库值。

成功为 HTTP 200：

```json
{
  "verified": true,
  "actual": {
    "status": "reviewed"
  },
  "mismatches": [],
  "verification_id": 456
}
```

不匹配仍为 HTTP 200：

```json
{
  "verified": false,
  "actual": {
    "status": "running"
  },
  "mismatches": [
    {
      "field": "status",
      "expected": "reviewed",
      "actual": "running"
    }
  ],
  "verification_id": 457
}
```

其他失败：

```text
参数错误 / resource_type 不支持：HTTP 400
资源不存在：HTTP 404
```

`token`：

- 可选。
- 原样写入验证审计记录。
- 不参与字段比较。
- 不提供幂等或去重。

`expected`：

- Hub 支持验证字段子集。
- 不要求覆盖完整写入 body。
- 字段值经过 `None/bool/string` 宽松归一后比较。

### 19.5 Workflow Run 写后读取

支持：

```http
GET /api/v1/workflow-runs/{run_id}
```

各写接口 commit 后，GET 能读取最新 Run 和 Step 状态。但 Step 字段位于 `steps[]`，不在 Run 顶层。

进度写入：

```http
POST /api/v1/workflow-runs/61/steps/requirement_review/progress
Authorization: Bearer <CLAW_TOKEN>
Content-Type: application/json
```

```json
{
  "phase": "analyzing",
  "message": "已处理 12 条需求",
  "percent": 40
}
```

写响应：

```json
{
  "ok": true,
  "step": {
    "step_id": "requirement_review",
    "progress_phase": "analyzing",
    "progress_message": "已处理 12 条需求",
    "progress_percent": 40
  }
}
```

随后 GET Run：

```json
{
  "id": 61,
  "status": "running",
  "steps": [
    {
      "step_id": "requirement_review",
      "progress_phase": "analyzing",
      "progress_message": "已处理 12 条需求",
      "progress_percent": 40
    }
  ]
}
```

Worker 当前只调用：

```text
verify_body.get("phase")
verify_body.get("message")
verify_body.get("percent")
```

这些字段不存在于 Run 顶层，验证必然失败。

正确实现应：

1. 根据 URL 解析 `run_id` 和 `step_id`。
2. GET Run。
3. 在 `steps[]` 查找同 `step_id`。
4. 按路由映射字段，例如：

```text
phase   → progress_phase
message → progress_message
percent → progress_percent
```

`claim/heartbeat/progress/status/result/retry` 需要分别定义 canonical expected，不能共享“完整 body 对比 Run 顶层”策略。

### 19.6 Topics

生产支持：

```text
POST /api/v1/topics
GET  /api/v1/topics/{id}
```

创建响应顶层 `id` 是整数。

请求：

```json
{
  "title": "S26 用例评审",
  "content": "请评审登录模块",
  "board": "test_methods",
  "visibility": "public"
}
```

响应：

```json
{
  "id": 91,
  "title": "S26 用例评审",
  "content": "请评审登录模块",
  "board": "test_methods",
  "status": "open",
  "visibility": "public",
  "author_claw_id": 11,
  "author_name": "小安"
}
```

GET：

```http
GET /api/v1/topics/91
Authorization: Bearer <CLAW_TOKEN>
```

返回同一顶层 `id`。

字段注意：

- `project_id` 会转换为 `project_name`，GET 不返回原始 `project_id`。
- `visibility` 会归一化。
- `grants` 通过关联表保存，不是普通 Topic 顶层字段。
- 用例评审字段只在 `board=case_review` 时处理。
- 未声明字段会被忽略。

基础 `title/content/board/visibility` 可使用 GET 回读；复杂创建必须使用规范化后的字段子集。

### 19.7 Tasks

Worker 当前假设的路径不存在：

```text
GET /api/v1/tasks/{id}
POST/PUT/PATCH /api/v1/tasks/{id}
```

生产实际只有只读任务上下文：

```http
GET /api/v1/tasks/{ref_type}/{ref_id}/context
Authorization: Bearer <CLAW_TOKEN>
```

合法 `ref_type`：

```text
todo
agent_task
workflow_step
```

示例：

```http
GET /api/v1/tasks/workflow_step/883/context
```

```json
{
  "project": "RacingGO",
  "required_skills": [
    "requirement-analysis",
    "basic-operations-preflight"
  ],
  "primary_skill": "requirement-analysis",
  "ref_type": "workflow_step",
  "ref_id": 883
}
```

Worker 必须删除 `/api/v1/tasks/{id}` 的写验证策略。

### 19.8 幂等性

当前 Hub 对 review-verdict、Workflow、Topic、Task：

```text
通用 Idempotency-Key：不支持
相同 key + 相同 body：不保证只执行一次
相同 key + 不同 body：不会统一返回冲突
幂等记录保留期：不存在
```

`review-verdict` 使用 `(requirement_item_id, review_key)` 做业务 upsert，但这不等于通用 HTTP 幂等。

Worker 生成的 `Idempotency-Key` 当前会被 Hub 忽略。并且 Worker 的 key 材料不含 query/body，未来即使 Hub 支持，也可能让相同 operation ID 的不同请求发生碰撞。

在 Hub 实现幂等存储前：

- Worker 可以保留 `operation_id` 作为本地日志与重试关联 ID。
- 不能宣称请求只执行一次。
- 对非天然 upsert 的创建接口，自动重试可能产生重复资源。

### 19.9 `{status, body}` Envelope

Hub HTTP API 本身返回：

```text
HTTP status + 原始 JSON body
```

`{status, body}` 是 Windows Worker 本地 proxy 的内部 envelope，不是 Hub 原生响应。

Worker 可以继续保持该 envelope，但必须保证所有分支一致，包括：

- Hub HTTP 非 2xx。
- proxy 内部异常。
- 响应超过大小限制。
- 写成功但验证失败。

当前 `hub_proxy.py` 的内部异常和超限截断分支会返回没有 `body` 的对象，与 `hub-api.ts` 和 Python Hub 客户端的解析要求冲突，需要修复。

### 19.10 DELETE

Hub 接受 Worker 暂时禁用 DELETE。

这是安全的保守策略。虽然目前：

- Workflow Run 删除后 GET 返回 404。
- Topic 软删除后 GET 返回 404。

但在统一“资源不存在”验证、权限和审计契约完成前，可以保持禁用。

### 19.11 Worker 必须修改

1. 删除 `/api/v1/tasks/{id}` 策略。
2. review-verdict 不得使用 `resource_type=agent_task`。
3. Verdict API 和专用验证能力上线前，将 review-verdict 写操作标记为 unsupported。
4. Workflow Step 使用 `steps[]` 中指定 step 的字段验证。
5. `expected` 改为每条路由规范化后的 canonical 字段子集。
6. 不得声明 Hub 已支持 `Idempotency-Key`。
7. proxy 所有返回分支保持 `{status, body}`。
8. Idempotency-Key 材料至少纳入 canonical query/body hash，供未来 Hub 幂等契约使用。
9. 补充以下测试：
   - review-verdict 成功、字段归一和错误资源类型。
   - `/ops/verify` 200 true、200 false、400、404。
   - Topics 创建后 GET。
   - Workflow Step progress/result 后从 `steps[]` 回读。
   - 不存在的 `/tasks/{id}` 被拒绝。
   - proxy 异常与截断仍保持 envelope。

### 19.12 Hub 团队最终答复

```text
review-verdict：
  生产暂不支持；本地目标响应顶层 id 为 RequirementReviewVerdict.id。
  resource_type=agent_task 错误。

ops/verify：
  生产支持。
  agent_task 只可验证真实 AgentTask.id。
  verified=false 为 HTTP 200；参数错误 400；资源不存在 404。

workflow-runs：
  生产支持写后 GET。
  Step 写入值在 steps[]，不能比较 Run 顶层。

topics：
  生产支持 POST 和 GET。
  创建响应顶层 id 为整数 Topic.id。

tasks：
  /api/v1/tasks/{id} 不支持。
  只支持 /api/v1/tasks/{ref_type}/{ref_id}/context GET。

Idempotency-Key：
  当前不支持，无冲突语义，无保留期。

DELETE：
  接受 Worker 暂时禁用。

C2 审查：
  Request Changes。
```

## 20. Windows Worker C2 修订方案复审（2026-08-03）

本章复审 Worker 在 `1b01025` 之后的当前未提交修改，并以 2026-08-03 Hub 生产环境实时 route map、模型和函数源码为准。第 19 章记录首次审查，本章为最新结论。

### 20.1 总结论

```text
review-verdict fail closed：通过
Workflow progress 回读：通过，但只能确认当前字段存在，尚无版本化稳定承诺
tasks context 限制：通过
{status, body} envelope：部分通过
Idempotency-Key：可作本地关联，不提供 exactly-once
长任务 Hub 模式：不通过

最终结论：Request Changes
```

当前代码可以保留在禁用/灰度状态，但开放长任务前必须修正路径、任务标识、结果结构和服务循环。review-verdict 在 Hub 正式契约上线前继续 fail closed。

### 20.2 review-verdict

Worker 已做正确修订：

- `packages/custom-agent/src/claw_custom_agent/tools/hub_api.py` 将 `/requirements/items/{id}/review-verdict` 列入 `_UNSUPPORTED_WRITES`。
- `src/hub_proxy.py` 删除了错误的 `resource_type=agent_task` 写后验证策略。
- API 未形成正式回读契约前，请求会在本地拒绝，不会先写 Hub 再验证失败。

生产实时事实：

```text
OPTIONS /api/v1/requirements/items/1/review-verdict → 404
生产 RequirementReviewVerdict 模型 → 不存在
```

本地目标实现仍为：

```text
POST/PUT /api/v1/requirements/items/{item_id}/review-verdict
响应 id = RequirementReviewVerdict.id
```

正式契约上线前还需要 Hub 决定并实现：

```text
resource_type = requirement_review_verdict
```

建议 `/ops/verify` 按 API canonical 字段回读，而不是暴露 ORM 内部字段：

```text
review_key
verdict
risk_level
testability
issues
summary
```

当前 `/ops/verify` 不支持 `requirement_review_verdict`，也没有按 Verdict ID 查询的单条 GET。迭代级列表 GET 只能间接查询，不能替代资源 ID 回读。因此 Worker 现在 fail closed 的处理应继续保留。

### 20.3 Workflow progress

Worker 已改为：

```text
写 POST progress
→ GET /api/v1/workflow-runs/{run_id}
→ 在 steps[] 中按 step_id 定位
→ phase   对比 progress_phase
→ message 对比 progress_message
→ percent 对比 progress_percent
→ progress 对比 progress_json
```

该映射与 Hub 当前本地、生产实现一致。

POST 支持：

```json
{
  "worker_id": "worker-1",
  "phase": "download",
  "message": "downloading",
  "percent": 40,
  "progress": {},
  "heartbeat": true
}
```

其中：

- `reporter` 可作为 `worker_id` 的兼容字段。
- `detail` 可作为 `progress` 的兼容字段。
- `percent` 转成整数并限制在 `0–100`；非法值落为 `null`。
- `heartbeat=true` 会同时刷新心跳。

GET Run 的核心字段当前为：

```text
steps[].status
steps[].display_state
steps[].claimed_by / claimed_at
steps[].heartbeat_at / heartbeat_by / heartbeat_count
steps[].missed_heartbeat_count
steps[].health_status / health_checked_at
steps[].progress_at / progress_by
steps[].progress_phase / progress_message
steps[].progress_percent / progress_json
steps[].summary
steps[].metrics / metrics_json
steps[].evidence / evidence_json
steps[].logs / logs_json
steps[].blocker / blocker_json
steps[].outputs_json
steps[].gate_result / gate_result_json
```

注意：

- `steps[].outputs` 是步骤定义中声明的输出名，不是执行结果。
- 实际执行结果是 `steps[].outputs_json`。
- 这些字段当前存在，但 Hub 没有 OpenAPI、协议版本和兼容性承诺，因此只能确认「当前可用」，不能宣称长期稳定。
- 本地与生产已经出现字段漂移：本地有 `target_post`，生产没有。

Hub 后续应把上述字段纳入版本化 Workflow Worker 契约。

### 20.4 `/api/v1/tasks/{id}`

Worker 已删除通用 tasks 写策略，只允许：

```text
GET /api/v1/tasks/{ref_type}/{ref_id}/context
```

生产探测：

```text
OPTIONS /api/v1/tasks/workflow_step/1/context → 200
OPTIONS /api/v1/tasks/1                       → 404
```

确认不存在以下通用或隐藏入口：

```text
GET/POST/PUT/PATCH /api/v1/tasks/{id}
```

`ref_type` 只允许：

```text
todo
agent_task
workflow_step
```

其他 `/test-plans/.../tasks/...`、`/openclaws/.../tasks/...` 是独立业务接口，不是通用 Task 写入口。

另需注意：`GET /workflow-runs/{id}` 和 `GET /workflow-runs/worker/tasks` 会调用健康扫描并 commit，可能触发重试或阻断，并非严格无副作用的纯 GET。

### 20.5 `{status, body}` envelope

Hub 原生响应契约是：

```text
HTTP status + Hub response body
```

`{status, body}` 是 Worker proxy 自己的工具契约，不是 Hub 原生 envelope。

Worker 已修复：

- 上游调用异常返回 `{status: 0, body: {error: ...}}`。
- 响应超限返回 `{status, body: {error, truncated, bytes}}`。
- 正常 Hub JSON 响应保持 `{status, body}`。

但以下 proxy 本地分支仍返回裸 `{error}`：

```text
404：非 /hub-api 路径
401：proxy token 错误
400：请求大小错误
400：参数或契约校验错误
```

因此「所有 JSON 分支统一」只部分成立。要么把这些分支也统一为 `{status, body}`，要么明确文档规定 envelope 只覆盖「已鉴权、请求格式合法的 `/hub-api` 调用」。

Hub 不能保证所有上游错误都是 JSON。生产实测未注册路径返回 `404 text/html`；网关 502/504、空响应、HTML 错误页也可能出现。Worker 必须自行把 HTML、文本、空响应和解析失败归一为合法 envelope，最好保留受限长度的原始文本和 content-type，避免排障信息完全丢失。

### 20.6 Idempotency-Key

Worker 新 key 材料包含：

```text
method
path
operation_id
query SHA256
body SHA256
proxy token
```

可用于单次 proxy 生命周期内的重试关联，但有两个限制：

1. Hub 的 review-verdict、Workflow、Topic、Task 路由不读取该 Header，不提供 exactly-once、重放缓存、异 body 冲突或保留期。
2. key 包含临时 proxy token，proxy 重启后同一业务请求会产生不同 key，不能作为跨进程 exactly-once 标识。

另外，当前 JSON 哈希未使用稳定键排序；键顺序不同但语义相同的对象可能产生不同 key。未来若对接 Hub 幂等，应使用 canonical JSON（至少 `sort_keys=True`）并重新确定是否保留 proxy token。

专用 Memory mutations 的 180 天幂等机制只适用于 Memory API，不能外推到这些接口。

### 20.7 长任务生产契约

生产 Hub 已部署的真实链路：

```text
GET  /api/v1/workflow-runs/worker/tasks
POST /api/v1/workflow-runs/{run_id}/steps/{step_id}/claim
POST /api/v1/workflow-runs/{run_id}/steps/{step_id}/heartbeat
POST /api/v1/workflow-runs/{run_id}/steps/{step_id}/progress
POST /api/v1/workflow-runs/{run_id}/steps/{step_id}/result
```

线上路由探测均为 200。

Worker 当前新增客户端却使用：

```text
POST /api/v1/worker/tasks/claim
POST /api/v1/worker/tasks/{task_id}/heartbeat
POST /api/v1/worker/tasks/{task_id}/progress
POST /api/v1/worker/tasks/{task_id}/result
```

生产 `/api/v1/worker/tasks/claim` 为 404。两套契约的根本差异：

```text
Hub 任务标识 = run_id + step_id
Worker 当前假设 = 整数 task_id
```

Worker 必须先 GET worker tasks，取得 `run_id/step_id`，再调用对应 Step 路由。

结果上报也不一致。Worker 当前发送：

```json
{
  "result": {
    "status": "passed",
    "summary": "ok"
  }
}
```

Hub 要求顶层字段：

```json
{
  "status": "passed",
  "summary": "ok",
  "metrics": {},
  "outputs": {},
  "evidence": {},
  "logs": {},
  "blocker": {}
}
```

Hub result 返回完整 Run，不是 `{ok, step}`。

当前 `src/job_service/__main__.py` 的 `main()` 直接返回 0；`HubWorkflowClient` 只有接口封装，没有 claim、执行、定时 heartbeat/progress、result、恢复和退出的服务循环。Windows 服务启动后会立即退出，所以当前长任务只完成骨架，尚未形成可运行实现。

### 20.8 长任务超时与可靠性限制

当前 Hub 行为：

```text
claim 默认 lease_seconds：180 秒
心跳健康周期：30 秒
超过 30 秒：stale
超过 90 秒：
  auto_block_on_heartbeat_loss=true → blocked
  否则保持 stale
progress 超时参考：300 秒
无心跳/进度硬超时：900 秒
硬超时后：按 retry_max 重派，用尽后 blocked
```

当前 claim 只是软锁：

- `lease_seconds` 由请求方提交，Hub 没有固定服务端租约。
- heartbeat 不更新已有 `claimed_at`。
- heartbeat 不校验 `worker_id == claimed_by`。
- result 不校验 claim owner，只校验该 Step 是否属于同一个 OpenClaw。
- 竞争 worker 可以用自己提交的 `lease_seconds` 判断旧 claim 是否过期。

因此当前契约不能保证排他执行。开放长任务前，Hub 应明确并实现：

1. 服务端固定租约或持久化 `lease_deadline`。
2. heartbeat 自动续租。
3. heartbeat/result 校验 claim owner。
4. result 严格校验状态枚举。
5. 重复 result 的幂等或 artifact 去重规则。

当前 Hub 对未知 result status 可能最终兜底为 `passed`，必须改为 fail closed，避免无效结果被包装为成功。

### 20.9 复审后的必改项

Worker：

1. 改用 `/workflow-runs/worker/tasks` 和按 `run_id/step_id` 的四个 Step 路由。
2. result 改为 Hub 要求的顶层结构。
3. 实现真正持续运行的 Job Service 循环。
4. 补齐 proxy 404/401/400 的 envelope，或明确工具契约边界。
5. Idempotency-Key 使用 canonical query/body 序列化，并明确生命周期。
6. 增加缺失 step、选错 step、字段不匹配和长任务端到端测试。

Hub：

1. 上线 review-verdict 前补 `requirement_review_verdict` 验证或单条 GET。
2. 将 Workflow Worker 字段形成版本化正式契约。
3. 修正长任务租约、owner 校验和 heartbeat 续租。
4. result status 改为严格枚举并 fail closed。
5. 若需要 exactly-once，必须实现服务端幂等存储和冲突语义。

### 20.10 验证结果

```text
Worker 契约相关测试：65 passed，56 subtests passed
Hub 契约相关测试：47 passed
```

现有 Worker 长任务测试断言的是生产不存在的 `/api/v1/worker/tasks/...`，所以测试通过不能证明生产契约正确。长任务在完成本章必改项前不得开放。

