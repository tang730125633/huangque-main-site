#!/usr/bin/env bash
# 视频 Agent 真实链路冒烟（在服务器上执行）：
#   内容服务 → 鉴权临时委托 → 无扣点 CLI 读取 → 自动撤销
# 用法：sudo bash smoke_cli_bridge.sh /tmp/hq-smoke.env
#   env 文件（0600）需包含 HQ_SMOKE_USERNAME 与 HQ_SMOKE_WEB_TOKEN；
#   HQ_INTERNAL_TOKEN 从 huangque-content 的 systemd 配置提取，不依赖
#   普通用户不可读的环境文件。
set -euo pipefail

SMOKE_ENV="${1:-/tmp/hq-smoke.env}"
[ -f "$SMOKE_ENV" ] || { echo "缺少冒烟凭证文件：$SMOKE_ENV" >&2; exit 1; }
set -a; . "$SMOKE_ENV"; set +a
: "${HQ_SMOKE_USERNAME:?冒烟缺少 HQ_SMOKE_USERNAME}"
: "${HQ_SMOKE_WEB_TOKEN:?冒烟缺少 HQ_SMOKE_WEB_TOKEN}"

INTERNAL_TOKEN="$(sudo systemctl show -p Environment --value huangque-content 2>/dev/null \
  | tr ' ' '\n' | sed -n 's/^HQ_INTERNAL_TOKEN=//p' | head -1)"
if [ -z "$INTERNAL_TOKEN" ]; then
  ENV_FILE="$(sudo systemctl show -p EnvironmentFile --value huangque-content 2>/dev/null | head -1 | cut -d' ' -f1)"
  if [ -n "${ENV_FILE:-}" ] && [ -f "$ENV_FILE" ]; then
    INTERNAL_TOKEN="$(sudo grep -m1 '^HQ_INTERNAL_TOKEN=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true)"
  fi
fi
[ -n "$INTERNAL_TOKEN" ] || { echo "无法从 systemd 提取 HQ_INTERNAL_TOKEN" >&2; exit 1; }
export HQ_INTERNAL_TOKEN="$INTERNAL_TOKEN"

cd /home/ubuntu/content-api
HQ_SMOKE_USERNAME="$HQ_SMOKE_USERNAME" HQ_SMOKE_WEB_TOKEN="$HQ_SMOKE_WEB_TOKEN" \
  python3 - <<'PY'
import os
import sys

sys.path.insert(0, "/home/ubuntu/content-api")
from content_domains import hq_cli_executor

# 委托端点与报价核验端点在鉴权服务上：这条链路同时验证 auth 新端点、
# 内容服务的内部令牌与 CLI 运行包，且只做无扣点读取，执行后自动撤销授权。
result = hq_cli_executor.execute(
    "account", {},
    username=os.environ["HQ_SMOKE_USERNAME"],
    web_token=os.environ["HQ_SMOKE_WEB_TOKEN"],
    scopes=["profile:read"], confirm=False,
    auth_base="http://127.0.0.1:8095",
)
assert isinstance(result, dict), result
assert result.get("user", {}).get("username") == os.environ["HQ_SMOKE_USERNAME"], result
print("  链路冒烟通过：临时委托 → 无扣点 CLI 读取 → 撤销")
PY
