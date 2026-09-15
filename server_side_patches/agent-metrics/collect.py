#!/usr/bin/env python3
# 解析 OpenClaw 三实例 journald 的 [feishu] 日志 → SQLite 会话事件表 (v1.1)
# v1.1: 把 dispatch(带客户) → complete(带回复数) 按 agent 时序关联, 让回复数能落到单客户
#
# M3 metrics 域改造（2026-09-16）：只在「连接点 + 表名」加分发，SQLite 分支与迁移前逐字节一致。
#   HQ_METRICS_STORE=sqlite（默认）→ 原来的 SQLite 路径，行为完全不变。
#   HQ_METRICS_STORE=postgres     → 写 ops.metrics_events（表由 alembic 20260915_0010 建好）。
#   详见同目录 metrics_store.py。
import sqlite3, subprocess, re, hashlib, os, sys
import metrics_store

DB = os.path.expanduser('~/agent-metrics/metrics.db')
UNITS = {'openclaw-gateway': '获客', 'openclaw-second': '文案', 'openclaw-visual': '图片'}
SINCE = sys.argv[1] if len(sys.argv) > 1 else '14 days ago'

os.makedirs(os.path.dirname(DB), exist_ok=True)
EVENTS = metrics_store.table('events')   # sqlite: events / postgres: ops.metrics_events
if metrics_store.enabled():
    con, cur = metrics_store.connect()
else:
    con = sqlite3.connect(DB); cur = con.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS events(
  hash TEXT PRIMARY KEY, ts TEXT, instance TEXT, capability TEXT, agent TEXT,
  client TEXT, grp TEXT, session TEXT, kind TEXT, replies INTEGER, detail TEXT)''')

ts_re      = re.compile(r'(\d{4}-\d\d-\d\dT[\d:.]+[+\-]\d\d:\d\d)')
agent_re   = re.compile(r'feishu\[([^\]]+)\]')
recv_re    = re.compile(r'received message from (\S+?) in (\S+)')
recvdm_re  = re.compile(r'received message from (\S+)')
sess_re    = re.compile(r'session=(agent:[^\s)]+)')
replies_re = re.compile(r'replies=(\d+)')
grp_re     = re.compile(r':group:([A-Za-z0-9_]+)')
dm_re      = re.compile(r':direct:([A-Za-z0-9_]+)')
err_re     = re.compile(r'insufficient[ _]balance|out of credits|insufficient_quota|billing|rawError=402|402 Insufficient Balance|exception|traceback|failed', re.I)
benign_re  = re.compile(r'failed to resolve sender name|no user authority error|started streaming|closed streaming|gateway already running|lock timeout|exiting with code 78|port \d+ is already in use|resolving authentication', re.I)

ins = 0
for unit, capability in UNITS.items():
    try:
        out = subprocess.run(
            ['journalctl', '--user', '-u', unit, '--since', SINCE, '-o', 'cat', '--no-pager'],
            capture_output=True, text=True, timeout=180).stdout
    except Exception as e:
        print(f'{unit}: journalctl 读取失败: {e}'); continue
    last_loc = {}   # (agent) -> (client, grp)  当前 agent 最近一次定位到的客户
    for line in out.splitlines():
        is_feishu = '[feishu]' in line
        if benign_re.search(line):
            continue
        is_err = bool(err_re.search(line))
        if not (is_feishu or is_err):
            continue
        tm = ts_re.search(line); ts = tm.group(1) if tm else ''
        am = agent_re.search(line); agent = am.group(1) if am else ''
        kind = client = grp = session = detail = ''; replies = None
        if 'received message' in line:
            kind = 'received'
            m = recv_re.search(line)
            if m: client, grp = m.group(1), m.group(2)
            else:
                m = recvdm_re.search(line)
                if m: client = m.group(1)
            if agent: last_loc[agent] = (client, grp)
        elif 'dispatching to agent' in line:
            kind = 'dispatch'
            sm = sess_re.search(line); session = sm.group(1) if sm else ''
            if session:
                gm = grp_re.search(session); dmm = dm_re.search(session)
                if gm: grp = gm.group(1)
                elif dmm: client = dmm.group(1)
            if agent and (client or grp): last_loc[agent] = (client, grp)
        elif 'dispatch complete' in line:
            kind = 'complete'
            rm = replies_re.search(line); replies = int(rm.group(1)) if rm else 0
            sm = sess_re.search(line)
            if sm:
                session = sm.group(1)
                gm = grp_re.search(session); dmm = dm_re.search(session)
                if gm: grp = gm.group(1)
                elif dmm: client = dmm.group(1)
            if not (client or grp) and agent in last_loc:   # 关键: 借用最近定位
                client, grp = last_loc[agent]
        elif is_err:
            kind = 'error'; detail = line.strip()[-140:]
        else:
            continue
        h = hashlib.md5((unit + '|' + line).encode('utf-8', 'ignore')).hexdigest()
        cur.execute('INSERT OR IGNORE INTO ' + EVENTS + ' VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    (h, ts, unit, capability, agent, client, grp, session, kind, replies, detail))
        ins += cur.rowcount
con.commit()
print(f'本次新增 {ins} 条; 库内总计 {cur.execute("select count(*) from " + EVENTS).fetchone()[0]} 条事件')
