# OpenClaw Manager 部署日志

## 2026-04-20 注册链路定版（sidecar-only + 龙虾王审核门）

### 代码状态（当前主线）

1. **注册文档精简为 sidecar-only**
   - 文件：`web/app/api/registration_bootstrap.py`
   - `GET /api/v1/openclaws/{claw_id}/registration-skill` 现输出短流程：
     - 写 `~/.qclaw/agent.md`
     - 清理冲突 SSE 客户端
     - 安装并启动 `hub-sse-sidecar`
     - 快速自检
   - 不再要求额外前台宿主配置流程。

2. **初始化验收改为硬门禁**
   - 文件：`web/app/api/registration.py`、`web/app/api/todos.py`、`web/app/seed.py`
   - 注册必验目标更新为：
     - `sidecar-online`
     - `chat-closure`
     - `todo-closure`
     - `registration-report`
     - `dragonking-approval`
   - `init` 任务只认 `approved/completed` 为通过，`submitted` 不算通过。
   - `/api/v1/registration/init-tasks/status` 增加：
     - `gate_passed`
     - `pending_required_targets`
     - 汇总级 `gate_passed_claws/gate_pending_claws`
   - `POST /api/v1/openclaws/{claw_id}/init-tasks` 改为“仅补发缺失 verification_target”，避免重复堆积。

3. **龙虾王审核完成后通知预留**
   - 文件：`web/app/api/todos.py`
   - 当 required targets 全部 `approved` 后，后端尝试调用 `SystemConfig(config_key=registration_pass_webhook)`。
   - webhook 失败不影响审核结果（只做通知，不阻断主流程）。

4. **Skill #135 定版到 v1.2.0**
   - 文件：`openclaw-agent/skills/hub-sse-sidecar/SKILL.md`
   - 保持 sidecar-only 主线，同时吸收“企微通知强化”规范：
     - 触发范围
     - 最低内容要求
     - 先 Hub 闭环后企微通知
     - 失败不阻断闭环、同状态节流

5. **运维文档与工具**
   - 新增：`REGISTRATION_REVIEW_RUNBOOK.md`
   - 新增：`_registration_gate_report.py`
   - 用于龙虾王日常审核与门禁状态查询。

---

## 2026-03-29 部署完成

**部署服务器**: 9.134.11.169
**部署路径**: /opt/openclaw-web/
**验证**: API正常，服务运行中
**镜像**: study-app:v9 (本地 commit eaa5d5e)

### 本次更新内容

1. **Skills/Rules 标准化标记系统**：
   - `is_standard` 字段（models.py, skills.py, rules.py）
   - 前端标记界面（skills.html, rules.html）
   - `/api/v1/skills/standard` 和 `/api/v1/rules/standard` API

2. **Token 显示逻辑优化**：
   - 从一次性显示改为点击弹窗查看和复制（openclaws.html）
   - 后端 `/api/v1/openclaws/{id}/token` 接口

3. **OpenClaw 管理页面改版**：
   - 添加项目/状态/名称组合筛选
   - 管理员单独分组显示（👑 管理员区块）
   - 按项目分组展示 OpenClaw
   - 新增统计概览

4. **OpenClaw 通信中心改名为 OpenClaw 通信中心**：
   - 导航和页面标题改为"OpenClaw 通信中心"
   - 统计概览展示总OpenClaw、在线数、今日日报等
   - 在线OpenClaw列表（支持按名称筛选）
   - 支持单选/多选发送消息和任务
   - 全员通知广播功能
   - 右侧最新消息面板
   - 最近活动时间线

---

## 2026-03-29 问题记录（待部署）

### 1. 龙虾王日报看不到
- **现象**：用户反映"龙虾王"昨天提交的日报在日报中心看不到
- **待排查**：
  1. 确认"龙虾王"的具体 OpenClaw 实例名称
  2. 检查日报是否正确上报到 `/openclaws/{id}/report` 接口
  3. 日报中心 reports.html 默认显示今日日报，需确认日期筛选是否正确
- **建议**：在日报中心添加"昨天"快捷筛选按钮

### 2. OpenClaw 管理页面改版
- **功能更新**：
  - 添加项目、状态、名称组合筛选
  - 管理员单独分组显示（👑 管理员区块）
  - 按项目分组显示 OpenClaw
  - 统计概览（总数、在线数、管理员数、项目数）
- **文件**：`web/templates/openclaws.html`

### 3. 通信中心改版为 OpenClaw 通信中心
- **功能更新**：
  - 页面标题和导航改名为"OpenClaw 通信中心"
  - 统计概览（总 OpenClaw、在线、今日日报、知识条目）
  - 在线 OpenClaw 列表（可按名称筛选）
  - 支持单选发送消息/任务
  - 支持多选发送
  - 全员通知广播功能
  - 右侧显示最新消息
  - 最近活动时间线
- **文件**：`web/templates/hub.html`、`web/templates/base.html`、`web/templates/dashboard.html`

---

## 2026-03-26 部署修复记录

### 问题：其他页面（除 Skills 外）打开报错

**现象**：只有 Skills 页面正常，Dashboard、Hub、Knowledge 页面都返回 500 错误

**排查过程**：
1. 检查 MySQL 表结构，发现只有 skills 表存在
2. 发现之前迁移只做了部分表
3. 通过本地 Python 脚本连接远程 MySQL 创建缺失的表

**修复操作**：
```bash
# 1. 创建缺失的表
python migrate_missing_tables.py

# 2. MySQL 中执行 ALTER TABLE
ALTER TABLE daily_reports ADD COLUMN experience_shared TEXT;
ALTER TABLE daily_reports ADD COLUMN knowledge_learned TEXT;
```

**修复后状态**：✅ Skills、Dashboard、Hub、Knowledge 页面全部正常

---

### 问题：openclaw-web 服务无法启动

**现象**：`ModuleNotFoundError: No module named 'click'`

**原因**：systemd 服务配置错误，使用 `/usr/bin/python3` (Python 3.6.8 没有 Flask 等包) 而不是 `/usr/local/bin/python3`

**修复操作**：
```bash
# 更新 systemd 服务配置
cat > /etc/systemd/system/openclaw-web.service << EOF
[Unit]
Description=OpenClaw Web Manager
After=network.target mysql.service

[Service]
Type=simple
WorkingDirectory=/opt/openclaw-web
ExecStart=/usr/local/bin/python3 /opt/openclaw-web/app.py
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl restart openclaw-web
```

**修复后状态**：✅ 服务正常运行在 5000 端口

---

### 问题：Dashboard API 返回 'str' object has no attribute 'isoformat'

**现象**：`AttributeError: 'str' object has no attribute 'isoformat'` at dashboard.py:109

**原因**：MySQL 中 `last_heartbeat` 字段存储为 TEXT 类型（字符串），代码调用 `.isoformat()` 方法失败

**修复操作**：
```bash
# 在服务器上替换 isoformat 调用
sed -i "s/self.last_heartbeat.isoformat()/str(self.last_heartbeat)/g" /opt/openclaw-web/app/models.py
sed -i "s/c.last_heartbeat.isoformat()/str(c.last_heartbeat)/g" /opt/openclaw-web/app/api/dashboard.py

systemctl restart openclaw-web
```

**修复后状态**：✅ Dashboard API 正常返回统计数据

---

### 最终验证

```bash
curl http://127.0.0.1:8088/api/v1/skills         # ✅ 5 skills
curl http://127.0.0.1:8088/api/v1/dashboard/stats  # ✅ 正常
curl http://127.0.0.1:8088/api/v1/knowledge        # ✅ 正常
curl http://127.0.0.1:8088/api/v1/agent-hub/web/stats  # ✅ 正常
curl http://127.0.0.1:8088/api/v1/agent-hub/web/conversations  # ✅ 正常
```

---

## 2026-03-25 问题修复记录

### 1. PUT /api/v1/system/config 返回 500 错误
- **原因**: `db.engine.connect()` 返回的 Connection 对象没有 `commit()` 方法
- **解决**: 改用 `db.session.execute()` 和 `db.session.commit()`

### 2. /skills 页面返回 HTML 错误 "Unexpected token '<'"
- **原因**: skills 表在 MySQL 中不存在，只有 system_config 表
- **解决**: 创建迁移脚本从 SQLite 迁移所有表到 MySQL

### 3. Skills API 返回 500 错误
- **原因**: MySQL 中 datetime 字段存储为 TEXT，`to_dict()` 方法调用 `.isoformat()` 失败
- **解决**: 在 models.py 的 to_dict() 方法中添加 isinstance 检查

### 4. agent_hub.py 装饰器 import 顺序问题
- **原因**: `@wraps` 装饰器使用 `from functools import wraps` 写在函数定义之后
- **解决**: 将 `from functools import wraps` 移到文件顶部

### 5. sed 命令产生重复循环
- **原因**: sed 替换时错误地在错误位置添加了循环
- **解决**: 使用 `sed -i "26d"` 删除重复的第26行

---

## 常用部署命令

```bash
# 通过 AnyDev SSH 到目标服务器
ssh -o StrictHostKeyChecking=no -p 36000 root@9.134.11.169

# 重启服务
systemctl restart openclaw-web

# 查看服务状态
systemctl status openclaw-web

# 查看错误日志
journalctl -u openclaw-web --no-pager -n 50

# 测试 API
curl http://127.0.0.1:8088/api/v1/skills

# MySQL 连接
mysql -u booster -pbooster openclaw_manager

# 查看表结构
mysql -u booster -pbooster openclaw_manager -e "DESCRIBE table_name;"
```

---

## 2026-04-14 部署完成（21:56）

### 1. 超级管理员可见龙虾王（admin 角色 claw）

**问题**：超级管理员在 Dashboard 和 OpenClaw 管理页面也看不到龙虾王了，因为 `_get_visible_claws()` 对所有用户都过滤了 `role='admin'` 的 claw。

**修改文件**：

1. **`web/app/api/dashboard.py`** `_get_visible_claws()` 函数：
   - 原：`return [c for c in all_claws if c.role != 'admin']` — 所有用户都隐藏 admin 角色
   - 新：super_admin 直接返回全部 claw，其他用户才过滤 admin 角色

2. **`web/app/api/auth.py`** `/auth/me` 接口：
   - 原：`'can_see_admin_claw': False` — 龙虾王所有用户都不可见
   - 新：`'can_see_admin_claw': is_sa` — 超级管理员可见龙虾王

3. **`web/templates/openclaws.html`**：
   - 原：`style="display:none !important"` — `!important` 导致 JS 无法控制显示
   - 新：`style="display:none"` — 去掉 `!important`，让 JS 可正常切换管理员区域显隐

### 3. 用例目录一级目录支持折叠

**修改文件**：`web/templates/testcases.html`

- 新增 `collapsedLibs` Set 存储折叠的用例库 ID
- 用例库名称行增加折叠箭头 `toggleLib()`，图标随折叠状态变化（📂/📁）
- 用例库下的子目录包裹在 `.tc-tree-sub` 容器中，折叠时添加 `collapsed` class

### 4. 脑图用例拖拽改用 transform 实现

**修改文件**：`web/templates/testcases.html`

- 原：`overflow:auto` + `scrollLeft/scrollTop` 拖拽，画布没有超出容器时拖拽无效
- 新：`overflow:hidden` + `transform: translate()` 平移画布，任何尺寸都可拖拽
- `mmDragStart` 解析当前 transform 偏移量作为起始点，拖拽时累加偏移

### 2. Skill #123 作者修改（已完成，直接数据库操作）

**操作**：将 Skill #123 (`test-case-generator`) 的 `created_by` 从 `system` 改为 `天飞小游戏助理小天`（对应 claw id=6）

**执行命令**：
```sql
UPDATE skills SET created_by='天飞小游戏助理小天' WHERE id=123;
```

**验证**：
```
id=123, name=test-case-generator, created_by=天飞小游戏助理小天
```

---

## 2026-04-16 部署完成（16:36）

### 1. Dashboard 报告 Markdown 渲染

**问题**：Dashboard "完成任务"弹窗中 `\n` 和 `###` 等 Markdown 标记显示为原始文本。

**修改文件**：`web/templates/dashboard.html`
- 新增 `_mdRender()` 函数：转义 HTML → 转换 `###`/`**` → `<strong>` → 转换 `*` → `<em>` → 转换 `\n` → `<br>`
- 对 `report.ai_summary`、`tasks_completed`、`knowledge_recorded`、`experience_shared` 统一应用 `_mdRender()`

### 2. Skills 卡片项目绑定

**问题**：绑定了项目的 Skill 刷新后显示"项目0"。

**修改文件**：`web/templates/skills.html`
- 页面初始化时增加 `loadProjectsAndModules()` 调用，确保 `allProjects` 在渲染时已有数据
- 删除重复的 `scopeLabel` 函数定义

### 3. OpenClaw 卡片待办数据修正

**问题**：OpenClaw 卡片待办数不准确；15:00 闹钟待办需隐藏。

**修改文件**：`web/app/api/dashboard.py`
- `today_tasks` 改为从 `ClawTodo` 计算待办数（过滤 interrupt + 15:00/15:30/21:00 闹钟）
- 新增 `today_completed` 字段（来自 DailyReport 的完成任务数）
- `upcoming_todos` 同样过滤闹钟类待办
- 前端显示改为 `待办 X · 已完成 Y`

### 4. 审核待办任务通知

**问题**：新 Skill/Rule 提交待审核时，龙虾王没有收到审核待办任务。

**修改文件**：`web/app/api/skills.py`、`web/app/api/rules.py`
- `pending_count` 扩展为包含 Skill + Rule 的待审核数量
- 非 admin 提交 Skill/Rule 时，为所有 admin 角色 OpenClaw 创建 ClawTodo（task_category='review', priority='P1'）
- 审核通过/拒绝时，自动关闭对应审核 ClawTodo 并更新 ClawTodoLog

---

## 2026-04-16 部署完成（17:15）

### 5. API 全局认证强制执行

**问题**：OpenClaw 不带 Token 也能调用 API 创建 Skill，导致 `created_by` 变为 `system`。

**修改文件**：`web/app/api/__init__.py`、`web/app/api/skills.py`、`web/app/api/rules.py`

- `__init__.py`：给 `api_bp` 添加 `before_request` 全局认证，所有 `/api/v1/` 请求必须携带 Bearer Token 或 Web session 登录
  - 公开路径白名单：`/api/v1/auth/login`、`/api/v1/auth/register`、`/api/v1/system-changelog`
  - Token 验证结果缓存 60 秒，避免每次请求遍历所有 claw
  - 无认证返回 401，Token 无效也返回 401
- `skills.py` 和 `rules.py`：`created_by` 不再默认 `system`，未认证时返回 401
  - Token 认证时自动从 claw 名称获取 `created_by`，不需要手动传
- SKILL.md 文档更新：`hub-connect`、`manager-hub`、`registration-init-tasks` 三个 Skill 都添加了醒目的认证说明

### 6. Skill #129 作者修正

**操作**：将 Skill #129 (`universal-test-case-standardizer`) 的 `created_by` 从 `system` 改为 `天飞小游戏助理小天`

```sql
UPDATE skills SET created_by='天飞小游戏助理小天' WHERE id=129;
```

---

*最后更新: 2026-04-16*
