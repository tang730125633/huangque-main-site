#!/usr/bin/env bash
# 本地开发：在本机跑 content-api + 静态站，改代码不用碰服务器。
# 用法：bash scripts/dev_local.sh
#   API   → http://127.0.0.1:8096   （content_api.py，零依赖，标准库即可）
#   静态站 → http://127.0.0.1:8097   （site/，workbench 页面）
# 需要 AI 能力时：复制 server/content.env 为 server/secret.local.env（已 gitignore，勿提交），
#   出站指向法兰克福中转即可，见文末。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

API_PORT="${API_PORT:-8096}"
WEB_PORT="${WEB_PORT:-8097}"
OUT_DIR="${CONTENT_OUT:-/tmp/hq_local_out}"
mkdir -p "$OUT_DIR"

# 可选：加载本地密钥（不进 git）
ENV_FILE="$ROOT/server/secret.local.env"
if [ -f "$ENV_FILE" ]; then set -a; . "$ENV_FILE"; set +a; echo "▸ 已加载本地密钥 $ENV_FILE"; else
  echo "▸ 未加载密钥（AI 能力将返回“未配置”，开发非 AI 逻辑无影响）"; fi

echo "▸ content-api → http://127.0.0.1:$API_PORT"
( cd "$ROOT/server" && CONTENT_API_PORT="$API_PORT" CONTENT_OUT="$OUT_DIR" python3 content_api.py ) &
API_PID=$!

echo "▸ 静态站 → http://127.0.0.1:$WEB_PORT/workbench/"
( cd "$ROOT/site" && python3 -m http.server "$WEB_PORT" >/dev/null 2>&1 ) &
WEB_PID=$!

trap 'kill $API_PID $WEB_PID 2>/dev/null || true' INT TERM EXIT
echo "▸ 就绪。Ctrl-C 停止。产物输出：$OUT_DIR"
wait
