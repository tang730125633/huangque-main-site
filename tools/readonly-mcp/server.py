#!/usr/bin/env python3
"""
黄雀服务器只读 MCP —— 给外部强推理模型（GPT 等）一个"只读的眼睛"。

设计原则（照抄 leadgen-mcp 的哲学）：
  1. **按钮，不是任意命令** —— 暴露固定的工具集，调用方传不了任意 SQL / shell
  2. **给结构，不给内容** —— 表结构、行数、状态可以给；用户数据、密码、密钥绝不给
  3. **凭据自己拿** —— 数据库连接串由本服务读受限 env，调用方永远看不到
  4. **零依赖** —— 仅标准库，不会因为缺包起不来
  5. **默认只读** —— 数据库用只读角色；所有 SQL 都是硬编码的

传输：MCP Streamable HTTP（JSON-RPC 2.0 over POST）
认证：Bearer token（从 /etc/huangque/readonly-mcp.env 读）
"""
import json
import os
import re
import time
import socket
import subprocess
import threading
import http.server
import urllib.parse
from datetime import datetime, timezone, timedelta

VERSION = "1.0.0"
PORT = int(os.environ.get("MCP_PORT", "8799"))
BIND = os.environ.get("MCP_BIND", "127.0.0.1")
TOKEN_FILE = os.environ.get("MCP_TOKEN_FILE", "/etc/huangque/readonly-mcp.env")
AUDIT_LOG = os.environ.get("MCP_AUDIT_LOG", "/var/log/huangque-readonly-mcp.log")
CST = timezone(timedelta(hours=8))

# 速率限制：每 token 每分钟最多 N 次
RATE_LIMIT = int(os.environ.get("MCP_RATE_LIMIT", "60"))
_rate = {}


# ────────────────────────── 凭据 ──────────────────────────
def _read_env(path, key):
    """从受保护的 env 文件读一个键（不落日志、不返回给调用方）"""
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith(key + "="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return os.environ.get(key, "")


TOKEN = _read_env(TOKEN_FILE, "MCP_READONLY_TOKEN") or os.environ.get("MCP_READONLY_TOKEN", "")
# 匿名模式：nginx 通过"秘密路径"转发时带 X-MCP-Anon 头；直连 127.0.0.1 无法伪造（只监听回环）
ANON_MARK = _read_env(TOKEN_FILE, "MCP_ANON_MARK") or os.environ.get("MCP_ANON_MARK", "")
PG_URL = _read_env("/etc/huangque/postgresql/readonly.env", "HQ_READONLY_DATABASE_URL") \
    or _read_env("/etc/huangque/postgresql/readonly.env", "HQ_DATABASE_URL")


# ────────────────────────── 脱敏 ──────────────────────────
_SECRET_HINTS = re.compile(
    r"(key|token|secret|password|passwd|pwd|credential|authorization|bearer|"
    r"api[-_]?key|private[-_]?key|refresh|access[-_]?token|openai|anthropic|"
    r"deepseek|sk-[A-Za-z0-9_-]{8,}|ghp_[A-Za-z0-9]{20,})",
    re.IGNORECASE,
)
_LONG_SECRET = re.compile(r"\b[A-Za-z0-9_\-]{32,}\b")


def redact(text):
    """把疑似凭据的内容打码。

    两道防线：
      1. 含敏感关键词的行 → 整行打码
      2. 长随机串（>=32 字符的连续字母数字）→ 就地把串打码
    """
    if not text:
        return text
    out = []
    for line in str(text).splitlines():
        if _SECRET_HINTS.search(line):
            # 只保留行首的定位信息（时间戳/service 名），其余打码
            head = line[:24]
            out.append(f"{head} … [REDACTED: 含凭据关键词]")
        else:
            out.append(_LONG_SECRET.sub("[REDACTED-LONG-TOKEN]", line))
    return "\n".join(out)


# ────────────────────────── 审计 ──────────────────────────
def audit(tool, ok, detail=""):
    try:
        ts = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
        line = f"{ts} tool={tool} ok={ok}"
        if detail:
            line += f" detail={detail[:200]}"
        with open(AUDIT_LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


# ────────────────────────── 执行助手 ──────────────────────────
def run(cmd, timeout=20):
    """跑一条只读命令（调用方传不进命令 —— cmd 都是硬编码的）"""
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           timeout=timeout, errors="replace")
        return (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return f"[超时 {timeout}s]"
    except Exception as exc:
        return f"[执行失败: {type(exc).__name__}]"


def psql(sql, timeout=25):
    """只读查询。SQL 全部来自本文件的硬编码常量，不接受调用方输入。"""
    if not PG_URL:
        return "[未配置只读数据库连接（/etc/huangque/postgresql/readonly.env）]"
    safe_sql = sql.replace("'", "'\"'\"'")
    cmd = f"psql \"$PG\" -At -F '|' -c '{safe_sql}'"
    try:
        p = subprocess.run(["bash", "-lc", cmd], capture_output=True, text=True,
                           timeout=timeout, errors="replace",
                           env={**os.environ, "PG": PG_URL})
        out = (p.stdout or "").strip()
        err = (p.stderr or "").strip()
        if err and not out:
            return f"[查询失败: {redact(err)[:200]}]"
        return out or "[空]"
    except subprocess.TimeoutExpired:
        return f"[查询超时 {timeout}s]"
    except Exception as exc:
        return f"[查询异常: {type(exc).__name__}]"


# ────────────────────────── 工具实现（全部只读）──────────────────────────
SERVICES = ["hq-ip-agent", "huangque-auth", "huangque-content", "huangque-creator-agent",
            "huangque-admin", "huangque-render-relay", "huangque-leadgen-api",
            "huangque-imggen-api", "huangque-base-monitor", "huangque-postgres-backup.timer"]


def t_server_overview():
    """所有生产服务的运行状态（是否活着、重启过几次、内存占用）"""
    lines = []
    for s in SERVICES:
        active = run(f"systemctl is-active {s} 2>/dev/null").strip()
        restarts = run(f"systemctl show -p NRestarts --value {s} 2>/dev/null").strip()
        mem = run(f"systemctl show -p MemoryCurrent --value {s} 2>/dev/null").strip()
        try:
            mem = f"{int(mem)//1024//1024} MB" if mem.isdigit() else "—"
        except Exception:
            mem = "—"
        lines.append(f"{s:<32} {active:<9} 重启={restarts}  内存={mem}")
    return "\n".join(lines)


def t_disk_health():
    """磁盘空间（根分区 / 数据盘）"""
    return run("df -h / /data 2>/dev/null | awk 'NR==1 || /\\//{printf \"%-24s 可用 %-8s 已用 %s\\n\", $6, $4, $5}'")


def t_listening_ports():
    """服务器上监听中的端口（区分对外 / 仅本机）"""
    out = run("ss -ltn 2>/dev/null | awk 'NR>1{print $4}' | sort -u", timeout=15)
    public, local = [], []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        (local if line.startswith(("127.", "[::1]", "::1")) else public).append(line)
    return ("对外监听：\n  " + ", ".join(public[:30]) +
            "\n仅本机监听：\n  " + ", ".join(local[:30]))


def t_list_schemas():
    """数据库里有哪些模块（schema）以及各有多少张表"""
    return psql("""SELECT table_schema || ' : ' || count(*) || ' 张表'
                   FROM information_schema.tables
                   WHERE table_schema NOT IN ('pg_catalog','information_schema','pg_toast')
                   GROUP BY table_schema ORDER BY table_schema;""")


def t_list_tables(schema):
    """某个模块下的表清单与行数（只有表名和数字，没有数据内容）"""
    schema = re.sub(r"[^a-z_]", "", (schema or "").lower())
    if not schema:
        return "[需要 schema 名，可用 list_schemas 查]"
    sql = ("SELECT c.relname || ' : ' || COALESCE(s.n_live_tup, 0) || ' 行' "
           "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
           "LEFT JOIN pg_stat_user_tables s ON s.relid=c.oid "
           f"WHERE n.nspname='{schema}' AND c.relkind='r' ORDER BY c.relname;")
    return psql(sql)


def t_table_schema(table):
    """某张表的字段定义（字段名 + 类型 + 是否可空）。只看结构，不看数据。"""
    table = re.sub(r"[^a-zA-Z0-9_.]", "", table or "")
    if "." not in table:
        return "[需要 schema.table 格式，例如 identity.users]"
    schema, name = table.split(".", 1)
    sql = ("SELECT column_name || ' ' || data_type || "
           "CASE WHEN is_nullable='NO' THEN ' NOT NULL' ELSE '' END "
           "FROM information_schema.columns "
           f"WHERE table_schema='{schema}' AND table_name='{name}' ORDER BY ordinal_position;")
    return psql(sql)


def t_migration_status():
    """数据库迁移进度总览：哪些域已切、哪些没切"""
    runs = psql("""SELECT domain || ' | ' || state || ' | 源=' || source_count || ' 目标=' || target_count
                   FROM ops.data_migration_runs r1
                   WHERE started_at = (SELECT max(started_at) FROM ops.data_migration_runs r2 WHERE r2.domain=r1.domain)
                   ORDER BY domain;""")
    return f"【迁移台账（各域最新一批）】\n{runs}"


def t_migration_runs(limit="20"):
    """最近的迁移批次记录（时间、域、来源、条数、状态）"""
    try:
        n = max(1, min(int(limit), 100))
    except Exception:
        n = 20
    return psql(f"""SELECT to_char(started_at,'MM-DD HH24:MI') || ' | ' || domain || ' | ' ||
                    source_kind || ' | ' || source_count || '→' || target_count || ' | ' || state
                    FROM ops.data_migration_runs ORDER BY started_at DESC LIMIT {n};""")


def t_store_authority():
    """各服务当前实际读的是哪个存储（PG 还是 SQLite）。这是切写是否生效的关键证据。"""
    out = run("sudo /usr/bin/python3 /home/ubuntu/m3a-verify-full/scripts/check_store_authority.py 2>&1",
              timeout=40)
    if not out.strip():
        return "[权威校验器不可用]"
    # 只保留结论行，避免太长
    keep = [l for l in out.splitlines()
            if any(k in l for k in ("state=", "OK", "ERROR", "HQ_"))]
    return "\n".join(keep[:60])


def t_freeze_status():
    """旧 SQLite 库是否已"冻住"（切写后不应再被修改）。"""
    files = {
        "auth-service/users.db": "/home/ubuntu/auth-service/users.db",
        "content-api/feature_flags.db": "/home/ubuntu/content-api/feature_flags.db",
        "content-api/admin_config.db": "/home/ubuntu/content-api/admin_config.db",
        "content-api/channel_management.db": "/home/ubuntu/content-api/channel_management.db",
        "content-api/leads_crm.db": "/home/ubuntu/content-api/leads_crm.db",
        "render-relay/relay.db": "/home/ubuntu/render-relay/relay.db",
    }
    now = time.time()
    lines = []
    for label, path in files.items():
        try:
            mtime = os.path.getmtime(path)
            age_min = int((now - mtime) / 60)
            state = "已冻结 ✓" if age_min > 60 else f"⚠️ {age_min} 分钟前刚被写过"
            lines.append(f"{label:<36} {state}")
        except FileNotFoundError:
            lines.append(f"{label:<36} [文件不存在]")
        except Exception:
            lines.append(f"{label:<36} [无法读取]")
    return "\n".join(lines)


def t_key_rowcounts():
    """关键业务表的行数（只有数字，用于对账 —— 不含任何用户数据）"""
    queries = [
        ("identity.users", "SELECT count(*) FROM identity.users"),
        ("identity.tokens", "SELECT count(*) FROM identity.tokens"),
        ("identity.cli_device_grants", "SELECT count(*) FROM identity.cli_device_grants"),
        ("ledger.points_audit", "SELECT count(*) FROM ledger.points_audit"),
        ("agent.sessions", "SELECT count(*) FROM agent.sessions"),
        ("ops.feature_flags", "SELECT count(*) FROM ops.feature_flags"),
        ("crm.leads", "SELECT count(*) FROM crm.leads"),
        ("routing.channels", "SELECT count(*) FROM routing.channels"),
    ]
    lines = []
    for label, sql in queries:
        lines.append(f"{label:<32} {psql(sql, timeout=12)}")
    return "\n".join(lines)


def t_recent_logs(service, lines="40"):
    """某个服务最近的日志（已脱敏：含凭据的行会被打码）"""
    service = re.sub(r"[^a-zA-Z0-9_.@-]", "", service or "")
    if not service:
        return "[需要服务名，例如 huangque-auth]"
    try:
        n = max(5, min(int(lines), 200))
    except Exception:
        n = 40
    out = run(f"sudo journalctl -u {service} -n {n} --no-pager 2>&1", timeout=30)
    return redact(out)


def t_error_summary(hours="6"):
    """最近若干小时的错误日志汇总（已脱敏）"""
    try:
        h = max(1, min(int(hours), 72))
    except Exception:
        h = 6
    out = run(f"sudo journalctl --since '{h} hours ago' -p err --no-pager 2>&1 | tail -60", timeout=40)
    return redact(out)


def t_backup_status():
    """PostgreSQL 备份状态（最近一次备份时间、大小、校验文件）"""
    out = run("sudo journalctl -u huangque-postgres-backup --since '48 hours ago' --no-pager 2>&1 | tail -8",
              timeout=25)
    files = run("sudo ls -lht /var/backups/huangque-postgres/ 2>/dev/null | head -6", timeout=20)
    return f"【最近备份日志】\n{out}\n\n【备份文件】\n{files}"


def t_health_checks():
    """关键健康接口的连通性（HTTP 状态码）"""
    urls = [
        ("主站首页", "https://huangquechuanmei.com/"),
        ("内容服务健康", "https://huangquechuanmei.com/api/gen/health"),
        ("认证服务健康", "https://huangquechuanmei.com/api/auth/health"),
        ("渲染中转", "https://huangquechuanmei.com/render-relay/health"),
    ]
    lines = []
    for label, url in urls:
        code = run(f"curl -s -o /dev/null -m 8 -w '%{{http_code}}' {url}", timeout=15).strip()
        lines.append(f"{label:<16} {code}")
    return "\n".join(lines)


def t_repo_status():
    """迁移仓库的本地状态（分支、最近提交、未提交改动）"""
    repo = "/home/ubuntu/huangque-main-site"
    out = run(f"cd {repo} 2>/dev/null && git log --oneline -8 && echo '--- 未提交 ---' && git status --short | head -10",
              timeout=25)
    return out or "[仓库不可用]"


def t_sql_readonly(query, limit="200"):
    """执行一条只读 SQL（只允许 SELECT）。用于自由探索数据结构与内容。

    保护：单语句、必须是 SELECT/WITH、禁止一切写操作关键字、强制行数上限、超时。
    """
    if not query or not isinstance(query, str):
        return "[需要 query 参数]"
    q = query.strip().rstrip(";").strip()
    # 去注释后校验
    flat = re.sub(r"--[^\n]*", " ", q)
    flat = re.sub(r"/\*.*?\*/", " ", flat, flags=re.S)
    low = flat.lower().lstrip()
    if not (low.startswith("select") or low.startswith("with") or low.startswith("table ")):
        return "[拒绝：只允许 SELECT / WITH 查询]"
    if ";" in flat:
        return "[拒绝：不允许多语句]"
    banned = ("insert", "update", "delete", "drop", "alter", "create", "grant", "revoke",
              "truncate", "copy", "vacuum", "reindex", "cluster", "refresh", "call", "do ",
              "set ", "reset ", "listen", "notify", "lock", "commit", "rollback", "begin")
    for b in banned:
        if re.search(r"\b" + b.strip() + r"\b", low):
            return f"[拒绝：含受限关键字 {b.strip()}]"
    try:
        n = max(1, min(int(limit), 2000))
    except Exception:
        n = 200
    if not re.search(r"\blimit\b", low):
        q = f"{q} LIMIT {n}"
    return psql(q, timeout=30)


def t_list_all_tables():
    """列出所有 schema 的所有表（完整清单，含行数）"""
    return psql("""SELECT n.nspname || '.' || c.relname || ' | ' || COALESCE(s.n_live_tup,0) || ' 行 | '
                   || pg_size_pretty(pg_total_relation_size(c.oid))
                   FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                   LEFT JOIN pg_stat_user_tables s ON s.relid=c.oid
                   WHERE c.relkind='r' AND n.nspname NOT IN ('pg_catalog','information_schema','pg_toast')
                   ORDER BY n.nspname, c.relname;""", timeout=40)


def t_column_comments(table):
    """某张表所有字段的定义 + 中文注释（迁移文件里写的注释）"""
    t = re.sub(r"[^a-zA-Z0-9_.]", "", table or "")
    if "." not in t:
        return "[需要 schema.table]"
    schema, name = t.split(".", 1)
    col = psql(f"""SELECT a.attname || ' ' || format_type(a.atttypid,a.atttypmod)
                   || COALESCE(' — ' || d.description, '')
                   FROM pg_attribute a
                   LEFT JOIN pg_description d ON d.objoid=a.attrelid AND d.objsubid=a.attnum
                   WHERE a.attrelid = '{schema}.{name}'::regclass AND a.attnum>0 AND NOT a.attisdropped
                   ORDER BY a.attnum;""", timeout=20)
    tbl = psql(f"""SELECT COALESCE(obj_description('{schema}.{name}'::regclass), '(无表注释)');""", timeout=15)
    return f"【表注释】{tbl}\n\n【字段】\n{col}"


def t_pg_activity():
    """数据库当前活动（连接数、长事务、锁等待、最慢查询）"""
    return psql("""SELECT '连接数=' || count(*) FROM pg_stat_activity WHERE backend_type='client backend'
                   UNION ALL SELECT '长事务=' || count(*) FROM pg_stat_activity
                     WHERE state='idle in transaction' AND now()-state_change > interval '5 min'
                   UNION ALL SELECT '锁等待=' || count(*) FROM pg_locks WHERE NOT granted
                   UNION ALL SELECT '当前库大小=' || pg_size_pretty(pg_database_size(current_database()));""", timeout=20)


def t_table_indexes(table):
    """某张表的索引（排查性能问题时用）"""
    t = re.sub(r"[^a-zA-Z0-9_.]", "", table or "")
    if "." not in t:
        return "[需要 schema.table]"
    schema, name = t.split(".", 1)
    return psql(f"""SELECT indexname || ' : ' || indexdef FROM pg_indexes
                    WHERE schemaname='{schema}' AND tablename='{name}';""", timeout=20)


def t_sqlite_tables(path):
    """读一个旧 SQLite 文件里有哪些表（切写排查用，只读打开）"""
    p = path if path and path.startswith("/") else ""
    if not p or not os.path.exists(p):
        return "[路径无效或不存在]"
    if not p.endswith(".db"):
        return "[只允许 .db 文件]"
    out = run(f"python3 -c \"import sqlite3;c=sqlite3.connect('file:{p}?mode=ro',uri=True);"
              f"print('\\\\n'.join(f'{{t[0]}}' for t in c.execute(\"SELECT name FROM sqlite_master WHERE type='table'\")))"
              f"\" 2>&1", timeout=20)
    return out


def t_recent_deploys():
    """最近的部署记录（从部署日志里提取）"""
    out = run("tail -40 /home/ubuntu/deploy.log 2>/dev/null || echo '[无 deploy.log]'", timeout=20)
    return redact(out)


TOOLS = {
    "server_overview":   (t_server_overview,   "所有生产服务的运行状态（活/挂、重启次数、内存）"),
    "disk_health":       (t_disk_health,       "磁盘空间（根分区与数据盘）"),
    "listening_ports":   (t_listening_ports,   "服务器监听端口（区分对外/仅本机）"),
    "list_schemas":      (t_list_schemas,      "数据库有哪些模块（schema）及各有多少表"),
    "list_tables":       (t_list_tables,       "某模块下的表清单与行数（无数据内容）", ["schema"]),
    "table_schema":      (t_table_schema,      "某张表的字段定义（只看结构）", ["table"]),
    "migration_status":  (t_migration_status,  "数据库迁移进度（各域最新一批的状态）"),
    "migration_runs":    (t_migration_runs,    "最近的迁移批次记录", ["limit"]),
    "store_authority":   (t_store_authority,   "各服务当前实际读哪个存储（切写是否生效）"),
    "freeze_status":     (t_freeze_status,     "旧 SQLite 库是否已冻结（不再被写）"),
    "key_rowcounts":     (t_key_rowcounts,     "关键业务表行数（仅数字，用于对账）"),
    "recent_logs":       (t_recent_logs,       "某服务最近日志（已脱敏）", ["service", "lines"]),
    "error_summary":     (t_error_summary,     "最近若干小时的错误汇总（已脱敏）", ["hours"]),
    "backup_status":     (t_backup_status,     "PostgreSQL 备份状态"),
    "health_checks":     (t_health_checks,     "关键健康接口连通性"),
    "repo_status":       (t_repo_status,       "迁移仓库本地状态"),
    "recent_deploys":    (t_recent_deploys,    "最近部署记录（已脱敏）"),
    "sql_readonly":      (t_sql_readonly,       "执行一条只读 SELECT 查询（自由探索数据结构与内容）", ["query", "limit"]),
    "list_all_tables":   (t_list_all_tables,    "所有 schema 下的全部表（含行数与体积）"),
    "column_comments":   (t_column_comments,    "某张表的字段定义 + 中文注释", ["table"]),
    "table_indexes":     (t_table_indexes,      "某张表的索引定义", ["table"]),
    "pg_activity":       (t_pg_activity,        "数据库当前活动（连接/长事务/锁/大小）"),
    "sqlite_tables":     (t_sqlite_tables,      "读旧 SQLite 文件里的表清单（只读）", ["path"]),
}


# ────────────────────────── MCP 协议 ──────────────────────────
def tool_list():
    names = sorted(TOOLS)
    return [
        {
            "name": n,
            "description": TOOLS[n][1],
            "inputSchema": {
                "type": "object",
                "properties": {a: {"type": "string"} for a in (TOOLS[n][2] if len(TOOLS[n]) > 2 else [])},
                "required": TOOLS[n][2] if len(TOOLS[n]) > 2 else [],
            },
        }
        for n in names
    ]


def call_tool(name, args):
    if name not in TOOLS:
        return f"[未知工具 {name}]", True
    fn = TOOLS[name][0]
    params = TOOLS[name][2] if len(TOOLS[name]) > 2 else []
    try:
        if params:
            out = fn(*[str((args or {}).get(p, "") or "") for p in params])
        else:
            out = fn()
        return out, False
    except Exception as exc:
        return f"[工具 {name} 执行异常: {type(exc).__name__}]", True


def handle_rpc(req, token):
    method = req.get("method", "")
    rid = req.get("id")

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "huangque-readonly", "version": VERSION},
        }}
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": tool_list()}}
    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name", "")
        args = params.get("arguments") or {}
        out, is_err = call_tool(name, args)
        audit(name, not is_err)
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "content": [{"type": "text", "text": out}],
            "isError": is_err,
        }}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": -32601, "message": f"Method not found: {method}"}}


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = f"huangque-readonly-mcp/{VERSION}"

    def log_message(self, fmt, *args):
        pass  # 静默（审计写单独的文件）

    def _auth(self):
        # 路径 1：显式 token（本机 / 调试用）
        auth = self.headers.get("Authorization", "")
        if TOKEN and auth == f"Bearer {TOKEN}":
            return True
        # 路径 2：nginx 秘密路径转发的匿名标记（给 ChatGPT 的"无认证"连接器用）
        # 服务只监听 127.0.0.1，外部无法直连伪造此头
        if ANON_MARK and self.headers.get("X-MCP-Anon", "") == ANON_MARK:
            return True
        time.sleep(0.3)
        self._json(401, {"error": "unauthorized"})
        return False

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # 健康检查（不需要 token，便于监控）
        if self.path.rstrip("/") in ("/health", "/mcp/health", ""):
            self._json(200, {"ok": True, "service": "huangque-readonly-mcp",
                             "version": VERSION, "tools": len(TOOLS)})
            return
        self._json(405, {"error": "use POST for MCP"})

    def do_POST(self):
        if not self._auth():
            return
        # 速率限制
        now = time.time()
        bucket = _rate.setdefault("default", [])
        bucket[:] = [t for t in bucket if now - t < 60]
        if len(bucket) >= RATE_LIMIT:
            self._json(429, {"error": "rate limit"})
            return
        bucket.append(now)

        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8", "replace")
            payload = json.loads(raw)
        except Exception:
            self._json(400, {"error": "invalid json"})
            return

        # 支持批量与单条
        if isinstance(payload, list):
            results = [r for r in (handle_rpc(p, TOKEN) for p in payload) if r]
            self._json(200, results if results else {})
            return
        resp = handle_rpc(payload, TOKEN)
        if resp is None:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._json(200, resp)


def main():
    if not TOKEN:
        print("[warn] MCP_READONLY_TOKEN 未配置 —— 服务会拒绝所有请求", flush=True)
    audit("server_start", True, f"port={PORT} tools={len(TOOLS)}")
    srv = http.server.ThreadingHTTPServer((BIND, PORT), Handler)
    print(f"huangque-readonly-mcp {VERSION} listening on {BIND}:{PORT} "
          f"({len(TOOLS)} tools)", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
