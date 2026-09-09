"""Bounded managed-channel execution and tests; never retry a generation POST."""
import base64
import io
import json
import socket
import ipaddress
import time
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import closing

from . import channel_manager as store, runtime_observability as trace


class OutcomeUnknown(RuntimeError):
    pass


class CheckUnsupported(ValueError):
    pass


class ProviderError(RuntimeError):
    definitive_rejection = True


def validate_payload(cfg, payload):
    if any(k.startswith('_short_drama') for k in payload) or payload.get('short_drama_binding'):
        raise ValueError('短剧绑定任务不支持通用渠道映射，请使用专用渠道配置')
    if not str(payload.get('prompt') or '').strip():
        raise ValueError('提示词不能为空')
    if len(str(payload['prompt'])) > 7000:
        raise ValueError('提示词不得超过7000字')
    if int(payload.get('n') or payload.get('count') or 1) != 1:
        raise ValueError('受管理渠道当前每个任务只生成一个产物')
    if payload.get('operation') not in {None,'','generate'}:
        raise ValueError('受管理视频渠道暂不支持编辑操作')
    refs = payload.get('reference_images') or ([] if not payload.get('image') else [payload['image']])
    if not isinstance(refs, list):
        raise ValueError('参考图必须为数组')
    if cfg['adapter'] == 'openai_image' and (refs or payload.get('images') or payload.get('mode') in {'img2img','edit'}):
        raise ValueError('此适配器仅支持文生图，参考图编辑请使用已有专用渠道')
    if cfg['adapter'] == 'minimax_h3':
        from .video_minimax_h3 import build_request
        build_request(payload['prompt'], refs, payload.get('ratio') or '9:16', payload.get('duration') or 5, payload.get('resolution') or '2K')
    if cfg['adapter'] == 'xai_video' and (len(refs)>1 or payload.get('video') or payload.get('reference_videos')):
        raise ValueError('此 Grok 适配器支持文生视频或单参考图，暂不支持视频编辑及多图')
    if cfg['adapter'] == 'xai_video':
        if str(payload.get('resolution') or '720p').lower() != '720p':
            raise ValueError('此 Grok 适配器仅支持720p，不能将更高分辨率任务降级执行')
        if not 1<=int(payload.get('duration') or 5)<=15:
            raise ValueError('Grok 视频时长须为1～15秒')
        if payload.get('ratio','9:16') not in {'9:16','16:9','1:1'}:
            raise ValueError('此适配器支持9:16、16:9或1:1比例')
        if cfg['model']=='grok-imagine-video-1.5' and not refs:
            raise ValueError('Grok 1.5需要参考图')


def opener(cfg):
    proxy = cfg.get('proxy')
    return urllib.request.build_opener(urllib.request.ProxyHandler({'http':proxy,'https':proxy} if proxy else {}), trace.NoRedirect())


def request(cfg, method, path, body=None):
    req = urllib.request.Request(cfg['base_url']+'/'+path.lstrip('/'),
        data=json.dumps(body).encode() if body is not None else None, method=method,
        headers={'Authorization':'Bearer '+cfg['secret'],'Content-Type':'application/json'})
    try:
        with opener(cfg).open(req, timeout=cfg['timeout']) as response:
            raw = response.read(8*1024*1024+1)
            if len(raw)>8*1024*1024:
                raise ValueError('响应过大')
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        if method == 'POST' and (exc.code in {408,429} or exc.code>=500):
            raise OutcomeUnknown('提交结果未知，禁止自动重发') from None
        raise ProviderError('供应商 HTTP %s' % exc.code) from None
    except (OSError, ValueError) as exc:
        if method == 'POST':
            raise OutcomeUnknown('提交响应未确认，禁止自动重发') from None
        raise RuntimeError('网络或响应异常：'+type(exc).__name__) from None


def _download(cfg, url):
    p = urllib.parse.urlsplit(str(url))
    if p.scheme != 'https' or not p.hostname or p.username or p.password:
        raise ValueError('供应商产物地址不是有效 HTTPS 地址')
    for addr in socket.getaddrinfo(p.hostname, p.port or 443):
        if not ipaddress.ip_address(addr[4][0]).is_global:
            raise ValueError('产物地址指向非公网网络')
    with opener(cfg).open(urllib.request.Request(url), timeout=cfg['timeout']) as r:
        raw = r.read(100*1024*1024+1)
    if len(raw)>100*1024*1024:
        raise ValueError('产物超过100MB')
    return raw


def generate(cfg, payload, rid, job_id):
    validate_payload(cfg, payload)
    refs = payload.get('reference_images') or ([] if not payload.get('image') else [payload['image']])
    adapter = cfg['adapter']
    metadata = dict(provider=cfg['name'],model=cfg['model'],host=urllib.parse.urlsplit(cfg['base_url']).hostname,
                    transport='proxy' if cfg.get('proxy') else 'direct')
    if adapter == 'openai_image':
        body = {'model':cfg['model'],'prompt':payload['prompt'],'n':1,'response_format':'b64_json'}
        path = '/images/generations'
    elif adapter == 'minimax_h3':
        from .video_minimax_h3 import build_request
        body = build_request(payload['prompt'], refs, payload.get('ratio') or '9:16',payload.get('duration') or 5,payload.get('resolution') or '2K')
        path = '/v2/video_generation'
    else:
        body = {'model':cfg['model'],'prompt':payload['prompt'],'duration':int(payload.get('duration') or 5),'aspect_ratio':payload.get('ratio') or '9:16','resolution':'720p'}
        if refs:
            body['image'] = {'url':refs[0]}
        path = '/videos/generations'
    trace.record(job_id,'route','recorded',**metadata)
    store.finish(rid,'running','提交供应商')
    result = trace.call(job_id,'provider_submit',lambda: request(cfg,'POST',path,body),**metadata)
    provider_id = str(result.get('task_id') or result.get('request_id') or '')
    if adapter != 'openai_image':
        if not provider_id:
            raise OutcomeUnknown('供应商未返回工单号，禁止自动重发')
        store.finish(rid,'running','供应商已接单',provider_id)
        trace.record(job_id,'provider_accepted','recorded',provider_task_id=provider_id,**metadata)
        deadline = time.monotonic()+1800
        while time.monotonic()<deadline:
            time.sleep(5)
            path = ('/v2/query/video_generation/' if adapter=='minimax_h3' else '/videos/')+urllib.parse.quote(provider_id,safe='')
            store.finish(rid,'running','查询供应商生成状态',provider_id)
            try:
                result = trace.call(job_id,'provider_query',lambda:request(cfg,'GET',path),provider_task_id=provider_id,**metadata)
            except Exception:
                raise OutcomeUnknown('生成查询中断，供应商工单状态尚未确认') from None
            task = result.get('task',{}) if adapter=='minimax_h3' else result
            status = task.get('status')
            if status in {'failed','expired','error'}:
                raise ProviderError('供应商明确报告生成失败')
            if status in {'succeeded','done'}:
                break
        else:
            raise OutcomeUnknown('等待生成超时，供应商工单保留，请人工核查')
        url = (task.get('content') or {}).get('url') if adapter=='minimax_h3' else (task.get('video') or {}).get('url')
        store.finish(rid,'running','下载生成视频',provider_id)
        raw = trace.call(job_id,'download',lambda:_download(cfg,url),**metadata)
    else:
        data = result.get('data') or []
        if not data:
            raise RuntimeError('供应商返回空产物')
        raw = base64.b64decode(data[0]['b64_json'],validate=True) if data[0].get('b64_json') else _download(cfg,data[0].get('url'))
    from . import core
    media = 'image' if adapter=='openai_image' else 'video'
    filename = 'channel_'+uuid.uuid4().hex+('.png' if media=='image' else '.mp4')
    target = core.OUT_DIR / filename
    store.finish(rid,'running','核验成品',provider_id)
    try:
        if media=='image':
            from PIL import Image
            with Image.open(io.BytesIO(raw)) as img:
                img.load()
                img.save(target,'PNG')
        else:
            import subprocess
            target.write_bytes(raw)
            probe = subprocess.run(['ffprobe','-v','error','-count_frames','-show_entries','stream=codec_type,width,height,nb_read_frames','-of','json',str(target)],capture_output=True,timeout=120,check=True)
            streams=json.loads(probe.stdout).get('streams',[])
            if probe.stderr or not any(s.get('codec_type')=='video' and int(s.get('width') or 0)>0 and int(s.get('height') or 0)>0 and str(s.get('nb_read_frames') or '').isdigit() and int(s['nb_read_frames'])>0 for s in streams):
                raise ValueError('成品帧解码未通过，不能判定视频有效')
        trace.record(job_id,'artifact','passed',provider_task_id=provider_id,**metadata)
        url = core.public_url(filename,'image/png' if media=='image' else 'video/mp4')
        trace.record(job_id,'delivery','unknown',**metadata)
        return {'type':media,'file':filename,'url':url,'files':[filename],'urls':[url],'count':1,
                'mode':'text2img' if media=='image' else 'generate','provider':cfg['name'],'model':cfg['model'],
                'request_id':provider_id,'channel_id':cfg['id'],'channel_version':cfg['version']}
    except Exception:
        target.unlink(missing_ok=True)
        raise


def execute(rid, payload=None):
    with closing(store.db()) as c:
        row = dict(c.execute('SELECT * FROM runs WHERE id=?',(rid,)).fetchone())
    cfg = store.version(row['channel'],row['version'])
    start = time.monotonic()
    try:
        cfg = store.version(row['channel'],row['version'],True)
        while True:
            with closing(store.db()) as c:
                c.execute('BEGIN IMMEDIATE')
                # Stale runs remain unknown; never resubmit after restart.
                c.execute("UPDATE runs SET state='unknown',detail='执行进程中断或超时，需人工核查' WHERE state='running' AND updated<?",(time.time()-2400,))
                active = c.execute("SELECT COUNT(*) FROM runs WHERE channel=? AND state='running'",(cfg['id'],)).fetchone()[0]
                rate = c.execute("SELECT COUNT(*) FROM events WHERE target=? AND action='runtime.dispatch' AND created>?",(cfg['id'],time.time()-60)).fetchone()[0]
                own = c.execute('SELECT state FROM runs WHERE id=?',(rid,)).fetchone()[0]
                if own!='queued':
                    c.commit()
                    return None
                if active<cfg['concurrency'] and rate<cfg['rpm']:
                    c.execute("UPDATE runs SET state='running',updated=? WHERE id=?",(time.time(),rid))
                    store._audit(c,'runtime.dispatch',cfg['id'],'runtime')
                    c.commit()
                    break
                c.commit()
            if time.monotonic()-start>120:
                raise RuntimeError('渠道并发或限流等待超时，尚未提交供应商')
            time.sleep(1)
        if row['kind']=='connection':
            req = urllib.request.Request(cfg['base_url'],method='HEAD')
            try:
                with opener(cfg).open(req,timeout=cfg['timeout']):
                    pass
            except urllib.error.HTTPError as exc:
                if not 400<=exc.code<500:
                    raise
            result, detail = None,'网络连接可达；不代表鉴权或生成成功'
        elif row['kind']=='auth':
            if cfg['adapter']=='minimax_h3':
                # A nonexistent resource cannot prove valid credentials.
                raise CheckUnsupported('此协议未提供可靠的独立鉴权证明，请运行完整生成测试')
            result = request(cfg,'GET','/models')
            if cfg['model'] not in [m.get('id') for m in result.get('data',[])]:
                raise ValueError('模型列表未包含指定模型，不能判定可用')
            detail = '鉴权与模型列表通过；不代表生成成功'
        else:
            result = generate(cfg,payload if payload is not None else cfg['fixture'],rid,row['job_id'] or 'channel-test:'+rid)
            detail = '成品已下载并核验；尚无用户接收证明'
        store.finish(rid,'passed',detail)
        _notify(row,'passed')
        return result
    except Exception as exc:
        state = 'blocked' if isinstance(exc,CheckUnsupported) else 'unknown' if isinstance(exc,OutcomeUnknown) else 'failed'
        # Raw provider errors/payloads and secrets never enter public diagnostics.
        with closing(store.db()) as c:
            phase = c.execute('SELECT detail FROM runs WHERE id=?',(rid,)).fetchone()[0]
        detail = phase+'：'+str(exc)[:200] if isinstance(exc,(ValueError,OutcomeUnknown,ProviderError)) else phase+'：'+type(exc).__name__
        if cfg.get('secret'):
            detail = detail.replace(cfg['secret'],'[隐藏]')
        store.finish(rid,state,detail)
        _notify(row,state)
        raise RuntimeError(detail) from None


def _notify(row,state):
    if state not in {'passed','failed','unknown'}:
        return
    with closing(store.db()) as c:
        c.execute('BEGIN IMMEDIATE')
        previous = c.execute('SELECT * FROM channel_incidents WHERE channel=? AND kind=?',(row['channel'],row['kind'])).fetchone()
        if previous and previous['state']==state:
            action,occurred=previous['action'],previous['occurred']
        else:
            action=('channel.recovered' if previous else '') if state=='passed' else 'channel.'+state
            occurred=time.time()
            c.execute('INSERT OR REPLACE INTO channel_incidents VALUES(?,?,?,?,?)',(row['channel'],row['kind'],state,action,occurred))
        c.commit()
    # Re-enqueue the same durable event identity after an interrupted write; INSERT OR IGNORE deduplicates.
    if action:
        trace.enqueue(action,row['channel'],occurred)


def run_task(binding,payload,job_id):
    cfg = store.version(binding['id'],binding['version'])
    rid = store.reserve(cfg['id'],'task',str(job_id),cfg)
    return execute(rid,payload)


def start_test(actor,body):
    kind = body.get('kind')
    if kind not in {'connection','auth','full'}:
        raise ValueError('未知测试类型')
    cid = str(body.get('id') or '')
    if kind=='full':
        cfg = store.version(cid)
        validate_payload(cfg,cfg['fixture'])
    rid = store.reserve(cid,kind)
    with closing(store.db()) as c:
        store._audit(c,'test.'+kind,cid,actor)
        c.commit()
    def work():
        try:
            execute(rid)
        except Exception:
            pass
    threading.Thread(target=work,daemon=True).start()
    return {'run_id':rid,'state':'queued'}


def monitor_cycle():
    now = time.time()
    due = []
    with closing(store.db()) as c:
        incidents=[dict(r) for r in c.execute("SELECT channel,action,occurred FROM channel_incidents WHERE action!=''")]
    for incident in incidents:
        trace.enqueue(incident['action'],incident['channel'],incident['occurred'])
    with closing(store.db()) as c:
        c.execute('BEGIN IMMEDIATE')
        for row in c.execute('SELECT s.*,ch.version FROM schedule s JOIN channels ch ON ch.id=s.channel WHERE ch.enabled=1').fetchall():
            cfg = store.version(row['channel'],row['version'])
            for kind, column, enabled in [('connection','light_due',cfg['monitor']),('full','full_due',cfg['daily_test'])]:
                if enabled and row[column]<=now:
                    due.append((row['channel'],kind))
                    next_at = now+cfg['poll_seconds'] if kind=='connection' else store._next_daily(cfg['daily_hour'],now)
                    c.execute('UPDATE schedule SET '+column+'=? WHERE channel=?',(next_at,row['channel']))
        c.commit()
    for cid,kind in due:
        try:
            start_test('scheduler',{'id':cid,'kind':kind})
        except Exception as exc:
            trace.enqueue('channel.schedule_blocked',cid,int(now))
            with closing(store.db()) as c:
                store._audit(c,'scheduler.blocked.'+kind,cid,'scheduler')
                c.commit()
    # Resume only work that has never been submitted. Running/unknown calls are not replayed.
    with closing(store.db()) as c:
        queued = [r[0] for r in c.execute("SELECT id FROM runs WHERE state='queued' AND kind!='task' ORDER BY started LIMIT 8")]
    for rid in queued:
        def work(run_id=rid):
            try:
                execute(run_id)
            except Exception:
                pass
        threading.Thread(target=work,daemon=True).start()
