"""Admin termination ledger, atomic with job terminal state and refund intent."""
import contextvars
import json
import sqlite3
import subprocess
import time
from contextlib import closing, contextmanager

_current = contextvars.ContextVar('terminating_task', default=None)


class TaskTerminated(RuntimeError):
    pass


def ensure(c):
    c.execute('''CREATE TABLE IF NOT EXISTS admin_task_terminations(
        job_id INTEGER PRIMARY KEY, actor TEXT NOT NULL, reason TEXT NOT NULL,
        requested_at REAL NOT NULL, previous_state TEXT NOT NULL,
        local_stopped_at REAL, provider_id TEXT NOT NULL DEFAULT '',
        remote_state TEXT NOT NULL)''')


def supported(row):
    try:
        payload = json.loads(row['payload'] or '{}')
    except (TypeError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    return (row['kind']=='matrix_template_video' and payload.get('mode')!='timeline'
            and not payload.get('_channel_binding'))


def get(c, job_id):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='admin_task_terminations'").fetchone():
        return None
    c.row_factory = sqlite3.Row
    row=c.execute('SELECT * FROM admin_task_terminations WHERE job_id=?',(job_id,)).fetchone()
    return dict(row) if row else None


def describe(c, job_id):
    c.row_factory = sqlite3.Row
    row=c.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
    if not row:
        return {'can_terminate':False,'unavailable_reason':'任务不存在'}
    info=get(c,job_id)
    if info:
        info['platform_state']='stopped' if info['local_stopped_at'] else 'stopping'
        info['refund_state']='not_needed' if int(row['cost'] or 0)<=0 else 'refunded' if row['refunded']==1 else 'pending'
    if not supported(row):
        reason='该任务类型尚未接入终止能力'
    elif row['status'] not in {'pending','running'}:
        reason='任务已结束，无法终止；请刷新查看最新状态'
    else:
        reason=''
    return {'can_terminate':not info and row['status'] in {'pending','running'} and supported(row),
            'unavailable_reason':reason,
            'termination':info}


def request(db_factory, job_id, actor, reason):
    reason=str(reason or '').strip()
    if not 2<=len(reason)<=200:
        raise ValueError('请填写2～200字的终止原因')
    with closing(db_factory()) as c:
        ensure(c)
        c.commit()
        c.execute('BEGIN IMMEDIATE')
        existing=get(c,job_id)
        if existing:
            c.commit()
            result=describe(c,job_id)
            result['created']=False
            return result
        row=c.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
        if not row:
            raise ValueError('任务不存在')
        if not supported(row):
            raise ValueError('当前仅支持普通模板成片任务，其他类型尚未接入')
        if row['status'] not in {'pending','running'}:
            raise ValueError('任务已结束，无法终止；请刷新查看最新状态')
        payload=json.loads(row['payload'] or '{}')
        provider_id=str((payload.get('_matrix_runtime') or {}).get('provider_job_id') or '')
        now=time.time()
        stopped=now if row['status']=='pending' else None
        remote='not_submitted' if stopped and not payload.get('_matrix_runtime') else 'unconfirmed'
        c.execute('INSERT INTO admin_task_terminations VALUES(?,?,?,?,?,?,?,?)',
                  (job_id,actor,reason,now,row['status'],stopped,provider_id,remote))
        c.execute("UPDATE jobs SET status='error',error=?,updated_at=?,refunded=CASE WHEN COALESCE(cost,0)>0 AND COALESCE(refunded,0)=0 THEN 2 ELSE refunded END WHERE id=? AND status IN ('pending','running')",
                  ('管理员终止：'+reason,int(now),job_id))
        c.commit()
        result=describe(c,job_id)
        result['created']=True
        return result


@contextmanager
def scope(job_id, db_factory):
    token=_current.set((job_id,db_factory))
    try:
        check()
        yield
    finally:
        _current.reset(token)


def acknowledge(db_factory, job_id):
    """Called only by the worker that claimed the job, after handler cleanup."""
    with closing(db_factory()) as c:
        if get(c,job_id):
            c.execute('UPDATE admin_task_terminations SET local_stopped_at=COALESCE(local_stopped_at,?) WHERE job_id=?',(time.time(),job_id))
            c.commit()


def sleep(seconds):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        check()
        time.sleep(min(.25,max(0,deadline-time.monotonic())))
    check()


def check():
    current=_current.get()
    if current:
        with closing(current[1]()) as c:
            if get(c,current[0]):
                raise TaskTerminated('管理员已终止任务')


def provider_submitted(provider_id):
    current=_current.get()
    if current and provider_id:
        with closing(current[1]()) as c:
            # Persist the receipt atomically with cancellation. A cancellation between
            # receiving this ID and _persist_runtime must not lose the remote order.
            c.row_factory = sqlite3.Row
            c.execute('BEGIN IMMEDIATE')
            if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone():
                return  # Standalone renderer invocations have no job store.
            row=c.execute('SELECT payload FROM jobs WHERE id=?',(current[0],)).fetchone()
            if row:
                payload=json.loads(row['payload'] or '{}')
                runtime=payload.setdefault('_matrix_runtime',{})
                runtime['provider_job_id']=str(provider_id)
                c.execute('UPDATE jobs SET payload=? WHERE id=?',(json.dumps(payload,ensure_ascii=False),current[0]))
            if get(c,current[0]):
                c.execute("UPDATE admin_task_terminations SET provider_id=?,remote_state='unconfirmed' WHERE job_id=?",(str(provider_id),current[0]))
            c.commit()


def run_process(command, **kwargs):
    if not _current.get():
        return subprocess.run(command, **kwargs)
    check()
    timeout=kwargs.pop('timeout',120)
    must_check=kwargs.pop('check',False)
    kwargs.pop('capture_output',None)
    process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,**kwargs)
    deadline=time.monotonic()+timeout
    try:
        while True:
            check()
            if time.monotonic()>=deadline:
                raise subprocess.TimeoutExpired(command,timeout)
            try:
                out,err=process.communicate(timeout=min(.3,max(.01,deadline-time.monotonic())))
                break
            except subprocess.TimeoutExpired:
                continue
        if must_check and process.returncode:
            raise subprocess.CalledProcessError(process.returncode,command,out,err)
        return subprocess.CompletedProcess(command,process.returncode,out,err)
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate()
