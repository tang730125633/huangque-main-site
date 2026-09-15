#!/usr/bin/env python3
# 读 OpenClaw 结构化会话文件 *.trajectory.jsonl(schema版本化) → runs 表(含 token/成本基础)。
# 取代脆弱的 grep journald。每个 run = 一次 session.started→session.ended。可重复跑(按 file+runId 去重)。
#
# M3 metrics 域改造（2026-09-16）：只在「连接点 + 表名」加分发，SQLite 分支与迁移前逐字节一致。
#   HQ_METRICS_STORE=sqlite（默认）→ 原来的 SQLite 路径，行为完全不变。
#   HQ_METRICS_STORE=postgres     → 写 ops.metrics_runs（表由 alembic 20260915_0010 建好）。
#   本脚本的 SQL 两种模式都合法：sum(replied) 是整数列求和，不是 SQLite 的布尔取巧写法。
import os, json, glob, sqlite3, hashlib, re
import metrics_store

BASE = os.path.expanduser('~/agent-metrics')
DB = BASE + '/metrics.db'
INST = {'.openclaw': '获客', '.openclaw-second': '文案', '.openclaw-visual': '图片'}

RUNS = metrics_store.table('runs')   # sqlite: runs / postgres: ops.metrics_runs
if metrics_store.enabled():
    con, cur = metrics_store.connect()
else:
    con = sqlite3.connect(DB); cur = con.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS runs(
  run_key TEXT PRIMARY KEY, ts TEXT, instance TEXT, capability TEXT, agent TEXT,
  chat_id TEXT, session_key TEXT, provider TEXT, model TEXT,
  tok_in INT, tok_out INT, tok_total INT, cache_read INT,
  status TEXT, replied INT, model_calls INT, trigger_ TEXT, msg_provider TEXT)''')

def parse_sk(sk):
    parts = (sk or '').split(':')
    agent = parts[1] if len(parts) > 1 else ''
    m = re.search(r'(oc_[A-Za-z0-9]+|ou_[A-Za-z0-9]+)', sk or '')
    return agent, (m.group(1) if m else '')

ins = 0
for inst, cap in INST.items():
    for f in glob.glob(os.path.expanduser(f'~/{inst}/agents/*/sessions/*.trajectory.jsonl')):
        runs = {}
        try:
            for line in open(f, encoding='utf-8', errors='ignore'):
                line = line.strip()
                if not line: continue
                try: o = json.loads(line)
                except: continue
                rid = o.get('runId') or o.get('sessionId')
                if not rid: continue
                r = runs.setdefault(rid, {'sk': o.get('sessionKey', ''), 'prov': o.get('provider', ''),
                    'model': o.get('modelId', ''), 'ts': o.get('ts', ''), 'ti': 0, 'to': 0, 'tt': 0, 'cr': 0,
                    'status': '', 'replied': 0, 'mc': 0, 'trig': '', 'mp': '', 'adata': ''})
                t = o.get('type'); data = o.get('data', {}) or {}
                if o.get('modelId'): r['model'] = o['modelId']; r['prov'] = o.get('provider', r['prov'])
                if t == 'session.started':
                    r['ts'] = o.get('ts', r['ts']); r['trig'] = data.get('trigger', ''); r['mp'] = data.get('messageProvider', ''); r['adata'] = data.get('agentId', '')
                elif t == 'model.completed':
                    r['mc'] += 1
                    u = data.get('usage', {}) or {}
                    r['ti'] += u.get('input', 0) or 0; r['to'] += u.get('output', 0) or 0
                    r['tt'] += u.get('total', 0) or 0; r['cr'] += u.get('cacheRead', 0) or 0
                    if data.get('assistantTexts'): r['replied'] = 1
                elif t == 'session.ended':
                    r['status'] = data.get('status', '')
        except: continue
        for rid, r in runs.items():
            agent, chat = parse_sk(r['sk'])
            if not agent: agent = r['adata']
            key = hashlib.md5((f + '|' + rid).encode()).hexdigest()
            cur.execute('INSERT OR IGNORE INTO ' + RUNS + ' VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (key, r['ts'], inst, cap, agent, chat, r['sk'], r['prov'], r['model'],
                 r['ti'], r['to'], r['tt'], r['cr'], r['status'], r['replied'], r['mc'], r['trig'], r['mp']))
            ins += cur.rowcount
con.commit()
print(f"runs 新增 {ins}; 库内 runs 共 {cur.execute('select count(*) from ' + RUNS).fetchone()[0]}")
print("=== 各能力组(feishu run): 运行数 / 回复数 / token合计 ===")
for r in cur.execute("select capability, count(*) n, sum(replied) rep, sum(tok_total) tt from " + RUNS + " where msg_provider='feishu' group by capability"):
    print("  %s: %d 运行 / %s 回复 / %s tokens" % (r[0], r[1], r[2], r[3]))
print("=== 模型分布 ===");
for r in cur.execute("select provider, model, count(*) n, sum(tok_total) tt from " + RUNS + " group by provider, model order by 4 desc limit 6"):
    print("  %s/%s: %d 次, %s tokens" % (r[0], r[1], r[2], r[3]))
