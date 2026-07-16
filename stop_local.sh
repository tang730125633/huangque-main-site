#!/bin/bash
# 停止本地后端 + worker
pkill -f "uvicorn app:app" 2>/dev/null && echo "✅ 后端已停"
pkill -f "worker/worker.py" 2>/dev/null && echo "✅ worker 已停"
pkill -f "main.py --platform dy" 2>/dev/null
echo "完成。"
