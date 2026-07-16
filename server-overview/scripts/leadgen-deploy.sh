#!/usr/bin/env bash
# 蓝绿滚动更新：更新代码不掉线；关键=确认一台真回到轮询、在接客了，才去动另一台(堵住空档)
set -uo pipefail
# 健康检查：实例自己的端口能否200
health() { for i in $(seq 1 12); do [ "$(curl -s -m5 -o /dev/null -w '%{http_code}' http://127.0.0.1:$1/api/keywords)" = "200" ] && return 0; sleep 1; done; return 1; }
# 等到 nginx 真把这台重新放进轮询(从门口:8090能打到它)，最多等25秒
wait_in_rotation() { for i in $(seq 1 25); do curl -s -m5 -D - -o /dev/null http://127.0.0.1:8090/api/keywords | grep -i 'X-Upstream' | grep -qE "127.0.0.1:$1" && return 0; sleep 1; done; return 1; }
deploy_slot() {
  local S=$1 SRC=${2:-/home/ubuntu/douyin-leadgen/server} D=/home/ubuntu/leadgen-$1 PORT
  [ "$S" = "A" ] && PORT=8091 || PORT=8092
  echo "[$S] 备份→部署($SRC)→重启…"
  cp -f "$D/app.py" "$D/app.py.bak"; cp -f "$D/index.html" "$D/index.html.bak"
  cp -f "$SRC/app.py" "$D/app.py"; cp -f "$SRC/index.html" "$D/index.html"
  sudo systemctl restart leadgen-$S
  if ! health "$PORT"; then
    echo "[$S] ❌ 自检不过 → 自动回滚"; cp -f "$D/app.py.bak" "$D/app.py"; cp -f "$D/index.html.bak" "$D/index.html"; sudo systemctl restart leadgen-$S
    health "$PORT" && echo "[$S] 已回滚到上一版" || echo "[$S] ⚠️ 回滚后仍异常,人工介入"; return 1
  fi
  echo "[$S] 自检OK，等它重新回到 nginx 轮询…"
  wait_in_rotation "$PORT" && echo "[$S] ✅ 已在接客 (:$PORT)" || { echo "[$S] ⚠️ 25秒内没回到轮询，停下别动另一台"; return 1; }
}
case "${1:-}" in
  A|B) deploy_slot "$1" "${2:-}";;
  roll) SRC=${2:-/home/ubuntu/douyin-leadgen/server}
    echo "=== 滚动更新：先A(回到轮询确认)后B，全程不掉线 ==="
    deploy_slot A "$SRC" || { echo "A失败已回滚，B没动，团队不受影响"; exit 1; }
    deploy_slot B "$SRC" || { echo "B失败已回滚；A新B旧仍可用"; exit 1; }
    echo "=== ✅ A/B 均更新到新版，全程未掉线 ===";;
  status) for S in A B; do P=8091; [ "$S" = B ] && P=8092; echo "[$S :$P] svc=$(systemctl is-active leadgen-$S) health=$(curl -s -m5 -o /dev/null -w '%{http_code}' http://127.0.0.1:$P/api/keywords)"; done; echo "[nginx :8090] $(systemctl is-active nginx) 对外=$(curl -s -m5 -o /dev/null -w '%{http_code}' http://127.0.0.1:8090/api/keywords)";;
  *) echo "用法: leadgen-deploy.sh <A|B|roll|status> [源码目录]";;
esac
