#!/usr/bin/env bash
set -euo pipefail

: "${HQ_RELEASE_BACKUP:?HQ_RELEASE_BACKUP is required}"
: "${HQ_RELEASE_STAGE:?HQ_RELEASE_STAGE is required}"
: "${HQ_EXPECT_JS_SHA:?HQ_EXPECT_JS_SHA is required}"
: "${HQ_EXPECT_CSS_SHA:?HQ_EXPECT_CSS_SHA is required}"
: "${HQ_EXPECT_HTML_SHA:?HQ_EXPECT_HTML_SHA is required}"

HQ_CONTENT_ROOT="${HQ_CONTENT_ROOT:-/home/ubuntu/content-api}"
HQ_WEB_ROOT="${HQ_WEB_ROOT:-/var/www/huangquechuanmei/workbench}"
HQ_SYSTEMD_TARGET="${HQ_SYSTEMD_TARGET:-/etc/systemd/system/huangque-content.service.d/formal-delivery.conf}"
HQ_PUBLIC_BASE_URL="${HQ_PUBLIC_BASE_URL:-https://huangquechuanmei.com/workbench}"
HQ_CONTENT_HEALTH_URL="${HQ_CONTENT_HEALTH_URL:-http://127.0.0.1:8096/api/gen/health}"
HQ_ADMIN_HEALTH_URL="${HQ_ADMIN_HEALTH_URL:-http://127.0.0.1:8098/api/admin/health}"
HQ_CURL_CONNECT_TIMEOUT="${HQ_CURL_CONNECT_TIMEOUT:-5}"
HQ_CURL_MAX_TIME="${HQ_CURL_MAX_TIME:-20}"
HQ_HTTPS_DIR="$(mktemp -d)"
HQ_ACTIVATED=0

bounded_curl() {
  curl --connect-timeout "$HQ_CURL_CONNECT_TIMEOUT" \
    --max-time "$HQ_CURL_MAX_TIME" "$@"
}

restore_release_manifest() {
  while IFS=$'\t' read -r HQ_SOURCE HQ_TARGET HQ_STATE; do
    case "$HQ_STATE" in
      present)
        HQ_BACKUP_FILE="$HQ_RELEASE_BACKUP/files/${HQ_TARGET#/}"
        sudo test -f "$HQ_BACKUP_FILE" || return 1
        sudo mkdir -p "$(dirname "$HQ_TARGET")" || return 1
        sudo cp -a -- "$HQ_BACKUP_FILE" "$HQ_TARGET" || return 1
        ;;
      absent)
        sudo rm -f -- "$HQ_TARGET" || return 1
        ;;
      *) return 1 ;;
    esac
  done <"$HQ_RELEASE_BACKUP/states.tsv"
}

finish_release() {
  HQ_STATUS=$?
  rm -rf "$HQ_HTTPS_DIR" || true
  if test "$HQ_STATUS" -ne 0 && test "$HQ_ACTIVATED" -eq 1; then
    set +e
    if ! sudo systemctl stop huangque-content huangque-admin; then
      echo 'CRITICAL: formal-delivery services could not be stopped; rollback aborted' >&2
      exit 1
    fi
    if restore_release_manifest; then
      if ! sudo systemctl daemon-reload; then
        echo 'CRITICAL: formal-delivery rollback restored files but daemon-reload failed' >&2
        exit 1
      fi
      if ! sudo systemctl restart huangque-content huangque-admin; then
        echo 'CRITICAL: formal-delivery rollback restored files but service restart failed' >&2
        exit 1
      fi
    else
      echo 'CRITICAL: formal-delivery manifest rollback failed; services remain stopped' >&2
      exit 1
    fi
  fi
  exit "$HQ_STATUS"
}
trap finish_release EXIT

test -f "$HQ_RELEASE_STAGE/release-manifest.tsv"
test -f "$HQ_RELEASE_BACKUP/states.tsv"
HQ_ACTIVATED=1
sudo systemctl stop huangque-content huangque-admin
while IFS=$'\t' read -r HQ_SOURCE HQ_TARGET; do
  case "$HQ_SOURCE:$HQ_TARGET" in
    server/*:"$HQ_CONTENT_ROOT"/*) ;;
    site/workbench/*:"$HQ_WEB_ROOT"/*) ;;
    deploy/systemd/*:"$HQ_SYSTEMD_TARGET") ;;
    *) exit 1 ;;
  esac
  sudo install -D -m 0644 "$HQ_RELEASE_STAGE/files/$HQ_SOURCE" "$HQ_TARGET"
done <"$HQ_RELEASE_STAGE/release-manifest.tsv"

test "$(sha256sum "$HQ_WEB_ROOT/short-drama-workspace.js" | awk '{print $1}')" = "$HQ_EXPECT_JS_SHA"
test "$(sha256sum "$HQ_WEB_ROOT/short-drama-workspace.css" | awk '{print $1}')" = "$HQ_EXPECT_CSS_SHA"
test "$(sha256sum "$HQ_WEB_ROOT/short-drama.html" | awk '{print $1}')" = "$HQ_EXPECT_HTML_SHA"
sudo systemctl daemon-reload
sudo systemctl restart huangque-content huangque-admin
sudo systemctl is-active --quiet huangque-content
sudo systemctl is-active --quiet huangque-admin
bounded_curl -fsS "$HQ_CONTENT_HEALTH_URL"
bounded_curl -fsS "$HQ_ADMIN_HEALTH_URL"

bounded_curl -fsS "$HQ_PUBLIC_BASE_URL/short-drama-workspace.js" \
  -o "$HQ_HTTPS_DIR/workspace.js"
bounded_curl -fsS "$HQ_PUBLIC_BASE_URL/short-drama-workspace.css" \
  -o "$HQ_HTTPS_DIR/workspace.css"
bounded_curl -fsS "$HQ_PUBLIC_BASE_URL/short-drama.html" \
  -o "$HQ_HTTPS_DIR/short-drama.html"
test "$(sha256sum "$HQ_HTTPS_DIR/workspace.js" | awk '{print $1}')" = "$HQ_EXPECT_JS_SHA"
test "$(sha256sum "$HQ_HTTPS_DIR/workspace.css" | awk '{print $1}')" = "$HQ_EXPECT_CSS_SHA"
test "$(sha256sum "$HQ_HTTPS_DIR/short-drama.html" | awk '{print $1}')" = "$HQ_EXPECT_HTML_SHA"
