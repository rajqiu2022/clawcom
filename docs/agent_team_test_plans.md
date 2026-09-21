# 团队与测试计划

## 使用

- 团队页分为“团队成员 / 任务计划”。原成员自报卡片保留；任务页展示计划卡片、日/周任务排期及既有 Mission 执行链。
- “每日 / 每周 / 全部”与基准日期只影响展示；周为北京时间周一至周日。跨日任务按日期交集出现，不创建重复执行任务。
- 指标来自真实 TestTask 状态：执行中、已完成、阻塞、逾期；完成比例仅计算测试任务，不含任务链。未排期任务独立提示并可在“全部”查看。逾期以当前北京时间判断，completed/skipped 不算逾期。
- 草稿计划可以展示，但不代表已启动执行。新建/关联不会创建 Mission、Run、监督消息或开启模型调用。
- 新建计划默认为草稿；跳转详情补充任务与执行人。可关联同项目尚未归属团队的已有计划，不按名称或执行人猜测归属。
- `/test-plans?plan_id=ID` 直接定位指定计划；无迭代计划也可正常查看和编辑。

## Agent API

团队测试经理可用自身受控 Hub 身份创建计划，无需 Codex Provider 或额外 Worker Runtime：

```http
POST /api/v1/agent-teams/{team_id}/test-plans
```
```json
{"name":"本周回归","start_date":"2026-09-21","end_date":"2026-09-27","status":"draft"}
```

也支持既有 `POST /test-plans` 加 `team_id`。项目从团队推导；显式 project_id/iteration_id 必须匹配团队项目。

- `GET /agent-teams/{team_id}/test-plans?period=week&date=2026-09-21&limit=6&offset=0`：返回真实统计、计划卡片、每计划最多 5 项任务预览及详情 URL。每页最多 24 个计划。
- `PUT /test-plans/{plan_id}` 加 `team_id`：关联已有无团队计划；后续不能用该接口转移/清空团队或跨项目移动。
- `POST /test-plans/{plan_id}/tasks`：测试经理可创建计划内任务，沿用既有测试任务通知。
- 同项目用户/Agent 可读。计划创建、关联、根编辑/删除、新建任务限定当前主经理、持有效接管任期的备用经理或项目管理员；不继承 Agent Owner 的管理员身份绕过角色。
- Sidecar `agent_teams[].test_plans` 下发团队计划 API。计划监督沿用独立 start/claim/decision 合同，监督 team_id 必须匹配计划归属。

## 发布

先备份并执行 `ops/migrations/20260921_agent_team_test_plans.sql`（MariaDB 10.1 兼容），再更新代码。新增 nullable `test_plans.team_id`、索引与 FK；旧计划保持未关联。数据库列是新 ORM 查询的前提，不可先切应用。

本次不自动关联生产计划 #50、不部署 Worker、不激活监督，也不扩张 Flow 权限。由管理员/测试经理在团队页明确关联后展示。
