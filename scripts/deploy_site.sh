#!/usr/bin/env bash
# 黄雀 AI 主站一键部署：rsync → 改属主 → 注入获客口令(从服务器 systemd env 读，不落 git)
# 用法：bash scripts/deploy_site.sh
# 可选环境变量：HQ_SMOKE_USERNAME + HQ_SMOKE_WEB_TOKEN 启用“临时委托 → 无扣点
# CLI 读取 → 撤销”的真实链路冒烟（凭证经 ssh stdin 传远端，不落盘、不进参数）。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
KEY="$HOME/.ssh/dapeng_server_ed25519"
HOST="dapeng-server"
WEBROOT="/var/www/huangquechuanmei"
AUTH_DIR="/home/ubuntu/auth-service"
CONTENT_DIR="/home/ubuntu/content-api"
NGINX_ACTIVE="/etc/nginx/sites-available/huangquechuanmei"
SSH="ssh -i $KEY -o IdentitiesOnly=yes -o BatchMode=yes $HOST"

echo "▸ 1/7 rsync site/ → $HOST:$WEBROOT"
rsync -az --delete \
  --exclude '_cloud_src/' --exclude '_logo_gen/' --exclude '_preview/' --exclude 'assets_raw/' --exclude '.DS_Store' \
  --rsync-path="sudo rsync" \
  -e "ssh -i $KEY -o IdentitiesOnly=yes -o BatchMode=yes" \
  "$ROOT/site/" "$HOST:$WEBROOT/"

echo "▸ 2/7 改属主 www-data"
$SSH "sudo chown -R www-data:www-data $WEBROOT"

# 视频 Agent 的委托端点 (/api/auth/internal/cli/delegate) 与报价核验端点
# (/api/auth/internal/cli/quote-claims) 都在鉴权服务上。它们必须与内容服务
# 同一次部署：先重启并验证 auth，再重启 content，否则新内容服务会全部在
# 鉴权侧失败。只部署 content 会形成“新内容服务 + 旧认证服务”的部分部署。
echo "▸ 3/7 部署鉴权后端并重启 huangque-auth"
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
echo "▸ 4/7 部署内容与管理后端"
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
# 真实链路冒烟脚本随发布同步，供本脚本与 ship 共用。
rsync -az --rsync-path="sudo rsync" \
  -e "ssh -i $KEY -o IdentitiesOnly=yes -o BatchMode=yes" \
  "$ROOT/scripts/smoke_cli_bridge.sh" "$HOST:/home/ubuntu/smoke_cli_bridge.sh"

echo "▸ 5/7 CLI 部署冒烟 + 重启 huangque-content / huangque-admin"
$SSH "cd $CONTENT_DIR; \
  PYTHONPATH=$CONTENT_DIR python3 -m hq_cli version --json >/dev/null || { echo '黄雀 CLI 模块部署预检失败' >&2; exit 1; }; \
  sudo systemctl restart huangque-content huangque-admin && sleep 1 && \
  test \"\$(systemctl is-active huangque-content)\" = active || { echo 'huangque-content 未激活' >&2; exit 1; }; \
  echo '  content:' \$(systemctl is-active huangque-content) '| admin:' \$(systemctl is-active huangque-admin)"

# Nginx 专用路由（视频助手上传限额/限速）必须随发布生效：备份 → 安装 →
# nginx -t 门禁 → reload；校验失败恢复备份，运行中的 nginx 不受影响。
echo "▸ 6/7 部署 nginx 配置（备份 → nginx -t → reload）"
rsync -az --rsync-path="sudo rsync" \
  -e "ssh -i $KEY -o IdentitiesOnly=yes -o BatchMode=yes" \
  "$ROOT/deploy/nginx-huangquechuanmei.conf" "$HOST:/tmp/huangquechuanmei.conf"
$SSH "sudo install -m 0644 '$NGINX_ACTIVE' '$NGINX_ACTIVE.rollback' && \
  sudo install -m 0644 /tmp/huangquechuanmei.conf '$NGINX_ACTIVE' && rm -f /tmp/huangquechuanmei.conf; \
  if ! sudo nginx -t; then \
    sudo install -m 0644 '$NGINX_ACTIVE.rollback' '$NGINX_ACTIVE'; \
    sudo nginx -t || true; \
    echo 'nginx 配置校验失败，已恢复备份' >&2; exit 1; \
  fi; \
  sudo systemctl reload nginx && echo '  nginx: reloaded'"

# 真实链路冒烟：内容服务 → 临时委托 → 无扣点 CLI 读取 → 自动撤销。
# 需要真实用户会话，仅在显式提供凭证时执行；通用 health 不能证明这条链路。
echo "▸ 7/7 真实链路冒烟"
if [ -n "${HQ_SMOKE_USERNAME:-}" ] && [ -n "${HQ_SMOKE_WEB_TOKEN:-}" ]; then
  # 凭证经 stdin 写成远端 0600 临时文件，避免进入远端进程参数；冒烟脚本用后即删。
  printf 'HQ_SMOKE_USERNAME=%s\nHQ_SMOKE_WEB_TOKEN=%s\n' \
    "$HQ_SMOKE_USERNAME" "$HQ_SMOKE_WEB_TOKEN" \
    | $SSH "umask 077; cat > /tmp/hq-smoke.env"
  $SSH "trap 'rm -f /tmp/hq-smoke.env' EXIT; sudo bash /home/ubuntu/smoke_cli_bridge.sh /tmp/hq-smoke.env"
else
  echo "  未提供 HQ_SMOKE_USERNAME / HQ_SMOKE_WEB_TOKEN，跳过真实链路冒烟（内部门禁预检已在第 3 步通过）"
fi

echo "✅ 部署完成 → https://huangquechuanmei.com"
