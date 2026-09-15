#!/bin/sh
set -eu

umask 077
database="${HQ_POSTGRES_DB:-huangque}"
backup_dir="${HQ_POSTGRES_BACKUP_DIR:-/var/backups/huangque-postgres}"
retention_days="${HQ_POSTGRES_BACKUP_RETENTION_DAYS:-7}"
lock_file="/run/lock/huangque-postgres-backup.lock"

case "$retention_days" in
  *[!0-9]*|"") echo "invalid retention days" >&2; exit 2 ;;
esac

mkdir -p "$backup_dir"
exec 9>"$lock_file"
flock -n 9 || exit 0

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
final="$backup_dir/${database}-${stamp}.dump"
temporary="$backup_dir/.${database}-${stamp}.dump.tmp"
trap 'test ! -e "$temporary" || rm -f "$temporary"' EXIT

pg_dump --format=custom --file="$temporary" "$database"
pg_restore --list "$temporary" >/dev/null
mv "$temporary" "$final"
sha256sum "$final" >"$final.sha256"
find "$backup_dir" -maxdepth 1 -type f \
  \( -name "${database}-*.dump" -o -name "${database}-*.dump.sha256" \) \
  -mtime "+$retention_days" -delete

printf 'backup=%s bytes=%s\n' "$final" "$(stat -c %s "$final")"
