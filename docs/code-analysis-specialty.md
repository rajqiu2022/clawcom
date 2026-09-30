# 项目代码分析专项接入说明

本功能复用 Hub 的 Workflow、测试报告、知识版本和 Skill 制品，不新增一套分析执行引擎。Hub 保存测试计划范围、代码基线引用、分析方法和人工反馈；配置的项目 Agent 执行分析和反馈学习。本说明面向部署维护者与分析 Agent。

## 启用和使用

先备份数据库，执行 `ops/migrations/20260930_code_analysis_specialty.sql`。它只新增四张表，依赖现有左移分析、纪要版本和 Workflow 表；MariaDB 使用 LONGTEXT 保存 JSON。然后设置 `CODE_ANALYSIS_ENABLED=true` 并重启 Hub。默认关闭；关闭后保留数据，不执行删除回滚。

公司 systemd Hub 使用 `python ops/deploy_code_analysis.py --key <SSH私钥路径>` 做只读预检，加 `--apply` 执行部署。脚本仅发布已提交的专项文件，保留线上独立热修；先备份文件与完整数据库、验证增量迁移和暂存应用，再短暂停机切换，并以真实 HTTP 回读验收。失败恢复旧代码和开关，新增表保留。备份和部署文件哈希记录在服务目录 `backups/` 中。部署不会启动分析 Run、修改 Worker 或创建 TAPD Bug。

打开 `/code-analysis`，选择项目并初始化，创建空白通用经验库和该项目经验库。初始化不会覆盖已有内容。选择本项目执行 Agent、初始 Skill、只读代码仓库 URL、源分支和主项目经验库；可额外选择最多 30 篇可读知识，包括跨项目共享知识。

从已有测试计划创建分析，填写起始代码基线。已完成计划也可用于复盘。创建时冻结计划任务、Skill 正文和文件摘要、知识版本与近期人工反馈；运行时将代码引用解析为固定 commit。启动不要求存在活动 Mission 或经理租约，但保留项目访问、执行身份、Worker 和 Workflow 的基本检查。

分析 Agent 需要可读代码的本机配置和可用 Hub 工具。配置 URL 不会自动创建源码访问权限或安装分析工具，Hub 不向模型传递仓库或 TAPD 凭据。禁止在仓库 URL 中填写密码或 Token。

## 分析 Agent 回写

普通 `agent_task` Workflow 的 `start_vars.code_analysis` 携带冻结合同、operation 和 result_api。分析和学习分别回写该地址。所有回写携带当前 `workflow_run_id` 和 `attempt_no`，只允许指定执行 Agent 的有效 attempt；保存后的完全相同回执可幂等回读。

分析结果示例：

```json
{
  "workflow_run_id": 123,
  "attempt_no": 1,
  "baseline": {"base_sha": "40或64位十六进制commit", "target_sha": "40或64位十六进制commit"},
  "report_id": 456,
  "summary": "分析结论",
  "findings": [{"finding_key": "稳定问题标识", "title": "潜在Bug", "description": "代码证据及风险", "severity": "high", "module": "登录", "code_locations": [{"file": "Login.cs", "line": 42}]}]
}
```

报告必须是本项目已发布、未删除且未隐藏的 TestReport；Hub 同时关联到原测试计划。候选最多 500 条。finding_key 在项目内稳定，用于同一问题跨次分析保留人工历史。完整报告不因知识共享而公开到其他项目。

## 人工处置和学习

人工动作包括直接提单、待确认和忽略；事实标签另记真实 Bug、误报、重复 Bug、风险接受或未确认。风险接受不计作误报。直接提单要求真实 Bug 标签及明确确认，使用现有 TAPD 凭据服务。遇到网络超时或不确定回执，保存为待核验，不自动重复创建；人工可查询 TAPD 并关联已有 Bug。

每次标注保留原证据和判断依据，幂等派发独立反馈学习 Run。学习回写包含 `expected_revision`、更新后的完整 `project_content`、`summary`，可附 `general_proposal`。只有项目知识新版本成功保存才显示已学习；版本冲突需重读合并，新的人工标注会使旧学习结果失效。

这属于反馈案例与知识更新，不是训练大模型权重。Agent 不能自行修改人工标签或直接覆盖通用库。通用建议须由人工审核去除项目专属信息，再追加到通用知识版本。来源项目维护者可为知识和已通过的 Skill 设置仅来源项目、指定项目或全项目共享；其他项目只读使用，不能改写原资源。

## 验收边界

单元测试使用 SQLite 与模拟 TAPD，不创建真实 Bug。上线前仍需确认 MariaDB 迁移、真实 Worker 对普通 AgentTask 合同的执行及 Hub 回写工具，再完成一次分析、人工标注、项目学习版本、通用发布和 TAPD 去重验收。

如果学习 Run 已创建但执行失败，页面保留 Run 链接，需按现有 Workflow 恢复机制处理同一 Run；不要用新人工标注绕过恢复。当前没有新增后台无限重试，也没有自动调优 Skill 或自动提单策略。
