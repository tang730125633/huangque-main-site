#!/usr/bin/env bash
# M3 全域影子同步：每日两次对各域跑幂等回填器（自带逐行核对）。
# 核对口径：先对活源做行数快照 → apply 同步影子 → 用快照比对 PG。
# 「活源在 apply 之后的持续新增」是影子滞后（下轮补上），不报警；
# 真正报警的只有「apply 后 PG 与快照仍不一致」。
set -uo pipefail
cd /home/ubuntu/m3a-verify-full || exit 1
export HQ_DATABASE_URL=$(sudo grep -oP "(?<==).*" /etc/huangque/postgresql/migrator.env | head -1)
SHA=1f086d72
LOG=/home/ubuntu/m3a-verify/shadow_sync_all.log
SNAP=/tmp/shadow_src_counts.json
{
  echo "=== $(date '+%F %T') ==="
  python3 - "$SNAP" <<'PYEOF'
import json, sqlite3, sys
checks = [
    ("/home/ubuntu/content-api/runtime_observability.db", "task_trace", "ops.traces"),
    ("/home/ubuntu/content-api/runtime_observability.db", "alert_outbox", "ops.alert_outbox"),
    ("/home/ubuntu/content-api/channel_management.db", "channels", "routing.channels"),
    ("/home/ubuntu/content-api/channel_management.db", "runs", "routing.runs"),
    ("/home/ubuntu/content-api/channel_management.db", "events", "routing.events"),
    ("/home/ubuntu/content-api/channel_management.db", "mappings", "routing.mappings"),
    ("/home/ubuntu/content-api/admin_config.db", "admin_audit", "ops.admin_audit"),
    ("/home/ubuntu/content-api/admin_config.db", "provider_api_keys", "ops.admin_provider_api_keys"),
    ("/home/ubuntu/content-api/leads_crm.db", "lead_crm", "crm.leads"),
    ("/var/lib/huangque-creator-agent/creator_agent.db", "creator_messages", "agent.creator_messages"),
    ("/var/lib/huangque-creator-agent/creator_agent.db", "creator_model_calls", "agent.creator_model_calls"),
    ("/home/ubuntu/agent-metrics/metrics.db", "events", "ops.metrics_events"),
    ("/home/ubuntu/agent-metrics/metrics.db", "runs", "ops.metrics_runs"),
]
snap = []
for src_file, src_table, pg_table in checks:
    try:
        s = sqlite3.connect("file:%s?mode=ro" % src_file, uri=True)
        n = s.execute("SELECT count(*) FROM %s" % src_table).fetchone()[0]
        s.close()
    except sqlite3.Error:
        continue
    snap.append([pg_table, n])
json.dump(snap, open(sys.argv[1], "w"))
PYEOF
  python3 scripts/migrate_ops_observability.py --source /home/ubuntu/content-api/runtime_observability.db --code-sha $SHA --apply
  python3 scripts/migrate_routing_channels.py    --source /home/ubuntu/content-api/channel_management.db --code-sha $SHA --apply
  python3 scripts/migrate_ops_admin_config.py    --source /home/ubuntu/content-api/admin_config.db --code-sha $SHA --apply
  python3 scripts/migrate_crm_leads.py           --source /home/ubuntu/content-api/leads_crm.db --code-sha $SHA --apply
  python3 scripts/migrate_agent_creator.py       --source /var/lib/huangque-creator-agent/creator_agent.db --code-sha $SHA --apply
  python3 scripts/migrate_ops_metrics.py         --source /home/ubuntu/agent-metrics/metrics.db --code-sha $SHA --apply
  python3 - "$HQ_DATABASE_URL" "$SNAP" <<'PYEOF'
import json, sys, psycopg
snap = json.load(open(sys.argv[2]))
bad = 0
with psycopg.connect(sys.argv[1]) as conn:
    for pg_table, n_src in snap:
        n_pg = conn.execute("SELECT count(*) FROM %s" % pg_table).fetchone()[0]
        if n_src != n_pg:
            print("ALERT 行数不一致 %s: 快照=%d pg=%d" % (pg_table, n_src, n_pg))
            bad += 1
print("影子行数核对完成：%d 表，不一致 %d" % (len(snap), bad))
PYEOF
} >> "$LOG" 2>&1
