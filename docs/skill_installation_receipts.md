# Hub Skill 安装回执合同 v1

## 范围与可信边界

Hub 的分配关系是期望状态，不是安装结果。普通聊天的 read/send/done、待办中的“安装成功”文字、历史 `installed_at` 都不再作为安装证明。

本合同只校验 **当前 Claw 认证身份下的 Worker 回执**、分配批次、当前文档包版本和完整文件摘要一致性。Hub 不能远程独立证明文件已落盘或目录确实隔离；实例目录、摘要计算和重载行为仍需 Worker 真实实现。当前 Agent 与 Worker 共用 Claw 凭据时，本接口也不能将二者密码学隔离。不得让模型编造回执。

## 分配与发现

管理员仍使用现有单个/批量 Skill 分配接口。每次分配生成新的 `installation.generation`，状态为 `pending`，并禁用同 Claw、同 Skill 的旧安装待办；原记录和回执保留用于审计。既有取消冲突卸载待办行为保留。

`GET /api/v1/openclaws/{claw_id}/assigned-skills` 与 `GET /api/v1/openclaws/{claw_id}/skill-manifest` 的各 Skill 新增 `installation`：

```json
{
  "contract_version": 1,
  "state": "pending",
  "verified": false,
  "generation": "由 Hub 生成的 32 位批次",
  "todo_id": 123,
  "receipt_api": "/api/v1/openclaws/60/skills/220/installation-receipts"
}
```

以上 ID 仅为示例；运行时必须以受控实例身份和当前 manifest 为准，不从聊天内容猜测。仅通过 Profile/任务上下文授权、未正式分配的 Skill，其 manifest 中 `installation=null`，不能提交正式安装回执。

市场接口 `assigned_count` 是分配数，`install_count` / `used_by_fresh` 仅统计有效回执；`installation_states` 提供逐实例状态。兼容字段 `used_by` 仍列分配名单。

## Worker 必须接线的流程（本次未修改 Worker）

1. 将 `sync_config` 和安装待办作为控制操作处理，不交给 CodeBuddy/其他模型自由安装。
2. 使用当前实例受控身份拉取 manifest 与其中 `pack_url`。不得自行再调用分配接口。
3. 写入实例隔离目录，拒绝共享 `~/.qclaw/skills`。校验全部文件，并按包内约定计算 SHA。不要自动运行下载包中的安装脚本。
4. 加载/重载实例 Skill 后，回读实际加载摘要；再提交回执。
5. 成功回执之后，调用当前 `installation.todo_id` 的已有 complete 接口。无有效回执返回 HTTP 409 `SKILL_INSTALLATION_RECEIPT_REQUIRED`；普通非安装待办不受影响。

## 安装回执 API

`POST /api/v1/openclaws/{claw_id}/skills/{skill_id}/installation-receipts`

只接受该 Claw 自身的 Bearer 认证，其他 Claw 或管理员登录会话不能代报。凭据由 Worker 受控客户端注入，不写入任务文本。

```json
{
  "generation": "当前 installation.generation",
  "event_id": "install-20260921-1",
  "state": "verified",
  "bundle_sha256": "当前 Skill 条目的 sha256",
  "content_version": "当前 Skill 条目的 content_version",
  "files": {
    "SKILL.md": "实际文件 SHA-256，与 manifest.files 一致"
  },
  "runtime": {
    "claw_id": 60,
    "instance_id": "worker-claw-60",
    "skill_scope": "instance",
    "manifest_sha256": "当前 Skill 包 sha256，不是整个 manifest JSON 的摘要",
    "loaded_sha256": "实际加载的 Skill 包摘要",
    "reload_succeeded": true
  }
}
```

`files` 必须包含本包的全部文件，不能缺失或增加文件。包摘要沿用 Hub `skill_bundle_descriptor` 算法：按路径排序，逐文件拼接路径 UTF-8 字节长度（8 字节大端）、路径、内容 UTF-8 字节长度（8 字节大端）、内容，再计算 SHA-256；不能用 ZIP 字节摘要替代。

允许状态：`syncing`、`failed`、`verified`。前两种不附带 files/runtime；failed 必须带非敏感的大写 `error_code`（例如 `RELOAD_FAILED`），syncing 不带 error_code。成功验证的同批次不接受新事件降级；强制重装须重新分配。内容更新会让旧验证失效，Worker 拉取新版本后重新验证。

幂等键为分配记录 + generation + event_id。相同事件、相同内容重放返回 `replayed=true`；内容不同返回 `SKILL_RECEIPT_CONFLICT`。重分配/内容更新/撤销后，旧回执即使重放也不能恢复旧状态。

主要错误：

- 401/403：未认证、身份不匹配、未分配或当前不可下发。
- 409 `SKILL_INSTALLATION_STALE` / `SKILL_CONTENT_STALE`：重新获取 manifest。
- 409 `SKILL_FILES_MISMATCH` / `SKILL_RUNTIME_UNVERIFIED`：校验或实际重载未通过。
- 409 `SKILL_INSTALLATION_TERMINAL`：本批次已验证，禁止迟到状态覆盖。
- 400 `SKILL_RECEIPT_INVALID`：字段/状态不符合合同。

## 发布与兼容

先备份并执行 `ops/migrations/20260921_skill_installation_receipts.sql`，再切换 Hub 应用。迁移保留 installed_at；旧分配统一视为待验证，补 generation，并绑定最新仍启用的安装待办、禁用其之前的同 Skill 安装待办。迁移不发送通知、不启动 Flow、不安装远端文件。

必须协调 Worker 接入本合同。旧 Worker 没有新回执时，安装待办会保持未验证/被拒绝完成，这是有意阻止假成功，不代表 Hub 消息传输失败。迁移后的历史待办文本可能仍是旧内容；Worker 应以 verification_target 和当前 manifest 识别控制操作，不执行旧文本里的共享目录安装命令。必要时由管理员重新分配生成新批次、新控制待办。

回滚应用时保留新增表列及回执记录，不执行破坏性删表。旧应用可能重新将分配当作安装，回滚不能视为安装验证仍生效。
