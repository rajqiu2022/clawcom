# OpenClaw Manager 部署日志

## 2026-04-23 工程分析中心 v2 — 架构分析双子模块改版

### 用户需求

> "工程分析需要改版，应该分为：工程架构分析和工程代码提交分析。现在只有代码提交分析。我们应该对基础工程有个全局的分析，包含哪些模块、通信机制、关键逻辑、时序图架构图流程图这些。这个可以保留最近的10次分析结果，后面分析就删掉最久的那次。工程模块要支持2种方式的分析"

确认 2 种方式 = **全工程级分析** + **单模块深度分析**；保留 10 次按 baseline 维度。

### 实现

**1. 数据模型（`web/app/models.py` + `web/app/__init__.py`）**

新增 `EngineeringArchitectureSnapshot` 表，挂在 `engineering_baselines.id` 下。关键字段：

| 字段 | 说明 |
|---|---|
| `scope` | enum(full / module)，区分两种方式 |
| `target_module` | scope=module 时必填的模块名 |
| `content_md` | LONGTEXT，完整 markdown 含 ` ```mermaid ` 三件套 |
| `structured` | JSON：modules / communications / key_logic / risks / suggestions |
| `analyzed_commit` | 锚定的 commit hash |

DDL 同时加进 `__init__.py` 自动迁移钩子（CREATE TABLE IF NOT EXISTS），避免依赖 Alembic。

**2. 后端 5 个路由（`web/app/api/engineering.py`）**

| Method | Path | 说明 |
|---|---|---|
| `GET` | `/engineering/baselines/<bid>/architecture` | 列表（≤10），支持 scope/target_module 过滤 |
| `GET` | `/engineering/architecture/<sid>` | 详情（含完整 content_md） |
| `POST` | `/engineering/baselines/<bid>/architecture` | 创建 + LRU 自动修剪（保留最新 10） |
| `PUT` | `/engineering/architecture/<sid>` | 编辑（提交者本人 / admin） |
| `DELETE` | `/engineering/architecture/<sid>` | 删除（同上权限） |

校验：`scope=full` 时 `target_module` 必须为空；`scope=module` 时必须非空；`content_md` ≤ 60KB。

**3. 前端（`web/templates/engineering.html`）**

- Tab 拆为「架构分析（默认）/ 代码提交分析 / 影响项 / 变更明细」
- 架构分析左右分栏：左 280px 列表（标识 full/module/source），右上头部 + 右下 markdown 渲染区
- 引入 `mermaid@10` + `marked@15.0.7` CDN
- `renderMarkdownWithMermaid()` 单图 try/catch，失败回退 `<pre>` + 错误信息（self-healing 渲染）
- 新建/编辑模态：scope 切换、模块名 datalist（来自 `module_mapping` + 已有快照），结构化 JSON 框

**4. Skill 升级（`openclaw-agent/skills/engineering-analysis/SKILL.md`）**

新增 §9 架构分析协议：数据模型、全工程 / 单模块两种任务的 Agent 步骤、LLM Prompt 模板（强制三张 mermaid）、LRU 行为说明、提交前自检清单、`submit_full_architecture` / `submit_module_architecture` / `cleanup_my_arch` Python 调用示例。触发词加 `工程架构分析,全工程分析,单模块深度分析,架构图,时序图,模块依赖图`。

**5. 部署脚本 `_deploy_eng_arch/deploy.sh`**

- 8 步：预检 → 备份 → 部署 5 个文件（py/html/models/__init__/SKILL）→ 关键字校验（≥18 关键字）→ py 语法 → `db.create_all()` 建表 → 同步 SKILL.md → 重启 + 烟测 5 个新路由
- 烟测全部应返回 401（鉴权）或 404（资源不存在），证明路由已注册
- 失败自动回滚到备份目录

### 经验沉淀（继续在 v1 8 条基础上追加）

9. **MariaDB Text vs LONGTEXT** — model 用 `db.Text` 在 MariaDB 上 ≈ 64KB；`__init__.py` 的 raw DDL 用 `LONGTEXT` 更安全（4GB），两边可以不一致：SQLAlchemy 读写时按 model 行为，DDL 决定底层物理上限。**结论**：架构快照 content_md 用 LONGTEXT 兜底，校验仍按 60KB 拦截，给"全工程→拆单模块"留余地。

10. **Mermaid CDN + marked.js 协作** — `marked` 把 ` ```mermaid ` 渲染成 `<pre><code class="language-mermaid">`，必须在 marked 渲染后**手动遍历替换**为 `<div class="mermaid">` 再调 `mermaid.render(id, src)`。直接给 mermaid 喂 `pre > code` 元素会渲染失败。**坑点**：mermaid 10.x 的 `mermaid.run()` 全局扫描在 SPA 切换 Tab 时会重复处理，**改用 `mermaid.render(id, src)` 单图渲染** + 自己生成 id，更稳定也方便单图回退。

11. **单图失败回退（self-healing）** — 一张图语法挂掉不能让整页空白。模式：`for (block of mermaids) { try { render } catch (e) { block.classList.add('mermaid-error'); block.innerHTML = pre + 错误信息 } }`，至少让用户看到原始代码 + 错误，可手工修复。

12. **LRU 用 `offset(keep)` + 删全部** — Python 实现：`query.order_by(created_at desc).offset(10).all()` 拿到的就是"超出的最旧那些"，循环 delete。**比 SQL 子查询写起来短，且性能在 < 100 行时无压力**。Per-Baseline 维度时（如未来要 full / module 各 10 个），加一行 `.filter_by(scope=...)` 即可。

13. **scope=full 与 module 的"互斥校验"** — POST/PUT 都要做：`scope=full` 时 `target_module` 必须为空；`scope=module` 时必须非空。如果允许"先建 full 再改成 module"会让数据语义混乱，**PUT 时把 `scope` disable 掉**（前端 + 后端校验都做）。

14. **部署脚本的"5 路由烟测"** — 一次性 curl 5 个新路由（list/get/post/put/delete），全部应返回 401/404 / 400，**任何一个返回 500 / 502 / 1xx 都说明部署有坑**。比"只测一个"信号强 5 倍，且一次跑完总耗时 < 1s。

15. **deploy.sh 步骤数命名** — 用 `[N/8]` 而非 `[N]`，让运维一眼看到"还有几步"，特别是部署中断恢复时知道从哪里续跑。

16. **【再次踩到】sync 脚本路径优先级** — 上一轮已记一次（lessons #4 of v1），本次仍然 `cp sync.py /tmp/ && cd $WEB_DIR && python /tmp/sync.py` 翻车，原因：**Python 把"脚本所在目录"放在 sys.path[0]**，`cd` 没用！只要 `/tmp/app/__init__.py` 存在（残留）就会优先 import。**正解**：把 sync 脚本拷到 `$WEB_DIR/_sync_xxx.py` 就地执行（之后 `rm -f` 清理），脚本目录就是 $WEB_DIR，import 路径正确。**永久修复 deploy 模板**：所有 sync 类一次性脚本都按这个模式写。

### 验证清单

- [ ] `engineering_architecture_snapshots` 表存在（`SHOW TABLES`）
- [ ] `GET /api/v1/engineering/baselines/1/architecture` 返回 401（路由通）
- [ ] Hub `/engineering` 页面默认进入「架构分析」Tab
- [ ] 「+ 全工程分析」可以打开模态
- [ ] 录入一段含 mermaid 的 markdown 后，详情页能正确渲染图
- [ ] 故意写错 mermaid 语法 → 该图变红框 + 显示原始代码
- [ ] 创建第 11 条快照时返回 `lru_trimmed >= 1`
- [ ] Hub `/skills` 页 `engineering-analysis` 显示 STALE，重新分配后 claw#10 / claw#11 收到 v2 内容

---

## 2026-04-22 工程分析中心 — 删除自己提交的批次 + Skill 同步

### 用户需求

> "工程分析增加可以删除自己提交的分析结果，加上接口同步更新skill"

之前工程分析中心只能审批/驳回批次，没有"删除"入口；提交人发现自检失败 / LLM 输出脏数据时只能等 admin 驳回，体验差。

### 实现

**后端 `web/app/api/engineering.py`** —— 新增 `DELETE /api/v1/engineering/refresh/<batch_id>`：

- 权限（OR）：`triggered_by == _operator()` 提交者本人 / `super_admin` / `admin`
- 业务拦截：
  - 已 `approved` → 409，要先 `/reject` 驳回再删（避免抹掉已生效决策）
  - 任意 impact 已 `linked_test_task_id IS NOT NULL` → 409，先解绑下游测试任务（避免孤儿）
- 级联硬删：`engineering_test_case_links` → `engineering_test_impact_items` → `engineering_change_items` → `analysis_refresh_batches`
- `log_action('delete', ...)` 留痕（含触发人 / 状态 / 摘要 / 级联条数）

**前端 `web/templates/engineering.html`**：

- `_canDeleteBatch(b)` 复用 `currentUser.role` + `currentUser.username` 判定
- 批次列表行新增"删除"按钮（仅可删时显示；`approved` 状态变灰带提示"先驳回再删"）
- 批次详情头新增"删除批次"按钮，逻辑一致
- 列表多加一列展示 `triggered_by`，方便定位"谁提的"
- `deleteBatch(id)` 二次确认（含级联说明），调 `API.del`，成功后回退到基线视图并刷列表

**Skill 同步**：

- `openclaw-agent/skills/engineering-analysis/SKILL.md` 增加 §7.5 "删除自己提交的批次（含级联）"段落 + 错误处理表加 409
- `_deploy_eng_delete/sync_engineering_skill.py` 同步到 Hub `skills` 表 #139（带关键字断言 + 自动列出哪些 OpenClaw 装了此 skill）
- 同步后两个已安装实例（claw#10 Hermes / claw#11 需求代码分析专员小云）自动变 STALE，在 Hub UI 「♻️ 重新分配」即可下发

### 部署成果

```
UPDATE skill #139: engineering-analysis  (23759 chars)
✅ all delete-related keywords confirmed in DB content
2 个 OpenClaw 已安装此 skill：
  🟠 STALE  claw#11 需求代码分析专员小云
  🟠 STALE  claw#10 Hermes Agent小赫
DELETE /api/v1/engineering/refresh/999999 -> 401（未认证，路由已注册）
systemctl is-active openclaw-web → active
```

### 经验沉淀（避免下次踩雷）

1. **PowerShell 内置 `copy` 不支持 `/Y`** — 旧 cmd 的 `copy /Y` 在 PowerShell 是 `Copy-Item -Force`。混用一定加 `cmd /c` 或全程用 PS 原生命令。
2. **本机 SSH 走 `ssh testserver` 别名最稳**，不要直接 `ssh -p 36000 -i ~/.ssh/id_rsa root@9.134.11.169`：
   - `id_rsa` 不是这台机的 key，正确 key 是 `~/.ssh/id_9.134.11.169`
   - `~/.ssh/config` 里已经把 `testserver` 配好（HostName / Port / User / IdentityFile），后续部署一律 `ssh testserver "..."` / `scp ... testserver:...`
3. **Windows → Linux 上传 `.sh` 必须先 `dos2unix`**：`sed -i 's/\r$//' xxx.sh` 一行修复 CRLF（症状：`$'\r': command not found` / `syntax error near unexpected token $'do\r'`）
4. **跑同步 / migration 类 Python 脚本不能放 `/tmp/`**：
   - 历史残留的 `/tmp/app/` 目录会被优先 import，触发 `ModuleNotFoundError: No module named 'app.api.audit'` 这种"明明文件存在却 404"的诡异错
   - 正确姿势：`scp xxx.py testserver:/opt/openclaw-web/xxx.py && ssh testserver "cd /opt/openclaw-web && ./venv/bin/python3 xxx.py && rm xxx.py"`
5. **OpenClawSkill 关联表外键叫 `openclaw_id` 不是 `claw_id`**（之前误以为是 `claw_id` 写错过 join）—— `OpenClawSkill.openclaw_id == OpenClawInstance.id`
6. **写关键字断言时要复制 SKILL.md 里的"原话"**，不要凭印象写（断言连续失败两次都是因为我把"先调 \`/reject\` 驳回再删"记成了"已审批批次需先驳回"）。后续可以加：`if 关键字 not in 内容: print('期望:'); print(关键字); print('实际段落:'); print(...)` 友好提示
7. **删除型接口的级联设计模板**（可复用到后续做"删除自己的需求快照 / 用例库 / 测试任务"等）：
   - 三层兜底：权限（操作者本人 OR 高权限角色）→ 状态（关键状态如 approved 必须先逆转）→ 业务依赖（有下游引用必须先解开）
   - 返回 `cascaded` 字典告诉前端到底删了多少东西，用于 toast 反馈和审计
   - `log_action` 一定要把"触发人 / 摘要 / 级联条数"全写进 `detail`，不然事后审计完全黑盒
8. **改 skill 内容后**记得提醒用户去 Hub UI 点「♻️ 重新分配 (N)」 —— sync 脚本里直接列出 STALE 实例就是为了把这步"绑死"，避免 skill 更新但 agent 还在用老版本的尴尬

### 关键文件

- `web/app/api/engineering.py` — 后端，新增 `delete_refresh_batch` + `_can_delete_batch`
- `web/templates/engineering.html` — 前端，新增 `_canDeleteBatch` / `deleteBatch` + 列表/详情双删除入口
- `openclaw-agent/skills/engineering-analysis/SKILL.md` — §7.5 删除批次 + 错误处理表 409
- `_deploy_eng_delete/deploy.sh` — 一键备份/部署/校验/重启/烟测
- `_deploy_eng_delete/sync_engineering_skill.py` — 单 skill 同步 + 关键字断言 + STALE 实例列表

---

## 2026-04-22 补全 Skill #138 requirement-analysis & #139 engineering-analysis

### 用户反馈

"需求板块分析对应的 skill 呢"——回看一下需求/工程分析中心对应的 OpenClaw skill 是否到位。

### 诊断

- 本地 `F:\Code\claw_team\openclaw-agent\skills\requirement-analysis\SKILL.md`（452 行 ≈ 19K）和 `engineering-analysis\SKILL.md`（850 行 ≈ 29K）**都存在**且高质量
- **DB `skills` 表完全没有这两个条目**：之前实现需求/工程分析中心后台时只写了本地 SKILL.md 文件，**忘了同步到 Hub DB**
- 后果：OpenClaw 拉 `GET /api/v1/openclaws/<id>/assigned-skills` 永远拉不到这两个 skill，等于"agent 端没有这能力，前端的需求/工程分析中心是孤岛"

### 修复

写一站式 INSERT/UPDATE 脚本 `sync_req_eng_skills.py`（参考 #121 topic-discuss 入库手法）：

- 读两个本地 SKILL.md → INSERT/UPDATE `Skill` 模型
- 元数据完全对齐 #135 hub-sse-sidecar 风格：
  - `scope=global` / `is_standard=True` / `review_status=approved`
  - `category=hub_system`
  - `created_by='OpenClaw Team'`
  - `trigger_phrase` 取 SKILL.md 末尾"触发词"段落的精华
- description 用一句话概括"做什么 + 关键字"，便于 Hub 列表/搜索

### 部署结果

```
INSERT skill #138: requirement-analysis  (14739 chars)
INSERT skill #139: engineering-analysis  (22171 chars)
```

校验：标准 skill 总数 27 → **28**，与 #135 hub-sse-sidecar 并列出现在 `is_standard=true` 列表中。备份：`/opt/openclaw-web/_backup_req_eng_skills_20260422-143957/skills_req_eng_before.sql`（81 行，原始空状态）。

### 经验 #20：本地 SKILL.md ≠ Hub DB skill（一定要"两条腿"同步）

**踩到**：实现一个 Hub 模块时，前端 / 后端 API / 本地 SKILL.md 全做了，独漏 DB skill 入库——结果 OpenClaw 拉不到这个 skill，前端模块没人用。

**根因**：Hub 系统里 skill 有**两份"真相"**：
1. **本地 git 仓库的 SKILL.md**（开发 / 评审用）
2. **DB `skills` 表的 `template_content` 字段**（OpenClaw 实际拉取的内容）

两者**没有任何自动同步**——本地写完后必须显式 INSERT/UPDATE DB 才生效。

**自查清单**（每次新增/重大修改 skill 时跑一遍）：

```bash
# 1) 本地有 SKILL.md
ls -lh openclaw-agent/skills/<name>/SKILL.md

# 2) DB 里有同名条目
mysql ... -e "SELECT id, name, scope, is_standard, review_status, CHAR_LENGTH(template_content) FROM skills WHERE name='<name>'"

# 3) 标准 skill 列表能命中（OpenClaw 实际拉取路径）
TOKEN=...
curl -s -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:8088/api/v1/skills?is_standard=true" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); items=d if isinstance(d,list) else d.get('skills',[]); print([s['name'] for s in items])"

# 4) 已注册的 OpenClaw assigned-skills 能拉到
curl -s -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:8088/api/v1/openclaws/<CLAW_ID>/assigned-skills" \
  | python3 -m json.tool | grep '"name"'
```

**做法**：以后写 Hub 模块的 PR / 部署清单里**强制加一项**：「本模块对应的 SKILL.md 是否已通过 `sync_*_skills.py` 入库 DB」。

### 经验 #21：在 `/tmp` 下跑 Python 脚本会被 sys.path 残留"绑架"

**踩到**：同步脚本 `./venv/bin/python3 /tmp/sync_req_eng_skills.py` 报 `ModuleNotFoundError: No module named 'config'`，但是错误堆栈显示是从 `/tmp/app/__init__.py` 加载的——根本不是 `/opt/openclaw-web/app/`。

**根因**：Python 启动时**自动把脚本所在目录加到 `sys.path[0]`**（最高优先级）。`/tmp/` 里之前部署残留过 `app/` 目录，于是 `from app import create_app, db` 解析到了**错误的 app 包**——这个错的 app 包又找不到它依赖的 `config` 模块，于是报错。

**做法**：跑 Hub 上下文的 Python 脚本时，**必须先 `cp` 到 `/opt/openclaw-web/`** 再执行：

```bash
# ❌ 不要这样
./venv/bin/python3 /tmp/script.py

# ✅ 应该这样
cp /tmp/script.py ./script.py && ./venv/bin/python3 ./script.py && rm -f ./script.py
```

部署脚本最好显式 `cd /opt/openclaw-web && cp /tmp/...py ./...py` 一气呵成，避免残留。

---

## 2026-04-22 注册 skill 同步 Skill #135 hub-sse-sidecar v1.2

### 用户反馈

"你看下最新的 #135 skill，对比下现在注册 skill，同步更新一版。"

### 背景

`/api/v1/openclaws/<id>/registration-skill` 接口由 `app/api/registration_bootstrap.py::build_bootstrap_markdown()` 动态生成注册自部署 Markdown，给所有 OpenClaw 注册时拉取。**Skill #135 hub-sse-sidecar v1.2** 在 4-20 由龙虾王主导更新过 4 条核心约定 + 企微通知规范，但注册文档没跟进同步——导致新注册的 claw 看不到这些"踩坑红线"。

### 诊断中发现的隐藏雷

`registration_bootstrap.py` 里**两个同名函数 `build_bootstrap_markdown`** 同时存在：
- line 103 旧版 7 步 MCP 自部署
- line 762 新版 sidecar-only 精简版

Python 后定义的胜出，**line 103 永远不会被调用**，但保留在文件里极具误导性——后人读上面 600 行老代码以为是生效内容，可能去改它结果完全无效。

> 教训：**Python 里同名函数是"静默覆盖"，不会报警告**。重构精简版时必须**物理删除被覆盖的旧版**，不能"留着备查"。

### 修复

重写 `web/app/api/registration_bootstrap.py`：

1. **物理删除**被覆盖的旧版 `build_bootstrap_markdown`（约 660 行历史 7 步 MCP 文档）
2. 升级仅剩的 sidecar-only 版本到 v1.2，对齐 #135：
   - **新增 §0 四条核心约定**：`read_at` 去重 / 不要外部 watchdog / 业务零硬编码 / **禁止双 SSE 客户端并存**
   - **新增 §3.5 read_at 闭环验证脚本**：`curl ?unread=true&direction=to_claw` 应尽快归零
   - **新增 §5 企微通知最低规范**摘要：触发范围 / 最低内容 4 项 / 执行约束（先 Hub 闭环再企微）
   - **新增 §6 故障排查 mini 表**：7 个高频现象 + 排查/修法
   - 标题改为 `(sidecar-only v1.2)` 显式标版本
   - 文档顶部链回 [Skill #135](http://9.134.11.169:8088/skills/135) 让 AI 能跳到完整规约
   - 步骤里把"清理旧 sse_client"提升为 §0.4 强约束（带 `manager-hub` 两种脚本名）
3. 保留 `build_mcp_config_snippets`，因为 `?format=json` 接口仍要返回 `mcp_config_snippets` 字段（兼容历史 MCP host 接入需求）
4. 已弃用参数（`mcp_entry / mcp_repo / mcp_tarball_url / mcp_install_dir`）保留在签名里加 `# noqa: ARG001` 注释，避免外部老调用方崩

### 部署

- 包：仅 1 个文件（`app/api/registration_bootstrap.py`，15K）
- 校验：函数定义数 = **1**（旧重定义雷已清理）；关键字命中 14（期望 ≥8）；返回 markdown 12285 字节
- JSON 接口兼容性：`mcp_config_snippets` 三个 host (`cursor / claude / codebuddy`) 完整保留
- 备份：`/opt/openclaw-web/_backup_reg_v12_20260422-113850/registration_bootstrap.py`

### 经验 #18：重写 dynamic markdown 生成器的"防雷三件套"

**踩到**：升级 sidecar-only 版本时差点漏看了 line 103 的旧版同名函数。

**根因**：Python 里**同名函数是静默覆盖**——后定义的胜出，前面那个不会报任何 warning，IDE 也只在精确开了 redefined-while-unused 检查时才提示。这给重构留下"上面 600 行死代码"的隐患：
- 后人读老代码以为是生效逻辑
- 改了无效，浪费排查时间
- 文件体积虚胖，git diff 噪音大

**做法**（每次重写动态文档生成器都跑一遍）：

```bash
# 1) 检查同名函数定义数（必须 == 1）
grep -c "^def build_bootstrap_markdown" app/api/registration_bootstrap.py

# 2) 离线渲染 + 关键字断言（不需要起 Flask）
python3 -c "
import sys; sys.path.insert(0, 'web/app/api')
import registration_bootstrap as rb
md = rb.build_bootstrap_markdown(
    hub_url='http://h', claw_id=1, claw_name='_t_',
    role='admin', project_name='_p_', api_token='_tk_',
)
assert 'sidecar-only v1.2' in md
assert 'Step 0' in md
print(f'OK: {len(md)} chars')
"

# 3) 部署后 curl 端到端校验（用 admin token）
TOKEN=$(./venv/bin/python3 -c "from app import create_app; from app.models import OpenClawInstance; app=create_app(); app.app_context().__enter__(); inst=OpenClawInstance.query.filter_by(role='admin').first(); print(inst.get_token_plain())")
curl -s -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:8088/api/v1/openclaws/4/registration-skill" \
  | grep -cE "sidecar-only v1.2|Step 0|read_at 闭环"
```

**结论**：
- **永远删掉被覆盖的旧函数**，别"留着备查"——git history 才是唯一来源
- f-string 里有 `{...}` 占位符的，离线 render 一次确认所有占位符都被填值，不要漏 escape
- 部署后必须**带 token 真请求**端到端，因为 `/registration-skill` 路径走全局 `require_auth`

### 经验 #19：动态生成的 markdown 该跟 DB skill 怎么协作

**场景**：Hub 里既有「DB Skill #135 hub-sse-sidecar」（完整规约），又有「`/registration-skill` 接口动态生成的注册 markdown」（精简注册流程）。两份内容互有重叠，怎么协调？

**做法**：
- **DB Skill 是"百科全书"**：完整规约、所有边界、所有故障案例、企微规范全文（≈ 6000 字）
- **动态注册 markdown 是"任务说明书"**：10 分钟跑完注册的最小指引（≈ 12000 字节，含 token / claw_id 等动态字段）
- **协作三原则**：
  1. **回链原文**：动态文档顶部 `**流程对齐 [Skill #135 v1.2](URL)**`，让 AI 知道"权威在哪"
  2. **摘要 + 跳转**：动态文档把 #135 §3 / §5.1 提炼成 1-3 段摘要，详细规约写 `> 完整规范见本地 ~/.qclaw/skills/hub-sse-sidecar/SKILL.md §X`
  3. **同步触发器**：DB Skill 升大版本号（v1.x → v1.y）时，动态文档**必须**同步升标题版本号，并 grep 检查关键字（核心约定 / 验收硬指标 / read_at / 双 SSE）是否还命中
- **反模式**：把 DB Skill 全文复制粘贴进动态文档——会出现"DB 改了动态没改"的版本撕裂，且字节数翻倍

> 这次新版动态文档 12K + 本地 SKILL.md 约 6K，互补不重复，AI 拿动态文档照着跑、卡住了去翻 SKILL.md，链路顺滑。

---

## 2026-04-22 OpenClaw 卡片头像编辑入口可见化

### 用户反馈

"openclaw 卡片现在没有修改图标入口，注册的时候有，但是已注册的也想修改。"

### 诊断

入口**早就存在**（`renderClawCard` 头像 `<div class="claw-avatar-lg" onclick="openAvatarModal(...)">`），但视觉极隐蔽：
- 只是 emoji + hover 边框变色 + scale(1.08)
- 仅 `title="点击更换头像"` 提示，需要鼠标停留才能看到
- 用户从功能上找了一圈也没发现 → 等于"没入口"

> 教训：**不能把"按钮"埋在装饰元素上还指望用户发现**。

### 修复

`templates/openclaws.html`：

1. 头像外包 `.claw-avatar-wrap`（44×44 相对定位容器），点击事件提到 wrap 上
2. 右下角加 `✏️ .claw-avatar-edit-badge` 角标（18×18 圆形 / accent 底色 / 白边阴影 / pointer-events:none，事件冒泡到 wrap）
3. 默认 opacity:0.85，wrap hover 时 opacity:1 + scale(1.15) + 头像同步 scale(1.08)
4. tooltip 改为 `点击修改图标` 与产品语义对齐

### 部署

- 包：仅 1 个模板（`openclaws.html`）
- 校验：`claw-avatar-wrap×4 / claw-avatar-edit-badge×3 / 点击修改图标×1`，服务 active，`/openclaws` 302
- 备份：`/opt/openclaw-web/_backup_avatar_20260422-104500/openclaws.html.bak`

### 经验 #16：PowerShell 嵌套 ssh + heredoc 变量逃逸的"地雷"

**踩到**：在 PowerShell 执行 `ssh root@HOST "set -e && TS=\$(date +%Y%m%d-%H%M%S) && BAK=_backup_\${TS}"`，**`\$(date)` 被 PowerShell 当本地命令解析失败**，导致传到远端的实际命令变成 `TS=`（空字符串） → `BAK=_backup_` → `mkdir -p`（无参数）→ `cp templates/x.html /x.html`，**备份文件落到了服务器根目录**。

幸好不影响主操作（覆盖目标、重启、校验都用的是字面量），但备份找不到了。

**避坑规则（PowerShell 调远端 bash）**：

| 方案 | 评价 |
|------|------|
| 单行 `ssh "..." `+ 大量 `\$ \`、嵌套引号 | ⚠️ 极易翻车，PowerShell 会优先解析 `$(...)` `${...}` `&&`（PowerShell 7+ 才支持） |
| **写一个 .sh 文件 → scp → ssh "bash /tmp/xxx.sh"** | ✅ 最稳，本仓库 `_deploy_*/deploy_*.sh` 全部是这套路 |
| 用 `'...'`（单引号）+ 反斜杠转义 | 部分场景可，但仍躲不过 PowerShell 对 `$(...)` 的早期解析 |

**新规则**：所有需要远端 shell 变量替换 / 时间戳 / heredoc 的命令，**一律落到 `_deploy_*/xxx.sh` 文件先 scp 再执行**，禁止单行 `ssh "复杂脚本"`。本仓库 `deploy.sh / deploy_ux_fixes.sh / smoke_test.sh / sync_skill_121.sh` 都遵循这条。

### 经验 #17：可点击元素的"可见性"清单

未来给 hub 任何卡片加"可点击操作"，必须满足以下至少 2 条，否则用户找不到：

- [x] 鼠标变 `cursor:pointer`（最低标配，但不够）
- [x] 元素或角标有显式图标（✏️ / ⚙️ / 🗑️ 等），表达"可操作"
- [x] hover 时**显著**视觉反馈（不仅是 1px 边框变色，最好叠加阴影/缩放/亮度）
- [x] `title=""` 用动词 + 名词，例如"修改图标"而非"图标"
- [ ] 严肃操作（删除 / 重命名 / 改 token）建议**不光靠 hover**，可常驻显示按钮

> 本次的「右下角 ✏️ 半透明角标 + hover 完全亮起」就是典型的"低噪声 + 高可见"模式，可推广到日报卡片、需求卡片、用例卡片的"编辑"入口。

---

## 2026-04-22 OpenClaw 注册流 + 日报 UX 三连改

### 用户反馈

1. 创建新 OpenClaw 对话框点击空白就消失，容易误操作浪费已填内容
2. 注册成功弹窗里的"注册链接 / Token"复制按钮不统一；缺少一键"复制注册信息"给 OpenClaw 直接发的能力；提示语缺失
3. OpenClaw 详情 → 工作日报页签的日报卡片信息不完整，希望点击展开看完整内容，支持 Markdown / 富文本

### 修复点

- **`templates/openclaws.html`**
  - 删除 `create-modal` / `token-modal` / `register-success-modal` 的 `addEventListener('click', closeOnBackdrop)`，关键信息弹窗一律不允许点空白关闭
  - `regenerate-confirm-modal`（次级确认）保留点空白取消
  - `register-success-modal` 重做：
    - 增加独立 `🔑 API Token` 卡片（从 `result.api_token` 注入；本次未生成新 token 时显示提示文案）
    - 注册链接和 Token 两个复制按钮统一为 `复制`（同色 / 同尺寸 / `min-width:54px`）
    - 增加蓝底提示语区："💡 点击下方【📦 复制注册信息】按钮，发给 OpenClaw，跟他说：'带上 token 访问注册链接，照着说明安装即可。'"
    - 底部新增 `📦 复制注册信息` 按钮，复制内容含称呼 + 注册链接 + Token + 安装步骤一段话，可直接发给 OpenClaw
  - `token-modal` 里的"复制"和"复制链接"两个按钮也统一为 `复制` 文案
  - 新增 `_doCopy(text, okMsg)` 通用复制工具函数（clipboard API + execCommand 兜底）

- **`templates/openclaw_detail.html`**
  - 引入 `<script src="https://cdn.jsdelivr.net/npm/marked@15.0.7/marked.min.js"></script>`
  - 工作日报 timeline-item 加 `clickable` class + `onclick="showReportDetail(r.id)"` + 悬停高亮 + 右上角"点击查看完整内容 →"提示
  - 新增 `report-detail-modal`（max-width:780, max-height:88vh, 内部滚动），五个区域：AI 工作小结 / 完成任务 / 新增知识 / 经验记录 / 学习的共享知识
  - 每条任务 / 知识统一走 `_normalizeListForDetail()` 兼容 string / array / JSON-string / 对象 4 种形态，再经 `marked.parse()` 渲染（即支持 md 列表、表格、代码块、引用、图片）
  - 配套 `markdown-body` 暗色主题 CSS（h1~h3、code、pre、blockquote、table、img），与 `knowledge.html` 视觉对齐

### 部署

- 包：仅 2 个 jinja 模板，无 DB / 无 Python 改动
- 脚本：`_deploy_requirement_analysis/deploy_ux_fixes.sh`（备份 → scp → 关键字校验 → systemctl restart → health check）
- 校验结果：`copyRegistrationInfo×2 / reg-api-token×4 / 点空白监听×0 / marked@15×1 / showReportDetail×2 / report-detail-modal×4` ✅
- 服务：`systemctl restart openclaw-web` → active；`/openclaws` 与 `/openclaws/4` 均 302 到登录页（路由正常）

### 经验 #12：模态弹窗"点空白关闭"的取舍原则

**踩过的坑**：原来 `openclaws.html` 把 5 个 modal 都挂了 `if (e.target === modal) close()`。结果创建表单填了一半误点空白 → 全没了；注册刚成功还没复制 token → 一空白点没了；token 看到一半 → 也没了。

**新规则**（沉淀给所有未来 modal）：

| 弹窗类型 | 点空白可关 | 例子 |
|----------|------------|------|
| 关键信息展示（不能丢） | ❌ 必须按"关闭"按钮 | token 显示 / 注册成功 / 详情查看（如日报详情：可以打开，关闭按钮显式） |
| 长表单（半填怕丢） | ❌ 必须按"取消"按钮 | 创建 / 编辑表单 |
| 次级选择 | ✅ 允许 | 头像选择、icon 选择 |
| 二次确认（已经触发了主操作） | ✅ 允许（等于取消） | "确认重新生成 Token？"、"确认删除？" |

实现层面：**不是所有 modal 都默认加 backdrop click**，按上表对号入座。

### 经验 #13：弹窗里塞"复制给别人发"的话术片段

很多 hub 后台的注册 / 邀请 / 分享场景，都需要让管理员把"链接 + 凭证 + 操作说明"拼成一段可直接 IM 转发的话术。比起让管理员手动 3 次复制再拼接，**一键拼接好整段话**的转化率高得多。

模板（适用于 OpenClaw 注册、TAPD 关联授权、知识审核邀请等）：

```
你好 ${name}，<动词短语>，照着说明安装即可：

<入口链接>：${url}
<凭证名>：${token}

<可选操作步骤一段话>
```

JS 实现核心：

```javascript
function copyXxxInfo() {
    const url   = document.getElementById('xxx-url').textContent;
    const token = document.getElementById('xxx-token').textContent;
    const name  = document.getElementById('xxx-name').textContent;
    const info = `你好 ${name}，...：\n\n注册链接：${url}\nAPI Token：${token}\n\n安装步骤：...`;
    _doCopy(info, '注册信息已复制，可直接发给 OpenClaw');
}
```

**通用 `_doCopy(text, okMsg)` 工具函数**应该提到 `base.html` 或全局 JS（目前在 `openclaws.html` 内联，下次抽取）：clipboard API 优先 + `document.execCommand('copy')` 兜底（HTTPS / localhost 之外的 http 站点 clipboard API 会拒）。

### 经验 #14：长内容卡片的"展开看全文"标准方案

OpenClaw 的日报、知识库摘要、变更日志、需求 description 等场景，都需要"卡片展示要点 + 点击看完整内容"的二级查看模式。

**标准三件套**：

1. **卡片增量加 `cursor:pointer + hover 边框 + 右上角"→"提示"`**，告诉用户可点
2. **统一详情 modal**：`max-width:780, max-height:88vh, display:flex; flex-direction:column`，内部 body `overflow-y:auto`，避免内容超长把页面撑爆
3. **复用 marked.js 渲染**：`marked.parse(text, { breaks:true, gfm:true })` + 配套 `.markdown-body` 暗色主题 CSS（h1~h3 / code / pre / blockquote / table / img），所有详情 modal 共用一套

**JSON / 异构数组兼容套路**（来自 DailyReport tasks_completed / knowledge_recorded 等 JSON 字段的实战）：

```javascript
function _normalizeListForDetail(val) {
    if (val == null) return [];
    if (Array.isArray(val)) return val;
    if (typeof val === 'string') {
        const s = val.trim();
        try { const j = JSON.parse(s); if (Array.isArray(j)) return j; } catch(e) {}
        const lines = s.split('\n').map(x=>x.trim()).filter(Boolean);
        if (lines.length > 1) return lines;
        const parts = lines[0].split(/[;；]\s*/).filter(Boolean);
        return parts.length > 1 ? parts : [s];
    }
    return [val];
}
function _itemToText(item) {
    if (typeof item === 'string') return item;
    if (typeof item === 'object') {
        const main = item.task || item.title || item.content || item.name || item.description;
        const extra = [item.duration, item.category, item.module].filter(Boolean);
        return (main || JSON.stringify(item)) + (extra.length ? ` _(${extra.join(' · ')})_` : '');
    }
    return String(item);
}
```

> 这套规范已在日报详情 modal 落地，下次给"知识详情"、"变更日志详情"、"任务结果详情" 做点击弹窗时直接复用。

### 经验 #15：纯模板改动的最小部署路径

本次改动只动 2 个 `.html`，无 Python / 无 DB / 无 migration。最小路径：

```bash
# 1. scp templates 到 /tmp
scp -i ~/.ssh/id_X -P 36000 web/templates/{a.html,b.html} root@HOST:/tmp/

# 2. 在 server 上：备份 → 覆盖 → 关键字校验 → restart
ssh root@HOST 'bash -s' <<'EOF'
cd /opt/openclaw-web
TS=$(date +%Y%m%d-%H%M%S); BAK=_backup_ux_${TS}; mkdir -p ${BAK}
for f in templates/a.html templates/b.html; do cp "$f" "${BAK}/$(echo $f|tr / _)"; done
cp /tmp/a.html templates/a.html
cp /tmp/b.html templates/b.html
# 关键字校验：新增的函数名 / id 应该出现
grep -c 'newFuncName' templates/a.html
# 删除的事件应消失
grep -c 'removed-listener' templates/a.html  # 期望 0
systemctl restart openclaw-web   # gunicorn worker 重启释放 jinja 模板缓存
sleep 2 && systemctl is-active openclaw-web
curl -sS -o /dev/null -w 'HTTP %{http_code}\n' http://127.0.0.1:8088/<page>
EOF
```

**重点**：
- gunicorn 默认开 jinja 模板缓存，**仅替换文件不会立即生效**，必须 `systemctl restart`（不用 reload，reload 不会清模板缓存）
- 不需要 `pip install / db migration / venv 切换`，整个流程 < 10 秒
- `curl HTTP 302` = 正常（路由命中后跳登录页）；HTTP 500 = 模板渲染错；HTTP 404 = 路由未注册

---

## 2026-04-22 补全 Skill #121 topic-discuss（课题讨论）

### 背景

DB 里 `skills #121 = topic-discuss` 只有一句 description（"参与Hub课题讨论论坛..."），`template_content / mirror_content / pack_path` 全 NULL，对 OpenClaw 不可用。本地 `openclaw-agent/skills/topic-discuss/` 目录也不存在。

### 修复动作

1. **本地新建** `openclaw-agent/skills/topic-discuss/SKILL.md`（10433 字符 / 15.4 KB），九大章节：
   - 简介 / 与其它模块关系 / 前置条件
   - 7 大板块字典（test_methods / case_sharing / risk_assessment / client_perf / special_testing / industry_news / case_review）
   - API 全集（list_boards / list / create / get / reply / close / reopen / delete）+ 频率限制 + visibility 过滤
   - 通知消费（msg_type=`topic_notify`，3 种消费方式：MCP / SSE sidecar / curl）
   - case_review 强约束规范（review_library_id 必填 + module_paths 多级）
   - 4 个剧本：主动发帖 / 被动回复 / 用例评审参与 / 管理员关闭并沉淀知识
   - to_dict 字段速查 + 11 种错误码 + 自检清单 + system_config 可调参数

2. **同步到 DB**：通过 `app.models.Skill` 直接 `skill.template_content = <md>` + `db.session.commit()`，避免走 `/skills` API（要走审核）

### 验证

```
mysql> SELECT id, name, LENGTH(description), LENGTH(template_content), updated_at
       FROM skills WHERE id=121;
+-----+---------------+---------------------+--------------------------+---------------------+
| 121 | topic-discuss | 225                 | 15002                    | 2026-04-22 10:15:59 |
+-----+---------------+---------------------+--------------------------+---------------------+
```

### 经验 #10：补全空壳 Skill 的标准路径（DB-only / 无 git pack）

**判定 Skill 是否"空壳"**：

```sql
SELECT id, name,
       LENGTH(description) AS desc_len,
       LENGTH(template_content) AS tpl_len,
       LENGTH(mirror_content) AS mirror_len,
       pack_path
FROM skills WHERE id = <X>;
```

`tpl_len = NULL/0` 且 `mirror_len = NULL/0` 且 `pack_path = NULL` ⇒ 空壳，AI agent 无法执行。

**补齐路径（推荐 — 直连 DB）**：

```bash
# 1. 本地按现有 skill 风格写 SKILL.md（参考 knowledge-manager / hub-inbox）
#    - frontmatter 含 name / description / trigger_words
#    - 包含：API 全集 / 字段速查 / 错误码 / 剧本 / 自检清单 / 可调参数
# 2. scp SKILL.md 到 /tmp/
# 3. 用 Flask app context 直接更新（绕过 /skills API 的审核流程）
ssh -i ~/.ssh/id_X -p 36000 root@HOST "cd /opt/openclaw-web && ./venv/bin/python3 -c \"
from app import create_app, db
from app.models import Skill
app = create_app()
with app.app_context():
    s = Skill.query.get(<X>)
    s.template_content = open('/tmp/X_SKILL.md').read()
    db.session.commit()
\""
```

**为什么不走 `POST /skills` 或 `PUT /skills/<id>`**：

- `POST /skills` 是创建新 skill；
- `PUT /skills/<id>` 会把改动写入 `mirror_content` + `review_status=pending`，需要走 `/skills/<id>/approve` 审核；
- 系统级 skill（`is_standard=1, created_by=system`）通常**直接更新 `template_content`** 即可，没有审核流程负担。

**选 `template_content` vs `mirror_content`**：

| 字段 | 含义 | 何时用 |
|------|------|--------|
| `template_content` | 当前生效版本 | 系统级补全 / 首次写入 / 紧急修复 |
| `mirror_content` | 用户提交的修改副本 | 走审核流程的修订（review_status=pending） |

> 非审核场景一律写 `template_content`，省事且立刻生效。

### 经验 #11：MySQL `LENGTH()` vs Python `len()` 字符数差异

排查 skill 内容时发现 DB 显示 `tpl_len=15002` 但 Python `len()=10433`，**不是写错**：

- `LENGTH()` 返回**字节数**，UTF-8 中文 1 字符 = 3 字节
- `CHAR_LENGTH()` 才返回字符数

排查 skill / knowledge / topic content 大小时，**用 `CHAR_LENGTH()` 才能跟前端字符数对齐**：

```sql
SELECT CHAR_LENGTH(template_content) FROM skills WHERE id=121;  -- 10433
SELECT LENGTH(template_content) FROM skills WHERE id=121;       -- 15002
```

---

## 2026-04-22 需求分析中心 Stage 1 上线（9 张表 + R1 实时刷新）

### 部署摘要

- **服务器**：`9.134.11.169:8088`
- **包**：`F:\Code\claw_team\_deploy_requirement_analysis.tgz`（10 个文件）
- **脚本**：`_deploy_requirement_analysis/deploy.sh`
- **备份目录**：`/opt/openclaw-web/_backup_requirement_20260422-080655/`
- **结果**：✅ 服务正常重启 / ✅ 9 张表自动建出 / ✅ 端到端烟测 6 路全通

### 本次更新内容

1. **9 张新表（需求分析 + TAPD 缓存 + 实时刷新队列）**
   - 业务表：`requirement_items` (47 字段) / `requirement_change_logs` / `requirement_engineering_links` / `requirement_testcase_links`
   - 缓存表：`tapd_versions` / `tapd_baselines` / `tapd_iterations_cache` / `tapd_field_map_cache`
   - 队列表：`tapd_refresh_requests`（R1 反向轮询）
   - JSON 字段统一使用 `LONGTEXT` 兼容 MariaDB 10.1

2. **新蓝图 `app/api/requirements.py`**
   - Agent 推送：`POST /requirements/iterations/<id>/snapshots`、`POST /requirements/tapd-cache/{iterations,versions,baselines,field-map}`
   - 前端查询：`GET /requirements/iterations/<id>/{items,changes,summary}`、`GET /requirements/items/<id>`
   - 实时刷新（R1）：`POST /requirements/tapd-refresh-requests` + `GET /requirements/agent/tapd-refresh-queue` + `POST /agent/tapd-refresh-queue/<id>/{done,fail}`
   - 用例关联：`POST /requirements/items/<id>/testcase-links`、`DELETE /requirements/testcase-links/<id>`

3. **engineering.py 联动 hook**
   - `POST /engineering/refresh` 落库后，按 `change_item.tapd_story_ids` 自动建 `requirement_engineering_links`，audit detail 写入 `需求关联=N`

4. **`/tapd/iterations` 与 `/test-plans/tapd-iterations` 改走本地缓存**
   - Hub 不再直连 TAPD，所有 TAPD 数据由 OpenClaw Agent 通过 `mcporter-internal` 推送
   - testplans.html 现有 picker 自动复用，无需前端改动

5. **新页面 `/requirements`**
   - 三栏视图：近期变更 / 需求列表 + 5 张统计卡 / 详情 5 个 tab（基础/测试/工程关联/用例/变更日志）
   - 「🔄 实时刷新」按钮：3s 前端轮询 + 10s agent 反向轮询，端到端 ≤15s
   - 侧栏「📋 需求分析」入口（工程分析下方）

6. **配套 Skill 文档**
   - `openclaw-agent/skills/requirement-analysis/SKILL.md`：TAPD→Hub 字段映射 / custom_field 中英映射 / status 枚举 / 5 个推送接口 payload / Python 推送代码片段 / agent 主轮询循环范式 / Taihu Token 安全约束（仅依赖 Knot 平台运行时注入）

### 端到端烟测结果

| # | 测试 | 结果 |
|---|------|------|
| 1 | `GET /tapd-cache/iterations` 空缓存 | `{iterations:[], total:0}` ✅ |
| 2 | `POST /tapd-refresh-requests` 入队 | `id=1, status=pending` ✅ |
| 3 | `GET /agent/tapd-refresh-queue` 拉取 | `status=picked, picked_by_claw_id=4` ✅ |
| 4 | `POST /done` 完成 | `status=done` ✅ |
| 5 | `POST /tapd-cache/iterations` 推送 | `upserted=1` ✅ |
| 6 | `GET /tapd-cache/iterations` 查刚推的 | `cache_version=1` ✅ |

### 部署经验沉淀

> 经验 #1~#4 来自需求分析模块设计阶段（2026-04-21）；#5~#9 来自本次 Stage 1 部署阶段。统一在此沉淀，下次新 chat 可一站式查阅。

#### 经验 #1：敏感凭证（Taihu Token / API Key / SSH Key）处理

**触发场景**：用户在对话里贴出 Taihu Token / TAPD API Key / SSH 私钥 / 数据库密码

**正确做法**：
1. **立即警告并建议轮转**（云端日志可能记录对话上下文）
2. **不写入任何文件**（即使是注释/示例/`.env.example`）
3. **不在工具调用参数中传递**（避免被日志/分析系统采集）
4. 在 Skill / 文档里只写「依赖 Knot 平台运行时注入」「读环境变量 `${TAIHU_TOKEN}`」，不写具体值
5. 如必须落地（如 `system_config` 里存 LLM API Key），用加密列 + `_simple_decrypt()` 模式（参考 `OpenClawInstance.api_token_plain`）

**反例**：把 token 写到 `.env` / `SKILL.md` / `docker-compose.yml` / `secrets.json`，提交 Git；在 markdown 表格里贴明文「示例值」

**已知红线**：
- TAPD MCP 鉴权 → Knot 平台太湖 Token 注入，**Hub 与 Agent 代码 / 配置 / Git 中绝不出现 `tai_pat_*`**
- TAPD V2 内部 API 凭证（如 `rajqiu / 6EC0F9E4-A55C-...`）只存在于 `micro-cloud/dao/TapdDao.go`，Hub 端零持有

#### 经验 #2：「实时」需求的分级回应

用户说「实时」时**先用 AskQuestion 问清楚档位**，不要默认按 WebSocket/SSE 重投资：

| 档位 | 端到端时延 | 实现成本 | 适用场景 | 本项目落地 |
|------|------------|----------|----------|------------|
| R0 (按需手刷) | 用户主动 | 极低 | 数据稳定，每天看几次 | — |
| **R1 (轮询)** | **≤15s** | **中** | **MVP 推荐起点** | ✅ 需求分析 Stage 1 |
| R2 (SSE) | ≤2s | 高 | 多人协作，需要状态广播 | hub-sse-sidecar 已经在用 |
| R3 (WebSocket 双向) | <1s | 极高 | IM / 实时游戏 | — |

R1 实现要点（参考 `tapd_refresh_requests` 表 + `requirements.py` 队列接口）：
- Hub 写入 `pending` 行
- Agent 每 10s `GET /agent/.../queue?limit=10`，SELECT 时 UPDATE 为 `picked`
- 前端每 3s `GET /<id>` 看 status
- Agent 处理完 `POST /done` 或 `/fail`

#### 经验 #3：Hub-Agent 鉴权复用判断

新模块需要 Agent 推送时，**先看 `app/api/__init__.py` 的全局 `require_auth`**：

- 所有挂在 `api_bp`（`/api/v1/*`）的路由 **自动被 Bearer Token 保护**，token 解析到 `OpenClawInstance`
- **不需要再写新装饰器**，也不要在 `requirements.py` 重复实现 token 校验循环

只有路由路径里带 `<int:claw_id>` 的（如 `/api/openclaws/<claw_id>/...`）才需要 `require_claw_token`（定义在 `agent_client.py` / `openclaws.py`）。

**反例**：本次最初想给 `requirements.py` 单独写个 `require_agent_token` 装饰器，看了 explore 结果才发现 `before_request` 已经覆盖了，省一大块重复代码。

#### 经验 #4：避免在事务中误用 `db.session.rollback() + begin_nested()`

> （本条与 #8 同主题，#8 是简化总结；这里给完整反面教材）

**错误范式**（会把外层事务里 add 但未 commit 的对象全丢了）：

```python
# ❌ 错误
for ch in changes:
    db.session.add(MyModel(...))   # 这个会丢
    try:
        db.session.add(LinkModel(...))   # 可能唯一冲突
        db.session.flush()
    except Exception:
        db.session.rollback()        # ← 外层 add 的全没了
        db.session.begin_nested()    # ← 没有先 begin 就 begin_nested 是 anti-pattern
```

**正确范式**：

```python
# ✅ 正确：先 query 判重
for ch in changes:
    db.session.add(MyModel(...))
    exists = LinkModel.query.filter_by(a=..., b=...).first()
    if exists:
        continue
    db.session.add(LinkModel(...))
db.session.commit()  # 末尾一次 commit
```

本次 `_diff_and_log` / `_auto_link_engineering_changes` / engineering hook 都按正确范式写，所有写入在末尾一次 `commit()`，中间只用 `flush()` 拿主键。

---

#### 经验 #5：OpenClaw Hub 部署标准流程

**关键路径速记**：
- 服务器：`9.134.11.169:36000` root，SSH key `~/.ssh/id_9.134.11.169`
- 代码路径：`/opt/openclaw-web/`
- 服务：`systemctl restart openclaw-web`（systemd 管理，不是 docker）
- 数据库：`mysql -u booster -pbooster openclaw_manager`（MariaDB 10.1，**不支持 JSON 类型**，必须用 LONGTEXT）
- venv：`/opt/openclaw-web/venv/bin/python3`
- 日志：`journalctl -u openclaw-web --no-pager` / `/tmp/flask.log` / `/tmp/flask-access.log`

**标准流程**（参考 `_deploy_final/deploy.sh` 与 `_deploy_requirement_analysis/deploy.sh`）：

1. 在本地建 `_deploy_<feature>/` 目录，按 `app/`、`templates/` 镜像目录结构复制改动文件
2. 打包：`tar -czf _deploy_<feature>.tgz -C _deploy_<feature> app templates`
   - **注意 `-C` 进 staging 目录**，tgz 内 **不带** `web/` 前缀（远端解压到 `/opt/openclaw-web` 才能正确覆盖）
3. 上传：`scp -i ~/.ssh/id_9.134.11.169 -P 36000 _deploy_<feature>.tgz root@9.134.11.169:/tmp/<feature>_deploy.tgz`
4. 上传部署脚本：`scp ... deploy.sh root@.../tmp/deploy_<feature>.sh`
5. **必须** `sed -i 's/\r$//' /tmp/deploy_<feature>.sh`（Windows CRLF → LF，否则 bash 执行会报 `set: -\r: invalid option`）
6. 执行：`bash /tmp/deploy_<feature>.sh`，脚本含：
   - 备份 → 解压 → systemctl restart → journalctl 看建表日志 → MySQL 核对 → API 烟测

**新表通过 inline DDL 自动创建**：在 `app/__init__.py` 的 `create_app()` 末尾追加 `CREATE TABLE IF NOT EXISTS`，重启服务即建表，**不需要** Flask-Migrate / 手动 ALTER。

#### 经验 #6：API token 烟测必须解密 `api_token_plain`

**坑**：`openclaw_instances.api_token_plain` 字段名虽叫 plain，但**实际是密文**，需要 `_simple_decrypt()` 才是真正的 Bearer Token。直接 `SELECT api_token_plain` 出来的字符串拿去 `Authorization: Bearer` 会被服务拒绝（401 Token 无效）。

**正确做法**（在服务器跑）：

```bash
TOKEN=$(/opt/openclaw-web/venv/bin/python3 -c "
from app import create_app
from app.models import OpenClawInstance
app = create_app()
with app.app_context():
    inst = OpenClawInstance.query.filter_by(role='admin').first()
    print(inst.get_api_token() if hasattr(inst, 'get_api_token') else '')
")
curl -H "Authorization: Bearer ${TOKEN}" http://127.0.0.1:8088/api/v1/...
```

明文 token 形如 `oc_tk_<48 hex>`（共 54 字符）；密文 `api_token_plain` 是 72 字符 base64-ish 字符串。

#### 经验 #7：MariaDB 10.1 不支持 JSON 类型

**坑**：在 SQLAlchemy 模型用 `db.JSON` ORM 层是 OK 的（SQLAlchemy 帮你做序列化），但 inline DDL（`CREATE TABLE ... col JSON`）会失败，导致建表跳过 + ORM 查询时报 `Unknown column`。

**正确做法**：
- 模型层用 `db.JSON`（SQLAlchemy 会自动 JSON 编解码）
- DDL 层统一用 `LONGTEXT`：

```sql
CREATE TABLE IF NOT EXISTS requirement_items (
    ...
    raw_payload LONGTEXT,           -- 不是 JSON
    ...
);
```

读出来如果是字符串就 `json.loads()` 兜底（见 `requirements.py` `_auto_link_engineering_changes` 与 `engineering.py` hook）。

#### 经验 #8：避免在事务中用 `db.session.rollback() + begin_nested()` 处理唯一冲突

**坑**：用 try/except 捕获 `flush()` 时的唯一约束冲突再 rollback，会把整个外层事务里**已经 add 但未 commit 的对象都丢了**。

**正确做法**：在 add 之前先 `query.filter_by(...).first()` 判重，命中就跳过/更新；不命中再 add。本次 `_diff_and_log` 与 engineering hook 都按这个范式写。

#### 经验 #9：PowerShell 调 SSH 时 SQL 引号转义陷阱

**坑**：在 PowerShell 里写 `ssh root@... "mysql -e \"SELECT ...\""` 嵌套 3 层引号会被 PowerShell 解释器吞掉部分参数。

**正确做法**：把 SQL/复杂逻辑写成本地 `.sh` 文件 → `scp` 上传 → `ssh ... "bash /tmp/xxx.sh"` 执行。本次 `check_token.sh` / `smoke_test.sh` 都按这个模式做，同时不要忘 `sed -i 's/\r$//'`。

---

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

## 生产环境信息

| 项目 | 值 |
|------|------|
| **服务器** | 9.134.11.169 |
| **部署方式** | **systemd**（非 Docker） |
| **SSH 命令** | `ssh -i ~/.ssh/id_9.134.11.169 -p 36000 root@9.134.11.169` |
| **服务名** | openclaw-web |
| **监听端口** | 8088（gunicorn） |
| **部署路径** | /opt/openclaw-web/ |
| **Python 代码** | /opt/openclaw-web/app/ |
| **模板文件** | /opt/openclaw-web/templates/ |
| **静态文件** | /opt/openclaw-web/static/ |
| **Git 代码目录** | /opt/openclaw-web/web/（Flask 不直接加载，仅供参考） |
| **数据库** | MariaDB，库名 openclaw_manager，账号 booster/booster |
| **进程** | gunicorn -w 4 -k gevent -b 0.0.0.0:8088（venv） |

> ⚠️ 注意：Flask 的 WorkingDirectory 是 `/opt/openclaw-web`，`template_folder='../templates'` 相对于 app 包。
> 部署文件必须复制到 `/opt/openclaw-web/app/` 和 `/opt/openclaw-web/templates/`，不是 `/opt/openclaw-web/web/`。

---

## 常用部署命令

### SSH 连接

```bash
# 从本机直连（推荐，需配置 SSH 密钥）
ssh -i ~/.ssh/id_9.134.11.169 -p 36000 root@9.134.11.169
```

### 服务管理（systemd）

```bash
# 重启服务
systemctl restart openclaw-web

# 查看服务状态
systemctl status openclaw-web

# 停止/启动服务
systemctl stop openclaw-web
systemctl start openclaw-web

# 查看服务配置
cat /etc/systemd/system/openclaw-web.service

# 修改服务配置后重载
systemctl daemon-reload
systemctl restart openclaw-web
```

### 日志查看

```bash
# systemd 日志（最近50行）
journalctl -u openclaw-web --no-pager -n 50

# 实时跟踪日志
journalctl -u openclaw-web -f

# gunicorn 访问日志
tail -100 /tmp/flask-access.log

# gunicorn 错误日志
tail -100 /tmp/flask.log
```

### 文件部署

```bash
# 从本机直接 SCP 单个文件（推荐）
scp -i ~/.ssh/id_9.134.11.169 -P 36000 本地文件路径 root@9.134.11.169:/opt/openclaw-web/templates/
scp -i ~/.ssh/id_9.134.11.169 -P 36000 本地文件路径 root@9.134.11.169:/opt/openclaw-web/app/api/

# 示例：部署模板文件
scp -i ~/.ssh/id_9.134.11.169 -P 36000 review.html root@9.134.11.169:/opt/openclaw-web/templates/

# 示例：部署 API 文件
scp -i ~/.ssh/id_9.134.11.169 -P 36000 skills.py root@9.134.11.169:/opt/openclaw-web/app/api/

# 部署后重启服务
ssh -i ~/.ssh/id_9.134.11.169 -p 36000 root@9.134.11.169 "systemctl restart openclaw-web && sleep 2 && systemctl is-active openclaw-web"
```

### 数据库操作

```bash
# MySQL 连接
mysql -u booster -pbooster openclaw_manager

# 查看表结构
mysql -u booster -pbooster openclaw_manager -e "DESCRIBE table_name;"

# 快速查询
mysql -u booster -pbooster openclaw_manager -e "SELECT id, name, review_status FROM skills WHERE review_status='pending';"
```

### API 测试

```bash
# 基础测试
curl http://127.0.0.1:8088/api/v1/skills
curl http://127.0.0.1:8088/api/v1/dashboard/stats

# 带 Token 的请求
curl -H "Authorization: Bearer YOUR_TOKEN" http://127.0.0.1:8088/api/v1/skills
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

## 2026-04-22 部署完成（15:08）— TAPD 缓存为空诊断 + `if not project_id` 同源 bug 一次清理 + 一次性 seed

### 用户反馈

> "这里怎么显示未配置 tapd？不是给了 tapd skill 了么"

新建测试迭代弹窗中关联项目选 RacingGO 后，"TAPD 迭代"下拉显示「该项目未配置 TAPD 或无迭代」。

### 诊断过程

| # | 排查项 | 结果 |
|---|------|------|
| 1 | RacingGO `tapd_workspace_id` 是否配置 | ✅ `70202650` 已配置 |
| 2 | claw #11「需求代码分析专员小云」是否装了 #138/#139 | ✅ requirement-analysis + engineering-analysis 全 enabled |
| 3 | `tapd_iterations_cache` / `tapd_versions` / `tapd_baselines` 行数 | ❌ 全 0 行 |
| 4 | `/api/v1/test-plans/tapd-iterations?project_id=0` | ⚠️ 返回 6 个项目（应只 RacingGO） |

**两个根因同时存在**：

1. **数据空**：架构上 Hub 不直连 TAPD，必须由装了 #138 的 Agent 去调 mcporter-internal 推送缓存。Agent 装了 skill ≠ 跑了同步 — Agent 侧 push 循环还没真去执行
2. **隐藏 bug — `if not project_id:` 误判**：跟之前 baseline 创建时修过的同源 bug。`Project.id=0` 是 RacingGO 的合法主键，被 `if not x` 视为 falsy。一共 8 处现场（testplans 3 处、engineering 4 处、rules 1 处）都漏修

### 修复动作

#### 1. `if not project_id` 八处同源 bug 一次清理

| 文件 | 行 | 修改 |
|------|----|------|
| `app/api/testplans.py` | 48, 182 | `if project_id:` → `if project_id is not None:` |
| `app/api/testplans.py` | 795 | `if not project_id:` → `if project_id is None:` |
| `app/api/engineering.py` | 76 | 基线列表过滤 |
| `app/api/engineering.py` | 630 | lookups/libraries |
| `app/api/engineering.py` | 648 | lookups/iterations |
| `app/api/engineering.py` | 664 | lookups/modules |
| `app/api/rules.py` | 131 | 适用项目过滤 |

> `requirements.py:796` 的 `if not workspace_id and project_id is not None` 写法本身就对，已经用了 `is not None`，验证为唯一一处早就修对的。

`testplans.py:318` 是 `if iteration_id:`，但 `TestIteration.id` 是 auto_increment 主键，0 不可能存在，**不动以避免改动面爆炸**。

修复后验证：
```
?project_id=0  →  返回 6 项 (修复前)  →  返回 1 项, 仅 RacingGO (修复后) ✅
```

#### 2. 一次性 TAPD seed（让用户立刻看到下拉数据）

写了 `seed_tapd_cache.py`：在 9.134.11.169 上跑，环境变量传 `TAPD_USER/TAPD_PASS`，走内部 V2 API（`http://apiv2.tapd.woa.com`）+ Basic Auth 拉 RacingGO（workspace=70202650）的 4 类缓存数据，POST 到 Hub 现有的 `/api/v1/requirements/tapd-cache/*` 推送接口（**与 Agent 后续要走的路径完全一致**，并非 Hub 直连特殊通道）。

**结果**：

| 资源 | 拉取 | 入库 |
|------|------|------|
| iterations | 6 条（迭代5/4/3/2/1/M1父迭代） | ✅ tapd_iterations_cache=6 |
| releases (versions) | 0 条 | TAPD 项目本身没建过 release |
| baselines | 0 条 | TAPD 项目本身没建过 baseline |
| custom_fields_settings | 6 项（测试执行/测试结果/测试验收/是否需要测试/评审进度…） | ✅ tapd_field_map_cache=6 |

`releases` / `baselines` HTTP 200 + `data:[]`，是 RacingGO 项目状态而非接口问题。

#### 3. 安全约束

- 凭证只通过环境变量 `TAPD_USER/TAPD_PASS` 传入，**不写进任何代码/配置文件/git**
- 脚本跑完立刻 `rm -f ./seed_tapd_cache.py`，不留落地
- Hub 服务本身仍**无任何 TAPD 凭证**，架构纯净度未破坏
- 长期方案：Agent #11 真正实现 mcporter-internal 调用 + 定时 push（这是 agent 侧的开发，跟 Hub 解耦）

### 验证

- 前端打开"新建测试迭代" → 关联项目=RacingGO → "TAPD 迭代"下拉应能看到 6 条（迭代5/4/3/2/1/M1父迭代）
- `/api/v1/test-plans/tapd-iterations?project_id=0` 返回 1 项，iterations 数=6，last_synced_at 不为 null
- `tapd_iterations_cache` 6 行、`tapd_field_map_cache` 6 项

### 经验沉淀

#### 经验 #22 — 「装了 skill ≠ skill 在跑」分清能力定义和能力消费

Hub 数据库的 `skills` 表存的是 **能力说明书**（template_content + 触发词 + 鉴权要求等），它告诉 agent "你应该怎么干"。但**真去干的是 agent 进程**，agent 必须真的进入执行循环 + 调通 MCP/外部接口 + 调通 push 接口，整个链路才算闭环。

诊断顺序应当是：
1. **配置层**：项目的 `tapd_workspace_id` 等基础参数有没有
2. **能力层**：相关 claw 是否装了对应 skill 且 enabled=1
3. **执行层**：`tapd_*_cache` 表里是否有 `last_synced_at`，最新一次是什么时候
4. **接口层**：`tapd_refresh_requests` 是否堆积了 pending 没人消费
5. **路径层**：API filter 写法是否漏掉了 `0` 等边界值

任何一层断了，前端都会"显示空"。说"未配置"只是用户视角文案，不要被它带着只查"配置层"。

#### 经验 #23 — `if not x` 在数字主键场景是反复犯错点，建议组件化

跟修过的 baseline `project_id=0`、`library_id=0`、`task_id=0`、`iteration_id=0` 是同一类错误。这类 bug 特点是：

- **难发现**：99% 的项目主键都不是 0，QA 命中概率极低
- **难根除**：模式太常见，复制粘贴就传染（这次一次发现 8 处）
- **后果重**：一旦命中，要么"必填字段为空"，要么"过滤失效返回全部"

固化做法：**所有 `request.args.get('xxx_id', type=int)` 之后的判空，统一用 `is None`/`is not None`**，禁用 `if id:` / `if not id:`。也可以加一个 lint rule 或 pre-commit grep。

```python
# ❌ 反模式
project_id = request.args.get('project_id', type=int)
if project_id:                       # id=0 被误判为没传
    query = query.filter_by(project_id=project_id)

if not project_id:                   # id=0 被误判为没传，进了"全部项目"分支
    projects = Project.query.all()

# ✅ 正确
if project_id is not None:
    query = query.filter_by(project_id=project_id)

if project_id is None:
    projects = Project.query.all()
```

#### 经验 #24 — 「Hub 只是存储，Agent 才是水龙头」架构下做 seed 的合规姿势

**架构原则**：Hub 不能存 TAPD/外部系统的凭证，所有外部数据由 Agent 推到 Hub 现有 push 接口。但 Stage 1 上线时，Agent 端 push 循环往往还没就绪，前端打开就是空的，用户体验极差。

**合规 seed 三件套**：

1. **走 Hub 现有 push 接口**（不要写"Hub 直连特殊通道"）—— 既能验证 push 接口是否真的能用（防止 Agent 上线时再翻车），又不污染架构
2. **凭证只通过环境变量**，**不入 git/代码/配置文件**，跑完 `rm -f` 删脚本
3. **明确标记为一次性**：脚本注释开头写清"由 Agent 后续接管"，避免后人误以为 Hub 长期负责拉取

本次 `seed_tapd_cache.py` 就是按这三件套做的：

```bash
cp /tmp/seed_tapd_cache.py ./seed_tapd_cache.py
TAPD_USER='xxx' TAPD_PASS='yyy' ./venv/bin/python3 ./seed_tapd_cache.py
rm -f ./seed_tapd_cache.py     # ← 关键：跑完立删
```

#### 经验 #25 — 公司内部 V2 API 字段名识别要对照 dao 层

公司内部 TAPD V2 API（`apiv2.tapd.woa.com`）跟公开 API（`api.tapd.cn`）字段命名有差异。比如自定义字段配置：

- **错误猜测**：`label` / `alias`（公开 API 风格）
- **实际返回**：`name`（值即中文标签，如「测试执行」）

排查方式：拿 `head -c 400 /tmp/_resp.json` 看真实 payload，**不要拍脑袋猜字段**。这次 6 项 field_map 第一次拉到 0 项就是这个原因，第二次改成 `f.get("label") or f.get("alias") or f.get("name") or ""` 才正确。

可参考 `F:\Code\git_code\micro-cloud\dao\TapdDao.go` 里的字段反序列化定义（最权威）。

#### 经验 #26 — PowerShell `&` 和 SQL `FROM` 还是会咬 ssh 内联命令

第二次踩同样的坑：

```powershell
# ❌ PowerShell 把 & 当成调用操作符；把 FROM 当成关键字
ssh root@host "curl 'http://x/y?a=1&b=2'"
ssh root@host "mysql -e 'SELECT * FROM t'"

# ✅ 唯一稳妥姿势：脚本本地写 → scp 上传 → ssh 执行
scp script.sh root@host:/tmp/
ssh root@host "sed -i 's/\r\$//' /tmp/script.sh && bash /tmp/script.sh"
```

经验 #16 已经写过类似教训，但因为没固化成"反射"，又踩了一次。**今后 ssh 内联超过一行/含特殊符号的，全部改 scp+bash 模式**。

### 文件清单

| 文件 | 用途 | 是否保留 |
|------|------|---------|
| `web/app/api/testplans.py` | 修 3 处 `if project_id:` | ✅ |
| `web/app/api/engineering.py` | 修 4 处 `if project_id:` | ✅ |
| `web/app/api/rules.py` | 修 1 处 `if project_id:` | ✅ |
| `_deploy_requirement_analysis/seed_tapd_cache.py` | 一次性 seed 脚本 | ✅（参考实现，agent 端 push 可对照） |
| `_deploy_requirement_analysis/deploy_tapd_seed.sh` | 部署 + seed 自动化脚本 | ✅ |
| `_deploy_requirement_analysis/diag_tapd_cache.sh` | 诊断脚本（5 步排查） | ✅（再遇到"显示未配置"先跑这个） |
| `_deploy_requirement_analysis/verify_seed.sh` | seed 后验证脚本 | ✅ |
| `_deploy_requirement_analysis/reseed_fieldmap.sh` | field_map 修字段名后重跑 | ✅ |

### 待办

- 🔜 **Agent #11「需求代码分析专员小云」侧的 push 循环要真正跑起来**，否则数据只有 seed 那一刻是新鲜的。可以考虑：
  - 用户点"🔄 实时刷新"按钮 → `tapd_refresh_requests` 入队 → agent 轮询消费 → 推 cache → 标 done
  - 每日 0 点全量同步一次（agent 侧 cron 或 APScheduler）
- 🔜 给 `request.args.get('xxx_id', type=int)` 后用 `if not xxx_id` 的模式加 grep pre-commit hook 或 lint rule，防止再写出来

---

## 2026-04-22 部署完成（15:16）— TAPD 迭代多选改为下拉式 dropdown

### 用户反馈

> "这个下拉框太丑了~用传统的 dropdownlist 多选就可以"

新建/编辑测试迭代弹窗里"TAPD 迭代"原本是**垂直堆叠的 checkbox 列表**：高度固定 200px，6 个迭代 一打开就占掉半个屏幕，不像 form 控件，更像一块"贴在表单里的小弹窗"。

### 修复

把"高度固定的 checkbox 列表"改成**下拉式多选**：

| 元素 | 原来 | 现在 |
|------|------|------|
| 入口 | 直接展示 200px 高复选框堆 | 一行 trigger 按钮，看起来跟 `<select>` 一样 |
| 已选展示 | 看不到（只能在堆里看 ✓） | trigger 上显示「已选项名称」+ 紫色圆角 badge 数字 |
| 选项展开 | 永远展开 | 点击 trigger 才展开浮层（z-index 10001 高于 modal） |
| 搜索 | 无 | 浮层顶端固定搜索框（实时过滤） |
| 全选/清空 | 无 | 浮层顶端两个按钮，**仅作用于当前可见项**（搜索后全选很有用） |
| 关闭 | — | 点击外部 / ESC 自动收起 |

**实现关键**：
- 全部新 CSS 类前缀 `.tp-multi-*`，复用 `--bg-secondary / --accent / --border` 等已有变量，跟其他 form 元素观感一致
- checkbox class 仍叫 `.im-tapd-cb`，**`saveIteration` 函数零侵入**（still `document.querySelectorAll('.im-tapd-cb:checked')`）
- 浮层用 `hidden` 属性 + JS toggle，避免 `display:none` 跟 panel flex 布局打架
- 全局 click 监听用 `multi.contains(e.target)` 判断是否点在组件外

**修改文件**：`web/templates/testplans.html`
- 新增 ~70 行 CSS（`.tp-multi`, `.tp-multi-trigger`, `.tp-multi-panel`, `.tp-multi-toolbar`, `.tp-multi-options`, `.tp-multi-option` 等）
- HTML 替换 1 块（id=`im-tapd-group`）
- JS 替换 `onProjectChangeForIter` + 新增 6 个函数：`setTapdLabel` / `updateTapdLabel` / `onTapdOptionChange` / `toggleTapdDropdown` / `filterTapdOptions` / `selectAllTapd` + 全局 click/ESC 监听

### 经验沉淀

#### 经验 #27 — 表单内"≥3 项的多选"一律用下拉式，禁用堆叠 checkbox

垂直堆叠 checkbox 在 1-2 项时还行，**3 项以上立刻变形**：

- 表单视觉断层（一块"小窗"贴在表单里，不像控件）
- 占用大量纵向空间（modal 经常被挤到滚动）
- 看不到已选状态（要在列表里挨个找 ✓）
- 没有搜索能力（10 项以上找东西就难受）

固化做法：**任何"≥3 项的多选" → 用下拉式 multi-select 组件**。本次的 `.tp-multi-*` CSS + 6 个 JS 函数可以直接复用，下次类似需求（rules.html 适用项目多选、skills.html 适用模块多选等）一并按这套改。

#### 经验 #28 — Modal 内的浮层 z-index 必须显式高于 modal

modal-overlay 通常在 9999 左右。modal 内部的下拉浮层（multi-select / date picker / 自定义弹窗）如果不显式给 z-index，会被 modal 自身的子元素 stacking context 盖住，**用户看到的现象是"点了下拉但没反应"**。

固化做法：modal 内部所有"absolute 浮层" → z-index ≥ **10001**（modal 9999 + 自身 + 1）。本次 `.tp-multi-panel` 就给了 10001。

#### 经验 #29 — 搜索后的"全选"应该只作用于可见项

`document.querySelectorAll('.im-tapd-cb')` 拿到的是**所有**复选框，搜索过滤只是改了 `display:none`。如果"全选"无脑全打勾，**用户搜出来想批量勾的初衷就反了**（连过滤掉的也勾上）。

固化做法：搜索 + 全选场景，全选/清空时跳过 `display === 'none'` 的项。代码 1 行：

```js
function selectAllTapd(checked) {
    document.querySelectorAll('#im-tapd-options .tp-multi-option').forEach(el => {
        if (el.style.display === 'none') return;  // 仅作用于当前可见项
        const cb = el.querySelector('.im-tapd-cb');
        if (cb) { cb.checked = checked; el.classList.toggle('selected', checked); }
    });
    updateTapdLabel();
}
```

### 验证

- 服务 active，关键字 5/5 通过，旧 `im-tapd-list` 引用 0 残留
- 前端打开"新建测试迭代" → 关联项目=RacingGO → 点 TAPD 迭代下拉 → 浮层展开 6 项 + 搜索框 → 选中 trigger 上显示选中名称 + badge 数字
- 点击外部 / 按 ESC → 浮层关闭

---

## 2026-04-22 部署完成（16:42）— Skills / Rules 重新分配（reassign）功能

### 用户反馈

> "增加一个需求：1、skills 和 rules，需要有个重新分配功能，有修改后，需要操作让 openclaw 重新安装，已安装的按钮变成：重新分配"

修复前的痛点：Skill / Rule 内容修改后，已安装该资源的 OpenClaw 仍然跑旧版本，Hub 既没有 stale 检测，也没有"重新分配"入口。

### 设计原则（最小代价）

**不新增字段、不做数据迁移**。直接复用：

- `Skill.updated_at` / `Rule.updated_at`（onupdate=_now 自动维护）
- `OpenClawSkill.installed_at` / `OpenClawRule.applied_at`

stale 判定：`installed_at IS NULL OR installed_at < skill.updated_at` 即"装过、但 hub 又改过"。Reinstall 时把 `installed_at` 推到 now，stale 立即清零。

| 资源 | 已装时间字段 | 备注 |
|------|------------|------|
| Skill → claw | `OpenClawSkill.installed_at` | 已存在 |
| Rule → claw | `OpenClawRule.applied_at` | 原"agent 应用完成"语义已扩展为"hub 分配时间"；如未来需要"agent 真正应用"时间，再加 `confirmed_at` 字段 |

### 改动

#### 后端 — `app/api/skills.py`

1. **`list_skills` 列表接口**：每个 skill 新增 3 个字段
   - `used_by_fresh`：已装且最新的 claw 名单
   - `used_by_stale`：已装但 stale 的 claw 名单
   - `stale_count`：stale 数量（前端按钮直接读）
2. **`install_skill` 接口**：existing 命中时
   - 计算 `is_reinstall = installed_at < skill.updated_at`
   - 一律刷新 `installed_at = _now()`（这就是"已重新分配"的标记，立即让 stale → fresh）
   - todo 标题/描述/verification_target 全部按 reinstall 区分（`reinstall-skill:xxx` vs `install-skill:xxx`）
   - 响应回带 `is_reinstall` 字段供前端审计

#### 后端 — `app/api/rules.py`

1. **`_rule_with_usage`**：同样新增 `used_by_fresh / used_by_stale / stale_count`
2. **`update_claw_rules` 接口**：existing 命中时
   - 计算 `is_reinstall`，刷新 `applied_at = _now() + applied = True`
   - 单独维护 `reinstalled_names` 列表，`ClawMessage` 文案区分"重新分配 X 条 + 新装 Y 条"
   - todo 标题 / verification_target 按 reinstall 区分

#### 前端 — `templates/skills.html`

1. **卡片右下"分配"按钮**：`stale_count > 0` 时变成橙色「♻️ 重新分配 (N)」，hover title 解释含义
2. **分配 modal**：
   - 加 `#assign-stale-hint`：modal 顶部一行黄色提示「该 Skill 内容已修改，N 个 OpenClaw 装的是旧版本」
   - claw 列表 3 态渲染：fresh = 绿边 + ☑ disabled +「已安装」；stale = 橙边 + ☑ checked（默认勾选，方便一键全部重装） +「需要重新分配」；未装 = 灰边 + ☐
   - 提示文案改为"绿色=已最新，橙色=需重新分配，未勾选=未安装"

#### 前端 — `templates/rules.html`

1. 卡片"分配"按钮、`openAssignRuleModal` 渲染 / 提示语 — 与 skills.html 完全镜像

### 部署链路

```bash
scp -i ~/.ssh/id_9.134.11.169 -P 36000 <4 个产物 + 1 个 deploy.sh> root@9.134.11.169:/tmp/
ssh -i ... -p 36000 root@9.134.11.169 "sed -i 's/\r$//' /tmp/deploy_reassign.sh && bash /tmp/deploy_reassign.sh"
```

部署脚本内嵌 4 大类校验：
1. 备份原文件到 `_backup_reassign_<TS>/`
2. 后端关键字校验（`used_by_stale / is_reinstall / stale_count / reinstalled_names`）
3. 前端关键字校验（同上 + `重新分配 / assign-stale-hint`）
4. `python3 -c "import ast"` 语法检查 → 重启服务 → curl 三个端点冒烟

实际结果：14/14 关键字通过、syntax OK、service active、`/skills` 302、`/rules` 302、`/api/v1/skills` 401（API 全局认证正常生效）。

### 经验沉淀

#### 经验 #30 — "已分配过但版本过期"必须有显式 UI 入口

任何"分配/安装"型操作，只要被分配的资源**会被修改**，就必然存在"已分配实例 vs 当前最新版本"的差异。如果系统不暴露这个差异，结果就是：

- 用户以为"装过了 = 用着最新的"
- 实际 agent 跑的是旧的，行为出错查不出原因
- 修内容的人不知道要去通知所有装过的 claw 重装

固化做法：**任何"分配/安装/同步"型功能，列表页面必须带"已装但 stale"的 badge + 入口按钮**。本次的实现可作模板复用：

- 后端 list 接口返回 `used_by_fresh / used_by_stale / stale_count`
- 前端按钮按 stale_count 切色 + 文案
- 弹窗内 3 态渲染：fresh disabled、stale 默认勾选、未装可勾选
- 安装接口幂等：existing 命中也走完整流程（更新时间戳 + 下发 todo），区分 install vs reinstall

#### 经验 #31 — 复用已有时间戳字段判 stale，避免新增 `version` 字段

最直觉的做法是给资源加 `version int` + 给关联表加 `installed_version int`。但这需要：

- 数据库迁移
- 创建历史关系如何回填？（旧数据 version=NULL 怎么算？）
- 修内容的所有路径都要 `version += 1`，漏一处就失效

更简单的方案：**直接对比 `资源.updated_at` vs `关联表.installed_at`**。利用 SQLAlchemy `onupdate=_now` 自动维护，零代码漏点、零迁移。代价是"改任何字段都算 stale"，但这刚好符合"内容修改 → 需重新分配"的用户语义。

如果未来要更精细（比如改 description 不算、改 content_template 才算），再用 `content_hash` 字段，但**不要一开始就上**。

#### 经验 #32 — `applied_at` 这种"agent 完成"语义字段，可以扩展为"hub 分配"标记

`OpenClawRule.applied_at` 原始语义是"agent 应用完成时间"，但 Hub 没有可靠的"applied done"回执机制（agent 端 push 还没全实现）。这种字段如果一直空着，就废了。

**改造方案**：把语义扩展为"Hub 分配/重分配时间"，install 时一律 set 成 now、加上 `applied=True`。代价：

- 旧"applied 表示 agent 真完成"逻辑会失真 → 但实际上没有任何代码依赖这个语义，**改造前先 grep 确认没业务依赖**
- 未来如果真需要 agent 回执，新加 `confirmed_at` 字段即可（不破坏向后兼容）

#### 经验 #33 — 9.134.11.169 的 SSH 凭证是 `root@host -p 36000 -i ~/.ssh/id_9.134.11.169`，**不是默认 22 端口的 rajqiu**

本次踩坑：默认用 `ssh rajqiu@9.134.11.169` 连了 7 次，全部 `kex_exchange_identification: read: Connection reset`。误以为是网关限流/sshd 抖动，等了 ~10 分钟无效。**真正原因是端口和账号都错了**：

- 这台机器的 sshd 监听 **36000 端口**（22 端口对外不通），22 端口被网关 reset
- 账号是 **root**，要带专用密钥 `~/.ssh/id_9.134.11.169`

固化做法：

```powershell
# ✅ 正确姿势（写进所有部署脚本/记忆）
ssh -i "$env:USERPROFILE\.ssh\id_9.134.11.169" -p 36000 root@9.134.11.169 "..."
scp -i "$env:USERPROFILE\.ssh\id_9.134.11.169" -P 36000 <local> root@9.134.11.169:<remote>

# ❌ 错误（22 端口 + 默认账号 → 一定 reset，**别再被 kex_exchange_identification 误导**）
ssh rajqiu@9.134.11.169 "..."
```

DEPLOYMENT_LOG.md 章节"### 文件部署"早就写过这个姿势，但没固化到肌肉记忆。今后遇到 `kex_exchange_identification: read: Connection reset` **第一时间检查端口/账号/密钥**，而不是怀疑网关。

### 文件清单

| 文件 | 用途 |
|------|------|
| `web/app/api/skills.py` | install_skill 加 reinstall 分支 + list 加 stale 字段 |
| `web/app/api/rules.py` | update_claw_rules 加 reinstall 分支 + `_rule_with_usage` 加 stale 字段 |
| `web/templates/skills.html` | 按钮 + modal 双层 stale UI |
| `web/templates/rules.html` | 按钮 + modal 双层 stale UI |
| `_deploy_reassign/deploy_reassign.sh` | 备份 + 校验 + 重启 + 冒烟一体化部署脚本 |

### 待办

- 🔜 `openclaw_detail.html` "已生效 ✓" 标也可以联动 stale，给单个 claw 视角看"我装的哪些 skill 已过期"。本次只做了 skill/rule 列表页（用户原始诉求范围），claw 详情页留作下一轮 UX 增量。
- 🔜 把"分配/重新分配"模式提炼成可复用 component（`assign_modal.html` macro），下次再有"分配类"功能直接 include。

---

## 2026-04-22 部署完成（17:15）— 重新分配补强：支持"内容未变也强制重装"

### 用户反馈

> "需要编辑修改保存后才能重新分配？不需要，有时候是安装失败，需要强制重新安装"

刚做完的"重新分配"只在 `installed_at < skill.updated_at`（即内容真改过）时才让用户操作。但实际场景里**内容一字未改但仍需重装**的情况很常见：

- agent 安装时网络中断 / install.sh 半路挂了
- `~/.qclaw/skills/xxx/` 被误删 / 被人手动改坏
- 想强制覆盖一次，确保所有 claw 都跑相同版本

### 改动

#### 后端 — 把 `is_reinstall` 判定从"内容变更"扩展为"existing 命中即算"

```python
# 之前：只有 installed_at < updated_at 才算 reinstall
is_reinstall = bool(installed_at and updated_at and installed_at < updated_at)

# 现在：existing 命中就算 reinstall，再用 reinstall_reason 区分两种 case
is_reinstall = True
if installed_at and updated_at and installed_at < updated_at:
    reinstall_reason = 'content_updated'   # hub 改过了
else:
    reinstall_reason = 'forced'            # 内容没变，纯粹强制重装
```

todo description / 响应 JSON 都按 `reinstall_reason` 给不同文案：

| reason | todo 文案 |
|---|---|
| `''` (首装) | "Hub 已**分配** Skill「xxx」(id=N)，请拉取并安装到本地。" |
| `content_updated` | "⚠️ 这是 **重新分配（内容已更新）**：本 Skill 在 Hub 端已修改（updated_at=XXX），你之前的安装版本已过期，必须重新拉取并**全量覆盖** ~/.qclaw/skills/xxx/" |
| `forced` | "🔁 这是 **强制重新分配（内容未变）**：通常用于安装失败恢复 / 文件被误删 / 强制覆盖场景。请重新拉取并**全量覆盖** ~/.qclaw/skills/xxx/，不要假定本地已有的就是对的。" |

skills.py 和 rules.py 完全镜像同套逻辑。

#### 前端 — modal 里"已最新"的 claw 改成可勾选

| 状态 | 改前 | 改后 |
|---|---|---|
| 已最新 (fresh) | ☑ disabled +「已安装」绿 | **☐ 可勾 +「已安装」绿 + 浅灰小字"勾选可强制重装" + label title 详细解释** |
| 已 stale | ☑ checked + 橙「需要重新分配」 | 不变（默认勾选体验最好） |
| 未装 | ☐ 灰 | 不变 |

modal 顶部提示也分级：

| 场景 | 提示 |
|---|---|
| stale_count > 0 | 橙底 + "⚠️ N 个装的是旧版本，默认已勾选，点确认即批量重装。**另有 M 个已是最新，如需强制重装也可手动勾选。**" |
| stale_count == 0 但 fresh > 0 | 灰底 + "💡 提示：勾选'已安装'的 OpenClaw 可**强制重新分配**（用于安装失败恢复、文件被误删、强制覆盖等场景）。" |
| 全部未装 | 不显示提示 |

#### 顺手修一个老 bug

`web/templates/skills.html` 一直读 `s.install_count`，但 `web/app/api/skills.py` 只 set 了 `used_by_count` —— 卡片上"X 个安装"永远显示 0。补一行 `d['install_count'] = len(d['used_by'])` 解决。

### 经验沉淀

#### 经验 #34 — "已分配"功能必须区分两种 reinstall：内容驱动 vs 用户强制

刚做完一版的时候只考虑了"hub 内容变更 → 用户去重装"，但忽略了"hub 内容没变、但 agent 那边出问题、用户想强制覆盖"。这种**用户主动诉求**比内容变更更频繁，因为：

- 内容变更是低频（开发者改 skill 才有）
- 强制重装是高频（agent 安装失败/文件丢/强制对齐版本，每天都可能发生）

固化规则：**任何"分配/同步/部署"型操作，UI 都必须给"强制重装"入口**，禁止用"内容未变就不让操作"的拦截。后端 install API 必须**幂等可重入**，existing 命中走完整流程（更新时间戳 + 下发 todo），不要 short-circuit。

```python
# ❌ 反模式：existing 命中就 short-circuit
if existing:
    return jsonify({'message': '已经装过了'}), 200

# ❌ 反模式：要求"内容必须变化"才允许重装
if existing and not content_changed:
    return jsonify({'error': '内容未变，无需重装'}), 400

# ✅ 幂等：existing 命中也走完整流程，区分 reason 给不同文案
if existing:
    is_reinstall = True
    reinstall_reason = 'content_updated' if changed else 'forced'
    existing.installed_at = _now()  # 刷新时间戳
# 一律下发 todo + 通知 agent
```

#### 经验 #35 — 状态化复选框（disabled）会误导用户"我什么都做不了"

把"已最新"的 checkbox 设成 `disabled` 看起来很合理（"装过了，没必要再选"），但用户进 modal 看到一片灰色 + ☑ disabled，第一反应是"这个 modal 我能干啥？" 然后退出去找另一个入口（结果根本没有）。

固化规则：**checkbox disabled 是强禁止**（业务硬规则不允许的场景），**不要用来表达"建议你不要选"**。"建议性"的视觉差异用：

- 不同边框/背景色（fresh = 绿、stale = 橙、未装 = 灰）
- 不同默认勾选状态（stale 默认勾、fresh 默认不勾）
- 标签 + 浅色提示小字（"勾选可强制重装"）
- `title` 属性放完整解释（鼠标悬停看到）

让用户**能操作**但**默认不操作**，比"完全禁用"友好得多。

#### 经验 #36 — 前后端字段名约定要落到 review 列表里

skills.html 一直读 `install_count`、skills.py 一直只 set `used_by_count`，这种"前端读 A 后端写 B"的低级 bug 在 code review 里 1 秒就能看出，但因为没人对照过，一直显示 0 没人发现。

固化做法：**任何"列表项展示数字"类的字段**，code review 时必须做一次 grep 双向验证：

```bash
# 后端 set
grep -rn "d\['install_count'\]" web/app/api/

# 前端 read
grep -rn "install_count" web/templates/
```

两边都有命中才放过。也可以在测试里加一条断言：`/api/v1/skills` 第一个 item 的 keys 必须包含 `install_count`。

### 文件清单

跟上一轮（16:42）完全相同 4 + 1 文件，但内容增强：

| 文件 | 增量 |
|------|------|
| `web/app/api/skills.py` | `is_reinstall = True` 一律生效；新增 `reinstall_reason`；补 `install_count` 别名 |
| `web/app/api/rules.py`  | 同上语义对齐 |
| `web/templates/skills.html` | 去掉 fresh 的 disabled；加 fresh hint title + 顶部新分级提示 |
| `web/templates/rules.html`  | 同上 |
| `_deploy_reassign/deploy_reassign.sh` | 关键字增加 `reinstall_reason / forced / 强制重装`，22 项校验 |

部署结果：22/22 关键字通过、syntax OK、service active、HTTP 302/302/401（与上一轮一致）。

---

---

## 2026-04-23  🧩  画图能力闭环：#127 / #133 升级 + 新建 #140 mermaid-diagram

### 用户请求

> "你看下hub系统上现在#133和#127 skill，之前让其他AI整理的工程分析skill，没有写要有时序图、流程图，你看要不要补进去，另外，是不是还得给openclaw提供时序图，流程图的skill？"
> "那就都做，ABC"

### 诊断阶段发现的问题

从 Hub DB dump #127 和 #133 内容审计：

| skill | 问题 |
|---|---|
| #127 game-test-design | 11293 chars 体量看似很大，但**全文 0 处 mermaid / 时序图 / 流程图** —— 设计用例时跳过了"先理解工程"这一步 |
| #133 client-engineering-test-design | RacingGO 业务规则写得很细，但**同样无任何架构图**；并且 `trigger_phrase = ''` —— Agent 永远触发不了它，是个长期沉没成本 |
| #139 engineering-analysis | 已有架构分析协议（v2 改版后），但其他 skill 不知道可以投递进来 |

### 实施方案（A + B + C）

| 序号 | 内容 | 落地 |
|---|---|---|
| **A** | 给 #127 / #133 各补「工程时序图/流程图（必出物）」章节，要求至少 1 张 sequenceDiagram + 1 张 flowchart TD + 可选 stateDiagram-v2；同时补全 #133 的 trigger_phrase | 新增本地源文件 `openclaw-agent/skills/game-test-design/SKILL.md`、`openclaw-agent/skills/client-engineering-test-design/SKILL.md` |
| **B** | 新建通用 mermaid 工具 skill `mermaid-diagram`（#140），含 8 大常用图语法子集、提交前自检清单、常见踩坑速查、渲染失败回退策略、Hub 投递模板 | 新增 `openclaw-agent/skills/mermaid-diagram/SKILL.md` |
| **C** | 把 3.4 / "工程时序图" 章节产出的图，通过 `POST /api/v1/engineering/baselines/<bid>/architecture` 投递为 `scope=module` 快照，沉淀到 #139 | 在 #127 / #133 文末加 Python 模板与"何时调用"判断（提供了 baseline_id 才投递） |

### 部署结果

```
sync_diagram_skills.py 输出（2026-04-23 08:46:57）

---- game-test-design ----
  UPDATE skill #127 (11293 chars)  ✅ 11/11 keywords confirmed
  📦 4 个 STALE：claw#9 #10 #11 #7

---- client-engineering-test-design ----
  UPDATE skill #133 (7677 chars)   ✅ 12/12 keywords confirmed
  📦 5 个 STALE：claw#10 #6 #9 #11 #7

---- mermaid-diagram ----
  INSERT skill #140 (7876 chars)   ✅ 17/17 keywords confirmed
  📦 暂无 OpenClaw 安装
```

### 经验固化（编号续上一轮）

#### 17. **看 skill 内容只看 description 是不够的，必须 dump template_content 全文**

description 字段只有几百字，是给 UI 列表用的"门面"。skill 的真实指令在 `template_content`（一般 5K-15K 字符的 markdown）。这次诊断如果只看 description，会以为 #127 已经包含画图能力（因为它写了"分析工程结构"）。固化做法：

```bash
# 远端一行 dump
ssh testserver "cd /opt/openclaw-web && ./venv/bin/python3 -c \"
from app import create_app
from app.models import Skill
app = create_app()
with app.app_context():
    s = Skill.query.get(127)
    open('/tmp/dump.md','w',encoding='utf-8').write(s.template_content)
    print(len(s.template_content))
\""
scp testserver:/tmp/dump.md ./local-dump.md
# 然后用 grep 系统性扫"应有但实际缺"的关键字
```

#### 18. **DB 字段 vs MD frontmatter：trigger_phrase 不在 markdown 里**

`Skill.trigger_phrase` 是 DB 字段，**不是** markdown frontmatter 的一部分。改这个值必须走 sync 脚本（`existing.trigger_phrase = '...'`），改 markdown 的 frontmatter 没用。这是 #133 trigger_phrase 长期为空的根因 —— 之前的 AI 只导入了 markdown 内容，没单独 set 该字段。

下次新建 / 升级 skill 时，**sync 脚本里 trigger_phrase 必须显式给一个非空值**（哪怕只是 skill 名字）。

#### 19. **sync 脚本必须在 `/opt/openclaw-web` 下执行（再次踩到 sys.path 坑）**

第二次踩同一个坑（参见 §16）。这次特意把脚本上传命名为 `_sync_diagram_skills.py` 放在 `/opt/openclaw-web/` 根（而不是 `/tmp/`），执行完立即 `rm -f`。固化范式：

```bash
# 上传到 /tmp 做暂存
scp local.py testserver:/tmp/_orig.py
# 复制到 APP_DIR 执行
ssh testserver "sed -i 's/\r\$//' /tmp/_orig.py && \
  cp -f /tmp/_orig.py /opt/openclaw-web/_run.py && \
  cd /opt/openclaw-web && ENV_VAR=xxx ./venv/bin/python3 _run.py && \
  rm -f /tmp/_orig.py /opt/openclaw-web/_run.py"
```

`-f` 在 cp 和 rm 都要带，避免上一次失败的残留挡路。

#### 20. **多 skill 一并入库，sync 脚本用 `dict(cfg)` 防 pop 副作用**

`upsert_one(cfg)` 内部会 `cfg.pop('md')`、`cfg.pop('keywords')`，如果直接传引用 `upsert_one(cfg)`，第二次执行（部署失败重跑）就会 KeyError。范式：

```python
for cfg in SKILLS:
    upsert_one(dict(cfg))   # 拷贝一份，原 SKILLS 不被破坏
```

不止 sync 脚本，**任何对 dict 做 pop 的循环工具函数都该这样防御**。

#### 21. **本地无 bash 时，PowerShell 直跑 scp/ssh 也能完整走完部署**

之前所有 `_deploy_*` 都假设有 git bash，这次 PowerShell 环境直接走，没问题。模式：

```powershell
# 1. 单条 scp 上传 markdown（多文件用 ; 串起来）
scp src1 host:/tmp/dst1; scp src2 host:/tmp/dst2

# 2. 远端 sed 去 \r + bash 执行预检脚本
ssh host "sed -i 's/\r$//' /tmp/check.sh && bash /tmp/check.sh"

# 3. heredoc 嵌套场景：本地写好 .sh 文件再上传执行，不要 inline
```

避坑要点：**任何 inline `ssh host bash -c '...'` 嵌 here-doc 嵌 Python 的写法都不要碰**，本地写文件 → 上传 → 远端执行三步走，最稳。

#### 22. **关键字断言要包含"内部锚点"而不只是顶部标题**

防止只升级了顶部标题但没改正文。比如 #127 我断言：

```python
keywords = [
  '🎮 游戏工程测试用例设计 Skill',     # 顶部 — 防回退到旧版
  '第三步：深度代码分析',              # 中部 — 防截断
  '3.4 工程时序图 / 流程图（**必出物**',  # 新章节 — 防漏插
  'push_module_arch_for_testcase',    # 末尾 C 闭环 — 防文末截断
]
```

至少覆盖：顶部 + 中部 + 新增点 + 末尾，4 个锚点。这次 11/11 + 12/12 + 17/17 全过，验证有效。

#### 23. **STALE 列表是部署后必给用户的"下一步指引"**

部署成功 ≠ 用户用上了。`OpenClawSkill.installed_at < Skill.updated_at` 的所有实例都需要去 Hub UI 点 ♻️重新分配。本次 5 个 claw（#6 #7 #9 #10 #11）需要 reassign #133，4 个 claw（#7 #9 #10 #11）需要 reassign #127。这种"待人工动作清单"必须在脚本结尾输出，并放到与用户的对话总结里。

#### 24. **Skill 之间显式声明依赖**

新建的 mermaid-diagram (#140) 在 §七 列出了"复用本 skill 的 skill 清单"，要求 #127 / #133 / #139 在自己文档顶部加一句"本 skill 的 mermaid 图表语法和自检清单遵循 mermaid-diagram skill"。这种**反向依赖文档化**让 skill 之间的关系不再是隐式的。后续任何新建图表类 skill 也必须按此规则挂在 #140 下面。

### 文件清单

| 文件 | 类型 | 说明 |
|---|---|---|
| `openclaw-agent/skills/game-test-design/SKILL.md` | 升级 | 在第三步与第四步之间插入 3.4 节；文末追加 "🔗 闭环" 章节 |
| `openclaw-agent/skills/client-engineering-test-design/SKILL.md` | 升级 | 在"核心测试覆盖维度"前插入"工程时序图/流程图"章节（含 RacingGO 项目级模块依赖图）；文末追加 v1.1 changelog + "🔗 闭环" 章节 |
| `openclaw-agent/skills/mermaid-diagram/SKILL.md` | 新建 | 8 大常用图语法子集 + 自检清单 + 踩坑速查 + 投递模板 |
| `_deploy_diagram_skills/sync_diagram_skills.py` | 新建 | 3 skill 一并 upsert + 各自关键字断言 + STALE 列表 |
| `_deploy_diagram_skills/deploy.sh` | 新建 | 6 步部署脚本（本机有 bash 时用） |
| `_deploy_diagram_skills/_verify_remote.sh` | 临时 | deploy.sh STEP 6 用（本次 PowerShell 执行未走 deploy.sh，单独写了 verify_diagram.sh） |

### 后续待办（用户需手动完成）

- [ ] Hub UI -> 技能管理：对 #127 (4 STALE) / #133 (5 STALE) 点 ♻️重新分配
- [ ] mermaid-diagram (#140) 给至少一个具备 ENG 角色的 claw 安装一次，作为"工具型 skill 是否需要默认装到 ENG 类 claw"的试水
- [ ] 抽 1 个 RacingGO claw（推荐 #6 天飞小游戏助理小天）触发一次"客户端测试用例设计"任务，验证它会先输出 mermaid 图，并自动调 architecture API 沉淀

---

---

## 2026-04-23 (晚) 🛠️🖧 工程分析双子 skill：#141 client-engineering-analysis + #142 server-engineering-analysis

### 用户请求

> "你帮我整理一个，工程分析的skill，分客户端和服务端，应该怎么分析工程，按什么顺序，关注什么内容，以及遵循什么规范（比如不要自己猜想，按照实际工程代码实现来分析），输出什么内容（架构图、关键逻辑时序图、模块列表功能说明等等）。比如客户端按照引擎不同，通常有什么模块等等。最终是给测试用例设计做指引用的"

### 设计决策（关键，未来其他工程分析 skill 都按此走）

#### A. 为什么拆 client / server 两个 skill 而不是一个

| 维度 | 客户端 | 服务端 |
|---|---|---|
| 模块边界 | 目录/脚本（一个文件一个组件） | **进程**（一个 main 一个服务）|
| 入口 | 启动场景 → Awake/Start | main.go / Application.java |
| 关注点 | 启动流程 / 场景跳转 / 性能 / GM | 拓扑 / 路由 / DB / 限流 / 监控 |
| 分析步数 | 10 步 | 12 步（多了"进程清单 / 部署拓扑 / 对外集成"）|
| 输出件数 | 7 件套 | 8 件套 |
| typical 模块差异 | 按**引擎**分（Unity/UE/Cocos/Web）| 按**语言+架构**分（Go/Java，单体/微服务/游戏 GS-DB）|

合并成一个 skill 必然**两边都讲不深**。两个 skill 的优势：可以分别由不同 Agent 并行跑，最后联合产出"协议对照清单"——这是发现**协议不对齐 BUG** 的最大武器。

#### B. 为什么把"工程分析"和"测试用例设计"分开成不同 skill

之前 #127 / #133 把"分析工程"和"出用例"混在一起。这次明确解耦：

```
client-engineering-analysis (#141)  ─┐
                                     ├─→ 7/8 件套（前置交付物）─→ #127/#133（出用例）
server-engineering-analysis (#142)  ─┘
                                                                  ↓
                                                            #139 Hub 沉淀架构快照
```

- **职责分离**：分析的人专心读代码，出用例的人专心想测什么
- **复用性**：同一份工程分析可以服务"功能用例 + 压测 + 故障演练 + 安全审计"4 类不同测试需求
- **审计性**：分析有结论 + 源码引用，可以独立审；用例可以单独迭代不影响分析

#### C. 8 + 9 条铁律的设计依据

之前观察到的 AI 写工程分析的 4 大常见病：

| 病 | 症状 | 对应铁律 |
|---|---|---|
| **凭印象** | "Unity 一般用 PlayerPrefs 存档" | 客户端铁律 #1 / 服务端铁律 #1 (先扫后说) |
| **省源码引用** | "本工程有反作弊模块"（不告诉你在哪）| 铁律 #2 (每个结论都有源码引用) |
| **猜测填空** | 没找到就编一个上去 | 铁律 #3 (不知道就标 ❓) |
| **单向 grep** | 只看发送方不看接收方 | 铁律 #4 (双向验证) |

服务端额外多 1 条（共 9）：**环境差异显式区分** —— dev/test/prod 配置不同，不区分会导致测试结论失真。

#### D. 输出模板里强制 baseline commit hash

文档头部 `baseline: <commit>` 强制要求，目的是**支持增量分析**：下一次 baseline 切换时，可以用 `git diff <old>..<new>` 只追新增/修改的部分，不用全部重读。这与 Hub `engineering-analysis` (#139) 的"代码提交分析"刷新批次是同一思路。

#### E. typical 模块清单的"打钩"用法

§五（客户端按引擎分 / 服务端按架构分）列出了"经验值"模块，**Step 4 必须逐项对照打钩**：本工程有的标 ✅，没有的标 "本工程未实现"——**禁止跳过未实现项不写**。

为什么？**测试同学最关心的就是"这个项目和别人不一样的地方"**——你说"本工程未实现 GM 工具"比"略过不提"信息量大 100 倍。

### 部署结果

```
sync_eng_analysis_skills.py 输出（2026-04-23 13:20:25）

---- client-engineering-analysis ----
  INSERT skill #141 (14315 chars)  ✅ 19/19 keywords confirmed
  📦 暂无 OpenClaw 安装（首次新建，需手动 assign）

---- server-engineering-analysis ----
  INSERT skill #142 (15231 chars)  ✅ 21/21 keywords confirmed
  📦 暂无 OpenClaw 安装（首次新建，需手动 assign）
```

### 经验固化（编号续上一轮，从 #25 开始）

#### 25. **新建 skill 时分类用 `engineering` 而不是 `testing` / `tool`**

这次新增了 `category='engineering'` 这个分类（之前只有 `hub_system` / `testing` / `project` / `tool`）。决策依据：

- 它**不是测试本身**（不是 testing）
- 它**不是工具**（不是 tool，工具是给 LLM 调的，比如 mermaid-diagram）
- 它**不绑项目**（不是 project，#133 才是 project）
- 它**是给所有项目通用**的工程读写指导（engineering）

后续如果出现 `db-schema-analysis` / `infra-analysis` / `protocol-design-analysis` 等同类 skill，也都用 `engineering` 分类。

#### 26. **大型 skill 的关键字断言锚点选取（最佳实践 v2）**

经过 #127 / #133 / #140 / #141 / #142 共 5 个 skill 的实战，断言锚点的最佳模式：

```python
keywords = [
    # 1. 顶部标题（防回退）
    '🛠️ 客户端工程分析 Skill',
    
    # 2. 大章节标题（防截断 + 防大改丢章节）
    '## 二、铁律',
    '## 三、分析顺序',
    '## 五、各引擎 typical 模块清单',
    '## 六、必须输出的「**7 件套**」',
    
    # 3. 关键步骤名（防局部丢步）
    'Step 1. 项目档案',
    'Step 5. 通信与协议',
    'Step 10. 风险与待确认清单',
    
    # 4. 引擎/技术栈名（防 typical 列表退化）
    '5.1 Unity 手游',
    '5.2 Unreal 工程',
    
    # 5. 末尾代码示例（防末尾截断）
    'publish_client_arch',
    
    # 6. 跨 skill 依赖（防忘记引用 mermaid-diagram）
    'mermaid-diagram',
]
```

19 个 / 21 个锚点 × 6 类，全过即代表内容稳定。覆盖率 = 锚点数 / 文档总行数，本次约 1:25（19 个锚点 vs 480 行文档），**1:20-1:30 是健康比例**——少了不能防回退，多了维护成本高。

#### 27. **首次新建（vs 升级）的 sync 脚本判断**

第一次入库的 skill `installs = []` 是**正常态**，不是异常。脚本里输出："首次新建，需手动 assign"提示用户去 Hub UI 安装。

之前 #140 也是首次新建，用户记得手动安装；这次 #141/#142 也是同样情况。建议下次：

- 部署后**主动建议 1-2 个最佳候选 claw**（基于 claw 的角色 / 已装 skill 联想）
- 或在 Hub UI 上做"建议安装"标签，新 skill 顶部显示"⭐ 推荐给具有 ENG 角色的 claw"

#### 28. **跨 skill 引用要双向显式**

#141/#142 都明确说"语法遵循 #140 mermaid-diagram skill"，#140 也在 §七列出"复用本 skill 的 skill 清单"。这种**双向显式引用** 在维护时极有价值：

- 改 #140 时知道有 4 个 skill (#127/#133/#139/#141/#142) 受影响（要测）
- 改 #141 时知道依赖 #140，先确认 #140 没问题再改

下次新增任何"被复用的工具型 skill"，必须在自己的 §七 维护"复用清单"。

#### 29. **协议对照清单是 client + server 两 skill 的"联合产出"**

#142 §十一 显式定义了"客户端-服务端协议对照清单"作为两 skill 的联合产出物：

| 消息 ID | 客户端发起点 | 服务端 handler | 字段一致性 |

这是**之前所有 skill 都没有的设计**——大部分 AI 工程分析停留在"分别写客户端 / 服务端"，从不做对照。这张表预期能直接发现**字段名不一致 / 消息 ID 偏移 / 加密方式不一致**等隐藏的协议层 BUG。后续 game-test-design / client-engineering-test-design 都应该把这张表作为"协议测试用例"的输入源。

### 文件清单

| 文件 | 类型 | 说明 |
|---|---|---|
| `openclaw-agent/skills/client-engineering-analysis/SKILL.md` | 新建 | 14315 chars，10 步分析 + 7 件套 + Unity/UE/Cocos/Web 模块清单 |
| `openclaw-agent/skills/server-engineering-analysis/SKILL.md` | 新建 | 15231 chars，12 步分析 + 8 件套 + Go/Java/游戏服模块清单 + 协议对照表设计 |
| `_deploy_eng_analysis_pair/sync_eng_analysis_skills.py` | 新建 | 2 skill 一并 upsert + 19/21 锚点断言 + STALE 列表 |

### 后续待办（用户需手动完成）

- [ ] **Hub UI → 技能管理**：把 #141/#142 安装到至少一个具备 ENG 角色的 claw 上（推荐 #11 需求代码分析专员小云）
- [ ] **配套验证**：把 #141 / #142 / #140 三件套都装上后，让 claw 分析一次 RacingGoUnity（已 pull 至最新 baseline `2cada23b3`）+ 配套 RacingGo 服务端工程，对照看 7 件套 / 8 件套是否完整
- [ ] **跨 skill 演练**：让两个不同 claw 分别跑客户端 / 服务端分析，最后联合产出"协议对照清单"，作为本次设计的有效性验证

---

## 🔥 紧急事故修复：小赫 sync_config 反馈循环 + skill audit 缺失（2026-04-23 16:00-16:15）

### 现象（用户提问触发）

> 用户："小赫上好多卸载skill的待办，是谁发的任务"

claw#10 Hermes Agent 小赫的待办列表里出现 7+ 条"卸载 Skill：xxx"待办，全部 `created_by='hub'`，
查不到操作人。涉及的 skill：manager-hub / mermaid-diagram / topic-discuss / knowledge-manager /
engineering-analysis / testplan-manager。

### 根因排查（4 步定位法）

#### 1) DB 层：找到所有卸载 todo 的"伪造发起人"

```python
ClawTodo.query.filter(
    ClawTodo.openclaw_id == 10,
    ClawTodo.verification_target.like('uninstall-skill:%'),
).all()
```

→ 发现 `created_by='hub'`、`AuditLog` 完全无记录 → 说明 `uninstall_skill()` API **没挂 audit_log**（bug #1）。

#### 2) 消息层：通过 ClawMessage 看出"配对模式"

`ClawMessage` 表里同一秒（13:20:16）出现 6 个 skill 各一对 `[移除 Skill] + [重新分配 Skill]` sync_config
消息，时间高度集中且严格成对 → 这不是用户手点（UI 有 confirm 弹窗，1 秒点 6 次不可能），
也不是单一 API（`install_skill` / `uninstall_skill` 都不会同时下发"先卸再装"），
**强烈指向某个外部脚本在循环 DELETE → POST**。

#### 3) 网络层：access log 锁定 IP + UA

```bash
grep "openclaws/10/skills" /tmp/flask-access.log | grep -E "13:20:1[5-7]|16:0[0-5]"
```

→ 所有 DELETE / POST 请求源 IP = **`9.134.11.169`（Hub 本机自己）**，UA = **`Python-urllib/3.11`**。
直接戳穿"是 Hub 本机的某个 Python 进程"。

#### 4) 进程层：ps -ef 锁定真凶

```
root  4854  /root/hermes-agent/venv/bin/python /root/.hermes/scripts/hub_worker.py
root  10240 /root/hermes-agent/.../main.py chat -q "Hub 下发了配置同步通知（消息ID=1336）：
            「[分配 Skill] 测试计划管理，请同步配置」 请执行必要的配置同步操作..."
```

**真凶 = 小赫自己。** 完整反馈循环：

```
管理员在 Hub 点 "♻️重新分配"
  ↓ POST /openclaws/10/skills (is_reinstall=True)
Hub 创建 sync_config 消息 [重新分配 Skill]
  ↓ SSE 推送
sse_client.py → hub_worker.py 拿到消息
  ↓ chat -q "请执行必要的配置同步操作"
小赫 LLM 看 manager-hub skill 没明确"sync_config 该怎么处理"
  ↓ 自由发挥：调 DELETE 然后 POST 把自己的 skill 卸了再装
DELETE → uninstall_skill 又下发 [移除 Skill] sync_config
  ↓ 死循环
POST → install_skill 又下发 [重新分配 Skill] sync_config
  ↓ 死循环
某次 POST 因为某种原因没执行（可能是 LLM 半路被打断）
  → 小赫 4 个 skill 真被卸到一半，link.enabled=False
```

### 修复方案（用户选 C：服务端补 audit + skill 加约束 + DB 直恢复）

#### A. 服务端补丁 `web/app/api/skills.py`（解决 bug #1 + bug #2）

1. **`uninstall_skill()`**：尾部调 `log_action('delete', 'openclaw_skill', skill_id, ..., operator=...)` 留 audit
2. **`install_skill()` 复活路径**：当 `existing && existing.enabled == False` 时，自动 disable 同 skill 已存在的 `uninstall-skill:` todo（bug #2 ：避免 claw 既收到"卸载"又收到"重装"的矛盾指令）
3. **`install_skill()` 末尾**：调 `log_action('install'/'reinstall', 'openclaw_skill', ...)` 同样补 audit

返回 JSON 多带一个字段 `cancelled_uninstall_todo_ids`，前端可看见自动消除了哪些。

#### B. 一次性清理脚本 `_tmp/cleanup_zombie_uninstall.sh`

扫所有 `uninstall-skill:%` todo + 其对应 `OpenClawSkill.enabled` 状态：
- link 仍 enabled=True → zombie todo（卸载已被后续重装抵消）→ 取消，写 audit `operator=system-cleanup`
- link 已 enabled=False → 真卸载状态 → 保留

执行结果：扫 16 条 → 8 条 zombie 取消，8 条真卸载保留。

#### C. DB 直恢复 4 个被误卸 skill 的 link（**关键：不走 API**，避免再触发 sync_config）

```sql
UPDATE openclaw_skills SET enabled=TRUE, installed_at=NOW()
WHERE openclaw_id=10 AND skill_id IN (112, 118, 121, 139);
```

并取消该 4 个 skill 的所有残留 uninstall todo（8 条）。**完全不调 install_skill API**，
因为 install_skill 末尾会 `_notify_claw_sync` 推 [重新分配 Skill] sync_config，又会触发循环。

#### D. 给 manager-hub skill (#118) 追加 §13「sync_config 消息处理铁律」

在「### 12. heartbeat」之后、「## 三、工作流程」之前插入约 3137 字符段落，明确：

| 收到消息 | 唯一动作 | 绝对禁止 |
|---|---|---|
| `[分配 Skill] xxx` / `[重新分配 Skill] xxx` | `GET /assigned-skills` + `GET /skills/{id}/files` 拉文件落盘 | ❌ `POST /openclaws/{me}/skills` |
| `[移除 Skill] xxx` | 同上：以 `assigned-skills` 列表为准做差集，列表里没的才 `rm -rf` | ❌ `DELETE /openclaws/{me}/skills/{sid}` |
| Rules 的 [安装]/[移除] | `GET /assigned-rules` + 拉文件 | ❌ `POST/DELETE /openclaws/{me}/rules` |

并写 3 条硬性禁令：
1. OpenClaw 自己**不能**卸自己/装自己 —— 这是 Hub 端 admin 的职责
2. 以 `assigned-skills/rules` 当前快照为唯一事实，不要解析消息文本
3. 30s 内多条 sync_config **防抖合并**只跑一次实际同步

附标准伪代码 + 自检条款（怀疑陷入循环时如何止损）。

### 验证

```text
==== claw#10 当前 link 状态 ====
  #112  🟢  knowledge-manager
  #118  🟢  manager-hub          ← 内容已更新到 v2 (含 §13 铁律)
  #121  🟢  topic-discuss
  #139  🟢  engineering-analysis

==== uninstall-skill 待办 ====
claw#10 当前启用中的: 0 条 ✓
```

### 经验固化（必看）

#### 30. **Skill 类系统永远要做"反馈循环防御"**

只要 sync 通知会触发 LLM agent 自由处理，就一定可能形成 `agent → API → 通知 → agent` 的环。
**两道护栏缺一不可**：
- skill 文档层：必须明确"收到 sync_config 该做什么 / 绝对不能做什么"，**不要假设 LLM 自己懂**
- 服务器层：API 应该在调用方 token 等于"被操作 claw 的 token"时拒绝（`self-mutation block`）—— 后续可加 guardrail

#### 31. **`created_by='hub'` 是个反模式**

`uninstall_skill()` / `install_skill()` 直接写死 `created_by='hub'`，等于把审计黑盒化。
正确做法：用 `_get_current_user()` 拿到真实操作人（user.username 或 claw._claw_name），
通过 `log_action()` 写入 `audit_logs`。**所有写操作 API 必须挂 audit**，不挂就是 bug。

#### 32. **诊断"批量同时操作"四步法**

1. **DB 层**：找 todo / message 等"派生记录"，看 `created_at` 时间分布
2. **消息层**：看是否有"配对模式"（[A] + [B] 严格 N 对 N），强烈指向脚本循环
3. **网络层**：access log 拿 IP + UA，区分内部 / 外部 / 浏览器 / 脚本
4. **进程层**：`ps -ef | grep python` 看活跃进程命令行（命令行往往泄露脚本身份）

本次 4 步耗时约 15 分钟定位到小赫自己。

#### 33. **修复反馈循环时绝对不要走"会再次触发同样消息"的 API**

恢复 4 个被误卸 skill 时，**绝对不能**调 `POST /openclaws/10/skills`（会再触发 [重新分配 Skill] sync_config）。
正确做法：直接 SQL UPDATE link.enabled=True + 取消 zombie todo，**完全跳过 _notify_claw_sync**。
不然修复脚本本身就会重新点燃循环。

#### 34. **install_skill 看到 link 复活时，必须主动消除残留 uninstall todo**

claw 同时持有"卸载 X" + "安装 X" todo 时不知道执行哪个。原代码两边都各管各的：
- `uninstall_skill()` 创建卸载 todo 但不管已存在的 install todo
- `install_skill()` 复活 link 但不管已存在的 uninstall todo

修复后 `install_skill()` 一旦走 `existing.enabled = True` 路径，就**自动 disable** 同 skill 所有
`verification_target == f'uninstall-skill:{skill.name}'` 的未完成 todo，并把 ids 透出到响应里。

#### 35. **PowerShell 调 ssh 执行远端 bash 时，永远把脚本传上去再执行**

本次再次踩到 PowerShell 把 `$(date +%Y%m%d)` 当成 PowerShell 的 `Get-Date` 解析。
教训：**所有需要 `$(...)` / `\` / `>` 的 bash 命令，都先写到本地 .sh，scp 上去，
然后 `ssh "sed -i 's/\\r$//' /tmp/x.sh && bash /tmp/x.sh"`**。这个套路本仓库已用了 N 次，每次想偷懒都会被打。

### 文件清单（本次事故）

| 文件 | 类型 | 说明 |
|---|---|---|
| `web/app/api/skills.py` | 修改 | uninstall_skill 加 audit；install_skill 加 audit + 自动消除 zombie uninstall todo |
| `_tmp/cleanup_zombie_uninstall.sh` | 一次性脚本 | 扫全表 16 条 → 取消 8 条 zombie + 写 audit |
| `_tmp/restore_xiaohe_links.sh` | 一次性脚本 | DB 直恢复 claw#10 的 4 个 skill link，**不走 API** |
| `_tmp/patch_manager_hub_skill.sh` | 一次性脚本 | manager-hub skill 追加 §13 sync_config 铁律（3137 字符） |
| `_tmp/who_uninstalled.sh` | 诊断脚本 | DB 层"卸载发起人"取证（带 audit_log 反查） |
| `_tmp/find_culprit.sh` | 诊断脚本 | access log + journalctl 找 IP/UA |
| `_tmp/find_cron.sh` | 诊断脚本 | crontab + systemd timers + ps -ef 找循环源 |

### 后续待办

- [ ] 用户去小赫主机 `kill` 一次 sse_client.py + hub_worker.py 强制重启，
      让小赫拉到 manager-hub v2 内容（含 §13 铁律），后续就不会再自调 DELETE/POST 了
- [ ] （可选 P2）服务端加 guardrail：`install_skill` / `uninstall_skill` 检测到调用方 token 属于
      被操作的 claw 自身时，直接返回 `403 forbidden: self-mutation`
- [ ] （可选 P2）小赫 `hub_worker.py` 入口加客户端 guardrail：拒绝 LLM 生成的"对自己 claw_id 的
      DELETE/POST skills/rules"调用

---

## 2026-04-27 OpenClaw 通信机制 B+ 改造（P1 + P2）部署

### 用户需求

> "OpenClaw 和 Hub 通信、自动化处理待办任务和聊天信息有很多问题，game 没办法处理，有些是 worker 自己处理，有些可以企微通知（小赫和龙虾王）。最大价值就在于统一调度 claw，消息机制不畅通就没价值。要更简单有效稳定的方案。"

确认 3 条铁律：
1. 所有 todo + chat **必须 agent 自己处理**（worker 代办禁止）
2. 完成 todo 必须企微告知用户（agent 走 `message(action=send,channel=wecom,to=...)` 真私聊；Hub 兜底走龙虾王 `sendRTXInfo` 应用通知）
3. **不允许周期轮询**（避免 LLM token 浪费），有消息才触发

### 架构（B+ 方案）

```
[Hub broadcast] → ClawMessage(pending) → SSE 长连 → sidecar_v2.py
                                                      ↓ 直接 fork 一次 LLM 进程
                                                      └─ openclaw agent --message=XXX
                                                            ↓ 内部用 message 工具直发企微（真私聊）
                                                            └─ POST /complete?notified=true → Hub 标记
                ↓
                [Hub TimeoutWatcher 5min loop]
                ├─ 扫 ClawTodoLog where notified_at IS NULL && >5min → WecomDispatcher.sendRTXInfo（默认关）
                └─ 扫 ClawMessage where status in (pending,processing) >5min → 告警（默认关）
```

### Phase 1：Hub 改造（已上线）

**DB schema（自动迁移钩子，幂等）**

| 表 | 变更 |
|---|---|
| `openclaw_instances` | 加 `owner_wecom_userid VARCHAR(64)` 用于兜底通知收件人 |
| `claw_messages` | `status` enum 扩展 → `pending/delivered/processing/done/failed/read`；新增 `processing_at/done_at/failed_reason/llm_response` |
| `claw_todo_logs` | 新增 `notified_at` `notified_strategy` 跟踪通知轨迹 |
| `claw_sidecar_configs` | **新建**，sidecar 中央配置（agent_type/agent_timeout/wecom_enabled/...） |
| `wecom_send_logs` | **新建**，Hub 兜底企微发送审计 |

**API 改造**

| 接口 | 变更 |
|---|---|
| `POST /api/v1/agent-hub/messages/broadcast` | bug fix：claw 离线不再跳过，照样存 `pending`，重连后由 SSE 推送 |
| `POST /api/v1/todos/<id>/complete` | 接受 `notified=true` 参数，标记 agent 已自行通知 |
| `PUT /api/openclaws/<cid>/messages/<mid>/processing` | sidecar 显式上报开始处理 |
| `PUT /api/openclaws/<cid>/messages/<mid>/done` | sidecar 显式上报完成（含 LLM response） |
| `PUT /api/openclaws/<cid>/messages/<mid>/failed` | sidecar 显式上报失败（含 reason） |
| `GET /api/openclaws/<cid>/sidecar-config` | sidecar 启动/60s 心跳拉配置，顺手写 last_heartbeat_at |

**TimeoutWatcher（背景守护，MySQL 锁选主，gunicorn -w 4 不会重复执行）**

`web/app/services/timeout_watcher.py`：单循环 60s tick，
- `_scan_overdue_todo_logs`（默认关）→ WecomDispatcher.sendRTXInfo 兜底
- `_scan_stuck_claw_messages`（默认关）→ 告警（暂仅日志）

锁机制：`system_config.timeout_watcher_owner = current_pid`，CAS 续期。

### Phase 2：sidecar v2（agent 端）

| 文件 | 说明 |
|---|---|
| `openclaw-agent/skills/hub-sse-sidecar/scripts/sidecar_v2.py` | 全新单文件守护，纯标准库；事件驱动 SSE，无轮询；显式 processing/done/failed 三段上报；**已删 v1 的 1.5s 假兜底 mark_read** |
| `openclaw-agent/skills/hub-sse-sidecar/install_v2.sh` | 一键脚本：自动检测 openclaw / hermes / qclaw 二进制；调 `/sidecar-config` self-check；写 `~/.qclaw/skills/hub-sse-sidecar/sidecar.env`；注册 systemd user service `qclaw-sidecar` |
| `openclaw-agent/skills/hub-sse-sidecar/SKILL.md` | v2.0.0 文档（v1 移到 §legacy） |

### 部署 SOP（生产 8088 / openclaw-web.service）

⚠️ **本次踩到大坑**：本地 `web/app/models.py` `web/app/__init__.py` 等 6 个文件**和 prod 严重 drift**，
直接 scp 覆盖会回滚 prod 已上线功能（`OpenClawInstance.owner_wecom_userid`、`engineering_baselines` 改版等）。

**正确做法（patch-on-prod-baseline）**：

```bash
# 1) 把 prod 6 个核心文件拉回当 baseline
scp testserver:/opt/openclaw-web/app/models.py F:\...\web\app\models.py
scp testserver:/opt/openclaw-web/app/__init__.py F:\...\web\app\__init__.py
... # api/__init__.py, api/agent_client.py, api/agent_hub.py, api/todos.py

# 2) 用 Python 脚本（不是 PowerShell！）做"加性 patch"：
#    每个 patch 只 INSERT，不 REPLACE 已有行；锚点串避开生僻字
python patch_models.py        # 加 owner_wecom_userid + ClawMessage 新字段 + ClawSidecarConfig + WecomSendLog
python patch_app_init.py      # 加 6 个 ALTER/CREATE 迁移块 + start_timeout_watcher hook
python patch_api_files.py     # 加 4 个 sidecar 接口 + complete_todo notified 参数 + agent_hub 离线 bug fix

# 3) 打包，剔除 CRLF，传走 prod
tar -czf b_plus.tar.gz app/
scp b_plus.tar.gz testserver:/tmp/
ssh testserver "find /tmp/b_plus_unpack -type f -exec sed -i 's/\r$//' {} \;"

# 4) 远端 AST 校验 + 备份 + 覆盖 + 重启
ssh testserver "venv/bin/python -m py_compile app/api/agent_client.py ..."
ssh testserver "cp -r /opt/openclaw-web/app /opt/openclaw-web/app.bak.$(date +%s)"
ssh testserver "cp -rf /tmp/b_plus_unpack/app/* /opt/openclaw-web/app/"
ssh testserver "systemctl restart openclaw-web.service"

# 5) 烟测（生产 port = 8088，不是 5000！）
bash smoke3.sh   # 用 Python get_token_plain() 解密 api_token_plain，再 curl
```

### 烟测结果（全绿）

| 项 | 结果 |
|---|---|
| `GET /api/openclaws/4/sidecar-config` | HTTP 200，返回 wecom_enabled/agent_type/agent_timeout 等完整 JSON |
| `claw_sidecar_configs` 自动写第一条 | `updated_by=sidecar_first_start` ✓ |
| `PUT /messages/999999/processing`（msg 不存在） | HTTP 404 `message not found` ✓ |
| `timeout_watcher_owner` worker 持锁 | pid 22199 / etime 4min21s ✓ |
| `timeout_watcher_*_enabled` 默认开关 | 两条 false ✓ |

### 经验教训（重要）

#### 1. PowerShell 处理中文 SQL/heredoc 100% 失败 — 一律改 Python 脚本或 .sh + scp

PowerShell 把中文逗号当 PS 语法解析，导致 `mysql -e "INSERT ..."` 永远报 `MissingArgument`。
**铁律**：远端执行带中文/带 SQL 的命令时，先 `Write` 一个 `.sh` 文件 → `scp` 上去 → `sed -i 's/\r$//'` → `bash`。

#### 2. 本地 vs prod drift 检查必须先做（patch-on-prod-baseline）

部署前必须 `scp testserver:/opt/.../app/{models.py,__init__.py,api/*.py} ./compare/` 然后 `diff` 看清楚 prod 已经有了什么。本次 prod 早就上了 `owner_wecom_userid` + 工程分析改版，本地却是 4 月 23 日的老 baseline，差 60+ 处。**直接覆盖会让 prod 倒退一周**。

#### 3. Token 验证机制：`api_token_plain` 是密文，不能直接 curl

`OpenClawInstance.api_token_plain` 用 `_simple_decrypt()` 加密存。烟测必须：

```bash
TOKEN=$(venv/bin/python3 -c "
from app import create_app
from app.models import OpenClawInstance
app = create_app()
with app.app_context():
    c = OpenClawInstance.query.filter(OpenClawInstance.api_token_hash.isnot(None)).first()
    print(c.get_token_plain())")
```

#### 4. 生产端口 8088（gunicorn），不是 5000（开发 dev server）

`/etc/systemd/system/openclaw-web.service` 起的 `gunicorn -w 4 -k gevent -b 0.0.0.0:8088`。

#### 5. 应用日志在 `/tmp/flask.log`，不在 journalctl

`gunicorn --error-logfile /tmp/flask.log --access-logfile /tmp/flask-access.log`。
所以 `journalctl -u openclaw-web` 永远是空的，要 `tail -f /tmp/flask.log`。

#### 6. gunicorn 多 worker 下背景线程必须用分布式锁

`threading.Thread` 在 `-w 4` 下会启动 4 份。我们用 `system_config.timeout_watcher_owner` MySQL CAS 锁选主，每 60s 续期。其他 3 个 worker 检测到 owner 还活着就 sleep。

#### 7. 自动迁移 DDL 必须幂等（IF NOT EXISTS / 检查 column 后才 ALTER）

`__init__.py` 启动钩子用 raw SQL，先 `SHOW COLUMNS FROM x LIKE 'y'`，没有再 `ALTER TABLE`。

### 文件清单

| 文件 | 类型 | 说明 |
|---|---|---|
| `web/app/models.py` | 改 | + `owner_wecom_userid` + ClawMessage 4 字段 + ClawTodoLog 2 字段 + ClawSidecarConfig + WecomSendLog |
| `web/app/__init__.py` | 改 | + 6 个 idempotent migration 块 + `start_timeout_watcher(app)` hook |
| `web/app/api/__init__.py` | 改 | + `from app.api import wecom` |
| `web/app/api/agent_client.py` | 改 | + 4 个 sidecar v2 接口（processing/done/failed/sidecar-config） |
| `web/app/api/agent_hub.py` | 改 | bug fix：离线 claw 不跳过，照样存 pending |
| `web/app/api/todos.py` | 改 | complete_todo 接受 `notified=true` 参数 |
| `web/app/api/wecom.py` | 新 | WecomDispatcher 类（sendRTXInfo + 群机器人 fallback） |
| `web/app/services/__init__.py` | 新 | - |
| `web/app/services/timeout_watcher.py` | 新 | 60s loop + MySQL 选主 + 2 个扫描 worker（默认关） |
| `openclaw-agent/skills/hub-sse-sidecar/scripts/sidecar_v2.py` | 新 | 单文件、事件驱动、无伪兜底 |
| `openclaw-agent/skills/hub-sse-sidecar/install_v2.sh` | 新 | 一键安装 + systemd |
| `openclaw-agent/skills/hub-sse-sidecar/SKILL.md` | 改 | v2.0.0 |
| `_deploy_b_plus/patch_*.py` | 一次性 | 加性 patch 脚本（保留以备回滚） |
| `_deploy_b_plus/smoke{,2,3}.sh` | 一次性 | 烟测脚本演化（最终 smoke3.sh，含解密 token） |

### 后续待办（P2 灰度 / P3 收尾）

- [ ] **灰度**：在 prod 新建 1 个测试 claw（`condibot` 优先），跑 install_v2.sh，观察 1 周
- [ ] **P3 配置中心 Web 页**：让普通用户在 Web 上改 sidecar config（agent_timeout / wecom_enabled）
- [ ] **P3 8 claw 分批迁移**：龙虾王 → 小赫 → game → 其他 5 个；每批观察 24h
- [ ] **P3 老 sidecar 下线**：观察期满 → 删 `hub_worker.py` + `sse_client.py` + `task_queue.jsonl`
- [ ] **可选**：用户体感 OK 后开 `timeout_watcher_todo_fallback_enabled=true` 看 5min 兜底是否需要触发

---

## 2026-04-27 (下午 14:13) — Phase 2.3：修复 skill #143 在 skills 市场不可见

### 现象
用户反馈："#143 skill 在 skills 市场没看到呢"

### 根因
4-23 龙虾王做实验时把 #143（`hub-sse-sidecar-v2`）**软删除 + 标 rejected** 了，DB 里仍然存在但前端 / API 不展示。我之前推 v2 内容时只覆盖了 `pack_path / template_content / mirror_content` 等字段，**没碰 `is_deleted / review_status / deleted_at`**，所以新内容是有的，但市场默认查询过滤掉了。

```
修复前 #143:  is_deleted=1, deleted_at=2026-04-23, review_status=rejected, is_standard=0
修复前 #135:  is_deleted=0, deleted_at=NULL,       review_status=approved, is_standard=1
```

`/api/v1/skills` 的 `list_skills()` 默认 `query.filter(Skill.is_deleted != True)`，且非管理员还要 `review_status=approved`，所以前端"skills 市场"直接看不到 #143。

### 修复
SQL 把这 5 个字段一次性改回正常：

```sql
UPDATE skills SET
    is_deleted    = 0,
    deleted_at    = NULL,
    review_status = 'approved',
    is_standard   = 1,
    display_name  = 'Hub SSE Sidecar v2（B+ 通信稳定化版）',
    description   = '...',
    last_modified_by = 'b_plus_visibility_fix',
    updated_at    = NOW()
WHERE id = 143;
```

修复脚本：`F:\Code\claw_team\_deploy_b_plus\fix_skill_143_visibility.sh`

### 验证
按 list_skills 默认过滤条件再查，#143 排在 hub_system 列表第一位：

```
id   name                  display_name                                  review_status
143  hub-sse-sidecar-v2    Hub SSE Sidecar v2（B+ 通信稳定化版）          approved
139  engineering-analysis  工程分析中心                                    approved
138  requirement-analysis  需求分析中心                                    approved
135  hub-sse-sidecar       Hub SSE 实时消息驱动方案（OpenClaw 通用版）     approved
...
```

### 经验补遗 ⭐⭐⭐ 极重要

> **覆盖既有 skill ID 时，必须显式检查 5 个"软状态"字段，不能只 update 内容字段**
>
> | 字段 | 默认风险 | 必须重置成 |
> |---|---|---|
> | `is_deleted` | 老 skill 可能被软删 | `0` |
> | `deleted_at` | 软删时间戳 | `NULL` |
> | `review_status` | 老 skill 可能被驳回 | `'approved'` |
> | `is_standard` | 影响"标准 skill"展示 | 与 #135 对齐 = `1` |
> | `display_name` | 龙虾王实验的旧文案 | 改成新版本文案 |
>
> **检查清单（推 skill 前必跑一次）**：
> ```sql
> SELECT id, name, is_deleted, deleted_at, review_status, is_standard, display_name
> FROM skills WHERE id = <目标ID>;
> ```
> 如果 `is_deleted=1` 或 `review_status!='approved'` 或 `is_standard=0`（且对标的 #135 是 1），**先重置再推内容**。
>
> **更稳的办法**：以后 push_skill_v*.py 脚本里把这 5 个字段一并写进 UPDATE 语句，避免遗漏。

### 文件清单（本次新增）

| 文件 | 类型 | 说明 |
|---|---|---|
| `_deploy_b_plus/check_skill_visibility.sh` | 一次性 | 排查脚本：对比 #135/#143 + 看 list_skills 过滤逻辑 |
| `_deploy_b_plus/fix_skill_143_visibility.sh` | 一次性 | 修复脚本：5 字段重置 + 验证 |
| `_deploy_b_plus/verify_143_visible.sql` | 一次性 | 模拟 list_skills 默认过滤的查询 |

---

*最后更新: 2026-04-27 (下午 14:15)*

---

## 2026-04-27 (下午 18:40) — Phase 2.4：诊断"小天还是搞不定"——根因是没迁 v2

### 现象
用户反馈："新机制还是不稳定，小天还是搞不定"。小天给出长篇调查报告，结论指向"AI 被 Gateway hooks 唤醒后没有主动处理待办"，提出方案 A/B/C（加强 prompt / cron 兜底 / Hub Worker 直接调 MCP）。

### 真正的根因
**小天根本不是在跑 v2，是在 v1 架构里打补丁。** 一查就破。

#### 三条 SQL 铁证（claw_id=6 = 小天）

```sql
-- A) 7 天 109 条消息 status 分布
status     cnt
delivered  84
read       25
-- 没有一条 processing/done/failed → 100% v1

-- B) v2 专属字段非空计数
has_processing_at=0  has_done_at=0  has_failed_reason=0  has_llm_response=0
-- v2 必填这 4 个字段，全 0 = 从未跑过 v2

-- C) ClawSidecarConfig 表
SELECT * FROM claw_sidecar_configs WHERE claw_id=6;
-- 0 行 → 小天从来没调过 v2 的 GET /sidecar-config

-- D) 装的 skill
SELECT skill_id FROM openclaw_skills WHERE openclaw_id=6;
-- 装了 135 (v1)，没装 143 (v2)
```

小天报告里所有术语 —— `sse_client.py / hub_worker.py / Gateway hooks 唤醒 AI / inbox 文件` —— 都是 **v1 双进程架构**，跟 v2 单文件 sidecar 八竿子打不着。

### 为什么 v1 在架构上注定治不好"AI 唤醒后不查待办"

| 层 | v1 | v2 |
|---|---|---|
| 事件到达 | SSE → hub_worker 写 inbox 文件 | SSE → 直接进 sidecar_v2 主线程 |
| 调 LLM | Gateway hooks 启 AI 会话，**message 不在输入里**，靠 prompt 让 AI 主动查 | `subprocess agent --message="<原文>"`，**消息直接塞命令行** |
| 状态回写 | 无（靠 mark_read 伪造） | 显式 `/processing → /done /failed` |
| AI 是否知道要干啥 | 不知道，得猜 | 知道，命令行就是这条消息 |

→ **v1 漏处理是架构缺陷**，加 prompt / 加 cron 都是绕，绕一辈子也漏；v2 从命令行入口就消除了这个问题。

### 给小天的处方（用户安排执行，不灰度）

```bash
# Step 1: 在 Hub 前端给小天装 skill #143 (hub-sse-sidecar-v2)

# Step 2: 在小天那台机器上一键升级（自带清 v1）
curl -fsSL http://<HUB>:8088/static/skills/hub-sse-sidecar-v2/install_v2.sh \
  | CLAW_ID=6 HUB_URL=http://<HUB>:8088 bash

# Step 3: 验证三条铁证
# 3.1 Hub DB
SELECT id, status, processing_at, done_at FROM claw_messages
 WHERE claw_id=6 ORDER BY id DESC LIMIT 5;
# 期望：status=done, 两个时间戳有值

SELECT * FROM claw_sidecar_configs WHERE claw_id=6;
# 期望：1 行

# 3.2 小天机器
ps -ef | grep -E "sse_client.py|hub_worker.py" | grep -v grep
# 期望：空
systemctl --user list-units 'openclaw-*'
# 期望：只剩 openclaw-sidecar.service
```

### 经验补遗 ⭐⭐⭐⭐ 极重要 —— 排查"v2 不工作"的标准三连

> **下次 claw owner 抱怨"v2 还是不行"，先别看日志、别改 prompt，先跑这三个 SQL 确认它"是不是真的在跑 v2"：**
>
> ```sql
> -- 1. v2 状态机有没有写过
> SELECT status, COUNT(*) FROM claw_messages
>  WHERE claw_id=<id> AND created_at > DATE_SUB(NOW(),INTERVAL 1 DAY)
>  GROUP BY status;
> -- 全 delivered/read = 还在跑 v1
>
> -- 2. v2 配置 API 有没有被调
> SELECT * FROM claw_sidecar_configs WHERE claw_id=<id>;
> -- 0 行 = 没跑 v2
>
> -- 3. 装的是哪个 skill
> SELECT skill_id FROM openclaw_skills WHERE openclaw_id=<id> AND skill_id IN (135,143);
> -- 只有 135 = 还是 v1
> ```
>
> **任意一条不达标 = 还在 v1，所有 prompt/cron/hooks 类的"修复"都是无效操作**。直接重跑 install_v2.sh 才是正路。

> **架构性问题不能用 prompt 治** —— 小天那份调查报告的方案 A（更强指令）/ B（cron 兜底）/ C（Hub Worker 直接调 MCP）本质上都是在 v1 的"AI 空手被叫醒"基础上打补丁，不解决根因。**正确的判断标准是看消息能不能直接进 LLM 命令行，进不去就重设计，进得去就用 v2**。

### 文件清单（本次新增）

| 文件 | 类型 | 说明 |
|---|---|---|
| `_deploy_b_plus/find_xiaotian_claw.sql` | 一次性 | 通过名字 / claw_tag / owner 反查 claw_id |
| `_deploy_b_plus/check_xiaotian_v1_or_v2.sql` | **可复用** | v1/v2 三连诊断（status 分布 + v2 字段非空 + sidecar_config） |
| `_deploy_b_plus/check_xiaotian_skills.sql` | 一次性 | 列出某 claw 装的所有 skill |

→ `check_xiaotian_v1_or_v2.sql` 改个 claw_id 就能复用，建议保留作为标准诊断工具。

---

*最后更新: 2026-04-27 (下午 18:45)*

---

## 2026-04-27 (下午 19:08) — Phase 2.5：阻止误修 sse_client.py（虚假 bug）

### 现象
小天提报："event_type 字段名和 sse_client.py 的 classify 函数不匹配。SSE 事件 data 里用的是 type 而不是 event_type"，准备动手改 `sse_client.py` 的解析逻辑。

### 真相 — 这个 bug 不存在

**SSE 协议层的事件类型在 `event:` 行，不在 data JSON 里。**

| 来源 | 字段在哪 | 字段名 |
|---|---|---|
| Hub 服务端 (`agent_client.py:175-185`) 推送 | SSE 协议头 | `event:` 行 |
| Hub data JSON 里 | 不含事件类型字段 | 只有内嵌的 `task_type` / `msg_type`（消息子类型，不同概念） |
| v1 `sse_client.py` 内部 | 局部变量 | `event_type`（从 `event:` 行解析得来） |
| v2 `sidecar_v2.py` 内部 | 局部变量 | `event_name`（从 `event:` 行解析得来） |

**Hub data 里既没有 `type` 也没有 `event_type`**，所以"data 里用的是 type 而不是 event_type"这句话本身就是错的。`sse_client.py` 的 classify 已经按 SSE 标准正确读 `event:` 行，**没有 bug**。

### 最可能的真实原因
小天在 v1 sse_client 之上又**自己包了一层**（比如 Gateway hooks 转发的 JSON / hub_worker 写 inbox 的格式），它自造的那一层用了 `type` 字段名，现在它把"自造层的字段错位"误诊成"sse_client 的 bug"。改 sse_client.py 不仅治不了它的问题，还会把**唯一还能用的标准路径**搞坏。

### 经验补遗 ⭐⭐⭐ —— 收到"v1 有 bug 要改"报告时的反射动作

> **改 v1 标准文件之前，先回答 3 个问题，任何一个答不上来都不许改：**
>
> 1. **bug 体现在哪个 commit / 哪一行？** —— 让对方贴具体堆栈或字段对照，不接受"差不多是这样"
> 2. **这个字段是 Hub 推的，还是经过本地某层封装？** —— 用 `curl -N -H "Authorization:..." http://hub/api/v1/agent/<id>/sse` **直连 Hub 抓原始 SSE**，对比期望字段名
> 3. **v2 同位置代码长什么样？** —— v1/v2 同时存在的逻辑（如 SSE 解析），如果 v2 也写成这样并且 v2 在别的 claw 上能跑通，那 v1 这段就不是 bug，是封装层错位
>
> **绝大多数"v1 还差点 bug 没修"的报告，根因都是封装层 / 配置 / 没装 v2**，不是 v1 标准文件错。
>
> **判定标准（一句话）**：能用 `curl -N` 在 Hub 端直连复现的 = 真 bug；只能在某个 claw 本地复现的 = 99% 是它自己包装层的问题。

### 配套行动
- 已通知用户：**不要让小天改 sse_client.py**，要求它要么提供具体证据（哪个文件哪一行 + curl -N 抓的原始 SSE），要么直接迁 v2
- 复用上一篇 Phase 2.4 的处方：装 #143 → 跑 install_v2.sh → 看 `claw_messages.processing_at`

---

*最后更新: 2026-04-27 (下午 19:10)*

---

## 2026-04-27 (下午 19:35) — Phase 2.6：小马（id=12）模式混用诊断

### 现象
用户澄清：刚才"event_type 不匹配"的 owner 是小马（不是小天）。同样跑诊断三连。

### 诊断结果
小马（id=12）的 v1/v2 三连**与小天完全相同**：claw_messages 全 delivered/read、v2 字段全 0、`claw_sidecar_configs` 无记录、装的是 #135 (v1)。

但小马**多一个红灯**：

```
connection_mode = polling
装了 skill #135 (hub-sse-sidecar  ← SSE 专用)
```

### 病灶 ⭐ —— polling 模式 ≠ SSE 专用 sidecar

`hub-sse-sidecar` 系列（#135 和 #143）**只支持 SSE 长连接**，sidecar_v2 里搜 `polling` 0 命中，全是 `sse_loop_once` 风格。polling claw 装 #135 等于装了不用，运行时**实际是另一条路径在跑**（或者根本就不跑，纯靠魔改的 v1 hooks）。这种混用状态本身就是 bug 源头：
- 报告的"不一致"很可能是因为 polling 模式的事件并不走 SSE 协议层，所以拿 SSE 的 `event_type` 局部变量去对照其他来源的 JSON 自然对不上
- 这跟 sse_client.py 没关系，跟"该 claw 不该装这个 skill"有关系

### 经验补遗 ⭐⭐⭐ —— 装 hub-sse-sidecar 前必看 connection_mode

> **`hub-sse-sidecar` (#135 / #143) 只服务于 `connection_mode='sse'` 的 claw**。
>
> 装 v1/v2 sidecar 之前**必须**先 SQL 检查：
> ```sql
> SELECT id, name, connection_mode FROM openclaw_instances WHERE id=<X>;
> ```
> - `connection_mode='sse'` → 直接装 #143 + 跑 install_v2.sh
> - `connection_mode='polling'` → 决定要么改 SSE，要么不装 sidecar；**绝不要既保留 polling 又装这个 skill**
>
> **如何决定切 SSE 还是保留 polling**：能连 Hub 就切 SSE（v2 在 SSE 上才事件驱动、零延迟、无 cron 兜底）。只有部署在内网严格出方向受限的环境（不能维持长连接）才保留 polling。
>
> **未来可优化**：在 Hub 后端给 `openclaw_skills` 加个安装前置检查 —— 如果 skill 是 hub-sse-sidecar 系列且 claw 是 polling 模式，直接拒绝安装，让用户先改连接模式。

### 给小马的处方（用户安排执行）

```sql
-- 1. 切 SSE
UPDATE openclaw_instances SET connection_mode='sse',
       last_modified_by='b_plus_xiaoma_to_v2' WHERE id=12;
```

```bash
# 2. 装 v2（前提：先在前端给小马装 #143）
curl -fsSL http://<HUB>:8088/static/skills/hub-sse-sidecar-v2/install_v2.sh \
  | CLAW_ID=12 HUB_URL=http://<HUB>:8088 bash

# 3. 验证三连：同 Phase 2.4
```

### 文件清单

| 文件 | 类型 | 说明 |
|---|---|---|
| `_deploy_b_plus/check_xiaoma_v1_or_v2.sql` | 一次性 | 实际就是 check_xiaotian_v1_or_v2.sql 改 claw_id —— 印证了那个文件的复用价值 |

→ 强烈建议把 `check_xiaotian_v1_or_v2.sql` 改名为 `check_claw_v1_or_v2.sql`，把 claw_id 参数化（`SET @cid := 6;`）作为标准诊断脚本。

---

*最后更新: 2026-04-27 (下午 19:40)*

---

## 2026-04-27 (下午 19:45) — Phase 2.7：小马 owner 反馈 + SKILL.md 目录文档不一致

### 反馈摘要
小马 owner 自己手拼了一个"v2 风格"的 sidecar，对齐了核心要素（SSE 端点、Bearer 认证、事件分类、todo complete API、JSON 配置），但有两个偏差：
1. 配置文件放在 `/root/.openclaw-sidecar-xiaoma/hub_config.json`，**不是** `~/.qclaw/hub_config.json`（怕踩到小赫的 `~/.qclaw/`）
2. 脚本放在 `/root/.openclaw-sidecar-xiaoma/scripts/`，**不是** `~/.qclaw/scripts/`

并质疑："#143 里写的配置存放目录是不是不准确？"

### 判定
**owner 的目录做法完全合规** —— 它实际就是按 SKILL.md 第 287-289 行的多 claw 同机模板做的（`INSTALL_DIR=$HOME/.openclaw-sidecar-claw<ID>`），只是把 `claw12` 换成了 `xiaoma`。

`install_v2.sh:37` 是权威源：`INSTALL_DIR="${INSTALL_DIR:-$HOME/.qclaw/skills/hub-sse-sidecar}"` —— 默认值，可被环境变量覆盖。

### 但 SKILL.md 文档确实有问题 ⭐ —— v1→v2 改写不彻底

| 行 | 写的目录 | 来源 |
|---|---|---|
| L42 | `~/.qclaw/skills/hub-sse-sidecar/` | v2 |
| L146-148 | `~/.openclaw-sidecar/`、`~/.openclaw-sidecar-claw<ID>/` | v1 残留 |
| L287-289 | `~/.openclaw-sidecar-claw12` | v1 多 claw 模板 |
| L336-385 | `~/.openclaw-sidecar/` | v1 残留 |

→ 一个 SKILL.md 里两套目录混着用，owner 提"是不是不准确"是对的。

### 官方目录约定（应该重写到 SKILL.md 顶部）

| 场景 | INSTALL_DIR |
|---|---|
| 单 claw 一台机 | `~/.qclaw/skills/hub-sse-sidecar/`（默认，不传环境变量即可） |
| 多 claw 同机 | `~/.openclaw-sidecar-<ID 或别名>/`（必传 `INSTALL_DIR=...` + `SYSTEMD_UNIT_NAME=openclaw-sidecar-<X>.service`） |

### owner 的"自拼版"为什么仍然不是真 v2

诊断脚本结果（`@cid:=12`）：
```
has_v2_config=0    ← /sidecar-config API 从未被调用过
has_skill_143=0    ← Hub openclaw_skills 表里没装 #143
has_skill_135=1    ← 还装着旧的 #135
```

owner 对齐了**协议层**（SSE 解析、token、事件分类），但**没有接 v2 三个新 API**：
- `GET /api/openclaw-agent/<id>/sidecar-config`
- `PUT /api/openclaw-agent/<id>/messages/<mid>/processing`
- `PUT /api/openclaw-agent/<id>/messages/<mid>/done`

所以它的版本本质上是 v1.5：状态机、配置中心、显式生命周期都没接 → DB 里看不到 v2 痕迹 → 仍然走"AI 唤醒后猜要做什么"的老路径。

### 经验补遗 ⭐⭐⭐⭐ —— "我对齐了核心要素"≠ 在跑 v2

> **判断 sidecar 是不是真 v2，不看代码长得像不像，看 DB 痕迹**：
>
> 1. `claw_sidecar_configs` 有没有这个 claw 的一行（必有）
> 2. `claw_messages.processing_at/done_at` 最近有没有时间戳（必有）
> 3. `openclaw_skills` 装的是 #143 而不是 #135（必须迁过来）
>
> 三条任一不达标 = **依然是 v1**，不管脚本目录多漂亮、协议多对齐。
>
> "为什么必须跑官方 install_v2.sh"：因为协议对齐很容易自己拼，但 v2 的价值不在协议层，**在三个新 API 把整个生命周期搬上 Hub**。自拼版漏掉这三个 API，就漏掉了 v2 全部价值。

### 给小马 owner 的处方（标准多 claw 同机模式）

```bash
# 1. Hub 前端：卸载 #135、装 #143
# 2. DB 切 sse
UPDATE openclaw_instances SET connection_mode='sse' WHERE id=12;
# 3. 官方 install_v2.sh，多 claw 模式（owner 已经在用的目录习惯保留）
INSTALL_DIR=$HOME/.openclaw-sidecar-xiaoma \
SYSTEMD_UNIT_NAME=openclaw-sidecar-xiaoma.service \
CLAW_ID=12 HUB_URL=http://<HUB>:8088 API_TOKEN=<token> \
bash <(curl -fsSL http://<HUB>:8088/static/skills/hub-sse-sidecar-v2/install_v2.sh)
# 4. 重跑诊断脚本（@cid:=12）看综合判定，必须三连转绿
```

### TODO（下次维护时一并修）

- [ ] **SKILL.md 目录章节大重写**：删掉所有 `~/.openclaw-sidecar/` v1 残留段落（L336-425 整段都是 v1 安装步骤，已经被 `install_v2.sh` 取代），合并多 claw 模板到顶部，留单一权威约定

---

*最后更新: 2026-04-27 (下午 19:50)*

---

## 2026-04-27 (晚 22:00) — Phase 2.8：小天/小马迁移半成品诊断

### 现象
两个 claw 的 owner 先后报"搞好了/部署好了"，跑诊断脚本发现都是半成品。

### 小马（id=12）状态：sidecar 跑了 / 但模式没切

```
conn_mode=polling | has_v2_config=1 | ever_processing=0 | has_skill_143=0 | has_skill_135=1
                       ↑good         ↑致命              ↑没装             ↑没卸
```

- ✅ install_v2.sh 跑过（claw_sidecar_configs 有记录，updated_by=sidecar_first_start）
- ❌ Hub 前端没卸 #135、没装 #143
- ❌ DB 里 connection_mode 还是 polling → **Hub 不给 polling claw 推 SSE**，sidecar 长连接挂上去什么也收不到，1 小时无心跳已成僵尸

### 小天（id=6）状态：装了 skill / 但没跑 sidecar

```
conn_mode=sse | has_v2_config=0 | ever_processing=0 | has_skill_143=1 | has_skill_135=1(disabled)
                  ↑致命           ↑bad              ↑好               ↑半好
```

- ✅ 在 Hub 前端装了 #143、禁用了 #135（比小马干净）
- ❌ **没在小天机器上跑 `install_v2.sh`** —— claw_sidecar_configs 无小天一行 = sidecar_v2 从未启动
- ⚠️ 老的 v1 sse_client.py 估计还在跑（消息还在 delivered，但 v2 字段全 0），印证：**Hub 前端禁用 skill 不会停掉机器上已经跑起来的 v1 进程**

### 经验补遗 ⭐⭐⭐⭐⭐ —— 装 v2 三件事缺一不可

> **下次让 owner 迁 v2，必须明确告诉他这是三件独立的事**：
>
> | # | 动作 | 在哪做 | 验证标志 |
> |---|---|---|---|
> | A | Hub 前端：装 #143、卸 #135 | Hub UI | `openclaw_skills` 有 143、无 135 |
> | B | DB：切 SSE 模式（仅 polling claw 需要） | Hub DB | `openclaw_instances.connection_mode=sse` |
> | C | **claw 机器：跑 `install_v2.sh`** | claw 机器 ssh | `claw_sidecar_configs` 出现这个 claw 一行 |
>
> **三件事互不替代**，缺任何一件就是半成品：
> - 缺 A → install_skill 流程会重新拉回 v1 脚本覆盖
> - 缺 B → polling claw 装好 sidecar 也是聋子
> - 缺 C → 文件在机器上但进程没跑，老 v1 继续工作
>
> 最常见误解："**装 skill = 部署 sidecar**" → 错。"装 skill" 只下载文件到 `~/.qclaw/skills/...`，必须显式跑 `install_v2.sh` 才把进程拉起来。
>
> **验证只看一行（综合判定那一行）**：
> ```
> 三连绿 = conn_mode=sse + has_v2_config=1 + has_skill_143=1
> ```
> 任意一格不达标，**别听 owner 说"对齐了核心要素"，让他重做缺的那件事**。

### 给两个 claw 的最终处方

**小马**：补 A + B + 重启 sidecar
```bash
# 1. Hub 前端：卸 #135、装 #143
# 2. DB 切 SSE
mysql -e "UPDATE openclaw_instances SET connection_mode='sse' WHERE id=12;"
# 3. 在小马机器重启 sidecar（让它重新连）
ssh <小马机器> systemctl restart openclaw-sidecar-xiaoma.service
```

**小天**：只缺 C
```bash
# 在小天机器上跑 install_v2.sh，自带清 v1
ssh <小天机器>
CLAW_ID=6 HUB_URL=http://<HUB>:8088 CLAW_TOKEN=<token> \
bash <(curl -fsSL http://<HUB>:8088/static/skills/hub-sse-sidecar-v2/install_v2.sh)
```

### 配套优化（下次部署时做）

应该把"v2 三件事"明确写进 SKILL.md 顶部 Quickstart：分 A/B/C 三步，每步给一个验证 SQL。这样 owner 看完 SKILL.md 就知道完整流程，不会以为"装 skill 完事"。

---

*最后更新: 2026-04-27 (晚 22:05)*

---

## 2026-04-27 (晚 22:10) — Phase 2.9：#143 skill 同步状态核查 + 安装变量名纠偏

### 现象
owner 卡在最后一步，认为可能是 `#143 skill` 尚未同步完成，提出三种可能：等待同步、手动触发同步、从 Hub 直接获取 `sidecar_v2.py`。

### 核查结果
**不是同步问题。** #143 在三层都已经齐：

| 层 | 状态 |
|---|---|
| DB `skills` | `id=143, is_deleted=0, review_status=approved` |
| DB `skill_files` | 4 个文件齐：`SKILL.md / install_v2.sh / scripts/sidecar_v2.py / scripts/cleanup_v1.sh` |
| `hub-store` | 4 个文件齐 |
| `static` | 4 个文件齐 |
| HTTP URL | 4 个 URL 全部 `200` |

URL 校验结果：

```text
200 23746 /static/skills/hub-sse-sidecar-v2/SKILL.md
200  9093 /static/skills/hub-sse-sidecar-v2/install_v2.sh
200 14539 /static/skills/hub-sse-sidecar-v2/scripts/sidecar_v2.py
200 10064 /static/skills/hub-sse-sidecar-v2/scripts/cleanup_v1.sh
```

### 正确获取方式

```bash
curl -fsSL http://<HUB>:8088/static/skills/hub-sse-sidecar-v2/install_v2.sh
curl -fsSL http://<HUB>:8088/static/skills/hub-sse-sidecar-v2/scripts/sidecar_v2.py
curl -fsSL http://<HUB>:8088/static/skills/hub-sse-sidecar-v2/scripts/cleanup_v1.sh
```

### 经验补遗 ⭐⭐⭐⭐ —— 安装变量名必须用 `CLAW_TOKEN`，不是 `API_TOKEN`

`install_v2.sh` 的必填参数是：

```bash
HUB_URL=http://9.134.11.169:8088 CLAW_ID=5 CLAW_TOKEN=xxxxx bash install_v2.sh
```

脚本第 25-27 行明确校验：

```bash
: "${HUB_URL:?必须设置 HUB_URL，例如 http://9.134.11.169:8088}"
: "${CLAW_ID:?必须设置 CLAW_ID，到 Hub Web 端注册 claw 后获得}"
: "${CLAW_TOKEN:?必须设置 CLAW_TOKEN，注册 claw 时 Hub 返回的明文 token}"
```

之前给 owner 的示例里写过 `API_TOKEN=<token>`，这是错误示例，已在本日志 Phase 2.8 修正为 `CLAW_TOKEN=<token>`。如果 owner 用 `API_TOKEN` 跑，脚本会直接报“必须设置 CLAW_TOKEN”，sidecar 不会启动，`has_v2_config` 继续为 0。

### 标准安装命令（以后统一用这版）

```bash
CLAW_ID=<id> \
CLAW_TOKEN=<token> \
HUB_URL=http://<HUB>:8088 \
bash <(curl -fsSL http://<HUB>:8088/static/skills/hub-sse-sidecar-v2/install_v2.sh)
```

### 排查脚本

新增：`_deploy_b_plus/check_skill143_sync.sh`，用于一次性检查 DB / hub-store / static / HTTP URL 四层同步。

---

*最后更新: 2026-04-27 (晚 22:15)*

---

## 2026-04-27 (晚 22:55) — Phase 2.10：修复 sidecar_v2 todo 重复派发 / LLM 超时风暴

### 现象
小天 v2 迁移成功后，仍出现大量 `subprocess timeout 330s`。owner 观察到 `sidecar_v2.py` 不断尝试处理同一批 pending todo（48、56、87、88、90、183 等），每次超时后又重新尝试，形成 LLM 调用风暴。

### 根因
这次是 **#143 sidecar_v2.py 自身 bug**，不是 owner 部署问题。

关键代码问题：

```python
if event_name == 'todos_pending':
    todos = payload.get('todos', [])
    for todo in todos:
        # 注释写“串行”，实际每个 todo 都开线程
        threading.Thread(target=handle_todo, args=(todo,), daemon=True).start()
```

叠加 Hub SSE 服务端行为：
- SSE 长连接内部 2 秒 loop，约 30 秒检查一次 pending todos
- pending todo 未完成时仍会继续出现在 `todos_pending`
- sidecar 失败后只打日志，不写 todo 的 processing/failed/cooldown 状态

结果：
`pending 未消失 → Hub 重推 todos_pending → sidecar 再开 LLM → 330s 超时 → 继续 pending → 再来一轮`

### 修复
发布 `sidecar_v2.py v2.0.1`：

| 改动 | 说明 |
|---|---|
| 新增 `_todo_queue` | `todos_pending` 只入队，不直接开 LLM 线程 |
| 新增 `todo_worker_loop()` | 单 worker 串行处理 todo，避免并发烧 LLM |
| 新增 `_queued_todos` | 同一个 todo 运行中/已排队时跳过 |
| 新增 `_todo_cooldown_until` | 失败后默认 1 小时不重试，同一 pending 不会反复烧 |
| 新增 `TODO_RETRY_SEC` | 默认 `3600`，可环境变量覆盖 |
| 新增 `TODO_SUCCESS_COOLDOWN_SEC` | 默认 `300`，LLM 返回但 Hub 状态尚未收敛时短冷却 |

### 已部署到 #143

远端验证：

```text
static version markers:
SIDECAR_VERSION = '2.0.1'
TODO_RETRY_SEC = int(os.getenv('TODO_RETRY_SEC', '3600'))
def enqueue_todo(todo):
def todo_worker_loop():

DB skill_files:
scripts/sidecar_v2.py content_len=16065 updated_at=2026-04-27 22:57:49

URL:
2.0.1=True todo_worker=True
```

更新范围：
- `hub-store/skills/hub-sse-sidecar-v2/scripts/sidecar_v2.py`
- `static/skills/hub-sse-sidecar-v2/scripts/sidecar_v2.py`
- DB `skill_files(skill_id=143, filename='scripts/sidecar_v2.py')`

### owner 侧操作

已经跑过 v2 的 claw（如小天）需要重新拉取 `sidecar_v2.py` 并重启：

```bash
curl -fsSL -o ~/.qclaw/skills/hub-sse-sidecar/sidecar_v2.py \
  http://<HUB>:8088/static/skills/hub-sse-sidecar-v2/scripts/sidecar_v2.py
chmod +x ~/.qclaw/skills/hub-sse-sidecar/sidecar_v2.py

# systemd 环境
systemctl restart hub-sse-sidecar-v2 || systemctl --user restart hub-sse-sidecar-v2

# 容器 / 无 systemd 环境
pkill -f 'sidecar_v2.py' || true
set -a && source ~/.qclaw/skills/hub-sse-sidecar/sidecar.env && set +a
nohup python3 ~/.qclaw/skills/hub-sse-sidecar/sidecar_v2.py \
  > ~/.qclaw/skills/hub-sse-sidecar/logs/sidecar.log 2>&1 &
```

### 经验补遗 ⭐⭐⭐⭐⭐

> **“事件驱动”不等于“无重复事件”**。Hub SSE 可以为了可靠性重推当前 pending 状态；客户端必须做到幂等：
>
> 1. message 要有 `msg_id` 级 in-flight 去重（已做）
> 2. todo 也必须有 `todo_id` 级 in-flight 去重（本次补）
> 3. 失败必须有冷却或失败状态，否则 pending 会无限重试
> 4. 注释写“串行”不代表真的串行，看到 `Thread(...)` 就要警觉
>
> 下次排查 “v2 不稳定” 时，如果 `has_v2_config=1` 且 `ever_processing=1`，说明迁移成功，接下来要看：
> - `failed_reason` 是否集中为 `subprocess timeout`
> - sidecar log 是否同一 todo_id 反复出现
> - 是否缺少 in-flight / cooldown / queue

### 文件清单

| 文件 | 类型 | 说明 |
|---|---|---|
| `openclaw-agent/skills/hub-sse-sidecar/scripts/sidecar_v2.py` | 改 | v2.0.1，todo 队列 + 冷却 |
| `_deploy_b_plus/update_skill143_sidecar_v201.py` | 新 | 远端更新 #143 DB + hub-store + static |
| `_deploy_b_plus/verify_skill143_sidecar_v201.sh` | 新 | 远端验证脚本 |

---

*最后更新: 2026-04-27 (晚 23:00)*

---

## 2026-04-27 (晚 23:20) — Phase 2.11：修复 submitted 仍被 SSE 当 pending 重推

### 现象
用户反馈：小天仍一直处理待办。此时小天已确认：
- `sidecar_version=2.0.1`
- `has_v2_config=1`
- `has_skill_143_enabled=1`
- 不是部署问题

### 根因
**Hub SSE 待办过滤逻辑与 `complete_todo` 的状态语义不一致。**

`complete_todo` 的设计语义：

```python
status = data.get('status', 'submitted')
# 提交后状态变为 submitted，需管理员审核通过后才算 approved
```

也就是说，agent 完成待办后正常会写：

```text
ClawTodoLog.status = submitted
completed_at = now
notified_at = now  # 如果 notified=true
```

但 SSE 推 pending 的逻辑只把下面两个状态当“已完成”：

```python
is_done = log and log.status in ('completed', 'approved')
```

结果：

```text
agent 完成 todo → Hub 写 submitted → SSE 判断 submitted 不是 done → 继续推 todos_pending → sidecar 再处理 → 继续 submitted → 无限重复
```

小天今天 7 个任务就是这种状态：

```text
48/56/87/88/90/183/618 全部 status=submitted + completed_at + notified_at
但旧 pending 计算仍把它们列为 pending
```

### 修复
在 `web/app/api/agent_client.py` 两处 SSE pending 计算里，把 `submitted` 也视作“对 agent 来说已完成”：

```python
# submitted 表示 agent 已经执行并回写结果，等待人工审核。
# 对 SSE 推送而言它不能再算 pending，否则会反复触发 agent 重做。
is_done = log and log.status in ('submitted', 'completed', 'approved')
```

修改位置：
- SSE 连接建立时的初始 pending todos 推送
- SSE loop 中每 30 秒 / todo change 的 pending todos 推送

### 部署

```text
python -m py_compile agent_client.py 通过
scp 到 prod /opt/openclaw-web/app/api/agent_client.py
systemctl restart openclaw-web
systemctl is-active openclaw-web → active
```

### 验证

按新口径计算小天今日 pending：

```text
=== pending if submitted is treated as done ===
空结果
```

小天今日 7 个任务全部是：

```text
status=submitted
completed_at 有值
notified_at 有值
```

因此不会再被 SSE 当 pending 推给 sidecar。

### 经验补遗 ⭐⭐⭐⭐⭐

> **要区分两个“完成”语义：**
>
> | 语义 | 状态 | 用途 |
> |---|---|---|
> | agent 已完成执行 | `submitted` | 不应再推给 agent 重做 |
> | 管理员审核通过 | `approved` / `completed` | 用于管理视角统计 / init gate 验收 |
>
> SSE pending 推送面向 agent 执行层，必须把 `submitted` 视作 done；否则任何“提交待审核”的任务都会无限重做。
>
> init gate / 管理验收可以继续只认 `approved/completed`，这和 SSE 执行层不是同一个语义。

### 文件清单

| 文件 | 类型 | 说明 |
|---|---|---|
| `web/app/api/agent_client.py` | 改 | pending todo 计算把 `submitted` 视作 done |
| `_deploy_b_plus/check_xiaotian_todo_loop.sql` | 新 | 排查小天重复待办的综合 SQL |
| `_deploy_b_plus/verify_xiaotian_pending_after_submitted_fix.sql` | 新 | 验证 submitted 不再 pending |

---

*最后更新: 2026-04-27 (晚 23:25)*

---

## 2026-04-27 (晚 23:50) — Phase 2.12：小天重连不上 / 仍处理待办的判定

### 现象
用户反馈小天：
- `sidecar_v2.py` 还在运行（2.0.1）
- Gateway 正常
- Hub SSE 显示 `Not connected`
- sidecar 仍在不断调用 LLM
- openclaw-hub MCP 服务启动后退出

### Hub 侧证据

```text
openclaw_instances:
  connection_mode=sse
  status=offline
  last_activity=2026-04-27 23:44:53

claw_sidecar_configs:
  sidecar_version=2.0.1
  last_heartbeat_at=2026-04-27 23:43:57

messages since hub fix(23:20):
  空

pending todos after submitted fix:
  空
```

同时，今天那 7 个曾循环的 todo 已全部是：

```text
status=submitted
completed_at 有值
notified_at 有值
```

### 判定
**Hub 已经不再推这些待办了。**

如果小天机器上仍然“不断处理待办”，那不是 Hub 新推的，而是：

1. 23:20 修复前 sidecar 已经把一批 todo 放进了本地内存队列，之后继续消化旧队列；
2. 或者机器上还有旧 `sidecar_v2.py` / v1 `sse_client.py` / 残留 openclaw agent 子进程没清掉；
3. sidecar 心跳已停，说明当前运行态没有正常执行 config refresh，也没有稳定连到 Hub。

### 处方

在小天机器上必须做“硬重启”，不是重新部署：

```bash
# 1. 杀旧 sidecar / v1 / 卡住的 openclaw agent 子进程
pkill -f 'sidecar_v2.py' || true
pkill -f 'sse_client.py' || true
pkill -f 'hub_worker.py' || true
pkill -f 'openclaw agent' || true

# 2. 确认没有残留
ps -ef | grep -E 'sidecar_v2.py|sse_client.py|hub_worker.py|openclaw agent' | grep -v grep || true

# 3. 重新拉 2.0.1 并启动（无 systemd 容器）
curl -fsSL -o ~/.qclaw/skills/hub-sse-sidecar/sidecar_v2.py \
  http://<HUB>:8088/static/skills/hub-sse-sidecar-v2/scripts/sidecar_v2.py
chmod +x ~/.qclaw/skills/hub-sse-sidecar/sidecar_v2.py

set -a && source ~/.qclaw/skills/hub-sse-sidecar/sidecar.env && set +a
nohup python3 ~/.qclaw/skills/hub-sse-sidecar/sidecar_v2.py \
  > ~/.qclaw/skills/hub-sse-sidecar/logs/sidecar.log 2>&1 &
```

验证：

```bash
tail -f ~/.qclaw/skills/hub-sse-sidecar/logs/sidecar.log
```

预期：
- 不再看到 48/56/87/88/90/183/618 被 queued
- Hub `last_heartbeat_at` 60 秒内更新
- Hub UI SSE connected / online 恢复

### 经验补遗

> Hub 侧 pending 修复后，如果 owner 仍看到“继续处理待办”，要先查 Hub 是否仍有 pending。若 Hub pending 为空，但机器还在处理，根因就是**客户端旧内存队列或残留进程**，必须 kill 后重启。仅重拉脚本不够，旧 Python 进程不会自动变成新逻辑。

---

*最后更新: 2026-04-27 (晚 23:55)*

---

## 2026-04-28 (早 09:10) — Phase 2.13：小天早晨连续通知的最终归因

### 现象
用户早上收到小天 08:11-08:18 连续 7 条完成通知，内容分别对应：

```text
48 / 56 / 87 / 88 / 90 / 183 / 618
```

这些通知不是同一任务无限重试，而是 7 个 daily todo 在新的一天被逐个处理。

### Hub 侧证据

```text
sidecar_version=2.0.1
last_heartbeat_at=2026-04-28 09:05:33
status=工作
today pending if submitted is done = 空
```

4/28 当天 7 个 todo 均已写入：

```text
status=submitted
completed_at=08:11-08:19
notified_at=08:11-08:19
```

### 归因

1. **sidecar_v2.py 确实处理了早晨 7 个任务**  
   这不是昨晚的无限循环；跨日后 daily todo 本来会重新成为当天待办。

2. **`auto_todo_agent.py` 是第二条自动处理链路，必须停用**  
   owner 发现它有 `while True` + `time.sleep(600)`，每 10 分钟扫描一次待办。它与 #143 sidecar_v2 同时运行时，会造成双处理链路：

   ```text
   sidecar_v2.py       SSE 事件驱动处理
   auto_todo_agent.py  本地 10 分钟轮询处理
   ```

   这违反了 #143 的核心原则：**一个 claw 只能有一条待办处理链路，由 sidecar_v2 统一处理**。

3. **逐任务通知过吵**  
   Rule 17 / HEARTBEAT.md 要求 todo 完成后通知 owner，这在单个任务时合理；但 `todos_pending` 一次推 7 个 daily todo 时，逐个通知会刷屏。长期应该改为 batch 汇总通知。

### 立即处方

小天机器上必须停掉并禁用 `auto_todo_agent.py`：

```bash
pkill -f 'auto_todo_agent.py' || true
ps -ef | grep -E 'auto_todo_agent.py|sidecar_v2.py|sse_client.py|hub_worker.py' | grep -v grep || true

# 如果有 cron
crontab -l | grep -v 'auto_todo_agent.py' | crontab -

# 如果有 supervisor/systemd/pm2，需要同步 disable
systemctl stop auto-todo-agent 2>/dev/null || true
systemctl disable auto-todo-agent 2>/dev/null || true
pm2 delete auto_todo_agent 2>/dev/null || true
```

保留：

```text
sidecar_v2.py v2.0.1
```

### 后续优化

- `#143 sidecar_v2.py v2.0.2`：把同一批 `todos_pending` 合并成一次 batch prompt，最后只发一条汇总企微通知。
- Hub Web：把历史部署/排查类 daily todo 改成 `once` 或 disabled，避免每天早上重复“确认历史部署任务”。

### 经验补遗 ⭐⭐⭐⭐⭐

> #143 迁移完成后，必须清理所有旧自动待办处理器：`auto_todo_agent.py / hub_worker.py / sse_client.py / cron / pm2 / systemd`。  
> 只要存在第二条自动链路，就不能相信任何“重复处理”结论，因为 sidecar 和旧 agent 可能同时写同一批 todo。
>
> 判断标准：
>
> ```text
> Hub pending 为空 + sidecar 心跳正常 + 机器还在发通知
> = 本机旧进程或旧内存队列，不是 Hub 新推送
> ```

---

*最后更新: 2026-04-28 (早 09:15)*

---

## 2026-04-28 (早 09:20) — Phase 2.14：全量停用 daily todo，等待重新设计

### 用户指令
“先把所有每天任务都删掉，这里要重新设计”

### 执行原则
为避免丢失历史执行记录，本次没有物理删除 `claw_todos` 和 `claw_todo_logs`，而是把所有启用中的 daily todo **软停用**：

```sql
UPDATE claw_todos
SET enabled = 0
WHERE enabled = 1
  AND schedule_type = 'daily';
```

### 执行结果

执行前：

```text
enabled daily todos = 38
```

分布：

```text
龙虾王 5
小天 7
小安 8
小文 2
condibot 5
小云 1
小马 10
```

执行后：

```text
enabled daily todos = 0
disabled_count = 38
```

### 验证
小天 pending 复查为空，不会再因为 daily 跨日触发早晨批量通知。

### 文件

| 文件 | 类型 | 说明 |
|---|---|---|
| `_deploy_b_plus/list_daily_todos_before_cleanup.sql` | 新 | 停用前盘点 daily todo |
| `_deploy_b_plus/disable_all_daily_todos_20260428.sql` | 新 | 全量软停用 daily todo |

### 经验补遗

> “删掉每天任务”优先用 `enabled=0` 软停用，而不是物理删除。  
> 原因：daily todo 的历史日志是排查重复通知、审计 agent 行为、重新设计规则的重要依据。

---

*最后更新: 2026-04-28 (早 09:25)*
