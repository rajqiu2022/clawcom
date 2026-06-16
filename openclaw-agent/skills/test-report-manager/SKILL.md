# 测试报告中心 (test-report-manager)

## 简介

本 Skill 用于在 Hub 的**全局测试报告中心**写入和查询报告。对应模块 MEMORY #134。
适用于所有需要交付报告的场景：版本计划、功能需求测试、专项测试、需求分析、工程分析、其他专项。
**这是 6 类测试报告的唯一权威写入入口**，逐步取代旧的 `POST /test-plans/<id>/reports`（仍兼容但不推荐）。

支持能力：

- **6 种报告类型**：version_plan / feature_test / specialized_test / requirement_analysis / engineering_analysis / other_specialized
- **风险分级**：high / medium / low / tbd
- **版本/迭代关联**：iteration_id FK + version_name 冗余
- **附件**：10MB 上限，超限提示找管理员
- **分享外链**：匿名只读 `/r/<token>`，可随时撤销
- **来源追溯**：source_ref_type/_id 反向关联到测试计划/任务/需求迭代/工程批次
- **自定义类别**：`custom_category_key` 使用类别标题作为唯一 key，可按类别获取最新/全部/时间段报告列表

---

## 前置条件

- 已注册并启动 `hub-sse-sidecar-v2`
- 拥有有效 `HUB_API_TOKEN` 和 `CLAW_ID`

## Hub 地址 & 认证

```
Hub: https://clawteam.woa.com
API 前缀: /api/v1
Authorization: Bearer {HUB_API_TOKEN}
Content-Type: application/json
```

---

## 数据结构

### TestReport（测试报告）

```json
{
  "id": 12,
  "title": "v3.2 发布前功能回归测试报告",
  "report_type": "feature_test",
  "report_type_label": "功能需求测试",
  "custom_category_key": "",
  "remark": "100% 用例已执行，3 个 P2 遗留，可发布",
  "risk_level": "medium",
  "risk_level_label": "中",
  "format": "markdown",
  "project_id": 1,
  "project_name": "QQ飞车",
  "iteration_id": 8,
  "iteration_name": "v3.2 发布迭代",
  "version_name": "v3.2.1",
  "source_ref_type": "test_plan",
  "source_ref_id": 15,
  "submitter_type": "openclaw",
  "submitter_name": "Hermes Agent小赫",
  "is_hidden": false,
  "is_shared": true,
  "share_token": "abcdEFGH...",
  "shared_at": "2026-05-13 14:00:00",
  "created_at": "2026-05-13 13:30:00",
  "updated_at": "2026-05-13 14:00:00",
  "attachments": [
    {"id": 5, "filename": "v3.2-bug-list.xlsx", "size_bytes": 102400,
     "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
     "download_url": "/api/v1/test-reports/12/attachments/5/download",
     "uploaded_at": "2026-05-13 14:02:00"}
  ]
}
```

### 字段枚举

| 字段 | 取值 | 说明 |
|---|---|---|
| `report_type` | `version_plan` | 版本计划（发布前总体计划） |
|  | `feature_test` | 功能需求测试 |
|  | `specialized_test` | 专项测试（性能/兼容/安全等） |
|  | `requirement_analysis` | 需求分析 |
|  | `engineering_analysis` | 工程分析 |
|  | `other_specialized` | 其他专项 |
| `risk_level` | `high`/`medium`/`low`/`tbd` | 整体风险等级 |
| `format` | `markdown`/`html` | 正文格式 |
| `source_ref_type` | `manual`/`test_plan`/`test_task`/`requirement_iteration`/`engineering_batch` | 来源追溯 |
| `is_hidden` | `true`/`false`（默认 false） | 隐藏报告：Web 列表不显示，但 Agent 正常可见、分享链接仍有效 |
| `custom_category_key` | string（可空） | 自定义类别标题；为空时保持 6 类固定 `report_type` 视图，不冲突 |

---

## 一、报告 CRUD

### 1. 获取类型/风险/上限元数据

```
GET /api/v1/test-reports/types
```

响应：

```json
{
  "types": [
    {"key": "version_plan", "label": "版本计划"},
    {"key": "feature_test", "label": "功能需求测试"},
    ...
  ],
  "risk_levels": [
    {"key": "high", "label": "高"},
    ...
  ],
  "source_ref_types": ["manual", "test_plan", "test_task",
                       "requirement_iteration", "engineering_batch"],
  "max_attachment_size": 10485760
}
```

### 2. 查询报告列表

```
GET /api/v1/test-reports?project_id=1&report_type=feature_test&risk_level=medium
    &iteration_id=8&source_ref_type=test_plan&source_ref_id=15
    &custom_category_key=每日代码分析报告
    &search=v3.2&page=1&page_size=20
```

所有参数都可选；不传 `project_id` 时返回所有可见报告（按调用方权限过滤）。
**优先按 `project_id + iteration_id` 或 `source_ref_type + source_ref_id` 精准定位**。

> **隐藏报告可见性规则**：Agent（Bearer Token）调用 list 接口**默认可以看到隐藏报告**；Web 普通用户默认看不到（admin/super_admin 可见）。如需显式控制，可传 `show_hidden=1`。

响应：

```json
{
  "items": [ /* TestReport 简版（不含 content） */ ],
  "total": 42,
  "page": 1,
  "page_size": 20
}
```

### 3. 获取报告详情（含正文 + 附件）

```
GET /api/v1/test-reports/{REPORT_ID}
```

响应额外包含：`content`（正文）+ `attachments[]` + `can_edit`（是否可编辑）。

### 4. 创建报告

```
POST /api/v1/test-reports

{
  "title": "v3.2 发布前功能回归测试报告",     // 必填，≤200
  "report_type": "feature_test",          // 必填，6 种之一
  "project_id": 1,                        // 必填
  "iteration_id": 8,                      // 可选，关联 TestIteration
  "version_name": "v3.2.1",               // 可选，关联 iteration 时自动用迭代版本号
  "remark": "100% 用例已执行，3个P2遗留",   // 可选，≤500
  "risk_level": "medium",                 // 可选，默认 tbd
  "format": "markdown",                   // 可选，默认 markdown
  "custom_category_key": "每日代码分析报告", // 可选；为空时不归入自定义类别
  "content": "# 报告\n\n## 测试概况\n...",  // 可选
  "source_ref_type": "test_plan",         // 可选，标注来源
  "source_ref_id": 15,                    // 可选
  "is_hidden": true                       // 可选，默认 false；设为 true 后 Web 列表不显示，但分享链接仍有效
}
```

返回 201 + TestReport 完整对象（含 id），后续上传附件用这个 id。

### 5. 更新报告

```
PUT /api/v1/test-reports/{REPORT_ID}

{
  "content": "...",       // 任意一个或多个字段
  "risk_level": "high",   // 可改
  "remark": "复测发现新问题，风险升级"
}
```

**重要**：可以直接 PUT 整个 GET 返回的对象——`is_shared`/`share_token`/`source_ref_*` 等只读字段会被忽略。
可通过 PUT `is_hidden: true/false` 来切换报告的隐藏状态。

### 6. 删除报告

```
DELETE /api/v1/test-reports/{REPORT_ID}
```

软删；同时**自动撤销分享外链**（is_shared=false）。

---

## 二、自定义类别

自定义类别与固定 `report_type` 分开存储，不会改变 6 类报告类型。类别标题就是 key，不能重复；报告不传 `custom_category_key` 或传空字符串时，保持原有展示和查询行为。

### 7. 获取自定义类别列表

```
GET /api/v1/test-reports/custom-categories
```

响应：

```json
[
  {"id": 1, "title": "每日代码分析报告", "key": "每日代码分析报告", "description": ""}
]
```

### 8. 创建自定义类别

```
POST /api/v1/test-reports/custom-categories

{
  "title": "每日代码分析报告",
  "description": "Agent 每日工程扫描/代码分析产物"
}
```

标题重复返回 409。创建报告时直接传新的 `custom_category_key` 也会自动创建对应类别记录。

### 9. 按自定义类别查询报告列表

```bash
# 最新 10 份（默认）
GET /api/v1/test-reports/custom-category-reports?category=每日代码分析报告

# 最新 N 份
GET /api/v1/test-reports/custom-category-reports?category=每日代码分析报告&limit=20

# 全部
GET /api/v1/test-reports/custom-category-reports?category=每日代码分析报告&all=1

# 时间段
GET /api/v1/test-reports/custom-category-reports?category=每日代码分析报告&since=2026-06-01&until=2026-06-16T23:59
```

返回：

```json
{
  "category": "每日代码分析报告",
  "count": 10,
  "items": [
    {
      "id": 42,
      "title": "每日代码分析报告-2026-06-16",
      "created_at": "2026-06-16 21:30:00",
      "created_task": {
        "source_ref_type": "engineering_batch",
        "source_ref_id": 188
      },
      "report_link": "https://clawteam.woa.com/test-reports/42"
    }
  ]
}
```

Agent 收到“取某类报告最新 10 份 / 全部 / 某时间段”时，优先用 `custom-category-reports`，不要自己拉全量报告再过滤。

---

## 三、附件管理

### 7. 上传附件（≤10MB）

```
POST /api/v1/test-reports/{REPORT_ID}/attachments

Content-Type: multipart/form-data
field: file=@/path/to/file
```

curl 示例：

```bash
curl -X POST "$HUB/api/v1/test-reports/12/attachments" \
  -H "Authorization: Bearer $HUB_API_TOKEN" \
  -F "file=@bug-list.xlsx"
```

成功 → 201 + 附件元数据。超 10MB → 413：

```json
{
  "error": "附件超过 10MB 上限，请联系管理员协助上传",
  "limit_bytes": 10485760
}
```

**遇到 413 时正确处理**：告诉用户/上游"文件过大，需要联系 OpenClaw Hub 管理员（龙虾王 或 super_admin）协助上传"。**不要尝试拆分压缩绕过限制**——10MB 是业务侧故意限制，超出说明附件本身设计不合理（如未压缩日志、完整 dump），需要人工评估。

### 8. 下载附件（需登录态/Token）

```
GET /api/v1/test-reports/{REPORT_ID}/attachments/{ATT_ID}/download
```

直接返回文件流，`Content-Disposition` 含中文文件名。匿名访问返回 401。

### 9. 删除附件

```
DELETE /api/v1/test-reports/{REPORT_ID}/attachments/{ATT_ID}
```

---

## 三、分享外链

### 10. 开启分享

```
POST /api/v1/test-reports/{REPORT_ID}/share
POST /api/v1/test-reports/{REPORT_ID}/share?refresh=1   // 强制换 token
```

响应：

```json
{
  "is_shared": true,
  "share_token": "abcdEFGH-_~22charsURLsafe",
  "share_url": "https://clawteam.woa.com/r/abcdEFGH...",
  "shared_at": "2026-05-13 14:00:00"
}
```

**token 复用规则**：默认调用会**复用旧 token**（之前发出去的链接继续有效）。仅当传 `?refresh=1` 才重新生成；调用前请确认是否真要让旧链接失效。

### 11. 撤销分享

```
DELETE /api/v1/test-reports/{REPORT_ID}/share
```

立即生效，匿名访问立刻 404。

### 12. 匿名只读访问（**给非 Hub 用户的接口**）

```
GET /api/v1/test-reports/shared/{TOKEN}      # 无需 Authorization 头
```

页面访问：`https://clawteam.woa.com/r/{TOKEN}` 或 `/test-reports/share/{TOKEN}`

匿名访问可看到：标题/类型/风险/项目/版本/提交人/正文/附件**文件名 + 大小**（不可下载，提示登录）。
ID、source_ref、submitter_user_id 等内部字段被脱敏。

---

## 四、权限模型

| 操作 | 谁可以 |
|---|---|
| 查看（list/detail/download） | 项目内所有用户 + admin/super_admin |
| 查看隐藏报告（list 中可见） | Agent（Bearer Token） + admin/super_admin；普通 Web 用户默认不可见 |
| 创建 | 任何已认证调用方（Bearer Token 或登录态）|
| 编辑/删除（PUT/DELETE/上传附件/分享） | 报告作者 + admin/super_admin（admin claw 含项目 admin） |
| 匿名访问外链 | 所有人（匿名）—— 附件下载除外；**隐藏报告的分享链接照常可用** |

`submitter_type=openclaw` 时作者是发起 POST 的 Agent；改写要用同一 token 或 admin claw。

---

## 五、常见任务模板

### 任务 A：发布前功能回归测试报告

```python
import requests, os
HUB = 'https://clawteam.woa.com'
H = {'Authorization': f'Bearer {os.environ["HUB_API_TOKEN"]}',
     'Content-Type': 'application/json'}

# 1) 先用 list 接口确认有没有同迭代同类型的旧报告（避免重复创建）
r = requests.get(f'{HUB}/api/v1/test-reports', headers=H, params={
    'project_id': 1, 'iteration_id': 8, 'report_type': 'feature_test',
})
existing = r.json().get('items', [])

# 2) 创建（或更新已有那份）
payload = {
    'title': 'v3.2 发布前功能回归测试报告',
    'report_type': 'feature_test',
    'project_id': 1,
    'iteration_id': 8,
    'version_name': 'v3.2.1',
    'remark': '100% 核心用例通过，3 个 P2 遗留，建议发布',
    'risk_level': 'medium',
    'format': 'markdown',
    'content': '# v3.2 发布前回归测试报告\n\n## 测试范围\n...',
    'source_ref_type': 'test_plan',
    'source_ref_id': 15,
}
if existing:
    rid = existing[0]['id']
    r = requests.put(f'{HUB}/api/v1/test-reports/{rid}', headers=H, json=payload)
else:
    r = requests.post(f'{HUB}/api/v1/test-reports', headers=H, json=payload)
report = r.json()
print('report id =', report['id'])
```

### 任务 B：上传附件 + 分享外链

```python
# 上传附件
with open('bug-list.xlsx', 'rb') as fp:
    r = requests.post(
        f'{HUB}/api/v1/test-reports/{report["id"]}/attachments',
        headers={'Authorization': H['Authorization']},   # multipart 不带 Content-Type
        files={'file': ('bug-list.xlsx', fp,
                        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')},
    )
if r.status_code == 413:
    print('附件超 10MB，请联系管理员（龙虾王）协助上传')
else:
    r.raise_for_status()

# 开启分享
r = requests.post(f'{HUB}/api/v1/test-reports/{report["id"]}/share', headers=H)
print('外链:', r.json()['share_url'])
```

### 任务 C：从测试计划/任务上下文回查报告

```python
# 一个测试计划下的所有报告
requests.get(f'{HUB}/api/v1/test-reports',
             headers=H,
             params={'source_ref_type': 'test_plan', 'source_ref_id': 15})

# 一个测试任务下的所有报告
requests.get(f'{HUB}/api/v1/test-reports',
             headers=H,
             params={'source_ref_type': 'test_task', 'source_ref_id': 42})

# 某迭代下所有类型的报告
requests.get(f'{HUB}/api/v1/test-reports',
             headers=H,
             params={'project_id': 1, 'iteration_id': 8})
```

### 任务 D：批量风险升级

```python
# 例如复测发现新阻塞，把当前迭代所有 medium 报告升级为 high
r = requests.get(f'{HUB}/api/v1/test-reports', headers=H, params={
    'project_id': 1, 'iteration_id': 8, 'risk_level': 'medium',
})
for item in r.json().get('items', []):
    requests.put(f'{HUB}/api/v1/test-reports/{item["id"]}', headers=H, json={
        'risk_level': 'high',
        'remark': item.get('remark', '') + '【复测后升级】',
    })
```

### 任务 E：创建隐藏报告 + 分享链接（推荐 Agent 常用模式）

```python
# Agent 创建报告但不让 Web 列表显示，通过分享链接交付
payload = {
    'title': '秒杀宝箱专项测试执行报告',
    'report_type': 'specialized_test',
    'project_id': 1,
    'risk_level': 'low',
    'format': 'markdown',
    'content': '# 专项测试报告\n\n...',
    'source_ref_type': 'test_task',
    'source_ref_id': 42,
    'is_hidden': True,    # 隐藏：Web 列表不展示
}
r = requests.post(f'{HUB}/api/v1/test-reports', headers=H, json=payload)
report = r.json()

# 开启分享，获取外链
r = requests.post(f'{HUB}/api/v1/test-reports/{report["id"]}/share', headers=H)
share_url = r.json()['share_url']
print(f'报告已创建（隐藏），分享链接: {share_url}')
# Agent 将 share_url 发给需要查看的人即可
```

---

## 六、迁移说明 / 与旧接口的关系

### 旧接口仍可用，但推荐改用本接口

| 旧入口（保留兼容） | 新接口（推荐） |
|---|---|
| `POST /test-plans/<id>/reports` | `POST /test-reports` + `source_ref_type=test_plan` |
| `POST /test-plans/<id>/tasks/<tid>/reports` | `POST /test-reports` + `source_ref_type=test_task` |
| `POST /test-plans/<id>/report` (单份) | 同上 |

### 已自动迁移的数据

部署时 Hub 已把所有 `test_plan_reports` 和 `test_task_reports` 一次性复制到 `test_reports` 表，
设置 `source_ref_type/id` 反查；旧表的 `linked_test_report_id` 也已回填。
**列表查询会同时看到新旧两份**——若发现重复，请用新表 id 调 PUT 合并/删除。

### 旧 Skill #1 `test-report-generator`

那是一个早期的本地报告写作助手（无 Hub API 调用），现已被本 Skill 取代。
Agent 若分配到该 Skill，应优先用本 Skill 走 API，而不是仅本地输出。

---

## 七、最佳实践

1. **同一份"逻辑报告"用 PUT 而不是反复 POST**：每个迭代每种类型应该只有 1～2 份正式报告，迭代过程中通过 PUT 增量更新内容，而不是每天创建新一份（会让分享链接和审计变乱）。
2. **风险等级要诚实**：`tbd` 仅在测试未启动时用；启动后必须落到 `low/medium/high` 之一。这个字段直接影响发布评审的决策路径。
3. **附件命名规范**：建议格式 `<版本号>-<内容>-<日期>.<ext>`，如 `v3.2.1-bug-list-20260513.xlsx`；中文名安全保留，但长度 ≤200。
4. **分享外链谨慎用**：外链是**完全匿名**的，任何拿到 token 的人都能看到正文。涉及未发布内幕、未脱敏 Bug 截图，不要走外链——发邮件 + Hub 登录访问更安全。
5. **关联来源比标题更重要**：`source_ref_type/id` 决定了从测试计划页"前往全局测试报告"按钮能不能找到这份报告；不填默认 `manual`，会变成"孤儿报告"难以追溯。
6. **格式选择**：默认 markdown（支持表格、代码块、Mermaid 图）；只有当报告含复杂样式（如设计稿截图带定位标注、自定义颜色）时才用 html。html 会走 iframe sandbox 渲染，不能执行 JS。

---

## 八、错误码参考

| HTTP | 含义 | 处理 |
|---|---|---|
| 400 | 字段非法（title 空 / report_type 不在 6 种 / format 非 markdown/html） | 修正字段重试 |
| 401 | Token 无效或未提供 | 检查 `HUB_API_TOKEN` |
| 403 | 无权限编辑（非作者非 admin） | 让作者或 admin claw 操作；查询不受影响 |
| 404 | 报告不存在/已软删/分享已撤销 | 用 list 查最新可用 id |
| 413 | 附件超 10MB | **找管理员协助**，不要拆分压缩绕过 |
| 500 | 服务端异常 | 重试 1 次，若仍 500 联系 super_admin（龙虾王） |
