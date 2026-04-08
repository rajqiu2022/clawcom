# 用例快照管理 (snapshot-manager)

## 简介

本 Skill 用于管理用例库的版本快照，类似 Git 的 commit/log/checkout/diff 操作：

- **创建快照**：保存用例库当前状态
- **版本历史**：查看所有快照记录
- **回滚用例**：恢复到任意历史版本
- **版本对比**：Diff 两个版本的差异
- **标签管理**：给快照打标签（如 release-v1.0）

**触发词**：快照、版本历史、回滚用例、版本对比、用例库备份

---

## 前置条件

| 变量 | 说明 |
|------|------|
| `HUB_URL` | Hub 服务地址 |
| `HUB_API_TOKEN` | API Token |
| `LIBRARY_ID` | 用例库 ID |

---

## API 接口

### 1. 创建快照

```
POST /api/v1/testcase-libraries/{library_id}/snapshots

请求体：
{
  "message": "完成核心单局用例编写",       // 必填，快照说明
  "snapshot_type": "manual",              // manual（手动）/ auto（自动）
  "created_by": "龙虾王"                   // 创建者
}

返回：
{
  "id": 5,
  "library_id": 1,
  "version": "v5",
  "message": "完成核心单局用例编写",
  "snapshot_type": "manual",
  "case_count": 42,
  "created_by": "龙虾王",
  "created_at": "2026-04-08T15:30:00"
}
```

### 2. 版本历史

```
GET /api/v1/testcase-libraries/{library_id}/snapshots

查询参数：
  snapshot_type=manual    按类型筛选
  limit=50                返回条数

返回：快照列表（按版本倒序）
[
  {
    "id": 5,
    "version": "v5",
    "message": "完成核心单局用例编写",
    "snapshot_type": "manual",
    "case_count": 42,
    "tag": "release-v1.0",
    "created_by": "龙虾王",
    "created_at": "2026-04-08T15:30:00"
  }
]
```

### 3. 获取快照详情

```
GET /api/v1/testcase-libraries/{library_id}/snapshots/{version}

返回：快照信息 + 用例数据（完整的 cases_json）
```

### 4. 回滚到历史版本

```
POST /api/v1/testcase-libraries/{library_id}/snapshots/{version}/checkout

请求体（可选）：
{
  "create_backup": true    // 默认 true，回滚前自动备份当前版本
}

逻辑：
1. 如果 create_backup=true → 先创建当前状态的备份快照
2. 删除当前所有用例
3. 从快照的 cases_json 恢复所有用例
4. 重建思维导图

返回：
{
  "message": "已回滚到 v3",
  "restored_cases": 35,
  "backup_version": "v6"
}
```

### 5. 版本对比（Diff）

```
GET /api/v1/testcase-libraries/{library_id}/snapshots/diff

查询参数：
  from=v3      起始版本（可选，默认上一个版本）
  to=v5        目标版本（可选，默认最新版本）
  with_current=true  与当前用例库对比（忽略 to 参数）

返回：
{
  "from_version": "v3",
  "to_version": "v5",
  "summary": { "added": 5, "removed": 2, "modified": 3 },
  "details": {
    "added": [{ "case_id": "TC_042", "title": "新增用例标题" }],
    "removed": [{ "case_id": "TC_010", "title": "被删除的用例" }],
    "modified": [{
      "case_id": "TC_005",
      "title": "被修改的用例",
      "changes": { "priority": {"old": "P2", "new": "P1"}, "content.steps": "changed" }
    }]
  }
}
```

### 6. 给快照打标签

```
PUT /api/v1/testcase-libraries/{library_id}/snapshots/{version}/tag

请求体：
{
  "tag": "release-v1.0"
}
```

---

## 推荐工作流

### 日常备份

```
1. 每次大批量修改用例前 → POST /snapshots {"message": "修改前备份"}
2. 批量操作完成后 → POST /snapshots {"message": "完成 XX 模块用例编写"}
3. 发版前 → POST /snapshots + PUT /tag {"tag": "release-v2.0"}
```

### 回滚恢复

```
1. 发现用例被误删/改坏
2. GET /snapshots → 找到正确的版本
3. GET /snapshots/diff?from=v3&with_current=true → 确认差异
4. POST /snapshots/v3/checkout → 回滚
```

### 审计对比

```
1. GET /snapshots/diff?from=v1&to=v5 → 查看两个版本间的所有变更
2. 确认新增/删除/修改了哪些用例
3. 用于版本评审或质量检查
```

---

## 触发词速查

| 触发词 | 对应操作 |
|--------|----------|
| 创建快照 / 备份用例库 | POST /snapshots |
| 版本历史 / 快照列表 | GET /snapshots |
| 回滚用例 / 恢复版本 | POST /snapshots/{v}/checkout |
| 版本对比 / Diff | GET /snapshots/diff |
| 打标签 / 标记版本 | PUT /snapshots/{v}/tag |
| 查看快照详情 | GET /snapshots/{v} |
