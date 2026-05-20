# Hub Agent 模板库功能需求

> 提出时间：2026-05-09  
> 提出背景：支持新 Agent 快速复刻成熟角色配置  
> 可见范围：仅超级管理员可见（Hub 管理端）

---

## 一、目标

新增「Agent 模板库」模块，将一个 Agent 在 Hub 上的可复用能力打包为模板，实现一键复刻：

- 基础身份信息（名字、角色、主要职责）
- 已安装 Skills 组合
- 工作规范 Rules 组合
- 定时任务配置
- 模板专属文件目录（可被说明文本引用）

目标是让新 Agent 能快速继承“成熟 Agent 角色”，减少手工重复配置。

---

## 二、核心原则

1. **模板由 Agent 发起提交**：模板可由 Agent 自己提交（或更新提交）。
2. **超级管理员管控**：模块入口、审核、发布、删除、应用模板，仅 `super_admin` 可见/可操作。
3. **模板与文件解耦**：模板结构化字段存 DB，附件/说明文件存模板独立目录。
4. **可引用文件**：在职责说明、规范说明等文本中支持引用模板目录中的文件。
5. **可追踪可回滚**：模板变更保留版本与审计记录，支持历史版本恢复。

---

## 三、权限与状态机

### 1) 页面可见性

- `super_admin`：可见并可管理 Agent 模板库全部功能。
- 其他角色（admin、普通用户、普通 Agent）：不显示模板库菜单，不可调用模板管理 API。

### 2) 提交与审核状态

建议模板状态：

- `draft`：草稿（Agent 或管理员编辑中）
- `pending_review`：待审核（Agent 提交后）
- `approved`：已发布可用
- `rejected`：审核拒绝（需修改后再提）
- `archived`：归档（不再推荐新建使用）

建议流程：

1. Agent 创建/更新模板 -> `pending_review`
2. 超级管理员审核 -> `approved` 或 `rejected`
3. 发布后可继续产生新版本（新版本再次进入 `pending_review`）

---

## 四、页面结构（页签）

模板详情页采用页签展示，按“简单说明 + 文件列表”风格：

1. **基础信息**
   - 模板名
   - 角色（如测试经理/模块负责人/专项测试等）
   - 主要职责（简述）
2. **Skills**
   - 已安装技能清单（ID、名称、版本/更新时间）
3. **Rules**
   - 工作规范规则清单（ID、名称、作用域）
4. **定时任务**
   - 任务标题、执行频率、触发时间、启用状态
5. **文件列表**
   - 模板专属目录中的文件树
   - 文件上传/删除/重命名
   - 文件引用路径复制
6. **版本记录（建议）**
   - 提交人、提交时间、审核人、审核意见

---

## 五、数据模型建议

## 1) `agent_role_templates`（模板主表）

- `id`
- `template_key`（唯一标识）
- `name`（模板名称）
- `role_code`（角色编码）
- `main_responsibility`（主要职责文本，支持文件引用）
- `owner_claw_id`（来源 Agent，可空）
- `status`（draft/pending_review/approved/rejected/archived）
- `current_version`
- `created_by`
- `reviewed_by`
- `review_comment`
- `created_at/updated_at`

## 2) `agent_role_template_versions`（版本快照）

- `id`
- `template_id`
- `version_no`
- `base_profile_json`（名字、角色、职责等快照）
- `skills_json`（skill id 列表快照）
- `rules_json`（rule id 列表快照）
- `schedules_json`（定时任务快照）
- `file_manifest_json`（文件清单快照）
- `submitted_by`
- `submitted_at`

## 3) `agent_role_template_files`（文件索引）

- `id`
- `template_id`
- `version_no`（可选，支持版本绑定）
- `file_path`（模板目录内相对路径）
- `file_name`
- `mime_type`
- `size`
- `sha256`
- `uploaded_by`
- `uploaded_at`

---

## 六、文件目录与引用机制

## 1) 存储目录

每个模板使用独立目录，建议：

`/opt/openclaw-web/data/agent_templates/{template_key}/`

可按版本分层：

`/opt/openclaw-web/data/agent_templates/{template_key}/v{version_no}/`

说明：

- 不与现有 skills/rules 目录混用
- 文件操作必须做路径穿越防护（禁止 `../`）
- 文件变更需写审计日志

## 2) 引用语法（建议）

在职责/规范文本中支持：

- `{{file:规范/测试流程.md}}`
- `{{file:prompts/日报模板.md#section=提交流程}}`（可选扩展）

渲染时由后端解析为可读链接或内嵌内容（按权限控制）。

---

## 七、模板应用（复刻）流程

## 1) 选择模板

仅允许选择 `approved` 模板。

## 2) 创建新 Agent 时可选「从模板初始化」

应用内容：

- 基础身份字段（可允许局部覆盖：如名字、owner）
- 安装模板内 Skills
- 应用模板内 Rules
- 创建模板内定时任务
- 可选复制模板文件到新 Agent 私有目录（或保持引用）

## 3) 应用结果

返回应用报告：

- 成功项数（skills/rules/todos/files）
- 跳过项（不存在或权限不足）
- 冲突项（已存在同名配置）

---

## 八、API 草案

```text
GET    /api/v1/agent-templates
POST   /api/v1/agent-templates
GET    /api/v1/agent-templates/<id>
PUT    /api/v1/agent-templates/<id>
POST   /api/v1/agent-templates/<id>/submit
POST   /api/v1/agent-templates/<id>/review
POST   /api/v1/agent-templates/<id>/apply

GET    /api/v1/agent-templates/<id>/files
POST   /api/v1/agent-templates/<id>/files
GET    /api/v1/agent-templates/<id>/files/<file_id>
DELETE /api/v1/agent-templates/<id>/files/<file_id>
```

鉴权：上述 API 全部仅 `super_admin`。  
Agent 提交场景可通过受控接口放行：`POST /submit`（仍进入 `pending_review`，不直接发布）。

---

## 九、验收标准

1. 超级管理员可在 Hub 管理端看到「Agent 模板库」菜单，其他角色不可见。
2. Agent 可提交模板草稿，提交后进入 `pending_review`。
3. 模板详情有 5 个核心页签：基础信息 / Skills / Rules / 定时任务 / 文件列表。
4. 每个模板文件都存到独立目录，且能在说明文本中被引用。
5. 新建 Agent 时可选择模板并一键复刻，生成应用结果报告。
6. 所有模板操作有审计日志（创建、提交、审核、应用、文件上传删除）。

---

## 十、实施建议（分期）

### Phase 1（MVP）
- 主表 + 文件索引
- 页签 UI（基础信息、Skills、Rules、定时任务、文件）
- 超管审核流
- 新 Agent 应用模板

### Phase 2
- 版本对比与回滚
- 文件引用高级语法（锚点、片段）
- 模板应用冲突策略（覆盖/跳过/合并）

