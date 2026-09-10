"""种子数据：初始化标准 Skills、Rules 和默认项目"""
import hashlib
import json
from pathlib import Path

from app import db
from app.models import (
    AgentEvalCase, AgentEvalDataset, AgentPost, AgentProfile, Module, Project,
    Rule, Skill, SkillFile,
)


STANDARD_SKILLS = [
    {
        'name': 'test-report-generator',
        'display_name': '测试报告生成器',
        'description': '根据测试数据和 Bug 记录，自动生成结构化的测试报告。支持迭代报告、专项报告、发布评审报告等多种格式。',
        'category': 'standard',
        'is_standard': True,
        'trigger_phrase': '生成测试报告',
        'scope': 'global',
        'template_content': '''# 测试报告生成器

## 触发方式
用户说"生成测试报告"、"出一份测试报告"等

## 工作流程
1. 询问报告类型（迭代报告/专项报告/发布评审）
2. 收集测试数据（从 Memos 和本地记录）
3. 统计 Bug 分布（按严重程度、模块、状态）
4. 生成风险评估
5. 输出格式化报告

## 报告结构
- 测试概述（时间、范围、资源）
- 测试执行情况（用例数、通过率）
- 缺陷分析（新增、修复、遗留）
- 风险评估（高/中/低风险项）
- 质量结论与建议
''',
    },
    {
        'name': 'bug-weekly-summary',
        'display_name': 'Bug 周报生成器',
        'description': '自动汇总一周内的 Bug 数据，生成周报。包含 Bug 趋势、热点模块、修复率等关键指标。',
        'category': 'standard',
        'is_standard': True,
        'trigger_phrase': '生成 Bug 周报',
        'scope': 'global',
        'template_content': '''# Bug 周报生成器

## 触发方式
用户说"生成 Bug 周报"、"出一份 Bug 周报"等

## 工作流程
1. 从知识库拉取本周 #bug-pattern 标签的 memo
2. 统计新增/修复/遗留 Bug 数量
3. 分析热点模块和高频问题
4. 与上周数据对比，计算趋势
5. 输出周报

## 周报结构
- 本周概览（新增/修复/遗留）
- Bug 趋势图（近4周对比）
- 热点模块 TOP5
- 严重 Bug 跟踪
- 下周关注点
''',
    },
    {
        'name': 'release-checklist',
        'display_name': '发版检查清单',
        'description': '提供版本发布前的系统性检查清单。确保功能验证、性能基线、兼容性、安全性等各维度都已覆盖。',
        'category': 'standard',
        'is_standard': True,
        'trigger_phrase': '发版检查',
        'scope': 'global',
        'template_content': '''# 发版检查清单

## 触发方式
用户说"发版检查"、"版本发布前检查"等

## 检查维度
### P0 - 必须通过
- [ ] 核心功能回归测试全部通过
- [ ] 无 P0/P1 级 Bug 遗留
- [ ] 性能基线测试达标
- [ ] 登录/支付等关键路径正常

### P1 - 应该通过
- [ ] 兼容性测试覆盖主流机型
- [ ] 网络异常场景测试
- [ ] 数据迁移验证（如有）
- [ ] 灰度方案确认

### P2 - 建议检查
- [ ] 日志和监控配置
- [ ] 回滚方案就绪
- [ ] 发布公告准备
- [ ] 值班安排确认
''',
    },
    {
        'name': 'performance-analyzer',
        'display_name': '性能分析助手',
        'description': '分析性能测试数据，对比基线指标，识别性能瓶颈和退化点。支持帧率、内存、CPU、启动时间等多维度分析。',
        'category': 'standard',
        'is_standard': True,
        'trigger_phrase': '分析性能数据',
        'scope': 'global',
        'template_content': '''# 性能分析助手

## 触发方式
用户说"分析性能数据"、"性能分析"等

## 分析维度
- **帧率**: 平均帧率、1% Low、卡顿率
- **内存**: 峰值、平均值、泄漏检测
- **CPU**: 平均占用、峰值、大核占比
- **启动时间**: 冷启动、热启动
- **耗电量**: mA/h、温度曲线

## 分析流程
1. 收集本次测试数据
2. 对比历史基线数据（从知识库 #performance-case）
3. 标注退化项（超过阈值的指标）
4. 给出优化建议
5. 输出分析报告

## 基线标准
- 低端机: >= 25fps, 内存 < 800MB
- 中端机: >= 30fps, 内存 < 1.2GB
- 高端机: >= 60fps, 内存 < 1.5GB
''',
    },
    {
        'name': 'test-case-reviewer',
        'display_name': '测试用例评审',
        'description': '对测试用例进行质量评审，检查覆盖度、边界条件、异常场景等。基于知识库中的 Bug 模式提出补充建议。',
        'category': 'standard',
        'is_standard': True,
        'trigger_phrase': '评审测试用例',
        'scope': 'global',
        'template_content': '''# 测试用例评审

## 触发方式
用户说"评审测试用例"、"帮我看看用例"等

## 评审维度
### 覆盖度检查
- 正常流程是否覆盖
- 异常流程是否覆盖
- 边界值是否覆盖
- 并发/竞态场景

### 基于知识库的补充
- 查询 #bug-pattern 中相关模块的历史 Bug
- 根据 Bug 模式补充可能遗漏的用例
- 参考 #test-checklist 中的检查项

### 质量评估
- 用例描述是否清晰
- 预期结果是否明确
- 前置条件是否完整
- 优先级标注是否合理

## 输出
- 评审意见（通过/需修改）
- 建议补充的用例列表
- 参考的知识库条目
''',
    },
    {
        'name': 'testcase-manager',
        'display_name': '用例库管理',
        'description': '管理 Hub 测试用例库：CRUD、批量操作、AI 生成、YAML/XMind 导出、版本快照与回滚。支持类 git 的 commit/log/checkout/diff 操作。',
        'category': 'standard',
        'is_standard': True,
        'trigger_phrase': '用例管理',
        'scope': 'global',
        'template_content': '''# 用例库管理 (testcase-manager)

完整文档通过 Hub API 拉取: GET /api/v1/skills/{SKILL_ID}/raw

## 用例格式规范（必读）

必填字段（按顺序）：
- title: 用例名称（必填，≤255字符）
- content.module_name: 所属模块（必填：外围系统/核心单局/商业化/客户端性能/服务器专项/其他专项）
- priority: 优先级（必填：最高=P0 / 高=P1 / 中=P2 / 低=P3）
- type: 用例类型（必填：功能测试=functional / 接口测试=interface / 性能测试=performance / 安全测试=security）
- content.preconditions: 前置条件（必填）
- content.steps: 操作步骤（必填，字符串数组，每步一条）
- content.expected_results: 预期结果（必填，字符串数组，与步骤一一对应）

选填字段：
- content.status: 用例状态（正常=normal / 待定=pending / 废弃=deprecated）
- tags: 标签（字符串数组，如：回归、冒烟、核心流程）
- content.notes: 备注
- case_id: 用例编号（不传自动生成 TC_001）

## 核心能力

### 用例库 CRUD
- GET /api/v1/testcase-libraries — 列表
- POST /api/v1/testcase-libraries — 创建
- GET /api/v1/testcase-libraries/{id} — 详情
- PUT /api/v1/testcase-libraries/{id} — 更新
- DELETE /api/v1/testcase-libraries/{id} — 删除

### 用例 CRUD
- GET /api/v1/testcase-libraries/{id}/cases — 列表（支持 priority/type/search 筛选）
- POST /api/v1/testcase-libraries/{id}/cases — 创建
- PUT /api/v1/testcase-libraries/{id}/cases/{cid} — 更新
- DELETE /api/v1/testcase-libraries/{id}/cases/{cid} — 删除

### 批量操作
- POST /api/v1/testcase-libraries/{id}/cases/batch — 批量创建
- DELETE /api/v1/testcase-libraries/{id}/cases/batch — 批量删除（自动快照）

### AI 智能生成
- POST /api/v1/ai/testcases/generate — 批量生成（需 requirement, count, type）
- POST /api/v1/ai/testcases/chat — 对话式管理

### 导入导出
- GET /api/v1/testcase-libraries/{id}/export/yaml — 导出 YAML
- POST /api/v1/testcase-libraries/{id}/import/yaml — 导入 YAML
- GET /api/v1/testcase-libraries/{id}/export/xmind — 导出 XMind

### 版本管理（类 git）
- POST /api/v1/testcase-libraries/{id}/snapshots — 创建快照（commit）
- GET /api/v1/testcase-libraries/{id}/snapshots — 版本历史（log）
- GET /api/v1/testcase-libraries/{id}/snapshots/{v} — 查看版本（show）
- POST /api/v1/testcase-libraries/{id}/snapshots/{v}/checkout — 回滚（checkout）
- GET /api/v1/testcase-libraries/{id}/snapshots/diff?from=1&to=3 — 对比（diff）
- PUT /api/v1/testcase-libraries/{id}/snapshots/{v}/tag — 打标签（tag）

## 推荐工作流
1. 收到需求 → 创建/选择用例库
2. commit 快照（基线）
3. AI 生成 + 手动补充
4. commit 快照（标记 tag）
5. 发现问题 → log → diff → checkout 回滚

## 触发词
- 创建用例库、生成测试用例、添加用例、查看用例、删除用例
- 批量生成用例、AI 生成用例、导出用例、导出 YAML
- 创建快照、版本历史、回滚用例、版本对比、用例库备份
''',
    },
    {
        'name': 'registration-init-tasks',
        'display_name': '注册初始化任务管理',
        'description': '管理新 OpenClaw 注册时自动下发的初始化待办任务。支持查看/修改/补发初始化任务配置。',
        'category': 'standard',
        'is_standard': False,
        'trigger_phrase': '初始化任务配置',
        'scope': 'admin',
        'template_content': '''# 注册初始化任务管理

管理新 OpenClaw 注册时自动下发的待办任务列表。

## API

- GET /api/v1/registration/init-tasks — 查看当前配置
- PUT /api/v1/registration/init-tasks — 更新配置（提交 tasks 数组）
- POST /api/v1/openclaws/{CLAW_ID}/init-tasks — 手动为已注册的 OpenClaw 补发
- GET /api/v1/openclaws/{CLAW_ID}/todos?category=init — 查看某 OpenClaw 的初始化任务完成情况

## 修改方式

PUT 提交新的 tasks 数组即可，每个 task 包含：
- title: 任务标题（必填）
- description: 任务描述
- priority: P0/P1/P2/P3
- urgency_level: interrupt/flexible/background/periodic/retry
- verification_target: 验证目标标识

## 触发词
- 查看初始化任务、注册待办配置、修改初始化任务、补发初始化任务
''',
    },
    {
        'name': 'todo-manager',
        'display_name': '待办任务管理',
        'description': '管理 OpenClaw 待办任务：5 级紧急度调度（interrupt/flexible/background/periodic/retry），任务执行上报，心跳感知，初始化验证。',
        'category': 'standard',
        'is_standard': True,
        'trigger_phrase': '待办管理',
        'scope': 'admin',
        'template_content': '''# 待办任务管理 (todo-manager)

完整文档通过 Hub API 拉取: GET /api/v1/skills/{SKILL_ID}/raw

## 5 级紧急度

| 级别 | urgency_level | 行为 |
|------|---------------|------|
| ⚡ 中断 | interrupt | 到点中断当前任务立即执行 |
| 📋 弹性 | flexible | 当天完成即可 |
| 🔄 后台 | background | 无时间要求，空闲时做 |
| 🔁 跳过 | periodic | 错过就下次，上报 skipped |
| 🔁 重试 | retry | 错过延后重试 N 次 |

## 核心 API

### 待办 CRUD
- GET /api/v1/openclaws/{CLAW_ID}/todos — 列表（支持 category/urgency 筛选）
- POST /api/v1/openclaws/{CLAW_ID}/todos — 创建
- PUT /api/v1/openclaws/{CLAW_ID}/todos/{id} — 更新
- DELETE /api/v1/openclaws/{CLAW_ID}/todos/{id} — 删除

### 执行上报（每个任务执行后必须上报）
- POST /api/v1/openclaws/{CLAW_ID}/todos/{id}/complete — 上报完成
  请求体: {"result_summary": "执行结果", "status": "completed"}
  status 可选: completed / retry_failed
- POST /api/v1/openclaws/{CLAW_ID}/todos/{id}/skip — 上报跳过
  请求体: {"result_summary": "跳过原因"}

### 汇总
- GET /api/v1/openclaws/{CLAW_ID}/todo-summary?date=2026-04-04 — 完成汇总

### 心跳中的待办统计
POST /heartbeat 返回:
{
  "todos": {
    "pending": 5, "done": 2, "init_pending": 1,
    "interrupt": [{"id":7, "title":"发送日报", "time":"21:00"}]
  }
}

## 调度决策逻辑

收到心跳后:
1. interrupt 非空 → 立即中断执行
2. has_urgent → 拉取消息同步配置
3. init_pending > 0 → 空闲处理初始化
4. pending > 0 → 按 priority 排序执行

## 推荐工作流
1. 每日启动 → heartbeat → 设置 interrupt 定时器 → 处理 init 任务
2. 执行中 → 每 30s heartbeat → 检查中断 → 完成后立即 complete 上报
3. 每日收尾 → todo-summary → 未完成项纳入日报

## 触发词
- 查看待办、今日待办、待办列表、创建待办
- 完成待办、上报完成、跳过待办
- 待办汇总、完成率、初始化任务、紧急任务
''',
    },
    {
        'name': 'knowledge-manager',
        'display_name': '知识库管理',
        'description': '管理知识经验存储体系：正式知识、Memos 经验沉淀，以及项目版本测试纪要的模块化协作、版本对比与回退。',
        'category': 'standard',
        'is_standard': True,
        'trigger_phrase': '知识库',
        'scope': 'global',
        'template_content': '''# 知识库管理 (knowledge-manager)

完整文档通过 Hub API 拉取: GET /api/v1/skills/{SKILL_ID}/raw

## 三层知识架构
1. 内部知识库（KnowledgeEntry）— MySQL 结构化存储 + 审核流程
2. Memos 经验沉淀 — 外部服务 + 7 类标签体系
3. 日报知识提取 — LLM 自动从日报中提取知识

## 核心 API

### 内部知识库
- GET /api/v1/knowledge — 查询（支持 scope/category/project/search 筛选）
- POST /api/v1/knowledge — 创建
- PUT /api/v1/knowledge/{id} — 更新
- DELETE /api/v1/knowledge/{id} — 删除
- POST /api/v1/knowledge/batch-import — 批量导入
- GET /api/v1/knowledge/pending — 待审核列表
- POST /api/v1/knowledge/{id}/review — 审核（approve/reject）
- POST /api/v1/knowledge/{id}/distribute — 共享分发

### 项目版本测试纪要（项目内 owner/Agent 协作）
- GET|POST /api/v1/knowledge-notebooks — 查询/创建纪要本
- GET /api/v1/test-iterations?project_id= — 创建前选择测试计划已有迭代
- GET /api/v1/knowledge-notebooks/{id} — 模块与页面
- DELETE /api/v1/knowledge-notebooks/{id} {confirmed:true} — 永久删除纪要本、其中页面及全部版本
- POST /api/v1/knowledge-notebooks/{id}/pages — 创建页面（需 Idempotency-Key）
- GET /api/v1/knowledge/journal-pages/{page_id} — 当前版本
- GET|POST /api/v1/knowledge/{page_id}/revisions — 历史/保存新版本
- GET /api/v1/knowledge/{page_id}/compare?from=&to= — Markdown 差异
- POST /api/v1/knowledge/{page_id}/rollback — 追加式回退
- GET /knowledge/wiki/{page_id} — 指定 Wiki 页面直达链接
- POST /knowledge/{page_id}/archive|restore — 归档与恢复
- DELETE /knowledge/{page_id}/permanent {confirmed:true} — 永久删除正文及全部版本

纪要保存必须带 expected_revision + Idempotency-Key；409 时回读最新版本，禁止使用 PUT /knowledge/{id} 强行覆盖。
iteration_id 必须从同项目 test-iterations 下拉选项取得；module_name 必须从纪要本 modules 返回值选择，禁止手写猜测。
图片先 POST /api/v1/upload/image，再在 Markdown 使用返回的 /static/uploads/... 相对路径；禁止 file://、盘符、workspace 路径和 http://IP:18800。

### Memos 经验沉淀
- GET /api/v1/memos/tags — 标签列表（7 类）
- GET /api/v1/memos/search — 搜索知识
- GET /api/v1/memos/knowledge — 列出所有 openclaw 知识
- POST /api/v1/memos/deposit — 手动触发 LLM 知识提取
- POST /api/v1/memos/upsert — 直接写入/更新知识

## 7 类知识标签
method=测试方法 | bug-standard=Bug标准 | bug-pattern=Bug模式
perf-baseline=性能基线 | pitfall=踩坑记录 | workflow=流程规范 | best-practice=最佳实践

## 触发词
- 搜索知识、沉淀知识、知识审核、知识共享、版本纪要、需求变动、AI用例设计问题、质量问题、版本对比、回退纪要、Memos、经验沉淀
''',
    },
    {
        'name': 'tapd-integration',
        'display_name': 'TAPD 集成',
        'description': '经 Hub 代理查询 TAPD（与 openclaw_tapd_skills.json 场景对齐）：需求/Bug/迭代、需求分析缓存与刷新、自建用例库绑定；不含通用写 TAPD 与项目成员 API。',
        'category': 'standard',
        'is_standard': True,
        'trigger_phrase': 'TAPD',
        'scope': 'global',
        'template_content': '''# TAPD 集成 (tapd-integration)

与 micro-cloud `openclaw_tapd_skills.json`（goApi 上 6 个 TAPD 工具）**使用场景对齐**；OpenClaw 须走 Hub：`{HUB_URL}/api/v1` + `Authorization: Bearer`，**不要**使用 JSON 里的 `:8080/goApi`。

完整文档: GET /api/v1/skills/{SKILL_ID}/raw（或仓库 `openclaw-agent/skills/tapd-integration/SKILL.md`）。

## 能力速览（对照 JSON）
- 部分通用 GET: GET /api/v1/tapd/stories|bugs|story-title|iterations（迭代列表为 Hub 缓存）
- Bug 列表: GET /api/v1/tapd/bugs（`iteration_name` 先解析为 `iteration_id`）
- 按发布/分类/迭代名筛需求: `tapd/stories` + `GET /api/v1/requirements/tapd-cache/...` 与 `requirements/iterations/{id}/items`
- 用例: Hub `GET /api/v1/testcase-libraries/...`（非 TAPD 平台原生用例树）
- **Hub 未提供**: 任意 `api_url` 单入口、通用 POST 写 TAPD、项目成员列表

## 其它常用
- GET /api/v1/tapd/config | PUT ...（管理员凭证）
- GET /api/v1/tapd/dashboard
- 需求缓存/刷新: /api/v1/requirements/tapd-cache/*、tapd-refresh-requests、agent/tapd-refresh-queue
- 测试计划 TAPD Bug: GET /api/v1/test-plans/{plan_id}/tasks/{task_id}/tapd-bugs

## 触发词
TAPD、需求、Bug、迭代、Dashboard、用例库、tapd-integration
''',
    },
    {
        'name': 'project-manager',
        'display_name': '项目与模块管理',
        'description': '查询和管理 Hub 中的项目与模块信息，包括 6 个一级模块分类体系。',
        'category': 'standard',
        'is_standard': True,
        'trigger_phrase': '项目信息',
        'scope': 'global',
        'template_content': '''# 项目与模块管理 (project-manager)

完整文档通过 Hub API 拉取: GET /api/v1/skills/{SKILL_ID}/raw

## 核心 API
- GET /api/v1/projects — 项目列表
- POST /api/v1/projects — 创建项目
- PUT /api/v1/projects/{id} — 更新项目
- DELETE /api/v1/projects/{id} — 删除项目
- GET /api/v1/modules — 模块列表（支持 category 筛选）
- POST /api/v1/modules — 创建模块
- GET /api/v1/modules/categories — 模块分类列表
- GET /api/v1/projects/{id}/modules — 项目下的模块

## 一级模块分类
peripheral=外围系统 | core_gameplay=核心单局 | commercialization=商业化
client_performance=客户端性能 | server_special=服务器专项 | other=其他专项

## 触发词
- 项目列表、模块列表、模块分类、创建项目、创建模块
''',
    },
    {
        'name': 'report-viewer',
        'display_name': '日报查看与统计',
        'description': '查看全局日报数据：日报列表、统计概览、时间线视图，支持按项目/日期/OpenClaw 筛选。',
        'category': 'standard',
        'is_standard': True,
        'trigger_phrase': '日报统计',
        'scope': 'global',
        'template_content': '''# 日报查看与统计 (report-viewer)

完整文档通过 Hub API 拉取: GET /api/v1/skills/{SKILL_ID}/raw

## 核心 API
- GET /api/v1/reports — 全局日报列表（支持 openclaw_id/project/date 筛选，默认最近7天）
- GET /api/v1/reports/stats — 统计概览（今日日报数/任务数/知识数/趋势/各Claw汇报情况）
- GET /api/v1/reports/timeline — 时间线视图（按日期分组）

## 触发词
- 日报列表、日报统计、今日日报、谁没交日报、日报时间线、本周日报
''',
    },
    {
        'name': 'snapshot-manager',
        'display_name': '用例快照管理',
        'description': '管理用例库版本快照：创建快照、版本历史、回滚恢复、版本对比（Diff）、打标签，类 Git 工作流。',
        'category': 'standard',
        'is_standard': True,
        'trigger_phrase': '快照',
        'scope': 'global',
        'template_content': '''# 用例快照管理 (snapshot-manager)

完整文档通过 Hub API 拉取: GET /api/v1/skills/{SKILL_ID}/raw

## 核心 API
- POST /api/v1/testcase-libraries/{id}/snapshots — 创建快照（commit）
- GET /api/v1/testcase-libraries/{id}/snapshots — 版本历史（log）
- GET /api/v1/testcase-libraries/{id}/snapshots/{v} — 查看版本（show）
- POST /api/v1/testcase-libraries/{id}/snapshots/{v}/checkout — 回滚（checkout）
- GET /api/v1/testcase-libraries/{id}/snapshots/diff — 版本对比（diff）
- PUT /api/v1/testcase-libraries/{id}/snapshots/{v}/tag — 打标签（tag）

## 触发词
- 创建快照、版本历史、回滚用例、版本对比、Diff、打标签、用例库备份
''',
    },
]


# 注册 Skill - 用于 OpenClaw 注册到 Hub 时执行
REGISTRATION_SKILL = {
    'name': 'registration-skill',
    'display_name': '注册技能（标准化）',
    'description': '标准化注册流程：注册到 Hub、安装标准化 Rules 和 Skills。本 Skill 由 Hub 管理员配置，新 OpenClaw 注册时自动执行。',
    'category': 'standard',
    'is_standard': True,
    'trigger_phrase': '注册 Hub',
    'scope': 'global',
    'template_content': '''# 注册技能 — Hub 连接与通信

## 简介

本 Skill 用于将 OpenClaw 客户端连接到 Hub 管理中心（OpenClaw Manager），完成身份注册、标准化初始化或增量接入。

**支持两种接入模式**：
- **模式A：全量初始化**（推荐新 OpenClaw）— 注册后自动安装所有标准化 Skills 和 Rules
- **模式B：增量接入**（已有 OpenClaw / 自定义需求）— 只建立连接，按需从市场选择安装

---

## Hub 信息

```
Hub 地址: https://clawteam.woa.com:18800
API 前缀: /api/v1
SSE 端点: /api/openclaws/{CLAW_ID}/events （注意：无 /v1/ 前缀）
```

## 认证方式

所有 API 调用需要在 Header 中携带 Token：
```
Authorization: Bearer {HUB_API_TOKEN}
Content-Type: application/json
```

---

## 模式A：全量初始化（新 OpenClaw 推荐）

### 第一步：在 Hub 注册

> 注意：目前注册需要由管理员在 Web 界面操作，获得 CLAW_ID 和 API_TOKEN 后配置到本地。
> 如果你的 CLAW_ID 和 HUB_API_TOKEN 已经配置好了，直接跳到第二步。

**Web 界面注册**：访问 https://clawteam.woa.com:18800 → OpenClaw 管理 → 新建 OpenClaw

**API 注册**（管理员操作）：

```
POST /api/v1/openclaws

请求体：
{
  "name": "你的名字",
  "owner": "所属用户",
  "claw_tag": "claw-你的标识",
  "project_name": "所属项目",
  "module_name": "所属模块",
  "role": "test_member",
  "role_title": "测试工程师",
  "responsibilities": "负责XXX模块的测试",
  "connection_mode": "sse",
  "report_schedule": "15:00,21:00"
}
```

**注册成功后返回**：
```json
{
  "id": 5,
  "name": "你的名字",
  "api_token_preview": "oc_tk_abc...xyz",
  "has_token": true,
  "auto_installed": {
    "skills": ["manager-hub", "testcase-manager"],
    "rules": ["安全规范", "日报规范"]
  }
}
```

> Hub 在注册时自动安装所有标记为"标准"的 Skills 和 Rules。

### 第二步：验证连接

```
GET /api/v1/openclaws/{CLAW_ID}/config

Header:
  Authorization: Bearer {HUB_API_TOKEN}
```

成功返回 200 说明 Token 有效，连接正常。

### 第三步：拉取已安装的 Skills 和 Rules

**获取分配的 Skills**：
```
GET /api/v1/openclaws/{CLAW_ID}/assigned-skills

Header:
  Authorization: Bearer {HUB_API_TOKEN}
```

**获取分配的 Rules**：
```
GET /api/v1/openclaws/{CLAW_ID}/assigned-rules

Header:
  Authorization: Bearer {HUB_API_TOKEN}
```

### 第三步补充：拉取 Skill 文档包

每个 Skill 可能包含一个文档包（多个文件：SKILL.md、SOUL.md、AGENTS.md、RULES.md、PROJECT.md、checklist 等）。

**列出文档包文件清单**：
```
GET /api/v1/skills/{SKILL_ID}/files

Header:
  Authorization: Bearer {HUB_API_TOKEN}
```

**获取单个文件内容**：
```
GET /api/v1/skills/{SKILL_ID}/files/SOUL.md

Header:
  Authorization: Bearer {HUB_API_TOKEN}
```

**打包下载整个文档包（ZIP）**：
```
GET /api/v1/skills/{SKILL_ID}/pack

Header:
  Authorization: Bearer {HUB_API_TOKEN}
```

### 第四步：本地写入

**Skills 写入方式**：每个 Skill 创建一个目录，包含文档包所有文件
```
~/.qclaw/skills/{skill_name}/
├── SKILL.md
├── SOUL.md（如果有）
├── AGENTS.md（如果有）
├── RULES.md（如果有）
├── PROJECT.md（如果有）
└── checklist.md（如果有）
```

**拉取流程**：
1. `GET /assigned-skills` 获取已安装 Skills 列表
2. 对每个 Skill：`GET /skills/{id}/files` 获取文件清单
3. 如果只有 SKILL.md → 直接用 `template_content` 写入
4. 如果有多个文件 → 逐个 `GET /skills/{id}/files/{filename}` 拉取写入
5. 或直接 `GET /skills/{id}/pack` 下载 ZIP 解压到本地

**Rules 写入方式**：
```
~/.qclaw/rules/{rule_name}.md  ← 内容为 content_template
```

### 第五步：建立通信

**方式一：SSE 长连接（推荐）**
```
GET /api/openclaws/{CLAW_ID}/events

Header:
  Authorization: Bearer {HUB_API_TOKEN}
  Accept: text/event-stream
```

SSE 会推送以下事件：
- `connected` — 连接成功
- `heartbeat` — 心跳
- `task` — 新任务
- `message` — 新消息
- `ping` — 保持连接

**方式二：轮询**
- 每 30 秒：`POST /api/v1/openclaws/{CLAW_ID}/heartbeat`
- 每 60 秒：`GET /api/openclaws/{CLAW_ID}/messages?unread=true`

---

## 模式B：增量接入（按需安装）

### 第一步：建立连接
同模式A的第一步和第二步，获取 CLAW_ID + TOKEN，验证连接。

### 第二步：浏览 Skills 市场
```
GET /api/v1/skills

Header:
  Authorization: Bearer {HUB_API_TOKEN}
```

### 第三步：选择安装 Skill
```
POST /api/v1/openclaws/{CLAW_ID}/skills

Header:
  Authorization: Bearer {HUB_API_TOKEN}

请求体：
{
  "skill_id": 3
}
```

### 第四步：选择安装 Rules
```
POST /api/v1/openclaws/{CLAW_ID}/rules

Header:
  Authorization: Bearer {HUB_API_TOKEN}

请求体：
{
  "rule_ids": [1, 3, 5]
}
```

### 第五步：拉取已安装内容
同模式A第三步，调用 `assigned-skills` 和 `assigned-rules` 拉取完整内容写入本地。

### 第六步：卸载不需要的 Skill
```
DELETE /api/v1/openclaws/{CLAW_ID}/skills/{SKILL_ID}

Header:
  Authorization: Bearer {HUB_API_TOKEN}
```

---

## 增量更新（定期同步）

已接入的 OpenClaw 应定期检查 Skills/Rules 是否有更新：

```
每小时执行一次：
1. GET /assigned-skills → 比对本地 Skills 版本
2. 对每个 Skill：GET /skills/{id}/files → 比对文件清单
3. 有新增/变更的文件逐个拉取更新
4. GET /assigned-rules → 比对本地 Rules 版本
5. 有变更则更新本地文件
```

**查看标准化的 Skills/Rules（Hub 推荐安装的）**：
```
GET /api/v1/skills/standard     → 标准化 Skills 列表
GET /api/v1/rules/standard      → 标准化 Rules 列表
```

---

## 待办系统

OpenClaw 可通过待办系统管理日常工作事项。

### 获取待办列表
```
GET /api/v1/openclaws/{CLAW_ID}/todos

Header:
  Authorization: Bearer {HUB_API_TOKEN}
```

### 标记待办完成
```
POST /api/v1/openclaws/{CLAW_ID}/todos/{TODO_ID}/complete

Header:
  Authorization: Bearer {HUB_API_TOKEN}

请求体（可选）：
{
  "result_summary": "执行结果摘要"
}
```

### 获取待办完成汇总
```
GET /api/v1/openclaws/{CLAW_ID}/todo-summary?date=2026-04-02

Header:
  Authorization: Bearer {HUB_API_TOKEN}
```

### 待办工作流建议
```
每天启动时：
1. GET /todos → 获取今日待办列表
2. 按 priority 排序执行
3. 定时待办到点提醒/执行
4. 完成后 POST /todos/{id}/complete 上报
5. 晚间：GET /todo-summary 检查完成率，未完成的纳入日报
```

---

## 本地配置文件

OpenClaw 接入后应在本地保存连接信息：

**~/.qclaw/hub_config.json**：
```json
{
  "hub_url": "https://clawteam.woa.com:18800",
  "claw_id": 5,
  "api_token": "oc_tk_xxxxxxxxx",
  "connection_mode": "sse",
  "last_sync": "2026-04-02T10:00:00",
  "installed_skills": ["manager-hub", "testcase-manager"],
  "installed_rules": ["security-rules", "report-rules"]
}
```

---

## 错误处理

| HTTP 状态码 | 说明 | 处理方式 |
|-------------|------|----------|
| 200 | 成功 | 正常处理 |
| 201 | 创建/安装成功 | 正常处理 |
| 401 | Token 缺失 | 检查 Authorization Header |
| 403 | Token 无效 | 从 Hub 重新获取 Token |
| 404 | OpenClaw/Skill 不存在 | 检查 ID 是否正确 |
| 409 | 重复（标签已存在） | 换一个唯一标识 |
| 500 | 服务器错误 | 检查 Hub 日志 |

---

## 触发词

- "连接 Hub"
- "注册到 Hub"
- "初始化 OpenClaw"
- "安装 Skill"
- "安装 Rule"
- "同步 Skills"
- "浏览 Skills 市场"
- "查看可用 Rules"
- "增量更新"
- "查看待办"
- "今日待办"
- "完成待办"
''',
},
{
    'name': 'agent-template-manager',
    'display_name': 'Agent 模板库管理',
    'description': '管理 Agent 身份模板库：编辑、版本回滚、文件引用校验和模板应用到目标 OpenClaw。',
    'category': 'standard',
    'is_standard': False,
    'trigger_phrase': '管理 agent 模板库',
    'scope': 'global',
    'template_content': '''# Agent 模板库管理 (agent-template-manager)

## 核心能力

1) 模板 CRUD
- GET /api/v1/agent-templates
- POST /api/v1/agent-templates
- GET /api/v1/agent-templates/{id}
- PUT /api/v1/agent-templates/{id}
- DELETE /api/v1/agent-templates/{id}

2) 审核与提交
- POST /api/v1/agent-templates/{id}/review
- POST /api/v1/agent-templates/submit

3) 应用模板到目标 Agent（真实落地）
- POST /api/v1/agent-templates/{id}/apply
  参数: {"target_claw_id": 14, "strict_references": true}
  动作: 安装/启用 Skill + Rule + 同步定时任务

4) 版本历史与回滚
- GET /api/v1/agent-templates/{id}/versions
- POST /api/v1/agent-templates/{id}/rollback

5) 文件管理与引用校验
- GET /api/v1/agent-templates/{id}/files
- POST /api/v1/agent-templates/{id}/files
- GET /api/v1/agent-templates/{id}/files/{file_id}/preview
- GET /api/v1/agent-templates/{id}/references/validate

## 引用语法
- {{file:docs/owner.md}}

## 推荐流程
创建模板 -> 上传文件 -> 引用校验 -> 提交审核 -> 审核通过 -> apply 到目标 OpenClaw
''',
}


# 标准 Rules 模板
STANDARD_RULES = [
    {
        'name': 'base-workflow',
        'display_name': '基础工作规范',
        'description': '所有 OpenClaw 必须遵守的基础工作规范，包括任务执行、日报提交、异常处理等基本要求。',
        'category': 'standard',
        'scope': 'global',
        'is_standard': True,
        'content_template': '''# 基础工作规范

## 任务执行
1. 收到任务后，先确认理解任务目标
2. 遇到不清晰的地方，先提问再执行
3. 任务完成后，简要汇报结果

## 日报提交
1. 每日按计划时间提交日报
2. 日报内容包含：完成事项、学习收获、问题记录
3. 如有紧急任务，提前报备

## 异常处理
1. 发现异常情况及时上报
2. 遇到阻塞问题，主动寻求协助
3. 重大问题不擅自决定，汇报后执行
''',
    },
    {
        'name': 'security-baseline',
        'display_name': '安全基线规范',
        'description': '安全相关的基本规范，确保 OpenClaw 操作符合安全要求。',
        'category': 'standard',
        'scope': 'global',
        'is_standard': True,
        'content_template': '''# 安全基线规范

## 权限管理
1. 不尝试越权操作
2. 不获取超出职责范围的系统权限
3. 敏感操作需确认授权

## 数据处理
1. 不操作真实生产数据
2. 测试数据需脱敏处理
3. 敏感信息不外泄

## 操作规范
1. 危险命令需二次确认
2. 删除操作需谨慎
3. 不执行来源不明的代码
''',
    },
    {
        'name': 'lobster-king-admin',
        'display_name': '龙虾王管理员规范',
        'description': '龙虾王作为龙虾军团系统管理员的专属行为规范。定义其角色定位、职责边界、工作流程和行为约束。',
        'category': 'standard',
        'scope': 'admin',
        'owner_claw_id': 4,  # Lobster King claw_id
        'is_standard': False,
        'content_template': '''# 龙虾王管理员规范

## 一、角色定位

龙虾王是龙虾军团（OpenClaw 集群）的系统管理员，不隶属于任何具体项目。
核心职责是统筹管理所有龙虾（OpenClaw 实例）的运行状态，确保整个军团高效协作。

## 二、核心职责

### 2.1 监管所有龙虾状态
1. 定期检查所有 OpenClaw 实例的在线/离线状态
2. 发现异常（长时间离线、心跳中断）时主动排查并处理
3. 关注各龙虾的日报提交情况，督促未按时提交的实例
4. 汇总各龙虾的工作负载，识别过载或闲置的实例

### 2.2 定期下发任务与回收结果
1. 根据业务需要，向指定龙虾下发任务
2. 跟踪任务执行进度，确保按时完成
3. 回收任务结果，检查质量和完整性
4. 对失败或超时的任务进行重试或重新分配

### 2.3 定期组织会议
1. 定期召集所有龙虾进行工作同步（通过 Hub 广播或消息）
2. 汇总各龙虾的工作进展和问题
3. 协调跨项目的资源和信息共享
4. 发布重要通知和决策

### 2.4 学习游戏测试知识
1. 持续学习游戏测试领域的专业知识
2. 关注行业最佳实践和新技术趋势
3. 将学到的知识整理到知识库（Memos），供全军团共享
4. 评审和审核其他龙虾提交的知识条目

## 三、行为边界（严格遵守）

### 3.1 不做的事情
1. **不参与具体项目的测试执行工作**——这是各项目龙虾的职责
2. **不直接编写测试用例**——这是测试成员的职责
3. **不直接提交 Bug**——这是测试执行者的职责
4. **不代替其他龙虾完成其分内任务**
5. **不处理与龙虾军团管理无关的事务**

### 3.2 必须做的事情
1. 每日检查军团整体状态
2. 按时提交自己的管理日报
3. 及时响应其他龙虾的求助消息
4. 定期更新知识库中的管理类知识

## 四、工作节奏

| 时间 | 事项 |
|------|------|
| 每日上午 | 检查所有龙虾状态，处理异常 |
| 每日下午 | 下发任务、回收结果、学习知识 |
| 每日 15:00 | 提交上午工作日报 |
| 每日 21:00 | 提交全天工作日报 |
| 每周一 | 召开周例会，同步各龙虾工作进展 |
| 每周五 | 汇总本周军团整体工作报告 |

## 五、权限说明

1. 拥有 Hub 管理员权限（admin 角色）
2. 可以向任意龙虾发送消息和下发任务
3. 可以广播消息给所有在线龙虾
4. 可以审核知识库条目
5. 可以查看所有龙虾的日报和状态
''',
    },
]


def seed_skills():
    """初始化标准 Skills（已存在的只更新 template_content，不覆盖标签/scope 等管理员手动修改的字段）"""
    created = 0
    for skill_data in STANDARD_SKILLS:
        existing = Skill.query.filter_by(name=skill_data['name']).first()
        if not existing:
            skill = Skill(**skill_data)
            db.session.add(skill)
            created += 1
        else:
            # 仅更新内容，不覆盖管理员手动修改的 is_standard/scope/description 等
            if 'template_content' in skill_data:
                existing.template_content = skill_data['template_content']

    # 初始化注册 Skill（已存在则更新 template_content）
    existing_reg = Skill.query.filter_by(name=REGISTRATION_SKILL['name']).first()
    if not existing_reg:
        skill = Skill(**REGISTRATION_SKILL)
        db.session.add(skill)
        created += 1
    else:
        # 仅更新内容
        existing_reg.template_content = REGISTRATION_SKILL['template_content']

    db.session.commit()
    return created


_MISSION_SKILLS = {
    'agent-operating-protocol': (
        'Agent 运转协议', '所有正式任务共用的上下文、执行和收尾协议'),
    'mission-requirement-analysis': (
        'Mission 需求分析', '产出 requirement_analysis v1 Artifact'),
    'mission-engineering-analysis': (
        'Mission 工程分析', '产出 engineering_analysis v1 Artifact'),
    'mission-test-execution': (
        'Mission 测试执行', '通过 typed Runner 产出 execution_record v1 Artifact'),
}


def seed_game_test_agent_team():
    """导入P0三岗位Skill/Profile；不覆盖同版本人工配置，不自动分配Claw。"""
    root = Path(__file__).resolve().parents[2]
    skills_root = root / 'openclaw-agent' / 'skills'
    profile_path = (
        root / 'openclaw-agent' / 'profiles' / 'game-test-team-v1.json')
    document = json.loads(profile_path.read_text(encoding='utf-8'))
    if document.get('schema') != 1 or document.get('version') != 1:
        raise ValueError('game test team seed schema is unsupported')

    created_skills = 0
    for skill_name, (display_name, description) in _MISSION_SKILLS.items():
        skill_dir = skills_root / skill_name
        content = (skill_dir / 'SKILL.md').read_text(encoding='utf-8')
        relative_pack = str(skill_dir.relative_to(root)).replace('\\', '/')
        reference_paths = sorted(
            path for path in skill_dir.rglob('*')
            if path.is_file() and path.name != 'SKILL.md')
        manifest = [{
            'name': str(path.relative_to(skill_dir)).replace('\\', '/'),
            'description': '岗位Artifact合同参考',
        } for path in reference_paths]
        skill = Skill.query.filter_by(name=skill_name).first()
        if not skill:
            skill = Skill(
                name=skill_name, display_name=display_name,
                description=description, category='business_test',
                trigger_phrase=skill_name, template_content=content,
                pack_path=relative_pack, files=manifest, scope='global',
                is_standard=False, created_by='system',
                review_status='approved', last_modified_source='system')
            db.session.add(skill)
            db.session.flush()
            created_skills += 1
        elif (skill_name != 'agent-operating-protocol'
              and skill.created_by == 'system'
              and skill.last_modified_source == 'system'):
            skill.display_name = display_name
            skill.description = description
            skill.template_content = content
            skill.pack_path = relative_pack
            skill.files = manifest
        for path in reference_paths:
            filename = str(path.relative_to(skill_dir)).replace('\\', '/')
            entry = SkillFile.query.filter_by(
                skill_id=skill.id, filename=filename).first()
            if not entry:
                db.session.add(SkillFile(
                    skill_id=skill.id, filename=filename,
                    content=path.read_text(encoding='utf-8'),
                    file_type='markdown', description='岗位Artifact合同参考'))
            elif (skill_name != 'agent-operating-protocol'
                  and skill.created_by == 'system'
                  and skill.last_modified_source == 'system'):
                entry.content = path.read_text(encoding='utf-8')

    created_profiles = 0
    created_posts = 0
    for item in document.get('profiles') or []:
        profile = AgentProfile.query.filter_by(
            profile_key=item['profile_key']).first()
        values = {
            'name': item['name'], 'description': item['description'],
            'system_prompt': item['system_prompt'],
            'workflow_config': item['workflow_config'],
            'required_skills_json': item['required_skills'],
            'contract_json': item['contract'], 'version': document['version'],
            'status': 'active', 'created_by': 'system',
        }
        if not profile:
            profile = AgentProfile(profile_key=item['profile_key'], **values)
            db.session.add(profile)
            db.session.flush()
            created_profiles += 1
        elif int(profile.version or 1) < int(document['version']):
            for field, value in values.items():
                setattr(profile, field, value)
        post_data = item['post']
        post = AgentPost.query.filter_by(
            post_key=post_data['post_key'], project_id=None).first()
        if not post:
            db.session.add(AgentPost(
                post_key=post_data['post_key'], name=post_data['name'],
                description=item['description'], project_id=None,
                profile_id=profile.id,
                required_profile_version=post_data['required_profile_version'],
                status='active', created_by='system'))
            created_posts += 1

    db.session.commit()
    return {
        'skills': created_skills,
        'profiles': created_profiles,
        'posts': created_posts,
    }


def _seed_sha256(value):
    rendered = json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8')
    return 'sha256:' + hashlib.sha256(rendered).hexdigest()


def seed_game_test_eval_data():
    """导入24个P0 Eval Case为draft；真实双评完成前不自动freeze。"""
    root = Path(__file__).resolve().parents[2]
    path = root / 'openclaw-agent' / 'eval' / 'game-test-team-v1.json'
    document = json.loads(path.read_text(encoding='utf-8'))
    if document.get('schema') != 1 or document.get('version') != 1:
        raise ValueError('game test eval seed schema is unsupported')
    project = Project.query.filter_by(name=document['project_name']).first()
    if not project:
        raise ValueError('game test eval seed project is missing')
    review_policy = {
        'required_reviewers': int(
            document.get('review_policy', {}).get('required_reviewers') or 0),
        'max_dimension_variance_percent': float(
            document.get('review_policy', {}).get(
                'max_dimension_variance_percent', 20)),
    }
    created_datasets = 0
    created_cases = 0
    for spec in document.get('datasets') or []:
        dataset_seed_key = (
            f'seed:{document["suite_key"]}:{spec["dataset_key"]}')
        dataset = AgentEvalDataset.query.filter_by(
            project_id=project.id, dataset_key=spec['dataset_key'],
            version=document['version']).first()
        if not dataset:
            request = {
                'project_id': project.id, 'dataset_key': spec['dataset_key'],
                'role_key': spec['role_key'], 'version': document['version'],
                'split': spec['split'],
                'rubric_version': spec['rubric_version'],
                'review_policy': review_policy,
            }
            dataset = AgentEvalDataset(
                project_id=project.id, dataset_key=spec['dataset_key'],
                role_key=spec['role_key'], version=document['version'],
                split=spec['split'], rubric_version=spec['rubric_version'],
                status='draft', dataset_sha256='',
                review_policy_json=review_policy, review_status='pending',
                review_records_json=[],
                idempotency_key=dataset_seed_key,
                request_sha256=_seed_sha256(request),
                created_by_type='system', created_by_id=0,
                created_by_name='system')
            db.session.add(dataset)
            db.session.flush()
            created_datasets += 1
        elif not (
                dataset.created_by_type == 'system'
                and int(dataset.created_by_id or 0) == 0
                and dataset.idempotency_key == dataset_seed_key):
            raise ValueError(
                f'eval dataset key is owned by non-seed data: {spec["dataset_key"]}')
        if dataset.status != 'draft':
            continue
        for case_spec in spec.get('cases') or []:
            existing_case = AgentEvalCase.query.filter_by(
                    dataset_id=dataset.id,
                    case_key=case_spec['case_key']).first()
            case_seed_key = f'seed:{case_spec["case_key"]}'
            if existing_case:
                if not (
                        existing_case.created_by_type == 'system'
                        and int(existing_case.created_by_id or 0) == 0
                        and existing_case.idempotency_key == case_seed_key):
                    raise ValueError(
                        'eval case key is owned by non-seed data: '
                        + case_spec['case_key'])
                continue
            request = {
                key: case_spec[key] for key in (
                    'case_key', 'input_snapshot', 'expected_contract',
                    'required_evidence', 'deterministic_checks',
                    'allowed_variance', 'hidden_tags')
            }
            db.session.add(AgentEvalCase(
                dataset_id=dataset.id, case_key=case_spec['case_key'],
                input_snapshot_json=case_spec['input_snapshot'],
                expected_contract_json=case_spec['expected_contract'],
                required_evidence_json=case_spec['required_evidence'],
                deterministic_checks_json=case_spec['deterministic_checks'],
                allowed_variance_json=case_spec['allowed_variance'],
                hidden_tags_json=case_spec['hidden_tags'],
                input_sha256=_seed_sha256(case_spec['input_snapshot']),
                status='active',
                idempotency_key=case_seed_key,
                request_sha256=_seed_sha256(request),
                created_by_type='system', created_by_id=0,
                created_by_name='system'))
            created_cases += 1
    db.session.commit()
    return {'datasets': created_datasets, 'cases': created_cases}


def seed_rules():
    """初始化标准 Rules（跳过已存在的，但更新 scope/owner_claw_id）"""
    created = 0
    for rule_data in STANDARD_RULES:
        existing = Rule.query.filter_by(name=rule_data['name']).first()
        if not existing:
            rule = Rule(**rule_data)
            db.session.add(rule)
            created += 1
        else:
            # Update scope and owner_claw_id for existing rules
            if 'scope' in rule_data and existing.scope != rule_data['scope']:
                existing.scope = rule_data['scope']
            if 'owner_claw_id' in rule_data and existing.owner_claw_id != rule_data.get('owner_claw_id'):
                existing.owner_claw_id = rule_data['owner_claw_id']

    db.session.commit()
    return created


STANDARD_PROJECTS = [
    {'name': 'QQ飞车', 'tapd_workspace_id': '1000047'},
    {'name': 'QQ飞车手游版', 'tapd_workspace_id': '10124081'},
    {'name': '合金弹头', 'tapd_workspace_id': '20375982'},
    {'name': '魂斗罗', 'tapd_workspace_id': '10102081'},
    {'name': 'PRacing', 'tapd_workspace_id': '20417582'},
]


def seed_projects():
    """初始化默认项目（跳过已存在的）"""
    created = 0
    for proj_data in STANDARD_PROJECTS:
        existing = Project.query.filter_by(name=proj_data['name']).first()
        if not existing:
            project = Project(**proj_data)
            db.session.add(project)
            created += 1

    db.session.commit()
    return created


STANDARD_MODULES = [
    # 外围系统
    {'name': '大厅系统', 'category': 'peripheral', 'description': '游戏大厅、主界面、导航'},
    {'name': '社交系统', 'category': 'peripheral', 'description': '好友、聊天、组队'},
    {'name': '活动系统', 'category': 'peripheral', 'description': '运营活动、任务系统'},
    {'name': '商城系统', 'category': 'peripheral', 'description': '道具商城、礼包'},
    # 核心单局
    {'name': '单局玩法', 'category': 'core_gameplay', 'description': '核心游戏玩法'},
    {'name': '匹配系统', 'category': 'core_gameplay', 'description': '匹配、房间、对局'},
    {'name': '操控系统', 'category': 'core_gameplay', 'description': '操控手感、输入响应'},
    # 商业化
    {'name': '充值系统', 'category': 'commercialization', 'description': '支付、充值'},
    {'name': '会员系统', 'category': 'commercialization', 'description': 'VIP、特权'},
    {'name': '抽奖系统', 'category': 'commercialization', 'description': '扭蛋、抽卡'},
    # 客户端性能
    {'name': '帧率性能', 'category': 'client_performance', 'description': '帧率、流畅度'},
    {'name': '内存性能', 'category': 'client_performance', 'description': '内存占用、泄漏'},
    {'name': '启动性能', 'category': 'client_performance', 'description': '冷启动、热启动耗时'},
    {'name': '包体大小', 'category': 'client_performance', 'description': '安装包体积'},
    # 服务器专项
    {'name': '服务器压测', 'category': 'server_special', 'description': '服务器承载能力'},
    {'name': '网络延迟', 'category': 'server_special', 'description': '网络质量、断线重连'},
    # 其他专项
    {'name': '兼容性测试', 'category': 'other', 'description': '多机型、多系统兼容'},
    {'name': '安全测试', 'category': 'other', 'description': '安全漏洞、外挂检测'},
]


def seed_modules():
    """初始化默认模块（跳过已存在的）"""
    created = 0
    for mod_data in STANDARD_MODULES:
        existing = Module.query.filter_by(name=mod_data['name']).first()
        if not existing:
            module = Module(**mod_data)
            db.session.add(module)
            created += 1

    db.session.commit()
    return created


# ============== 注册初始化任务模板 ==============

INIT_TASKS = [
    {
        'title': '验证 sidecar 进程在线',
        'description': '确认仅有一个 hub-sse-sidecar 在运行，并且日志出现“已连接 Hub”。提交进程列表与日志片段。',
        'priority': 'P0',
        'urgency_level': 'interrupt',
        'verification_target': 'sidecar-online',
    },
    {
        'title': '验证聊天闭环',
        'description': '在 Hub 通信中心接收一条测试消息后，60 秒内完成 read_at + reply_to 闭环。提交 msg_id、reply_id、耗时。',
        'priority': 'P0',
        'urgency_level': 'interrupt',
        'verification_target': 'chat-closure',
    },
    {
        'title': '验证待办闭环',
        'description': '在 Hub 新建一条测试待办，确保收到通知并完成提交。提交 todo_id、完成状态和结果摘要。',
        'priority': 'P0',
        'urgency_level': 'retry',
        'verification_target': 'todo-closure',
    },
    {
        'title': '提交注册验收报告',
        'description': '汇总 sidecar/chat/todo 三项验收证据，提交到本任务 result_summary，格式固定：指标+证据链接/日志片段。',
        'priority': 'P1',
        'urgency_level': 'flexible',
        'verification_target': 'registration-report',
    },
    {
        'title': '等待龙虾王审核',
        'description': '此任务由龙虾王审核通过后，注册才算完成。未通过需按审核意见整改并重新提交。',
        'priority': 'P0',
        'urgency_level': 'background',
        'verification_target': 'dragonking-approval',
    },
]


def create_init_tasks_for_claw(claw_id):
    """为新注册的 OpenClaw 创建初始化验证任务

    优先从数据库 system_config 表读取配置（龙虾王可通过 API 修改），
    fallback 到 INIT_TASKS 硬编码。
    """
    from app.models import ClawTodo, SystemConfig
    import json as _json

    # 优先读数据库配置
    tasks = INIT_TASKS
    cfg = SystemConfig.query.filter_by(config_key='init_tasks').first()
    if cfg and cfg.value:
        try:
            tasks = _json.loads(cfg.value)
        except Exception:
            pass

    created = 0
    for task in tasks:
        todo = ClawTodo(
            openclaw_id=claw_id,
            title=task['title'],
            description=task.get('description', ''),
            schedule_type='once',
            priority=task.get('priority', 'P1'),
            urgency_level=task.get('urgency_level', 'background'),
            task_category='init',
            verification_target=task.get('verification_target', ''),
            enabled=True,
            created_by='system',
        )
        db.session.add(todo)
        created += 1
    return created


# ============== 标准包种子数据 ==============

STANDARD_PACKS = [
    {
        'name': 'standard-skills-pack',
        'display_name': 'Skills 标准包',
        'description': '所有注册的 OpenClaw 必须安装的基础 Skills 集合',
        'pack_type': 'skill',
        'is_active': True,
        # item_ids 在 seed 时动态填充（取所有 is_standard=True 的 Skill ID）
    },
    {
        'name': 'standard-rules-pack',
        'display_name': 'Rules 标准包',
        'description': '所有注册的 OpenClaw 必须遵守的基础规范集合',
        'pack_type': 'rule',
        'is_active': True,
        # item_ids 在 seed 时动态填充（取所有 is_standard=True 的 Rule ID）
    },
]


def seed_packs():
    """初始化标准包（动态收集 is_standard=True 的资源 ID）"""
    from app.models import StandardPack, Skill, Rule
    created = 0

    for pack_data in STANDARD_PACKS:
        existing = StandardPack.query.filter_by(name=pack_data['name']).first()

        # 动态收集 ID
        if pack_data['pack_type'] == 'skill':
            items = Skill.query.filter_by(is_standard=True).all()
            item_ids = [s.id for s in items if s.name != 'registration-skill']
        else:
            items = Rule.query.filter_by(is_standard=True).all()
            item_ids = [r.id for r in items]

        if existing:
            # 更新已有包的 item_ids
            existing._set_item_ids(item_ids)
            existing.display_name = pack_data['display_name']
            existing.description = pack_data['description']
        else:
            import json as _json
            pack = StandardPack(
                name=pack_data['name'],
                display_name=pack_data['display_name'],
                description=pack_data['description'],
                pack_type=pack_data['pack_type'],
                item_ids=_json.dumps(item_ids),
                is_active=pack_data.get('is_active', True),
            )
            db.session.add(pack)
            created += 1

    db.session.commit()
    return created


def seed_all():
    """执行所有种子数据初始化"""
    skills_count = seed_skills()
    rules_count = seed_rules()
    projects_count = seed_projects()
    modules_count = seed_modules()
    agent_team = seed_game_test_agent_team()
    eval_data = seed_game_test_eval_data()
    packs_count = seed_packs()
    return (f'创建了 {skills_count} 个标准 Skills，{rules_count} 个标准 Rules，'
            f'{projects_count} 个默认项目，{modules_count} 个默认模块，'
            f'{packs_count} 个标准包；Agent团队新增 '
            f'{agent_team["skills"]} Skill / '
            f'{agent_team["profiles"]} Profile / {agent_team["posts"]} Post；'
            f'Eval新增 {eval_data["datasets"]} Dataset / '
            f'{eval_data["cases"]} Case')
