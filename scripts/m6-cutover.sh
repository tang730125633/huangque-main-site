#!/usr/bin/env bash
# M6 切写：auth identity/ledger 域 SQLite -> PostgreSQL（低峰窗口执行）
#
# 用法：
#   m6-cutover.sh cutover [--force]   切写（默认要求 23:00-05:30）
#   m6-cutover.sh rollback            回滚到 sqlite（秒级，SQLite 全程只读保留）
#   m6-cutover.sh status              现状检查（只读）
#
# 纪律：
#   - 账务零容忍：ledger apply 必须携带当次 verify-balances 的 report_checksum
#     指纹（--ack-balance-report）；硬报警（unexplained/ledger_end_mismatch/
#     ledger_row_inconsistent/duplicate_keys 任一非零）直接停。
#   - 停写窗口：先停 claims timer 再回填再切 auth，窗口内 SQLite 冻结。
#   - 本脚本不打印任何数据库密码；verify-balances 的完整输出（含手机号）只落
#      root-only 文件，不 echo。
set -euo pipefail

REPO=/home/ubuntu/m3a-verify-full
SNAP_DIR=/home/ubuntu/m3a-verify-full/m6-cutover-state
STATE_FILE="$SNAP_DIR/state.json"
AUTH_DROPIN=/etc/systemd/system/huangque-auth.service.d/m6-identity-ledger.conf
CLAIMS_DROPIN=/etc/systemd/system/huangque-invite-reward-claims.service.d/m6-postgres.conf
CODE_SHA=4c9d7c5a
MODE="${1:-status}"
FORCE="${2:-}"

log() { echo "[m6-cutover] $(date '+%F %T') $*"; }
die() { log "STOP: $*"; exit 1; }

migrator_url() { sudo grep -oP '(?<==).*' /etc/huangque/postgresql/migrator.env | head -1; }
auth_rt_url() { sudo sed -n 's/^Environment=HQ_DATABASE_URL=//p' /etc/systemd/system/huangque-auth.service.d/migration.conf | head -1; }

check_time_window() {
    [ "$FORCE" = "--force" ] && return 0
    local hh
    hh=$(date +%H)
    if [ "$hh" -ge 23 ] || [ "$hh" -lt 6 ]; then return 0; fi
    die "不在低峰窗口（当前 $(date +%H:%M)，窗口 23:00-05:30）；确认要白天切用 --force"
}

check_preconditions() {
    log "前置检查..."
    local cur
    cur=$(cd "$REPO" && env HQ_DATABASE_URL="$(migrator_url)" python3 -m alembic current 2>/dev/null | tail -1)
    [ "$cur" = "20260916_0012" ] || die "alembic 版本 $cur != 20260916_0012"
    systemctl is-active --quiet huangque-auth || die "huangque-auth 不在 active"
    systemctl is-active --quiet huangque-leadgen-api || die "huangque-leadgen-api 不在 active"
    [ -f /home/ubuntu/auth-service/content_domains/auth_store.py ] || die "auth_store.py 未部署"
    log "前置检查 OK（alembic=$cur）"
}

verify_balances() {
    # 输出 report_checksum 到 stdout；完整报告落 root-only 文件。
    local snapshot="$1" outfile checksum
    outfile="$SNAP_DIR/verify-balances-$(date +%Y%m%d%H%M%S).json"
    mkdir -p "$SNAP_DIR"
    (cd "$REPO" && env HQ_DATABASE_URL="$(migrator_url)" \
        python3 scripts/migrate_auth_ledger.py --source "$snapshot" --verify-balances) > "$outfile" 2>/dev/null
    sudo chown root:root "$outfile" && sudo chmod 600 "$outfile"
    checksum=$(python3 -c "import json;print(json.load(open('$outfile'))['report_checksum'])")
    # 硬报警门禁
    python3 - "$outfile" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))
hard = []
if d.get("unexplained"): hard.append("unexplained=%s" % d["unexplained"])
if d.get("ledger_end_mismatch"): hard.append("ledger_end_mismatch=%s" % d["ledger_end_mismatch"])
users = d.get("ledger_row_inconsistent") or {}
if users.get("users"): hard.append("ledger_row_inconsistent users=%s" % users["users"])
if d.get("duplicate_keys"): hard.append("duplicate_keys=%s" % d["duplicate_keys"])
print("verify-balances: matched=%s explained=%s diffs=%s trial_points=%s untracked=%s non_balance_delta=%s" % (
    d.get("matched"), d.get("explained"), d.get("diffs"),
    d.get("trial_points"), d.get("untracked_balance_without_ledger_rows"),
    d.get("non_balance_delta")))
if hard:
    print("HARD-ALERT: %s" % "; ".join(hard))
    sys.exit(1)
PYEOF
    echo "$checksum"
}

do_cutover() {
    check_time_window
    check_preconditions

    log "停 claims timer（停写窗口开始）"
    sudo systemctl stop huangque-invite-reward-claims.timer

    local snapshot checksum
    snapshot="$SNAP_DIR/users-$(date +%Y%m%d%H%M%S).db"
    mkdir -p "$SNAP_DIR"
    cp /home/ubuntu/auth-service/users.db "$snapshot"
    log "快照完成：$snapshot"

    log "identity dry-run"
    (cd "$REPO" && env HQ_DATABASE_URL="$(migrator_url)" \
        python3 scripts/migrate_auth_identity.py --source "$snapshot") | tail -1
    log "ledger dry-run"
    (cd "$REPO" && env HQ_DATABASE_URL="$(migrator_url)" \
        python3 scripts/migrate_auth_ledger.py --source "$snapshot") | tail -1

    log "ledger verify-balances（账务指纹）"
    checksum=$(verify_balances "$snapshot")
    log "balance_report_checksum=$checksum"

    log "identity apply"
    (cd "$REPO" && env HQ_DATABASE_URL="$(migrator_url)" \
        python3 scripts/migrate_auth_identity.py --source "$snapshot" --code-sha "$CODE_SHA" --apply) | tail -1
    log "ledger apply（带当次余额指纹）"
    (cd "$REPO" && env HQ_DATABASE_URL="$(migrator_url)" \
        python3 scripts/migrate_auth_ledger.py --source "$snapshot" --code-sha "$CODE_SHA" \
        --ack-balance-report "$checksum" --apply) | tail -1

    log "幂等复核（行数/校验和应不变）"
    local id1 id2
    id1=$(cd "$REPO" && env HQ_DATABASE_URL="$(migrator_url)" python3 scripts/migrate_auth_identity.py --source "$snapshot" | python3 -c "import json,sys;d=json.load(sys.stdin);print(d['source_checksum'], d['users'])")
    id2=$(cd "$REPO" && env HQ_DATABASE_URL="$(migrator_url)" python3 scripts/migrate_auth_ledger.py --source "$snapshot" | python3 -c "import json,sys;d=json.load(sys.stdin);print(d['source_checksum'], d['points_audit'])")
    log "identity 复核: $id1"
    log "ledger 复核: $id2"

    log "写 auth drop-in（identity/ledger -> postgres）"
    sudo tee "$AUTH_DROPIN" > /dev/null <<'EOF'
[Service]
Environment=HQ_IDENTITY_STORE=postgres
Environment=HQ_LEDGER_STORE=postgres
EOF
    sudo systemctl daemon-reload
    sudo systemctl restart huangque-auth
    sleep 3
    systemctl is-active --quiet huangque-auth || die "huangque-auth 重启后不在 active"

    log "检查 auth 权威声明日志"
    local log1 log2
    log1=$(sudo journalctl -u huangque-auth --since '30 sec ago' --no-pager | grep -c "HQ_IDENTITY_STORE authority announced: mode=postgres" || true)
    log2=$(sudo journalctl -u huangque-auth --since '30 sec ago' --no-pager | grep -c "HQ_LEDGER_STORE authority announced: mode=postgres" || true)
    [ "$log1" -ge 1 ] && [ "$log2" -ge 1 ] || die "权威声明日志缺失（identity=$log1 ledger=$log2）"
    log "mode=postgres 声明 OK"

    log "权威校验器"
    sudo python3 "$REPO/scripts/check_store_authority.py" || die "权威校验器 EXIT != 0"

    log "E2E 冒烟（PG 模式函数级）"
    (cd "$REPO/server" && env HQ_IDENTITY_STORE=postgres HQ_LEDGER_STORE=postgres \
        HQ_DATABASE_URL="$(auth_rt_url)" python3 - <<'PYEOF'
import sys, time
sys.path.insert(0, '/home/ubuntu/m3a-verify-full/server')
import auth_server
S = '_m6prod_%d' % int(time.time())
ok, resp = auth_server.register_account(S, 'smoke123456', client_ip='127.0.0.1')
assert ok and 'token' in ok, resp
p, err = auth_server.deduct_points(S, 2, 'm6 cutover smoke')
assert err is None and p['points'] == 14, (p, err)
p, err = auth_server.refund_points(S, 2, 'm6 cutover smoke refund', transaction_key='m6cut-%s' % S)
assert err is None and p['points'] == 16, (p, err)
print('E2E-OK user=%s points=%s' % (S, p['points']))
PYEOF
)

    log "claims 单元切 PG 并恢复 timer"
    sudo mkdir -p /etc/systemd/system/huangque-invite-reward-claims.service.d
    sudo tee "$CLAIMS_DROPIN" > /dev/null <<EOF
[Service]
Environment=HQ_IDENTITY_STORE=postgres
Environment=HQ_LEDGER_STORE=postgres
Environment=HQ_DATABASE_URL=$(auth_rt_url)
EOF
    sudo systemctl daemon-reload
    sudo chmod 600 "$CLAIMS_DROPIN"
    sudo systemctl start huangque-invite-reward-claims.timer

    log "冰冻监控基线"
    python3 - "$STATE_FILE" <<'PYEOF'
import json, os, sys, sqlite3, time
out = sys.argv[1]
os.makedirs(os.path.dirname(out), exist_ok=True)
db = "/home/ubuntu/auth-service/users.db"
st = os.stat(db)
c = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
n = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
c.close()
json.dump({"cutover_at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "users_db_mtime": st.st_mtime, "users_db_size": st.st_size,
           "users_rows": n}, open(out, "w"), indent=2)
print("frozen baseline: users=%d mtime=%s size=%d" % (n, time.ctime(st.st_mtime), st.st_size))
PYEOF

    log "切写完成。观察项：users.db mtime 冻结、journalctl 无 ERROR、claims timer 正常"
}

do_rollback() {
    log "回滚开始"
    sudo systemctl stop huangque-invite-reward-claims.timer || true
    sudo rm -f "$CLAIMS_DROPIN"
    sudo rm -f "$AUTH_DROPIN"
    sudo systemctl daemon-reload
    sudo systemctl restart huangque-auth
    sleep 3
    systemctl is-active --quiet huangque-auth || die "huangque-auth 重启后不在 active"
    sudo systemctl start huangque-invite-reward-claims.timer
    log "回滚完成：auth 与 claims 均回 sqlite（users.db 全程未动）"
    do_status
}

do_status() {
    log "=== M6 状态 ==="
    echo "auth drop-in: $([ -f "$AUTH_DROPIN" ] && echo 存在 || echo 无)"
    echo "claims drop-in: $([ -f "$CLAIMS_DROPIN" ] && echo 存在 || echo 无)"
    systemctl is-active huangque-auth huangque-leadgen-api huangque-invite-reward-claims.timer
    local mtime
    mtime=$(stat -c %Y /home/ubuntu/auth-service/users.db)
    echo "users.db mtime: $(date -d @$mtime '+%F %T')（冻结=切写成功）"
    if [ -f "$STATE_FILE" ]; then python3 -c "import json;d=json.load(open('$STATE_FILE'));print('基线:', d)"; fi
    sudo journalctl -u huangque-auth --since '10 min ago' --no-pager | grep -E 'authority announced' | tail -4 || echo "（近 10 分钟无权威声明日志）"
}

case "$MODE" in
    cutover) do_cutover ;;
    rollback) do_rollback ;;
    status) do_status ;;
    *) echo "用法: $0 {cutover [--force] | rollback | status}"; exit 2 ;;
esac
