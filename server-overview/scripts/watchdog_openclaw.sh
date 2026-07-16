#!/bin/bash
# OpenClaw 保活脚本 — 每分钟检查一次，挂了自动重启

export PATH="/usr/local/bin:/usr/bin:/bin:/home/ubuntu/.npm-global/bin"
export HOME="/home/ubuntu"

PORT=18789
LOG="/home/ubuntu/.openclaw/logs/watchdog.log"
OPENCLAW="/home/ubuntu/.npm-global/bin/openclaw"

# 检查端口是否在监听
ss -tlnp 2>/dev/null | grep -qE ":$PORT[[:space:]]"
if [ $? -ne 0 ]; then
    echo "[$(date "+%Y-%m-%d %H:%M:%S")] OpenClaw 挂了 (port $PORT)，正在重启..." >> $LOG
    nohup $OPENCLAW gateway --port $PORT > /tmp/openclaw-start.log 2>&1 &
    sleep 4
    ss -tlnp 2>/dev/null | grep -qE ":$PORT[[:space:]]"
    if [ $? -eq 0 ]; then
        echo "[$(date "+%Y-%m-%d %H:%M:%S")] ✅ 重启成功" >> $LOG
    else
        echo "[$(date "+%Y-%m-%d %H:%M:%S")] ❌ 重启失败" >> $LOG
    fi
fi
