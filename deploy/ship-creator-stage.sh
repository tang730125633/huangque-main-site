#!/usr/bin/env bash

creator_stage_path_safe(){
  local stage="${1:-}" suffix
  case "$stage" in
    /tmp/huangque-creator-agent-*) ;;
    *) return 1 ;;
  esac
  suffix="${stage#/tmp/huangque-creator-agent-}"
  case "$suffix" in
    ''|*[!A-Za-z0-9._-]*) return 1 ;;
  esac
}

cleanup_creator_remote_stage(){
  local stage="${CREATOR_REMOTE_STAGE:-}"
  [ -n "$stage" ] || return 0
  creator_stage_path_safe "$stage" \
    || { echo "  ❌ 拒绝清理非法 Creator 暂存路径：$stage" >&2; return 2; }
  if ! $SSHC "$REMOTE" bash -s -- "$stage" <<'RS'
set -eu
stage="$1"
case "$stage" in
  /tmp/huangque-creator-agent-*) ;;
  *) exit 2 ;;
esac
suffix="${stage#/tmp/huangque-creator-agent-}"
case "$suffix" in
  ''|*[!A-Za-z0-9._-]*) exit 2 ;;
esac
sudo rm -rf -- "$stage"
test ! -e "$stage"
RS
  then
    echo "  ❌ Creator 远端暂存目录清理失败：$stage" >&2
    return 1
  fi
  CREATOR_REMOTE_STAGE=""
}

cleanup_creator_stage(){
  local status=$? cleanup_status=0
  set +e
  if [ -n "${CREATOR_LOCAL_STAGE:-}" ]; then
    rm -rf -- "$CREATOR_LOCAL_STAGE" || cleanup_status=1
    CREATOR_LOCAL_STAGE=""
  fi
  cleanup_creator_remote_stage || cleanup_status=1
  [ "$status" -ne 0 ] && return "$status"
  return "$cleanup_status"
}
