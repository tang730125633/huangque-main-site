#!/usr/bin/env bash
set -Eeuo pipefail

# Transactional Hermes release. Every mutation after the service is stopped is
# guarded by an EXIT trap until both the local and public health probes pass.

: "${HERMES_RELEASE_DIR:?HERMES_RELEASE_DIR is required}"
: "${HERMES_SHA:?HERMES_SHA is required}"

APP_DIR="${HERMES_APP_DIR:-/home/ubuntu/hermes-web}"
DATA_DIR="${HERMES_DATA_DIR:-$APP_DIR/data}"
BACKUP_ROOT="${HERMES_BACKUP_ROOT:-/home/ubuntu/deploy-backups}"
LAST_BACKUP_FILE="${HERMES_LAST_BACKUP_FILE:-/home/ubuntu/hermes-last-backup}"
ENV_FILE="${HERMES_ENV_FILE:-/home/ubuntu/.secrets/hermes-openai.env}"
SERVICE="${HERMES_SERVICE:-hermes-ip12-preview.service}"
SYSTEMD_TARGET="${HERMES_SYSTEMD_TARGET:-/etc/systemd/system/hermes-ip12-preview.service}"
NGINX_DIRECT_AVAILABLE="${HERMES_NGINX_DIRECT_AVAILABLE:-/etc/nginx/sites-available/hermes-ip12-direct}"
NGINX_DIRECT_ENABLED="${HERMES_NGINX_DIRECT_ENABLED:-/etc/nginx/sites-enabled/hermes-ip12-direct}"
NGINX_SITE_AVAILABLE="${HERMES_NGINX_SITE_AVAILABLE:-/etc/nginx/sites-available/huangquechuanmei}"
NGINX_SITE_ENABLED="${HERMES_NGINX_SITE_ENABLED:-/etc/nginx/sites-enabled/huangquechuanmei}"
PYTHON="${HERMES_PYTHON:-python3}"
SUDO="${HERMES_SUDO-sudo}"
LOCAL_HEALTH_URL="${HERMES_LOCAL_HEALTH_URL:-http://127.0.0.1:3102/healthz}"
PUBLIC_HEALTH_URLS="${HERMES_PUBLIC_HEALTH_URLS:-https://huangquechuanmei.com/workbench/ip12/healthz http://129.204.166.13:3101/healthz}"
MIGRATION_SCRIPT="$HERMES_RELEASE_DIR/scripts/migrate_hermes_artifacts.py"
MIGRATION_MANIFEST="$DATA_DIR/.migrations/hermes-owner-artifacts-v1.json"
backup="$BACKUP_ROOT/hermes-${HERMES_SHA}-$(date +%Y%m%d%H%M%S)"
release_committed=0
rollback_running=0
ROLLBACK_FAILURE_EXIT=125

privileged() {
  if test -n "$SUDO"; then
    "$SUDO" "$@"
  else
    "$@"
  fi
}

backup_file() {
  target="$1"
  name="$2"
  if privileged test -e "$target" || privileged test -L "$target"; then
    privileged cp -a "$target" "$backup/$name"
    printf 'present\n' > "$backup/$name.state"
  else
    printf 'absent\n' > "$backup/$name.state"
  fi
}

restore_file() {
  local saved="$1"
  local state="$2"
  local target="$3"
  local restore_failed=0
  privileged mkdir -p "$(dirname "$target")" || restore_failed=1
  if test "$(cat "$state")" = present; then
    privileged rm -f "$target" || restore_failed=1
    privileged cp -a "$saved" "$target" || restore_failed=1
  else
    privileged rm -f "$target" || restore_failed=1
  fi
  return "$restore_failed"
}

fail_if_requested() {
  if test "${HERMES_FAULT_AFTER:-}" = "$1"; then
    echo "injected Hermes release failure after $1" >&2
    return 97
  fi
}

rollback_release() {
  local status="$?"
  if test "$status" -eq 0 || test "$release_committed" -eq 1; then
    return
  fi
  if test "$rollback_running" -eq 1; then
    exit "$status"
  fi
  rollback_running=1
  trap - EXIT ERR INT TERM
  set +e
  export HERMES_ROLLBACK_ACTIVE=1
  local rollback_failed=0
  local rollback_failures=""

  mark_rollback_failure() {
    rollback_failed=1
    if test -n "$rollback_failures"; then
      rollback_failures="$rollback_failures, $1"
    else
      rollback_failures="$1"
    fi
    echo "Hermes rollback step failed: $1" >&2
  }

  rollback_step() {
    local rollback_label="$1"
    shift
    if ! "$@"; then
      mark_rollback_failure "$rollback_label"
    fi
    return 0
  }

  echo "Hermes release failed; restoring $backup" >&2
  rollback_step "stop failed release service" privileged systemctl stop "$SERVICE"
  if test "$(cat "$backup/migration-manifest.state")" = absent \
      && test -f "$MIGRATION_MANIFEST"; then
    rollback_step "rollback artifact migration" \
      "$PYTHON" "$MIGRATION_SCRIPT" --data-dir "$DATA_DIR" --rollback
  fi
  rollback_step "restore application files with rsync" \
    rsync -a --delete \
      --exclude data/ --exclude media_library/ --exclude knowledge/ \
      --exclude .agnes_key --exclude agnes_key.txt --exclude '*cookies*.txt' \
      --exclude backups/ --exclude '*.log' --exclude nohup.out \
      --exclude '*.bak*' --exclude '*_backup.py' \
      --exclude __pycache__/ --exclude '*.pyc' \
      "$backup/code/" "$APP_DIR/"
  rollback_step "restore systemd unit" restore_file \
    "$backup/hermes-ip12-preview.service" \
    "$backup/hermes-ip12-preview.service.state" "$SYSTEMD_TARGET"
  rollback_step "restore direct nginx configuration" restore_file \
    "$backup/nginx-hermes-ip12-direct.conf" \
    "$backup/nginx-hermes-ip12-direct.conf.state" "$NGINX_DIRECT_AVAILABLE"
  rollback_step "restore direct nginx enabled link" restore_file \
    "$backup/nginx-hermes-ip12-direct-enabled.conf" \
    "$backup/nginx-hermes-ip12-direct-enabled.conf.state" "$NGINX_DIRECT_ENABLED"
  rollback_step "restore site nginx configuration" restore_file \
    "$backup/nginx-huangquechuanmei.conf" \
    "$backup/nginx-huangquechuanmei.conf.state" "$NGINX_SITE_AVAILABLE"
  rollback_step "restore site nginx enabled link" \
    restore_file "$backup/nginx-huangquechuanmei-enabled.conf" \
    "$backup/nginx-huangquechuanmei-enabled.conf.state" "$NGINX_SITE_ENABLED"
  rollback_step "reload systemd units" privileged systemctl daemon-reload
  if test "$(cat "$backup/hermes-ip12-preview.enabled")" = enabled; then
    rollback_step "restore service enabled state" privileged systemctl enable "$SERVICE"
  else
    rollback_step "restore service disabled state" privileged systemctl disable "$SERVICE"
  fi
  if privileged nginx -t; then
    rollback_step "reload nginx" privileged systemctl reload nginx
  else
    mark_rollback_failure "validate restored nginx configuration"
  fi
  if test "$(cat "$backup/hermes-ip12-preview.active")" = active; then
    rollback_step "restart restored service" privileged systemctl restart "$SERVICE"
    rollback_step "verify restored service state" \
      privileged systemctl is-active --quiet "$SERVICE"
    rollback_step "verify restored service health" \
      curl -fsS "$LOCAL_HEALTH_URL" >/dev/null
  else
    rollback_step "restore inactive service state" privileged systemctl stop "$SERVICE"
  fi
  if test "$rollback_failed" -ne 0; then
    echo "Hermes rollback FAILED; manual recovery required; backup=$backup; failed_steps=$rollback_failures" >&2
    exit "$ROLLBACK_FAILURE_EXIT"
  fi
  echo "Hermes rollback completed: $backup" >&2
  exit "$status"
}

test -f "$MIGRATION_SCRIPT"
test -f "$HERMES_RELEASE_DIR/deploy/systemd/hermes-ip12-preview.service"
test -f "$HERMES_RELEASE_DIR/deploy/nginx-hermes-ip12-direct.conf"
test -f "$HERMES_RELEASE_DIR/deploy/nginx-huangquechuanmei.conf"
test -f "$ENV_FILE"
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a
: "${HERMES_LEGACY_OWNER:?HERMES_LEGACY_OWNER is required}"
DEPLOY_USER="${HERMES_DEPLOY_USER:-$(id -un)}"
DEPLOY_GROUP="${HERMES_DEPLOY_GROUP:-$(id -gn)}"

privileged install -d -o "$DEPLOY_USER" -g "$DEPLOY_GROUP" -m 0700 "$BACKUP_ROOT"
privileged install -d -o "$DEPLOY_USER" -g "$DEPLOY_GROUP" -m 0700 "$backup"
install -d -m 0700 "$backup/code"
mkdir -p "$APP_DIR"
rsync -a \
  --exclude data/ --exclude media_library/ --exclude knowledge/ \
  --exclude .agnes_key --exclude agnes_key.txt --exclude '*cookies*.txt' \
  --exclude __pycache__/ --exclude '*.pyc' \
  "$APP_DIR/" "$backup/code/"
backup_file "$SYSTEMD_TARGET" hermes-ip12-preview.service
backup_file "$NGINX_DIRECT_AVAILABLE" nginx-hermes-ip12-direct.conf
backup_file "$NGINX_DIRECT_ENABLED" nginx-hermes-ip12-direct-enabled.conf
backup_file "$NGINX_SITE_AVAILABLE" nginx-huangquechuanmei.conf
backup_file "$NGINX_SITE_ENABLED" nginx-huangquechuanmei-enabled.conf
privileged systemctl is-enabled "$SERVICE" \
  > "$backup/hermes-ip12-preview.enabled" 2>/dev/null || printf 'disabled\n' \
  > "$backup/hermes-ip12-preview.enabled"
privileged systemctl is-active "$SERVICE" \
  > "$backup/hermes-ip12-preview.active" 2>/dev/null || printf 'inactive\n' \
  > "$backup/hermes-ip12-preview.active"
if test -f "$MIGRATION_MANIFEST"; then
  printf 'present\n' > "$backup/migration-manifest.state"
else
  printf 'absent\n' > "$backup/migration-manifest.state"
fi

trap rollback_release EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

privileged systemctl stop "$SERVICE" || true
"$PYTHON" "$MIGRATION_SCRIPT" \
  --root-dir "$APP_DIR" \
  --data-dir "$DATA_DIR" \
  --legacy-owner "$HERMES_LEGACY_OWNER" \
  --quota-mb "${HERMES_DATA_QUOTA_MB:-2048}" \
  --dry-run
"$PYTHON" "$MIGRATION_SCRIPT" \
  --root-dir "$APP_DIR" \
  --data-dir "$DATA_DIR" \
  --legacy-owner "$HERMES_LEGACY_OWNER" \
  --quota-mb "${HERMES_DATA_QUOTA_MB:-2048}"

rsync -a --delete \
  --exclude data/ --exclude media_library/ --exclude knowledge/ \
  --exclude .agnes_key --exclude agnes_key.txt --exclude '*cookies*.txt' \
  --exclude backups/ --exclude '*.log' --exclude nohup.out \
  --exclude '*.bak*' --exclude '*_backup.py' \
  --exclude __pycache__/ --exclude '*.pyc' \
  "$HERMES_RELEASE_DIR/server/hermes_ip12/" "$APP_DIR/"
install -d -m 0755 "$APP_DIR/scripts"
install -m 0755 "$MIGRATION_SCRIPT" "$APP_DIR/scripts/migrate_hermes_artifacts.py"
fail_if_requested rsync

"$PYTHON" -m pip install -r "$APP_DIR/requirements.txt"
fail_if_requested pip
PATH="${HERMES_COMMAND_PATH:-/home/ubuntu/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH}"
export PATH
command -v ffmpeg
command -v ffprobe
command -v yt-dlp
command -v edge-tts

privileged install -m 0644 \
  "$HERMES_RELEASE_DIR/deploy/systemd/hermes-ip12-preview.service" "$SYSTEMD_TARGET"
privileged install -m 0644 \
  "$HERMES_RELEASE_DIR/deploy/nginx-hermes-ip12-direct.conf" "$NGINX_DIRECT_AVAILABLE"
privileged install -m 0644 \
  "$HERMES_RELEASE_DIR/deploy/nginx-huangquechuanmei.conf" "$NGINX_SITE_AVAILABLE"
privileged install -m 0644 \
  "$HERMES_RELEASE_DIR/deploy/nginx-huangquechuanmei.conf" "$NGINX_SITE_ENABLED"
privileged ln -sfn "$NGINX_DIRECT_AVAILABLE" "$NGINX_DIRECT_ENABLED"
privileged systemctl daemon-reload
privileged systemd-analyze verify "$SYSTEMD_TARGET"
test "$(
  cd "$APP_DIR"
  "$PYTHON" -c \
    'from server import app; print(len({r.rule for r in app.url_map.iter_rules() if r.endpoint != "static"}))' \
    | tail -1
)" = 76
privileged nginx -t
privileged systemctl enable "$SERVICE"
privileged systemctl restart "$SERVICE"
privileged systemctl is-active --quiet "$SERVICE"
curl -fsS "$LOCAL_HEALTH_URL" >/dev/null
privileged systemctl reload nginx
for url in $PUBLIC_HEALTH_URLS; do
  curl -fsS "$url" >/dev/null
done
fail_if_requested health

printf '%s\n' "$backup" > "$LAST_BACKUP_FILE"
release_committed=1
trap - EXIT INT TERM
echo "Hermes release completed: $HERMES_SHA"
