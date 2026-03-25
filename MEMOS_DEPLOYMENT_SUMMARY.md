# Memos 知识库部署问题总结与解决方案

## 项目背景
- **项目**: OpenClaw 游戏测试经理角色工程
- **需求**: 部署 Memos 作为团队知识库，替代 OpenViking
- **服务器**: 腾讯云轻量应用服务器 (9.134.11.169:36000)
- **目标端口**: 5230（后改为 5231）

## 问题时间线

### 第一阶段：SSH 连接问题
**问题描述**：
- 初始尝试通过 WorkBuddy 直接 SSH 连接服务器失败
- 网络环境限制导致无法建立外部连接

**解决方案**：
- 用户手动在服务器执行部署命令
- 采用本地 SSH 客户端完成连接

### 第二阶段：端口映射异常
**问题描述**：
1. 容器运行正常（`docker ps` 显示 Up 状态）
2. 容器内服务正常（日志显示 `Version 0.24.0 has been started on port 5230`）
3. **但** `docker port memos` 输出为空
4. 浏览器无法访问 `http://9.134.11.169:5230`

**排查过程**：
```bash
# 检查容器状态
docker ps -a | grep memos  # 显示 Up 状态

# 检查容器日志
docker logs memos --tail 10  # 显示服务已启动

# 检查端口映射
docker port memos  # 输出为空

# 检查容器内网络
docker exec memos netstat -tulpn | grep 5230  # 显示监听中

# 检查容器详细信息
docker inspect memos | grep -A 10 "Ports"  # 显示 "Ports": null
```

### 第三阶段：根本原因发现
**关键发现**：
```bash
# 检查 Docker 网络
docker network ls

# 输出：
# NETWORK ID          NAME                DRIVER              SCOPE
# 54dacb7a6df1        host                host                local
# f12d3b043032        none                null                local
```

**问题根本原因**：
1. **缺少 bridge 网络**：Docker 的默认 `bridge` 网络不存在
2. **网络配置异常**：容器只能使用 `host` 或 `none` 网络
3. **端口映射依赖 bridge**：`-p 端口映射` 功能需要 bridge 网络支持

## 解决方案

### 方案一：使用 host 网络模式（最终采用）
```bash
# 停止并删除旧容器
docker stop memos && docker rm memos

# 使用 host 网络启动
docker run -d \
  --name memos \
  --network host \  # 关键：使用主机网络
  -v /data/memos:/var/opt/memos \
  --restart unless-stopped \
  ghcr.io/usememos/memos:latest
```

**优点**：
- 容器直接使用主机网络栈
- 无需端口映射，容器内端口 = 主机端口
- 简单直接，避免网络配置问题

### 方案二：创建自定义 bridge 网络
```bash
# 创建 bridge 网络
docker network create --driver bridge memos-bridge

# 使用自定义网络启动
docker run -d \
  --name memos \
  --network memos-bridge \
  -p 5231:5230 \
  -v /data/memos:/var/opt/memos \
  --restart unless-stopped \
  ghcr.io/usememos/memos:latest
```

### 方案三：修复 Docker 网络（高级）
```bash
# 停止 Docker 服务
systemctl stop docker

# 清理网络配置
rm -rf /var/lib/docker/network/

# 重启 Docker
systemctl start docker

# 重新创建默认网络
docker network create --driver bridge bridge
```

## 经验总结

### 1. Docker 网络排查要点
- **检查网络列表**：`docker network ls` 查看可用网络
- **验证端口映射**：`docker port <容器名>` 确认映射生效
- **容器内检查**：`docker exec <容器名> netstat -tulpn` 确认服务监听
- **详细配置**：`docker inspect <容器名>` 查看完整配置

### 2. 常见端口映射问题
- **端口占用**：`netstat -tulpn | grep <端口>` 检查冲突
- **防火墙限制**：检查 iptables/ufw/firewalld 规则
- **安全组设置**：云服务商防火墙需单独配置

### 3. 备选部署方案
- **二进制安装**：直接下载 Memos 二进制文件，无需 Docker
- **docker-compose**：使用 docker-compose.yml 统一管理配置
- **更换端口**：如 5230 有问题，可尝试 5231、5232 等端口

## 最终配置
- **访问地址**: `http://9.134.11.169:5230`
- **网络模式**: host
- **数据持久化**: `/data/memos` → `/var/opt/memos`
- **重启策略**: unless-stopped

## 后续步骤
1. **创建管理员账号**：首次访问 Memos 页面
2. **生成 API Key**：设置 → API Key → 生成
3. **配置 OpenClaw MCP**：使用 API Key 连接 memos-mcp
4. **团队知识库迁移**：将现有知识导入 Memos

## 技术要点备忘
- **Memos 版本**: 0.24.0
- **数据库**: SQLite (`/var/opt/memos/memos_prod.db`)
- **运行模式**: prod
- **默认端口**: 5230
- **数据目录**: `/var/opt/memos` (容器内)

---
*文档生成时间: 2026-03-24*
*部署完成时间: 2026-03-24*
*问题解决: 使用 --network host 模式*