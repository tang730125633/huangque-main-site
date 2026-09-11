"""Versioned channel routing. No credentials or mutable configuration in jobs."""
import base64
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path

ADAPTERS = {
    'openai_image': {'name': 'OpenAI 兼容文生图', 'kind': 'image', 'references': False},
    'minimax_h3': {'name': 'MiniMax H3 视频协议', 'kind': 'xiaole_video', 'references': True},
    'xai_video': {'name': 'Grok 视频协议', 'kind': 'xiaole_video', 'references': True},
    # 乐创（api.lechuang.chat）统一生成协议：POST /generations，图/视频共用同一入口。
    'lechuang_image': {'name': '乐创统一生图', 'kind': 'image', 'references': True},
    'lechuang_video': {'name': '乐创统一视频', 'kind': 'xiaole_video', 'references': True},
}


def db():
    path = Path(os.environ.get('HQ_CHANNEL_DB', str(Path(__file__).resolve().parents[1] / 'channel_management.db')))
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(path), timeout=15)
    c.row_factory = sqlite3.Row
    os.chmod(path, 0o600)
    c.executescript('''
      CREATE TABLE IF NOT EXISTS channels(id TEXT PRIMARY KEY, version INTEGER, enabled INTEGER);
      CREATE TABLE IF NOT EXISTS versions(channel TEXT, version INTEGER, config TEXT, secret TEXT,
        actor TEXT, created REAL, PRIMARY KEY(channel,version));
      CREATE TABLE IF NOT EXISTS mappings(selector TEXT PRIMARY KEY, config TEXT, actor TEXT, updated REAL);
      CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, channel TEXT, version INTEGER, kind TEXT,
        state TEXT, started REAL, updated REAL, duration REAL, detail TEXT, job_id TEXT, provider_id TEXT,
        reservation REAL DEFAULT 0);
      CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, action TEXT, target TEXT, actor TEXT, created REAL);
      CREATE TABLE IF NOT EXISTS schedule(channel TEXT PRIMARY KEY, light_due REAL, full_due REAL);
      CREATE TABLE IF NOT EXISTS settings(id INTEGER PRIMARY KEY, value TEXT);
      CREATE TABLE IF NOT EXISTS channel_incidents(channel TEXT, kind TEXT, state TEXT, action TEXT, occurred REAL,
        PRIMARY KEY(channel,kind));
      CREATE INDEX IF NOT EXISTS channel_runs_recent ON runs(channel,kind,started);
      CREATE INDEX IF NOT EXISTS channel_runs_job ON runs(job_id,kind);
      CREATE INDEX IF NOT EXISTS channel_runs_state ON runs(state,started);
      CREATE INDEX IF NOT EXISTS channel_events_rate ON events(action,target,created);
    ''')
    return c


def _crypt(value, decrypt=False):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from .provider_keys import _master_key
    cipher = AESGCM(_master_key())
    if decrypt:
        raw = base64.b64decode(value)
        return cipher.decrypt(raw[:12], raw[12:], b'hq-channel-v1').decode()
    nonce = os.urandom(12)
    return base64.b64encode(nonce + cipher.encrypt(nonce, value.encode(), b'hq-channel-v1')).decode()


def _url(value, proxy=False):
    from .safe_http import validate_target
    value = str(value or '').strip().rstrip('/')
    target = validate_target(value, proxy=proxy)
    if proxy and target.request_target != '/':
        raise ValueError('代理地址不能包含路径或查询参数')
    if target.request_target != '/':
        # Base paths are supported, but query strings would leak into every API path.
        from urllib.parse import urlsplit
        if urlsplit(value).query:
            raise ValueError('基础地址不能包含查询参数')
    return value


def _number(body, key, default, low, high):
    value = float(body.get(key, default))
    if not low <= value <= high:
        raise ValueError('%s 应在 %s～%s 之间' % (key, low, high))
    if key in {'concurrency','queue_limit','rpm','poll_seconds','daily_hour','daily_limit'} and not value.is_integer():
        raise ValueError('%s 必须为整数' % key)
    return value


def save(actor, body):
    cid = str(body.get('id') or uuid.uuid4().hex)
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', cid):
        raise ValueError('渠道编号格式无效')
    adapter = body.get('adapter')
    if adapter not in ADAPTERS:
        raise ValueError('该协议尚未接入，不可启用')
    name, model = str(body.get('name') or '').strip(), str(body.get('model') or '').strip()
    if not name or len(name) > 80 or not model or len(model) > 120:
        raise ValueError('请填写渠道名称及实际模型 ID')
    if adapter == 'minimax_h3' and model != 'MiniMax-H3':
        raise ValueError('MiniMax H3 适配器仅支持 MiniMax-H3')
    supplier = str(body.get('supplier') or '').strip()
    connection_type = body.get('connection_type') or 'unknown'
    if len(supplier) > 100 or connection_type not in {'unknown', 'official', 'relay'}:
        raise ValueError('供应商名称过长或接入方式无效')
    config = dict(name=name, adapter=adapter, model=model, base_url=_url(body.get('base_url')),
                  supplier=supplier, connection_type=connection_type,
                  proxy=_url(body['proxy'], True) if body.get('proxy') else '',
                  timeout=_number(body, 'timeout', 120, 5, 300),
                  concurrency=int(_number(body, 'concurrency', 2, 1, 32)),
                  queue_limit=int(_number(body, 'queue_limit', 20, 0, 200)),
                  rpm=int(_number(body, 'rpm', 30, 1, 1000)),
                  poll_seconds=int(_number(body, 'poll_seconds', 900, 60, 86400)),
                  daily_hour=int(_number(body, 'daily_hour', 9, 0, 23)),
                  daily_limit=int(_number(body, 'daily_limit', 1, 0, 20)),
                  test_cost=_number(body, 'test_cost', 0, 0, 10000),
                  daily_budget=_number(body, 'daily_budget', 0, 0, 100000),
                  monitor=body.get('monitor') is True, daily_test=body.get('daily_test') is True,
                  fixture=body.get('fixture') or {})
    if not isinstance(config['fixture'], dict) or len(json.dumps(config['fixture'])) > 12 * 1024 * 1024:
        raise ValueError('测试素材格式或大小无效')
    if config['daily_test'] and (not config['daily_limit'] or not config['test_cost'] or config['test_cost'] > config['daily_budget']):
        raise ValueError('定时生成测试须配置次数、单次预算和每日预算')
    with closing(db()) as c:
        c.execute('BEGIN IMMEDIATE')
        old = c.execute('SELECT * FROM channels WHERE id=?', (cid,)).fetchone()
        if old and int(body.get('version', -1)) != old['version']:
            raise ValueError('配置已被修改，请刷新后重试')
        if old:
            old_config=json.loads(c.execute('SELECT config FROM versions WHERE channel=? AND version=?',(cid,old['version'])).fetchone()[0])
            if old_config.get('_lifecycle',{}).get('deleted'):
                raise ValueError('渠道在回收站，请先恢复后编辑')
            if old_config.get('_lifecycle'):
                config['_lifecycle']=old_config['_lifecycle']
            if old_config.get('parameters'):
                from .channel_parameters import validate
                config['parameters']=validate(config,old_config['parameters'])
            for mapping_row in c.execute('SELECT config FROM mappings'):
                mapping = json.loads(mapping_row['config'])
                if (mapping.get('enabled') and cid in {mapping.get('channel'), mapping.get('backup')}
                        and mapping.get('kind') != ADAPTERS[adapter]['kind']):
                    raise ValueError('该渠道仍被已启用映射使用，不能更改为不兼容协议；请先调整映射')
        version = old['version'] + 1 if old else 1
        if old and 'reference_images' not in config['fixture']:
            previous = json.loads(c.execute('SELECT config FROM versions WHERE channel=? AND version=?',(cid,old['version'])).fetchone()[0])
            if previous.get('fixture',{}).get('reference_images'):
                config['fixture']['reference_images'] = previous['fixture']['reference_images']
        secret = _crypt(str(body['secret'])) if body.get('secret') else ''
        if not secret and old:
            secret = c.execute('SELECT secret FROM versions WHERE channel=? AND version=?', (cid, old['version'])).fetchone()[0]
        if not secret:
            raise ValueError('请填写 API 密钥；保险箱未配置时不能保存密钥')
        if config['daily_test']:
            from .channel_runtime import validate_payload
            validate_payload(config,config['fixture'])
        c.execute('INSERT INTO versions VALUES(?,?,?,?,?,?)', (cid, version, json.dumps(config), secret, actor, time.time()))
        c.execute('INSERT OR REPLACE INTO channels VALUES(?,?,?)', (cid, version, int(body.get('enabled') is True)))
        c.execute('INSERT OR REPLACE INTO schedule VALUES(?,?,?)', (cid, time.time()+60, _next_daily(config['daily_hour'])))
        _audit(c, 'channel.save', cid, actor)
        c.commit()
    return {'id': cid, 'version': version}


def _next_daily(hour, now=None):
    now = time.time() if now is None else now
    # Explicit Asia/Shanghai, independent of server timezone.
    day = int((now + 8*3600) // 86400) * 86400 - 8*3600
    value = day + hour*3600
    return value if value > now else value + 86400


def _audit(c, action, target, actor):
    c.execute('INSERT INTO events VALUES(?,?,?,?,?)', (uuid.uuid4().hex, action, target, actor, time.time()))


def version(cid, rev=None, with_secret=False):
    with closing(db()) as c:
        if rev is None:
            row = c.execute('SELECT version FROM channels WHERE id=?', (cid,)).fetchone()
            if not row:
                raise ValueError('渠道不存在')
            rev = row[0]
        row = c.execute('SELECT * FROM versions WHERE channel=? AND version=?', (cid, rev)).fetchone()
    if not row:
        raise ValueError('渠道版本不存在')
    result = dict(json.loads(row['config']), id=cid, version=rev)
    if with_secret:
        result['secret'] = _crypt(row['secret'], True)
    return result


def save_mapping(actor, body):
    kind, front = str(body.get('kind') or ''), str(body.get('front') or '').strip()
    cid, backup = str(body.get('channel') or ''), str(body.get('backup') or '')
    if kind == 'xiaole_video' and front not in {'grok','grok15','minimax','omni','micro'}:
        raise ValueError('请选择现有视频请求标识 grok、grok15、minimax、omni 或 micro；新增前台入口需另行接入价格与权限')
    cfg = version(cid)
    if kind != ADAPTERS[cfg['adapter']]['kind'] or not front or len(front) > 100:
        raise ValueError('功能类型与渠道能力不兼容，或前台标识未填写')
    if backup and (backup == cid or ADAPTERS[version(backup)['adapter']]['kind'] != kind):
        raise ValueError('备用渠道必须不同且能力兼容')
    config = dict(kind=kind, front=front, label=str(body.get('label') or front)[:100], channel=cid,
                  backup=backup, enabled=body.get('enabled') is True)
    with closing(db()) as c:
        c.execute('BEGIN IMMEDIATE')
        for target in filter(None,(cid,backup)):
            current=c.execute('SELECT version FROM channels WHERE id=?',(target,)).fetchone()
            if not current:
                raise ValueError('渠道不存在')
            target_cfg=json.loads(c.execute('SELECT config FROM versions WHERE channel=? AND version=?',(target,current[0])).fetchone()[0])
            if target_cfg.get('_lifecycle',{}).get('deleted'):
                raise ValueError('回收站渠道不能配置映射')
            if ADAPTERS[target_cfg['adapter']]['kind']!=kind:
                raise ValueError('渠道能力已变化，请刷新后重试')
        c.execute('INSERT OR REPLACE INTO mappings VALUES(?,?,?,?)', (kind+':'+front, json.dumps(config), actor, time.time()))
        _audit(c, 'mapping.save', kind+':'+front, actor)
        c.commit()
    return config


def capture(kind, payload, preparation=False):
    clean = dict(payload)
    clean.pop('_channel_binding', None)  # Never trust a client supplied private snapshot.
    if preparation and clean.get('parameter_selection'):
        from .channel_parameters import historical, apply
        cfg=historical(clean)
        if ADAPTERS[cfg['adapter']]['kind']!=kind:raise ValueError('参数功能类型不匹配')
        clean,_=apply(cfg,clean)
        from .channel_runtime import validate_payload
        validate_payload(cfg,clean)
        clean['_channel_binding']={'id':cfg['id'],'version':cfg['version']}
        return clean
    if kind not in {'image', 'xiaole_video'}:
        from .channel_lifecycle import require_legacy
        require_legacy(kind,clean)
        return clean
    front = str(clean.get('channel') if kind == 'xiaole_video' else clean.get('model') or '')
    with closing(db()) as c:
        row = c.execute('SELECT config FROM mappings WHERE selector=?', (kind+':'+front,)).fetchone()
        if not row or not json.loads(row[0]).get('enabled'):
            if clean.get('parameter_selection'):
                raise ValueError('功能映射已变化，请刷新参数后重新提交')
            from .channel_lifecycle import require_legacy
            require_legacy(kind,clean)
            return clean
        mapping = json.loads(row[0])
        ch = c.execute('SELECT * FROM channels WHERE id=?', (mapping['channel'],)).fetchone()
        if not ch or not ch['enabled']:
            raise ValueError('该功能的主渠道已停用，请管理员切换渠道')
    cfg = version(ch['id'], ch['version'])
    from .channel_parameters import apply
    clean, parameter_points = apply(cfg,clean)
    from .channel_runtime import validate_payload
    validate_payload(cfg, clean)
    clean['_channel_binding'] = {'id': ch['id'], 'version': ch['version'], 'front': front}
    return clean


def rollback(actor, body):
    old = version(str(body.get('id')), int(body.get('target_version')))
    secret = version(old['id'], old['version'], True)['secret']
    current = version(old['id'])
    return save(actor, dict(old, version=current['version'], secret=secret, enabled=body.get('enabled') is True))


def overview():
    from .channel_lifecycle import legacy_states, LEGACY_SCOPES
    now = time.time()
    with closing(db()) as c:
        channels = [dict(r) for r in c.execute('SELECT * FROM channels')]
        mappings = [json.loads(r[0]) for r in c.execute('SELECT config FROM mappings')]
        runs = [dict(r) for r in c.execute('SELECT * FROM runs ORDER BY started DESC LIMIT 100')]
        events = [dict(r) for r in c.execute('SELECT * FROM events ORDER BY created DESC LIMIT 30')]
        for channel in channels:
            cfg = version(channel['id'], channel['version'])
            channel.update(cfg)
            channel['configured'] = True
            channel['fixture'] = {k:v for k,v in cfg['fixture'].items() if k != 'reference_images'}
            channel['material_count'] = len(cfg['fixture'].get('reference_images') or [])
            channel['history'] = [dict(r) for r in c.execute('SELECT version,actor,created FROM versions WHERE channel=? ORDER BY version DESC LIMIT 20', (channel['id'],))]
            schedule_row = c.execute('SELECT light_due,full_due FROM schedule WHERE channel=?',(channel['id'],)).fetchone()
            channel['schedule'] = dict(schedule_row) if schedule_row else {}
            latest = c.execute("SELECT * FROM runs WHERE channel=? AND version=? AND kind='full' ORDER BY started DESC LIMIT 1", (channel['id'], channel['version'])).fetchone()
            channel['health'] = ('未验证' if not latest else '验证已过期' if now-latest['updated']>86400 else {'passed':'成品核验通过','failed':'异常','unknown':'结果未知','running':'检测中','queued':'待检测'}.get(latest['state'], '未验证'))
            problem = c.execute("SELECT state FROM runs WHERE channel=? AND version=? AND state IN ('failed','unknown') AND updated>? ORDER BY updated DESC LIMIT 1",(channel['id'],channel['version'],max(now-86400,latest['updated'] if latest else 0))).fetchone()
            if problem:
                channel['health'] = '异常' if problem[0]=='failed' else '结果未知'
            stats = c.execute("SELECT COUNT(*) total,SUM(state='failed') failed,SUM(state='unknown') unknown,AVG(CASE WHEN state IN ('passed','failed','unknown') THEN duration END) avg_duration FROM runs WHERE channel=? AND kind='task' AND started>?", (channel['id'], now-86400)).fetchone()
            channel['stats'] = dict(stats)
            channel['checks'] = []
            for check_kind in ('connection', 'auth', 'full'):
                check_row = c.execute("SELECT kind,state,updated,detail FROM runs WHERE channel=? AND version=? AND kind=? ORDER BY started DESC,rowid DESC LIMIT 1", (channel['id'],channel['version'],check_kind)).fetchone()
                if check_row:
                    channel['checks'].append(dict(check_row))
    return {'items': channels, 'mappings': mappings, 'runs': runs, 'events': events, 'adapters': ADAPTERS,
            'legacy_controls':legacy_states(), 'legacy_scopes':LEGACY_SCOPES,
            'notifications': notification_settings(), 'timezone':'Asia/Shanghai', 'stats_window':'最近24小时'}


def reserve(cid, kind, job_id='', snapshot=None):
    cfg = snapshot or version(cid)
    now, rid = time.time(), uuid.uuid4().hex
    with closing(db()) as c:
        c.execute('BEGIN IMMEDIATE')
        if kind!='task':
            current=c.execute('SELECT ch.enabled,v.config FROM channels ch JOIN versions v ON v.channel=ch.id AND v.version=ch.version WHERE ch.id=?',(cid,)).fetchone()
            if not current or not current['enabled'] or json.loads(current['config']).get('_lifecycle',{}).get('deleted'):
                raise ValueError('渠道已停用或在回收站，不能发起新测试')
        if kind == 'full':
            start = int((now+8*3600)//86400)*86400-8*3600
            used = c.execute("SELECT COUNT(*) n,COALESCE(SUM(reservation),0) cost FROM runs WHERE channel=? AND kind='full' AND started>=?", (cid,start)).fetchone()
            if not cfg['test_cost'] or used['n'] >= cfg['daily_limit'] or used['cost']+cfg['test_cost']>cfg['daily_budget']:
                raise ValueError('完整测试预算或次数不足，请先配置；失败和未知结果同样占用预算')
        if kind == 'task':
            existing = c.execute(
                "SELECT id,channel,version,state FROM runs WHERE job_id=? AND kind='task' "
                "ORDER BY started DESC LIMIT 1", (str(job_id),),
            ).fetchone()
            if existing:
                if (existing['state'] == 'queued' and existing['channel'] == cid
                        and int(existing['version']) == int(cfg['version'])):
                    c.commit()
                    return existing['id']
                raise ValueError('该任务已有渠道执行记录，禁止重复提交，请按工单核查')
        pending = c.execute("SELECT COUNT(*) FROM runs WHERE channel=? AND state='queued'",(cid,)).fetchone()[0]
        if pending >= max(1,cfg['queue_limit']):
            raise ValueError('渠道等待队列已满')
        c.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', (rid,cid,cfg['version'],kind,'queued',now,now,None,'等待执行',str(job_id),'',cfg['test_cost'] if kind=='full' else 0))
        c.commit()
    return rid


def finish(rid, state, detail, provider_id=''):
    with closing(db()) as c:
        c.execute('UPDATE runs SET state=?,detail=?,updated=?,duration=?-started,provider_id=CASE WHEN ?!=\'\' THEN ? ELSE provider_id END WHERE id=?',
                  (state, detail[:300], time.time(), time.time(), provider_id, provider_id, rid))
        c.commit()


def notification_settings(private=False):
    with closing(db()) as c:
        row = c.execute('SELECT value FROM settings WHERE id=1').fetchone()
    value = json.loads(row[0]) if row else {'enabled':False}
    if value.get('endpoint'):
        value['endpoint'] = _crypt(value['endpoint'], True) if private else '已配置（隐藏）'
    from . import runtime_observability
    with closing(runtime_observability.database()) as c:
        value['delivery'] = {r['state']:r['n'] for r in c.execute("SELECT state,COUNT(*) n FROM alert_outbox WHERE event_id LIKE 'channel.%' GROUP BY state")}
    return value


def task_evidence(job_id):
    with closing(db()) as c:
        row = c.execute("SELECT channel,version,state,provider_id FROM runs WHERE kind='task' AND job_id=? ORDER BY started DESC LIMIT 1",(str(job_id),)).fetchone()
    return dict(row) if row else {}


def task_recovery_state(job_id):
    """Conservative paid-task recovery state; unreadable evidence is never failure."""
    try:
        evidence = task_evidence(job_id)
        return evidence.get('state') or 'absent'
    except (OSError, sqlite3.Error):
        return 'unavailable'


def mark_interrupted_task_unknown(job_id, detail):
    """Atomically preserve an interrupted provider submission for reconciliation."""
    try:
        with closing(db()) as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute(
                "SELECT id,state FROM runs WHERE kind='task' AND job_id=? "
                "ORDER BY started DESC LIMIT 1", (str(job_id),),
            ).fetchone()
            if not row:
                c.commit()
                return 'absent'
            state = row['state']
            if state == 'running':
                now = time.time()
                changed = c.execute(
                    "UPDATE runs SET state='unknown',detail=?,updated=?,duration=?-started "
                    "WHERE id=? AND state='running'",
                    (str(detail or 'worker interrupted')[:300], now, now, row['id']),
                ).rowcount
                if changed:
                    state = 'unknown'
            c.commit()
            return state
    except (OSError, sqlite3.Error):
        return 'unavailable'


def search_task_ids(query):
    """Find managed tasks by durable provider/channel/version evidence."""
    needle = '%' + str(query or '').lower() + '%'
    if needle == '%%':
        return set()
    try:
        with closing(db()) as c:
            rows = c.execute(
                "SELECT DISTINCT r.job_id FROM runs r LEFT JOIN versions v "
                "ON v.channel=r.channel AND v.version=r.version "
                "WHERE r.kind='task' AND (LOWER(r.provider_id) LIKE ? "
                "OR LOWER(r.channel) LIKE ? OR LOWER(CAST(r.version AS TEXT)) LIKE ? "
                "OR LOWER(COALESCE(v.config,'')) LIKE ?)",
                (needle, needle, needle, needle),
            ).fetchall()
        return {str(row[0]) for row in rows if row[0] not in (None, '')}
    except (OSError, sqlite3.Error):
        return set()


def save_notifications(actor, body):
    from .runtime_observability import valid_endpoint
    old = notification_settings(True)
    endpoint = body.get('endpoint') or old.get('endpoint', '')
    if endpoint and not valid_endpoint(endpoint):
        raise ValueError('通知地址必须为 HTTPS 或本机 HTTP')
    if body.get('enabled') and not endpoint:
        raise ValueError('请填写通知接收地址')
    with closing(db()) as c:
        c.execute('INSERT OR REPLACE INTO settings VALUES(1,?)', (json.dumps({'enabled':body.get('enabled') is True,'endpoint':_crypt(endpoint) if endpoint else ''}),))
        _audit(c,'notification.save','webhook',actor)
        c.commit()
    return notification_settings()
