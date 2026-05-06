# 长期记忆

## 项目：OpenClaw Agent Hub 通信中心
- **状态**: 已合并到 `f:/Code/claw_team/web/` 主系统
- **数据库**: 新增 Agent/Message/Conversation 模型到 `web/app/models.py`
- **API 路径**: `/api/v1/agent-hub/*`
- **Web 页面**: `/hub`（通信中心入口）
- **认证**: Bearer Token（OpenClaw 实例间通信用）
- **独立服务位置**: `f:/Code/claw_team/hub/`（已停用，可删除）
- **旧数据库**: `f:/Code/claw_team/hub/agent_hub.db`（不再使用）

## OpenClaw 子Agent Hub Skill
- **Skill 位置**: `C:/Users/rajqiu/.qclaw/workspace/skills/sub-agent-hub/`
- **功能**: 让 OpenClaw 通过子 Agent 与其他 OpenClaw 实例通信
- **核心文件**:
  - `SKILL.md` - Skill 主文档（给 AI 读的指令）
  - `scripts/hub_client.py` - Hub 通信客户端库
  - `scripts/sub_agent.py` - 子 Agent 核心类
  - `scripts/spawn_subagent.py` - 子 Agent 注册/生成工具
  - `references/API_REFERENCE.md` - API 完整文档
- **使用方式**: 在 OpenClaw 中说"使用子 Agent 给 xxx 发消息"
- **依赖配置**: `config/subagents.json`（配置 hub_url 和 agent 信息）

## 项目：OpenClaw 游戏测试经理角色工程
- **设计文档位置**: `C:/Users/rajqiu/.qclaw/workspace/FULL-DESIGN.md`（V3.0，2026-03-23）
- **工作仓库**: `f:/Code/claw_team`
- **现有文件位置**: `C:/Users/rajqiu/.qclaw/workspace/`（SOUL.md、roles/、skills/、knowledge/ 等已有框架文件）
- **核心目标**: AI 角色工程，把 OpenClaw 打造成专业游戏测试经理，支持团队共享和自我进化
- **知识库方案**: Memos + memos-mcp（替代 OpenViking），Docker host 网络模式，端口 5230
- **Memos 服务地址**: `http://9.134.11.169:5230`
- **memos-mcp**: Python 包，`uvx memos-mcp` 运行，环境变量 `MEMOS_URL` + `MEMOS_API_KEY`
- **MCP 配置位置**: `~/.workbuddy/mcp.json`
- **架构分工**: Git 管规范文件 + Memos 管团队经验知识
- **知识库标签**: #bug-pattern, #test-checklist, #performance-case, #project-lesson, #tool-usage, #test-summary
- **关键外部依赖**: Memos（知识库）、TAPD MCP Skills、OpenClaw 平台
- **当前阶段**: V4.0 设计完成，准备开发 Web 管理系统

## V4.0 重大升级方向
- **核心变化**: 从纯文档驱动升级为 Web 管理系统
- **技术栈**: Flask + MySQL + Memos + Docker Compose
- **新增能力**: 多 OpenClaw 实例管理、Skills 市场、工作日报上报、知识审核共享
- **知识库分级**: 三级作用域（global/project/module），Memos 标签组合实现
- **管理员 OpenClaw**: AI 扫描知识变化 → 人工确认 → 共享到指定范围
- **设计文档**: artifact 目录下 `openclaw-v4-design.md`
- **开发计划**: 5 个 Phase（基础框架→Skills市场→知识库管理→管理员+日报→Docker整合）

*最后更新: 2026-03-25*

## 部署信息

### 生产服务器 (openclaw-manager) — Hub / Web
- **SSH**: 9.134.11.169:36000, root/Test@speed2021（Hub 与 OpenClaw 管理端所在机）
- **MySQL**: booster/booster, 数据库 `openclaw_manager`
- **Web**: Flask 运行在 5000 端口，通过 nginx 代理到 8088
- **服务**: systemd `openclaw-web.service`
- **代码位置**: `/opt/openclaw-web/`
- **访问地址**: http://9.134.11.169:8088

### Hermes Agent 部署目标机（与 Hub 分离，2026-04-29 起）

与 `F:/Code/hermes agent/部署指南.md` **§一 服务器连接信息** 一致：**在 249 上跑 Agent**，**仍连 169 上的 Hub**。

| 角色 | 地址 | 说明 |
|------|------|------|
| **Hermes / systemd 目标机** | `9.134.51.249:36000` | SSH：`ssh -p 36000 -i ~/.ssh/id_9.134.51.249 root@9.134.51.249`；可选别名 `Host hermes-249` |
| **Hub（API / Web）** | `http://9.134.11.169:8088` | Agent `config.yaml` 里 `hub.base_url` / 环境变量 `HUB_BASE_URL` 指向此地址 |
| **连通性检查** | 在 249 上 `ping 9.134.11.169` | 确认 Agent 机能访问 Hub |

Hub「代建 Hermes」弹窗里 **目标机 host** 填 **249**（SSH 部署落点），**不是** 169。

### 部署方式
- 通过 AnyDev 云研发跳转：AnyDev IP 21.214.206.189 → 目标服务器 9.134.11.169
- AnyDev 命令执行：webshell → ssh 到目标服务器

### MySQL 数据库 (openclaw_manager)
当前已有 13 个表：
- agents, alembic_version, conversations, daily_reports
- knowledge_distributions, knowledge_entries, messages
- modules, openclaw_instances, openclaw_skills
- projects, skills, system_config

*最后更新: 2026-04-29（补充 Hermes 目标机 9.134.51.249 与 Hub 169 分工）*

## 问题修复记录

### 1. PUT /api/v1/system/config 返回 500 错误
- **日期**: 2026-03-25
- **原因**: `db.engine.connect()` 返回的 Connection 对象没有 `commit()` 方法
- **解决**: 改用 `db.session.execute()` 和 `db.session.commit()`
- **文件**: `/opt/openclaw-web/app/api/system.py`

### 2. /skills 页面返回 HTML 错误 "Unexpected token '<'"
- **日期**: 2026-03-25
- **原因**: skills 表在 MySQL 中不存在，只有 system_config 表
- **解决**: 创建迁移脚本从 SQLite 迁移所有表到 MySQL
- **迁移的表**: skills, conversation, document_knowledge, project, user_feedback

### 3. Skills API 返回 500 错误
- **日期**: 2026-03-25
- **原因**: MySQL 中 datetime 字段存储为 TEXT，`to_dict()` 方法直接调用 `.isoformat()` 失败
- **解决**: 在 models.py 的 to_dict() 方法中添加 isinstance 检查：
```python
self.created_at.isoformat() if isinstance(self.created_at, datetime) else self.created_at
```

### 4. agent_hub.py 装饰器 import 顺序问题
- **日期**: 2026-03-25
- **原因**: `@wraps` 装饰器使用 `from functools import wraps` 写在函数定义之后
- **解决**: 将 `from functools import wraps` 移到文件顶部（第5行）
- **文件**: `f:/Code/claw_team/web/app/api/agent_hub.py`

### 5. sed 命令产生重复循环
- **日期**: 2026-03-25
- **原因**: sed 替换时错误地在错误位置添加了循环
- **解决**: 使用 `sed -i "26d"` 删除重复的第26行

### 6. 其他页面（除 Skills 外）打开报错
- **日期**: 2026-03-26
- **原因**: MySQL 数据库缺少多个业务表（agents, conversations, messages 等），只有 skills 表迁移到 MySQL
- **症状**: Skills 页面正常，其他页面（如 Dashboard、Hub）返回 500 错误
- **解决**: 
  1. 连接远程 MySQL (9.134.11.169:3306)
  2. 创建缺失的表：agents, conversations, messages
  3. 迁移已有数据：projects (5行), openclaw_instances (1行), openclaw_skills (1行)
- **验证**: MySQL 现已有 13 个表，数据迁移完成

### 7. openclaw-web 服务无法启动 (ModuleNotFoundError: No module named 'click')
- **日期**: 2026-03-26
- **原因**: systemd 服务使用 /usr/bin/python3 (Python 3.6.8) 而不是 /usr/local/bin/python3
- **解决**: 更新 /etc/systemd/system/openclaw-web.service，使用 /usr/local/bin/python3

### 8. Dashboard API 返回 'str' object has no attribute 'isoformat'
- **日期**: 2026-03-26
- **原因**: MySQL 中 last_heartbeat 等字段存储为 TEXT 类型，代码调用 .isoformat() 方法失败
- **解决**: 在服务器上执行 sed 命令替换：
  - `sed -i "s/self.last_heartbeat.isoformat()/str(self.last_heartbeat)/g" /opt/openclaw-web/app/models.py`
  - `sed -i "s/c.last_heartbeat.isoformat()/str(c.last_heartbeat)/g" /opt/openclaw-web/app/api/dashboard.py`

### 9. daily_reports 表缺少 experience_shared 和 knowledge_learned 字段
- **日期**: 2026-03-26
- **原因**: MySQL 表结构与 models.py 定义不一致
- **解决**: `ALTER TABLE daily_reports ADD COLUMN experience_shared TEXT, ADD COLUMN knowledge_learned TEXT`

### 10. openclaws.py POST 返回 isoformat 错误
- **日期**: 2026-03-26
- **原因**: models.py 中 updated_at 字段的 isoformat() 调用没有 isinstance 检查
- **解决**: `sed -i "s/updated_at.isoformat()/isinstance(self.updated_at, datetime) and self.updated_at.isoformat() or self.updated_at/g" /opt/openclaw-web/app/models.py`

### 11. openclaw_instances 表结构错误（id 非自增，所有字段为 TEXT）
- **日期**: 2026-03-26
- **原因**: 从 SQLite 迁移数据时表结构不正确
- **症状**: `Duplicate entry '0' for key 'PRIMARY'`
- **解决**: 重建表结构，添加 AUTO_INCREMENT
- **龙虾王添加成功**: ID=4, api_token=`oc_tk_bb780b6341d4abcdbd9b012bb4a651ede6db441353060fa1`

## 部署经验总结

### git 操作安全规范（重要！）
在 gametool 周报系统（9.134.51.249）的正式环境中：
- `dist/` 目录是前端构建产物，被 nginx 直接引用
- `.gitignore` 中的 untracked 目录
- **禁止使用**: `git stash --include-untracked` 或 `git clean`
- **正确做法**: 
  - 只 stash tracked 文件（`git stash` 不带 --include-untracked）
  - 或用 `git checkout -- <file>` 针对性还原冲突文件，再执行 git pull

### SSH 连接问题
- 服务器使用 keyboard-interactive 认证，部分 AI 工具无法直接 SSH
- 可能需要手动操作或使用其他能 SSH 的客户端

### 12. 用例库共享 + 邀请评审功能（参考工程分析模块）
- **日期**: 2026-04-20
- **场景**: 给 `TestCaseLibrary` 加和工程分析一样的 share-grant + review 工作流
- **实现要点**:
  - **新表 2 张**（在 `app/__init__.py` `with app.app_context()` 段用 `CREATE TABLE IF NOT EXISTS` 幂等迁移，不走 Flask-Migrate）：
    - `test_case_library_shares`：`share_type=user/claw/public`，`permission=readonly/reviewer/editor`，`granted_by`、`expires_at`、`note`
    - `test_case_library_reviews`：`status=submitted/approved/rejected/withdrawn`，`scope_summary`、`submit_note`、`decided_by`、`decision_note`、`related_topic_id`
  - **`test_case_libraries` 加 3 个字段**：`review_status` (draft/pending_review/approved/rejected)、`current_review_id`、`review_status_at`，全部用 `ALTER TABLE ADD COLUMN` 幂等执行
  - **API 12 个**（`app/api/testcases.py`）：5 share + 6 review + 1 detail，每个写操作都 `log_action`
  - **权限关键**：`_ensure_library_access(write=False)` 改成"作者 / super_admin / 项目成员 / 已被 share 的用户/claw / public"五选一；写操作只放给作者+super
  - **跨模块联动**：`app/api/topics.py` 的 `create_topic` 在创建 `case_review` topic 时调用 `_ensure_library_access`，让被共享人也能发起评审课题
  - **前端**：`templates/testcases.html` 在树节点旁加 👥 / 📝 按钮 + `共享给我` / `共享 N` / `评审中` 等徽章，弹窗用 `text input` 填 user_id/openclaw_id（和 engineering.html 风格一致）
- **坑 / 教训**:
  - **本地 `models.py` 缺 `EngineeringShare` 类**：远程已部署但本地源码没合回来，导致本地 `from app import create_app` 直接 `ImportError`。临时验证可用 `import app.models as M; M.EngineeringShare = type('EngineeringShare', (), {})` 注入桩对象再导入 `app.api.testcases` 验证 endpoint。**部署到远程时不会受影响**，因为远程的 models.py 是完整的。
  - 验证新 endpoint 的快速法：用 stub 后 `[r for r in app.url_map.iter_rules() if 'testcase-librar' in str(r) and ('share' in str(r) or 'review' in str(r))]`
- **远程部署**：把 `app/models.py`、`app/__init__.py`、`app/api/testcases.py`、`app/api/topics.py`、`templates/testcases.html` 5 个文件覆盖到 `/opt/openclaw-web/`，重启 `openclaw-web` 服务即可（迁移会在 startup 自动跑）

### 13. 重大教训：本地源码可能与生产不同步，禁止直接覆盖关键模型文件
- **日期**: 2026-04-23
- **场景**: 部署用例库共享功能时差点把生产 `EngineeringShare` 类删除
- **根因**: 本地 `web/app/models.py` 中之前的某次开发 **in-place 改写**了 `EngineeringShare` 类（替换为 `TestCaseLibraryShare` 命名），同时还删除了 `EngineeringArchitectureSnapshot.project_id` 字段。生产 `models.py` 是完整的（4月23日 15:09 更新），本地反而是退化版本。
- **关键 diff 信号**：`git diff --no-index --stat prod local` 看 deletions：
  - `__init__.py`：85 insertions, **0 deletions** ✅ 安全直接覆盖
  - `topics.py`：13 insertions, **0 deletions** ✅ 安全
  - `testcases.html`：368 insertions, **0 deletions** ✅ 安全
  - `testcases.py`：607 insertions, 6 deletions（确认是合法函数签名替换） ✅
  - `models.py`：115 insertions, **30 deletions** ⚠️ 必须以 prod 为基准 patch
- **正确部署流程**（已落地为标准 SOP）：
  1. **先拉**：`scp` 把生产 5 个文件 → `_remote_compare/<feature>/prod_*.{py,html}`
  2. **diff stat**：`git diff --no-index --stat prod local`，**任何 deletions > 0 都要逐行 inspect**
  3. **deletions 安全的文件**：直接复制本地版本到 `_deploy_<feature>/`
  4. **deletions 危险的文件**：`Copy-Item prod → _deploy/`，再用 `StrReplace` 把本地新增的字段/方法 patch 到 prod 副本上（保留 prod 已有的全部内容）
  5. 远端 deploy.sh 必须含：备份 → 解压 → AST 校验 → import 校验（`from app import create_app` + `from app.models import 关键类`）→ restart → 烟测
- **OpenClaw API token 取法**（坑过多次，记牢）：
  - ❌ `inst.api_token_plain` 字段名虽叫 plain 但实际是密文
  - ❌ `inst.get_api_token()` 不存在
  - ✅ **`inst.get_token_plain()`** 才是返回 `oc_tk_<48 hex>` 明文的方法（54 字符）
  - 所以服务器烟测 token 取法：
    ```python
    inst = OpenClawInstance.query.filter_by(role='admin').first()
    token = inst.get_token_plain()   # 不是 get_api_token / api_token_plain
    ```
- **PowerShell SSH 嵌套执行 Python 的坑**：
  - 在 `ssh root@host "..."` 里嵌 `\`...\`` 反引号 + Python 多行代码，PowerShell 会先把整个内容当成自己的脚本解析，导致 `from`、`(` 等被当作 PowerShell 关键字报错
  - **唯一可靠做法**：把 Python 代码写到 `.py` 文件 → `scp` 上去 → `ssh "cp /tmp/x.py /opt/openclaw-web/_x.py && cd /opt/openclaw-web && venv/bin/python3 _x.py; rm _x.py"`（必须 cp 到项目根再 cd 执行，否则 `from config import config` 会 ModuleNotFoundError）
- **本次部署产物 / 回滚信息**：
  - tarball: `_deploy_tcl_share.tgz`（65KB，含 5 文件）
  - 部署脚本: `_deploy_tcl_share/deploy.sh`
  - 烟测脚本: `_deploy_tcl_share/smoke_test.sh`
  - 远端备份: `/tmp/openclaw_backup_tcl_share_20260423_174633/`
  - 回滚: `cp -rp /tmp/openclaw_backup_tcl_share_20260423_174633/app /opt/openclaw-web/ && cp -p /tmp/openclaw_backup_tcl_share_20260423_174633/templates/testcases.html /opt/openclaw-web/templates/ && systemctl restart openclaw-web`
  - 验证：11 个新 endpoint 全部注册，`test_case_library_shares`/`test_case_library_reviews` 表自动建出，HTTP 烟测全 ✅

---

## 14) 用例库评审 v2：按子目录评审 + 并行 pending（2026-04-23 同日二次发布）

### 业务场景
v1 版本评审只能"整库一起评"，业务上要求：
- 不同子目录可以独立发评审（如「登录/手机号登录」56 条）
- 同一库内允许多个 pending review **并行存在**（v2.0 / v2.1 子模块同步推进）
- 列表/树节点上能看到「哪个子目录在评审中」的徽章

### 关键设计决策
- **并发模型**：放开"全库唯一 pending"限制，唯一性约束变为 `(library_id, scope_type, scope_module_path)`
- **library.review_status 语义弱化**：保留作为「最近一次/全局粗略指示」，准确状态以 GET 返回的 `pending_reviews` 数组为准
- **`_set_library_review_status` 关键改造**：approve/reject/withdraw 时若库内仍有其他 pending，就**强制保持 pending_review 不变**，只有最后一个 pending 处理完才真正切到 approved/rejected/draft（避免状态被反复覆盖）
- **scope 字段 3 件套**：`scope_type`(library/module/cases) + `scope_module_path`(VARCHAR 500) + `scope_case_count`(INT，发起时快照)
- **case_count 自动统计**：后端 `TestCase.module_path == prefix OR LIKE prefix||'/%'`，前端实时按 `casesCache` 同步预览

### 部署产物
- tarball: `_deploy_tcl_share_v2.tgz`（69KB）
- 部署脚本: `_deploy_tcl_share/deploy_v2.sh`
- 烟测脚本: `_deploy_tcl_share/smoke_test_v2.sh`
- 远端备份: `/tmp/openclaw_backup_tcl_share_v2_20260423_180650/`
- 回滚命令同上一节，路径换 v2 时间戳即可

### 烟测覆盖（7 场景全过）
1. 整库评审发起 → scope_summary 自动生成「整库（N 条）」 ✓
2. 子目录评审并行发起 → scope_summary 自动生成「子目录 X（N 条）」 ✓
3. 同一库 2 个不同 scope 的 pending 并存 ✓
4. 同 scope 重复发起 → 409 + current_review_id 提示 ✓
5. `GET /testcase-libraries` 列表返回 `pending_reviews` 数组 ✓
6. **关键**：通过子目录评审时 library.review_status **保持 pending_review**（因为整库评审还在）✓
7. **关键**：通过最后一个 pending 才真正切到 approved ✓

### 经验沉淀
- **db.or_ vs or_**：`testcases.py` 顶部已经 `from sqlalchemy import desc, or_`，直接用 `or_` 不要写 `db.or_`（lint 不报但运行时报错）
- **历史脏数据回填**：新加 `scope_type` 列后必须 `UPDATE ... SET scope_type='library' WHERE scope_type IS NULL OR scope_type=''`，否则旧 review 在新代码里 filter_by(scope_type='library') 会查不到
- **autoflush 信任**：approve 端点里先 `review.status='approved'` 再调 `_set_library_review_status` 内 query `status='submitted'`，SQLAlchemy autoflush 会自动 flush 当前 session 改动，count 是准确的
- **小步增量**：v1 + v2 同日发布时，v2 的 `_deploy_<feature>/` 目录里的文件已经包含 v1 的全部改动（基于 v1 staged 版本继续 patch），不需要再次和生产做 diff（v1 部署完后那些文件 = 当前生产状态）

---

## 15) Skill 同步到 Hub + 一键给所有装机的 claw 重分配（v2 内容下发标准 SOP）

### 触发场景
代码层面新加了 API endpoint / 字段，对应的 SKILL.md 也必须同步更新到 hub DB，否则 OpenClaw 实例还在用旧的指令书，根本不知道有新能力。

### 标准三步走
```
1. 改本地 SKILL.md（openclaw-agent/skills/<name>/SKILL.md）
2. 写 sync_<name>_skill.py（参考 _deploy_eng_arch/sync_engineering_skill.py 模板）：
   - 读本地 .md → UPSERT skills 表（按 name 唯一）
   - 校验关键字必须出现在 template_content 里（防止上传错误版本）
   - 列出已安装的 claw + STALE 标记
3. 写 reassign_<name>.py（可选但强烈推荐）：
   - 取 admin claw 的 token (get_token_plain)
   - 对所有已装的 claw 调 POST /api/v1/openclaws/{claw_id}/skills { skill_id }
   - hub endpoint 自动刷新 installed_at + 下发 "重新拉取" todo
```

### 关键 API
| 用途 | 端点 | 说明 |
|---|---|---|
| 安装/重装 skill 给 claw | `POST /api/v1/openclaws/{claw_id}/skills` body=`{skill_id}` | 已存在会 reinstall（reason=content_updated/forced），刷新 installed_at + 下发 todo |
| 卸载 | `DELETE /api/v1/openclaws/{claw_id}/skills/{skill_id}` | 慎用，参考第 12 条小赫 7 条无效卸载教训 |

### 坑 & 经验
- **必须 cp 脚本到 `/opt/openclaw-web/` 项目根再执行**，否则 `from config import config` ModuleNotFoundError（第 13 条记过，再确认一次）
- **同 session 二次查 STALE 是假阳性**：reassign 脚本里调完 install_skill API 后，再用同 db.session 查 `installed_at` 还是旧的。原因是 install_skill 在自己的 transaction 里 commit，但脚本里的 ORM session 缓存了旧实例。**正确验证方式**：用独立的 mysql 客户端（写 `.sql` 文件 scp 上去执行）或者 `db.session.expire_all()` + 重新 query。
- **PowerShell 调 mysql -e 执行带括号的 SQL 会被 PS 误解析为表达式**：解决办法是把 SQL 写到 `.sql` 文件，scp 上去用 `mysql < file.sql` 执行，避免任何转义。
- **多个 STALE 的 claw 不要一个个手动操作**：写一个 reassign 脚本批量循环，几秒钟搞定。本次 7 个 claw 一次成功，HTTP 201 全绿。
- **被邀请评审 / 受共享的 claw 看到的 GET /testcase-libraries 字段**：`shared_with_me=true` + `my_share_permission=reviewer` + `can_review=true`。SKILL 文档要明确这些字段含义，否则 LLM 无法判断是否能调 approve。

### 本次产物
- 本地 skill: `openclaw-agent/skills/testcase-manager/SKILL.md`（21853 字符，对比旧版 11K）
- 同步脚本: `_deploy_tcl_share/sync_testcase_manager_skill.py`（含 28 个 v2 关键字校验）
- 重分配脚本: `_deploy_tcl_share/reassign_testcase_manager.py`（一键给所有 claw 重装）
- 验证工具: `_deploy_tcl_share/check_stale.sql`（独立 SQL 查 FRESH/STALE）
- 已分发: 7 个 claw 全部 FRESH（claw#4/6/7/8/9/10/11）

*最后更新: 2026-04-23（同日二次发布 v2 + skill 同步分发）*

---

## 16) Skill/Rule 编辑按钮拦截 bug + last_modified_by 设计 + OpenClaw 自助回写 SOP

### 用户反馈（2026-04-24）
> "怎么无法修改自己创建的skill？另外skill没显示最后修改人是谁或者是openclaw"

### 根因
**两个独立问题叠加：**

1. **前端拦截 bug（可怕）**：`templates/skills.html` / `rules.html` 的"编辑"按钮渲染条件是
   `(s.review_status === 'approved' || s.review_status === 'revise') && _canEditSkill(s)`
   ——把所有 `pending` 状态都拦在外面。但后端 `update_skill` 完全允许 created_by 在 `pending` 时继续修改（会更新 mirror_content）。结果：用户点不到按钮 ⇒ 误以为"无权修改"，其实是 UI 层欺骗。

2. **数据字段缺失**：模型只有 `created_by` + `mirror_updated_by` 两个署名字段。
   - `created_by`：永远是首次创建人（不变）
   - `mirror_updated_by`：只在"待审核镜像"存在时有值，approved 后清空
   - **缺少**：审核通过后到底是谁、什么时候、从 web 还是 openclaw 改的 ⇒ 没法回溯责任

### 修复方案
| 位置 | 改动 |
|---|---|
| `models.py` Skill+Rule | 新增 `last_modified_by` (varchar 100) / `last_modified_at` (datetime) / `last_modified_source` (enum web/openclaw/system) |
| `__init__.py` | 6 个 `ALTER TABLE ADD COLUMN IF NOT EXISTS` 幂等迁移 |
| `api/skills.py` `update_skill` | 不论走 super_admin 直改还是 mirror_content 镜像，都写入 last_modified_*；source 根据是否带 Bearer Token 判断 web/openclaw |
| `api/skills.py` `review_skill` | approved 合入镜像时也写 last_modified_*（推断来源 by claw name 是否在 OpenClawInstance 表） |
| `api/rules.py` | 同上对称处理 |
| `templates/skills.html` `_modifierTag(s)` | 新增 helper：mirror 待审核 ⇒ "✏️ {who} 待审核"；否则按 source 显示 🤖(openclaw) / ⚙️(system) / 👤(web) + last_modified_by + 时间 |
| `templates/skills.html` 编辑按钮 | 条件改为 `s.review_status !== 'rejected' && _canEditSkill(s)`，pending 状态加 title 提示"修改将更新待审核镜像" |
| `templates/rules.html` | 镜像两处改动 |

### 部署策略：远端 in-place 增量 patch（不再整文件覆盖）
**背景**：本次本地分支严重落后线上（线上 EngineeringShare 已合入但本地没拉），整文件覆盖必删功能。

**SOP**：
1. 写 `patch_inplace.py`，每个 patch 三元组 `(marker, search, replace)`：
   - 已含 marker → skip（**幂等**，二次运行不报错）
   - 含 search → replace
   - 都没找到 → "anchor not found" 退出
2. 用 `pathlib.shutil.copy2` 自动备份到同目录下 `.bak.{ts}`
3. `scp` 到远端 `/tmp/` 直接 `python3` 执行 → 6 个文件 15 处 patch 一次完成

**坑**：
- **不要在 `search` 里塞 f-string 或 emoji 转义**：JS 风格的 `\u{1F916}` 在 Python 里是 SyntaxError，必须直接写 emoji 字符串
- **HTML 里的字面 `\n`**：原文件如果含字符串字面量 `\n`（不是真换行），patch 脚本必须用 raw string `r"..."` 否则匹配失败
- **marker 必须用 patch 后才出现的字面字符串，不能 f-string 化**：否则二次运行会报 anchor not found（第一次成功了 search 已不存在，但 marker 又匹配不上 f-string）

### OpenClaw 自助回写 SKILL.md（重要纠正！）
**之前以为**：本地改 SKILL.md 后只能让用户在 Hub 后台手动复制粘贴。
**实际上**：标准 API 完整支持自助：

```
PUT /api/v1/skills/{SKILL_ID}
Header: Authorization: Bearer {OPENCLAW_API_TOKEN}
Body: {"template_content": "<新 SKILL.md 完整内容>", ...其它可选字段}
```

权限矩阵 `_can_edit`：
- super_admin OpenClaw（龙虾王）→ 直改生效
- 普通 OpenClaw `created_by == claw_name` → 写入 mirror_content + review_status=pending，触发管理员审核
- 其他 → 403

**已写入 hub-connect SKILL.md**（id=124）的"修改 Skill / Rule（OpenClaw 自助回写 SKILL.md）"章节，含权限矩阵 + 工作流 + 自查脚本。OpenClaw 下次按 stale 自检拉到新版本就知道这条能力。

### 烟测两套
| 脚本 | 角色 | 验证点 |
|---|---|---|
| `smoke.sh` | 龙虾王 super_admin token PUT skill 128 description | 直改生效，last_modified_by=龙虾王/source=openclaw，review_status 不变 |
| `smoke_condibot.sh` | condibot 自己创建的 skill PUT trigger_phrase | mirror_content 路径，mirror_updated_by=condibot/last_modified_by=condibot，review_status=pending |
| `verify_124.sh` | hub-connect API GET | template_content 含新章节 + last_modified_* 字段全部回填 |

### 关键经验
1. **遇到"用户说没权限"，先怀疑前端按钮拦截，再怀疑后端 403**：本次根本不是后端拒绝，是前端不渲染。永远先看 `_can_*` helper 在 HTML 里的实际调用条件。
2. **`created_by` 永远不要被覆盖**：它是历史问责字段，所有"最后修改人"语义必须用独立字段 `last_modified_by`，否则会丢失"谁创建的 vs 谁最后改的"区别。
3. **source 字段要枚举死**：`web` / `openclaw` / `system` 三类——web 是用户、openclaw 是 AI、system 是脚本/迁移自动写。UI 用三个 emoji 一眼区分。
4. **hub-connect 是 off-shelf skill**（`OFF_SHELF_SKILL_NAMES = {'hub-connect'}`），不能通过 `POST /openclaws/{cid}/skills` 重分配，会返 403"已下架"。但 Hub DB 里的 template_content 改了，OpenClaw 下次 stale 自检拉新内容就行。
5. **PowerShell SSH 必须用 .ssh/config 别名**（本机配了 `Host testserver`），直接写 `ssh -p 36000 root@9.134.11.169` 会因密钥不匹配 Permission denied。
6. **PS 调 ssh 远端 mysql -e 带 `()` 或中文必炸**：一律 `.sql` 文件 → scp → `mysql < file.sql`。

### 本次产物
- 后端：`models.py` / `__init__.py` / `api/skills.py` / `api/rules.py`（远端 in-place patch）
- 前端：`templates/skills.html` / `templates/rules.html`（远端 in-place patch）
- 部署脚本：`_deploy_lastmod/patch_inplace.py` + `patch_rules_modifier.py` + `verify.sh` + `smoke.sh` + `smoke_condibot.sh`
- Skill 同步：`openclaw-agent/skills/hub-connect/SKILL.md`（8183 → 10892 字符）+ `_deploy_lastmod/sync_hub_connect.py` + `verify_124.sh`
- DB 迁移落地：skills + rules 各 3 个新列已就位

*最后更新: 2026-04-24（last_modified_by 字段 + 编辑按钮放开 pending + hub-connect 自助回写章节）*

---

## 17) 不要信任客户端传的 source_openclaw_id（Agent 不知道自己 ID 会 hardcode 错坑）

### 用户反馈（2026-04-25）
> "小赫提交了一篇知识库，服务端接口协议测试用例设计方法论（OpenClaw 主导模式），但是提交者显示未知agent"

### 根因（双重 bug）
1. **小赫不知道自己的 claw_id**，调 `POST /api/v1/knowledge` 时 hardcode 了 `source_openclaw_id=1`（猜的或者抄了 skill 文档里的示例数字）
2. **Hub DB 里 claw_id=1 不存在**（最早 4 = 龙虾王，1~3 是历史已删除）→ FK 失效
3. **后端 `create_knowledge` 不校验**：从 body 拿到啥就写啥，不验证 source_openclaw_id 是否真实存在
4. **`KnowledgeEntry.to_dict()` LEFT JOIN 不到 → `source_openclaw_name=None`**
5. **前端兜底文案 `'未知 Agent'`**：完全看不出错在哪一层

### 数据现场
```
SELECT id, title, source_openclaw_id FROM knowledge_entries WHERE id=4;
-- id=4, source_openclaw_id=1 (claw 不存在)
SELECT MIN(id), MAX(id) FROM openclaw_instances;
-- MIN=4 (龙虾王), 1~3 已 deleted（不在表里）
```

### 修复方案：服务端**强制反推**，不信任客户端
**新增 `_get_current_openclaw()` helper**：从 `Authorization: Bearer ...` 反查 `OpenClawInstance`，返回 claw 实例本身（不是 owner User）。

**`create_knowledge` / `batch_import_knowledge` 改造**：
```python
caller_claw = _get_current_openclaw()
if caller_claw:
    # OpenClaw 调用：忽略 body 里的 source_openclaw_id，强制覆盖
    source_openclaw_id = caller_claw.id
    source_type = 'openclaw'
else:
    # Web 用户调用：按 body 走（默认 manual）
    source_openclaw_id = data.get('source_openclaw_id')
    source_type = data.get('source_type', 'manual')
```

**前端容错** `getSubmitterInfo`：分三档显示
- `source_openclaw_name` 有 → 显示名字
- `source_openclaw_id` 有但名字空 → `Agent #ID (已删除)` ← 至少能看出哪个 ID 失效
- 都没有 → `未知 Agent (无 ID)` ← 真正的"客户端没传"

**Skill 文档校正** `knowledge-manager/SKILL.md`：
- 抖掉所有 `"source_openclaw_id": <CLAW_ID>` 和 `"source_openclaw_id": 4` 示例
- 加 ⚠️ 注释："不要传，后端从 token 自动反推。传了也会被覆盖"
- 历史教训写进 skill 让所有 OpenClaw 看得到

### 数据回填
```sql
UPDATE knowledge_entries SET source_openclaw_id = 10 WHERE id = 4 AND source_openclaw_id = 1;
```
回填后 entry 4 显示为 `Hermes Agent小赫`（claw_id=10）。

### 烟测验证（用 condibot token 故意传错）
请求体 `source_openclaw_id=99 source_type=manual` →
返回 `source_openclaw_id=9 source_type=openclaw source_openclaw_name=condibot`
✅ 后端硬保护生效，客户端的谎言无效。

### 关键经验
1. **凡是写 created_by / source_*_id / submitter 的字段，永远从 token 反推，不要信 body**。
   客户端（特别是 LLM Agent）会 hardcode、复制粘贴示例数字、瞎填、忘填——任何能想到的错法都会出现。
2. **Agent 真的不知道自己的 ID**。Skill 文档里写 `<CLAW_ID>` 占位符是反模式：LLM 看到要么忽略要么填示例值。**正确姿势**：让后端反推，skill 文档明确告诉 Agent "你不需要传"。
3. **FK 字段一定要前端容错**：`x_id` 有值但 `x_name` 空 = FK 失效，要明确显示 "已删除/不存在 (#ID)"，不要简单 fallback "未知"——会掩盖真问题导致排查困难。
4. **类似坑要排查**：所有 `source_openclaw_id` / `created_by` / `submitter_id` / `author_id` 这种 FK + 名字派生字段，都要审一遍是否硬保护。本次只查了 knowledge.py，memos_api.py 是从 token 反推的没问题；其它 API（report、todo、distribute）后续遇到时再补。

### 部署产物
- `app/api/knowledge.py` 新增 `_get_current_openclaw()` + 改造 2 个 endpoint
- `templates/knowledge.html` `getSubmitterInfo` 三档容错
- `openclaw-agent/skills/knowledge-manager/SKILL.md` 抖掉错误示例（9496 → 9828 字符）
- `_deploy_lastmod/deploy_knowledge_fix.sh` + `backfill_entry4.sql` + `smoke_knowledge.sh` + `sync_knowledge_manager.py`
- 已分发：8 个装了 knowledge-manager 的 claw 全部 stale，下次自检会拉新

*最后更新: 2026-04-25（source_openclaw_id 服务端强制反推 + Skill 抖错示例）*

---

## 18) 评审中心多轮评审记录（review_comments 多态表 + Web 用户暂不站内信决策）

### 用户痛点（2026-04-25）
> "评审中心的审核要增加评论，可能不是一次评审通过，需要整改，那就要增加一个状态：通过 / 整改 / 废弃，并且可以有评论。提交者可以收到评审通知以及可以获取评审记录。"

### 现状梳理（改造前）
| 模块 | 状态字段 | 评审意见字段 | 历史记录 | 整改 | 通知提交人 |
|---|---|---|---|---|---|
| Skill | review_status enum approved/pending/revise/rejected | review_comment 单字段被覆盖 | 无 | 后端有，前端没暴露 | 已有（仅 OpenClaw） |
| Rule | 同 Skill | 同 Skill | 无 | 同 Skill | 同 Skill |
| Knowledge | status enum draft/pending_review/approved/rejected | reviewer_notes 单字段 | 无 | **没有 revise** | **没接通知** |
| TestCaseLibrary | 独立 reviews 表 submitted/approved/rejected/withdrawn | decision_note 单字段 | 一次评审一行 | 无 | 已有 |

核心问题：
1. `review_comment` 单字段每次审核都被覆盖，**多轮评审完全无法追溯**。
2. Knowledge 缺 `revise` 枚举，且审核完全没接通知。
3. 提交人没有"我的提交进度 + 评审记录"入口。

### 设计决策（已与用户确认）

#### A. 用统一多态表 `review_comments`（不是给每个模块加 reviews 表）
```sql
CREATE TABLE review_comments (
    id INT PRIMARY KEY AUTO_INCREMENT,
    resource_type ENUM('skill','rule','knowledge','testcase_library') NOT NULL,
    resource_id INT NOT NULL,
    parent_review_id INT,                              -- 仅 TCL 用，关联 test_case_library_reviews.id
    action ENUM('submit','approve','revise','reject','withdraw','comment') NOT NULL,
    from_status VARCHAR(30) DEFAULT '',
    to_status VARCHAR(30) DEFAULT '',
    content TEXT,                                       -- 评审意见 / 整改要求 / 提交说明
    author VARCHAR(100) DEFAULT '',
    author_type ENUM('user','openclaw','system') DEFAULT 'user',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX ix_rc_resource (resource_type, resource_id),
    INDEX ix_rc_resource_created (resource_type, resource_id, created_at),
    INDEX ix_rc_author (author),
    INDEX ix_rc_action (action)
);
```
**关键设计点**：
- `resource_id` **不加真 FK**，避免删除资源时级联 / 跨表事务问题，应用层校验。
- `parent_review_id` 兼容 TCL 老的 `test_case_library_reviews` 表（保留 decision_note 不动）。
- `action='submit'` 对应"提交人重新提交"，状态机可视化（revise → pending）。

#### B. 通知策略：只走 OpenClaw（Web 用户暂不站内信）
- 提交人 = OpenClaw → 复用 `_notify_submitter_review_result`（ClawMessage）
- 提交人 = Web 用户 → **不主动推送**，靠"我的提交"Tab 自取
- 理由：站内信表 + 已读未读 + 推送通道 是个独立坑，本次不开。后续真有需要再加 UserNotification 表。

#### C. KnowledgeEntry.status enum 加 'revise'
用 `ALTER TABLE ... MODIFY COLUMN`（不是 DROP/ADD），存量数据零影响：
```sql
ALTER TABLE knowledge_entries MODIFY COLUMN status
ENUM('draft','pending_review','approved','revise','rejected') DEFAULT 'draft';
```

### 实施清单（11 个 todo 全绿）
1. **models.py** 加 `ReviewComment` 类 + KnowledgeEntry status enum 加 revise
2. **app/__init__.py** 加 `CREATE TABLE review_comments` + `ALTER knowledge_entries.status`（幂等）
3. **新建 `app/api/review_comments.py`**：
   - `POST /api/v1/review-comments` 纯评论
   - `GET /api/v1/review-comments?resource_type=&resource_id=` 完整时间线
   - `GET /api/v1/review-comments/my-submissions` 我的提交聚合
   - `add_review_comment(...)` helper 给 review_skill/rule/knowledge/tcl 调用
4. **app/api/__init__.py** import 链加 `review_comments`（不加路由不注册！）
5. **review_skill / review_rule** 改造：
   - 每次审核 → 写一条 `ReviewComment(action=approve/revise/reject)`
   - 提交人重新提交（PUT skill/rule）→ 写一条 `action='submit'`
   - 通知扩展到 `(revise, rejected, approved)`（之前只 revise 通知）
6. **review_knowledge** 改造：
   - body 接受 `action='approve'|'revise'|'reject'` + `comment` 字段
   - 新增通知钩子（之前完全没接）
   - 提交人 PUT 更新内容时 `revise → pending_review` 自动流转 + 写 submit 记录
7. **TCL approve/reject/withdraw** 各写一条 ReviewComment，`parent_review_id=tcl_review.id`
8. **templates/review.html 大改**：
   - 新增"打回整改"按钮（橙色）
   - 评审意见模态框（整改/废弃必填）
   - "🕒 评审记录"按钮展开时间线
   - 新 Tab "↩️ 整改中" 和 "📤 我的提交"

### 部署 SOP（沿用 Section 16 in-place patch）
1. 先 `scp testserver:/opt/openclaw-web/<file>` 把 prod 当前版本拉回作 patch 基准
2. 本地 staging 目录改完 → tar.gz → scp → 远端解包 → AST 校验 → cp 覆盖 → systemctl restart
3. **CRLF 必须 sed**：`find . -type f \( -name '*.py' -o -name '*.html' \) -exec sed -i 's/\r$//' {} \;`
4. 验证 SQL 用 `.sql 文件 → mysql < file`，不要 `mysql -e "..."`（PowerShell 转义会炸）
5. **MySQL 凭据**：`/opt/openclaw-web/.env` → `booster:booster@openclaw_manager`（不是 openclaw / openclaw_db）
6. **claw_messages 表字段**：`msg_type / sender_name / content`（没有 type / title 字段）

### 烟测脚本（一键全链路）
```bash
condibot 提交 skill → 龙虾王 revise + 评论 → ClawMessage 入库
  → condibot PUT 内容（auto reset to pending + submit 记录）
  → 龙虾王 approved → 4 条 ReviewComment 入库
  → GET /review-comments 时间线返回 revise → submit → approve
  → GET /review-comments/my-submissions 返回 comment_count=3, latest=approve
```

### 关键经验
1. **多态表的 FK 处理**：`resource_id` 不加真 FK，应用层 `_resource_exists()` 校验。删资源时不级联，避免事务复杂度。
2. **Author 字段永远不信任 body**：从 session/token 反推（同 Section 17 教训）。
3. **状态枚举扩展用 MODIFY 不用 DROP**：MariaDB `ALTER TABLE ... MODIFY COLUMN ... ENUM('a','b','c',NEW)` 对存量数据零影响。
4. **小型新功能不要再"开发独立 reviews 表"**：4 个模块都要"评审"时，统一表（多态）比 4 张表清爽得多，且天然支持"我的提交"这种跨模块聚合。
5. **add_review_comment(commit=False)**：让调用方控制事务边界，避免双重 commit 引发的 race condition。
6. **`renderTimeline` 前端**：用 `border-left:3px solid <action_color>` + 等宽时间戳，比真"时间线 SVG"轻量好看 80%。
7. **review_comment 字段保留**：老的单字段 `review_comment` 不删（前端已经用了），新表只是补充时间线，迁移零成本。

### 本次产物
- 后端：`models.py` / `app/__init__.py` / `app/api/__init__.py` / `app/api/skills.py` / `app/api/rules.py` / `app/api/knowledge.py` / `app/api/testcases.py`
- 新文件：`app/api/review_comments.py`（API + add_review_comment helper）
- 前端：`templates/review.html`（完全重写：5 Tab + 模态框 + 时间线）
- 部署：`_deploy_review/deploy.sh` + `verify.sh` + `smoke.sh`
- DB：`review_comments` 表创建（含 4 个索引）+ `knowledge_entries.status` enum 扩 revise

### 路由清单（部署后）
```
/api/v1/review-comments                    GET / POST
/api/v1/review-comments/my-submissions     GET
/api/v1/skills/<id>/review                 POST  ← 已支持 approve/revise/reject + 写 ReviewComment
/api/v1/rules/<id>/review                  POST  ← 同上
/api/v1/knowledge/<id>/review              POST  ← 已加 revise + 通知
/api/v1/testcase-libraries/.../approve|reject|withdraw  POST  ← 已写 ReviewComment(parent_review_id)
```

### Skill 文档同步（2026-04-25 补完）
原计划 D 步说"知识层不需要新 skill"，实际上有 3 个 skill 必须更新告诉 OpenClaw 新流程：
1. **knowledge-manager**（skill_id=112，9828 → 10895 chars）：补 `action: 'approve'/'revise'/'reject'` + `comment` 字段 + 提交人 PUT 内容自动 revise→pending_review + review-comments 时间线 API + my-submissions
2. **hub-connect**（skill_id=124，12795 → 12988 chars）：新增"评审流程 v2"整章，覆盖状态机、resubmit_note、3 种结果都通知、review-comments / my-submissions 接口、纯评论接口
3. **testcase-manager**（skill_id=100，21853 → 23227 chars）：在评审状态机后补"评审记录时间线 v2.1" 段，说明 4 模块通用接口 + parent_review_id 关联回 test_case_library_reviews.id

**同步 SOP**（沿用 Section 16）：
1. `scp` 3 个 SKILL.md 到 `/tmp/<name>-SKILL.md`
2. 一个 Python 脚本 UPSERT 到 Hub DB（同时校验 must_have_keywords 防同步错文件）：
   ```bash
   python3 /tmp/sync_review_skills.py
   ```
3. **不需要手动 reassign**：`Skill.updated_at` 自动 onupdate=now，`OpenClawSkill.is_stale = installed_at < skill.updated_at` 会被自动判定，下次 OpenClaw 心跳/sync 时就拉新版本
4. 用 SQL 验证 stale 状态：
   ```sql
   SELECT i.name, os.installed_at, s.updated_at,
          CASE WHEN os.installed_at < s.updated_at THEN 'STALE' ELSE 'FRESH' END
   FROM openclaw_skills os JOIN skills s JOIN openclaw_instances i WHERE s.name=...
   ```

**影响范围**：8 个 OpenClaw 实例（4 龙虾王、6 小天、7 小安、8 小文、9 condibot、10 小赫、11 小云、12 小马）全部 stale，下次自检拉新。

### 经验补充（skill 同步层）
8. **改了后端流程一定要回头检查 SKILL.md**：本次改完 review_knowledge 加了 `revise` action，但 knowledge-manager skill 还在告诉 Agent `action: "approve" or "reject"`——LLM Agent 会按照 skill 文档调，根本不会知道有新 action。**SOP**：任何改 API contract 的 PR，都要查 grep `openclaw-agent/skills` 找匹配的 skill 一起更新。
9. **must_have_keywords 校验是好习惯**：sync 脚本里加 `missing = [k for k in must_have if k not in new_content]` 能防"同步了错文件 / 同步了未保存的旧版本 / 字符编码炸了"等坑。
10. **不需要手动 OpenClawSkill 表写 reassign**：Hub 端用 `installed_at < skill.updated_at` 自动判 stale，UPSERT 改 template_content 时 `updated_at` 自动走 onupdate，无需额外动作。

### 本次产物（补完）
- 新增：`openclaw-agent/skills/knowledge-manager/SKILL.md`（v2 评审章节）
- 新增：`openclaw-agent/skills/hub-connect/SKILL.md`（评审流程 v2 整章）
- 新增：`openclaw-agent/skills/testcase-manager/SKILL.md`（评审记录时间线 v2.1）
- 新增：`_deploy_review/sync_review_skills.py`（一键 UPSERT 3 个 skill + 关键词校验）
- 新增：`_deploy_review/check_stale.sql`（stale 状态验证）

### ⚠️ 事故复盘：评审中心 UI 被另一 AI 回退（2026-04-25 12:07）

**症状**：用户反馈"评审中心给另一个 AI 改回去了，打回整改状态没了"。

**根因排查**：
- `/opt/openclaw-web/templates/review.html` mtime = 12:07，但本次部署是 02:55 → 文件被外部改动
- 远端文件大小 10381 bytes，对照本地 staging 23479 bytes → 完全回退到 v1 简化版
- 本地 `web/templates/review.html` mtime 也是 12:07 / 10381 bytes → **本地工作目录也被同步改了**
- 后端无损：`grep -c revise` skills.py 11、knowledge.py 11、rules.py 8、review_comments.py 1、testcases.py 0
- API 路由活着：`curl /api/v1/review-comments/my-submissions` 返回 401（路由存在）

**直接结论**：另一个 AI agent 在没看 MEMORY.md 的情况下，按照旧 `web/templates/review.html` 当作"权威源"重新部署了一遍，把多轮评审 UI 推回了简化版。**后端没动**，所以接口和数据全在，只是页面看不到 revise/整改/comment/timeline/我的提交。

**修复**（5 分钟内完成，零数据损失）：
1. `_deploy_review/staging/templates/review.html` 是唯一保留的 23479 bytes 完整版（部署时的 staging 备份）
2. 拷回本地 `web/templates/review.html`
3. scp 推到远端 `/opt/openclaw-web/templates/review.html`，覆盖前先 `cp ... .simplified_120731.bak` 留 forensic
4. 模板按 mtime 自动重读，**无需** `systemctl restart openclaw-web`
5. grep 验证 34 处关键字（整改/revise/review-comments/我的提交/reviseModal/loadReviseItems/loadMyItems）回来

**经验补充（防回退）**：
11. **`_deploy_review/staging/` 这个 staging 目录是救命备份，不要删**：每次部署留下的 staging 是发生回退/损坏时的唯一权威源；如果 staging 也被覆盖，就只能从远端服务器拉版本（这次刚好 staging 还在）
12. **同 repo 多 AI 协作要前置自检**：任何 AI 在改 `templates/review.html` / `app/api/skills.py` 等高频文件前，应该先 `head -50 MEMORY.md | grep -A 20 'Section 1[5-9]'`，看看最近是否有 v2 改造正在进行；改完前后 grep 关键功能词（revise/review-comments）防止减功能
13. **被改回退后第一时间 forensic 备份**：远端那份"被改的 10381 bytes 简化版"先 `cp ...bak` 保存，再恢复完整版——保留对方的改动可以判断他到底想做什么，避免下次再撞
14. **本地工作目录不是真理**：本次本地 `web/templates/review.html` 也被改成简化版，说明对方是同步改了 git 工作树再 push，不能假设"本地代码 == 上一次我提交的状态"

**事件记录**：
- 备份位置（远端）：`/opt/openclaw-web/templates/review.html.simplified_120731.bak`
- 备份位置（本地）：`F:\Code\claw_team\_deploy_review\review_simplified_by_other_ai_120731.html`
- 修复脚本（无）：直接 scp 一文件搞定，未生成新脚本

### 事故后续：合并对方的合理意图（2026-04-25 12:15）

事后了解对方 12:07 的回退**不是恶意**，而是为了修一个真实 bug：评审中心初始化时只 `loadPendingItems()`，其他 tab 的页签计数（已通过/已废弃）都显示 0，要等点击 tab 才刷新；审核操作后也只刷 pending。对方的修复方法是回退到 v1 + 在 `checkReviewPermission()` 里并行加载 3 个 tab + `reviewAction()` 后同步刷新所有 tab。

**正确做法**：保留 v2 多轮评审 UI，把对方那两个修复点合进来 + 扩展到 5 个 tab：
```js
function checkReviewPermission() {
    canReviewOperate = !!currentUser && currentUser.role === 'super_admin';
    loadPendingItems();
    loadReviseItems();   _loaded.revise = true;
    loadApprovedItems(); _loaded.approved = true;
    loadRejectedItems(); _loaded.rejected = true;
    loadMyItems();       _loaded.mine = true;
}
// reviewAction 成功后也并行刷新全部 5 个 tab，不再依赖 switchTab 触发
```

**经验补充**：
15. **被外部 AI 回退的代码，先看动机再合并**：不要假设回退就是恶意/不知情；先 diff 对方版本和当前版本，提取对方的合理修复（这次是"页签计数不及时刷新"的真实 bug），把这些点合到主线版本里，避免下次又被回退一次
16. **多 tab UI 不要只 lazy load 当前 tab**：除非数据特别贵，否则初始化时并行加载所有 tab 的计数（用同一个 API.request 池），用户感知到的"切换 tab 立刻有数据"远比节省一次请求重要

*最后更新: 2026-04-25 12:15（合并对方的"页签计数即时刷新"修复 + 经验 15-16）*

---

## 19. 课题讨论详情页 UI 重构（2026-04-25 13:25）

### 背景
用户反馈 `/topics/{id}` 详情页 reply 区显示"太丑"：
1. 用户名/内容区分不明显，超长帖子全展开占满屏
2. 回复按发帖正序，老贴在上、新贴要往下翻
3. placeholder 写"支持 Markdown"但实际没渲染，且不能贴图

### 改动清单（`web/templates/topic_detail.html`）

| 改动 | 文件位置 |
|---|---|
| 引入 `marked@15.0.7` + `DOMPurify@3.0.9`，加 `renderMarkdown()` helper | `{% block scripts %}` 顶部 |
| reply 卡片：彩色头像（名字 hash → HSL）+ 粗体名 + #楼号徽标 + 时间右对齐 | `renderReplyCard()` |
| 长内容自动折叠：`>280` 字符或 `>6` 行触发，蒙版渐隐 + "▾ 展开全文" | `.reply-body-wrap.collapsed` + `toggleReplyFold()` |
| 顶部"全部折叠/全部展开"总开关 | `toggleAllReplies()` |
| reply 列表 `.reverse()` 倒序展示，**楼号 `_floor` 在 reverse 之前打到对象上**（避免楼号乱） | `renderReplies()` |
| topic body 也走 markdown 渲染（不再 `white-space: pre-wrap`） | `renderTopic()` 末尾 |
| reply textarea → **Vditor IR 编辑器**，复用 knowledge 页同款配置 | `initReplyEditor()` + `submitReply()` |

### 静态依赖本地化（`web/static/vendor/`）

| 文件 | 体积 | 来源 |
|---|---|---|
| `marked.min.js` | 39589 B | jsdelivr@15.0.7 |
| `purify.min.js` | 21105 B | jsdelivr@3.0.9 |

- 模板用 `{{ url_for('static', filename='vendor/xxx.min.js') }}` 引用
- vditor 自身较大（~1MB）继续走 CDN，避免 git 仓库膨胀；如未来内网封 CDN 再迁
- `.gitignore` 检查过：`web/static/vendor/` 不在忽略列表，可正常 `git add`

### 图片上传链路
- 复用现成 `POST /api/v1/upload/image`（`web/app/api/knowledge.py:59`）
- 接口返回 `{url, data:{url}}` 两种格式都给，knowledge / topic 都能用
- vditor 配置 `upload.format()` 把 `{url}` 包装成 vditor 期望的 `{code:0,data:{succMap}}`
- 上传走前端 cookie 认证；浏览器登录态下直接可用

### 部署
```powershell
ssh testserver "mkdir -p /opt/openclaw-web/static/vendor"
scp web/static/vendor/*.min.js testserver:/opt/openclaw-web/static/vendor/
scp web/templates/topic_detail.html testserver:/opt/openclaw-web/templates/topic_detail.html
ssh testserver "sed -i 's/\r$//' /opt/openclaw-web/templates/topic_detail.html"
# 模板和静态资源都不需要重启 gunicorn
```

烟测结果：
- `curl /static/vendor/marked.min.js` 200 / 39589B
- `curl /static/vendor/purify.min.js` 200 / 21105B
- `/topics/8` 302（未登录跳 login，正常）
- `/api/v1/upload/image` 401（未带 cookie，正常；浏览器有 cookie 直接 200）

### 经验

17. **倒序展示但保留发帖楼号**：先用 `map((r,i)=>({...r, _floor:i+1}))` 把楼号固化到对象，再 `.reverse()`；如果直接在 reverse 后 map 用 `i+1`，最新回复会变成 `#1`，引用对不上号
18. **markdown 渲染必带 sanitize**：marked v5+ 已经移除内置 sanitize 选项，必须 `DOMPurify.sanitize(marked.parse(text))`，否则 `<img onerror=...>` / `<script>` 直接执行；这个 70KB 的成本必须付
19. **CSS 蒙版折叠比 max-height + JS 截断优雅**：`mask-image: linear-gradient(to bottom, #000 70%, transparent 100%)` 实现底部渐隐，配合 `max-height` + `overflow:hidden`，展开时直接去 class 即可，零 reflow 抖动
20. **vendor 静态资源命名约定**：`web/static/vendor/{lib}.min.js`，所有内网复用的第三方 JS 都放这里；体积 <100KB 直接放，>500KB 评估是否值得
21. **vditor 复用 knowledge 页配置即可**：mode/theme/toolbar/upload 完全一致；唯一调整 `height: 360→240`（reply 框比知识编辑窄）和 toolbar 加 `emoji`/去 `headings 顶端`；初始化要等 `replyEditor === null` 才创建，否则切换 topic 时会双实例
22. **图片上传接口已经现成不要重写**：`/api/v1/upload/image` 在 knowledge 页跑了好久，复用即可，没必要给每个使用场景再开一个接口

### Skill 同步（2026-04-25 13:33）

UI 改了，OpenClaw 写 reply 的姿势也得改，否则它们还在按"正序展示 + 纯文本"的旧模型行文。

**修改 `openclaw-agent/skills/topic-discuss/SKILL.md`**（12086 → 14094 chars）：

- §二.5 回复课题：补 "content 渲染管线" 段，明确 marked@15.0.7 + DOMPurify@3.0.9 + 完整 GFM 支持
- §二.5.5 新增 "图片上传" 整段：`POST /api/v1/upload/image` 接口规范、约束、curl 示例
- §五 剧本 B+ 新增 "贴图回复"：上传 → 拼绝对 URL → markdown 嵌入的完整链路
- §七.5 新增 "回复展示规则" 整章：倒序 / 楼号不变 / 折叠阈值 280 字符 6 行 / 行文 SOP（TL;DR 前置 / `##` 切段 / 反例 vs 正例）
- §八 自检清单加 4 条：超长必带 TL;DR / 引用必用 #N楼或@xxx / 截图先上传再嵌入绝对 URL

**Hub DB 同步**（沿用 Section 16 SOP）：
```bash
scp openclaw-agent/skills/topic-discuss/SKILL.md testserver:/tmp/topic-discuss-SKILL.md
scp _tmp/sync_topic_skill.py testserver:/tmp/
ssh testserver "cd /opt/openclaw-web && python3 /tmp/sync_topic_skill.py"
```

`sync_topic_skill.py` 关键点：
- 7 个 must_have_keywords 校验（marked@15.0.7 / DOMPurify@3.0.9 / /api/v1/upload/image / 倒序展示 / TL;DR / 剧本 B+ / 回复展示规则）
- UPSERT `Skill.template_content`，`updated_at` 自动 onupdate=now
- 不需要 reassign 表操作，靠 `installed_at < skill.updated_at` 自动判 STALE

**验证**：skill_id=121, updated_at=`2026-04-25 13:33:23`, 8 个已安装 OpenClaw 全部 STALE：
| claw_id | name | installed_at |
|---|---|---|
| 4 | 龙虾王 | 2026-04-23 11:21:21 |
| 6 | 天飞小游戏助理小天 | 2026-04-23 11:21:21 |
| 7 | 小安-自动化测试专家 | 2026-04-24 03:10:56 |
| 8 | 小文 | 2026-04-23 11:21:21 |
| 9 | condibot | 2026-04-23 11:21:21 |
| 10 | Hermes Agent小赫 | 2026-04-23 16:10:06 |
| 11 | 需求代码分析专员小云 | 2026-04-23 11:21:21 |
| 12 | 小马-需求代码分析专员 | 2026-04-24 17:30:48 |

下次每个 OpenClaw 心跳/sync_skills 时自动拉新 14094 字版本。

### 经验补充（skill 同步 v2 配套）

23. **OpenClawSkill 关联字段是 `openclaw_id` 不是 `claw_id`**：写 sync 脚本验证 stale 时容易写错（其它表如 ClawTodo 用的是 `claw_id`），统一以 `models.py` 为准
24. **`Skill` 模型没有 `version` 字段**：sync 脚本里只能打印 `id` / `name` / `updated_at` / 字符数，要"版本号"得自己在 `template_content` 里写注释（如 `<!-- skill version: v2.1 -->`）
25. **UI 改了必查对应 skill**：UI 模板和 OpenClaw 的"使用说明书"是耦合的；改完 UI 第一件事 `grep -l 'topic\|reply' openclaw-agent/skills` 找到所有相关 skill，按新 UI 行为更新行文规范，否则 OpenClaw 按旧模型写出的 reply 在新 UI 下体验会差（如折叠后看不到要点）
26. **must_have_keywords 比 grep 更严格**：sync 时直接校验关键词在新内容里，能挡住"我以为改了实际还是旧文件 / 编码炸了"等坑；对于本次"折叠/倒序/Vditor"等新概念，至少要 5+ 个关键词覆盖每个新增章节

## Hub 代建 Hermes Agent + systemd（2026-04-28）

### 背景与目标

- 在「新增 OpenClaw」时可选 **同时部署 Hermes Agent**：Hub 经 **SSH** 在目标机部署，凭据不落库。
- **不再开放 Docker 模式**：Hub 代建 Hermes Agent 统一走 **systemd 隔离部署**；API 默认 `deploy_method=systemd`，显式传 `docker` 会被拒绝。Docker 相关代码仅作历史保留，不作为 Web/API 入口。
- **Systemd**（对齐 `F:/Code/hermes agent/部署指南.md` v2 + 2026-04-30 隔离要求）：**不**远程 git/pip。`hermes_install_dir` 指向已准备好的 venv/源码（只读运行时）；`hermes_data_dir` / `hermes_home` 必须位于 `/opt/openclaw-agents/claw-<id>-<safe_name>/` 下作为本 agent 私有工作目录。共享目录固定 `/opt/agent_share`。unit **`hermes-gateway-claw-<id>.service`**；默认服务用户 **`oclaw_<id>`**（不再 root）；unit 加 `ProtectSystem=strict` + `ReadWritePaths=<私有目录> /opt/agent_share`，确保只能写自己目录和共享目录；默认 **`python -m hermes_cli.main gateway run --replace`**；日志 **journald**。**SSH 部署目标机** 见上文「Hermes Agent 部署目标机」**249**；**Hub** 仍为 **169:8088**。
- **权限**：创建/重部署 Agent **仅 `super_admin`**；后端 `openclaws.py` + `agent_deployments.py` POST；前端 `openclaws.html` 用 `isSuperAdmin` 隐藏整块 UI 且提交时 `createAgent = isSuperAdmin && toggle`。
- **OpenClaw 创建与部署解耦**：部署失败不回滚 OpenClaw；状态在 `agent_deployments` 表，前端轮询 `/api/v1/openclaws/<id>/agent-deployments/latest`。

### 关键代码路径

| 区域 | 文件 |
|------|------|
| 模型 | `web/app/models.py` — `AgentDeployment`（`deploy_method` 当前固定 systemd；`container_name` systemd 时存 `hermes-gateway-claw-<id>.service`；`image` systemd 时为空） |
| 部署执行 | `web/app/services/agent_deployer.py` — `_deploy_systemd`，`_render_env_file` / `_render_systemd_unit`；`_deploy_docker` 仅历史保留 |
| API 解析与记录 | `web/app/api/agent_deployments.py` — `_parse_deploy_options`、`create_deployment_record`、`trigger_async_deployment`；蓝图注册在 `web/app/api/__init__.py` |
| 创建时触发 | `web/app/api/openclaws.py` — `create_openclaw` 内 `create_agent` + `deploy` dict |
| 前端 | `web/templates/openclaws.html` — 部署方式下拉、systemd 专用字段、成功弹窗轮询部署状态 |
| 依赖 | `web/requirements.txt` — `paramiko` |

### Sidecar 多实例隔离（与 Hub 代建配套）

- `openclaw-agent/skills/hub-sse-sidecar/scripts/sse_client.py`、`hub_worker.py`：`BASE_DIR` 来自 `OPENCLAW_SIDECAR_CONFIG` 所在目录。
- `install.sh`、`systemd/openclaw-sidecar.service.tpl`、`hub-connect/bootstrap.sh`：`INSTALL_DIR`、`SYSTEMD_UNIT_NAME` 默认带 `CLAW_ID`，避免多 claw 共用同一路径/unit。

### UI 小改动（同轮）

- 待办列表 **任务 ID** 弱展示：`openclaw_detail.html`（今日/待审核/近 3 天）、`dashboard.html`（今日/待审核）。

### 运维与排障要点

1. **老机选 systemd（标准拆分）**：安装目录如 `/opt/hermes-runtime`（只读 venv/源码），数据目录必须是 `/opt/openclaw-agents/claw-<id>-<safe>/data`；**SSH 目标机多为 `9.134.51.249`**（与 Hub `9.134.11.169` 分离，见 `F:/Code/hermes agent/部署指南.md` §一）。先 SSH 验证 `{安装}/venv/bin/python -c 'import hermes_cli'`（Gateway）或 `-m hermes_agent`（Module）；`config.yaml` 内含 **hub:** 节点（Hub 代建写入，base_url 指向 169:8088）；排障用 `journalctl -u hermes-gateway-claw-<id> -f`。
2. **单目录兼容**：仅填 `hermes_home` 时仍为旧路径 `config/config.yaml`，默认 Module；若已升级到 Gateway，在部署参数里显式传 `hermes_start_mode=gateway`。
3. **不要再走 Docker**：Hub 代建入口只支持 systemd；若目标机 Docker 不可用，不需要绕回 Docker 方案。
4. **生产部署后**：确保 DB 有 `agent_deployments` 表（`web/app/__init__.py` 自动迁移若已包含则随启动建表）；目标机装 `paramiko` 所在环境即 Hub 进程环境。
5. **Windows 本机跑 bash 语法检查**：若无 `bash`，可用 `C:\Program Files\Git\bin\bash.exe -n script.sh`。

### 经验沉淀（新增编号接在 26 后）

27. **同一 DB 字段复用要文档化**：`AgentDeployment.container_name` 在 systemd 下表示 **unit 文件名**，前端用 `deploy_method` 切换「容器 / Systemd unit」文案，避免运维误解。
28. **systemd 模式不做远程 pip/git**：外网、私库凭据、版本锁定都留在人工准备阶段；Hub 只负责 **配置 + unit + 启停**，失败面最小。
29. **super_admin 双端一致**：仅藏前端不够，必须在 `create_openclaw` 与 `POST .../agent-deployments` 都拒绝非 super_admin，避免 API 直调绕过。
30. **Hub systemd 与《部署指南》v2 对齐**：install/data 分离时 **HERMES_HOME** 只指向数据目录；**不要**再把 `config.yaml` 放在 `config/` 子目录；Gateway 与旧 `hermes_agent` 单进程用 `hermes_start_mode` 区分，避免无 `hermes_cli` 的 venv 误选 Gateway。
31. **#135 只保留 legacy 语境，新注册必须走 #143**：`hub-connect` 这类启动器文档不能再写 "#135 必装" 或 `skill_id=135`。权威通信运行时是 `hub-sse-sidecar-v2` (#143) + `install_v2.sh` + `sidecar-deployment-verify`；#135 只用于"已装旧版需 cleanup/禁用"说明。
32. **#118 = 通信中心模块（市场可见），#143 = 运行时**：`manager-hub`（#118）在技能市场公开展示，与 #143 配套；`hub-sse-sidecar` SKILL §0.0 仍建议与 #118 一并安装。
33. **Hermes 大模型选择是本地功能改动，不等于线上已部署**：OpenClaw 创建同步部署 Hermes 时模型选项统一走 `llm_provider='venus'` + `llm_model`（默认 `venus`，渲染 config 时映射到 `glm-5.1`）；卡片修改只允许本人 / 绑定 OpenClaw / super_admin，并同步 `claw_sidecar_configs.config_version`。如果线上还是老界面，先部署 Web 静态/模板/API 与 DB 自动迁移，再验证 `openclaw_instances.llm_model` 列。
34. **Hermes Agent 必须做 OS 级写入隔离**：私有写路径只允许 `/opt/openclaw-agents/claw-<id>-<safe>/...`，共享写路径只允许 `/opt/agent_share`；systemd 服务默认 `oclaw_<id>` 用户，unit 必须包含 `ProtectSystem=strict`、`NoNewPrivileges=true`、`ReadWritePaths=<私有目录> /opt/agent_share`。不要再把 `HERMES_HOME` 放 `/root/.hermes-*`。
35. **Hub 代建 Hermes Agent 不开放 Docker 模式**：虽然 `agent_deployer.py` 里还保留 `_deploy_docker` 历史实现，但 `agent_deployments._parse_deploy_options()` 必须默认/仅允许 `systemd`；避免后续误以为可选 Docker 导致权限隔离不一致。
36. **企微 Bot 绑定只给 Hub 代建成功的 Hermes Agent**：OpenClaw 卡片「设置」里默认只有大模型；最近一次 `AgentDeployment(agent_type=hermes, deploy_method=systemd, status=success)` 才显示「企微绑定」页签。`wecom_bot_id` 明文展示，`wecom_bot_secret` 用 `_simple_encrypt` 存储且前端不回显；保存后推进 `claw_sidecar_configs.config_version`，`/sidecar-config` 与 `/config` 可下发给本 claw。
37. **2026-04-30 Hub Web 部署记录**：已部署 Hermes 代建 systemd-only、OS 写入隔离、大模型选择、企微 Bot 绑定、#118 下架相关 Web 代码到 `9.134.11.169:/opt/openclaw-web`。本次覆盖文件：`app/__init__.py`、`app/models.py`、`app/hermes_models.py`、`app/api/__init__.py`、`app/api/agent_client.py`、`app/api/agent_deployments.py`、`app/api/openclaws.py`、`app/api/skills.py`、`app/services/agent_deployer.py`、`templates/openclaws.html`、`requirements.txt`；远端备份目录 `/opt/openclaw-web/_backup_hermes_20260430-091333`，staging `/tmp/openclaw_deploy_20260430-091333`。远端 venv 已安装 `paramiko==3.5.1`（pip 自动解析），烟测：DB 列/路由检查 `REMOTE_SMOKE_OK`、`/openclaws=302`、未认证 `/api/v1/openclaws=401`、`system-changelog=200`、服务 active。重启时旧 gunicorn stop 超时被 systemd SIGKILL 一次，但随后成功 active，无 Python traceback。
38. ~~#118 仅限下架并可按角色 API 安装~~ **已废止**：#118 已从 `OFF_SHELF_SKILL_IDS` 移除，`install_skill` 仅对 `OFF_SHELF_SKILL_NAMES`（如 `hub-connect`）保留「仅管理员可装」。
39. **#118 上架后需同步 Hub DB 元数据**：仓库内 `manager-hub/SKILL.md` 的 `display_name` 等变更后，线上 `skills` 表要更新，否则市场标题可能仍是旧文案。
40. **OpenClaw 创建页 API 路径别重复 `/api/v1`**：`web/static/js/api.js` 的 `API.request()` 已自动拼 `base='/api/v1'`，模板里调用应写 `/users`、`/projects/<id>/modules` 这类相对路径；写成 `/api/v1/users` 会请求到 `/api/v1/api/v1/users`，表现为「所属用户加载失败」。Hermes Agent 代建表单目前只暴露 API Key / 大模型 / 安装目录 / 数据目录；目录布局固定 `split`，启动方式固定 `gateway`，Python/Systemd User/Module 包名不再展示。
41. **2026-04-30 09:55 Hub 小部署**：发布到 `testserver:/opt/openclaw-web`，staging `/tmp/openclaw_deploy_20260430-0955`，备份 `/opt/openclaw-web/_backup_openclaw_form_20260430-0955`。覆盖 `app/api/skills.py`、`templates/openclaws.html`、`static/skills/{manager-hub,hub-sse-sidecar,hub-sse-sidecar-v2,hub-connect}/SKILL.md`，并同步 DB：#118/#143/#124 `template_content`，#118 改为 `display_name=通信中心模块`、`is_deleted=False`、`review_status=approved`、`is_standard=True`。生产 Python 不支持 `frozenset[int]`，要用 `OFF_SHELF_SKILL_IDS = frozenset()` 兼容。烟测：`REMOTE_APP_SMOKE_OK`、`/openclaws=200`、`/api/v1/system-changelog=200`、未认证 `/api/v1/openclaws=401`、`openclaw-web=active`。
42. **OpenClaw owner 下拉展示和值要分离**：创建页「所属用户」`option.value` 必须继续用 `username`（英文名，写入 `owner` 字段），显示文本用 `display_name(username)`，如 `大圣(chancesong)`；不要把中文显示名写入 owner，否则权限判断/owner 匹配会偏。
43. **2026-04-30 10:07 Hub 小部署**：仅覆盖 `templates/openclaws.html`，staging `/tmp/openclaw_deploy_20260430-1007`，备份 `/opt/openclaw-web/_backup_owner_label_20260430-1007`。发布 owner 下拉显示 `display_name(username)`、value 保持 `username`。烟测：`REMOTE_TEMPLATE_OWNER_LABEL_OK`、`/api/v1/system-changelog=200`、未认证 `/api/v1/openclaws=401`、`openclaw-web=active`。注意 PowerShell 直传 here-doc 到 ssh 时可能带 CR/引号转义问题；复杂烟测脚本可临时 scp `.py` 到 `/tmp` 执行。
44. **Hermes 代建 Venus Key 不应从页面填写**：创建 OpenClaw 同步部署 Hermes 时，Venus API Key 使用 Hub 服务端预置配置，前端不展示、不提交。后端 `_parse_deploy_options()` 优先读 `DEPLOY_DEFAULT_VENUS_API_KEY` / `HERMES_VENUS_API_KEY` / `VENUS_API_KEY`，再读 `system_config` 的 `deploy_venus_api_key` / `hermes_venus_api_key` / `venus_api_key`；仅当 `llm_provider=venus` 时兼容读取 `llm_api_key`。
45. **Hermes 部署完成通知走龙虾王待办，不直接发企微**：Hub 后台部署线程在 `AgentDeployment.status` 进入 `success/failed` 后创建 `ClawTodo(verification_target=agent-deploy-notify:<deployment_id>)` 给所有 `role=admin` 的 OpenClaw，并 SSE 通知它们；待办描述里写目标用户名 `claw.owner`、部署状态、目标机、unit、失败原因，让龙虾王按自身 skill 去发企微。创建页同步部署成功返回后不再弹注册成功框，只 toast 后台部署中；列表卡片根据最近一次 `agent_deployment_status in (pending,in_progress)` 禁用详情/设置/Token/头像操作。
46. **远程脚本写中文不要用 PowerShell heredoc 直传**：2026-04-30 #118 `display_name/description` 曾被写成 `????`，根因是本机 PowerShell heredoc/终端编码把中文替换后再传给远端 Python。修复方式：把 UTF-8 `.py` 文件 scp 到远端执行，并在脚本里 `os.chdir('/opt/openclaw-web'); sys.path.insert(0, '/opt/openclaw-web')`，避免从 `/tmp` 误导入 `/tmp/app`。验证：`SKILL118_TITLE_OK`。
47. **PUT/PATCH Skill 500 有两层原因**：一是 `skills.update_skill()` / `rules.update_rule()` 直接访问 `user.bound_claw_name`，但普通 `User` 和 `_ClawAdminProxy` 没有该属性；必须用 `getattr(user, 'bound_claw_name', '') or getattr(user, '_claw_name', '') ...`。二是线上全局间歇 500 来自 SQLAlchemy `QueuePool size 5 overflow 10` 耗尽；`config.py` 里旧式 `SQLALCHEMY_POOL_SIZE/MAX_OVERFLOW` 不生效，必须设置 `SQLALCHEMY_ENGINE_OPTIONS={'pool_size':20,'max_overflow':30,'pool_pre_ping':True,'pool_recycle':3600,'pool_timeout':30}`。2026-04-30 已部署 `config.py`、`app/api/skills.py`、`app/api/rules.py` 到 `/opt/openclaw-web`，备份 `/opt/openclaw-web/_backup_skill_update_500_20260430-1604`，烟测 `REMOTE_SKILL_UPDATE_FIX_OK`、`system_changelog=200`、服务 active。
48. **Skills 市场卡片修改者 tooltip 要包含具体人/Claw 名**：`skills.html` 左下角 `_modifierTag()` 不能只写“OpenClaw 修改/用户修改”，要写 `${who}（OpenClaw/用户/系统）修改 · time`；外层 span 不再重复设置只有 `last_modified_by` 的 title，避免 hover 命中错误 tooltip。
49. **通信中心聊天记录按会话分组，不再平铺消息**：`claw_messages` 已足够区分来源；`from_claw_id IS NULL` 或 `from_claw_id == claw_id` 归为 `Web/Admin ↔ 单个 OpenClaw`，`from_claw_id != claw_id` 归为 `OpenClaw ↔ OpenClaw`。前端消息记录页应先展示有消息的会话，再点击查看明细；不要把无来往的 Claw 组合生成空会话。
50. **2026-04-30 16:27 通信中心会话记录部署**：发布到 `testserver:/opt/openclaw-web`，staging `/tmp/openclaw_deploy_comm_20260430-162751`，备份 `/opt/openclaw-web/_backup_comm_center_20260430-162751`。覆盖 `app/api/agent_hub.py`、`templates/hub.html`、`templates/skills.html`（包含 Skills tooltip 上次本地修复）。烟测：`/hub=200`、未登录新 API 返回 `401`（符合登录态要求）、直接 handler 模拟 super_admin：`claw-conversations=200 admin=9 pairs=0`、`conversation-messages=200 count=64`、`openclaw-web=active`。当前线上无 claw↔claw 历史消息，所以 pairs=0 正常。
51. **通信中心历史消息 `from_claw_id` 不一定可靠**：2026-04-30 发现龙虾王给小赫的历史消息落库为 `claw_id=10/from_claw_id=10/sender_name=龙虾王`，不能只靠 `from_claw_id != claw_id` 判断 claw↔claw。`agent_hub.py` 需用 `sender_name -> OpenClaw.id` 反推真实发送方，若 sender 匹配另一个 claw 则归入双方会话。已部署补丁：`/tmp/openclaw_deploy_comm_fix_20260430-163347`，备份 `/opt/openclaw-web/_backup_comm_pair_fix_20260430-163347`；烟测 `pairs=6`，包含 `龙虾王 <-> Hermes Agent小赫`，明细 count=3。
52. **通信中心记录页避免复用左侧 Claw 选择栏**：消息记录页是看历史会话，不是下发任务；进入 `records` 视图时用 `records-mode` 隐藏 `.cc-sidebar`，并在记录面板内用二级 tab 区分 `Web/Admin ↔ OpenClaw` 与 `OpenClaw ↔ OpenClaw`，避免用户误以为左侧选择会过滤消息记录。已部署模板 `templates/hub.html` 到 `testserver:/opt/openclaw-web`，staging `/tmp/openclaw_deploy_records_tabs_20260430-164315`，备份 `/opt/openclaw-web/_backup_records_tabs_20260430-164315`；远端模板校验含 `records-mode/record-tab-admin/record-tab-claw/switchRecordTab`，服务 active。注意未登录访问 `/hub` 会 302/返回登录页，不能用未登录 HTML 判断模板是否生效。
53. **claw↔claw 会话明细应像聊天框，不要每条写 A→B**：`templates/hub.html` 的 `openConversationRecord()` 在 `type=claw_pair` 时用 `display_from_claw_id/from_claw_id` 判断左右侧，meta 只显示 `发送者 · 时间`；Web/Admin 会话仍可显示 `发送者 → 接收者 · 时间`。这样用户能直观看出哪边是哪只 Claw，避免每条消息都像流水日志。已部署到 `testserver:/opt/openclaw-web`，staging `/tmp/openclaw_deploy_pair_chat_20260430-180453`，备份 `/opt/openclaw-web/_backup_pair_chat_20260430-180453`；远端模板确认含 `const isClawPair`、`display_from_claw_id`、`formatTime(m.created_at)`，服务 active。
54. **Hermes 代建 Venus Key 必须前后端一起发布**：仅隐藏 `openclaws.html` 不够，线上 `agent_deployments.py` 若还是旧版仍会要求前端提交 `venus_api_key`。2026-04-30 发现线上模板和后端都回退/未更新，已发布 `templates/openclaws.html` + `app/api/agent_deployments.py`，staging `/tmp/openclaw_deploy_hide_venus_20260430-181124`，备份 `/opt/openclaw-web/_backup_hide_venus_20260430-181124`。校验：远端模板不含 `deploy_venus_api_key`，包含“页面不展示也不提交”；后端含 `_configured_venus_api_key`、`DEPLOY_DEFAULT_VENUS_API_KEY`、“不要求前端提交”；服务 active。
55. **Hermes 代建安装/数据目录也不从页面填**：`openclaws.html` 不展示、不提交 `deploy_hermes_install_dir` / `deploy_hermes_data_dir`，只提示“使用 Hub 服务端默认值”。`agent_deployments.py` 在 systemd 且未传目录时自动补 `hermes_install_dir = DEPLOY_DEFAULT_HERMES_INSTALL_DIR or HERMES_INSTALL_DIR or /opt/hermes-runtime`，`hermes_data_dir = build_default_systemd_data_dir(claw.id, claw.name)`。已部署：staging `/tmp/openclaw_deploy_hide_dirs_20260430-181659`，备份 `/opt/openclaw-web/_backup_hide_dirs_20260430-181659`；校验远端模板不含两个旧 input/旧错误文案，后端含目录默认逻辑，服务 active。
56. **OpenClaw 头像池要偏可爱动物/人物，不要只放图标**：`templates/openclaws.html` 的 `ROBOT_AVATARS` 是注册和卡片编辑共用头像池。2026-05-01 已把默认头像改为 🐱，前 24 个注册可见头像换成动物为主（猫/狗/兔/熊猫/狐狸等），完整编辑池加入可爱人物/职业角色（👩‍💻/🧑‍🚀 等），保留少量机器人/星星类作为补充。已部署：staging `/tmp/openclaw_deploy_cute_avatars_20260501-115329`，备份 `/opt/openclaw-web/_backup_cute_avatars_20260501-115329`；远端校验含可爱文案、动物/人物头像和 `slice(0, 24)`，服务 active。
57. **OpenClaw 注册所属模块不要用 datalist + 项目私有模块单一路径**：`/projects/<id>/modules` 只返回挂到该项目的模块，很多“系统模块”在 `/modules`，所以选择项目后会把旧默认 datalist 清空，用户看起来无法选择。2026-05-01 改为真正的 `<select name=module_name id=module-input>`；`onProjectChange()` 先拉项目模块，为空或失败则回退 `API.listModules()`，打开注册弹窗时也预加载系统模块。已部署：staging `/tmp/openclaw_deploy_module_select_20260501-115926`，备份 `/opt/openclaw-web/_backup_module_select_20260501-115926`；远端模板校验无 `module-options`、含 `API.listModules()`；DB 样例模块存在。
58. **OpenClaw 注册所属模块应取系统一级模块分类，而非项目模块或 DB 模块列表**：用户要求只用系统设置的一级模块：外围系统、核心单局、商业化、客户端性能、服务器专项、其他专项。2026-05-01 将 `openclaws.html` 的 `onProjectChange()` 改为刷新 `API.getModuleCategories()`，并保留六类 fallback；不再调用 `API.getProjectModules(projectId)` 或 `API.listModules()`。已部署：staging `/tmp/openclaw_deploy_module_categories_20260501-120455`，备份 `/opt/openclaw-web/_backup_module_categories_20260501-120455`；远端校验通过，`MODULE_CATEGORIES` 当前为这六类。
59. **Hermes 代建隐藏表单后必须配置服务端部署默认值**：2026-05-01 “部署参数无效”根因是前端已不传 host/ssh/key/目录，线上也没有 `DEPLOY_DEFAULT_HOST` 等配置；后来还发现 `llm_provider=doubao` 导致已有 `llm_api_key` 不被当作 Venus key。已改 `agent_deployments.py` 支持从 `system_config` 读取 `deploy_default_host/ssh_port/ssh_user/ssh_key/ssh_password/hermes_install_dir`，并允许专用 Venus key 缺失时 fallback 到 `llm_api_key`。线上已配置 `deploy_default_host=9.134.51.249`、`deploy_default_ssh_port=36000`、`deploy_default_ssh_user=root`、`deploy_default_ssh_key=<本机 id_9.134.51.249 私钥>`、`deploy_default_hermes_install_dir=/opt/hermes-runtime`；Hub→249 SSH OK，`_parse_deploy_options()` 最小参数 PARSE_OK。注意：249 当前未发现 `/opt/hermes-runtime`，后续真实部署可能从“参数无效”变成“运行时目录不存在/启动失败”，需先准备 runtime。
60. **Hermes 代建失败也必须写 `agent_deployments`，否则卡片不会锁定**：2026-05-01 大赫创建时部署参数校验失败但没有落部署记录，导致卡片仍能正常操作，违背“未部署完只能看进度/重部署”。已改 `openclaws.py`：非 super_admin、参数 ValueError、任务下发异常都会 `_record_failed_agent_deployment()` 写 `status=failed`；已改 `openclaws.html`：`pending/in_progress/failed` 都锁卡，隐藏头像/设置/token/详情入口，只保留“查看进度”，失败且 super_admin 显示“重新部署”。已给大赫补 `AgentDeployment#1 failed`。部署：staging `/tmp/openclaw_deploy_lock_cards_20260501-121803`，备份 `/opt/openclaw-web/_backup_lock_cards_20260501-121803`；服务 active。
61. **失败部署卡片不要用底部 overlay 堆信息**：2026-05-01 大赫卡片里模型、失败状态、查看进度、重新部署、修改信息挤在一起，且“重新部署”不像可点主操作。已将 `openclaws.html` 改为独立 `.deploy-panel`：模型行只保留模型/企微等基础 tag，失败/部署中状态放到卡片描述下方，右侧放“查看进度”和高亮 `btn-primary` “重新部署”；去掉 `data-lock-text` 伪元素，卡片锁定但不整体压暗到不可读。已部署：staging `/tmp/openclaw_deploy_card_ui_20260501-122300`，补规范备份 `/opt/openclaw-web/_backup_card_ui_20260501-122300`；烟测 `CARD_UI_TEMPLATE_OK`、`/openclaws=302`、`openclaw-web=active`。注意 PowerShell 双引号里的远端 `$backup` 会被本地提前展开，远端脚本变量要转义或直接写死路径。
62. **OpenClaw 页面不要用浏览器原生弹窗**：2026-05-01 “查看进度”曾用 `alert()`，不符合 Hub 统一交互；同段“重新部署”也用了 `confirm()`。已改 `openclaws.html`：新增 `deploy-progress-modal` 站内弹窗展示部署状态、目标机、unit、目录、错误和日志；`viewDeploymentProgress()` 打开该弹窗并支持刷新；`redeployHermesAgent()` 改用全局 `customConfirm()`。发布：staging `/tmp/openclaw_deploy_progress_modal_20260501-122707`，备份 `/opt/openclaw-web/_backup_progress_modal_20260501-122707`；校验 `PROGRESS_MODAL_TEMPLATE_OK`，模板无 `alert(`/`confirm(`，`/openclaws=302`，服务 active。
63. **Hermes runtime 迁移后要修 venv 绝对路径与 `.env` 权限**：2026-05-01 大赫重新部署失败链路：先是 249 缺 `/opt/hermes-runtime/venv/bin/python`，从 169 的 `/opt/hermes-xiaohe` 同步 `src/venv` 到 249 后，又发现 venv 的 `python3.11` 绝对软链指向 `/root/.local/bin/python3.11`、editable install 映射指向 `/opt/hermes-xiaohe/venv/src/hermes-agent`；已在 249 改为 `/usr/bin/python3` 与 `/opt/hermes-runtime/venv/src/hermes-agent`，验证 `HERMES_RUNTIME_OK`。随后 systemd 启动失败是因为 Hub 用 root 写 `.env` 为 `0600`，但 unit 以 `oclaw_14` 运行；已修 `agent_deployer.py`：systemd 写完 `config.yaml/.env` 后 `chown <service_user>:openclaw_agents`，`config.yaml=0644`、`.env=0600`。发布：`/tmp/openclaw_deploy_env_perm_20260501-123808`，备份 `/opt/openclaw-web/_backup_env_perm_20260501-123808`；大赫 `AgentDeployment#4 success`，`hermes-gateway-claw-14.service active`。
64. **大模型设置只属于 Hub 代建成功的 Hermes Agent**：2026-05-01 发现所有可管理/本人 OpenClaw 都能打开卡片“设置”并修改 `llm_model`，但普通 Claw 没有 Hermes runtime，模型设置无效且误导。已改 `openclaws.html`：非 `has_hermes_agent` 不显示模型 tag 和设置入口，打开/保存设置时也用 toast 拦截；已改 `openclaws.py`：`PUT /openclaws/<id>` 收到 `llm_provider/llm_model` 时必须 `_has_registered_hermes_agent(claw)`，否则 403。发布：staging `/tmp/openclaw_deploy_hermes_model_scope_20260501-132120`，备份 `/opt/openclaw-web/_backup_hermes_model_scope_20260501-132120`；烟测 `NON_HERMES_TEST 龙虾王 -> 403`、`HERMES_AVAILABLE 大赫 True`、`/openclaws=302`、服务 active。
65. **Hermes 模型/企微配置保存后要重启 systemd**：2026-05-01 用户明确 `llm_model`、企微 Bot Key/Secret 改完后运行中 Agent 需要吃新配置。已新增 `POST /api/v1/openclaws/<id>/agent/restart`（`agent_deployments.py`）：仅 owner/绑定者/super_admin 且 `_has_registered_hermes_agent` 为真可调用；读取最近一次成功 systemd 部署记录定位 `hermes-gateway-claw-<id>.service`，用服务端默认 SSH 配置执行 `systemctl restart` 并确认 `is-active=active`。`openclaws.html` 设置弹窗新增“重启 Agent”按钮；保存模型/企微设置成功后自动调用重启，成功 toast “设置已保存，Hermes Agent 已重启”，失败则提示“设置已保存，但重启失败”。发布：staging `/tmp/openclaw_deploy_agent_restart_20260501-152211`，备份 `/opt/openclaw-web/_backup_agent_restart_20260501-152211`；烟测大赫重启 200/active，龙虾王重启 403，`/openclaws=302`，服务 active。
66. **Hub 代建 Hermes 不能只起 Gateway，必须配套 sidecar v2 + 真实 Venus 模型名**：2026-05-01 “大赫没有回复”排查链路：`hermes-gateway-claw-14.service active` 但日志只有 `No messaging platforms enabled`，Hub 侧 `claw.status=offline`、消息 pending，根因是代建流程只启动 Hermes Gateway，没有安装 #143 `sidecar_v2.py`；补装 `openclaw-sidecar-v2-claw-14.service` 后又暴露 `VENUS_API_KEY=test-key`（来自 `llm_api_key` fallback）导致 403，以及 UI 存储的 `deepseek-v4` 不是 Venus 真实模型 ID导致“模型不存在”。修复：线上 `system_config.deploy_venus_api_key` 写入专用 key；`hermes_models.py` 映射 `deepseek-v4 -> deepseek-v4-pro`、`hunyuan-v3 -> hy3-preview`；`agent_deployer.py` 在 systemd 部署成功后自动下载 `/static/skills/hub-sse-sidecar-v2/scripts/sidecar_v2.py`、写 `hermes_sidecar_wrapper.sh`/`sidecar.env`、初始化 `/sidecar-config`、启动 `openclaw-sidecar-v2-claw-<id>.service`。已部署：staging `/tmp/openclaw_deploy_sidecar_fix_20260501-1539`，备份 `/opt/openclaw-web/_backup_sidecar_fix_20260501-1539`；验证大赫 `AgentDeployment#7 success`、gateway/sidecar 均 active，测试消息 `2342` 已 `done` 并生成回复 `2343: OK`。注意连续测试可能触发 Venus 公共服务限流 `429 当前限流 10/min`，不应误判为 sidecar 离线。
67. **企微不通先分清 Bot 凭据、收件人、LLM 工具调用三层**：2026-05-01 “还是没企微”排查：大赫 `wecom_bot_id/secret` 已保存，但 `owner_wecom_userid` 为空，sidecar 下发时应 fallback 到 `claw.owner`（如 `rajqiu`）；另外 sidecar 只拿到 Hub 配置，没有把 `wecom_bot_id/secret` 注入 Hermes 子进程，导致 Hermes `send_message` 认为 `Platform wecom is not configured`。修复：`agent_client.py` 下发 `owner_wecom_userid = owner_wecom_userid or owner`；`sidecar_v2.py` 调 LLM 时注入 `WECOM_BOT_ID/WECOM_KEY/WECOM_SECRET/WECOM_HOME_CHANNEL`，提示词明确 `send_message(action='send', target='wecom')`（不要写 `wecom:<owner>`，未建 channel directory 时会 `Could not resolve`，依赖 `WECOM_HOME_CHANNEL` 发给 owner），并修正 todo complete URL 为 `/api/v1/openclaws/<id>/todos/<todo_id>/complete`；`agent_deployer.py` 生成 wrapper 时启用 `-t hermes-wecom`。验证方法：不要只等 LLM，可在目标机用大赫 token 拉 `/sidecar-config` 后直接实例化 `gateway.platforms.wecom.WeComAdapter`，本次直连 `connect=True`、`send=True`，返回 `message_id=aibot_send_msg-42d5...`，说明 Bot ID/Secret 可用；若用户仍未收到，优先核对 `owner_wecom_userid`/企微用户 ID 是否就是 `rajqiu`。
68. **Hub 代建 Hermes 企微必须同时按部署文档写 `config.yaml` 和 `.env`，不能只依赖 sidecar 动态注入**：2026-05-01 用户追问“有按照部署文档来部署么”后对照 `docs/hermes_agent_部署文档.md`，发现文档 4.1/8 要求 `config.yaml` 有 `wecom.key/secret`，环境变量有 `WECOM_KEY/WECOM_SECRET`；此前 Hub 只在 sidecar 调 Hermes 子进程时注入，远端 `config.yaml/.env` 不符合文档，容易导致 gateway/工具初始化路径判断为无平台。修复：`agent_deployer.DeployRequest` 增加 `wecom_bot_id/wecom_bot_secret/owner_wecom_userid`，`_render_config_yaml()` 输出 `wecom:` 节点，`_render_env_file()` 输出 `WECOM_KEY/WECOM_BOT_ID/WECOM_SECRET/WECOM_HOME_CHANNEL`；`agent_deployments._parse_deploy_options()` 从 `OpenClawInstance` 解密带入。已部署到 `testserver:/opt/openclaw-web`，staging `/tmp/openclaw_deploy_doc_wecom_20260501-1605`，备份 `/opt/openclaw-web/_backup_doc_wecom_20260501-1605`；大赫重部署 `AgentDeployment#8 success`，远端核查 `HAS_WECOM_NODE True`、`.env` 含企微变量、gateway/sidecar active。端到端测试消息 `2362` 已 `done`，回复 `2366` 报告企微发送成功，包含 `aibot_send_msg-4ff8...` 和最终通知 `aibot_send_msg-2eea...`。注意：`send_message(action='list')` 可能仍返回 `No messaging platforms`，但 `target='wecom'` 发送实际可成功，不要把 list 的 quirks 当作发送失败。
69. **Hermes WeCom 默认不配对要设 `WECOM_ALLOW_ALL_USERS=true`，只设 `WECOM_DM_POLICY=open` 不够**：2026-05-01 用户收到 “Hi~ I don't recognize you yet! pairing code ... hermes pairing approve wecom ...”，根因在 Hermes `gateway/run.py` 授权链：WeCom 先看 `WECOM_ALLOW_ALL_USERS` / allowlist / pairing approved；没有命中才进入 unauthorized DM behavior，默认 `pair` 发配对码。Hub 代建要求“默认开放，不用配对”，修复 `agent_deployer._render_env_file()`：有 WeCom Bot 时写 `WECOM_ALLOW_ALL_USERS=true` 和 `WECOM_DM_POLICY=open`；文档环境变量汇总也补同样说明。已部署到 `testserver:/opt/openclaw-web`，staging `/tmp/openclaw_deploy_wecom_open_20260501-161429`，备份 `/opt/openclaw-web/_backup_wecom_open_20260501-161429`；大赫当前 `.env` 已补写并重启 `hermes-gateway-claw-14.service`，运行中 `/proc/<pid>/environ` 确认 `WECOM_ALLOW_ALL_USERS=true`、`WECOM_DM_POLICY=open`、`WECOM_HOME_CHANNEL=rajqiu`。
70. **DeepSeek V4 拆成 Flash/Pro 两个 Hermes 模型选项，旧值兼容到 Pro**：2026-05-01 用户要求大模型设置里 DeepSeek V4 增加 Flash 和 Pro 两个版本。修复 `web/app/hermes_models.py`：新增 `deepseek-v4-flash -> deepseek-v4-flash`、`deepseek-v4-pro -> deepseek-v4-pro`，旧别名 `deepseek-v4`/`DeepSeek V4` 归一到 `deepseek-v4-pro`，避免历史 DB 值失效；`openclaws.html` 创建弹窗和设置弹窗下拉改为 `DeepSeek V4 Flash` / `DeepSeek V4 Pro`；`docs/hermes_agent_部署文档.md` 模型表同步；`check_hermes_model_options.py` 更新断言（顺手修正 Hunyuan 应渲染 Venus 真实模型 `hy3-preview`）。已部署到 `testserver:/opt/openclaw-web`，staging `/tmp/openclaw_deploy_deepseek_variants_20260501-162144`，备份 `/opt/openclaw-web/_backup_deepseek_variants_20260501-162144`；远端烟测：`deepseek-v4-flash => DeepSeek V4 Flash/deepseek-v4-flash`，`deepseek-v4-pro => DeepSeek V4 Pro/deepseek-v4-pro`，旧 `deepseek-v4 => deepseek-v4-pro`。
71. **Hub 新增 Hermes Agent 的部署闭环已固化到脚本，sidecar wrapper 必须显式 source `.env`**：2026-05-01 晚整理部署流程后发现下一次新建最容易遗漏的是“Gateway systemd 吃 `.env`，但 sidecar 调 Hermes CLI 的 wrapper 未显式加载 `.env`”，导致 Hub 通信侧可能拿不到 `VENUS_API_KEY/WECOM_*`。修复 `agent_deployer.py`：`_render_sidecar_wrapper()` 按拆分/兼容布局设置 `HERMES_CONFIG_PATH`，并 `set -a; . <HERMES_HOME>/.env; set +a`；部署时写完 `config.yaml/.env` 后新增 `verify hermes config/env`，检查 `VENUS_API_KEY/HERMES_CONFIG_PATH/model:`，有企微时检查 `WECOM_KEY/WECOM_SECRET/WECOM_ALLOW_ALL_USERS=true/wecom:`；`sidecar_v2.py` 调 LLM 时补 `WECOM_ALLOW_ALL_USERS=true`、`WECOM_DM_POLICY=open`。文档 `docs/hermes_agent_部署文档.md` 新增“Hub 代建标准部署流程”8步：默认配置读取、目录/用户、config/env、自检、gateway、sidecar、验收条件。已部署到 `testserver:/opt/openclaw-web`：脚本/静态 sidecar staging `/tmp/openclaw_deploy_hermes_pipeline_20260501-232644`，备份 `/opt/openclaw-web/_backup_hermes_pipeline_20260501-232644`；文档 staging `/tmp/openclaw_deploy_hermes_doc_20260501-232714`，备份 `/opt/openclaw-web/_backup_hermes_doc_20260501-232714`。本地 `check_hermes_model_options.py` + `py_compile` OK；远端渲染烟测 `HERMES_PIPELINE_RENDER_SMOKE_OK`，确认新 Agent 的 config/env/wrapper/unit 均含关键项，静态 `/static/skills/hub-sse-sidecar-v2/scripts/sidecar_v2.py` 可下载。
72. **大赫不回复时先查实例漂移和卡住的自动待办**：2026-05-02 00:24 用户反馈“大赫现在没有回复”。排查顺序：Hub DB 最近消息无新 pending；249 上 gateway/sidecar active；但远端 `/opt/openclaw-agents/claw-14-unnamed/data/scripts/hermes_sidecar_wrapper.sh` 不存在，`config.yaml/.env` 仍是旧手工内容（模型 `kimi-k2.6`、缺 `WECOM_ALLOW_ALL_USERS=true`，`WECOM_HOME_CHANNEL=T95460001A`），说明大赫实例没有应用最新部署闭环。用线上最新 `agent_deployer` 对大赫重部署，生成 `AgentDeployment#9 success`，模型规范为 `deepseek-v4-pro`，wrapper/config/env/sidecar 都补齐，gateway/sidecar active。重启后 sidecar 立即执行待办 `759 提交今日日报` 并启动 Hermes CLI，导致 Hub 测试消息 `2376` 只到 `delivered` 未进入处理；终止该卡住子进程后，sidecar 给 todo 759 失败冷却 3600s，再发测试消息 `2377`，日志显示 `[msg] 开始处理`、31 秒后 `[msg] done id=2377 hub_code=200`，回复 `2378` 生成。结论：大赫当时问题是“实例配置漂移 + sidecar 自动待办占用/卡住”，修复方式是重跑标准部署并清掉卡住的 todo LLM 子进程。若用户说企微仍无回复，下一步看 `journalctl -u hermes-gateway-claw-14.service --since ...` 是否出现 `aibot_event_callback msgtype=text`；本次 00:26 后 gateway 只有 auth/ping，没有看到用户文本事件进来。
73. **Hermes 设置页切模型不能只重启，必须先同步远端 `config.yaml/.env/wrapper`**：2026-05-02 用户把大赫模型切到 DeepSeek V4 Flash 后仍显示/运行 Kimi/旧模型。排查确认 Hub DB `openclaw_instances.llm_model=deepseek-v4-flash`，`claw_sidecar_config.llm_model=deepseek-v4-flash`，但远端 `/opt/openclaw-agents/claw-14-unnamed/data/config.yaml` 和 `.env` 仍是上次部署时的 `deepseek-v4-pro`（更早还曾是 `kimi-k2.6`）。根因：设置保存后前端调用 `/api/v1/openclaws/<id>/agent/restart`，旧实现只 `systemctl restart`，不会重写远端文件；Hermes gateway 读本地 `config.yaml/.env`，所以 DB 已变但运行时不变。修复 `agent_deployments._restart_systemd_agent()`：重启前基于当前 claw 配置调用 `_parse_deploy_options()`，用 `_render_config_yaml/_render_env_file/_render_sidecar_wrapper` 覆盖远端文件，chown/chmod 后重启 gateway 和 sidecar，并返回 `config_synced=True,llm_model=<normalized>`。已部署到 `testserver:/opt/openclaw-web`，staging `/tmp/openclaw_deploy_restart_sync_20260502-005024`，备份 `/opt/openclaw-web/_backup_restart_sync_20260502-005024`；已对大赫执行同步，远端 `config.yaml`/`.env` 和运行中 gateway `/proc/<pid>/environ` 均确认 `deepseek-v4-flash`、`WECOM_ALLOW_ALL_USERS=true`。
74. **#135 下线不能只改引用，必须在 Skills API 和 DB 同时下架**：2026-05-02 用户发现 #135 仍在 OpenClaw 可分配 Skill 列表。根因：线上 `Skill#135 hub-sse-sidecar` 仍是 `is_deleted=False/review_status=approved`，且 `skills.py` 的 `OFF_SHELF_SKILL_IDS` 为空，只按 name 下架 `hub-connect`；OpenClaw 详情页用 `API.listSkills()` 直接把未安装 skills 当“可分配”。修复：`web/app/api/skills.py` 设置 `OFF_SHELF_SKILL_IDS=frozenset({135})`，列表查询默认 `Skill.id.notin_(OFF_SHELF_SKILL_IDS)`，安装接口对 ID 级下架直接 403（#135 已由 #143 取代，不允许管理员继续分配）。线上同步 DB：`Skill#135 is_deleted=True, review_status='rejected', review_comment='已由 #143 hub-sse-sidecar-v2 取代，禁止继续分配'`，并禁用 2 条现有 `OpenClawSkill(skill_id=135, enabled=True)`。部署：staging `/tmp/openclaw_deploy_offshelf_135_20260502-234336`，备份 `/opt/openclaw-web/_backup_offshelf_135_20260502-234336`；烟测登录态 `/api/v1/skills` 不含 135，强行 `POST /api/v1/openclaws/14/skills {skill_id:135}` 返回 403，启用安装关系为 0。
75. **SSE 消息重复推送要同时查 DB 归属、Hub 推送日志和目标机旧客户端残留**：2026-05-02 用户问“消息ID=2407 大赫怎么还收到”。排查发现 `ClawMessage#2407` 实际是小赫（`claw_id=10`）对旧消息 `#1099 恢复正常了没` 的 `from_claw` 回复，不是发给大赫（`claw_id=14`）的 `to_claw`；Hub 23:41-23:47 对大赫只推了 `2396/2397/2398` 三条 sync_config，没有推 `2407`。但大赫 249 机器残留旧 #135 手工进程 `/opt/.../scripts/sse_client.py`（root、PPID=1），与新的 `openclaw-sidecar-v2-claw-14.service` 并存，可能造成重复消费/误判；已杀掉该旧进程并把 `sse_client.py`、`hub_sse_client_daemon.py` 改名为 `.disabled.<timestamp>`。代码修复两层：`agent_client.py` 的 SSE 主循环改为 `SELECT ... FOR UPDATE` 后先把 pending 原子认领为 `delivered` 再 yield，避免多 SSE 连接同时查到同一条 pending；`agent_deployer.py` 在 systemd 部署时清理旧 `sse_client.py$|hub_sse_client_daemon.py$` 进程并禁用旧脚本，防止 #135 残留复活。已部署到 `testserver:/opt/openclaw-web`，服务 active；大赫目标机只剩 `sidecar_v2.py` 进程。
76. **用例库目录元信息不应新建目录表，可沿用占位用例承载目录创建信息**：2026-05-03 用户要求用例库到每个子目录显示创建者，若下级目录创建者一致则只在父目录显示，并可点信息图标看创建/修改/评审记录。当前设计目录来自 `TestCase.module_path`，空目录靠 `is_placeholder=True` 占位用例保留；因此实现时不要引入新表，而是给 `TestCaseLibrary` 增加 `created_by/updated_by`，给 `TestCase` 增加 `updated_by`，并且创建真实用例时不再删除同路径 placeholder，避免目录创建人丢失。历史目录没有 placeholder 时，前端用该目录子树中最早记录兜底创建者/创建时间，用最新记录兜底修改者/更新时间。`testcases.html` 左侧树用 creator badge 去重展示：父子创建者相同则子目录不重复显示；每个库/目录右侧 `i` 图标打开信息弹窗，展示范围、创建者、创建时间、修改者、最后修改、用例数、子目录数，并拉 `/reviews` 过滤相关评审。已部署到 `testserver:/opt/openclaw-web`，备份 `/opt/openclaw-web/_backup_testcase_meta_<timestamp>`；验证 `py_compile` OK，DB 列 `test_case_libraries.created_by/updated_by`、`test_cases.updated_by` 已存在，模板含 `tc-info-modal/openTestcaseInfoModal/tc-tree-creator`，`system-changelog=200`，服务 active。
77. **用例库左侧目录栏必须保留可拖拽宽度，新增徽标/信息按钮后默认宽度也要加大**：2026-05-03 用户反馈左侧目录树又变窄、长文字显示不全，原因是 `testcases.html` 的 `.tc-sidebar` 固定回 `width:250px`，且创建者 badge、信息 `i` 图标占用了更多横向空间。修复：默认宽度调为 340px，`min-width=260/max-width=560`，加 `.tc-sidebar-resizer` 拖拽把手；`initSidebarResize()` 将用户拖拽宽度保存到 `localStorage['testcases.sidebar.width']`，刷新后保持。已部署到 `testserver:/opt/openclaw-web`，备份 `/opt/openclaw-web/_backup_testcase_sidebar_<timestamp>`；远端模板校验 `TESTCASE_SIDEBAR_RESIZE_TEMPLATE_OK`，服务 active。
78. **用例库左侧目录栏要同时支持“展开可拉宽”和“收缩为图标栏”**：2026-05-03 用户要求最左侧栏可以收缩成图标。基于 #77 的可拖拽宽度继续改 `testcases.html`：新增 `tc-sidebar-toggle` 按钮，展开时仍按 `localStorage['testcases.sidebar.width']` 恢复用户手动宽度；折叠时加 `.tc-sidebar.collapsed`，固定 64px，隐藏搜索框、底部新建按钮、计数、创建者 badge、信息/新增/删除按钮和文本，只保留目录/库图标；折叠状态保存到 `localStorage['testcases.sidebar.collapsed']`。已部署到 `testserver:/opt/openclaw-web`，备份 `/opt/openclaw-web/_backup_testcase_sidebar_collapse_<timestamp>`；远端模板校验 `TESTCASE_SIDEBAR_COLLAPSE_TEMPLATE_OK`，服务 active。
79. **清理重复每日任务时先按关键词审计，再按确认 ID 删除，避免误删历史 disabled 记录**：2026-05-03 用户怀疑多个 Claw 有每日“重装 manager-hub #118”任务。排查 `claw_todos` 中 `manager-hub/#118/通信中心` 共 42 条，大多数是历史 `once` 且 `enabled=False`；真正会每日触发的是 9 条 `enabled=True/schedule_type=daily`、标题 `【技能分配】重新安装 manager-hub(#118) 技能`、`created_by=龙虾王`。已删除 ID `727-735`，对应 CharlesBot、小马、小云、小赫、condibot、小文、小安、小天、龙虾王；复查 `REMAIN_BY_ID=0` 且 `REMAIN_ENABLED_DAILY_MANAGER_HUB=0`。注意：如果删除前某个 sidecar 当天已领取该待办，可能仍会完成当前一次运行，但不会再由 Hub 每日重复派发。
80. **OpenClaw 详情页待办需要单独展示当前启用的 daily 模板任务**：2026-05-03 用户要求每个 Claw 任务页增加“每日任务”页签，便于直接看到当前 `schedule_type=daily` 的长期任务，避免混在今日待办或近 3 日记录里难排查。实现只改 `openclaw_detail.html` 前端分组，不新增接口：仍从 `/api/v1/openclaws/<id>/todos?enabled_only=false` 拉全量，筛 `enabled && schedule_type === 'daily'`，按 `schedule_time` 和标题排序；新增 `todo-subtab-daily`、`todo-panel-daily`、`detail-daily-badge`、`renderDailyTodos()`，统计栏显示 `每日任务 N`。页签顺序调整为「今日待办 / 近3天记录 / 每日任务 / 待审核」，保留待审核入口避免管理员审核能力丢失。已部署到 `testserver:/opt/openclaw-web`，备份 `/opt/openclaw-web/_backup_daily_todo_tab_<timestamp>`；远端模板校验 `OPENCLAW_DAILY_TODO_TAB_TEMPLATE_OK`，服务 active。
81. **每日任务删除语义必须区分“删除今日”和“彻底删除模板”**：2026-05-03 用户要求每日任务页里增加“彻底删除”，表示后续不再重复；普通“删除”只删除当次任务。实现只改 `openclaw_detail.html` 前端语义：`deleteTodo(id, permanent=false)` 若发现任务是 `schedule_type=daily` 且不是 permanent，则调用现有 `/todos/<id>/skip` 写当天 `skipped` 日志，toast “已删除今天这次，后续每日任务保留”；每日任务页新增两个按钮：`删除今日` 和红色 `彻底删除`，后者才调用 `DELETE /todos/<id>` 删除 `ClawTodo` 模板。daily 状态展示新增 `skipped -> 今日已删除`。已部署到 `testserver:/opt/openclaw-web`，备份 `/opt/openclaw-web/_backup_daily_todo_delete_semantics_<timestamp>`；远端模板校验 `DAILY_TODO_DELETE_SEMANTICS_TEMPLATE_OK`，服务 active。
82. **TAPD 凭证保存后不回显是安全设计，但设置页必须显示脱敏生效状态**：2026-05-03 用户问系统设置里的 TAPD 账号/token 保存后是否生效。线上 DB 核查 `system_config` 已有 `tapd_api_user/tapd_api_password`，更新时间 `2026-05-03 10:35:56`，后端 `_get_tapd_credentials()` 返回 configured=True；前端看不到是因为 `settings.html` 只显示“已连接”，不会把 password/token 回填。修复：`tapd.py GET /tapd/config` 增加 `password_configured/updated_at`，`settings.html loadTAPDConfig()` 展示“已生效”、脱敏账号、更新时间和“Token/Password 不回显”提示，输入框仅更新 placeholder，不写入 masked value，避免用户直接保存时把脱敏值覆盖真实账号。部署注意：线上实际加载路径是 `/opt/openclaw-web/app/...` 与 `/opt/openclaw-web/templates/...`，不要只同步到 `/opt/openclaw-web/web/...`。
83. **SSE 长连接里用 ORM 查询必须主动 `db.session.remove()`，否则连接池会被长连接占满导致页面 500**：2026-05-03 用户反馈“页面访问不了了”。线上服务 active，但 `/api/v1/system-changelog` 超时，`/openclaws`/`/skills` 500，日志显示 `QueuePool limit of size 20 overflow 30 reached`；访问日志里大量 `/api/openclaws/<id>/events` SSE 连接 500。根因：`agent_client.claw_sse_events()` 是 `stream_with_context` 长连接，请求 teardown 不会很快执行，循环中的 `AgentTask/ClawMessage/ClawTodo` ORM 查询把 SQLAlchemy session/连接随着 SSE 连接长期持有，多个 Claw/重复 sidecar 重连后耗尽连接池。修复：在 SSE 中把 ORM 对象先转成普通 dict/event 字符串，然后在任何 `yield`/`sleep` 前调用 `_release_orm_session()` 主动归还连接；连接建立阶段读取 `claw_name`、推未读消息/待办后也立即释放。已部署到 `/opt/openclaw-web/app/api/agent_client.py` 并同步 `web/app/api`，重启后验证 `/api/v1/system-changelog=200`、`/openclaws=302`，观察 2 分钟无 QueuePool 新错误，sidecar API 恢复 200。注意：日志仍显示某些 claw（如 #6）有多条 SSE 连接，后续若连接数异常高，应继续清理重复 sidecar 进程或旧部署。
84. **#113 TAPD 集成 Skill 文档必须覆盖 Hub 代查、需求缓存和刷新队列三类接口**：2026-05-03 用户确认 #113 是否使用系统设置保存的 TAPD 账号/token 后，又指出“这里的接口是不是还不全”。排查 `tapd.py`、`requirements.py`、`testplans.py` 后发现旧 #113 只列 `/tapd/config|stories|bugs|iterations|dashboard`，且 `/tapd/config` 返回字段还是旧的 `workspace_id/has_password`。已更新 `openclaw-agent/skills/tapd-integration/SKILL.md`：明确 OpenClaw 不直接拿 TAPD token，而是通过 Hub 代读 `system_config.tapd_api_user/tapd_api_password`；补齐 `/tapd/story-title`、`/requirements/tapd-cache/{iterations,versions,baselines,field-map}`、`/requirements/iterations/<id>/{items,changes,summary}`、`/requirements/items/<id>`、需求-用例关联、`/requirements/tapd-refresh-requests`、Agent 刷新队列和 `/test-plans/<plan_id>/tasks/<task_id>/tapd-bugs`。线上已用 UTF-8 文件更新 `skills.id=113.template_content`，校验包含 `password_configured`、`/api/v1/tapd/story-title`、`/api/v1/requirements/tapd-cache/versions`、`/api/v1/requirements/tapd-refresh-requests`。
85. **#113 与 micro-cloud `openclaw_tapd_skills.json` 对齐方式**：该 JSON 描述的是 `:8080/goApi/...` 的 6 个工具（通用 GET/POST、按发布分类迭代名筛需求、项目成员、Bug 列表、TAPD 测试用例）。OpenClaw Hub 无对应单入口 `postTapdCommonFunc/getTapdCommonFunc`，已按「能力对照表」重写 #113：`stories/bugs/story-title`、缓存型 `iterations`、需求分析 `requirements/*`、Hub 用例库 `testcase-libraries` 与 JSON 场景一一映射；明确 **未实现** 通用写 TAPD、项目成员、TAPD 原生用例树；强调调用前缀为 `{HUB_URL}/api/v1` + Bearer，勿照抄 JSON 的 `:8080`。部署时不要用 `root@IP -p 36000` 直连 scp（可能卡认证），用 `testserver` alias；先 `scp SKILL.md testserver:/tmp/skill_113_tapd_integration.md`，再远端 `venv/bin/python` 读取 UTF-8 文件写 `skills.id=113.template_content`。2026-05-04 已修标题乱码风险：把 `—/…/→` 替换为 ASCII/中文表达并同步线上，校验 `char_len=6830`、首行 `# TAPD \u96c6\u6210 (tapd-integration) - Skill #113`、`bad_unicode_count=0`、`required_ok=True`、`/api/v1/system-changelog=200`、`openclaw-web=active`。用户仍看到 `????` 时，实际是 `skills.description` 曾被 PowerShell/远端脚本文本污染；已用 ASCII-only Unicode escape 更新 `display_name/description`，复查 `name/display_name/description/trigger_phrase/template_content` 全部 `has_qmarks=False`、`ALL_QMARKS_CLEAR=True`。
86. **Skills 重新分配不要默认勾选已安装 Claw，且安装/重装必须触发待办变更事件**：2026-05-04 用户反馈 Skills 点“重新分配”时所有已安装 Claw（尤其 stale 橙色项）默认勾选，容易误批量重装；同时 #113 重新分配给小赫提示成功但看不到新增待办。排查线上 DB：小赫是 `OpenClawInstance#10 Hermes Agent小赫`，#113 已产生 `ClawTodo#767/#768` 和 sync_config 消息，说明后端创建待办成功；根因是 `install_skill()` 创建待办后只调用 `notify_claw()`，没有调用 `notify_claw_todo()` 标记待办列表变更，SSE/sidecar 在待办数量未变化或只等 todo 事件时不一定立即感知。修复：`templates/skills.html` 将 fresh/stale 已安装项都改为默认不勾选，文案改成“需要重新分配请手动勾选”；`app/api/skills.py` 在安装/重装提交后改调 `notify_claw_todo(claw_id)`（内部仍会唤醒 SSE）。已部署到 `testserver:/opt/openclaw-web`，备份 `/opt/openclaw-web/_backup_skill_reassign_fix_20260504-004004`；远端校验 `REMOTE_REASSIGN_FIX_STATIC_OK`、`openclaw-web=active`、`/api/v1/system-changelog=200`。注意：远端校验脚本也不要直写中文断言，PowerShell 管道会把中文变 `????`；用 `\uXXXX` ASCII-only 断言。
87. **SSE 待办推送不能只比较 pending 数量，要比较待办 ID 签名**：2026-05-04 用户仍反馈看不到小赫待办。复查 `/api/v1/openclaws/10/todos?enabled_only=false`：#113 TAPD 重装待办 `#767/#768/#769` 都在 API 前 3 条，`enabled=True`、`schedule_type=once`、`today_status=pending`，按 `openclaw_detail.html` 的今日待办过滤可得到 `PENDING_COUNT_BY_TEMPLATE_JS=3`，说明 DB/API/详情页过滤本身有数据。更深层问题在 SSE sidecar：`agent_client.py` 循环只用 `current_count != last_todo_count` 判断是否推 `todos_pending`，如果待办内容变化但数量不变，sidecar 不会收到新列表；`notify_claw_todo()` 又是 gunicorn worker 进程内内存态，多 worker 下不能作为唯一保障。修复：`agent_client.py` 增加 `last_todo_signature/current_signature = tuple(todo.id...)`，待办 ID 签名变化就推送，并保留 `has_todo_change` 快速路径。已部署到 `testserver:/opt/openclaw-web`，备份 `/opt/openclaw-web/_backup_todo_sse_signature_20260504-010543`；校验 `REMOTE_TODO_SSE_SIGNATURE_STATIC_OK`、`openclaw-web=active`、`/api/v1/system-changelog=200`。后续排查若页面仍不显示，优先让浏览器强刷详情页；若 agent 侧仍不处理，再查小赫 sidecar 日志是否收到 `todos_pending` 和是否处于 LLM 冷却/处理中。
88. **网站 500 时先查 MySQL 连接耗尽，SSE 循环不能每 2 秒打 DB**：2026-05-04 用户反馈“网站又无法访问”。外网 `/api/v1/system-changelog` 500/超时，`openclaw-web` active；日志显示 `(1040, Too many connections)`、随后 `QueuePool limit of size 8 overflow 4 reached`，access log 里多个 sidecar `/api/openclaws/<id>/events` 与 `/sidecar-config` 反复 500 重试。先修本机 SSH 私钥 ACL（`icacls ~/.ssh/id_9.134.11.169 /inheritance:r /grant:r <user>:R ...`）恢复 `testserver` 登录；临时重启 `openclaw-web` 释放连接，页面恢复。根因：`config.py` 旧默认 `pool_size=20/max_overflow=30`，4 worker 理论上可开 200 连接，超过 MySQL 上限；降到 `pool_size=8/max_overflow=4` 后仍发现 worker 对 MySQL 保持 20+ ESTABLISHED，继续查到 `agent_client.py` SSE loop 每 2 秒给每条长连接查消息/任务/状态，且每 10 秒写 last_activity，sidecar 重连风暴会打满 DB。修复：消息轮询降到 10s，任务/待办仍 10/30s，`last_activity/status` 写库降到 60s，pymysql 连接加 `connect/read/write_timeout`，并保留待办 ID 签名推送。部署：`config.py` 备份 `/opt/openclaw-web/_backup_db_pool_limit_<timestamp>`，`agent_client.py` 备份 `/opt/openclaw-web/_backup_sse_db_throttle_20260504-014843`；验证外网 `/api/v1/system-changelog`、`/openclaws`、`/skills` 均 200，20 秒后 MySQL socket 统计 Web 侧降为约 `8/6/2/2`，总 28（含其它 Java 服务 10）。
89. **TAPD 实时查询必须对齐 micro-cloud 的 `apiv2.tapd.woa.com`，不要用公网 `api.tapd.cn`**：2026-05-04 #113 小赫仍报 TAPD 401，用户指出 micro-cloud 已部署在 `9.134.11.169:8080` 且 `dao/TapdDao.go` 有 TAPD 操作。复查发现 `TapdDao.TapdInit()` 写死 `api_user=rajqiu/password=6EC...572E`，但 `url_v2 = "http://apiv2.tapd.woa.com/"`；`GetTapdCommonFunc` 拼 `url_v2 + api_url` 并 `req.SetBasicAuth(...)`。在 Hub 服务器验证：`https://api.tapd.cn/quickstart/testauth` 仍 401，但 `http://apiv2.tapd.woa.com/stories/count?workspace_id=70202650` 返回 `status=1,count=222`，`stories`/`bugs` 均 200；旧 `POST http://9.134.11.169:8080/goApi/getTapdCommonFunc api_url=stories/count workspace_id=70202650` 也返回 `count=222`。修复 Hub：`web/app/api/tapd.py` 增加 `TAPD_API_BASE_URL=os.getenv('TAPD_API_BASE_URL','http://apiv2.tapd.woa.com')`，`story-title/stories/bugs/dashboard` 全部改用该 base；部署备份 `/opt/openclaw-web/_backup_tapd_apiv2_20260504-024209`，静态校验 `REMOTE_TAPD_APIV2_STATIC_OK`、服务 active、health 200。#113 文档同步补充：Claw 仍走 Hub `/api/v1`，Hub 内部上游是 `apiv2.tapd.woa.com`，不是公网 `api.tapd.cn`。
90. **SSE 离线清理不能在每次建连时 ORM 批量更新，否则会 deadlock 拖垮业务 API**：2026-05-04 用户反馈 `POST /api/v1/openclaws/14/heartbeat`、`GET /api/v1/openclaws/14/todos`、`/dashboard/stats` 等批量 500。日志 traceback 实际集中在 `/api/openclaws/<id>/events`：`_cleanup_stale_connections()` 每条 SSE 建连都查询所有超时 claw，再 ORM flush 批量 `UPDATE openclaw_instances SET status='offline'`，多个 sidecar 同时重连时并发更新同一批 id，触发 MySQL `(1213) Deadlock found when trying to get lock`，异常未捕获导致请求 500。修复 `agent_client.py`：新增 `_stale_cleanup_lock/_last_stale_cleanup_at`，120s 节流；清理改为独立 pymysql 单条 `UPDATE ... WHERE status IN (...) AND last_activity < %s`，设置 connect/read/write timeout，并捕获异常只记 warning，不影响当前请求。另补 `GET /api/v1/openclaws/<id>/todos/submitted`，因为调用方已使用该路径；目标 claw token 可查本 claw submitted，admin/super_admin 可查。部署备份 `/opt/openclaw-web/_backup_todos_submitted_20260504-114401`。验证：#14 token 访问 heartbeat/todos/todos-submitted/dashboard 均 200；普通 #14 token 访问全局 `/todos/submitted` 为 403（符合权限），龙虾王 admin token 为 200；access log 后续无这批接口 500。注意远端脚本里中文断言仍要避免 PowerShell heredoc 直传，使用 ASCII-only assert。
91. **测试计划页面计划多时不要卡片铺满；报告由 Agent 写 API、页面只读**：2026-05-04 用户要求测试计划展示改为统一下拉框、计划操作增加“报告”、用例执行分页。实现：`templates/testplans.html` 将迭代详情下的计划卡片列表改成 `#plan-select`，选择计划后才渲染详情和操作按钮；操作区新增“报告”按钮，打开只读 modal，Markdown 走前端轻量渲染，HTML 走 sandbox iframe；任务卡片保持原样。`test_plans` 新增 `report_content/report_format/report_updated_by/report_updated_at`，`app/__init__.py` 启动迁移；`GET /api/v1/test-plans/<id>/report` 读取，`POST/PUT .../report` 供 Agent 写入，format 仅 `markdown/html`。`GET /test-plans/<plan_id>/tasks/<task_id>/cases` 增加 `page/page_size`，传参时返回 `{items,total,page,page_size,pages}`，不传时仍兼容旧数组；页面固定 `page_size=50`。`testplan-manager` Skill #136 已补报告 API 与分页说明，并同步线上 DB `skills.id=136.template_content`。部署备份 `/opt/openclaw-web/_backup_testplan_report_20260504-130037`；验证 `DB_AND_ROUTE_OK`，`REPORT_GET 200`，`CASES_PAGE 200 1 50 164`，`/api/v1/system-changelog=200`，`/testplans` 可访问。部署前仍要对 `models.py/__init__.py` 做 prod/local diff stat，确认只有新增无删除再覆盖。
92. **测试任务仍绑定 Claw，但 API/页面必须展示和支持 Claw 对应用户**：2026-05-04 用户指出 `assignee_name` 只是 `assignee_claw_id` 自动映射的 Claw 名，没有单独人名字段，但测试任务需要支持 Claw 对应用户。实现不新增自由文本人名字段，避免任务脱离 Agent；`TestTask.to_dict()` 从 `assignee.owner` 和 `users.username` 返回 `assignee_owner`、`assignee_owner_display_name`、`assignee_owner_user_id`、`assignee_owner_wecom_userid`。`testplans.py` 新增 `_resolve_assignee_claw_id()`，创建/更新任务时可传 `assignee_owner` 或 `assignee_username`，优先用 `User.bound_claw_id`，再按 `OpenClawInstance.owner` 找 Claw；任务列表支持 `?assignee_owner=` 过滤。`testplans.html` 任务卡展示 `用户 + Claw`，任务表单下拉改为 `owner - claw.name`。Skill #136 已补指派语义并同步 DB。部署备份 `/opt/openclaw-web/_backup_testplan_assignee_user_20260504-130703`；验证真实 API：计划 2 任务返回 `Hermes Agent小赫 rajqiu rajqiu`、`小安-自动化测试专家 rajqiu rajqiu`，`/tasks?assignee_owner=rajqiu` 返回 2 条且 owner 全匹配。注意：本次验证期间 SSE 重连再次短暂打满连接池，先 `systemctl restart openclaw-web` 恢复，再用 admin token 直调 API 验证；避免用长时间 `create_app()+test_client` 脚本做线上验证。
93. **测试任务分配展示只显示 owner，不显示 Agent 名**：2026-05-04 用户进一步要求“测试任务分配给 owner，就不显示 agent”。保留 #92 的后台映射和 API 字段不变，仅改展示口径：`templates/testplans.html` 任务卡去掉 `assignee_name`/🤖 Agent 展示，只显示 `assignee_owner_display_name`；新建/编辑任务下拉 label 改为“指派 owner”，选项文本只显示 `owner`（没有 owner 才 fallback 到 claw.name），提示改为“页面只展示 owner，后台会自动映射到对应执行 Agent”。`testplan-manager` Skill #136 同步说明“业务上按 owner 分配和展示，不直接显示 Agent 名；底层仍以 assignee_claw_id 绑定执行 Agent”。部署备份 `/opt/openclaw-web/_backup_testplan_owner_only_20260504-145242`；模板校验 `HAS_AGENT_DISPLAY False`、`HAS_OWNER_LABEL True`、Skill DB 校验包含“按 owner 分配/不直接显示 Agent”。部署后健康探测一度因旧 `/api/openclaws/6/events` SSE 连接池问题超时，重启 `openclaw-web` 后 `/api/v1/system-changelog=200`、`/testplans=200`。
94. **Hermes Agent 额外工作目录必须同时落库、重写 unit 并 daemon-reload**：2026-05-04 用户要求系统部署的 Hermes Agent 卡片详情可添加工作目录，添加路径后有权限访问。实现：`openclaw_instances.work_dirs` 存 JSON 列表；`openclaws.html` 设置弹窗新增“工作目录”页签，每行一个绝对路径；`PUT /api/v1/openclaws/<id>` 仅 owner/绑定者/super_admin 且 Hub 代建成功时可改，后端校验绝对路径、禁用 `/etc`、`/usr`、`/root`、`/opt/openclaw-web` 等系统目录。部署器 `DeployRequest.work_dirs` 参与 `_render_systemd_unit/_render_sidecar_unit`，gateway 和 sidecar 都写入 `ReadWritePaths=<data> /opt/agent_share <work_dirs>`，并设置 `AGENT_WORK_DIRS` 环境变量；部署/重启同步时执行 `mkdir -p`、顶层 `chown <oclaw_id>:openclaw_agents`、`chmod 2770`，重启接口必须重写 unit + `systemctl daemon-reload`，不能只重启服务。线上部署备份 `/opt/openclaw-web/_backup_hermes_workdirs_20260504-150435`；验证 `py_compile` OK、DB 列存在、normalizer 拒绝 `/etc`、`PUT work_dirs` 200、认证渲染 `/openclaws` 含 `settings-tab-workdirs/settings-work-dirs`。
95. **`/root/*` 不能作为 Hermes Agent 工作目录，必须拒绝子路径而非只拒绝 `/root` 本身**：2026-05-04 用户给大赫配置 `/root/racinggo` 后仍 `Permission denied`。原因有两层：`oclaw_14` 无法穿越 `/root` 的 0700 权限，且 gateway unit 还启用 `ProtectHome=true`；`ReadWritePaths=/root/racinggo` 不等于给非 root 用户打开 `/root` 父目录。修复 `normalize_agent_work_dirs()`：系统目录判断从 `path == blocked` 改为 `path == blocked or path.startswith(blocked + '/')`，但要特判 `blocked='/'`，否则会误拒绝所有绝对路径；UI 提示改为“系统根目录及其子目录”并建议 `/opt/agent_work/<项目>` 或 `/opt/agent_share/<项目>`。线上清理大赫 `work_dirs ['\/root\/racinggo'] => []`，部署备份 `/opt/openclaw-web/_backup_workdir_rootfix_20260504-153249`，校验 `REMOTE_WORKDIR_ROOT_BLOCK_OK`、`/opt/agent_work/racinggo` 可通过、服务 active。注意：尝试调用大赫 restart sync 时目标机 SSH/命令链路卡住超过 2 分钟，已重启 `openclaw-web` 清掉 worker；若要访问 RacingGO，应先把代码移动/复制到非 `/root` 路径再配置。
96. **Skill/Rule 审核权限应允许 admin，不应只限 super_admin**：2026-05-06 用户反馈“龙虾王没有权限审核 skill，不符合预期”。排查确认 `skills.py review_skill` 与 `rules.py review_rule` 都写成 `user.role == 'super_admin'` 才可审核，导致 `admin`（含龙虾王）被 403。修复为 `user.role in ('super_admin', 'admin')`，并统一错误文案“仅管理员可审核 Skill/Rule”。部署备份（重试版）`/opt/openclaw-web/_backup_admin_skill_review_retry_20260506-165959`，烟测以 admin 用户 session 调用审核接口（传非法状态做鉴权探针）返回 400（而非 403），确认已进入审核逻辑：`ADMIN_REVIEW_API_AUTH_OK`。

*最后更新: 2026-05-06（Skill/Rule 审核权限开放给 admin；经验至 96）*
