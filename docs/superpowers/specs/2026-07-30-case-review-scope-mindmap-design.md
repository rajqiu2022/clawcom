# 用例评审改造设计：评审范围可见性 + 目录级发起 + 目录脑图视图

日期：2026-07-30
状态：设计待确认（未开始实现）

## 1. 背景与现状事实

改造前先核对了现有实现，有几处与需求预设不一致，直接决定了方案形状。

### 1.1 用例库没有目录表

目录树是虚拟的：由 `TestCase.module_path`（`/` 分隔字符串，`VARCHAR(500)`）+ `is_placeholder=True`
的占位用例拼出来，没有 `TestCaseDirectory` 表，也没有 `parent_id` 自关联。

结论：**「任意层级目录发起评审」不需要建目录表**，范围表达为 `(library_id, module_path 前缀)`。

### 1.2 评审范围的表结构已经存在，但没接线

`TestCaseLibraryReview`（`web/app/models.py`）已经有这三列：

- `scope_type`：`library` / `module` / `cases`
- `scope_module_path`：`scope_type=module` 时按前缀匹配子树
- `scope_case_count`：发起时的用例数快照

但 `submit_library_review`（`web/app/api/testcases.py`）**完全没有读写它们**，永远落库为默认
`scope_type='library'`。属于建好没接线的死结构。

结论：目录级评审**不需要新增列**，只要把发起接口与前端接上。

### 1.3 `invited_reviewers` 不具备鉴权能力

`TestCaseLibraryReview.invited_reviewers` 是 JSON 快照，除了写审计日志时统计数量外，
**没有任何地方用它做权限判定**。

结论：「指定用户或 Agent」必须新建真正的授权表，不能复用这个字段。

### 1.4 现有 `public` 并不是「完全公开」

`Topic.visibility` 目前只有 `public` / `project`。而 `public` 的实际效果是
「登录 **且** 有项目权限」，因为 `web/app/api/__init__.py` 的全局 `before_request` 里有项目门禁：
无项目权限的登录用户访问几乎所有 `/api/v1/*` 都会拿到 `403 PROJECT_REQUIRED`。

关键约束：**项目门禁是按路径前缀判定的，而可见性是按课题逐条判定的**。所以不能简单把
`/api/v1/topics` 整个加进白名单，否则四档范围会全部失效。

### 1.5 已有库级脑图接口，但不是目录树，且前端没用

`GET /testcase-libraries/<id>/mindmap` 存在，但 `build_mindmap_from_cases` 是按
**优先级 → 用例类型** 分组的，不是目录树；`testcases.html` 也没有调用它，用户实际看到的是
点用例标题打开的**单条用例脑图**（自研 DOM + SVG，无第三方库）。

结论：目录脑图是一份**新的视图数据源**，不能直接复用；同时不改动现有 `/mindmap` 接口，
避免影响 XMind 导出等既有链路。

## 2. 已确认的产品决策

| 决策项 | 结论 |
| --- | --- |
| 完全公开的范围 | 仅限已登录用户/Agent，但**不校验项目权限**；不做匿名外链、不做 share_token |
| 脑图图标来源 | 混合：`P0/P1/P2` 自动取用例 `priority`；`❗⚠️🚩` 由人在脑图上手动标记 |
| 标记归属 | 标记是**评审侧的镜像层**，先不同步回原用例库 |
| 图标语义 | `❗`=有问题/待修改，`⚠️`=风险或待确认，`🚩`=重点关注 |
| 评审范围粒度 | 单个起始目录，自动含全部子目录 |
| 节点级评论 | 不做。评审意见仍然汇总后走课题回复 |
| 标记写权限 | 能评论该课题的人都能标记，保留 `marked_by`；发起人与用例库管理者可清除他人标记 |
| 库级评审状态 | 保持 `TestCaseLibrary.review_status` 由最近一次评审驱动，接受已知局限（见 §10.3） |

## 3. 可见性四档设计

### 3.1 枚举取值（向后兼容，不做数据迁移）

| `visibility` | 需求叫法 | 判定 | 变更 |
| --- | --- | --- | --- |
| `public_all` | 完全公开 | 已登录（用户或 Agent）即可，**跳过项目门禁** | 新增 |
| `public` | Hub 用户或 Agent | 已登录 + 有项目权限（即现行 `public` 行为） | 保留 |
| `project` | 项目内用户或 Agent | `caller.project_ids` 命中课题 `project_name` 对应项目 | 保留 |
| `assigned` | 指定用户或 Agent | 命中 `topic_grants` 授权 | 新增 |

刻意**不**把现有 `public` 改名成 `hub`：那样要全表迁移历史课题，且会让存量课题语义漂移。
新增值挂在旁边，存量数据零改动。

### 3.2 新表 `topic_grants`

参照仓库里成熟的 `TestCaseLibraryShare` / `EngineeringShare` 模式（关联表，而非 JSON 字段）：

| 列 | 说明 |
| --- | --- |
| `topic_id` | FK → `topics.id` |
| `grant_type` | `user` / `claw` |
| `target_user_id` | `grant_type=user` 时有值 |
| `target_claw_id` | `grant_type=claw` 时有值 |
| `granted_by` | 授权人 username / claw_name |
| `expires_at` | 可空，过期自动失效 |

唯一约束 `(topic_id, grant_type, target_user_id, target_claw_id)`。

沿用 `testcases.py` 里「授权给用户时，其名下 Agent（`owner == username`）自动继承」的既有语义，
避免出现「人能看、他的 Agent 看不到」的割裂。

`invited_reviewers` 快照保持不动（历史兼容），但**鉴权只认 `topic_grants`**。

### 3.3 统一判定服务 `web/app/services/review_visibility.py`

现在 `topics.py` 的可见性过滤在 `list_topics` 里重复写了两遍（列表 + 板块计数），
详情/回复各自又写了一份 `project` 判定。这次一并收口：

```
can_view_topic(caller, topic, grants=None) -> bool
can_comment_topic(caller, topic, grants=None) -> bool
visible_topic_filter(caller) -> SQLAlchemy 条件      # 供列表与计数复用
is_project_exempt_review_path(path, method) -> bool  # 供项目门禁豁免
```

### 3.4 项目门禁的精确豁免

在 `api/__init__.py` 的项目门禁判定**之前**先问 `is_project_exempt_review_path(path, method)`，
只豁免下面这些（对 method 敏感）：

- `GET /api/v1/topics`
- `GET /api/v1/topics/<id>`
- `POST /api/v1/topics/<id>/replies`
- `GET /api/v1/topics/<id>/review-mindmap`
- `GET|PUT /api/v1/topics/<id>/review-marks`

明确**不**豁免：

- `POST /api/v1/topics`：发起课题仍要求项目权限
- `/api/v1/testcase-libraries/*`：用例库接口一律不放宽

豁免只是跳过「有没有项目」这道粗门禁，**逐条可见性判定照旧**：无项目权限的用户走
`visible_topic_filter` 只会看到 `public_all`，请求 `project` / `assigned` 课题仍然 403。

这是本次改造最容易出安全问题的一处，必须有针对性回归测试。

## 4. 任意层级目录发起评审

### 4.1 接口变更

`POST /api/v1/testcase-libraries/<library_id>/reviews` 新增可选入参：

```json
{
  "scope_type": "module",
  "scope_module_path": "登录模块/手机号登录",
  "visibility": "public_all",
  "grants": [{"type": "user", "id": 12}, {"type": "claw", "id": 11}],
  "submit_note": "本轮只评审手机号登录子树"
}
```

行为：

1. 写入 `TestCaseLibraryReview.scope_type / scope_module_path / scope_case_count`
   （`scope_case_count` 用前缀匹配 `module_path == p or module_path LIKE 'p/%'` 统计，排除占位用例）。
2. 自动创建的 `case_review` 课题写入 `Topic.review_module_paths = [scope_module_path]`
   与 `Topic.visibility`；`visibility=assigned` 时同步写 `topic_grants`。
3. 防重复的唯一性从「库」下沉到「库 + 范围」：同一 `(library_id, scope_module_path)`
   不允许并存多个 `reviewing`，不同目录可以并行评审。

### 4.2 发起权限不放宽

发起仍走 `_can_manage_library` 或 share `editor`。理由：发起评审等于把用例内容对外公开，
属于扩大可见范围的动作，不能因为「只是评审一个子目录」就降低门槛。

### 4.3 前端

`testcases.html` 目录树的每个目录节点增加发起评审入口（当前只有用例库根节点有 `📝`），
弹窗里增加范围选择（四档可见性 + 指定用户/Agent 选择器）。

## 5. 目录脑图视图

### 5.1 新服务 `web/app/services/case_mindmap.py`

```
build_directory_mindmap(library, root_module_path='', marks=None) -> dict
```

节点 schema 在旧结构（`id` / `text` / `children`）基础上扩展，保持前向兼容：

```json
{
  "id": "mod:登录模块",
  "node_type": "module",
  "text": "登录模块",
  "case_count": 12,
  "priority": null,
  "mark": "flag",
  "icons": ["flag"],
  "children": [
    {
      "id": "case:123",
      "node_type": "case",
      "text": "手机号验证码登录成功",
      "priority": "P0",
      "mark": "question",
      "icons": ["P0", "question"]
    }
  ]
}
```

- `node_type`：`module` / `case`，前端据此决定可否展开与可否标记
- `icons`：优先级图标自动派生，标记图标来自 `marks`
- 颜色不写死在后端，只给 `priority` + `mark`，由前端按既有 `.p0~.p3` 色板派生

### 5.2 两个入口，ACL 不同（关键设计）

| 接口 | 鉴权 | 用途 |
| --- | --- | --- |
| `GET /api/v1/testcase-libraries/<id>/directory-mindmap?module_path=` | 用例库 ACL（`_ensure_library_access` 只读） | 用例库页的目录脑图 Tab |
| `GET /api/v1/topics/<id>/review-mindmap` | **课题 ACL**（`can_view_topic`） | 课题详情页的脑图页签 |

第二条是这次能不能做成「完全公开评审」的核心：外部评审人**没有用例库权限**，
如果脑图接口走 `_ensure_library_access` 一定 403。所以公开评审读脑图必须挂在课题命名空间下，
由课题可见性授权，并且**把返回范围锁死在该评审的 `scope_module_path` 子树内**，越界节点不返回。

现有 `GET /testcase-libraries/<id>/mindmap`（优先级分组）保持不动。

## 6. 评审标记（镜像层）

### 6.1 新表 `case_review_node_marks`

| 列 | 说明 |
| --- | --- |
| `topic_id` | FK → `topics.id`，标记随评审课题存在 |
| `node_type` | `module` / `case` |
| `node_key` | `node_type=case` 时为 `test_cases.id`；`module` 时为 `module_path` |
| `mark` | `question` / `risk` / `flag` |
| `note` | 可空短备注 |
| `marked_by` / `marked_at` | 留痕 |

唯一约束 `(topic_id, node_type, node_key)`。

### 6.2 明确的隔离边界

标记是镜像层，**不回写用例库**：

- 不写 `TestCase` 任何列，不写 `content.status`
- 用例库页自己的目录脑图 Tab **不显示**评审标记
- 用例被删除后标记成为孤儿记录，读取时跳过，不报错

后续若要「评审结论回写用例」，单独设计一次显式的同步动作，不在本期隐式打通。

### 6.3 API

- `GET /api/v1/topics/<id>/review-marks`
- `PUT /api/v1/topics/<id>/review-marks`：批量 upsert / 清除，幂等

### 6.4 写权限

**能评论该课题的人都能标记**（即 `can_mark == can_comment_topic`），保留 `marked_by` 留痕；
评审发起人与用例库管理者可清除他人标记。

因为标记不回写用例库，被乱改的影响面仅限该次评审的镜像视图，风险可接受。
若日后要收紧，只需把 `can_mark` 换成更窄的判定，其余设计不变。

## 7. 前端复用

新增 `web/static/js/case_mindmap.js`，作为两个页面共用的渲染器：

- 不引第三方脑图库、不加新 CDN（仓库现状只有 Google Fonts 一个 CDN，内网环境不宜再加）
- 沿用现有单条用例脑图的自研 DOM + SVG 思路，但补上目录树必需的**折叠/展开**与大节点量处理
- `testcases.html`：目录节点增加「🧠 目录脑图」入口，开 Tab
- `topic_detail.html`：新增「用例脑图」页签，替换现在贴链接的做法

两处共用同一渲染器与同一节点 schema，避免出现两套脑图实现继续分裂。

## 8. Agent 支持

所有新接口统一走 `topics.py` 的 `_get_caller_info()`（已同时支持 Web session 与 Bearer Token），
Agent 可发起目录评审、读脑图、读写标记、发评论。

**Skill 文档落点更正**：仓库里没有 `case-review/SKILL.md`，评审相关能力实际分布在两个已有 Skill，
改造后按职责分别更新，不新建 Skill：

- `openclaw-agent/skills/topic-discuss/SKILL.md` —— visibility 四档、授权名单接口、
  `review-mindmap`、`review-marks`（含 `skipped.reason` 对照表）
- `openclaw-agent/skills/testcase-manager/SKILL.md` —— 发起评审的 `visibility` / `grants` 入参、
  库级 `directory-mindmap`、以及它与 `review-mindmap` 的鉴权差异

## 9. 分期实施

| 阶段 | 内容 | 可独立上线 | 状态 |
| --- | --- | --- | --- |
| P0 | 可见性四档 + `topic_grants` + `review_visibility.py` + 项目门禁精确豁免 + 判定收口 | 是 | 已完成 |
| P1 | 目录级发起评审（接线已有列）+ 发起弹窗范围选择 | 是 | 已完成 |
| P2 | `case_mindmap.py` + 两个脑图接口 + `case_review_node_marks` + 共用渲染器 + 用例库 Tab | 是 | 已完成 |
| P3 | 课题详情脑图页签替换贴链接 + Skill 文档 + 经验记录 | 是 | 已完成 |

本地验收：新增 6 个测试模块共 106 例全绿（含 `tests/test_review_mindmap_api.py`
18 例走真实 HTTP 的端到端契约测试）；全量 `unittest discover` 265 例，仅
`test_panorama_test_metrics` 2 例报错，属改造前既有问题（`testcase_panorama_links`
缺 `agent_test_metric_payload`），与本次无关。

端到端层重点守住三条不能回归的线：无项目权限用户**能**读 `public_all` 评审脑图、
**不能**读 `public` / 未授权 `assigned` 评审、**不能**借目录评审标记范围外用例
（越界节点进 `skipped[].reason=OUT_OF_SCOPE`）。

每阶段都以「本地单测 + 契约测试通过」为门槛，灰度上线沿用共享记忆那套做法：
先上代码与表结构，功能开关默认关闭，验证无回归后再逐档开启可见性。

## 10. 风险与回归重点

1. **项目门禁豁免**是最高风险点：必须有测试覆盖「无项目权限用户能看 `public_all`、
   看不到 `public` / `project` / `assigned`」。
2. `visibility` 新增取值后，旧前端若把非 `public` 一律当 `project` 展示会显示错标签，
   需要同步更新 `topics.html` 与 `topic_detail.html` 的标签映射。
3. **已知局限（已确认接受）**：目录评审并行后，`TestCaseLibrary.review_status` 是**库级**单值，
   无法表达「A 目录评审中、B 目录已通过」。本期保持库级状态由最近一次评审驱动；
   目录维度的真实状态查 `test_case_library_reviews` 按 `scope_module_path` 过滤。
   前端在目录评审场景下应展示该目录自己的评审记录状态，而不是库级 badge，避免误导。
4. 大用例库（数千条）的目录脑图需要限制单次返回节点数并支持懒加载子树，否则前端会卡。
   **上线时在这一条上真的翻车了**：初版让目录节点与用例叶子共用一个 `max_nodes` 预算，
   深度优先把预算耗在第一个顶层目录的子树里，导致 2311 条用例的库**丢掉一个顶层目录**、
   根节点 `case_count` 只有 627（真实 2311）。已改为「目录不占叶子预算 + 计数无条件累加 +
   只截断用例叶子」，并把默认叶子上限从 800 提到 2000。
   截断语义收敛为：**目录树与各级 `case_count` 永远完整准确，只有用例叶子可能不全**；
   调用方判断"用例是否都拿到"看 `shown_case_count == total_case_count`，不看 `truncated`。

## 11. 上线记录（2026-07-30）

部署脚本 `_deploy_case_review_scope_mindmap.py`（符号级漂移预检 → SQL 迁移 → 备份 →
上传 → py_compile → restart → 建表/路由/HTTP/静态资源验证，失败自动回滚）。

线上验证（`_smoke_case_review_online.py`，只读、不造数据）：

| 项 | 结果 |
| --- | --- |
| 可见性枚举 | `assigned / project / public / public_all` |
| 新增表 | `topic_grants`、`case_review_node_marks` 均已建 |
| 新增路由 | 6 条全部注册 |
| 存量课题 visibility 分布 | `public:23 / project:21`，未被改造改坏 |
| 整库脑图（2311 条） | 200，405ms，顶层目录 4/4 完整，`case_count` 2311 准确 |
| 整库脑图（1317 / 896 条） | 200，210ms / 129ms，完全不截断 |
| 存量评审课题脑图 | 200，35ms |

漂移预检在上线前拦下一个真实事故：远端 `api/__init__.py` 含两处本地没有的线上改进
（`workflows` 导入、`_verify_bearer_token` 的 detached instance 修复），整传会把它们删掉。
已把本地对齐成「远端版 + 本次改动」。
