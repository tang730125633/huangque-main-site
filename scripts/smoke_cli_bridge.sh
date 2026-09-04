#!/usr/bin/env bash
# 视频 Agent 真实链路冒烟（在服务器上执行）：
#   内容服务 → 鉴权临时委托 → 无扣点 CLI 读取 → 自动撤销
# 用法：printf '%s' '<credentials-json>' | sudo bash smoke_cli_bridge.sh
#   JSON 仅包含 username 与 web_token，经 stdin 读取，不落盘、不作为 shell 代码解析；
#   HQ_INTERNAL_TOKEN 从 huangque-content 的 systemd 配置提取，不依赖
#   普通用户不可读的环境文件。
set -euo pipefail

# fd 0 随后承载 Python 源码；先把调用方的 JSON stdin 保留在 fd 3。
exec 3<&0

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
python3 - 3<&3 <<'PY'
import json
import os
import sys

sys.path.insert(0, "/home/ubuntu/content-api")
from content_domains import hq_cli_executor

with os.fdopen(3, encoding="utf-8") as stream:
    credentials = json.load(stream)
if not isinstance(credentials, dict) or set(credentials) != {"username", "web_token"}:
    raise SystemExit("冒烟凭证 JSON 字段无效")
username = credentials["username"]
web_token = credentials["web_token"]
if not isinstance(username, str) or not username or len(username) > 128:
    raise SystemExit("冒烟用户名无效")
if not isinstance(web_token, str) or not web_token or len(web_token) > 4096:
    raise SystemExit("冒烟 Web 会话令牌无效")

# 委托端点与报价核验端点在鉴权服务上：这条链路同时验证 auth 新端点、
# 内容服务的内部令牌与 CLI 运行包，且只做无扣点读取，执行后自动撤销授权。
result = hq_cli_executor.execute(
    "account", {},
    username=username,
    web_token=web_token,
    scopes=["profile:read"], confirm=False,
    auth_base="http://127.0.0.1:8095",
)
assert isinstance(result, dict), result
assert result.get("user", {}).get("username") == username, result
print("  链路冒烟通过：临时委托 → 无扣点 CLI 读取 → 撤销")
PY
