#!/usr/bin/env python3
"""M3A 影子核对：生产 SQLite feature_flags.db 与 PG ops 两表的持续对照。

观察期（2026-09-16 00:00 起 48h）内每日两次由 cron 调用。原则：
- 全程只读 SQLite；PG 侧由回填器维护影子，回填器幂等且有冲突护栏。
- 发现 SQLite 校验和变化（admin 改了开关）→ 先重跑回填同步影子，再逐行对比。
- 零差异记录 OK 日志；任何无法解释的差异以非零退出告警。
"""
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone

ROOT = "/home/ubuntu/m3a-verify"
SOURCE = "/home/ubuntu/content-api/feature_flags.db"
LOG = "/home/ubuntu/m3a-verify/shadow_check.log"


def log(msg):
    line = "%s %s" % (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), msg)
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def source_checksum():
    conn = sqlite3.connect("file:%s?mode=ro" % SOURCE, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        flags = {r["feature"]: dict(r) for r in conn.execute("SELECT * FROM feature_flags")}
        prices = {r["rule"]: dict(r) for r in conn.execute("SELECT * FROM pricing_rules")}
    finally:
        conn.close()
    import hashlib
    import json
    encoded = json.dumps({"flags": flags, "prices": prices},
                         ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest(), flags, prices


def pg_snapshot():
    import psycopg
    url = os.environ.get("HQ_DATABASE_URL") or ""
    if not url:
        raise RuntimeError("HQ_DATABASE_URL 未设置")
    with psycopg.connect(url, row_factory=psycopg.rows.dict_row) as conn:
        flags = {r["feature"]: r for r in conn.execute("SELECT * FROM ops.feature_flags")}
        prices = {r["rule"]: r for r in conn.execute("SELECT * FROM ops.pricing_rules")}
        runs = conn.execute(
            "SELECT source_fingerprint, code_sha FROM ops.data_migration_runs "
            "WHERE domain = 'ops' ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
    return flags, prices, runs


def compare(src, dst, kind):
    if set(src) != set(dst):
        raise SystemExit("差异：%s 键集合不一致 src=%d tgt=%d" % (kind, len(src), len(dst)))
    for key, s in src.items():
        t = dst[key]
        if kind == "flags":
            fields = ("enabled", "updated_by", "updated_at")
            for f in fields:
                sv, tv = s[f], t[f]
                if f == "enabled":
                    sv, tv = bool(sv), bool(tv)
                if str(sv) != str(tv):
                    raise SystemExit("差异：flags %s.%s src=%r tgt=%r" % (key, f, sv, tv))
        else:
            for f in ("points", "updated_by", "updated_at"):
                if str(s[f]) != str(t[f]):
                    raise SystemExit("差异：prices %s.%s src=%r tgt=%r" % (key, f, s[f], t[f]))


def resync():
    # 同步影子：dry-run 不写；apply 需 code-sha。用审计表最近一次 code_sha。
    url = os.environ["HQ_DATABASE_URL"]
    _, _, runs = pg_snapshot()
    code_sha = runs["code_sha"] if runs else None
    if not code_sha:
        raise SystemExit("审计表里没有可用的 code_sha，影子无法同步")
    cmd = [sys.executable, os.path.join(ROOT, "scripts", "migrate_ops_flags_pricing.py"),
           "--source", SOURCE, "--code-sha", code_sha, "--apply"]
    env = dict(os.environ, HQ_DATABASE_URL=url)
    subprocess.run(cmd, check=True, env=env)


def main():
    checksum, flags, prices = source_checksum()
    pg_flags, pg_prices, runs = pg_snapshot()
    try:
        compare(flags, pg_flags, "flags")
        compare(prices, pg_prices, "prices")
        same = True
    except SystemExit as exc:
        same = False
        log("发现差异，尝试判定来源：%s" % exc)
    if same:
        log("OK zero-diff (flags=%d prices=%d, source=%s)" % (len(flags), len(prices), checksum[:12]))
        return 0
    # 有差异：判断是否 SQLite 在观察期内被 admin 改过（指纹 vs 审计表）。
    if runs and runs["source_fingerprint"] != checksum:
        log("SQLite 已更新（旧指纹 %s，新 %s），重跑回填同步影子" %
            ((runs["source_fingerprint"] or "")[:12], checksum[:12]))
        resync()
        pg_flags, pg_prices, _ = pg_snapshot()
        compare(flags, pg_flags, "flags")
        compare(prices, pg_prices, "prices")
        log("重新同步后 OK zero-diff")
        return 0
    log("无法解释的差异且 SQLite 指纹未变 —— 需要人工介入")
    return 1


if __name__ == "__main__":
    sys.exit(main())
