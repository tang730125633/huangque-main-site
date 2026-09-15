#!/usr/bin/env python3
# 读 metrics.db → 输出 Markdown 会话看板
#
# M3 metrics 域改造（2026-09-16）：只在「连接点 + 表名」加分发，SQLite 分支与迁移前逐字节一致。
#   HQ_METRICS_STORE=sqlite（默认）→ 原来的 SQLite 路径，行为完全不变。
#   HQ_METRICS_STORE=postgres     → 读 ops.metrics_events。
# 三处 SQL 改成两种模式都合法的等价写法（结果集实测逐行一致，见迁移报告）：
#   1) sum(kind='received')          → sum(case when kind='received' then 1 else 0 end)
#      （PG 没有 sum(boolean)，SQLite 两写法等价）
#   2) group by capability, agent, cli → 把 cli 的表达式原样写进 GROUP BY
#      （PG 的 GROUP BY 不接受输出别名；SQLite 原本也是解析到该表达式）
#   3) ④ 的裸列 agent → max(agent)（PG 要求非聚合列进 GROUP BY；SQLite 取组内任意一行的值，
#      实测 8 行输出与 max(agent) 完全一致）
import sqlite3, os
import metrics_store

DB = os.path.expanduser('~/agent-metrics/metrics.db')
EV = metrics_store.table('events')   # sqlite: events / postgres: ops.metrics_events
if metrics_store.enabled():
    con, cur = metrics_store.connect()
else:
    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row; cur = con.cursor()
def q(sql): return cur.execute(sql).fetchall()

tot = cur.execute("select count(*) from " + EV).fetchone()[0]
span = cur.execute("select min(ts), max(ts) from " + EV + " where ts!=''").fetchone()
print('# 🗂️ AI 会话看板 v1')
print(f"> 事件 {tot} 条 ｜ 跨度 {(span[0] or '?')[:16]} ~ {(span[1] or '?')[:16]}\n")

print('## ① 按能力组')
print('| 能力 | 客户数 | 收到 | 回复数 | 回复率 | 报错 |')
print('|---|--:|--:|--:|--:|--:|')
for r in q('''select capability,
  count(distinct case when grp!='' then grp else client end) clients,
  sum(case when kind='received' then 1 else 0 end) recv,
  sum(case when kind='complete' then replies else 0 end) reps,
  sum(case when kind='error' then 1 else 0 end) errs
  from ''' + EV + ''' group by capability order by 3 desc'''):
    rate = f"{100*r['reps']//r['recv']}%" if r['recv'] else '—'
    print(f"| {r['capability']} | {r['clients']} | {r['recv']} | {r['reps']} | {rate} | {r['errs']} |")

print('\n## ② 按子 agent（谁在真干活）')
print('| 能力 | agent | 客户数 | 收到 | 回复 |')
print('|---|---|--:|--:|--:|')
for r in q('''select capability, agent,
  count(distinct case when grp!='' then grp else client end) clients,
  sum(case when kind='received' then 1 else 0 end) recv,
  sum(case when kind='complete' then replies else 0 end) reps
  from ''' + EV + ''' where agent!='' group by capability, agent
  having sum(case when kind='received' then 1 else 0 end)>0 order by 4 desc limit 15'''):
    print(f"| {r['capability']} | {r['agent']} | {r['clients']} | {r['recv']} | {r['reps']} |")

print('\n## ③ 按客户/会话（一个客户一行）')
print('| 能力 | agent | 客户(群/人) | 轮数 | 回复 |')
print('|---|---|---|--:|--:|')
for r in q('''select capability, agent,
  case when grp!='' then grp else client end cli,
  sum(case when kind='received' then 1 else 0 end) rounds,
  sum(case when kind='complete' then replies else 0 end) reps
  from ''' + EV + ''' where (grp!='' or client!='')
  group by capability, agent, case when grp!='' then grp else client end
  having sum(case when kind='received' then 1 else 0 end)>0 order by 4 desc limit 15'''):
    cli = r['cli'] or ''
    cli = (cli[:16] + '…') if len(cli) > 17 else cli
    print(f"| {r['capability']} | {r['agent']} | {cli} | {r['rounds']} | {r['reps']} |")

print('\n## ④ 报错 Top')
rows = q('''select capability, max(agent) agent, detail, count(*) n from ''' + EV + '''
  where kind='error' group by capability, detail order by 4 desc limit 8''')
if rows:
    print('| 能力 | agent | 次数 | 摘要 |')
    print('|---|---|--:|---|')
    for r in rows:
        d = (r['detail'] or '')[:55].replace('|', '/')
        print(f"| {r['capability']} | {r['agent'] or '-'} | {r['n']} | {d} |")
else:
    print('（无报错记录 🎉）')
