#!/usr/bin/env bash
set -euo pipefail

ROOT="${CREATOR_AGENT_RELEASE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SHA="${CREATOR_AGENT_RELEASE_SHA:-$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || date +%s)}"
RUNTIME=/opt/huangque/creator-agent
RELEASES="$RUNTIME/releases"
CURRENT="$RUNTIME/current"
SERVICE=huangque-creator-agent.service
UNIT_SOURCE="$ROOT/deploy/systemd/$SERVICE"
UNIT_TARGET="/etc/systemd/system/$SERVICE"
NGINX_SOURCE="$ROOT/deploy/nginx-huangquechuanmei.conf"
NGINX_TARGET=/etc/nginx/sites-available/huangquechuanmei
NGINX_ENABLED=/etc/nginx/sites-enabled/huangquechuanmei
BACKUP="${CREATOR_AGENT_BACKUP_DIR:-$(mktemp -d /var/tmp/creator-agent-release.XXXXXX)}"
NEW_RELEASE=""
OLD_CURRENT=""
WAS_ACTIVE=0
SUCCEEDED=0

cleanup(){
  status=$?
  set +e
  if [[ "${CREATOR_AGENT_VALIDATE_ONLY:-0}" == "1" ]]; then
    rm -rf "$BACKUP"
    return "$status"
  fi
  if [[ "$SUCCEEDED" -ne 1 ]]; then
    if [[ -n "$OLD_CURRENT" && -d "$OLD_CURRENT" ]]; then ln -sfn "$OLD_CURRENT" "$CURRENT"; else rm -f "$CURRENT"; fi
    [[ -f "$BACKUP/unit" ]] && install -o root -g root -m 0644 "$BACKUP/unit" "$UNIT_TARGET" || rm -f "$UNIT_TARGET"
    [[ -f "$BACKUP/nginx" ]] && install -o root -g root -m 0644 "$BACKUP/nginx" "$NGINX_TARGET"
    if [[ -f "$BACKUP/nginx-enabled.state" ]]; then
      if grep -qx present "$BACKUP/nginx-enabled.state"; then
        install -o root -g root -m 0644 "$BACKUP/nginx-enabled" "$NGINX_ENABLED"
      else
        rm -f "$NGINX_ENABLED"
      fi
    fi
    systemctl daemon-reload >/dev/null 2>&1 || true
    nginx -t >/dev/null 2>&1 && systemctl reload nginx >/dev/null 2>&1 || true
    if [[ "$WAS_ACTIVE" -eq 1 && -L "$CURRENT" ]]; then systemctl restart "$SERVICE" >/dev/null 2>&1 || true
    else systemctl disable --now "$SERVICE" >/dev/null 2>&1 || true; fi
    [[ -n "$NEW_RELEASE" && -d "$NEW_RELEASE" ]] && rm -rf "$NEW_RELEASE"
  fi
  rm -rf "$BACKUP"
  return "$status"
}
trap cleanup EXIT

for file in "$ROOT/server/creator_agent_api.py" "$ROOT/server/creator_agent/__init__.py" \
            "$ROOT/server/creator_agent/store.py" "$ROOT/server/creator_agent/planner.py" \
            "$ROOT/server/creator_agent/profile_agent.py" \
            "$ROOT/server/creator_agent/profile_pdf.py" \
            "$ROOT/server/creator_agent/model_usage.py" \
            "$ROOT/server/creator_agent/service.py" \
            "$ROOT/deploy/requirements-creator-agent.txt" \
            "$UNIT_SOURCE" "$NGINX_SOURCE"; do
  [[ -f "$file" && ! -L "$file" ]] || { echo "missing release file: $file" >&2; exit 2; }
done
if [[ "${CREATOR_AGENT_VALIDATE_ONLY:-0}" == "1" ]]; then
  SUCCEEDED=1
  exit 0
fi
[[ "$(id -u)" -eq 0 ]] || { echo "run as root" >&2; exit 2; }
[[ -f /etc/huangque/creator-agent.env ]] || { echo "/etc/huangque/creator-agent.env is missing" >&2; exit 2; }
[[ "$(stat -c '%U' /etc/huangque/creator-agent.env)" = root ]] || { echo "creator-agent.env must be owned by root" >&2; exit 2; }
case "$(stat -c '%a' /etc/huangque/creator-agent.env)" in 600|640) ;; *) echo "creator-agent.env must be mode 600 or 640" >&2; exit 2;; esac
grep -Eq '^CREATOR_AGENT_BASE_URL=https://api\.deepseek\.com/?$' /etc/huangque/creator-agent.env \
  || { echo "creator agent must use the official DeepSeek API base" >&2; exit 2; }
grep -Eq '^CREATOR_AGENT_MODEL=deepseek-v4-flash$' /etc/huangque/creator-agent.env \
  || { echo "creator agent model must be deepseek-v4-flash" >&2; exit 2; }
grep -Eq '^CREATOR_AGENT_API_KEY=.{16,}$' /etc/huangque/creator-agent.env \
  || { echo "creator agent API key is missing" >&2; exit 2; }
! grep -Eq '^CREATOR_AGENT_API_KEY=(replace-|change-me|placeholder)' /etc/huangque/creator-agent.env \
  || { echo "creator agent API key is still a placeholder" >&2; exit 2; }

systemctl is-active --quiet "$SERVICE" && WAS_ACTIVE=1 || true
[[ -L "$CURRENT" ]] && OLD_CURRENT="$(readlink -f "$CURRENT")"
[[ -f "$UNIT_TARGET" ]] && cp -a "$UNIT_TARGET" "$BACKUP/unit"
[[ -f "$NGINX_TARGET" ]] && cp -a "$NGINX_TARGET" "$BACKUP/nginx"
if [[ -f "$NGINX_ENABLED" ]]; then
  cp -L "$NGINX_ENABLED" "$BACKUP/nginx-enabled"
  printf 'present\n' > "$BACKUP/nginx-enabled.state"
else
  printf 'absent\n' > "$BACKUP/nginx-enabled.state"
fi

install -d -o root -g root -m 0755 "$RUNTIME" "$RELEASES"
NEW_RELEASE="$(mktemp -d "$RELEASES/${SHA}.XXXXXX")"
chmod 0755 "$NEW_RELEASE"
install -d -o root -g root -m 0755 "$NEW_RELEASE/creator_agent"
install -o root -g root -m 0644 "$ROOT/server/creator_agent_api.py" "$NEW_RELEASE/creator_agent_api.py"
install -o root -g root -m 0644 "$ROOT/server/creator_agent/"*.py "$NEW_RELEASE/creator_agent/"
install -o root -g root -m 0644 \
  "$ROOT/deploy/requirements-creator-agent.txt" "$NEW_RELEASE/requirements.txt"
/usr/bin/python3 -m venv "$NEW_RELEASE/.venv"
"$NEW_RELEASE/.venv/bin/python" -m pip install \
  --disable-pip-version-check --no-cache-dir \
  --requirement "$NEW_RELEASE/requirements.txt"
"$NEW_RELEASE/.venv/bin/python" -c 'import reportlab; from reportlab.platypus import SimpleDocTemplate'
PYTHONDONTWRITEBYTECODE=1 "$NEW_RELEASE/.venv/bin/python" -m py_compile \
  "$NEW_RELEASE/creator_agent_api.py" "$NEW_RELEASE/creator_agent/"*.py
ln -sfn "$NEW_RELEASE" "$CURRENT.next"
mv -Tf "$CURRENT.next" "$CURRENT"

install -o root -g root -m 0644 "$UNIT_SOURCE" "$UNIT_TARGET"
install -o root -g root -m 0644 "$NGINX_SOURCE" "$NGINX_TARGET"
install -o root -g root -m 0644 "$NGINX_SOURCE" "$NGINX_ENABLED"
systemctl daemon-reload
nginx -t
systemctl enable "$SERVICE"
systemctl restart "$SERVICE"
for _ in $(seq 1 30); do
  if curl -fsS --max-time 5 http://127.0.0.1:8114/health \
      | python3 -c 'import json,sys; d=json.load(sys.stdin); raise SystemExit(0 if d.get("ok") is True and d.get("ready") is True else 1)'; then
    systemctl reload nginx
    systemctl is-active --quiet nginx
    SUCCEEDED=1
    echo "$SERVICE deployed at $SHA"
    exit 0
  fi
  sleep 0.5
done
echo "creator agent health check failed" >&2
exit 1
