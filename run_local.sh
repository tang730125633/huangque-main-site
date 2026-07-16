#!/bin/bash
# 本地一键启动：后端 + Mac worker，全在本机跑。
# 用法：bash run_local.sh    然后浏览器打开 http://localhost:8090
cd "$(dirname "$0")"
ROOT="$(pwd)"

echo "▶ 启动后端 (localhost:8090)…"
pkill -f "uvicorn app:app" 2>/dev/null
sleep 1
cd "$ROOT/server"
nohup "$ROOT/.venv/bin/uvicorn" app:app --host 127.0.0.1 --port 8090 > /tmp/leadgen_local_server.log 2>&1 &
cd "$ROOT"
sleep 3

echo "▶ 启动 Mac worker（领任务→爬取→过滤→回传）…"
pkill -f "worker/worker.py" 2>/dev/null
sleep 1
LEADGEN_SERVER=http://127.0.0.1:8090 nohup python3 worker/worker.py > /tmp/leadgen_local_worker.log 2>&1 &
sleep 2

echo
echo "✅ 已启动！"
echo "   网页：http://localhost:8090   （口令 meiye2026）"
echo "   后端日志：/tmp/leadgen_local_server.log"
echo "   worker日志：/tmp/leadgen_local_worker.log"
echo "   停止：bash stop_local.sh"
