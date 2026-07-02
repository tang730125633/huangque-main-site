#!/usr/bin/env bash
# 黄雀 AI 主站一键部署：rsync → 改属主 → 注入获客口令(从服务器 systemd env 读，不落 git)
# 用法：bash scripts/deploy_site.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
KEY="$HOME/.ssh/dapeng_server_ed25519"
HOST="dapeng-server"
WEBROOT="/var/www/huangquechuanmei"
SSH="ssh -i $KEY -o IdentitiesOnly=yes -o BatchMode=yes $HOST"

echo "▸ 1/5 同步 API 文档规格 docs/api → site/api-docs"
cp "$ROOT/docs/api/openapi.json" "$ROOT/site/api-docs/openapi.json"
cp "$ROOT/docs/api/openapi-tikhub.json" "$ROOT/site/api-docs/openapi-tikhub.json"

echo "▸ 2/5 rsync site/ → $HOST:$WEBROOT"
rsync -az --delete \
  --exclude '_cloud_src/' --exclude '_logo_gen/' --exclude '_preview/' --exclude 'assets_raw/' --exclude '.DS_Store' \
  --rsync-path="sudo rsync" \
  -e "ssh -i $KEY -o IdentitiesOnly=yes -o BatchMode=yes" \
  "$ROOT/site/" "$HOST:$WEBROOT/"

echo "▸ 3/5 改属主 www-data"
$SSH "sudo chown -R www-data:www-data $WEBROOT"

echo "▸ 4/5 部署认证后端 auth_server.py + 重启 huangque-auth"
rsync -az --rsync-path="sudo rsync" \
  -e "ssh -i $KEY -o IdentitiesOnly=yes -o BatchMode=yes" \
  "$ROOT/server/auth_server.py" "$HOST:/home/ubuntu/auth-service/"
$SSH "sudo systemctl restart huangque-auth && sleep 1 && echo '  auth-api:' \$(systemctl is-active huangque-auth)"

echo "▸ 5/5 部署内容后端 content_api.py + tikhub.py + 重启 huangque-content"
rsync -az --rsync-path="sudo rsync" \
  -e "ssh -i $KEY -o IdentitiesOnly=yes -o BatchMode=yes" \
  "$ROOT/server/content_api.py" "$ROOT/server/tikhub.py" "$HOST:/home/ubuntu/content-api/"
$SSH "sudo systemctl restart huangque-content && sleep 1 && echo '  content-api:' \$(systemctl is-active huangque-content)"

echo "✅ 部署完成 → https://huangquechuanmei.com"
