#!/bin/bash
# 生产 worker：在 Mac 上跑，连「公网服务器」领任务。
# 伙伴访问 http://129.204.166.13:8090 提交 → 这个 worker 在你 Mac 上爬 → 回传。
# 用法：bash run_worker.sh    （需保持 Mac 开机联网）
set -euo pipefail
cd "$(dirname "$0")"
: "${LEADGEN_WORKER_TOKEN:?请先通过环境变量配置 LEADGEN_WORKER_TOKEN}"
export LEADGEN_SERVER="${LEADGEN_SERVER:-http://129.204.166.13:8090}"
export LEADGEN_WORKER_TOKEN
pkill -f "worker/worker.py" 2>/dev/null
sleep 1
nohup python3 worker/worker.py > /tmp/leadgen_worker.log 2>&1 &
sleep 2
echo "✅ worker 已启动（连公网服务器 129.204.166.13:8090）pid=$!"
echo "   日志：/tmp/leadgen_worker.log"
echo "   伙伴访问：http://129.204.166.13:8090"
echo "   停止：pkill -f worker/worker.py"
