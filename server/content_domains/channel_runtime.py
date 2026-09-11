"""Bounded managed-channel execution and tests; never retry a generation POST."""
import base64
import io
import json
import time
import threading
import urllib.parse
import uuid
from contextlib import closing

from . import channel_manager as store, runtime_observability as trace, safe_http


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
    if cfg['adapter'] == 'openai_image':
        spec = cfg.get('parameters') or {}
        mask_present = bool(payload.get('mask'))
        if spec.get('reference_max', 0) >= 1:
            if len(refs) > spec['reference_max']:
                raise ValueError('参考图数量超出当前模型支持范围')
        elif refs or payload.get('images') or payload.get('mode') in {'img2img','edit'}:
            raise ValueError('此适配器仅支持文生图，参考图编辑请使用已有专用渠道')
        if mask_present:
            if not spec.get('mask'):
                raise ValueError('此渠道未启用局部修图（蒙版）')
            if len(refs) != 1:
                raise ValueError('局部修图需要恰好 1 张参考图')
    if cfg['adapter'] == 'minimax_h3':
        from .video_minimax_h3 import build_request
        build_request(payload['prompt'], refs, payload.get('ratio') or '9:16', payload.get('duration') or 5, payload.get('resolution') or '2K')
    if cfg['adapter'] == 'xai_video' and (len(refs)>1 or payload.get('video') or payload.get('reference_videos')):
        raise ValueError('此 Grok 适配器支持文生视频或单参考图，暂不支持视频编辑及多图')
    if cfg['adapter'] == 'xai_video':
        if str(payload.get('resolution') or '720p').lower() not in ({'720p'} | ({'1080p'} if cfg['model']=='grok-imagine-video-1.5' else set())):
            raise ValueError('此 Grok 模型不支持该分辨率，不能将更高分辨率任务降级执行')
        if not 1<=int(payload.get('duration') or 5)<=15:
            raise ValueError('Grok 视频时长须为1～15秒')
        if payload.get('ratio','9:16') not in {'9:16','16:9','1:1'}:
            raise ValueError('此适配器支持9:16、16:9或1:1比例')
        if cfg['model']=='grok-imagine-video-1.5' and not refs:
            raise ValueError('Grok 1.5需要参考图')
    if cfg['adapter'] == 'lechuang_image':
        if payload.get('video') or payload.get('reference_videos'):
            raise ValueError('乐创生图不支持视频参考')
        if len(refs) > 9:
            raise ValueError('乐创生图参考图最多 9 张')
        if payload.get('background') not in (None,'','auto','opaque','transparent'):
            raise ValueError('乐创生图背景仅支持 auto/opaque/transparent')
    if cfg['adapter'] == 'lechuang_video':
        ref_max = 1 if cfg['model'] == 'grok-video-1.5' else 7
        if payload.get('video') or payload.get('reference_videos'):
            raise ValueError('乐创视频适配器暂不支持视频参考')
        if len(refs) > ref_max:
            raise ValueError('该模型参考图最多 %d 张' % ref_max)
        if str(payload.get('resolution') or '720p').lower() not in {'480p','720p','1080p'}:
            raise ValueError('乐创视频仅支持 480p/720p/1080p')
        if not 1<=int(payload.get('duration') or 5)<=15:
            raise ValueError('Grok 视频时长须为1～15秒')
        if payload.get('ratio','9:16') not in {'9:16','16:9','1:1'}:
            raise ValueError('乐创视频支持9:16、16:9或1:1比例')


def request(cfg, method, path, body=None, extra_headers=None, files=None):
    headers = {'Authorization': 'Bearer ' + cfg['secret']}
    if extra_headers:
        headers.update(extra_headers)
    try:
        url = cfg['base_url']+'/'+path.lstrip('/')
        if files is not None:
            return safe_http.request_multipart_json(
                method, url, fields=body or {}, files=files,
                headers=headers,
                timeout=cfg['timeout'], proxy=cfg.get('proxy') or '',
            )
        return safe_http.request_json(
            method, url, body=body,
            headers=headers,
            timeout=cfg['timeout'], proxy=cfg.get('proxy') or '',
        )
    except safe_http.SafeHttpError as exc:
        if method == 'POST' and (exc.status in {0,408,429} or exc.status>=500):
            raise OutcomeUnknown('提交结果未知，禁止自动重发') from None
        if exc.status:
            raise ProviderError('供应商 HTTP %s' % exc.status) from None
        if method == 'POST':
            raise OutcomeUnknown('提交响应未确认，禁止自动重发') from None
        raise RuntimeError('网络或响应异常：'+type(exc).__name__) from None
    except (OSError, ValueError) as exc:
        if method == 'POST':
            raise OutcomeUnknown('提交响应未确认，禁止自动重发') from None
        raise RuntimeError('网络或响应异常：'+type(exc).__name__) from None


def _download(cfg, url, headers=None):
    return safe_http.request_bytes(
        'GET', url, timeout=cfg['timeout'], max_bytes=100*1024*1024,
        headers=headers,
        proxy=cfg.get('proxy') or '',
    )


_LECHUANG_VIDEO_SIZES = {
    '9:16': {'480p': '480x848', '720p': '720x1280', '1080p': '1080x1920'},
    '16:9': {'480p': '848x480', '720p': '1280x720', '1080p': '1920x1080'},
    '1:1': {'480p': '480x480', '720p': '720x720', '1080p': '1080x1080'},
}


def _lechuang_refs(refs):
    """乐创 ReferenceMediaRequest：HTTP(S) 传 url，其余按 data_url。"""
    return [
        {'type': 'url' if str(r or '').startswith(('http://', 'https://')) else 'data_url', 'value': str(r)}
        for r in refs
    ]


def _lechuang_image_resolution(size):
    """按乐创 gpt-image-2 规格由像素尺寸推导 1k/2k/4k。"""
    try:
        edge = max(int(x) for x in str(size or '').split('x'))
    except ValueError:
        return '1k'
    return '1k' if edge <= 1280 else ('2k' if edge <= 2304 else '4k')


def _decode_data_url(value, field):
    value = str(value or '').strip()
    if not value.startswith('data:') or ',' not in value:
        raise ValueError(field + ' 必须为 data URL')
    meta, b64 = value.split(',', 1)
    if ';base64' not in meta:
        raise ValueError(field + ' 必须为 base64 编码')
    cleaned = ''.join(b64.split())
    cleaned += '=' * ((4 - len(cleaned) % 4) % 4)
    try:
        raw = base64.b64decode(cleaned, validate=True)
    except Exception:
        raise ValueError(field + ' 必须是合法 base64') from None
    if not raw:
        raise ValueError(field + ' 内容为空')
    if len(raw) > 10 * 1024 * 1024:
        raise ValueError(field + ' 超过 10MB，请先压缩图片再上传')
    return raw


def _edits_parts(cfg, payload, refs, placeholder=False):
    spec = cfg.get('parameters') or {}
    fields = {'prompt': str(payload.get('prompt') or ''), 'size': str(payload.get('size') or '1024x1024'), 'n': '1'}
    for key, default in (('output_format', 'png'), ('background', 'opaque')):
        if spec and key in {f['key'] for f in spec.get('fields', [])}:
            fields[key] = str(payload.get(key) or default)
    files = []
    if placeholder:
        files.append(('image', 'reference.png', b''))
        if payload.get('mask'):
            files.append(('mask', 'mask.png', b''))
    else:
        files.append(('image', 'image.png', _decode_data_url(refs[0], '参考图')))
        if payload.get('mask'):
            files.append(('mask', 'mask.png', _decode_data_url(payload['mask'], '蒙版')))
    return fields, files


def build_generation_request(cfg, payload, preview=False):
    from .channel_parameters import image_request
    validate_payload(cfg,payload)
    refs = payload.get('reference_images') or ([] if not payload.get('image') else [payload['image']])
    adapter=cfg['adapter']
    if adapter == 'openai_image':
        if refs:
            body, files = _edits_parts(cfg, payload, refs, placeholder=preview)
            return '/images/edits', body, files
        return '/images/generations', image_request(cfg,payload), None
    if adapter == 'minimax_h3':
        from .video_minimax_h3 import build_request
        body = build_request(payload['prompt'], refs, payload.get('ratio') or '9:16',payload.get('duration') or 5,payload.get('resolution') or '2K')
        return '/v2/video_generation', body, None
    if adapter == 'lechuang_image':
        from .channel_parameters import SIZES
        size = str(payload.get('size') or '1024x1024')
        image_input = {
            'prompt': payload['prompt'],
            'mode': 'image_to_image' if refs else 'text_to_image',
            'n': 1,
            'resolution': _lechuang_image_resolution(size),
            'aspect_ratio': SIZES.get(size, '1:1'),
            'quality': str(payload.get('quality') or 'auto'),
            'background': str(payload.get('background') or 'auto'),
        }
        if refs:
            image_input['reference_images'] = _lechuang_refs(refs)
        return '/generations', {'model': cfg['model'], 'input': image_input}, None
    if adapter == 'lechuang_video':
        ratio = str(payload.get('ratio') or '9:16')
        resolution = str(payload.get('resolution') or '720p').lower()
        video_input = {
            'prompt': payload['prompt'],
            'mode': 'image_to_video' if refs else 'text_to_video',
            'size': _LECHUANG_VIDEO_SIZES.get(ratio, {}).get(resolution, '720x1280'),
            'resolution': resolution,
            'duration_seconds': int(payload.get('duration') or 5),
        }
        if refs:
            video_input['reference_images'] = _lechuang_refs(refs)
        return '/generations', {'model': cfg['model'], 'input': video_input}, None
    body = {'model':cfg['model'],'prompt':payload['prompt'],'duration':int(payload.get('duration') or 5),'aspect_ratio':payload.get('ratio') or '9:16','resolution':str(payload.get('resolution') or '720p')}
    if refs:
        body['image'] = {'url':refs[0]}
    return '/videos/generations', body, None


def generate(cfg, payload, rid, job_id):
    from .channel_parameters import apply, image_request
    payload,_=apply(cfg,payload,required=False)
    validate_payload(cfg, payload)
    refs = payload.get('reference_images') or ([] if not payload.get('image') else [payload['image']])
    adapter = cfg['adapter']
    is_lechuang = adapter in ('lechuang_image', 'lechuang_video')
    metadata = dict(provider=cfg['name'],model=cfg['model'],host=urllib.parse.urlsplit(cfg['base_url']).hostname,
                    transport='proxy' if cfg.get('proxy') else 'direct')
    path,body,files=build_generation_request(cfg,payload)
    trace.record(job_id,'route','recorded',**metadata)
    store.finish(rid,'running','提交供应商')
    # 乐创付费创建请求要求 8-128 字符幂等键；run id 为 32 位 hex，天然幂等。
    extra_headers = {'Idempotency-Key': str(rid)} if is_lechuang else None
    result = trace.call(job_id,'provider_submit',lambda: request(cfg,'POST',path,body,extra_headers=extra_headers,files=files),**metadata)
    provider_id = str(result.get('task_id') or result.get('request_id') or '')
    if is_lechuang:
        envelope = (result or {}).get('data') if isinstance(result, dict) else {}
        provider_id = str(envelope.get('request_id') or '')
        if not provider_id:
            err = envelope.get('error') or {}
            if err.get('message'):
                raise ProviderError('乐创拒绝提交：' + str(err['message'])[:180])
            raise OutcomeUnknown('供应商未返回工单号，禁止自动重发')
        store.finish(rid,'running','乐创已接单',provider_id)
        trace.record(job_id,'provider_accepted','recorded',provider_task_id=provider_id,**metadata)
        deadline = time.monotonic()+1800
        while time.monotonic()<deadline:
            time.sleep(5)
            store.finish(rid,'running','查询乐创生成状态',provider_id)
            try:
                polled = trace.call(job_id,'provider_query',
                                    lambda:request(cfg,'GET','/generations/'+urllib.parse.quote(provider_id,safe='')),
                                    provider_task_id=provider_id,**metadata)
            except Exception:
                raise OutcomeUnknown('生成查询中断，供应商工单状态尚未确认') from None
            envelope = (polled or {}).get('data') if isinstance(polled, dict) else {}
            status = str(envelope.get('status') or '')
            if status in {'failed','error','expired','cancelled'}:
                err = envelope.get('error') or {}
                raise ProviderError('乐创明确报告生成失败：' + str(err.get('message') or status)[:160])
            if status == 'succeeded':
                break
        else:
            raise OutcomeUnknown('等待生成超时，供应商工单保留，请人工核查')
        out = envelope.get('output') or {}
        if adapter == 'lechuang_image':
            images = out.get('images') or []
            if not images or not images[0].get('url'):
                raise RuntimeError('乐创返回空图片产物')
            store.finish(rid,'running','下载生成图片',provider_id)
            raw = trace.call(job_id,'download',lambda:_download(cfg,images[0]['url']),**metadata)
        else:
            videos = out.get('videos') or []
            if not videos:
                raise RuntimeError('乐创返回空视频产物')
            content = videos[0].get('content_url') or videos[0].get('url') or ''
            if not content:
                raise RuntimeError('乐创视频缺少下载地址')
            store.finish(rid,'running','下载生成视频',provider_id)
            if str(content).startswith(('http://', 'https://')):
                raw = trace.call(job_id,'download',lambda:_download(cfg,str(content)),**metadata)
            else:
                auth = {'Authorization': 'Bearer ' + cfg['secret']}
                if str(content).startswith('/'):
                    # 平台返回根相对路径（自带 /api/v1 前缀），须接在源站 origin 上。
                    parts = urllib.parse.urlsplit(cfg['base_url'])
                    url = parts.scheme + '://' + parts.netloc + str(content)
                else:
                    url = cfg['base_url'].rstrip('/') + '/' + str(content).lstrip('/')
                raw = trace.call(job_id,'download',lambda:_download(cfg,url,headers=auth),**metadata)
    elif adapter != 'openai_image':
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
    media = 'image' if adapter in ('openai_image','lechuang_image') else 'video'
    output_format=payload.get('output_format','png') if cfg.get('parameters') else 'png'
    filename = 'channel_'+uuid.uuid4().hex+('.'+output_format if media=='image' else '.mp4')
    target = core.OUT_DIR / filename
    store.finish(rid,'running','核验成品',provider_id)
    try:
        if media=='image':
            from PIL import Image
            with Image.open(io.BytesIO(raw)) as img:
                img.load()
                if cfg.get('parameters') and payload.get('size') and tuple(map(int,payload['size'].split('x')))!=img.size:
                    raise ValueError('供应商返回图片尺寸与所选尺寸不一致，不能判定交付通过')
                if output_format=='jpeg':img=img.convert('RGB')
                img.save(target,{'png':'PNG','jpeg':'JPEG','webp':'WEBP'}[output_format])
        else:
            import subprocess
            target.write_bytes(raw)
            probe = subprocess.run(['ffprobe','-v','error','-count_frames','-show_entries','stream=codec_type,width,height,nb_read_frames','-of','json',str(target)],capture_output=True,timeout=120,check=True)
            streams=json.loads(probe.stdout).get('streams',[])
            if probe.stderr or not any(s.get('codec_type')=='video' and int(s.get('width') or 0)>0 and int(s.get('height') or 0)>0 and str(s.get('nb_read_frames') or '').isdigit() and int(s['nb_read_frames'])>0 for s in streams):
                raise ValueError('成品帧解码未通过，不能判定视频有效')
        trace.record(job_id,'artifact','passed',provider_task_id=provider_id,**metadata)
        url = core.public_url(filename,'image/'+output_format if media=='image' else 'video/mp4')
        trace.record(job_id,'delivery','unknown',**metadata)
        return {'type':media,'file':filename,'url':url,'files':[filename],'urls':[url],'count':1,
                'mode':('text2img' if not refs else 'img2img') if media=='image' else 'generate','provider':cfg['name'],'model':cfg['model'],
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
            try:
                safe_http.request_bytes(
                    'HEAD', cfg['base_url'], timeout=cfg['timeout'],
                    max_bytes=1024, proxy=cfg.get('proxy') or '',
                )
            except safe_http.SafeHttpError as exc:
                if not 400 <= exc.status < 500:
                    raise
            result, detail = None,'网络连接可达；不代表鉴权或生成成功'
        elif row['kind']=='auth':
            if cfg['adapter']=='minimax_h3':
                # A nonexistent resource cannot prove valid credentials.
                raise CheckUnsupported('此协议未提供可靠的独立鉴权证明，请运行完整生成测试')
            result = request(cfg,'GET','/models')
            data = result.get('data')
            if isinstance(data, dict):
                # 乐创统一协议：{code,message,data:{list:[{id,...}]}}
                models = [m.get('id') for m in (data.get('list') or []) if isinstance(m, dict)]
            elif isinstance(data, list):
                models = [m.get('id') for m in data if isinstance(m, dict)]
            else:
                models = []
            if not models or cfg['model'] not in models:
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
