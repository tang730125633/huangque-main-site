"""Published parameter contracts, private drafts and immutable per-job pricing.

Uses existing channel versions/settings. Public metadata never contains credentials,
upstream model names, URLs, proxies, drafts or operator identities.
"""
import json
import time
from contextlib import closing
from . import channel_manager as store

LABELS={'size':'图片尺寸','quality':'生成质量','output_format':'文件格式','background':'背景',
        'ratio':'画面比例','resolution':'清晰度','duration':'时长（秒）'}
SIZES={'1024x1024':'1:1','1536x1024':'3:2','1024x1536':'2:3',
       '1792x1024':'7:4','1024x1792':'4:7','256x256':'1:1','512x512':'1:1',
       '2048x2048':'1:1','2048x1152':'16:9','1152x2048':'9:16','3840x2160':'16:9','2160x3840':'9:16',
       '2048x1536':'4:3','1536x2048':'3:4',
       '1024x1280':'4:5','1280x1024':'5:4','1024x768':'4:3','768x1024':'3:4',
       '1280x720':'16:9','720x1280':'9:16'}


def capabilities(cfg,profile=None):
    adapter=cfg['adapter']
    if adapter=='openai_image':
        default='gpt_image' if cfg['model'].startswith('gpt-image') else 'dalle3' if cfg['model']=='dall-e-3' else 'compatible'
        if cfg['model']=='gpt-image-2' or cfg['model'].startswith('gpt-image-2-'):default='gpt_image_2'
        profile=profile or default
        if default=='gpt_image_2' and profile!='gpt_image_2':raise ValueError('GPT Image 2 须选择对应参数协议')
        if cfg['model'].startswith('gpt-image-1') and profile!='gpt_image':raise ValueError('GPT Image 1 系列须选择对应参数协议')
        if profile=='gpt_image_2':
            if cfg['model'].startswith('gpt-image-1'):raise ValueError('GPT Image 1 系列不能使用 GPT Image 2 的扩展尺寸')
            fields={'size':['1024x1024','1536x1024','1024x1536','2048x2048','2048x1152','1152x2048','3840x2160','2160x3840','2048x1536','1536x2048'],
                    'quality':['low','medium','high'],'output_format':['png','jpeg','webp']}
        elif profile=='gpt_image':
            fields={'size':['1024x1024','1536x1024','1024x1536'],'quality':['low','medium','high'],
                    'output_format':['png','jpeg','webp'],'background':['opaque','transparent']}
        elif profile=='dalle3':
            fields={'size':['1024x1024','1792x1024','1024x1792'],'quality':['standard','hd'],'output_format':['png']}
        elif profile=='compatible':
            fields={'size':['1024x1024'],'quality':['standard'],'output_format':['png']}
        else:raise ValueError('未知图片参数协议')
        if cfg['model']=='dall-e-3' and profile!='dalle3':raise ValueError('DALL-E 3 须选择对应参数协议')
        return dict(profile=profile,profiles=['gpt_image','gpt_image_2','dalle3','compatible'],fields=fields,reference_min=0,reference_max=0,count=1)
    if adapter=='minimax_h3':
        from .video_minimax_h3 import MODEL
        if cfg['model']!=MODEL:raise ValueError('此协议实际模型固定为 '+MODEL+'，请先修正连接配置')
        return dict(profile='minimax_h3',profiles=['minimax_h3'],fields={'ratio':['9:16','16:9','1:1','4:3','3:4','21:9'],
                    'resolution':['2K'],'duration':list(range(4,16))},reference_min=0,reference_max=5,count=1)
    if adapter=='xai_video':
        return dict(profile='xai_video',profiles=['xai_video'],fields={'ratio':['9:16','16:9','1:1'],
                    'resolution':['720p'],'duration':list(range(1,16))},
                    reference_min=1 if cfg['model']=='grok-imagine-video-1.5' else 0,reference_max=1,count=1)
    if adapter=='lechuang_image':
        # 乐创统一生图（gpt-image-2 等）：文生图 + 图生图修图（1..9 参考图），部分线路支持透明底。
        return dict(profile='lechuang_image',profiles=['lechuang_image'],
                    fields={'size':['1024x1024','1024x1280','1280x1024','1024x768','768x1024',
                                    '1280x720','720x1280','1024x1536','1536x1024'],
                            'quality':['auto','low','medium','high'],
                            'background':['auto','opaque','transparent']},
                    reference_min=0,reference_max=9,count=1)
    if adapter=='lechuang_video':
        # 乐创统一视频（Grok 1.0 / Grok 1.5）：文生视频 + 图生视频。
        # Grok 1.0 图生视频最多 7 张参考图；Grok 1.5 单图视频模型最多 1 张。
        ref_max=1 if cfg['model']=='grok-video-1.5' else 7
        return dict(profile='lechuang_video',profiles=['lechuang_video'],
                    fields={'ratio':['9:16','16:9','1:1'],
                            'resolution':['480p','720p','1080p'],
                            'duration':list(range(1,16))},
                    reference_min=0,reference_max=ref_max,count=1)
    raise ValueError('该适配器尚未接入参数配置')


def validate(cfg,spec):
    if not isinstance(spec,dict):raise ValueError('参数配置须为对象')
    cap=capabilities(cfg,spec.get('profile'))
    if spec.get('profile')!=cap['profile']:raise ValueError('参数协议不匹配')
    fields=spec.get('fields')
    if not isinstance(fields,list) or {f.get('key') for f in fields if isinstance(f,dict)}!=set(cap['fields']) or len(fields)!=len(cap['fields']):
        raise ValueError('参数字段与协议不匹配')
    clean_fields=[]
    for f in fields:
        label=str(f.get('label') or LABELS[f['key']]).strip()
        if not 1<=len(label)<=40:raise ValueError('参数展示名称须为1～40字')
        clean_fields.append(dict(key=f['key'],label=label,visible=f.get('visible') is not False))
    combinations=spec.get('combinations')
    if not isinstance(combinations,list) or not 1<=len(combinations)<=128:raise ValueError('请设置1～128个合法参数组合')
    seen=set();ids=set();clean=[]
    for item in combinations:
        if not isinstance(item,dict):raise ValueError('参数组合须为对象')
        cid=str(item.get('id') or '')
        values=item.get('values')
        if not cid or len(cid)>64 or cid in ids:raise ValueError('参数组合编号缺失或重复')
        if not isinstance(values,dict) or set(values)!=set(cap['fields']):raise ValueError('组合参数不完整')
        for key,value in values.items():
            if isinstance(value,bool) or value not in cap['fields'][key]:raise ValueError('不支持的参数：'+key)
        if values.get('background')=='transparent' and values.get('output_format')=='jpeg':raise ValueError('透明背景不能使用 JPEG')
        key=json.dumps(values,sort_keys=True)
        if key in seen:raise ValueError('存在重复参数组合')
        seen.add(key);ids.add(cid)
        points=item.get('points')
        if isinstance(points,bool) or not isinstance(points,int) or not 1<=points<=100000:raise ValueError('每个组合的总点数须为1～100000整数')
        clean.append(dict(id=cid,values=dict(values),points=points))
    for f in clean_fields:
        if not f['visible'] and len({str(c['values'][f['key']]) for c in clean})!=1:
            raise ValueError('隐藏参数必须固定为一个值：'+f['label'])
    if spec.get('default') not in ids:raise ValueError('默认组合必须存在')
    low=spec.get('reference_min',cap['reference_min']);high=spec.get('reference_max',cap['reference_max'])
    if isinstance(low,bool) or isinstance(high,bool) or not isinstance(low,int) or not isinstance(high,int) or not cap['reference_min']<=low<=high<=cap['reference_max']:
        raise ValueError('参考图数量超出适配器范围')
    return dict(profile=cap['profile'],fields=clean_fields,combinations=clean,default=spec['default'],reference_min=low,reference_max=high)


def drafts(c):
    row=c.execute('SELECT value FROM settings WHERE id=3').fetchone()
    return json.loads(row[0]) if row else {}


def admin_state(cid,profile=None):
    cfg=store.version(cid)
    with closing(store.db()) as c:
        draft=drafts(c).get(cid)
        history=[]
        for r in c.execute('SELECT version,config,actor,created FROM versions WHERE channel=? ORDER BY version DESC',(cid,)):
            spec=json.loads(r['config']).get('parameters')
            if spec:history.append(dict(version=r['version'],actor=r['actor'],created=r['created'],parameters=spec))
    selected_profile=profile or (draft or {}).get('parameters',{}).get('profile') or cfg.get('parameters',{}).get('profile')
    return dict(id=cid,version=cfg['version'],capabilities=capabilities(cfg,selected_profile),labels=LABELS,sizes=SIZES,
                published=cfg.get('parameters'),draft=draft,history=history[:30])


def change(actor,body):
    cid=str(body.get('id') or '');action=body.get('action')
    if action not in {'draft','publish','rollback'}:raise ValueError('未知参数操作')
    with closing(store.db()) as c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute('SELECT ch.version,v.config,v.secret FROM channels ch JOIN versions v ON v.channel=ch.id AND v.version=ch.version WHERE ch.id=?',(cid,)).fetchone()
        if not row or row['version']!=body.get('version'):raise ValueError('渠道版本已变化，请刷新后重试')
        cfg=json.loads(row['config'])
        if cfg.get('_lifecycle',{}).get('deleted'):raise ValueError('请先恢复回收站渠道')
        all_drafts=drafts(c);old=all_drafts.get(cid,{})
        if body.get('draft_revision',0)!=old.get('revision',0):raise ValueError('草稿已被其他管理员修改，请刷新')
        if action=='rollback':
            previous=c.execute('SELECT config FROM versions WHERE channel=? AND version=?',(cid,body.get('target_version'))).fetchone()
            if not previous:raise ValueError('历史版本不存在')
            spec=json.loads(previous[0]).get('parameters')
        elif action=='publish':
            if old.get('base_version')!=row['version']:raise ValueError('请重新保存草稿后发布')
            spec=old.get('parameters')
        else:spec=body.get('parameters')
        spec=validate(cfg,spec)
        if action=='draft':
            all_drafts[cid]=dict(revision=old.get('revision',0)+1,base_version=row['version'],parameters=spec,actor=actor,updated=time.time())
        else:
            if body.get('confirmed') is not True:raise ValueError('请先确认前台参数、点数和影响范围')
            cfg['parameters']=spec
            version=row['version']+1
            c.execute('INSERT INTO versions VALUES(?,?,?,?,?,?)',(cid,version,json.dumps(cfg),row['secret'],actor,time.time()))
            c.execute('UPDATE channels SET version=? WHERE id=?',(version,cid))
            all_drafts[cid]=dict(revision=old.get('revision',0)+1,base_version=version,parameters=spec,actor=actor,updated=time.time())
        c.execute('INSERT OR REPLACE INTO settings VALUES(3,?)',(json.dumps(all_drafts),))
        store._audit(c,'parameters.'+action,cid,actor);c.commit()
    return admin_state(cid)


def token(cfg):
    return cfg['id']+':'+str(cfg['version'])


def historical(payload):
    selection=payload.get('parameter_selection') or {}
    if not isinstance(selection,dict):raise ValueError('参数选择格式无效')
    revision=str(selection.get('revision') or '')
    try:
        cid,version=revision.rsplit(':',1)
        if len(cid)>64:raise ValueError()
        cfg=store.version(cid,int(version))
        apply(cfg,payload)
        return cfg
    except (ValueError,TypeError,KeyError):raise ValueError('参数版本无效，请刷新页面') from None


def public_catalog():
    result=[]
    with closing(store.db()) as c:
        for row in c.execute('SELECT config FROM mappings'):
            m=json.loads(row[0])
            if not m.get('enabled'):continue
            ch=c.execute('SELECT version,enabled FROM channels WHERE id=?',(m['channel'],)).fetchone()
            if not ch or not ch['enabled']:continue
            cfg=json.loads(c.execute('SELECT config FROM versions WHERE channel=? AND version=?',(m['channel'],ch['version'])).fetchone()[0])
            spec=cfg.get('parameters')
            if not spec or cfg.get('_lifecycle',{}).get('deleted'):continue
            cfg.update(id=m['channel'],version=ch['version'])
            result.append(dict(kind=m['kind'],front=m['front'],label=m['label'],revision=token(cfg),
                fields=spec['fields'],combinations=spec['combinations'],default=spec['default'],
                reference_min=spec['reference_min'],reference_max=spec['reference_max'],count=1))
    return {'items':result,'refresh_seconds':15}


def apply(cfg,payload,required=True):
    spec=cfg.get('parameters')
    if not spec:
        if payload.get('parameter_selection'):raise ValueError('参数配置已撤回或渠道已切换，请刷新页面')
        return dict(payload),None
    selection=payload.get('parameter_selection') or {}
    if not isinstance(selection,dict):raise ValueError('参数选择格式无效')
    if required and selection.get('revision')!=token(cfg):raise ValueError('参数或点数已更新，请刷新并重新确认')
    selected=selection.get('combination') or (None if required else spec['default'])
    combo=next((r for r in spec['combinations'] if r['id']==selected),None)
    if combo is None:raise ValueError('该参数组合不可用，请重新选择')
    refs=payload.get('reference_images') or ([] if not payload.get('image') else [payload['image']])
    if not isinstance(refs,list) or not spec['reference_min']<=len(refs)<=spec['reference_max']:raise ValueError('参考图数量不符合当前模型要求')
    if int(payload.get('count') or payload.get('n') or 1)!=1:raise ValueError('当前适配器每任务仅支持一个产物')
    clean=dict(payload);clean.update(combo['values'])
    if 'size' in combo['values']:clean['ratio']=SIZES[combo['values']['size']]
    clean['count']=1
    return clean,combo['points']


def quote(kind,payload,allow_historical=False):
    if kind not in {'image','xiaole_video'}:return None
    if allow_historical and payload.get('parameter_selection'):
        return apply(historical(payload),payload)[1]
    front=str(payload.get('channel') if kind=='xiaole_video' else payload.get('model') or '')
    with closing(store.db()) as c:
        r=c.execute('SELECT config FROM mappings WHERE selector=?',(kind+':'+front,)).fetchone()
        mapping=json.loads(r[0]) if r else {}
        if not mapping.get('enabled'):
            if payload.get('parameter_selection'):raise ValueError('功能映射已变化，请刷新后重试')
            return None
        cfg=store.version(mapping['channel'])
        return apply(cfg,payload)[1]


def image_request(cfg,payload):
    body=dict(model=cfg['model'],prompt=payload['prompt'],n=1)
    spec=cfg.get('parameters')
    if spec:
        body.update({k:payload[k] for k in ('size','quality')})
        if spec['profile'] in {'gpt_image','gpt_image_2'}:body.update({k:payload[k] for k in ('output_format','background') if k in {f['key'] for f in spec['fields']}})
        else:body['response_format']='b64_json'
    elif cfg['model'].startswith('gpt-image'):
        pass  # GPT Image returns base64; response_format is not supported.
    else:body['response_format']='b64_json'
    return body


def preview(actor,body):
    cfg=store.version(str(body.get('id') or ''))
    if cfg['version']!=body.get('version'):raise ValueError('渠道版本已变化，请刷新后重试')
    spec=validate(cfg,body.get('parameters'));cfg['parameters']=spec
    payload=dict(prompt='示例提示词（仅预览，不发起生成）',parameter_selection={'combination':body.get('combination') or spec['default']},
                 reference_images=['https://example.invalid/reference.png']*spec['reference_min'])
    payload,points=apply(cfg,payload,required=False)
    from .channel_runtime import build_generation_request
    path,request=build_generation_request(cfg,payload)
    return dict(path=path,body=request,points=points,validation='参数校验通过；尚未调用供应商，不代表成品验证通过')
