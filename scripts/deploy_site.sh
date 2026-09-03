#!/usr/bin/env bash
# 黄雀 AI 主站一键部署：rsync → 改属主 → 注入获客口令(从服务器 systemd env 读，不落 git)
# 用法：bash scripts/deploy_site.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
KEY="$HOME/.ssh/dapeng_server_ed25519"
HOST="dapeng-server"
WEBROOT="/var/www/huangquechuanmei"
SSH="ssh -i $KEY -o IdentitiesOnly=yes -o BatchMode=yes $HOST"

echo "▸ 1/4 rsync site/ → $HOST:$WEBROOT"
rsync -az --delete \
  --exclude '_cloud_src/' --exclude '_logo_gen/' --exclude '_preview/' --exclude 'assets_raw/' --exclude '.DS_Store' \
  --rsync-path="sudo rsync" \
  -e "ssh -i $KEY -o IdentitiesOnly=yes -o BatchMode=yes" \
  "$ROOT/site/" "$HOST:$WEBROOT/"

echo "▸ 2/4 改属主 www-data"
$SSH "sudo chown -R www-data:www-data $WEBROOT"

# 顶层运行文件继续使用白名单；content_domains 是 content_api 的完整运行包，必须整体同步。
# 只传 content_api.py 会造成“前端已上线、后端路由仍是旧版”的部分部署。
# func_names.py 是 content 和 admin 【共用】的 —— 漏传它，两个服务一起 ImportError 起不来。
echo "▸ 3/4 部署内容与管理后端"
rsync -az --rsync-path="sudo rsync" \
  -e "ssh -i $KEY -o IdentitiesOnly=yes -o BatchMode=yes" \
  "$ROOT/server/content_api.py" "$ROOT/server/admin_api.py" \
  "$ROOT/server/tikhub.py" "$ROOT/server/func_names.py" \
  "$HOST:/home/ubuntu/content-api/"
rsync -az --rsync-path="sudo rsync" \
  -e "ssh -i $KEY -o IdentitiesOnly=yes -o BatchMode=yes" \
  "$ROOT/server/content_domains/" "$HOST:/home/ubuntu/content-api/content_domains/"
# video Agent 通过 python3 -m hq_cli 调用；生产 content-api 目录不是完整 Git
# checkout，因此必须把可导入包同步到固定路径，并删掉已移除的旧模块。
rsync -az --delete --exclude '__pycache__/' --exclude '*.pyc' --rsync-path="sudo rsync" \
  -e "ssh -i $KEY -o IdentitiesOnly=yes -o BatchMode=yes" \
  "$ROOT/tools/hq-cli/src/hq_cli/" "$HOST:/home/ubuntu/content-api/hq_cli/"

echo "▸ 4/4 CLI 部署冒烟 + 重启 huangque-content / huangque-admin"
$SSH "cd /home/ubuntu/content-api; \
  PYTHONPATH=/home/ubuntu/content-api python3 -m hq_cli version --json >/dev/null || { echo '黄雀 CLI 模块部署预检失败' >&2; exit 1; }; \
  sudo systemctl restart huangque-content huangque-admin && sleep 1 && \
  echo '  content:' \$(systemctl is-active huangque-content) '| admin:' \$(systemctl is-active huangque-admin)"

echo "✅ 部署完成 → https://huangquechuanmei.com"
