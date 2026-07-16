#!/usr/bin/env bash
# 黄雀 AI 主站一键部署：rsync → 改属主 → 注入获客口令(从服务器 systemd env 读，不落 git)
# 用法：bash scripts/deploy_site.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
KEY="$HOME/.ssh/dapeng_server_ed25519"
HOST="dapeng-server"
WEBROOT="/var/www/huangquechuanmei"
SSH="ssh -i $KEY -o IdentitiesOnly=yes -o BatchMode=yes $HOST"

echo "▸ 1/3 rsync site/ → $HOST:$WEBROOT"
rsync -az --delete \
  --exclude '_cloud_src/' --exclude '_logo_gen/' --exclude '_preview/' --exclude 'assets_raw/' --exclude '.DS_Store' \
  --rsync-path="sudo rsync" \
  -e "ssh -i $KEY -o IdentitiesOnly=yes -o BatchMode=yes" \
  "$ROOT/site/" "$HOST:$WEBROOT/"

echo "▸ 2/3 改属主 www-data"
$SSH "sudo chown -R www-data:www-data $WEBROOT"

# ⚠️ 这份是【白名单】：新增 server/ 下的模块，必须同时加进这里和 drift_sentinel 的 BACKEND_RUNTIME。
# func_names.py 是 content 和 admin 【共用】的 —— 漏传它，两个服务一起 ImportError 起不来。
echo "▸ 3/3 部署内容后端 content_api.py + tikhub.py + func_names.py + 重启 huangque-content / huangque-admin"
rsync -az --rsync-path="sudo rsync" \
  -e "ssh -i $KEY -o IdentitiesOnly=yes -o BatchMode=yes" \
  "$ROOT/server/content_api.py" "$ROOT/server/tikhub.py" "$ROOT/server/func_names.py" \
  "$HOST:/home/ubuntu/content-api/"
$SSH "sudo systemctl restart huangque-content huangque-admin && sleep 1 && echo '  content:' \$(systemctl is-active huangque-content) '| admin:' \$(systemctl is-active huangque-admin)"

echo "✅ 部署完成 → https://huangquechuanmei.com"
