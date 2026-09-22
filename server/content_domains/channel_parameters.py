"""Published parameter contracts, private drafts and immutable per-job pricing.

Uses existing channel versions/settings. Public metadata never contains credentials,
upstream model names, URLs, proxies, drafts or operator identities.

存储层：设置行（settings id=3/4）、渠道版本与映射统一经 ``channel_manager`` /
``channel_store`` 分发；``HQ_CHANNEL_STORE=postgres`` 时走 ``channel_store``
（routing schema），SQLite 路径与行为逐字节不变。本模块不再直连
``channel_manager.db()``。
"""
import json
import time
from contextlib import closing
from . import channel_manager as store
from . import channel_store
from . import channel_lifecycle, feature_flags

LABELS={'size':'图片尺寸','quality':'生成质量','output_format':'文件格式','background':'背景',
        'ratio':'画面比例','resolution':'清晰度','duration':'时长（秒）'}
SIZES={'1024x1024':'1:1','1536x1024':'3:2','1024x1536':'2:3',
       '1792x1024':'7:4','1024x1792':'4:7','256x256':'1:1','512x512':'1:1',
       '2048x2048':'1:1','2048x1152':'16:9','1152x2048':'9:16','3840x2160':'16:9','2160x3840':'9:16',
       '2048x1536':'4:3','1536x2048':'3:4',
       '1024x1280':'4:5','1280x1024':'5:4','1024x768':'4:3','768x1024':'3:4',
       '1280x720':'16:9','720x1280':'9:16'}

# 前台工作台入口目录：后台布局和用户页都从这里取名称与入口编号，避免两边各维护一份。
WORKBENCH_ENTRY_CATALOG = {
    'video': [
        {'key':'grok','label':'果肉视频生成'}, {'key':'talking','label':'数字化 IP'},
        {'key':'cinematic','label':'电影化身'}, {'key':'tryon','label':'换装换背景'},
        {'key':'minimax','label':'麦克视频'}, {'key':'micro','label':'Seedance 视频'},
        {'key':'sora','label':'Sora 2'}, {'key':'omni','label':'Omni 视频'},
    ],
    'image': [
        {'key':'gpt','label':'黄雀引擎 2','provider':'openai'},
        {'key':'banana','label':'纳米香蕉','provider':'gemini'},
        {'key':'seedream','label':'黄雀引擎 1','provider':'seedance'},
        {'key':'lechuang','label':'乐创 · 生图','managed_group':'lechuang'},
        {'key':'xiaole','label':'果肉生图','feature':'image_xiaole'},
        {'key':'zelong2','label':'泽龙2生图','host':'zelong.huangquechuanmei.com'},
    ],
}
WORKBENCH_LAYOUT_KEYS = {page:[item['key'] for item in items] for page,items in WORKBENCH_ENTRY_CATALOG.items()}
DEFAULT_WORKBENCH_LAYOUT = {
    'video': {'order': list(WORKBENCH_LAYOUT_KEYS['video']), 'default': 'grok'},
    'image': {'order': list(WORKBENCH_LAYOUT_KEYS['image']), 'default': 'gpt'},
}


def _clean_layout(raw, strict=False):
    raw = raw if isinstance(raw, dict) else {}
    result = {}
    for page, keys in WORKBENCH_LAYOUT_KEYS.items():
        section = raw.get(page)
        section = section if isinstance(section, dict) else {}
        order = [str(x) for x in (section.get('order') or []) if isinstance(x, str)]
        if strict:
            if set(order) != set(keys):
                raise ValueError(page + ' 布局顺序必须包含全部渠道且不能重复')
        else:
            order = [k for k in order if k in keys]
            order += [k for k in keys if k not in order]
        default = str(section.get('default') or '') if isinstance(section.get('default'), str) else ''
        if default not in keys:
            default = order[0] if order else ''
        result[page] = {'order': order, 'default': default}
    return result


def layout_state():
    if channel_store.enabled():
        return _clean_layout(channel_store.layout_setting())
    with closing(store.db()) as c:
        row = c.execute('SELECT value FROM settings WHERE id=4').fetchone()
    return _clean_layout(json.loads(row[0]) if row else {})


def _entry_front(task, oid):
    """目录条目对外的选择键。

    优先取识别条件里用户真正会提交的那个字段；都没有就退回功能 ID 的末段
    —— 目录身份不能建立在「这些字段必然存在」之上。
    """
    for key in ('channel', 'model', 'variant', 'voice_scope'):
        value = str((task or {}).get(key) or '').strip()
        if value:
            return value
    tail = str(oid or '').rsplit('.', 1)[-1]
    return tail


def _operation_for_front(kind, front):
    """按 front 反查业务功能：直接用识别条件比对，不靠命名猜测。

    同一个 front 可能对应多个功能（例如纳米香蕉 2 的文生图与参考图都提交
    model=nb2），所以这个函数只用于「旧映射条目该归属哪个功能」；
    功能之间靠 operation_id 区分，不在这里合并。
    """
    from .function_registry import operation_catalog
    hits = []
    for spec in operation_catalog():
        task = spec.get('task_match') or {}
        if str(task.get('kind') or '').strip() != kind:
            continue
        for key in ('channel', 'model', 'variant', 'voice_scope'):
            if str(task.get(key) or '').strip() == front:
                hits.append(spec)
                break
    if not hits:
        return None
    # 同一个 front 可能命中多个功能（grok 文生图 / grok 参考图；纳米香蕉 2 的
    # 文生图 / 参考图都提交 model=nb2）。这时【不能猜】——猜哪个都可能把用户的
    # 请求送到错的渠道。返回 None 让调用方把它当明确的兼容入口保留，
    # 由客户端按 operation_id + 真实输入解析（见 function_registry.classify_task）。
    if len(hits) > 1:
        return None
    return hits[0]


def _route_for_function(oid):
    """功能当前该走谁。返回 None 表示这个功能不进目录（只有暂停才这样）。

    统一解析：
      * 没有功能映射  → 'unmapped'：该功能尚未迁移，旧入口按明确的兼容关系
        继续可用，旧映射条目原样保留（不能因为「还没迁」就把入口删掉）；
      * state=legacy → 按原厂规则执行，条目原样保留；
      * state=managed→ 用主渠道当前版本，旧条目也改由它决定；
      * state=paused → 明确拒绝新任务，不进目录。
    """
    mapping = store.operation_mapping(oid)
    if not mapping:
        return {'state': 'unmapped', 'cfg': None, 'primary': ''}
    state = str(mapping.get('state') or '')
    if state == 'paused':
        return None
    cid = str(mapping.get('channel') or '')
    if state != 'managed' or not cid:
        return {'state': state or 'legacy', 'cfg': None, 'primary': ''}
    try:
        cfg = store.version(cid)
    except ValueError:
        # 映射指向的渠道已经不存在（被删或版本被回收）。这是**数据**情况，
        # 不是读取失败：该功能当前没有可用渠道，不进目录，由后台如实显示。
        # 其他异常（读库失败等）不在这里吞掉，继续向上抛。
        return None
    return {'state': 'managed', 'cfg': cfg, 'primary': cid}


def _operation_items(existing):
    """按业务功能补齐目录，并把旧映射条目重新归一到功能当前的主渠道。

    两件事，缺一不可：
      1. 旧映射条目（grok / gpt-image-2 …）只是「怎么找到这个功能」的入口标识，
         它该走谁必须由该功能当前的功能映射决定 —— 否则管理员把
         video.grok.text 切到 B，旧 grok 条目仍会把任务拉到 A。
      2. 只配了功能映射、没有旧条目的功能（纳米香蕉 2、黄雀引擎 1 …）
         必须补进来，且按 operation_id 各自成条 —— 文生图与参考图可以用不同渠道。

    读取失败一律抛出：目录读不到就让调用方报错，绝不悄悄退回旧配置。
    """
    from .function_registry import operation_catalog
    by_op = {}
    for spec in operation_catalog():
        oid = str(spec.get('operation_id') or '')
        task = spec.get('task_match') or {}
        kind = str(task.get('kind') or '').strip()
        if oid and kind:
            by_op[oid] = (spec, kind, task)

    def build(oid, spec, kind, task, cfg, front):
        params_spec = (cfg or {}).get('parameters')
        if not params_spec:
            return None
        return dict(kind=kind, front=front,
                    label=(cfg or {}).get('name') or spec.get('title') or oid,
                    revision=token(cfg), fields=params_spec['fields'],
                    combinations=params_spec['combinations'], default=params_spec['default'],
                    reference_min=params_spec['reference_min'],
                    reference_max=params_spec['reference_max'],
                    count=1, mask_enabled=params_spec.get('mask') is True,
                    operation_id=oid, match=task)

    # ① 旧映射条目：保留它作为入口标识，但渠道改由功能映射决定
    covered = set()
    rewritten = []
    for item in existing:
        spec = _operation_for_front(item['kind'], item['front'])
        if not spec:
            # 对应不到唯一业务功能（没有对应功能，或一个 front 对应多个功能）。
            # 保留为【明确的兼容入口】：它只表示「这条旧线路还能用」，
            # 不带 operation_id，不与功能条目混淆。管理员对某个功能的切换
            # 由该功能自己的条目生效，不会被这条兼容条目遮蔽。
            compat = dict(item)
            compat['legacy_compat'] = True
            rewritten.append(compat)
            continue
        oid = spec['operation_id']
        route = _route_for_function(oid)
        if route is None:
            continue                        # 功能已暂停：不进目录
        covered.add(oid)
        if route['state'] == 'managed' and route['cfg'] is not None:
            entry = build(oid, spec, item['kind'], spec.get('task_match') or {},
                          route['cfg'], item['front'])
            if entry:
                rewritten.append(entry)
                continue
        rewritten.append(item)

    # ② 只配了功能映射、没有旧条目的功能：按 operation_id 各自成条
    added = []
    for oid, (spec, kind, task) in by_op.items():
        if oid in covered:
            continue
        route = _route_for_function(oid)
        if route is None or route['state'] != 'managed' or route['cfg'] is None:
            continue
        entry = build(oid, spec, kind, task, route['cfg'], _entry_front(task, oid))
        if entry:
            added.append(entry)
    return rewritten + added


def _published_items():
    if channel_store.enabled():
        result=[]
        for m,version,cfg in channel_store.published_channel_configs():
            spec=cfg.get('parameters')
            if not spec or cfg.get('_lifecycle',{}).get('deleted'):continue
            cfg.update(id=m['channel'],version=version)
            result.append(dict(kind=m['kind'],front=m['front'],label=m['label'],revision=token(cfg),
                fields=spec['fields'],combinations=spec['combinations'],default=spec['default'],
                reference_min=spec['reference_min'],reference_max=spec['reference_max'],count=1,
                mask_enabled=spec.get('mask') is True))
        return _operation_items(result)
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
                reference_min=spec['reference_min'],reference_max=spec['reference_max'],count=1,
                mask_enabled=spec.get('mask') is True))
    return _operation_items(result)


def _lechuang_items(items):
    known={'gpt-image-2','gpt-image-2.5-flare'}
    return [item for item in items if item.get('kind')=='image' and
            (item.get('front') in known or '乐创' in str(item.get('label') or ''))]


def workbench_entries(published_items=None, hostname='huangquechuanmei.com'):
    published_items = _published_items() if published_items is None else published_items
    legacy = channel_lifecycle.legacy_states()
    hostname = str(hostname or '').strip().lower().split(':',1)[0]
    result={}
    for page,catalog in WORKBENCH_ENTRY_CATALOG.items():
        entries=[]
        for source in catalog:
            item=dict(source);visible=True;defaultable=True;status='visible';reason='主站显示';models=[];model_keys=[]
            provider=item.get('provider')
            if provider and legacy.get(provider,{}).get('enabled') is False:
                defaultable=False;status='provider_off';reason='供应商已暂停新任务'
            if item.get('managed_group')=='lechuang':
                matches=_lechuang_items(published_items)
                models=[str(x.get('label') or x.get('front') or '') for x in matches]
                model_keys=[str(x.get('front') or '') for x in matches]
                visible=defaultable=bool(matches)
                if not matches:status='unconfigured';reason='尚无已启用且已发布参数的渠道映射'
            if item.get('feature') and not feature_flags.is_enabled(item['feature']):
                visible=defaultable=False;status='feature_off';reason='功能开关未开启'
            required_host=item.get('host')
            if required_host and hostname!=required_host:
                visible=defaultable=False;status='site_only';reason='仅在专属站点 '+required_host+' 显示'
            entries.append(dict(key=item['key'],label=item['label'],visible=visible,
                                defaultable=defaultable,status=status,reason=reason,
                                models=models,model_keys=model_keys))
        result[page]=entries
    return result


def _effective_layout(layout, entries):
    effective={}
    for page,cfg in layout.items():
        available={x['key'] for x in entries.get(page,[]) if x['visible'] and x['defaultable']}
        default=cfg.get('default')
        if default not in available:
            default=next((key for key in cfg.get('order',[]) if key in available),'')
        effective[page]={'order':list(cfg.get('order',[])),'default':default}
    return effective


def admin_layout_state(hostname='huangquechuanmei.com'):
    """Return saved layout plus the user-visible, availability-aware directory."""
    layout=layout_state();entries=workbench_entries(hostname=hostname)
    return {'layout':layout,'effective_layout':_effective_layout(layout,entries),'entries':entries}


def layout_save(actor, body):
    value = _clean_layout(body.get('layout') if isinstance(body, dict) else {}, strict=True)
    entries=workbench_entries()
    for page,cfg in value.items():
        allowed={item['key'] for item in entries.get(page,[]) if item['defaultable'] and item['visible']}
        if cfg['default'] not in allowed:
            raise ValueError(page + ' 默认渠道当前不可用，请选择可接单渠道')
    if channel_store.enabled():
        channel_store.save_layout(actor, value)
        return admin_layout_state()
    with closing(store.db()) as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute('INSERT OR REPLACE INTO settings VALUES(4,?)', (json.dumps(value, ensure_ascii=False),))
        store._audit(c, 'layout.save', 'workbench', actor)
        c.commit()
    return admin_layout_state()

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
                    'quality':['low','medium','high'],'output_format':['png','jpeg','webp'],'background':['opaque','transparent']}
        elif profile=='gpt_image':
            fields={'size':['1024x1024','1536x1024','1024x1536'],'quality':['low','medium','high'],
                    'output_format':['png','jpeg','webp'],'background':['opaque','transparent']}
        elif profile=='dalle3':
            fields={'size':['1024x1024','1792x1024','1024x1792'],'quality':['standard','hd'],'output_format':['png']}
        elif profile=='compatible':
            fields={'size':['1024x1024'],'quality':['standard'],'output_format':['png']}
        else:raise ValueError('未知图片参数协议')
        if cfg['model']=='dall-e-3' and profile!='dalle3':raise ValueError('DALL-E 3 须选择对应参数协议')
        editable = profile in {'gpt_image', 'gpt_image_2'}
        return dict(profile=profile,profiles=['gpt_image','gpt_image_2','dalle3','compatible'],fields=fields,
                    reference_min=0,reference_max=1 if editable else 0,mask=editable,count=1)
    if adapter=='minimax_h3':
        from .video_minimax_h3 import MODEL
        if cfg['model']!=MODEL:raise ValueError('此协议实际模型固定为 '+MODEL+'，请先修正连接配置')
        return dict(profile='minimax_h3',profiles=['minimax_h3'],fields={'ratio':['9:16','16:9','1:1','4:3','3:4','21:9'],
                    'resolution':['2K'],'duration':list(range(4,16))},reference_min=0,reference_max=5,count=1)
    if adapter=='xai_video':
        resolutions = ['720p', '1080p'] if cfg['model']=='grok-imagine-video-1.5' else ['720p']
        return dict(profile='xai_video',profiles=['xai_video'],fields={'ratio':['9:16','16:9','1:1'],
                    'resolution':resolutions,'duration':list(range(1,16))},
                    reference_min=1 if cfg['model']=='grok-imagine-video-1.5' else 0,reference_max=1,count=1)
    if adapter=='wavespeed_tryon':
        # 线路二换装：人物图 + 衣服图 → 换装展示视频。时长取自原厂同一套 _tryon_seconds 规则。
        from . import video as video_domain
        return dict(profile='wavespeed_tryon',profiles=['wavespeed_tryon'],
                    fields={'duration':list(range(5,16))},
                    reference_min=2,reference_max=2,count=1)
    if adapter=='cosyvoice_tts':
        # 复用原厂 audio.py 的量纲：payload 的 speed/pitch/volume 与 CosyVoice 参数的换算一致，
        # 这里只声明可用范围，不另写一份。
        return dict(profile='cosyvoice_tts',profiles=['cosyvoice_tts'],
                    fields={'speed':[round(0.5+0.1*i,1) for i in range(16)],
                            'pitch':list(range(-12,13,2)),
                            'volume':[-50,-25,0,25,50,75,100]},
                    reference_min=0,reference_max=0,count=1)
    if adapter=='sora_video':
        # 复用原厂 Sora 的模型 / 时长 / 比例 / 尺寸事实，不另写一份白名单。
        from . import video as video_domain
        if cfg['model'] not in video_domain.SORA_MODELS:
            raise ValueError('Sora 适配器仅支持模型：'+'、'.join(sorted(video_domain.SORA_MODELS)))
        ratios = sorted(video_domain.SORA_RATIOS)
        resolutions = sorted({key[1] for key in video_domain.SORA_SIZE_MAP if key[0] == cfg['model']}) or ['720p']
        return dict(profile='sora_video',profiles=['sora_video'],
                    fields={'ratio':ratios,'resolution':resolutions,
                            'duration':sorted(video_domain.SORA_SECONDS)},
                    reference_min=0,reference_max=1,count=1)
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
    mask = spec.get('mask') is True
    if mask:
        if not cap.get('mask'):raise ValueError('该适配器不支持局部修图（蒙版）')
        if high < 1:raise ValueError('开启局部修图前请把参考图上限设为至少 1 张')
    return dict(profile=cap['profile'],fields=clean_fields,combinations=clean,default=spec['default'],
                reference_min=low,reference_max=high,mask=mask)


def drafts(c):
    row=c.execute('SELECT value FROM settings WHERE id=3').fetchone()
    return json.loads(row[0]) if row else {}


def admin_state(cid,profile=None):
    cfg=store.version(cid)
    if channel_store.enabled():
        draft=channel_store.draft_setting().get(cid)
        history=[]
        for r in channel_store.channel_versions(cid):
            spec=json.loads(r['config']).get('parameters')
            if spec:history.append(dict(version=r['version'],actor=r['actor'],created=r['created'],parameters=spec))
    else:
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
    if channel_store.enabled():
        channel_store.change_parameters(actor,body)
        return admin_state(cid)
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


def public_catalog(hostname='huangquechuanmei.com'):
    result=_published_items();entries=workbench_entries(result,hostname)
    layout=_effective_layout(layout_state(),entries)
    return {'items':result,'refresh_seconds':15,'layout':layout,'layout_entries':entries}


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
    operation_id, operation_route = store.routing_for_payload(kind, payload)
    if operation_route:
        if operation_route['state'] == 'paused':
            raise ValueError('该功能已由管理员暂停，无法报价')
        if operation_route['state'] == 'managed':
            cfg = store.version(operation_route['channel'])
            return apply(cfg, payload)[1]
    front=str(payload.get('channel') if kind=='xiaole_video' else payload.get('model') or '')
    if channel_store.enabled():
        mapping=channel_store.mapping_by_selector(kind+':'+front)
        if not mapping.get('enabled'):
            if payload.get('parameter_selection'):raise ValueError('功能映射已变化，请刷新后重试')
            return None
        cfg=store.version(mapping['channel'])
        return apply(cfg,payload)[1]
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
    ref_count = 1 if spec.get('mask') else spec['reference_min']
    payload=dict(prompt='示例提示词（仅预览，不发起生成）',parameter_selection={'combination':body.get('combination') or spec['default']},
                 reference_images=['https://example.invalid/reference.png']*ref_count)
    if spec.get('mask'):
        payload['mask']='data:image/png;base64,<占位，预览不读取>'
    payload,points=apply(cfg,payload,required=False)
    from .channel_runtime import build_generation_request
    path,request,files=build_generation_request(cfg,payload,preview=True)
    summary=dict(path=path,body=request,points=points,validation='参数校验通过；尚未调用供应商，不代表成品验证通过')
    if files:summary['files']=[dict(name=name,size=len(content) if content else 0) for name,_,content in files]
    return summary
