#!/bin/bash
# ClawTeam 一键部署脚本
set -e

echo "=== ClawTeam 部署脚本 ==="

# 创建 .env 文件（如果不存在）
if [ ! -f .env ]; then
    echo "创建 .env 配置文件..."
    cat > .env << 'EOF'
FLASK_ENV=production
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_USER=your_mysql_user_or_pass
MYSQL_PASSWORD=your_mysql_user_or_pass
MYSQL_DATABASE=openclaw_manager
SECRET_KEY=clawteam-prod-secret-$(date +%s)
MEMOS_URL=http://your-hub-host:5230
MEMOS_API_KEY=
EOF
    echo ".env 文件已创建，请根据实际环境修改配置"
fi

# Docker 部署
if command -v docker &> /dev/null; then
    echo "检测到 Docker，使用容器化部署..."
    docker compose down 2>/dev/null || true
    docker compose up -d --build
    echo "=== 部署完成 ==="
    echo "访问: http://$(hostname -I | awk '{print $1}'):8088"
else
    echo "未检测到 Docker，使用直接部署..."
    
    # 安装依赖
    pip3 install -r requirements.txt
    
    # 启动
    source .env 2>/dev/null || true
    export FLASK_ENV=production
    gunicorn -w 4 -b 0.0.0.0:8088 --timeout 120 -D run:app
    
    echo "=== 部署完成 ==="
    echo "访问: http://$(hostname -I | awk '{print $1}'):8088"
fi
