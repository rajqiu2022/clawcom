# 长期记忆

## 项目：OpenClaw Agent Hub 通信中心
- **状态**: 已合并到 `f:/Code/claw_team/web/` 主系统
- **数据库**: 新增 Agent/Message/Conversation 模型到 `web/app/models.py`
- **API 路径**: `/api/v1/agent-hub/*`
- **Web 页面**: `/hub`（通信中心入口）
- **认证**: Bearer Token（OpenClaw 实例间通信用）
- **独立服务位置**: `f:/Code/claw_team/hub/`（已停用，可删除）
- **旧数据库**: `f:/Code/claw_team/hub/agent_hub.db`（不再使用）

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
