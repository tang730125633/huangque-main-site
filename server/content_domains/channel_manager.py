"""Versioned channel routing. No credentials or mutable configuration in jobs.

存储层：HQ_CHANNEL_STORE=sqlite（默认，现状）走本模块 SQLite 路径
（HQ_CHANNEL_DB 指向的 channel_management.db）；postgres 走 ``channel_store``
（routing schema 的 11 张表）。切换时所有读方（content / imggen / admin）必须同一
开关一起切，禁止双权威。

PG 模式下本模块的公开读写函数自动分发到 ``channel_store``；``db()`` 是给 SQLite
原生 SQL 调用方用的（channel_runtime / channel_parameters），PG 模式下直接抛错，
它们必须先完成迁移（见 docs/runbooks/postgresql-routing-channels-m3c.md）。
"""
import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

from . import channel_store

ADAPTERS = {
    'openai_image': {'name': 'OpenAI 兼容文生图', 'kind': 'image', 'references': False},
    'gemini_image': {'name': 'Google Gemini 官方生图', 'kind': 'image', 'references': True},
    'minimax_h3': {'name': 'MiniMax H3 视频协议', 'kind': 'xiaole_video', 'references': True,
                    'verification': ('connection', 'full')},
    'xai_video': {'name': 'Grok 视频协议', 'kind': 'xiaole_video', 'references': True},
    # 乐创（api.lechuang.chat）统一生成协议：POST /generations，图/视频共用同一入口。
    'lechuang_image': {'name': '乐创统一生图', 'kind': 'image', 'references': True,
                      # 乐创的 /api/v1/models 端点真实存在（无 Key 401），但带 Key 长期
                      # 返回 500「Internal Server Error」——上游辅助接口异常。
                      # 判定必需项只保留 connection + full：full 已经真实用过这个 Key
                      # 和模型完成生成，鉴权证据比模型列表接口更强；把辅助接口异常
                      # 当成渠道不可用，会让真实能出成品的渠道永久变红。
                      # auth 仍留在 checks_supported：管理员可以手动跑，失败记录照常展示。
                      'verification': ('connection', 'full'),
                      'checks_supported': ('connection', 'auth', 'full'),
                      # 乐创前面挂着 Cloudflare，开了「按浏览器特征封禁」：只带
                      # Content-Type + Authorization 的请求会被 403 error code 1010
                      # 直接挡掉，与走哪条出口代理无关（隧道/mihomo/直连三级全一样）。
                      # 补上浏览器特征头后 Cloudflare 放行，才拿得到乐创自己的响应。
                      # 按【协议】声明，不按渠道名或 base_url 硬编码。
                      'request_headers': {
                          'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                                         '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'),
                          'Accept': 'application/json, text/plain, */*',
                          'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
                      }},
    'lechuang_video': {'name': '乐创统一视频', 'kind': 'xiaole_video', 'references': True,
                      'verification': ('connection', 'full'),
                      'checks_supported': ('connection', 'auth', 'full'),
                      'request_headers': {
                          'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                                         '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'),
                          'Accept': 'application/json, text/plain, */*',
                          'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
                      }},
    # Sora：复用原厂 video_openai 客户端（已支持注入 api_key / api_base），
    # kind 用任务类型 sora_video，与 function_registry 的 task_match.kind 一致。
    'sora_video': {'name': 'OpenAI Sora 协议', 'kind': 'sora_video', 'references': True,
                   'verification': ('connection', 'full')},
    # 换装两条线路是不同供应商、不同输入（线路二=人物图+衣服图；线路一=人物视频），
    # 各占一个适配器，由任务类型契约的 line 区分匹配。
    'wavespeed_tryon': {'name': 'WaveSpeed 换装（线路二）', 'kind': 'tryon', 'references': True,
                        'line': '2', 'verification': ('connection', 'full'),
                        # 工作流型 API，没有独立的鉴权端点可探
                        'checks_supported': ('connection', 'full')},
    # 配音：复用原厂 audio.py 的 CosyVoice 链路。一个渠道 = 一套 DashScope 凭据 + 接入点，
    # 拖动即切换不同的配音账号；音色仍由任务参数决定。
    'cosyvoice_tts': {'name': '阿里百炼 CosyVoice 配音', 'kind': 'audio', 'references': False,
                      'verification': ('connection', 'full'),
                      # 该服务没有 /models 列表端点，鉴权由完整生成证明
                      'checks_supported': ('connection', 'full')},
    # HeyGen：渠道的 secret 是一份完整的 MCP OAuth 凭据 JSON（access/refresh token）。
    # 落到渠道专属文件后由 video.heygen_credential_scope 注入，原厂那 ~11 处 MCP 调用
    # 自动改用渠道自己的账号；拖动即切换不同的 MCP 账号。
    # 数字人与电影化身走不同的原厂入口，因此各占一条适配器。
    # HeyGen 走 MCP/OAuth + egress 隧道，普通 HTTP HEAD 探不到（mcp.heygen.com 不接受 HEAD），
    # 连接探测对它永远是失败——那是探测方式的问题，不是渠道不可用。
    # 按协议如实声明：只以完整生成为证据。
    'heygen_mcp_video': {'name': 'HeyGen MCP（数字人口播）', 'kind': 'video', 'references': True,
                         'verification': ('full',),
                         # MCP 端点不接受 HEAD 连接探测，也没有独立鉴权端点，
                         # 实测带不带出口代理都失败——两项都不适用，只留完整生成
                         'checks_supported': ('full',)},
    'heygen_mcp_cinematic': {'name': 'HeyGen MCP（电影化身）', 'kind': 'cinematic', 'references': True,
                             'verification': ('full',),
                             'checks_supported': ('full',)},
}


ALL_CHECKS = ('connection', 'auth', 'full')


def verification_required(adapter):
    """判定「当前配置验证通过」需要哪些检测项。

    来自协议自身声明；没声明就按三项算（不因为缺声明而放行）。
    """
    spec = ADAPTERS.get(adapter) or {}
    return tuple(spec.get('verification') or ALL_CHECKS)


def checks_supported(adapter):
    """该协议实际上能执行哪些检测项（可提交、可产生有效证据）。

    与 verification_required 是两件事：某项不是「判定必需」不等于「协议不支持」。
    只有核实过确实不支持的才排除，例如 MCP 端点不接受 HEAD 连接探测、
    配音/工作流 API 没有独立鉴权端点。
    """
    spec = ADAPTERS.get(adapter) or {}
    return tuple(spec.get('checks_supported') or ALL_CHECKS)


def db():
    if channel_store.enabled():
        # PG 模式下 SQLite 不再是权威：绝不返回旧库连接，避免「配置读 PG、证据写 SQLite」
        # 的双权威。仍直连本函数的模块必须先迁移（见 M3C runbook 的切换前置条件）。
        raise RuntimeError(
            'HQ_CHANNEL_STORE=postgres：channel_management.db 已停用，db() 不再提供连接；'
            '本模块公开 API 已自动分发到 channel_store，'
            'channel_runtime / channel_parameters 必须先完成迁移再切换'
        )
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
      CREATE TABLE IF NOT EXISTS operation_mappings(operation_id TEXT PRIMARY KEY, revision INTEGER,
        state TEXT, config TEXT, actor TEXT, updated REAL);
      CREATE TABLE IF NOT EXISTS operation_mapping_versions(operation_id TEXT, revision INTEGER,
        state TEXT, config TEXT, actor TEXT, created REAL, PRIMARY KEY(operation_id,revision));
      CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, channel TEXT, version INTEGER, kind TEXT,
        state TEXT, started REAL, updated REAL, duration REAL, detail TEXT, job_id TEXT, provider_id TEXT,
        reservation REAL DEFAULT 0);
      CREATE TABLE IF NOT EXISTS run_snapshots(run_id TEXT PRIMARY KEY, operation_id TEXT,
        mapping_revision INTEGER, invocation_source TEXT, snapshot TEXT);
      CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, action TEXT, target TEXT, actor TEXT, created REAL);
      CREATE TABLE IF NOT EXISTS schedule(channel TEXT PRIMARY KEY, light_due REAL, full_due REAL);
      CREATE TABLE IF NOT EXISTS settings(id INTEGER PRIMARY KEY, value TEXT);
      CREATE TABLE IF NOT EXISTS channel_incidents(channel TEXT, kind TEXT, state TEXT, action TEXT, occurred REAL,
        PRIMARY KEY(channel,kind));
      CREATE INDEX IF NOT EXISTS channel_runs_recent ON runs(channel,kind,started);
      CREATE INDEX IF NOT EXISTS channel_runs_job ON runs(job_id,kind);
      CREATE INDEX IF NOT EXISTS channel_runs_state ON runs(state,started);
      CREATE INDEX IF NOT EXISTS channel_events_rate ON events(action,target,created);
      CREATE INDEX IF NOT EXISTS operation_mapping_history
        ON operation_mapping_versions(operation_id,revision DESC);
    ''')
    return c


def _begin_write(connection):
    """Open the write transaction; SQLite serializes it, this module holds no lock itself."""
    connection.execute('BEGIN IMMEDIATE')


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


def _channel_config(body):
    """构造并校验渠道配置；SQLite 与 PostgreSQL 两条路径共用这一份，报错逐字一致。"""
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
    return cid, config


def save(actor, body):
    if channel_store.enabled():
        return channel_store.save(actor, body)
    cid, config = _channel_config(body)
    with closing(db()) as c:
        _begin_write(c)
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
                if (mapping.get('enabled') and cid in mapping_channel_ids(mapping)
                        and mapping.get('kind') != ADAPTERS[config['adapter']]['kind']):
                    raise ValueError('该渠道仍被已启用映射使用，不能更改为不兼容协议；请先调整映射')
            for mapping_row in c.execute('SELECT operation_id,state,config FROM operation_mappings'):
                mapping = json.loads(mapping_row['config'])
                if (mapping_row['state'] in {'shadow', 'managed'}
                        and cid in mapping_channel_ids(mapping)):
                    from .function_registry import operation
                    try:
                        _validate_operation_config(config, operation(mapping_row['operation_id']))
                    except ValueError as exc:
                        raise ValueError('该渠道仍被功能映射使用，不能保存不兼容配置；请先调整映射') from exc
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
    if channel_store.enabled():
        return channel_store.version(cid, rev, with_secret)
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


MAPPING_STATES = {'legacy', 'shadow', 'managed', 'paused'}
MAX_MAPPING_CHANNELS = 8
ROUTE_CANDIDATE_FIELDS = ('id', 'version', 'adapter', 'model')


def mapping_channel_ids(mapping):
    """Return the ordered channel ids for old and new mapping payloads."""
    if not isinstance(mapping, dict):
        return []
    raw = mapping.get('channels')
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item or '').strip()]
    return list(dict.fromkeys(
        str(mapping.get(key) or '').strip() for key in ('channel', 'backup')
        if str(mapping.get(key) or '').strip()
    ))


def _requested_mapping_channels(body):
    if 'channels' not in body:
        return mapping_channel_ids(body)
    raw = body.get('channels')
    if not isinstance(raw, list):
        raise ValueError('渠道优先级必须是有序列表')
    channels = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise ValueError('渠道优先级包含无效渠道')
        channels.append(item.strip())
    if len(channels) > MAX_MAPPING_CHANNELS:
        raise ValueError('单个功能最多配置 %d 个渠道优先级' % MAX_MAPPING_CHANNELS)
    if len(set(channels)) != len(channels):
        raise ValueError('渠道优先级不能包含重复渠道')
    return channels


def _display_order(body, state, channels):
    """Persist UI order separately from the executable managed failover chain."""
    order = body.get('display_order')
    if order is None:
        return None
    if (not isinstance(order, list) or not order or len(order) > 200
            or any(not isinstance(x, str) or not x.strip() or len(x) > 200 for x in order)
            or len(set(order)) != len(order)):
        raise ValueError('显示顺序必须是不重复的渠道列表')
    expected = channels[0] if state == 'managed' and channels else '@original' if state == 'legacy' else None
    if expected and order[0] != expected:
        raise ValueError('显示顺序第一项必须是当前接单线路')
    return list(order)


def _route_candidate(cfg):
    return {key: cfg.get(key) for key in ROUTE_CANDIDATE_FIELDS}


def _same_parameter_contract(left, right):
    return (left.get('model') == right.get('model')
            and left.get('adapter') == right.get('adapter')
            and (left.get('parameters') or None) == (right.get('parameters') or None))


def _managed_image_route(mapping, contract, connection=None):
    """Build a secret-free immutable route plan for one paid image job.

    映射里的 ``channels`` 是管理员排的可选渠道列表（允许各自使用不同模型 ID）；
    自动故障切换链在这里按需派生，两者职责分开，不额外建表：

    * **主渠道** ``route_order[0]``：管理员明确指定的接单渠道。只要渠道自身可用
      （存在、未回收、已启用、能承接该功能）就按它自己的模型 / 参数 / 凭据接单，
      不再要求与其它渠道同名同参数，也不要求有最近 24 小时的完整生成测试。
    * **候补链** ``route_candidates``：只收与主渠道同一模型 / 协议 / 参数契约的渠道。
      自动故障切换只在等价渠道之间跳，异模型渠道只进 ``skipped`` 并说明原因。
    """
    route_order = mapping_channel_ids(mapping)
    if not route_order:
        raise ValueError('该功能没有可用渠道')
    pricing = _mapping_channel(
        route_order[0], contract, require_enabled=True, connection=connection)
    candidates, skipped = [], []
    for channel_id in route_order:
        try:
            cfg = _mapping_channel(
                channel_id, contract, require_enabled=True, connection=connection)
            if channel_id != pricing['id'] and not _same_parameter_contract(pricing, cfg):
                raise ValueError('与主渠道模型或参数契约不一致，不进入自动故障切换链')
            candidates.append(_route_candidate(cfg))
        except ValueError as exc:
            skipped.append({'id': channel_id, 'reason': str(exc)[:120]})
    if not candidates:
        raise ValueError('该功能没有可用渠道')
    return pricing, candidates, skipped


def public_execution_snapshot(binding):
    """Keep routing evidence useful without persisting secrets or raw config."""
    if not isinstance(binding, dict):
        return {}
    snapshot = {key: binding.get(key) for key in (
        'operation_id', 'mapping_revision', 'id', 'version', 'adapter',
        'model', 'front', 'invocation_source', 'pricing_id', 'pricing_version',
        'route_attempt', 'switch_reason',
    ) if binding.get(key) not in (None, '')}
    snapshot['route_order'] = [str(item) for item in binding.get('route_order', [])
                               if str(item or '')][:MAX_MAPPING_CHANNELS]
    snapshot['route_candidates'] = [
        {key: item.get(key) for key in ROUTE_CANDIDATE_FIELDS}
        for item in binding.get('route_candidates', [])[:MAX_MAPPING_CHANNELS]
        if isinstance(item, dict) and item.get('id')
    ]
    snapshot['route_skipped'] = [
        {'id': str(item.get('id') or ''), 'reason': str(item.get('reason') or '')[:120]}
        for item in binding.get('route_skipped', [])[:MAX_MAPPING_CHANNELS]
        if isinstance(item, dict) and item.get('id')
    ]
    snapshot['attempts'] = [
        {key: item.get(key) for key in ('attempt', 'channel', 'version', 'state', 'detail')}
        for item in binding.get('attempts', [])[:MAX_MAPPING_CHANNELS]
        if isinstance(item, dict) and item.get('channel')
    ]
    return snapshot


def operation_mapping(operation_id, revision=None, connection=None):
    if channel_store.enabled():
        return channel_store.operation_mapping(operation_id, revision, connection)
    operation_id = str(operation_id or '').strip()
    owns_connection = connection is None
    c = connection or db()
    try:
        if revision is None:
            row = c.execute(
                'SELECT operation_id,revision,state,config,actor,updated FROM operation_mappings WHERE operation_id=?',
                (operation_id,),
            ).fetchone()
        else:
            row = c.execute(
                'SELECT operation_id,revision,state,config,actor,created AS updated '
                'FROM operation_mapping_versions WHERE operation_id=? AND revision=?',
                (operation_id, int(revision)),
            ).fetchone()
    finally:
        if owns_connection:
            c.close()
    if not row:
        return None
    result = dict(json.loads(row['config']), operation_id=row['operation_id'],
                  revision=row['revision'], state=row['state'], actor=row['actor'],
                  updated=row['updated'])
    result['channels'] = mapping_channel_ids(result)
    return result


def _validate_operation_config(cfg, contract):
    if not contract or ADAPTERS[cfg['adapter']]['kind'] != contract['channel_kind']:
        raise ValueError('功能与渠道能力不兼容')
    # 换装两条线路是不同供应商、不同输入（线路二=人物图+衣服图；线路一=人物视频），
    # 用 task_match.line 做硬约束，避免把线路二渠道配到线路一功能上。
    adapter_line = str(ADAPTERS[cfg['adapter']].get('line') or '')
    contract_line = str((contract.get('task_match') or {}).get('line') or '')
    if adapter_line and contract_line and adapter_line != contract_line:
        raise ValueError('该渠道协议对应换装线路 %s，与功能的线路 %s 不一致' % (
            adapter_line, contract_line))
    rule = contract.get('task_match') or {}
    needs_references = rule.get('reference_count') == '>0'
    supports_references = bool(
        ADAPTERS[cfg['adapter']].get('references')
        or int((cfg.get('parameters') or {}).get('reference_max') or 0) > 0
    )
    if needs_references and not supports_references:
        raise ValueError('该功能需要参考图，但渠道协议未声明参考图能力')
    if rule.get('mask_present') and not (cfg.get('parameters') or {}).get('mask'):
        raise ValueError('该功能需要蒙版能力，但渠道未启用蒙版参数')
    if (rule.get('reference_count') == 0 and cfg['adapter'] == 'xai_video'
            and cfg.get('model') == 'grok-imagine-video-1.5'):
        raise ValueError('Grok 1.5 必须使用参考图，不能承接文生视频功能')


def _mapping_channel(cid, contract_or_kind, require_enabled=False, connection=None):
    """解析一个渠道的当前配置，并做「能不能接这个功能」的校验。

    ``require_enabled=True`` 只额外要求渠道处于启用状态。
    完整生成测试的 24 小时时效**不再**是接单门槛，只作为后台状态提示；
    它仍然保留在 ``_ready_candidate()``（自动故障切换前的复核）里。
    """
    if channel_store.enabled():
        return channel_store._mapping_channel(
            cid, contract_or_kind, require_enabled=require_enabled, connection=connection)
    contract = contract_or_kind if isinstance(contract_or_kind, dict) else None
    kind = contract['channel_kind'] if contract else str(contract_or_kind)
    cid = str(cid or '').strip()
    if not cid:
        raise ValueError('请选择主渠道')
    owns_connection = connection is None
    c = connection or db()
    try:
        current = c.execute('SELECT version,enabled FROM channels WHERE id=?', (cid,)).fetchone()
        if not current:
            raise ValueError('渠道不存在')
        version_row = c.execute(
            'SELECT config FROM versions WHERE channel=? AND version=?',
            (cid, current['version']),
        ).fetchone()
        if not version_row:
            raise ValueError('渠道版本不存在')
        cfg = dict(json.loads(version_row['config']), id=cid, version=current['version'])
        if cfg.get('_lifecycle', {}).get('deleted'):
            raise ValueError('回收站渠道不能配置映射')
        if contract:
            _validate_operation_config(cfg, contract)
        elif ADAPTERS[cfg['adapter']]['kind'] != kind:
            raise ValueError('功能与渠道能力不兼容')
        if require_enabled and not current['enabled']:
            raise ValueError('渠道尚未启用，不能接单或发布为托管状态')
    finally:
        if owns_connection:
            c.close()
    return cfg


def _ready_candidate(cid, connection=None):
    """自动故障切换前的候选复核：存在、不在回收站、仍启用、且有最近 24 小时完整生成测试。

    这里刻意**不**跟着手动切换一起放宽：管理员手动指定主渠道只看「渠道可用」，
    而自动切换会在无人值守的情况下把任务交给另一个渠道，因此仍然要求最近 24 小时
    内有通过的完整生成测试。报错按「候补渠道」措辞，便于后台看懂为什么这一跳被跳过。
    """
    if channel_store.enabled():
        return channel_store._ready_candidate(cid, connection=connection)
    cid = str(cid or '').strip()
    if not cid:
        raise ValueError('候选渠道无效')
    owns_connection = connection is None
    c = connection or db()
    try:
        current = c.execute('SELECT version,enabled FROM channels WHERE id=?', (cid,)).fetchone()
        if not current:
            raise ValueError('候选渠道不存在')
        row = c.execute('SELECT config FROM versions WHERE channel=? AND version=?',
                        (cid, current['version'])).fetchone()
        if not row:
            raise ValueError('候选渠道版本不存在')
        if json.loads(row['config']).get('_lifecycle', {}).get('deleted'):
            raise ValueError('候选渠道在回收站')
        if not current['enabled']:
            raise ValueError('候选渠道已停用')
        latest = c.execute(
            "SELECT state,updated FROM runs WHERE channel=? AND version=? AND kind='full' "
            'ORDER BY started DESC,rowid DESC LIMIT 1', (cid, current['version']),
        ).fetchone()
        if not latest or latest['state'] != 'passed' or time.time() - latest['updated'] > 86400:
            raise ValueError('候选渠道缺少最近24小时通过的完整生成测试')
    finally:
        if owns_connection:
            c.close()
    return True


def save_operation_mapping(actor, body):
    """Publish one immutable operation mapping revision with optimistic locking."""
    if channel_store.enabled():
        return channel_store.save_operation_mapping(actor, body)
    from .function_registry import operation
    operation_id = str(body.get('operation_id') or '').strip()
    contract = operation(operation_id)
    if not contract or not contract['channel_eligible']:
        raise ValueError('该功能不支持通用图片/视频渠道映射')
    state = str(body.get('state') or 'legacy').strip().lower()
    if state not in MAPPING_STATES:
        raise ValueError('映射状态必须为 legacy、shadow、managed 或 paused')
    channels = _requested_mapping_channels(body)
    now = time.time()
    with closing(db()) as c:
        _begin_write(c)
        if state in {'shadow', 'managed'}:
            if not channels:
                raise ValueError('请选择主渠道')
            # 手动切换只看「渠道自身能否接这个功能」：存在、未回收、已启用、能力匹配。
            # 不再要求与其它渠道同名 / 同协议适配器 / 同参数配置——映射里的每条渠道
            # 各自保留自己的模型 ID，由渠道快照带着走，不为了通过比较而改写模型名。
            for target in channels:
                _mapping_channel(target, contract, require_enabled=True, connection=c)
        else:
            channels = []
        cid = channels[0] if channels else ''
        backup = channels[1] if len(channels) > 1 else ''
        config = {
            'kind': contract['channel_kind'], 'label': contract['name'],
            'channel': cid, 'backup': backup, 'channels': channels,
        }
        display_order = _display_order(body, state, channels)
        if display_order is not None:
            config['display_order'] = display_order
        current = c.execute('SELECT revision FROM operation_mappings WHERE operation_id=?',
                            (operation_id,)).fetchone()
        expected = body.get('expected_revision')
        try:
            expected_number = None if expected in (None, '') else int(expected)
        except (TypeError, ValueError):
            raise ValueError('功能映射版本无效，请刷新后重试') from None
        if current and expected_number != int(current['revision']):
            raise ValueError('功能映射已被修改，请刷新后重试')
        if not current and expected_number not in (None, 0):
            raise ValueError('功能映射版本无效，请刷新后重试')
        revision = int(current['revision']) + 1 if current else 1
        encoded = json.dumps(config, ensure_ascii=False)
        c.execute('INSERT INTO operation_mapping_versions VALUES(?,?,?,?,?,?)',
                  (operation_id, revision, state, encoded, actor, now))
        c.execute('INSERT OR REPLACE INTO operation_mappings VALUES(?,?,?,?,?,?)',
                  (operation_id, revision, state, encoded, actor, now))
        _audit(c, 'operation-mapping.publish', operation_id + ':v' + str(revision), actor)
        c.commit()
    return operation_mapping(operation_id)


def rollback_operation_mapping(actor, body):
    """Restore a historical mapping by publishing it as a new revision."""
    operation_id = str(body.get('operation_id') or '').strip()
    try:
        target_revision = int(body.get('target_revision'))
    except (TypeError, ValueError):
        raise ValueError('请选择有效的历史映射版本') from None
    old = operation_mapping(operation_id, target_revision)
    if not old:
        raise ValueError('历史映射版本不存在')
    return save_operation_mapping(actor, {
        'operation_id': operation_id, 'state': old['state'],
        'channels': mapping_channel_ids(old),
        'display_order': old.get('display_order'),
        'expected_revision': body.get('expected_revision'),
    })


def save_mapping(actor, body):
    if channel_store.enabled():
        return channel_store.save_mapping(actor, body)
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
        _begin_write(c)
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


def _front(kind, payload):
    return str(payload.get('channel') if kind == 'xiaole_video' else payload.get('model') or '')


def _legacy_capture(kind, clean, preparation=False):
    """Compatibility route for operations not yet published to the new control plane."""
    if preparation and clean.get('parameter_selection'):
        from .channel_parameters import historical, apply
        cfg = historical(clean)
        if ADAPTERS[cfg['adapter']]['kind'] != kind:
            raise ValueError('参数功能类型不匹配')
        clean, _ = apply(cfg, clean)
        from .channel_runtime import validate_payload
        validate_payload(cfg, clean)
        clean['_channel_binding'] = {'id': cfg['id'], 'version': cfg['version']}
        return clean
    if kind not in {'image', 'xiaole_video'}:
        from .channel_lifecycle import require_legacy
        require_legacy(kind, clean)
        return clean
    front = _front(kind, clean)
    with closing(db()) as c:
        row = c.execute('SELECT config FROM mappings WHERE selector=?', (kind + ':' + front,)).fetchone()
        if not row or not json.loads(row[0]).get('enabled'):
            if clean.get('parameter_selection'):
                raise ValueError('功能映射已变化，请刷新参数后重新提交')
            from .channel_lifecycle import require_legacy
            require_legacy(kind, clean)
            return clean
        mapping = json.loads(row[0])
        ch = c.execute('SELECT * FROM channels WHERE id=?', (mapping['channel'],)).fetchone()
        if not ch or not ch['enabled']:
            raise ValueError('该功能的主渠道已停用，请管理员切换渠道')
    cfg = version(ch['id'], ch['version'])
    from .channel_parameters import apply
    clean, _ = apply(cfg, clean)
    from .channel_runtime import validate_payload
    validate_payload(cfg, clean)
    clean['_channel_binding'] = {'id': ch['id'], 'version': ch['version'], 'front': front}
    return clean


def routing_for_payload(kind, payload):
    """Resolve only the immutable operation mapping metadata; no provider call occurs."""
    from .function_registry import classify_task
    operation_id = classify_task(kind, payload)
    return operation_id, operation_mapping(operation_id) if operation_id else None


def _seal_shadow(snapshot):
    """Authenticate the server-owned payload used for crash-safe projection."""
    from .provider_keys import _master_key
    clean = dict(snapshot)
    clean.pop('proof', None)
    body = json.dumps(
        clean, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
    ).encode('utf-8')
    clean['proof'] = hmac.new(
        _master_key(), b'hq-channel-shadow-v1\0' + body, hashlib.sha256,
    ).hexdigest()
    return clean


def _valid_shadow(snapshot):
    if not isinstance(snapshot, dict):
        return False
    supplied = str(snapshot.get('proof') or '')
    return bool(supplied) and hmac.compare_digest(
        supplied, _seal_shadow(snapshot).get('proof', ''),
    )


def _confirm_acceptance(connection, payload):
    binding = payload.get('_channel_binding') if isinstance(payload, dict) else None
    if not isinstance(binding, dict) or not binding.get('operation_id'):
        return True
    operation_id = str(binding['operation_id'])
    from .function_registry import operation
    contract = operation(operation_id)
    mapping = operation_mapping(operation_id, connection=connection)
    route_order = [str(item) for item in binding.get('route_order', []) if str(item or '')]
    candidates = binding.get('route_candidates') or []
    if (not mapping or mapping.get('state') != 'managed'
            or int(mapping.get('revision') or 0) != int(binding.get('mapping_revision') or 0)
            or (route_order and mapping_channel_ids(mapping) != route_order)
            or (not route_order and mapping.get('channel') != binding.get('id'))):
        raise ValueError('功能映射已变化，请刷新后重新提交')
    snapshots = candidates or [_route_candidate(binding)]
    if candidates:
        pricing = _mapping_channel(
            binding.get('pricing_id') or route_order[0], contract, connection=connection)
        if int(pricing['version']) != int(binding.get('pricing_version') or 0):
            raise ValueError('渠道版本已变化，请刷新后重新提交')
    for snapshot in snapshots:
        try:
            cfg = _mapping_channel(
                snapshot.get('id'), contract, require_enabled=True, connection=connection)
        except ValueError:
            raise ValueError('渠道版本已变化，请刷新后重新提交') from None
        if any(cfg.get(key) != snapshot.get(key) for key in ROUTE_CANDIDATE_FIELDS):
            raise ValueError('渠道版本已变化，请刷新后重新提交')
    return True


@contextmanager
def acceptance_guard(payloads):
    """Keep channel state immutable until the corresponding job commit finishes."""
    if channel_store.enabled():
        with channel_store.acceptance_guard(payloads):
            yield
        return
    payloads = list(payloads)
    managed = [payload for payload in payloads
               if isinstance(payload, dict)
               and isinstance(payload.get('_channel_binding'), dict)
               and payload['_channel_binding'].get('operation_id')]
    if not managed:
        yield
        return
    with closing(db()) as c:
        _begin_write(c)
        try:
            for payload in managed:
                _confirm_acceptance(c, payload)
            yield
        finally:
            # This transaction is a lock-backed read snapshot. Releasing it only
            # after the job commit makes that commit the acceptance linearization.
            c.rollback()


def capture(kind, payload, preparation=False, invocation_source='web'):
    if channel_store.enabled():
        return channel_store.capture(kind, payload, preparation, invocation_source)
    clean = dict(payload)
    clean.pop('_channel_binding', None)  # Never trust a client supplied private snapshot.
    clean.pop('_channel_shadow', None)
    source = str(invocation_source or 'web').strip().lower()
    if source not in {'web', 'agent', 'admin_e2e', 'internal'}:
        source = 'internal'
    operation_id, mapping = routing_for_payload(kind, clean)
    if not mapping or mapping['state'] == 'legacy':
        return _legacy_capture(kind, clean, preparation)
    if mapping['state'] == 'paused':
        raise ValueError('该功能已由管理员暂停，未执行旧线路降级')
    cid = mapping.get('channel') or ''
    if mapping['state'] == 'shadow':
        try:
            from .function_registry import operation
            cfg = _mapping_channel(cid, operation(operation_id) or mapping['kind'])
            clean['_channel_shadow'] = _seal_shadow({
                'operation_id': operation_id, 'mapping_revision': mapping['revision'],
                'observation_id': uuid.uuid4().hex,
                'id': cfg['id'], 'version': cfg['version'], 'adapter': cfg['adapter'],
                'model': cfg['model'], 'invocation_source': source,
            })
        except ValueError as exc:
            clean['_channel_shadow'] = _seal_shadow({
                'operation_id': operation_id, 'mapping_revision': mapping['revision'],
                'observation_id': uuid.uuid4().hex,
                'id': cid, 'state': 'invalid', 'detail': str(exc)[:120],
                'invocation_source': source,
            })
        return _legacy_capture(kind, clean, preparation)
    from .function_registry import operation
    contract = operation(operation_id) or mapping['kind']
    route_candidates, route_skipped, pricing = [], [], None
    if kind == 'image':
        pricing, route_candidates, route_skipped = _managed_image_route(mapping, contract)
        cfg = version(route_candidates[0]['id'], route_candidates[0]['version'])
    else:
        cfg = _mapping_channel(cid, contract, require_enabled=True)
        pricing = cfg
    from .channel_parameters import apply
    clean, _ = apply(pricing, clean, required=not preparation)
    from .channel_runtime import validate_payload
    validate_payload(cfg, clean)
    clean['_channel_binding'] = {
        'operation_id': operation_id, 'mapping_revision': mapping['revision'],
        'id': cfg['id'], 'version': cfg['version'], 'adapter': cfg['adapter'],
        'model': cfg['model'], 'front': _front(kind, clean),
        'invocation_source': source,
    }
    if route_candidates:
        clean['_channel_binding'].update({
            'pricing_id': pricing['id'], 'pricing_version': pricing['version'],
            'route_order': mapping_channel_ids(mapping),
            'route_candidates': route_candidates, 'route_skipped': route_skipped,
            'route_attempt': 1, 'attempts': [],
        })
    return clean


def record_shadow(job_id, snapshot):
    """Persist a server-owned shadow observation without making a provider call."""
    if channel_store.enabled():
        return channel_store.record_shadow(job_id, snapshot)
    if (not _valid_shadow(snapshot) or not snapshot.get('operation_id')
            or not re.fullmatch(r'[0-9a-f]{32}', str(snapshot.get('observation_id') or ''))):
        return None
    public_snapshot = {key: snapshot.get(key) for key in (
        'operation_id', 'mapping_revision', 'observation_id', 'id', 'version', 'adapter',
        'model', 'invocation_source', 'state', 'detail',
    ) if snapshot.get(key) not in (None, '')}
    now = time.time()
    with closing(db()) as c:
        _begin_write(c)
        rid = public_snapshot['observation_id']
        existing = c.execute(
            "SELECT r.job_id,s.snapshot FROM runs r JOIN run_snapshots s ON s.run_id=r.id "
            "WHERE r.id=? AND r.kind='shadow'", (rid,),
        ).fetchone()
        if existing:
            if (str(existing['job_id']) != str(job_id)
                    or json.loads(existing['snapshot']) != public_snapshot):
                raise ValueError('影子观察身份冲突，拒绝覆盖既有证据')
            c.commit()
            return rid
        c.execute(
            'INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            (rid, str(public_snapshot.get('id') or ''),
             int(public_snapshot.get('version') or 0), 'shadow', 'captured',
             now, now, 0, '候选渠道快照（未调用 Provider）', str(job_id), '', 0),
        )
        c.execute('INSERT INTO run_snapshots VALUES(?,?,?,?,?)', (
            rid, public_snapshot['operation_id'],
            public_snapshot.get('mapping_revision'),
            public_snapshot.get('invocation_source', 'web'),
            json.dumps(public_snapshot, ensure_ascii=False),
        ))
        c.commit()
    return rid


def rollback(actor, body):
    if channel_store.enabled():
        return channel_store.rollback(actor, body)
    old = version(str(body.get('id')), int(body.get('target_version')))
    secret = version(old['id'], old['version'], True)['secret']
    current = version(old['id'])
    return save(actor, dict(old, version=current['version'], secret=secret, enabled=body.get('enabled') is True))


def overview():
    if channel_store.enabled():
        return channel_store.overview()
    from .channel_lifecycle import legacy_states, LEGACY_SCOPES
    now = time.time()
    with closing(db()) as c:
        channels = [dict(r) for r in c.execute('SELECT * FROM channels')]
        mappings = [json.loads(r[0]) for r in c.execute('SELECT config FROM mappings')]
        operation_mappings = [operation_mapping(r[0]) for r in c.execute(
            'SELECT operation_id FROM operation_mappings ORDER BY operation_id')]
        for mapping in operation_mappings:
            mapping['history'] = [dict(row) for row in c.execute(
                'SELECT revision,state,actor,created FROM operation_mapping_versions '
                'WHERE operation_id=? ORDER BY revision DESC LIMIT 20',
                (mapping['operation_id'],))]
        runs = [dict(r) for r in c.execute(
            'SELECT r.*,s.operation_id,s.mapping_revision,s.invocation_source,s.snapshot '
            'FROM runs r LEFT JOIN run_snapshots s ON s.run_id=r.id '
            'ORDER BY r.started DESC LIMIT 100')]
        for run in runs:
            raw_snapshot = run.pop('snapshot', None)
            if raw_snapshot:
                run['execution_snapshot'] = json.loads(raw_snapshot)
        events = [dict(r) for r in c.execute('SELECT * FROM events ORDER BY created DESC LIMIT 30')]
        for channel in channels:
            cfg = version(channel['id'], channel['version'])
            channel.update(cfg)
            channel['configured'] = True
            # 判定规则随每条渠道一起下发：前端不再靠缺字段回退成「要求三项」，
            # 否则一条不适用项的失败会把能用的渠道判成异常。
            adapter = channel.get('adapter')
            channel['verification'] = list(verification_required(adapter))
            channel['checks_supported'] = list(checks_supported(adapter))
            channel['rules_known'] = adapter in ADAPTERS
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
                check_row = c.execute("SELECT kind,state,updated,detail,version FROM runs WHERE channel=? AND version=? AND kind=? ORDER BY started DESC,rowid DESC LIMIT 1", (channel['id'],channel['version'],check_kind)).fetchone()
                if check_row:
                    channel['checks'].append(dict(check_row))
    from .function_registry import operation_catalog
    operations = operation_catalog(channel_eligible=True)
    # 全量功能目录（含不可切换的）：后台据此展示「为什么这个功能不能切」，
    # 而不是只给一个拖不动的手柄。不改变 operations 的既有语义。
    all_operations = operation_catalog()
    current = {item['operation_id']: item for item in operation_mappings}
    for item in operations:
        item['mapping'] = current.get(item['operation_id'])
    return {'items': channels, 'mappings': mappings, 'operation_mappings': operation_mappings,
            'operations': operations, 'all_operations': all_operations,
            'runs': runs, 'events': events, 'adapters': ADAPTERS,
            'legacy_controls':legacy_states(), 'legacy_scopes':LEGACY_SCOPES,
            'notifications': notification_settings(), 'timezone':'Asia/Shanghai', 'stats_window':'最近24小时'}


def run_status(rid):
    """按任务 ID 读一条 run 的当前状态（只读），行不存在返回 None。"""
    if channel_store.enabled():
        return channel_store.run_status(rid)
    with closing(db()) as c:
        row = c.execute('SELECT * FROM runs WHERE id=?', (str(rid),)).fetchone()
    return dict(row) if row else None


def find_active(cid, version, kind, within=1800):
    """查找同渠道、同配置版本、同检测类型的进行中任务。

    用作提交去重：重复点击、或提交响应丢失后重试，都不该再创建一条新任务——
    完整生成是收费的，重复创建等于重复扣费。
    """
    # PG 模式下 SQLite 不再是权威：公开 API 必须自动分发，绝不回落到旧库。
    if channel_store.enabled():
        return channel_store.find_active(cid, version, kind, within)
    with closing(db()) as c:
        row = c.execute(
            "SELECT * FROM runs WHERE channel=? AND version=? AND kind=? "
            "AND state IN ('queued','running') AND started>? ORDER BY started DESC LIMIT 1",
            (cid, version, kind, time.time() - within),
        ).fetchone()
    return dict(row) if row else None


def reserve(cid, kind, job_id='', snapshot=None, execution_snapshot=None):
    if channel_store.enabled():
        return channel_store.reserve(cid, kind, job_id, snapshot, execution_snapshot)
    cfg = snapshot or version(cid)
    now, rid = time.time(), uuid.uuid4().hex
    with closing(db()) as c:
        _begin_write(c)
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
        binding = execution_snapshot if isinstance(execution_snapshot, dict) else {}
        if binding:
            public_snapshot = public_execution_snapshot(binding)
            c.execute('INSERT INTO run_snapshots VALUES(?,?,?,?,?)', (
                rid, public_snapshot.get('operation_id', ''),
                public_snapshot.get('mapping_revision'),
                public_snapshot.get('invocation_source', 'web'),
                json.dumps(public_snapshot, ensure_ascii=False),
            ))
        c.commit()
    return rid


def finish(rid, state, detail, provider_id=''):
    if channel_store.enabled():
        return channel_store.finish(rid, state, detail, provider_id)
    with closing(db()) as c:
        c.execute('UPDATE runs SET state=?,detail=?,updated=?,duration=?-started,provider_id=CASE WHEN ?!=\'\' THEN ? ELSE provider_id END WHERE id=?',
                  (state, detail[:300], time.time(), time.time(), provider_id, provider_id, rid))
        c.commit()


def finish_task_failover_safe(rid, detail):
    """Atomically mark a task attempt as failed and safe to move to the next route."""
    if channel_store.enabled():
        return channel_store.finish_task_failover_safe(rid, detail)
    now = time.time()
    with closing(db()) as c:
        _begin_write(c)
        row = c.execute(
            "SELECT r.kind,r.state,r.provider_id,s.snapshot FROM runs r "
            "JOIN run_snapshots s ON s.run_id=r.id WHERE r.id=?", (rid,),
        ).fetchone()
        if (not row or row['kind'] != 'task'
                or row['state'] not in {'queued', 'running'} or row['provider_id']):
            # queued 也在允许范围内：并发/限流排队超时的任务从未提交供应商，同样是「未受理」。
            raise ValueError('当前任务状态不允许自动切换渠道')
        snapshot = json.loads(row['snapshot'])
        snapshot['failover_safe'] = True
        snapshot['failure_reason'] = str(detail or '')[:300]
        c.execute(
            "UPDATE runs SET state='failed',detail=?,updated=?,duration=?-started WHERE id=?",
            (str(detail or '')[:300], now, now, rid),
        )
        c.execute('UPDATE run_snapshots SET snapshot=? WHERE run_id=?', (
            json.dumps(snapshot, ensure_ascii=False), rid))
        c.commit()


def prepare_task_failover(rid, candidate, reason=''):
    """Move one safely failed task run to a sealed next candidate without a new charge."""
    if channel_store.enabled():
        return channel_store.prepare_task_failover(rid, candidate, reason)
    candidate = _route_candidate(candidate if isinstance(candidate, dict) else {})
    if not candidate.get('id') or not candidate.get('version'):
        raise ValueError('候选渠道快照无效')
    now = time.time()
    with closing(db()) as c:
        _begin_write(c)
        row = c.execute(
            "SELECT r.*,s.snapshot FROM runs r JOIN run_snapshots s ON s.run_id=r.id "
            "WHERE r.id=?", (rid,),
        ).fetchone()
        if (not row or row['kind'] != 'task' or row['state'] != 'failed'
                or row['provider_id']):
            raise ValueError('当前任务状态不允许自动切换渠道')
        snapshot = json.loads(row['snapshot'])
        if not snapshot.get('failover_safe'):
            raise ValueError('上一渠道结果未确认，禁止自动切换')
        expected = next((item for item in snapshot.get('route_candidates', [])
                         if item.get('id') == candidate['id']), None)
        if not expected or any(expected.get(key) != candidate.get(key)
                               for key in ROUTE_CANDIDATE_FIELDS):
            raise ValueError('候选渠道不在任务受理快照中')
        attempts = list(snapshot.get('attempts') or [])
        attempts.append({
            'attempt': int(snapshot.get('route_attempt') or len(attempts) + 1),
            'channel': row['channel'], 'version': row['version'],
            'state': 'failed', 'detail': str(row['detail'] or '')[:300],
        })
        snapshot.update(candidate)
        snapshot['route_attempt'] = len(attempts) + 1
        snapshot['switch_reason'] = str(reason or snapshot.get('failure_reason') or '')[:300]
        snapshot['attempts'] = attempts[:MAX_MAPPING_CHANNELS]
        snapshot.pop('failover_safe', None)
        snapshot.pop('failure_reason', None)
        _ready_candidate(candidate['id'], c)   # 采集后被停用/回收/证据过期的候选不再接单
        cfg_row = c.execute(
            'SELECT config FROM versions WHERE channel=? AND version=?',
            (candidate['id'], candidate['version']),
        ).fetchone()
        if not cfg_row:
            raise ValueError('候选渠道版本不存在')
        cfg = json.loads(cfg_row['config'])
        pending = c.execute(
            "SELECT COUNT(*) FROM runs WHERE channel=? AND state='queued' AND id!=?",
            (candidate['id'], rid),
        ).fetchone()[0]
        if pending >= max(1, cfg['queue_limit']):
            raise ValueError('候选渠道等待队列已满')
        c.execute(
            "UPDATE runs SET channel=?,version=?,state='queued',detail=?,updated=?,"
            "duration=NULL,provider_id='' WHERE id=?",
            (candidate['id'], candidate['version'], '安全切换到下一渠道', now, rid),
        )
        c.execute('UPDATE run_snapshots SET snapshot=? WHERE run_id=?', (
            json.dumps(snapshot, ensure_ascii=False), rid))
        c.commit()
    return snapshot


def notification_settings(private=False):
    if channel_store.enabled():
        return channel_store.notification_settings(private)
    with closing(db()) as c:
        row = c.execute('SELECT value FROM settings WHERE id=1').fetchone()
    value = json.loads(row[0]) if row else {'enabled':False}
    if value.get('endpoint'):
        value['endpoint'] = _crypt(value['endpoint'], True) if private else '已配置（隐藏）'
    from . import runtime_observability
    value['delivery'] = runtime_observability.alert_counts('channel.%')
    return value


def task_evidence(job_id):
    if channel_store.enabled():
        return channel_store.task_evidence(job_id)
    with closing(db()) as c:
        row = c.execute(
            "SELECT r.id,r.kind,r.channel,r.version,r.state,r.provider_id,"
            "s.operation_id,s.mapping_revision,"
            "s.invocation_source,s.snapshot FROM runs r LEFT JOIN run_snapshots s ON s.run_id=r.id "
            "WHERE r.kind IN ('task','shadow') AND r.job_id=? "
            "ORDER BY CASE WHEN r.kind='task' THEN 0 ELSE 1 END,r.started DESC LIMIT 1",
            (str(job_id),),
        ).fetchone()
    if not row:
        return {}
    result = dict(row)
    if result.get('snapshot'):
        result['execution_snapshot'] = json.loads(result.pop('snapshot'))
    else:
        result.pop('snapshot', None)
    return result


def task_recovery_state(job_id):
    """Conservative paid-task recovery state; unreadable evidence is never failure."""
    if channel_store.enabled():
        return channel_store.task_recovery_state(job_id)
    try:
        evidence = task_evidence(job_id)
        return evidence.get('state') or 'absent'
    except (OSError, sqlite3.Error):
        return 'unavailable'


def mark_interrupted_task_unknown(job_id, detail):
    """Atomically preserve an interrupted provider submission for reconciliation."""
    if channel_store.enabled():
        return channel_store.mark_interrupted_task_unknown(job_id, detail)
    try:
        with closing(db()) as c:
            _begin_write(c)
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
    if channel_store.enabled():
        return channel_store.search_task_ids(query)
    needle = '%' + str(query or '').lower() + '%'
    if needle == '%%':
        return set()
    try:
        with closing(db()) as c:
            rows = c.execute(
                "SELECT DISTINCT r.job_id FROM runs r LEFT JOIN versions v "
                "ON v.channel=r.channel AND v.version=r.version "
                "LEFT JOIN run_snapshots s ON s.run_id=r.id "
                "WHERE r.kind IN ('task','shadow') AND (LOWER(r.provider_id) LIKE ? "
                "OR LOWER(r.channel) LIKE ? OR LOWER(CAST(r.version AS TEXT)) LIKE ? "
                "OR LOWER(COALESCE(v.config,'')) LIKE ? OR LOWER(COALESCE(s.operation_id,'')) LIKE ? "
                "OR LOWER(COALESCE(CAST(s.mapping_revision AS TEXT),'')) LIKE ?)",
                (needle, needle, needle, needle, needle, needle),
            ).fetchall()
        return {str(row[0]) for row in rows if row[0] not in (None, '')}
    except (OSError, sqlite3.Error):
        return set()


def save_notifications(actor, body):
    if channel_store.enabled():
        return channel_store.save_notifications(actor, body)
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
