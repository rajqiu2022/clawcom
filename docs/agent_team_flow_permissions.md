# 团队 Flow 自动授权

管理员在团队「调度范围」勾选并保存 Flow 后：

- 主/备测试经理获得读取、启动和向本团队工作成员派发的权限。
- 代码分析员、测试执行员获得读取与执行权限，不获得转派其他 Worker 的权限。
- 不授予 Flow 编辑、删除或 ACL 管理权限。
- 仅对已开启团队功能的项目、active 团队、同项目且未删除的成员、active Flow 生效。

这是动态授权来源，不写入 Flow 手工 ACL，也不修改持久化 Sidecar 白名单。取消勾选、移除成员、暂停/归档团队或关闭项目团队功能后，新请求立即重算；另一团队或手工授予的独立权限保留。已有业务 Run 不会因此被强制取消。

## Worker 配置

`/api/openclaws/{claw_id}/sidecar-config` 将当前团队授权加入有效
`system_context.policy.allowed_workflow_create_definition_ids`，不再被旧的持久白名单漏项挡住。
团队信息与有效权限变化会改变 `system_context_digest`，Worker 在既有周期刷新配置后可使用。
非法 Codex 策略仍 fail-closed；`codex_orchestrator.allowed_next_flows` 及自动恢复转换关系不自动扩大，自动恢复与团队显式派工不是同一授权。

## 派发边界

普通 Run 中，经理只能依团队授权向同一授权团队内的代码分析员/执行员指定 `worker_claw_id`。
Mission 仍须经理租约、当前团队策略与不可变阶段成员快照的交集、Worker 白名单、角色/专长匹配、可信 Runtime 和预算。
普通 Mission 若在创建时使用团队 Flow 授权，记录 Hub 生成的授权来源，后续派发重新检查该权限，不能用旧 Mission 绕过撤权。
Flow 本身的运行模式、资源/凭据授权及实际执行能力要求不变。

## 上线

本改动无需新增数据库字段，也无需重新保存现有团队。上线后，现有勾选的 Flow 自动按此规则计算；不自动启动任何业务 Run。
若同批发布团队测试计划功能，仍须执行该功能单独的数据库迁移。

专项回归：`tests/test_agent_team_flow_permissions.py`。
