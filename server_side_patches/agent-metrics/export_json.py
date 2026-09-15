#!/usr/bin/env python3
# 读 metrics.db + name_map + bot_groups → dashboard_data.json
# v4: 客户数=当前所在的"外部群"个数(按群计, 排除测试/内部/私聊/历史)。群分类可被 group_tags.json 人工覆盖。
#
# M3 metrics 域改造（2026-09-16）：只在「连接点 + 表名」加分发，SQLite 分支与迁移前逐字节一致。
#   HQ_METRICS_STORE=sqlite（默认）→ 原来的 SQLite 路径，行为完全不变。
#   HQ_METRICS_STORE=postgres     → 读 ops.metrics_events / ops.metrics_runs。
# SQL 等价改写（结果集实测逐行一致，见迁移报告）：
#   sum(kind='received') → sum(case when kind='received' then 1 else 0 end)
#     （PG 没有 sum(boolean)；SQLite 两写法等价）
#   grp like 'oc_%' → grp like ?（把通配串当参数传，避开占位符转义）
# by_client 一条查询在两种模式下写法不同，原因见 BY_CLIENT_* 注释。
import sqlite3, os, json, datetime
import metrics_store

BASE = os.path.expanduser('~/agent-metrics')
DB, OUT = BASE + '/metrics.db', BASE + '/web/dashboard_data.json'
EV = metrics_store.table('events')   # sqlite: events / postgres: ops.metrics_events
RN = metrics_store.table('runs')     # sqlite: runs   / postgres: ops.metrics_runs
if metrics_store.enabled():
    con, cur = metrics_store.connect()
else:
    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row; cur = con.cursor()
def q(sql, params=None):
    rows = cur.execute(sql, params) if params else cur.execute(sql)
    return [dict(r) for r in rows.fetchall()]

# by_client 一条查询两种模式写法不同：
#   SQLite 原文 `group by capability, agent, client` 里 client 既是输出别名又是输入列，实测
#   SQLite 取**输入列** events.client（与显式写 events.client 的结果逐行相同）；但同一个 SELECT
#   里还留了裸列 grp，PG 不允许裸列 —— 所以 PG 侧显式按输入列分组、显示列改用确定性 max(...)。
#   数据量小、切换后由 PG 权威重算，故两模式都保留「按输入列 client 分组」的原始口径：
#   分组键、dispatch/reply 计数、排序、limit 完全一致，只有「客户标签」这一列可能取到组内不同成员
#   （SQLite 是未定义顺序的任意一行；PG 取组内最大的 grp，确定性）。
BY_CLIENT_SQLITE = '''select capability, agent,
      case when grp!='' then grp else client end client,
      sum(case when kind='dispatch' then 1 else 0 end) rounds,
      sum(case when kind='complete' and replies>0 then 1 else 0 end) replied
      from ''' + EV + ''' where (grp!='' or client!='')
      group by capability, agent, client
      having sum(case when kind='dispatch' then 1 else 0 end)>0 order by 4 desc limit 60'''
BY_CLIENT_POSTGRES = '''select capability, agent,
      max(case when grp!='' then grp else client end) client,
      sum(case when kind='dispatch' then 1 else 0 end) rounds,
      sum(case when kind='complete' and replies>0 then 1 else 0 end) replied
      from ''' + EV + ''' where (grp!='' or client!='')
      group by capability, agent, ''' + EV + '''.client
      having sum(case when kind='dispatch' then 1 else 0 end)>0 order by 4 desc limit 60'''
BY_CLIENT = BY_CLIENT_POSTGRES if metrics_store.enabled() else BY_CLIENT_SQLITE

nm = json.load(open(BASE + '/name_map.json')) if os.path.exists(BASE + '/name_map.json') else {}
bot_groups = json.load(open(BASE + '/bot_groups.json')) if os.path.exists(BASE + '/bot_groups.json') else []
TAGS = json.load(open(BASE + '/group_tags.json')) if os.path.exists(BASE + '/group_tags.json') else {}
def gname(oc): return nm.get(oc) or (oc[:8] + '…' + oc[-4:] if oc and len(oc) > 14 else oc)

def gtype(oc, name):
    """customer(外部客户) / internal(内部·自己人) / test(测试·未命名·私聊)。group_tags.json 可覆盖。"""
    if oc in TAGS: return TAGS[oc]
    if str(oc).startswith('ou_'): return 'internal'        # 私聊
    s = name or ''
    if any(k in s.lower() for k in ['测试', 'test', '父openclaw', '开发测试']): return 'test'
    if any(k in s for k in ['小方', '唐泽龙', '大鹏', '东晟', '陈远志']): return 'internal'
    if (not s) or s.startswith('oc_'): return 'test'        # 未命名/拿不到名(多半历史/废弃)
    return 'customer'

# 每个 capability / agent 当前所在的"外部客户群"(去重, 按群)
cap_cust, agent_cust = {}, {}
for b in bot_groups:
    for g in b['in_groups']:
        if str(g['id']).startswith('oc_'):   # 一个群聊 = 一个客户(= 一个成交); 只排除私聊
            cap_cust.setdefault(b['capability'], set()).add(g['id'])
            agent_cust.setdefault((b['capability'], b['agent']), set()).add(g['id'])

span = cur.execute("select min(ts) a, max(ts) b from " + EV + " where ts!=''").fetchone()
data = {
  'generated_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M'),
  'span': {'from': (span['a'] or '')[:16], 'to': (span['b'] or '')[:16]},
  'total_events': cur.execute('select count(*) from ' + EV).fetchone()[0],
  'by_capability': q('''select capability,
      sum(case when kind='received' then 1 else 0 end) received,
      sum(case when kind='dispatch' then 1 else 0 end) dispatched,
      sum(case when kind='complete' and replies>0 then 1 else 0 end) replied,
      sum(case when kind='error' then 1 else 0 end) errors
      from ''' + EV + ''' group by capability order by 2 desc'''),
  'by_client': q(BY_CLIENT),
  'errors': q('''select capability, count(*) count, max(detail) sample
      from ''' + EV + ''' where kind='error' group by capability, detail order by 2 desc limit 12'''),
}
for r in data['by_capability']:
    r['reply_rate'] = round(100*r['replied']/r['dispatched']) if r['dispatched'] else 0
    r['clients'] = len(cap_cust.get(r['capability'], set()))   # 客户数=外部群数(按你定义)

cur_groups = {}
for b in bot_groups:
    cur_groups.setdefault((b['capability'], b['agent']), set()).update(g['id'] for g in b['in_groups'])
for r in data['by_client']:
    r['client_id'] = r['client']; r['client'] = gname(r['client'])
    r['current'] = (r['client_id'] in cur_groups.get((r['capability'], r['agent']), set()))
    r['type'] = gtype(r['client_id'], r['client'])

# 流量统计供花名册
traf = {}
for r in cur.execute('''select capability, agent,
    sum(case when kind='dispatch' then 1 else 0 end) dispatched,
    sum(case when kind='complete' and replies>0 then 1 else 0 end) replied
    from ''' + EV + ''' where agent!='' group by capability, agent'''):
    traf[(r['capability'], r['agent'])] = r
disp = {}
for r in cur.execute('''select agent, grp, sum(case when kind='dispatch' then 1 else 0 end) d
    from ''' + EV + ''' where grp like ? group by agent, grp''', ('oc_%',)):
    disp[(r['agent'], r['grp'])] = r['d']

data['by_agent'], data['by_agent_groups'] = [], []
for b in bot_groups:
    cap, ag = b['capability'], b['agent']
    t = traf.get((cap, ag))
    d_, rep = (t['dispatched'], t['replied']) if t else (0, 0)
    data['by_agent'].append({'capability': cap, 'agent': ag,
        'clients': len(agent_cust.get((cap, ag), set())),
        'dispatched': d_, 'replied': rep, 'reply_rate': round(100*rep/d_) if d_ else 0, 'idle': (d_ == 0)})
    groups = [{'group': g['name'], 'dispatched': disp.get((ag, g['id']), 0), 'type': gtype(g['id'], g['name'])}
              for g in b['in_groups']]
    if groups:
        data['by_agent_groups'].append({'capability': cap, 'agent': ag, 'group_count': len(groups),
            'groups': sorted(groups, key=lambda x: -x['dispatched'])})
data['by_agent'].sort(key=lambda x: (x['capability'], -x['dispatched']))
data['by_agent_groups'].sort(key=lambda x: (x['capability'], x['agent']))

# ===== 成本账本 (来自 trajectory runs 表) =====
PRICES = json.load(open(BASE + '/model_prices.json')) if os.path.exists(BASE + '/model_prices.json') else {}
def cost_of(model, ti, to, cr):
    p = PRICES.get(model) or PRICES.get('default') or {'input': 2, 'output': 8, 'cache_read': 0.5}
    return (ti or 0)/1e6*p['input'] + (to or 0)/1e6*p['output'] + (cr or 0)/1e6*p.get('cache_read', 0)
try:
    runs = [dict(r) for r in cur.execute("select capability, agent, chat_id, provider, model, tok_in, tok_out, tok_total, cache_read from " + RN)]
except Exception:
    runs = []
cap_cost, agent_cost, model_agg, client_cost = {}, {}, {}, {}
for r in runs:
    c = cost_of(r['model'], r['tok_in'], r['tok_out'], r['cache_read'])
    cap_cost.setdefault(r['capability'], [0, 0.0]); cap_cost[r['capability']][0] += r['tok_total'] or 0; cap_cost[r['capability']][1] += c
    k = (r['capability'], r['agent']); agent_cost.setdefault(k, [0, 0.0]); agent_cost[k][0] += r['tok_total'] or 0; agent_cost[k][1] += c
    mk = (r['provider'], r['model']); model_agg.setdefault(mk, [0, 0, 0.0]); model_agg[mk][0] += 1; model_agg[mk][1] += r['tok_total'] or 0; model_agg[mk][2] += c
    if r['chat_id']:
        client_cost.setdefault(r['chat_id'], [0, 0.0]); client_cost[r['chat_id']][0] += r['tok_total'] or 0; client_cost[r['chat_id']][1] += c
for r in data['by_capability']:
    cc = cap_cost.get(r['capability'], [0, 0.0]); r['tokens'] = cc[0]; r['cost_cny'] = round(cc[1], 2)
for r in data['by_agent']:
    cc = agent_cost.get((r['capability'], r['agent']), [0, 0.0]); r['tokens'] = cc[0]; r['cost_cny'] = round(cc[1], 2)
data['by_model'] = [{'provider': p, 'model': m, 'runs': v[0], 'tokens': v[1], 'cost_cny': round(v[2], 2)}
                    for (p, m), v in sorted(model_agg.items(), key=lambda x: -x[1][2])]
data['top_cost_clients'] = sorted([{'client': gname(cid), 'tokens': v[0], 'cost_cny': round(v[1], 2)}
                                   for cid, v in client_cost.items()], key=lambda x: -x['cost_cny'])[:15]
rng = cur.execute("select min(ts), max(ts) from " + RN + " where ts!=''").fetchone()
data['cost_summary'] = {'window': {'from': (rng[0] or '')[:16], 'to': (rng[1] or '')[:16]},
    'total_tokens': sum(r['tok_total'] or 0 for r in runs),
    'total_cost_cny': round(sum(cost_of(r['model'], r['tok_in'], r['tok_out'], r['cache_read']) for r in runs), 2)}

# ===== 图片生成成本 (gpt-image-2, 按 visual 产出文件数 × 单价; token账单抓不到图片费) =====
import glob as _glob
_imgdir = os.path.expanduser('~/.openclaw-visual/media/outbound')
_imgs = []
for _ext in ('*.png', '*.jpg', '*.jpeg', '*.webp'):
    _imgs += _glob.glob(_imgdir + '/' + _ext)
_imgn = len(_imgs)
_ip = PRICES.get('image', {}) or {}
_per = _ip.get('gpt-image-2_usd', 0.2); _rate = _ip.get('usd_to_cny', 7.2)
_imgcost = round(_imgn * _per * _rate, 2)
data['cost_summary']['image_count'] = _imgn
data['cost_summary']['image_unit'] = '$%s/张 ×%s汇率' % (_per, _rate)
data['cost_summary']['image_cost_cny'] = _imgcost
data['cost_summary']['token_cost_cny'] = data['cost_summary'].get('total_cost_cny', 0)
data['cost_summary']['total_cost_cny'] = round(data['cost_summary'].get('total_cost_cny', 0) + _imgcost, 2)
for r in data['by_capability']:
    if r['capability'] == '图片':
        r['cost_cny'] = round((r.get('cost_cny') or 0) + _imgcost, 2); r['image_count'] = _imgn
if _imgn:
    data['by_model'].append({'provider': 'openai', 'model': 'gpt-image-2 (图片×%d张)' % _imgn, 'runs': _imgn, 'tokens': 0, 'cost_cny': _imgcost})
    data['by_model'].sort(key=lambda x: -x['cost_cny'])

json.dump(data, open(OUT, 'w'), ensure_ascii=False, indent=1)
print("客户数(外部群,按你定义):", {r['capability']: r['clients'] for r in data['by_capability']})
# 顺带统计各 capability 当前群的分类构成
comp = {}
for b in bot_groups:
    for g in b['in_groups']:
        comp.setdefault(b['capability'], {}).setdefault(gtype(g['id'], g['name']), set()).add(g['id'])
for capn, dd in comp.items():
    print("  %s 当前群构成:" % capn, {k: len(v) for k, v in dd.items()})
