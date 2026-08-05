#!/usr/bin/env bash
# 黄雀开发服务器一键部署：在全新阿里云轻量 Ubuntu 22.04 上复刻一套独立开发环境。
# 用法（admin 用户执行）:  bash setup-dev-server.sh <名字>     例: bash setup-dev-server.sh fang
# 效果: https://<名字>.huangquechuanmei.com = 完整前端 + 六个后端服务 + 全新独立测试库
# 前提: ① 子域名 A 记录已解析到本机  ② 本机部署公钥已加为仓库只读 Deploy Key（脚本会引导）
set -euo pipefail

NAME="${1:?用法: bash setup-dev-server.sh <名字>  例: fang}"
DOMAIN="$NAME.huangquechuanmei.com"
REPO="git@github.com:tang730125633/huangque-main-site.git"
R="$HOME/huangque-main-site"
EMAIL="${CERT_EMAIL:-}"
DEV_PASSWORD="${DEV_TEST_PASSWORD:-$(openssl rand -base64 18 | tr -d '/+=')}"

echo "== [1/8] 基础软件 =="
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  nginx git sqlite3 certbot python3-certbot-nginx rsync psmisc python3-pip ffmpeg >/dev/null
# COS 存储 SDK：缺它则音色试听/作图/视频成片存不进腾讯云 COS、回退本地私有地址而无法访问。
# 必须装到服务实际用的解释器 /usr/bin/python3（服务以 ubuntu 用户跑该 python）。
sudo /usr/bin/python3 -m pip install -q cos-python-sdk-v5 2>/dev/null \
  || sudo /usr/bin/python3 -m pip install -q cos-python-sdk-v5 --break-system-packages 2>/dev/null || true

echo "== [2/8] 仓库访问（只读 Deploy Key）=="
[ -f ~/.ssh/id_ed25519 ] || ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N "" -C "deploy-$NAME-dev" -q
if ! GIT_SSH_COMMAND="ssh -o StrictHostKeyChecking=accept-new" git ls-remote -q "$REPO" >/dev/null 2>&1; then
  echo "❌ 还连不上仓库。把下面这行公钥发给 Tang，加为仓库 Deploy Key（只读），然后重跑本脚本："
  cat ~/.ssh/id_ed25519.pub
  exit 1
fi
[ -d "$R/.git" ] || git clone -q "$REPO" "$R"
git -C "$R" pull -q --ff-only

echo "== [3/8] ubuntu 用户与目录（照抄生产布局）=="
sudo useradd -m -s /bin/bash ubuntu 2>/dev/null || true
sudo mkdir -p /home/ubuntu/{auth-service,content-api,dl-service} /etc/huangque
sudo mkdir -p /home/ubuntu/auth-service/content_domains
sudo cp "$R"/server/auth_server.py "$R"/server/hq_cli_api.py "$R"/server/wechat_subscribe.py "$R"/server/invites.py "$R"/server/invite_network.py "$R"/server/business_cards.py "$R"/server/wxpay.py /home/ubuntu/auth-service/
sudo cp "$R"/scripts/process_invite_reward_claims.py /home/ubuntu/auth-service/
sudo cp "$R"/server/content_domains/__init__.py "$R"/server/content_domains/cos.py "$R"/server/content_domains/miniprogram_security.py "$R"/server/content_domains/pricing.py /home/ubuntu/auth-service/content_domains/
sudo cp "$R"/server/content_api.py "$R"/server/leadgen_api.py "$R"/server/imggen_api.py \
  "$R"/server/admin_api.py "$R"/server/tikhub.py "$R"/server/func_names.py /home/ubuntu/content-api/
sudo mkdir -p /home/ubuntu/content-api/content_domains
sudo rsync -a --delete "$R"/server/content_domains/ /home/ubuntu/content-api/content_domains/
sudo mkdir -p /home/ubuntu/content-api/providers
sudo rsync -a --delete "$R"/server/providers/ /home/ubuntu/content-api/providers/
sudo cp "$R"/server/dl_service.py /home/ubuntu/dl-service/

echo "== [4/8] 环境文件（全新随机 token；API Key 留空待各自独立 Key）=="
if ! sudo test -f /home/ubuntu/auth-service/auth.env; then
  TOKEN=$(openssl rand -hex 24)
  printf "HQ_INTERNAL_TOKEN=%s\nHQ_AUTH_TOKEN_TTL=2592000\n" "$TOKEN" | sudo tee /home/ubuntu/auth-service/auth.env >/dev/null
  printf "HQ_INTERNAL_TOKEN=%s\n# 国外 API 走新加坡出境中转 sg.huangquechuanmei.com（本机公网 IP 需在其 nginx 白名单内）\nGEMINI_BASE=https://sg.huangquechuanmei.com/gemini\nOPENAI_BASE=https://sg.huangquechuanmei.com/openai\nHEYGEN_API_BASE=https://sg.huangquechuanmei.com/v3\nHEYGEN_RELAY_BASE=https://sg.huangquechuanmei.com\nXAI_API_BASE=https://sg.huangquechuanmei.com/xai/v1\nNO_PROXY=127.0.0.1,localhost,::1\n# 独立 API Key 发下来后填在这里\nOPENAI_API_KEY=\nGEMINI_API_KEY=\nTIKHUB_KEY=\n" "$TOKEN" | sudo tee /home/ubuntu/content-api/content.env >/dev/null
fi
if sudo grep -qE '^(WX_PAY_|WX_MP_)' /home/ubuntu/auth-service/auth.env /home/ubuntu/content-api/content.env; then
  echo "❌ dev 环境禁止配置 WX_PAY_/WX_MP_，请移除支付变量后重跑"
  exit 1
fi
sudo test -f /etc/huangque/runninghub.env || printf "RUNNINGHUB_API_KEY=\n" | sudo tee /etc/huangque/runninghub.env >/dev/null
sudo chmod 600 /home/ubuntu/auth-service/auth.env /home/ubuntu/content-api/content.env /etc/huangque/runninghub.env
sudo chown -R ubuntu:ubuntu /home/ubuntu

echo "== [5/8] systemd 六服务（含 drop-in）=="
sudo cp "$R"/deploy/systemd/huangque-{auth,content,dl,imggen-api,leadgen-api,admin}.service /etc/systemd/system/
sudo cp "$R"/deploy/systemd/huangque-invite-reward-claims.{service,timer} /etc/systemd/system/
sudo cp -r "$R"/deploy/systemd/huangque-{auth,content,dl,imggen-api,leadgen-api,admin}.service.d /etc/systemd/system/ 2>/dev/null || true
sudo rm -f /etc/systemd/system/*.service.d/*.example
sudo systemctl daemon-reload
sudo systemctl enable --now huangque-auth huangque-content huangque-dl \
  huangque-imggen-api huangque-leadgen-api huangque-admin
sudo systemctl enable --now huangque-invite-reward-claims.timer
sleep 3

echo "== [6/8] 前端 + nginx（域名替换为 $DOMAIN）=="
sudo mkdir -p /var/www/huangquechuanmei
sudo rsync -a --delete "$R"/site/ /var/www/huangquechuanmei/
sudo test -f /etc/nginx/.htpasswd-hq || { printf "hq:%s\n" "$(openssl passwd -apr1 "$DEV_PASSWORD")" | sudo tee /etc/nginx/.htpasswd-hq >/dev/null; sudo chmod 640 /etc/nginx/.htpasswd-hq; sudo chown root:www-data /etc/nginx/.htpasswd-hq; }
sed -e "s/server_name huangquechuanmei.com www.huangquechuanmei.com;/server_name $DOMAIN;/g" \
    -e "s#/etc/letsencrypt/live/huangquechuanmei.com/#/etc/letsencrypt/live/$DOMAIN/#g" \
    "$R"/deploy/nginx-huangquechuanmei.conf | sudo tee /etc/nginx/sites-available/huangquechuanmei >/dev/null
sudo ln -sf /etc/nginx/sites-available/huangquechuanmei /etc/nginx/sites-enabled/huangquechuanmei
sudo rm -f /etc/nginx/sites-enabled/default

echo "== [7/8] HTTPS 证书（要求 $DOMAIN 已解析到本机）=="
if ! sudo test -d "/etc/letsencrypt/live/$DOMAIN"; then
  [ -n "${CERT_EMAIL:-}" ] || { echo "❌ 首次签发证书前请设置 CERT_EMAIL=你的邮箱"; exit 1; }
  # 先用无证书的临时配置起 nginx 供 certbot 验证
  sudo sed -i "s/listen 443 ssl/listen 443/; s/listen \[::\]:443 ssl ipv6only=on/listen [::]:443/; /ssl_certificate/d; /ssl_dhparam/d; /include \/etc\/letsencrypt/d" /etc/nginx/sites-available/huangquechuanmei
  sudo nginx -t && sudo systemctl reload nginx
  sudo certbot --nginx -d "$DOMAIN" -n --agree-tos -m "$EMAIL"
fi
sudo nginx -t && sudo systemctl reload nginx

echo "== [8/8] 自检 + 独立测试账号 =="
sudo -u ubuntu env DEV_TEST_PASSWORD="$DEV_PASSWORD" python3 - <<'PYEOF'
import os
import sys

sys.path.insert(0, "/home/ubuntu/auth-service")
import auth_server

password = os.environ["DEV_TEST_PASSWORD"]
for username in ["test01", "test02", "test03", "test04", "test05"]:
    try:
        auth_server.create_user(username, password, points=100000)
    except Exception as exc:
        print(f"  {username}: {exc}")
try:
    auth_server.create_user("admin01", password, points=100000, role="admin")
except Exception as exc:
    print(f"  admin01: {exc}")
PYEOF
echo "---- 验收 ----"
systemctl is-active huangque-auth huangque-content huangque-dl \
  huangque-imggen-api huangque-leadgen-api huangque-admin
for port in 8095 8096 8097 8098 8100 8101; do
  curl -s -o /dev/null -w "127.0.0.1:$port -> %{http_code}\n" "http://127.0.0.1:$port/" || true
done
curl -s -o /dev/null -w "首页:   %{http_code}\n" "https://$DOMAIN/"
curl -s -o /dev/null -w "后端:   %{http_code}\n" "https://$DOMAIN/api/gen/health"
echo "✅ 完成: https://$DOMAIN"
echo "测试账号: test01~test05 / admin01"
echo "本次 dev 密码: $DEV_PASSWORD（用于 hq 基础认证和测试账号；请立即保存）"
echo "AI 功能需在 content.env 填入独立 dev Key 后重启对应服务；禁止复制生产 Key。"
