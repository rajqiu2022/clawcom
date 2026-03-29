# OpenClaw Manager 部署日志

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

*最后更新: 2026-03-26*
