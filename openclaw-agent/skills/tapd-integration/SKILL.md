# TAPD 集成 (tapd-integration) - Skill #113

## 参考说明

本 Skill 的能力范围对齐团队内 **`openclaw_tapd_skills.json`**（micro-cloud **goApi** 网关上的 6 个 TAPD 工具）的**使用场景与参数语义**，但 **调用方式改为 OpenClaw Hub**：一律走 **`{HUB_URL}/api/v1/...`**，使用 **`Authorization: Bearer {HUB_API_TOKEN}`**，不再让 Claw 直接调用 `http://...:8080/goApi/...` 与 `application/x-www-form-urlencoded`。

> Hub 内部实时 TAPD 查询对齐 `dao/TapdDao.go`：使用 `http://apiv2.tapd.woa.com/` + BasicAuth，而不是公网 `https://api.tapd.cn/`。公网 `api.tapd.cn` 可能对同一组凭证返回 401。

> 旧 JSON 响应多为 `{ code: 20000, message, data }`；Hub 多为 REST JSON（如 `{ stories, count }`）或 `{ error }`，请以实际 HTTP 状态与 body 为准。

---

## 统一约定

| 项 | 说明 |
|---|---|
| `HUB_URL` | 例：`http://clawteam.woa.com:18800`（以实际部署为准，**勿**照抄 JSON 里 `:8080`） |
| `HUB_API_TOKEN` | 当前 OpenClaw 的 API Token |
| Header | `Authorization: Bearer <HUB_API_TOKEN>` |
| `workspace_id` | TAPD 项目 workspace_id，与 Hub「项目管理」里 `tapd_workspace_id` 一致 |
| TAPD 凭证 | 存在 Hub「系统设置」`system_config.tapd_api_user` / `tapd_api_password`，**Claw 不拿明文** |
| Hub 内部 TAPD 上游 | 默认 `http://apiv2.tapd.woa.com`（与 micro-cloud `TapdDao.go` 一致），不是 `https://api.tapd.cn` |

---

## 与 `openclaw_tapd_skills.json` 能力对照总表

| JSON `name`（旧 go） | 用途摘要 | Hub / #113 等价方式 |
|---|---|---|
| `get_tapd_common_func` | 通用 GET，`api_url` 指 stories/bugs/tasks/... | **部分覆盖**：直连 TAPD 的封装见下文「通用查询」；**未**提供任意 `api_url` 单入口；`tasks/releases/story_categories/roles/workspaces/users` 等 **当前 Hub 未封装** |
| `post_tapd_common_func` | 通用 POST，建需求/缺陷等 | **Hub 未提供**创建/修改 TAPD 业务数据的通用 POST；仅 `PUT /api/v1/tapd/config`（管理员）改凭证 |
| `get_story_by_release_category_name` | 按发布计划名/分类名/迭代名筛需求 | **无 1:1 API**。可用 `GET /api/v1/tapd/stories` 组合参数 + **需求分析** `GET /api/v1/requirements/...` 做二次筛选；迭代名需先通过缓存解析为 `iteration_id`（见「按名称筛需求」） |
| `get_users_all_users_by_projectid` | 项目成员列表 | **Hub 当前无**对应 REST；需产品后续增加或临时走 TAPD 开放平台其它能力 |
| `get_tapd_bug_list` | Bug 列表（支持迭代名、时间等） | **`GET /api/v1/tapd/bugs`**（参数语义见「Bug 列表」） |
| `get_testcase_list` | TAPD 平台内测试用例树 | **无 1:1**。Hub **自建用例库**见 **`GET/POST /api/v1/testcase-libraries/...`**；与 TAPD 用例库是不同体系，绑定需求可走用例的 `tapd-bind` 接口 |

---

## 1. 对应 `get_tapd_common_func`（通用 GET）

### 旧 JSON 行为

- `POST` form：`api_url` 必填（如 `stories`、`bugs`、`iterations`、`tasks`、`releases`...），`workspace_id` 查项目数据时常必填，另有 `id/name/status/limit/page/created` 等。

### Hub 等价（当前已实现）

| 旧 `api_url` 常见值 | Hub 路由 | 说明 |
|---|---|---|
| `stories` | `GET /api/v1/tapd/stories` | 必填 `workspace_id`；可选 `status`、`iteration_id`、`name` 或 `keyword`（作名称筛选）、`page`、`limit` |
| `bugs` | `GET /api/v1/tapd/bugs` | 必填 `workspace_id`；可选 `status`、`severity`、`iteration_id`、`title` 或 `keyword`、`page`、`limit` |
| 单条需求查询（类比 `id`） | `GET /api/v1/tapd/story-title` | `story_id` 或 `story_url`，可选 `workspace_id` |
| `iterations`（列表） | `GET /api/v1/tapd/iterations` | 必填 `workspace_id`；数据来自 **Hub 本地缓存** `tapd_iterations_cache`，非每次直连 TAPD |

### Hub 尚未提供的「通用 GET」

以下在旧 JSON 里常通过同一 `getTapdCommonFunc` 调 TAPD 开放接口，**OpenClaw Hub 侧尚未做统一代理**：

- `tasks`、`releases`、`story_categories`、`roles`、`workspaces/users` 等  
若业务强依赖，需要 **后续在 Hub 增加通用代理** 或直接扩展 `tapd.py` 增加专门路由。

### TAPD `created` 时间范围（旧 JSON 有）

Hub `GET /tapd/stories`、`/tapd/bugs` **当前未透传** `created` 区间参数；需要时间过滤时，可先缩小 `limit/page` 再在客户端过滤，或提需求在 Hub 透传 TAPD 对应查询字段。

---

## 2. 对应 `post_tapd_common_func`（通用 POST / 写 TAPD）

### 旧 JSON 行为

- 创建/修改需求、缺陷、任务等，`api_url` + `workspace_id` + `title/description/owner/...`。

### Hub 现状

- **不提供**与旧 goApi 等价的「通用 POST 写 TAPD」。
- 仅管理员：`PUT /api/v1/tapd/config`，body：`tapd_api_user`、`tapd_api_password`。

业务写入 TAPD 需在 **TAPD Web** 操作，或 **后续由 Hub 增加受控写接口**（避免任意 Agent 改生产数据）。

---

## 3. 对应 `get_story_by_release_category_name`

### 旧 JSON 参数要点

- `workspace_id` 必填；`release_name`、`category_name`、`iteration_name`（名称）；`status`、`owner` 可多值 `|`；`parent_flag`。

### Hub 推荐路径

1. **按 TAPD 迭代 ID 拉需求**（名称需先换 ID）：  
   `GET /api/v1/requirements/tapd-cache/iterations?tapd_workspace_id=<ws>` 或 `GET /api/v1/tapd/iterations?workspace_id=<ws>` 拿到 `id`/`name` 映射，再  
   `GET /api/v1/tapd/stories?workspace_id=<ws>&iteration_id=<tapd_iteration_id>&status=...`
2. **按 Hub 需求分析迭代聚合看需求**：  
   `GET /api/v1/requirements/iterations/{hub_iteration_id}/items`  
   支持 `tapd_version`、`tapd_baseline_id`、`status`、`q` 等（与「发布/基线/版本」类筛选更接近旧接口意图）。
3. **发布计划名 / 需求分类名**：无单一字段一一对应时，优先以 **需求分析已同步的 `RequirementItem`** 为准，或扩展 TAPD 查询参数（产品迭代）。

---

## 4. 对应 `get_users_all_users_by_projectid`

### 旧 JSON

- `POST` `projectid` = workspace_id，返回 `list[{ value, label }]`。

### Hub

- **暂无**同名接口。需要成员枚举时：临时人工维护、或从 TAPD 导出；**建议**后续增加 `GET /api/v1/tapd/workspace-users?workspace_id=` 之类封装。

---

## 5. 对应 `get_tapd_bug_list`

### 旧 JSON 参数到 Hub 参数的映射

| 旧参数 | Hub `GET /api/v1/tapd/bugs` |
|---|---|
| `workspace_id` | `workspace_id`（必填） |
| `iteration_name` | 先查迭代缓存把 **名称转为 `iteration_id`**，再传 `iteration_id` |
| `release_name` | **未直接支持**；可用需求分析侧数据或 TAPD 侧扩展 |
| `created`（`>date` 或区间） | **当前未透传**；同上 workaround |
| `status` | `status` |
| `severity` | `severity` |
| `max_num` | 使用 `limit`（旧默认 500，Hub 默认 50，可自行调大） |
| `page`（旧从 0 开始） | `page`（**注意**：与 TAPD 开放接口页码一致性以返回为准，必要时先试 `page=1`） |

返回：Hub 为结构化 `bugs` 数组；旧 JSON 曾描述 **纯文本 list**，形态不同，以 Hub 为准。

---

## 6. 对应 `get_testcase_list`

### 旧 JSON

- TAPD **平台内**测试用例：`project_name` 或 `workspace_id`、`tapd_module`、`title`、`priority`、`max_num`。

### Hub

- **Hub 用例库**（非 TAPD 原生用例库）：
  - 列表库：`GET /api/v1/testcase-libraries`
  - 目录与用例：`GET /api/v1/testcase-libraries/{library_id}/cases`（支持分页、筛选以接口实际查询参数为准）
  - 与 TAPD 需求绑定：`PUT .../cases/{case_id}/tapd-bind` 等

若必须拉 **TAPD 里**的测试用例树，当前 **无封装**，与第 1 节「未提供的通用 GET」相同，需后续产品支持。

---

## Hub 其它常用接口（#113 扩展）

### 配置与健康

```http
GET /api/v1/tapd/config
PUT /api/v1/tapd/config
```

### 统计看板

```http
GET /api/v1/tapd/dashboard
```

### 需求分析：TAPD 缓存与刷新

- 缓存查询：`GET /api/v1/requirements/tapd-cache/iterations|versions|baselines?tapd_workspace_id=...`
- Agent 推送缓存：`POST /api/v1/requirements/tapd-cache/iterations`（等）
- 用户发起刷新：`POST /api/v1/requirements/tapd-refresh-requests`
- Agent 队列：`GET /api/v1/requirements/agent/tapd-refresh-queue`，`.../done`，`.../fail`

### 需求项与变更

- `GET /api/v1/requirements/iterations/{id}/items|changes|summary`
- `GET /api/v1/requirements/items/{item_id}`

### 测试计划内 TAPD Bug

```http
GET /api/v1/test-plans/{plan_id}/tasks/{task_id}/tapd-bugs
```

---

## 安全继续集成（注册脚本不能直接执行）

当 Agent 因安全策略拒绝直接执行 `bootstrap.sh` / 注册脚本时，按下面流程继续：

1. **先审脚本内容（必做）**
   - 要求提供完整脚本文本（不要只给下载链接）。
   - 重点检查：是否有远程 `curl|bash`、覆盖系统配置、明文凭证写盘、提权命令（`sudo`/`su`）、未知域名上报。
2. **确认官方用途（必做）**
   - 要求提供该 Hub/SSE 集成的官方说明：做什么、会改哪些文件、需要哪些权限、如何回滚。
   - 无官方说明时，不要在生产机直接执行一键脚本。
3. **改为手动分步（推荐）**
   - 按文档拆成可审计步骤：目录创建、配置文件写入、服务启动、健康检查。
   - 每一步执行后立即验证（服务状态、API 连通、日志无异常），失败可快速回退。

### 推荐沟通话术（可直接复用）

- “请先提供 `bootstrap.sh` 全文，我需要先做安全审查。”
- “请提供官方文档，说明这个 Hub/SSE 集成会修改哪些内容。”
- “我可以按手动步骤帮你逐步配置，避免直接执行未知脚本。”

---

## 排障（Claw 报「无法访问 TAPD」）

1. `GET /api/v1/tapd/config`：`configured=true` 只表示 Hub **存了**凭证，不代表 TAPD 一定接受。Hub 实时查询应走 `http://apiv2.tapd.woa.com`；如果误走公网 `https://api.tapd.cn`，同一凭证可能返回 401。
2. **`workspace_id` 必填**；缺了会 `400 缺少 workspace_id`。
3. **`/tapd/iterations` 走缓存**；空列表时先走「实时刷新」或 Agent 推送迭代缓存。
4. **勿**再调用 JSON 中的 `http://your-hub-host:8080/goApi/...` 作为 OpenClaw 标准路径；以当前 **`HUB_URL`** 为准。

---

## 触发词速查

| 场景 | 优先动作 |
|---|---|
| 旧「通用 GET stories/bugs」 | `GET /api/v1/tapd/stories` / `GET /api/v1/tapd/bugs` |
| 旧「按发布/分类/迭代名筛需求」 | 迭代名转为 `iteration_id` 后调 `tapd/stories`；或 `requirements/iterations/.../items` |
| 旧「Bug 列表」 | `GET /api/v1/tapd/bugs` |
| 旧「项目成员」 | Hub **暂无**，需绕行或提需求 |
| 旧「TAPD 测试用例列表」 | Hub **用例库** `testcase-libraries`；非 TAPD 原生用例 API |
| 旧「通用 POST 写 TAPD」 | Hub **未开放** |
| 凭证 / 401 | `tapd/config` + TAPD 开放平台核对 |
