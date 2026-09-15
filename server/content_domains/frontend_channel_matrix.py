"""Read-only operator view of frontend image products and their real routes.

The public workbench speaks in product/model names while the channel manager
speaks in provider and channel records.  This module owns that translation so
the admin browser never has to guess which supplier a frontend model uses.
"""
import time
import urllib.parse

from . import banana_provider
from .function_registry import IMAGE_FUNCTIONS
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
_RETIRED = {'xiaole': '运行时已下架，保留历史入口和配置供排查'}
_OFFICIAL_HOSTS = {
    'api.openai.com', 'generativelanguage.googleapis.com',
    'ark.cn-beijing.volces.com', 'api.xiaolevideo.cn',
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
    return 'official' if str(host).lower() in _OFFICIAL_HOSTS else ('relay' if host else 'unknown')


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
        'base_host': host, 'model': item.get('model') or '',
        'credential_source': '渠道密钥库', 'configured': bool(item.get('configured')),
        'enabled': bool(item.get('enabled')) and not bool((item.get('_lifecycle') or {}).get('deleted')),
        'auth': _check(checks.get('auth'), now), 'full': _check(checks.get('full'), now),
        'source': 'managed',
    }


def _legacy_channel(key, credentials, probes, controls, now):
    item = credentials.get(key) or {}
    host = item.get('env_base_host') or item.get('pool_base_host') or ''
    control = controls.get(key) or {}
    return {
        'id': 'legacy:' + key, 'name': item.get('name') or key,
        'supplier': item.get('name') or key,
        'connection_type': _transport(host), 'base_host': host,
        'model': item.get('model') or '',
        'credential_source': '服务器环境变量 · ' + ' / '.join(item.get('required_env') or []),
        'configured': bool(item.get('configured')),
        'enabled': item.get('accepts_new_jobs', True) is not False and control.get('enabled') is not False,
        'auth': _check(probes.get(key), now),
        'full': {'state': 'unverified', 'label': '未建立模型级成品证据', 'checked_at': None},
        'source': 'legacy',
    }


def _legacy_backup(key, credentials, probes, controls, now):
    """Expose an actual application fallback, not a network-egress hop."""
    if key != 'openai':
        return None
    item = credentials.get(key) or {}
    primary_host = item.get('env_base_host') or ''
    fallback_host = item.get('pool_base_host') or ''
    if not fallback_host or fallback_host == primary_host:
        return None
    fallback = _legacy_channel(key, credentials, probes, controls, now)
    fallback.update({
        'id': 'legacy:openai:fallback', 'name': 'OpenAI 图片兼容兜底',
        'base_host': fallback_host, 'connection_type': _transport(fallback_host),
    })
    return fallback


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
    legacy = _legacy_channel(legacy_key, credentials, probes,
                             workspace.get('legacy_controls') or {}, now)
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


def build(workspace, key_rows, probes, layout_state, feature_rows, now=None):
    """Return a secret-free matrix grouped exactly as the image workbench is grouped."""
    now = int(time.time() if now is None else now)
    credentials = {x.get('key'): x for x in key_rows or []}
    features = {x.get('key'): bool(x.get('enabled')) for x in feature_rows or []}
    image_entries = {x.get('key'): x for x in (layout_state.get('entries', {}).get('image') or [])}
    products = []
    for product in IMAGE_FUNCTIONS:
        entry = image_entries.get(_ENTRY_KEYS.get(product['key']), {})
        feature_key = _FEATURE_KEYS.get(product['key'])
        enabled = features.get(feature_key, True) if feature_key else True
        models = _model_rows(product, workspace, credentials, probes or {}, enabled, now)
        products.append({
            'key': product['key'], 'label': product['name'], 'description': product.get('desc') or '',
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
    models = [model for product in products for model in product['models']]
    return {
        'page': 'image', 'label': '图片生成', 'read_only': True,
        'summary': {'products': len(products), 'models': len(models),
                    'visible_products': sum(1 for x in products if x['visible']),
                    'admitted_models': sum(1 for x in models if x['admitted']),
                    'attention_models': sum(1 for x in models if x.get('attention'))},
        'products': products,
    }
