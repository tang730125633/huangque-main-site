"""Versioned channel operations. Deletion is reversible; execution snapshots survive."""
import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from . import channel_manager as store

LEGACY_SCOPES = {
    'xai': '果肉视频新任务', 'minimax': 'MiniMax 视频新任务',
    'gemini': 'Nano Banana 图片和 Omni 视频新任务',
    'seedance': 'Seedream 图片和 Seedance 视频新任务',
    'openai': 'OpenAI 图片和 Sora 视频新任务',
}


def legacy_states(connection=None):
    if connection is None:
        with closing(store.db()) as c:
            return legacy_states(c)
    row=connection.execute('SELECT value FROM settings WHERE id=2').fetchone()
    return json.loads(row[0]) if row else {}


def legacy_provider(kind,payload):
    if kind=='image':
        provider=str(payload.get('provider') or 'openai').strip().lower()
        return {'banana':'gemini','openai':'openai','seedream':'seedance','xiaole':None,'zelong2':None}.get(provider,'openai')
    if kind=='xiaole_video':
        return {'grok':'xai','minimax':'minimax','omni':'gemini','micro':'seedance'}.get(str(payload.get('channel') or 'grok').strip().lower())
    if kind=='sora_video':
        return 'openai'
    return None


def require_legacy(kind,payload):
    provider=legacy_provider(kind,payload)
    if provider:
        current=legacy_states().get(provider,{})
        if current.get('enabled') is False:
            raise ValueError('该供应商已暂停新任务：'+LEGACY_SCOPES[provider])


def mutate_legacy(actor,body):
    key=str(body.get('id') or '')
    action=body.get('action')
    reason=str(body.get('reason') or '').strip()
    if key not in LEGACY_SCOPES or action not in {'enable','disable'}:
        raise ValueError('内置供应商有固定功能引用；仅已接入的任务线路支持启停，不支持删除')
    if not 2<=len(reason)<=200:
        raise ValueError('请填写 2～200 字原因')
    with closing(store.db()) as c:
        c.execute('BEGIN IMMEDIATE')
        states=legacy_states(c)
        old=states.get(key,{'revision':0,'enabled':True})
        if body.get('version')!=old['revision']:
            raise ValueError('供应商状态已变化，请刷新后重新确认')
        current={'revision':old['revision']+1,'enabled':action=='enable','actor':actor,
                 'reason':reason,'at':time.time(),'scope':LEGACY_SCOPES[key]}
        states[key]=current
        c.execute('INSERT OR REPLACE INTO settings VALUES(2,?)',(json.dumps(states,ensure_ascii=False),))
        store._audit(c,'legacy.'+action,key+' · v'+str(current['revision'])+' · '+reason,actor)
        c.commit()
    return current


def active_jobs(cid):
    path = Path(os.environ.get('CONTENT_JOB_DB', str(Path(__file__).resolve().parents[1] / 'content_jobs.db')))
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as c:
            rows = c.execute("SELECT id,payload FROM jobs WHERE status IN ('pending','running')").fetchall()
    except sqlite3.Error as exc:
        raise ValueError('任务存储暂不可读，不能确认是否有未结束任务；暂不可删除') from exc
    found=[]
    for jid, raw in rows:
        try:
            payload=json.loads(raw or '{}')
        except (TypeError,ValueError):
            raise ValueError('存在无法核对的任务记录，暂不可删除')
        if not isinstance(payload,dict) or not isinstance(payload.get('_channel_binding') or {},dict):
            raise ValueError('存在无法核对的任务绑定，暂不可删除')
        if (payload.get('_channel_binding') or {}).get('id')==cid:
            found.append(jid)
    return found


def mutate(actor, body):
    cid=str(body.get('id') or '')
    action=body.get('action')
    reason=str(body.get('reason') or '').strip()
    if action not in {'enable','disable','delete','restore'} or not 2<=len(reason)<=200:
        raise ValueError('请选择有效操作并填写 2～200 字原因')
    with closing(store.db()) as c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
        if not row:
            raise ValueError('渠道不存在')
        if body.get('version') != row['version']:
            raise ValueError('渠道已被修改，请刷新后重新确认')
        previous=c.execute('SELECT config,secret FROM versions WHERE channel=? AND version=?',(cid,row['version'])).fetchone()
        config=json.loads(previous['config'])
        deleted=bool(config.get('_lifecycle',{}).get('deleted'))
        if deleted and action!='restore':
            raise ValueError('渠道已在回收站，请先恢复')
        if not deleted and action=='restore':
            raise ValueError('渠道不在回收站')
        mappings=[json.loads(r[0]) for r in c.execute('SELECT config FROM mappings')]
        references=[m for m in mappings if cid in {m.get('channel'),m.get('backup')}]
        if action=='delete':
            if row['enabled']:
                raise ValueError('请先停用渠道，再移入回收站')
            if references:
                raise ValueError('渠道仍被主/备用映射引用，请先切换或删除关联映射')
            if c.execute("SELECT 1 FROM runs WHERE channel=? AND state IN ('queued','running','unknown') LIMIT 1",(cid,)).fetchone():
                raise ValueError('存在执行中或结果未知的调用，请先核对，暂不可删除')
            if active_jobs(cid):
                raise ValueError('渠道仍有未结束任务，暂不可删除')
        if action=='enable' and not all(config.get(k) for k in ('adapter','base_url','model')):
            raise ValueError('渠道配置不完整，不能启用')
        enabled=action=='enable'
        revision=row['version']+1
        config['_lifecycle']={'deleted':action=='delete','action':action,'reason':reason,
                              'actor':actor,'at':time.time(),'previous_version':row['version']}
        c.execute('INSERT INTO versions VALUES(?,?,?,?,?,?)',(cid,revision,json.dumps(config,ensure_ascii=False),previous['secret'],actor,time.time()))
        c.execute('UPDATE channels SET version=?,enabled=? WHERE id=?',(revision,int(enabled),cid))
        if enabled:
            c.execute('INSERT OR REPLACE INTO schedule VALUES(?,?,?)',(cid,time.time()+60,store._next_daily(config.get('daily_hour',9))))
        else:
            c.execute('DELETE FROM schedule WHERE channel=?',(cid,))
        store._audit(c,'channel.'+action,cid+' · v'+str(revision)+' · '+reason,actor)
        c.commit()
    return {'id':cid,'version':revision,'enabled':enabled,'deleted':action=='delete'}


def unmap(actor, body):
    selector=str(body.get('selector') or '')
    with closing(store.db()) as c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute('SELECT config FROM mappings WHERE selector=?',(selector,)).fetchone()
        if not row or json.loads(row[0]) != body.get('expected'):
            raise ValueError('映射已变化，请刷新后重试')
        c.execute('DELETE FROM mappings WHERE selector=?',(selector,))
        store._audit(c,'mapping.delete',selector,actor)
        c.commit()
    return {'ok':True}
