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

### 生产服务器 (openclaw-manager)
- **SSH**: 9.134.11.169:36000, root/Test@speed2021
- **MySQL**: booster/booster, 数据库 `openclaw_manager`
- **Web**: Flask 运行在 5000 端口，通过 nginx 代理到 8088
- **服务**: systemd `openclaw-web.service`
- **代码位置**: `/opt/openclaw-web/`
- **访问地址**: http://9.134.11.169:8088

### 部署方式
- 通过 AnyDev 云研发跳转：AnyDev IP 21.214.206.189 → 目标服务器 9.134.11.169
- AnyDev 命令执行：webshell → ssh 到目标服务器

### MySQL 数据库 (openclaw_manager)
当前已有 13 个表：
- agents, alembic_version, conversations, daily_reports
- knowledge_distributions, knowledge_entries, messages
- modules, openclaw_instances, openclaw_skills
- projects, skills, system_config

*最后更新: 2026-03-26*

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
- **Docker**：per-claw 目录 `/opt/openclaw-agents/claw-<id>-<safe_name>/`，容器名 `hermes-agent-claw-<id>`，需目标机 Docker 可用且能拉镜像。
- **Systemd**（应对 CentOS 7 + Docker 1.13.1、compose/镜像拉取失败等）：**不**远程安装 Hermes；假设 `HERMES_HOME` 下已有 `venv` 且 `python -m hermes_agent` 可跑；Hub 只写 `config/config.yaml`、`config/.env`，生成 `/etc/systemd/system/hermes-agent-claw-<id>.service`，`daemon-reload` + `enable` + `restart`，用 `EnvironmentFile=` 注入敏感环境变量。
- **权限**：创建/重部署 Agent **仅 `super_admin`**；后端 `openclaws.py` + `agent_deployments.py` POST；前端 `openclaws.html` 用 `isSuperAdmin` 隐藏整块 UI 且提交时 `createAgent = isSuperAdmin && toggle`。
- **OpenClaw 创建与部署解耦**：部署失败不回滚 OpenClaw；状态在 `agent_deployments` 表，前端轮询 `/api/v1/openclaws/<id>/agent-deployments/latest`。

### 关键代码路径

| 区域 | 文件 |
|------|------|
| 模型 | `web/app/models.py` — `AgentDeployment`（`deploy_method` docker/systemd；`container_name` systemd 时存 unit 名；`image` systemd 时为空） |
| 部署执行 | `web/app/services/agent_deployer.py` — `_deploy_docker` / `_deploy_systemd`，`_render_env_file` / `_render_systemd_unit` |
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

1. **老机选 systemd**：`HERMES_HOME` 填如 `/opt/hermes-xiaohe`，先 SSH 验证 `venv/bin/python -m hermes_agent`；Hub 写 `config/.env` 后 unit 里 `ExecStartPre` 会建 `sessions`/`logs` 并兜底空 `sessions.json`。
2. **Docker 失败前置提示**：`_deploy_docker` 里 `docker --version` 失败会提示可改用 `deploy_method=systemd`。
3. **生产部署后**：确保 DB 有 `agent_deployments` 表（`web/app/__init__.py` 自动迁移若已包含则随启动建表）；目标机装 `paramiko` 所在环境即 Hub 进程环境。
4. **Windows 本机跑 bash 语法检查**：若无 `bash`，可用 `C:\Program Files\Git\bin\bash.exe -n script.sh`。

### 经验沉淀（新增编号接在 26 后）

27. **同一 DB 字段复用要文档化**：`AgentDeployment.container_name` 在 systemd 下表示 **unit 文件名**，前端用 `deploy_method` 切换「容器 / Systemd unit」文案，避免运维误解。
28. **systemd 模式不做远程 pip/git**：外网、私库凭据、版本锁定都留在人工准备阶段；Hub 只负责 **配置 + unit + 启停**，失败面最小。
29. **super_admin 双端一致**：仅藏前端不够，必须在 `create_openclaw` 与 `POST .../agent-deployments` 都拒绝非 super_admin，避免 API 直调绕过。

*最后更新: 2026-04-28（新增 Section「Hub 代建 Hermes Agent + systemd」+ 经验 27-29）*
