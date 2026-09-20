"""Read-only operator view of frontend image products and their real routes.

The public workbench speaks in product/model names while the channel manager
speaks in provider and channel records.  This module owns that translation so
the admin browser never has to guess which supplier a frontend model uses.
"""
import time
import urllib.parse

from . import banana_provider
from .function_registry import AUDIO_FUNCTIONS, IMAGE_FUNCTIONS, VIDEO_FUNCTIONS
from .image_model_catalog import OPENAI_IMAGE_MODEL, SEEDREAM_MODELS, XIAOLE_IMAGE_MODEL


_MODEL_LABELS = {
    ('banana', 'nb2'): '纳米香蕉 2',
    ('banana', 'pro'): '纳米香蕉 Pro',
    ('openai', 'default'): 'GPT Image 2',
    ('seedream', 'std'): '标准版',
    ('seedream', 'pro'): 'Pro 版',
    ('xiaole', 'default'): 'GPT Image 2',
}
_ACTUAL_MODELS = {
    ('banana', 'nb2'): banana_provider.MODELS['nb2'],
    ('banana', 'pro'): banana_provider.MODELS['pro'],
    ('openai', 'default'): OPENAI_IMAGE_MODEL,
    ('xiaole', 'default'): XIAOLE_IMAGE_MODEL,
}
_ENTRY_KEYS = {'openai': 'gpt', 'banana': 'banana', 'seedream': 'seedream', 'xiaole': 'xiaole'}
_FEATURE_KEYS = {'openai': 'image', 'seedream': 'image', 'banana': 'banana', 'xiaole': 'image_xiaole'}
_RETIRED = {}
_OFFICIAL_HOSTS = {
    'api.openai.com', 'generativelanguage.googleapis.com',
    'ark.cn-beijing.volces.com', 'api.xiaolevideo.cn',
    'api.x.ai', 'api.heygen.com', 'mcp.heygen.com',
    'www.runninghub.cn', 'api.wavespeed.ai',
}
_VIDEO_ENTRY_KEYS = {
    'digital_ip': 'talking', 'cinematic': 'cinematic', 'tryon': 'tryon',
    'grok': 'grok', 'sora': 'sora', 'minimax': 'minimax',
    'omni': 'omni', 'seedance': 'micro',
}
_VIDEO_FEATURE_KEYS = {
    'digital_ip': 'video', 'cinematic': 'cinematic', 'tryon': 'tryon',
    'grok': 'grok_video', 'sora': 'sora_video',
    'minimax': 'minimax_h3_video', 'omni': 'omni_video',
    'seedance': 'seedance_video',
}
_VIDEO_LEGACY_HOSTS = {
    'heygen': 'api.heygen.com', 'runninghub': 'www.runninghub.cn',
    'wavespeed': 'api.wavespeed.ai',
}
# 音频页：配音（CosyVoice）。此前 AUDIO_FUNCTIONS 已在注册表里，但从未接进
# 前台功能矩阵，导致后台「音频与配音」页没有任何配音功能 —— 渠道建好了也
# 找不到切换入口。这里补齐，与视频页同一套结构。
_AUDIO_MODELS = {
    'tts': [
        {'key': 'public', 'label': '公共音色配音', 'model': 'CosyVoice 公共音色',
         'dependency': 'cosyvoice', 'operations': ['audio.tts.public']},
        {'key': 'personal', 'label': '个人音色配音', 'model': 'CosyVoice 复刻音色',
         'dependency': 'cosyvoice', 'operations': ['audio.tts.personal']},
    ],
}

_VIDEO_MODELS = {
    'digital_ip': [
        {'key': 'default', 'label': 'HeyGen 数字人口播',
         'model': 'heygen_mcp_subscription / HeyGen API', 'dependency': 'heygen'},
    ],
    'cinematic': [
        {'key': 'default', 'label': 'HeyGen 电影化身',
         'model': 'HeyGen Cinematic Avatar', 'dependency': 'heygen'},
    ],
    'tryon': [
        {'key': 'fast', 'label': '线路二 · 极速', 'model': 'WaveSpeed 换装',
         'dependency': 'wavespeed', 'operations': ['video.tryon.fast']},
        {'key': 'classic', 'label': '线路一 · 经典', 'model': 'RunningHub 换装工作流',
         'dependency': 'runninghub', 'operations': ['video.tryon.classic']},
    ],
    'grok': [
        {'key': 'standard', 'label': '通用版 · 文生/图生',
         'model': 'grok-imagine-video', 'dependency': 'xai', 'provider': 'xai'},
        {'key': '1.5', 'label': '1.5 · 高质量图生',
         'model': 'grok-imagine-video-1.5', 'dependency': 'xai', 'provider': 'xai',
         'operations': ['video.grok.image']},
    ],
    'sora': [
        {'key': 'standard', 'label': 'Sora 2 · 标准',
         'model': 'sora-2', 'dependency': 'openai', 'provider': 'sora'},
        {'key': 'pro', 'label': 'Sora 2 Pro · 高画质',
         'model': 'sora-2-pro', 'dependency': 'openai', 'provider': 'sora'},
    ],
    'minimax': [
        {'key': 'default', 'label': 'MiniMax H3 · 2K',
         'model': 'MiniMax-H3', 'dependency': 'minimax', 'provider': 'minimax'},
    ],
    'omni': [
        {'key': 'default', 'label': 'Omni 视频',
         'model': 'gemini-omni-flash-preview', 'dependency': 'gemini', 'provider': 'omni'},
    ],
    'seedance': [
        {'key': 'standard', 'label': 'Seedance 2.0 · 标准',
         'model': 'doubao-seedance-2-0-260128', 'dependency': 'seedance', 'provider': 'seedance'},
        {'key': 'fast', 'label': 'Seedance 2.0 · Fast（暂未开通）',
         'model': 'doubao-seedance-2-0-fast-260128', 'dependency': 'seedance',
         'provider': 'seedance', 'frontend_enabled': False},
    ],
}


def _host(value):
    text = str(value or '').strip()
    if not text:
        return ''
    if '://' not in text:
        return text.split('/', 1)[0]
    try:
        return urllib.parse.urlsplit(text).netloc
    except ValueError:
        return ''


def _transport(host, declared='unknown'):
    if declared in {'official', 'relay'}:
        return declared
    value = str(host or '').strip()
    try:
        hostname = urllib.parse.urlsplit(
            value if '://' in value else '//' + value
        ).hostname or ''
    except ValueError:
        hostname = ''
    return 'official' if hostname.lower() in _OFFICIAL_HOSTS else ('relay' if host else 'unknown')


def _check(check, now):
    if not check:
        return {'state': 'unverified', 'label': '未验证', 'checked_at': None}
    updated = int(check.get('updated') or check.get('checked_at') or 0)
    if not updated or now - updated > 86400:
        return {'state': 'stale', 'label': '证据已过期', 'checked_at': updated or None}
    state = str(check.get('state') or check.get('status') or '')
    labels = {
        'passed': '通过', 'auth_ok': '鉴权通过', 'failed': '失败',
        'credential_rejected': '凭据被拒绝', 'not_configured': '凭据未配置',
        'permission_denied': '权限或模型未开通', 'quota_or_plan': '额度或套餐不足',
        'throttled': '渠道限流', 'upstream_unavailable': '上游不可用',
        'network_error': '网络连接失败', 'reachable_only': '仅验证连通',
        'running': '检测中', 'queued': '排队中', 'unknown': '结果未知',
    }
    ok = state in {'passed', 'auth_ok'}
    return {'state': 'ok' if ok else ('pending' if state in {'running', 'queued'} else 'attention'),
            'label': labels.get(state, str(check.get('message') or '未验证')),
            'checked_at': updated or None}


def _managed_channel(cid, workspace, now):
    item = next((x for x in workspace.get('items', []) if x.get('id') == cid), None)
    if not item:
        return None
    checks = {x.get('kind'): x for x in item.get('checks', [])}
    host = _host(item.get('base_url'))
    return {
        'id': item.get('id'), 'name': item.get('name') or item.get('id'),
        'supplier': item.get('supplier') or '未标注供应商',
        'connection_type': _transport(host, item.get('connection_type')),
        'base_host': host, 'base_urls': [item.get('base_url')] if item.get('base_url') else [],
        'model': item.get('model') or '',
        'credential_source': '渠道密钥库', 'configured': bool(item.get('configured')),
        'enabled': bool(item.get('enabled')) and not bool((item.get('_lifecycle') or {}).get('deleted')),
        'auth': _check(checks.get('auth'), now), 'full': _check(checks.get('full'), now),
        'source': 'managed',
        'management': {'kind': 'managed_channel', 'uid': 'managed:' + str(item.get('id') or '')},
    }


def _legacy_channel(key, credentials, probes, controls, now, route='primary'):
    item = credentials.get(key) or {}
    host = item.get('image_' + route + '_base_host') or (
        item.get('env_base_host') or item.get('pool_base_host') or ''
    )
    base_url = item.get('image_' + route + '_base_url') or (
        item.get('env_base_url') or item.get('pool_base_url') or ''
    )
    probe = probes.get(key)
    probe_host = item.get('image_probe_base_host') or host
    route_probe = probe if not host or not probe_host or host == probe_host else None
    control = controls.get(key) or {}
    return {
        'id': 'legacy:' + key + ':' + route,
        'name': (item.get('name') or key) + (' · 兜底' if route == 'fallback' else ''),
        'supplier': item.get('name') or key,
        'connection_type': _transport(host), 'base_host': host,
        'base_urls': [base_url] if base_url else [],
        'model': item.get('model') or '',
        'credential_source': '服务器环境变量 · ' + ' / '.join(item.get('required_env') or []),
        'configured': bool(item.get('configured')),
        'enabled': item.get('image_accepts_new_jobs', item.get('accepts_new_jobs', True)) is not False
                   and control.get('enabled') is not False,
        'auth': _check(route_probe, now),
        'full': {'state': 'unverified', 'label': '未建立模型级成品证据', 'checked_at': None},
        'source': 'legacy',
        'management': {'kind': 'server_env', 'uid': 'legacy:' + str(key or '')},
    }


def _legacy_backup(key, credentials, probes, controls, now):
    """Expose the runtime fallback only when the official route is active."""
    item = credentials.get(key) or {}
    if item.get('image_primary_active') is False:
        return None
    primary_host = item.get('image_primary_base_host') or item.get('env_base_host') or ''
    fallback_host = item.get('image_fallback_base_host') or ''
    if not fallback_host or fallback_host == primary_host:
        return None
    return _legacy_channel(key, credentials, probes, controls, now, route='fallback')


def _video_environment_channel(key, credentials, probes, controls, now):
    item = credentials.get(key) or {}
    host = item.get('video_base_host') or item.get('env_base_host') or _VIDEO_LEGACY_HOSTS.get(key, '')
    base_url = item.get('video_base_url') or item.get('env_base_url') or ''
    control = controls.get(key) or {}
    channel = {
        'id': 'legacy:' + key + ':video', 'name': item.get('name') or key,
        'supplier': item.get('name') or key, 'connection_type': _transport(host),
        'base_host': host, 'base_urls': [base_url] if base_url else [],
        'model': item.get('video_model') or '',
        'credential_source': item.get('video_credential_source')
                             or '服务器环境变量 · ' + ' / '.join(item.get('required_env') or []),
        'configured': bool(item.get('video_configured', item.get('configured'))),
        'enabled': item.get('accepts_new_jobs', True) is not False
                   and control.get('enabled') is not False,
        'auth': _check((probes or {}).get(key), now),
        'full': {'state': 'unverified', 'label': '未建立模型级成品证据', 'checked_at': None},
        'source': 'legacy',
        'management': {'kind': 'server_env', 'uid': 'legacy:' + str(key or '')},
    }
    return channel


def _pool_channel(provider, key, credentials, pool_keys, controls, now):
    item = credentials.get(key) or {}
    keys = [x for x in (pool_keys or [])
            if x.get('provider') == provider and x.get('managed') is not False]
    active = [x for x in keys if x.get('state') == 'active']
    usable = [x for x in active if x.get('health_status') != 'unhealthy']
    base_urls = list(dict.fromkeys(str(x.get('base_url') or '').strip() for x in active
                                  if str(x.get('base_url') or '').strip()))
    hosts = list(dict.fromkeys(_host(value) for value in base_urls if _host(value)))
    transports = {_transport(host) for host in hosts}
    healthy = [x for x in usable if x.get('health_status') == 'healthy']
    if healthy:
        checked = max((int(x.get('last_checked_at') or 0) for x in healthy), default=0)
        auth = (_check({'status': 'auth_ok', 'checked_at': checked}, now) if checked else
                {'state': 'unverified', 'label': '号池密钥尚未鉴权通过', 'checked_at': None})
        if auth['state'] == 'ok':
            auth['label'] = '号池有鉴权通过的密钥'
    elif keys:
        checked = max((int(x.get('last_checked_at') or 0) for x in keys), default=0)
        auth = {'state': 'unverified', 'label': '号池密钥尚未鉴权通过', 'checked_at': checked or None}
    else:
        auth = {'state': 'unverified', 'label': '号池未配置', 'checked_at': None}
    control = controls.get(key) or {}
    return {
        'id': 'pool:' + provider, 'name': (item.get('name') or provider) + ' 号池',
        'supplier': item.get('name') or provider,
        'connection_type': next(iter(transports)) if len(transports) == 1 else 'unknown',
        'base_host': ' / '.join(hosts), 'base_urls': base_urls, 'model': '',
        'credential_source': '后台密钥号池 · %d 个密钥，%d 个可轮转' % (len(keys), len(usable)),
        'pool_size': len(keys), 'pool_usable': len(usable),
        'configured': bool(keys), 'enabled': bool(usable) and control.get('enabled') is not False,
        'auth': auth,
        'full': {'state': 'unverified', 'label': '未建立模型级成品证据', 'checked_at': None},
        'source': 'pool',
        'management': {'kind': 'provider_pool', 'uid': 'legacy:' + str(key or ''),
                       'provider': provider},
    }


def _tier(product_key, mode):
    match = mode.get('task_match') or {}
    return str(match.get('model') or match.get('variant') or 'default')


def _actual_model(product_key, tier):
    if product_key == 'seedream':
        return SEEDREAM_MODELS.get(tier) or ''
    return _ACTUAL_MODELS.get((product_key, tier), '')


def _capability_name(product_name, mode_name):
    prefix = product_name + ' · '
    text = str(mode_name or '')
    if text.startswith(prefix):
        text = text[len(prefix):]
    for marker in ('纳米香蕉 2 · ', '纳米香蕉 Pro · ', '标准 · ', 'Pro · '):
        if text.startswith(marker):
            return text[len(marker):]
    return text


def _route_for_mode(product, mode, workspace, credentials, probes, now):
    op_id = mode['key']
    mapping = next((x for x in workspace.get('operation_mappings', [])
                    if x.get('operation_id') == op_id), None)
    legacy_key = (product.get('dependencies') or [{}])[0].get('key')
    credential = credentials.get(legacy_key) or {}
    route = ('fallback' if credential.get('image_primary_active') is False
             and credential.get('image_fallback_base_host') else 'primary')
    legacy = _legacy_channel(legacy_key, credentials, probes,
                             workspace.get('legacy_controls') or {}, now, route=route)
    legacy['model'] = _actual_model(product['key'], _tier(product['key'], mode))
    state = str((mapping or {}).get('state') or 'legacy')
    primary = legacy
    backup = _legacy_backup(legacy_key, credentials, probes,
                            workspace.get('legacy_controls') or {}, now)
    if backup:
        backup['model'] = legacy['model']
    candidate = None
    if state == 'managed':
        primary = _managed_channel(mapping.get('channel'), workspace, now)
        backup = _managed_channel(mapping.get('backup'), workspace, now)
    elif state == 'shadow':
        candidate = _managed_channel(mapping.get('channel'), workspace, now)
    elif state == 'paused':
        primary = backup = None
    admitted = state != 'paused' and bool(primary and primary.get('enabled'))
    return {
        'operation_id': op_id, 'capability': _capability_name(product['name'], mode.get('name')),
        'control_state': state, 'primary': primary, 'backup': backup,
        # Retain original metadata for configuration and manual restoration,
        # without adding it to managed automatic failover.
        'original': legacy,
        'candidate': candidate, 'admitted': admitted,
        'reason': ('管理员已暂停该操作' if state == 'paused' else
                   '主渠道未启用或不存在' if not admitted else ''),
    }


def _model_rows(product, workspace, credentials, probes, feature_enabled, now):
    grouped = {}
    for mode in product.get('modes') or []:
        tier = _tier(product['key'], mode)
        grouped.setdefault(tier, []).append(
            _route_for_mode(product, mode, workspace, credentials, probes, now))
    rows = []
    for tier, routes in grouped.items():
        route_models = list(dict.fromkeys(
            route['primary'].get('model') for route in routes
            if route.get('primary') and route['primary'].get('model')
        ))
        actual = route_models[0] if len(route_models) == 1 else (
            '按能力分流：' + ' / '.join(route_models) if route_models else _actual_model(product['key'], tier)
        )
        retired_reason = _RETIRED.get(product['key'], '')
        admitted = feature_enabled and not retired_reason and any(x['admitted'] for x in routes)
        reasons = []
        if not feature_enabled:
            reasons.append('功能开关未开启')
        if retired_reason:
            reasons.append(retired_reason)
        reasons.extend(x['reason'] for x in routes if x['reason'])
        warnings = list(reasons)
        for route in routes:
            primary = route.get('primary')
            if not primary:
                continue
            if not primary.get('configured'):
                warnings.append(route['capability'] + '：凭据未配置')
            if primary.get('auth', {}).get('state') != 'ok':
                warnings.append(route['capability'] + '：' + primary.get('auth', {}).get('label', '鉴权未验证'))
            if primary.get('full', {}).get('state') != 'ok':
                warnings.append(route['capability'] + '：' + primary.get('full', {}).get('label', '成品未验证'))
            if not route.get('backup'):
                warnings.append(route['capability'] + '：未配置备用渠道')
        rows.append({
            'key': tier, 'label': _MODEL_LABELS.get((product['key'], tier), tier),
            'actual_model': actual, 'capabilities': [x['capability'] for x in routes],
            'routes': routes, 'admitted': admitted,
            'reason': '；'.join(dict.fromkeys(reasons)),
            'warnings': list(dict.fromkeys(warnings)),
            'attention': bool(warnings),
        })
    return rows


def _lechuang_product(workspace, layout_entry, feature_enabled, now):
    mappings = [x for x in workspace.get('mappings', []) if x.get('kind') == 'image']
    channels = {x.get('id'): x for x in workspace.get('items', [])}
    mappings = [x for x in mappings if channels.get(x.get('channel'), {}).get('adapter') == 'lechuang_image']
    published_models = set(layout_entry.get('model_keys') or [])
    mappings = [x for x in mappings
                if str(x.get('front') or '') in published_models]
    models = []
    for mapping in mappings:
        front = str(mapping.get('front') or '')
        primary = _managed_channel(mapping.get('channel'), workspace, now)
        backup = _managed_channel(mapping.get('backup'), workspace, now)
        admitted = bool(feature_enabled and mapping.get('enabled') and primary and primary.get('enabled'))
        models.append({
            'key': front, 'label': mapping.get('label') or front,
            'actual_model': (primary or {}).get('model') or front,
            'capabilities': ['文生图', '参考图生成', '局部修改'],
            'routes': [{'operation_id': 'image:' + front, 'capability': '全部生成方式',
                        'control_state': 'managed', 'primary': primary, 'backup': backup,
                        'candidate': None, 'admitted': admitted,
                        'reason': '' if admitted else '映射或主渠道未启用'}],
            'admitted': admitted, 'reason': '' if admitted else '映射或主渠道未启用',
            'warnings': ([] if feature_enabled else ['图片生成功能开关未开启'])
                        + ([] if admitted else ['映射或主渠道未启用'])
                        + ([] if primary and primary.get('auth', {}).get('state') == 'ok'
                           else ['鉴权未通过或未验证'])
                        + ([] if primary and primary.get('full', {}).get('state') == 'ok'
                           else ['成品未通过或未验证'])
                        + ([] if backup else ['未配置备用渠道']),
            'attention': not (admitted and primary and primary.get('auth', {}).get('state') == 'ok'
                              and primary.get('full', {}).get('state') == 'ok' and backup),
        })
    return {
        'key': 'lechuang', 'label': layout_entry.get('label') or '乐创 · 生图',
        'description': '由统一渠道管理器发布的多模型图片入口',
        'visible': bool(layout_entry.get('visible')), 'visibility_reason': layout_entry.get('reason') or '',
        'admitted': any(x['admitted'] for x in models), 'attention': any(x['attention'] for x in models),
        'models': models,
        'warning': '' if models else '尚无已发布的图片模型映射',
    }


def _summary(page, products):
    models = [model for product in products for model in product['models']]
    return {
        'page': page, 'products': len(products), 'models': len(models),
        'visible_products': sum(1 for x in products if x['visible']),
        'admitted_models': sum(1 for x in models if x['admitted']),
        'attention_models': sum(1 for x in models if x.get('attention')),
    }


def _image_matrix(workspace, credentials, probes, layout_state, features, now):
    image_entries = {x.get('key'): x for x in (layout_state.get('entries', {}).get('image') or [])}
    products = []
    for product in IMAGE_FUNCTIONS:
        entry = image_entries.get(_ENTRY_KEYS.get(product['key']), {})
        feature_key = _FEATURE_KEYS.get(product['key'])
        enabled = features.get(feature_key, True) if feature_key else True
        models = _model_rows(product, workspace, credentials, probes or {}, enabled, now)
        products.append({
            'key': product['key'], 'label': entry.get('label') or product['name'],
            'description': product.get('desc') or '',
            'visible': bool(entry.get('visible')), 'visibility_reason': entry.get('reason') or '',
            'admitted': any(x['admitted'] for x in models), 'models': models,
            'attention': any(x['attention'] for x in models),
            'warning': _RETIRED.get(product['key'], ''),
        })
    products.append(_lechuang_product(
        workspace, image_entries.get('lechuang', {}), features.get('image', True), now))
    zelong = image_entries.get('zelong2', {})
    products.append({
        'key': 'zelong2', 'label': zelong.get('label') or '泽龙2生图',
        'description': '历史专属站点图片入口', 'visible': bool(zelong.get('visible')),
        'visibility_reason': zelong.get('reason') or '', 'admitted': False,
        'attention': True, 'models': [], 'warning': '运行时维护中且主站隐藏，当前不可接单',
    })
    order = layout_state.get('layout', {}).get('image', {}).get('order') or []
    position = {key: index for index, key in enumerate(order)}
    entry_to_product = {'gpt': 'openai', 'banana': 'banana', 'seedream': 'seedream',
                        'lechuang': 'lechuang', 'xiaole': 'xiaole', 'zelong2': 'zelong2'}
    product_position = {entry_to_product.get(key, key): index for key, index in position.items()}
    products.sort(key=lambda x: product_position.get(x['key'], 999))
    return {
        'page': 'image', 'label': '图片生成', 'read_only': True,
        'summary': _summary('image', products),
        'products': products,
    }


def _video_route(operation_id, capability, model, dependency, provider,
                 workspace, credentials, probes, pool_keys, now):
    mapping = next((x for x in workspace.get('operation_mappings', [])
                    if x.get('operation_id') == operation_id), None)
    if provider:
        legacy = _pool_channel(provider, dependency, credentials, pool_keys,
                               workspace.get('legacy_controls') or {}, now)
    else:
        legacy = _video_environment_channel(
            dependency, credentials, probes, workspace.get('legacy_controls') or {}, now)
    legacy['model'] = legacy.get('model') or model
    state = str((mapping or {}).get('state') or 'legacy')
    primary, backup, candidate = legacy, None, None
    if state == 'managed':
        primary = _managed_channel(mapping.get('channel'), workspace, now)
        backup = _managed_channel(mapping.get('backup'), workspace, now)
    elif state == 'shadow':
        candidate = _managed_channel(mapping.get('channel'), workspace, now)
    elif state == 'paused':
        primary = None
    admitted = state != 'paused' and bool(
        primary and primary.get('enabled') and primary.get('configured')
    )
    return {
        'operation_id': operation_id, 'capability': capability,
        'control_state': state, 'primary': primary, 'backup': backup,
        # Retain original metadata for configuration and manual restoration,
        # without adding it to managed automatic failover.
        'original': legacy,
        'candidate': candidate, 'admitted': admitted,
        'reason': ('管理员已暂停该操作' if state == 'paused' else
                   '主渠道未配置、未启用或不存在' if not admitted else ''),
    }


def _video_model(product, spec, workspace, credentials, probes, pool_keys,
                 feature_enabled, now, unavailable_reason='功能开关未开启'):
    operations = set(spec.get('operations') or [])
    modes = [mode for mode in (product.get('modes') or [])
             if not operations or mode.get('key') in operations]
    routes = [
        _video_route(
            mode['key'], _capability_name(product['name'], mode.get('name')),
            spec['model'], spec['dependency'], spec.get('provider'),
            workspace, credentials, probes, pool_keys, now,
        )
        for mode in modes
    ]
    route_models = list(dict.fromkeys(
        route['primary'].get('model') for route in routes
        if route.get('primary') and route['primary'].get('model')
    ))
    actual_model = route_models[0] if len(route_models) == 1 else (
        '按能力分流：' + ' / '.join(route_models) if route_models else spec['model']
    )
    frontend_enabled = spec.get('frontend_enabled', True) is not False
    admitted = feature_enabled and frontend_enabled and any(x['admitted'] for x in routes)
    reasons = []
    if not feature_enabled:
        reasons.append(unavailable_reason)
    if not frontend_enabled:
        reasons.append('前台当前未开放该模型')
    reasons.extend(route['reason'] for route in routes if route['reason'])
    warnings = list(reasons)
    for route in routes:
        primary = route.get('primary')
        if not primary:
            continue
        if not primary.get('configured'):
            warnings.append(route['capability'] + '：凭据未配置')
        if primary.get('auth', {}).get('state') != 'ok':
            warnings.append(route['capability'] + '：' + primary.get('auth', {}).get('label', '鉴权未验证'))
        if primary.get('full', {}).get('state') != 'ok':
            warnings.append(route['capability'] + '：' + primary.get('full', {}).get('label', '成品未验证'))
    return {
        'key': spec['key'], 'label': spec['label'], 'actual_model': actual_model,
        'capabilities': [x['capability'] for x in routes], 'routes': routes,
        'admitted': admitted, 'reason': '；'.join(dict.fromkeys(reasons)),
        'warnings': list(dict.fromkeys(warnings)), 'attention': bool(warnings),
        'visible': frontend_enabled,
    }


def _video_matrix(workspace, credentials, probes, pool_keys, layout_state,
                  features, runtime_health, now):
    entries = {x.get('key'): x for x in (layout_state.get('entries', {}).get('video') or [])}
    products = []
    for product in VIDEO_FUNCTIONS:
        specs = _VIDEO_MODELS.get(product['key'])
        if not specs:
            continue
        entry = entries.get(_VIDEO_ENTRY_KEYS.get(product['key']), {})
        enabled = features.get(_VIDEO_FEATURE_KEYS.get(product['key']), True)
        health_key = (product.get('surface_visibility_key')
                      or product.get('acceptance_health_key'))
        runtime_available = (
            runtime_health.get(health_key) is True
            if health_key and runtime_health is not None else True
        )
        admission_enabled = enabled and runtime_available
        unavailable_reason = (
            '功能开关未开启' if not enabled else '前台运行时当前未开放'
        )
        models = [
            _video_model(product, spec, workspace, credentials, probes,
                         pool_keys, admission_enabled, now, unavailable_reason)
            for spec in specs
        ]
        # The four legacy tabs are always rendered by video.html; their feature
        # flags gate submission, not visibility.  Newer provider-backed tabs
        # expose a runtime health key that the browser itself uses to hide them.
        visible = bool(entry.get('visible')) and runtime_available
        products.append({
            'key': product['key'], 'label': entry.get('label') or product['name'],
            'description': product.get('desc') or '',
            'visible': visible,
            'visibility_reason': (unavailable_reason if not runtime_available
                                  else entry.get('reason') or ''),
            'admitted': any(x['admitted'] for x in models),
            'attention': any(x['attention'] for x in models),
            'models': models, 'warning': '',
        })
    order = layout_state.get('layout', {}).get('video', {}).get('order') or []
    position = {key: index for index, key in enumerate(order)}
    product_position = {
        product: position.get(entry, 999)
        for product, entry in _VIDEO_ENTRY_KEYS.items()
    }
    products.sort(key=lambda x: product_position.get(x['key'], 999))
    return {
        'page': 'video', 'label': '视频生成', 'read_only': True,
        'summary': _summary('video', products), 'products': products,
    }


def _audio_matrix(workspace, credentials, probes, layout_state, features, now):
    """音频页：目前只有「AI 配音」一个产品，其下按音色范围分公共/个人两条路由。"""
    entries = {x.get('key'): x for x in (layout_state.get('entries', {}).get('audio') or [])}
    products = []
    for product in AUDIO_FUNCTIONS:
        specs = _AUDIO_MODELS.get(product['key'])
        if not specs:
            continue
        entry = entries.get(product['key'], {})
        # 音频没有历史布局条目：没有条目时按可见处理，避免整页消失。
        enabled = features.get('audio', True)
        models = [
            _video_model(product, spec, workspace, credentials, probes, [],
                         enabled, now,
                         '功能开关未开启' if not enabled else '前台运行时当前未开放')
            for spec in specs
        ]
        products.append({
            'key': product['key'],
            'label': entry.get('label') or product['name'],
            'description': product.get('desc') or '',
            'visible': entry.get('visible', True),
            'visibility_reason': entry.get('reason') or '',
            'admitted': any(x['admitted'] for x in models),
            'attention': any(x['attention'] for x in models),
            'models': models, 'warning': '',
        })
    return {
        'page': 'audio', 'label': '音频与配音', 'read_only': True,
        'summary': _summary('audio', products), 'products': products,
    }


def build(workspace, key_rows, probes, layout_state, feature_rows, now=None,
          provider_key_rows=None, runtime_health=None):
    """Return one secret-free operator matrix spanning the image and video workbenches."""
    now = int(time.time() if now is None else now)
    credentials = {x.get('key'): x for x in key_rows or []}
    features = {x.get('key'): bool(x.get('enabled')) for x in feature_rows or []}
    image = _image_matrix(workspace, credentials, probes or {}, layout_state, features, now)
    video = _video_matrix(
        workspace, credentials, probes or {}, provider_key_rows or [],
        layout_state, features, runtime_health, now,
    )
    # Keep the original image fields at the top level for clients deployed before
    # the multi-page view, while the admin UI consumes the explicit pages list.
    audio = _audio_matrix(workspace, credentials, probes or {}, layout_state, features, now)
    result = dict(image)
    result['pages'] = [image, video, audio]
    return result
