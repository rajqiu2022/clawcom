[Unit]
Description=OpenClaw Hub SSE Sidecar (claw __CLAW_ID__)
Documentation=http://clawteam.woa.com:18800/static/skills/hub-sse-sidecar/SKILL.md
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=__USER__
Environment=HOME=__HOME__
Environment=PATH=__PATH__
Environment=PYTHONUNBUFFERED=1
# __INSTALL_DIR__ 由 install.sh 渲染成本 claw 的实际工作目录
# （默认 __HOME__/.openclaw-sidecar，多实例场景会带 -claw-<id> 后缀）
Environment=OPENCLAW_SIDECAR_CONFIG=__INSTALL_DIR__/config.env
WorkingDirectory=__INSTALL_DIR__

ExecStart=/usr/bin/python3 -u __INSTALL_DIR__/scripts/sse_client.py

# sse_client.py 内置 90s 心跳 watchdog，断流会自己 exit；systemd 接管重启
Restart=always
RestartSec=10
# 防止疯狂 crash-loop（短时间 5 次内崩 → 暂停 60s）
StartLimitBurst=5
StartLimitIntervalSec=300

# 日志直接 append 到与 nohup 模式同一个文件，运维体验一致
StandardOutput=append:__INSTALL_DIR__/logs/sse_client.log
StandardError=append:__INSTALL_DIR__/logs/sse_client.log

# 资源约束（保守值，避免 sidecar 自身吃爆机器）
MemoryMax=512M
CPUQuota=50%

[Install]
WantedBy=multi-user.target
