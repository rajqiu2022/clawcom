#!/bin/bash
# Memos 部署脚本
echo "停止并删除现有容器..."
docker stop memos 2>/dev/null
docker rm memos 2>/dev/null

echo "重新启动 Memos 容器..."
docker run -d --name memos -p 5230:5230 -v /data/memos:/var/opt/memos --restart unless-stopped ghcr.io/usememos/memos:latest

echo "等待容器启动..."
sleep 5

echo "检查容器状态:"
docker ps | grep memos

echo "检查端口映射:"
docker port memos

echo "从服务器内部测试:"
curl -I http://localhost:5230 2>/dev/null || echo "curl 测试失败，检查日志..."

echo "查看容器日志:"
docker logs memos --tail 5