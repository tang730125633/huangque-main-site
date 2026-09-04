#!/usr/bin/env bash
# 黄雀 AI 主站一键部署：rsync → 改属主 → 注入获客口令(从服务器 systemd env 读，不落 git)
# 用法：bash scripts/deploy_site.sh
# 可选环境变量：HQ_SMOKE_USERNAME + HQ_SMOKE_WEB_TOKEN 启用“临时委托 → 无扣点
# CLI 读取 → 撤销”的真实链路冒烟；HQ_ENV_FILE 覆盖服务器环境文件路径。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
KEY="$HOME/.ssh/dapeng_server_ed25519"
HOST="dapeng-server"
WEBROOT="/var/www/huangquechuanmei"
AUTH_DIR="/home/ubuntu/auth-service"
CONTENT_DIR="/home/ubuntu/content-api"
SSH="ssh -i $KEY -o IdentitiesOnly=yes -o BatchMode=yes $HOST"

echo "▸ 1/6 rsync site/ → $HOST:$WEBROOT"
rsync -az --delete \
  --exclude '_cloud_src/' --exclude '_logo_gen/' --exclude '_preview/' --exclude 'assets_raw/' --exclude '.DS_Store' \
  --rsync-path="sudo rsync" \
  -e "ssh -i $KEY -o IdentitiesOnly=yes -o BatchMode=yes" \
  "$ROOT/site/" "$HOST:$WEBROOT/"

echo "▸ 2/6 改属主 www-data"
$SSH "sudo chown -R www-data:www-data $WEBROOT"

# 视频 Agent 的委托端点 (/api/auth/internal/cli/delegate) 与报价核验端点
# (/api/auth/internal/cli/quote-claims) 都在鉴权服务上。它们必须与内容服务
# 同一次部署：先重启并验证 auth，再重启 content，否则新内容服务会全部在
# 鉴权侧失败。只部署 content 会形成“新内容服务 + 旧认证服务”的部分部署。
echo "▸ 3/6 部署鉴权后端并重启 huangque-auth"
rsync -az --rsync-path="sudo rsync" \
  -e "ssh -i $KEY -o IdentitiesOnly=yes -o BatchMode=yes" \
  "$ROOT/server/auth_server.py" "$ROOT/server/hq_cli_api.py" \
  "$HOST:$AUTH_DIR/"
$SSH "sudo systemctl restart huangque-auth && sleep 1 && \
  test \"\$(systemctl is-active huangque-auth)\" = active || { echo 'huangque-auth 未激活' >&2; exit 1; }; \
  code=\$(curl -s -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:8095/api/auth/internal/cli/delegate -H 'Content-Type: application/json' -d '{}'); \
  [ \"\$code\" = 403 ] || { echo \"delegate 内部门禁预检失败：HTTP \$code\" >&2; exit 1; }; \
  code=\$(curl -s -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:8095/api/auth/internal/cli/quote-claims -H 'Content-Type: application/json' -d '{}'); \
  [ \"\$code\" = 403 ] || { echo \"quote-claims 内部门禁预检失败：HTTP \$code\" >&2; exit 1; }; \
  echo '  auth: active | delegate / quote-claims 内部门禁预检通过'"

# 顶层运行文件继续使用白名单；content_domains 是 content_api 的完整运行包，必须整体同步。
# 只传 content_api.py 会造成“前端已上线、后端路由仍是旧版”的部分部署。
# func_names.py 是 content 和 admin 【共用】的 —— 漏传它，两个服务一起 ImportError 起不来。
echo "▸ 4/6 部署内容与管理后端"
rsync -az --rsync-path="sudo rsync" \
  -e "ssh -i $KEY -o IdentitiesOnly=yes -o BatchMode=yes" \
  "$ROOT/server/content_api.py" "$ROOT/server/admin_api.py" \
  "$ROOT/server/tikhub.py" "$ROOT/server/func_names.py" \
  "$HOST:$CONTENT_DIR/"
rsync -az --rsync-path="sudo rsync" \
  -e "ssh -i $KEY -o IdentitiesOnly=yes -o BatchMode=yes" \
  "$ROOT/server/content_domains/" "$HOST:$CONTENT_DIR/content_domains/"
# video Agent 通过 python3 -m hq_cli 调用；生产 content-api 目录不是完整 Git
# checkout，因此必须把可导入包同步到固定路径，并删掉已移除的旧模块。
rsync -az --delete --exclude '__pycache__/' --exclude '*.pyc' --rsync-path="sudo rsync" \
  -e "ssh -i $KEY -o IdentitiesOnly=yes -o BatchMode=yes" \
  "$ROOT/tools/hq-cli/src/hq_cli/" "$HOST:$CONTENT_DIR/hq_cli/"

echo "▸ 5/6 CLI 部署冒烟 + 重启 huangque-content / huangque-admin"
$SSH "cd $CONTENT_DIR; \
  PYTHONPATH=$CONTENT_DIR python3 -m hq_cli version --json >/dev/null || { echo '黄雀 CLI 模块部署预检失败' >&2; exit 1; }; \
  sudo systemctl restart huangque-content huangque-admin && sleep 1 && \
  test \"\$(systemctl is-active huangque-content)\" = active || { echo 'huangque-content 未激活' >&2; exit 1; }; \
  echo '  content:' \$(systemctl is-active huangque-content) '| admin:' \$(systemctl is-active huangque-admin)"

# 真实链路冒烟：内容服务 → 临时委托 → 无扣点 CLI 读取 → 自动撤销。
# 需要真实用户会话，仅在显式提供凭证时执行；通用 health 不能证明这条链路。
echo "▸ 6/6 真实链路冒烟"
if [ -n "${HQ_SMOKE_USERNAME:-}" ] && [ -n "${HQ_SMOKE_WEB_TOKEN:-}" ]; then
  HQ_SMOKE_USERNAME="$HQ_SMOKE_USERNAME" HQ_SMOKE_WEB_TOKEN="$HQ_SMOKE_WEB_TOKEN" \
    $SSH "bash -s" <<'SMOKE'
set -euo pipefail
cd /home/ubuntu/content-api
set -a; . "${HQ_ENV_FILE:-/home/ubuntu/content-api/content.env}" 2>/dev/null || true; set +a
HQ_SMOKE_USERNAME="$HQ_SMOKE_USERNAME" HQ_SMOKE_WEB_TOKEN="$HQ_SMOKE_WEB_TOKEN" python3 - <<'PY'
import os
import sys

sys.path.insert(0, "/home/ubuntu/content-api")
from content_domains import hq_cli_executor

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
SMOKE
else
  echo "  未提供 HQ_SMOKE_USERNAME / HQ_SMOKE_WEB_TOKEN，跳过真实链路冒烟（内部门禁预检已在第 3 步通过）"
fi

echo "✅ 部署完成 → https://huangquechuanmei.com"
