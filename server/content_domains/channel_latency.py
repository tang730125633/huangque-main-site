"""Read-only, key-free HEAD timing for registered admin channel identities."""
import socket
import time
from urllib.parse import urlsplit

from . import channel_manager, safe_http

SOURCES = ('env', 'pool', 'image_primary', 'image_fallback', 'video')


def _classify(status):
    """把 HTTP 状态码翻译成「已收到响应」事实 + 一句管理员能看懂的话。

    重点：404/401/403/405 都说明**地址确实回应了**，只是这条路不是业务接口、
    或没带凭据、或用错了方法。它们不是「渠道不可用」的证据，所以文案里
    明确说「已收到响应」，把判断权交给管理员。
    """
    if 200 <= status < 300:
        return 'ok', ''
    if status == 404:
        return 'not_found', '基础路径未找到'
    if status in (401, 403):
        return 'rejected', '无凭据探测被拒绝'
    if status == 405:
        return 'head_not_allowed', '不支持 HEAD 探测'
    if 300 <= status < 400:
        return 'redirect', '未跟随'
    if 500 <= status < 600:
        return 'server_error', '远端返回服务错误'
    return 'http_error', ''


def _headline(status):
    """这一行结果的开头：收到了响应 / 收到了重定向。"""
    if 300 <= status < 400:
        return '已收到重定向'
    return '已收到响应'


def measure(actor, body, legacy_loader):
    # Never accept a caller-provided URL, headers, credential or proxy.
    if not isinstance(body, dict) or set(body) - {'uid', 'source'}:
        raise ValueError('延迟检测仅接受已登记渠道 uid 和 source')
    uid = str(body.get('uid') or '')
    source = str(body.get('source') or '')
    if source and source not in SOURCES:
        raise ValueError('未知线路来源')
    result = dict(ok=True, uid=uid, source=source, state='unavailable',
                  network_reachable=False, latency_ms=None, http_status=None,
                  checked_at=int(time.time()), measurement='server_head',
                  authentication_tested=False, generation_tested=False,
                  timeout_seconds=10,
                  # 给管理员看的两项：reason 是稳定短码，message 是可直接展示的文案。
                  # 目的是把「地址已响应」「连不上」「没配地址」分清楚 ——
                  # 基础地址返回 404（如 /api/v1）只说明该路径没有业务接口，
                  # 不代表渠道不可用，不能因为一个数字让人误判。
                  reason='unavailable', message='未配置可检测地址')
    if uid.startswith('managed:') and uid[8:]:
        cfg = channel_manager.version(uid[8:])  # public config; never decrypt a key
        if cfg.get('_lifecycle', {}).get('deleted'):
            raise ValueError('渠道已移入回收站')
        url = cfg.get('base_url') or ''
        result.update(source='managed', version=cfg.get('version'))
    elif uid.startswith('legacy:') and uid[7:]:
        cfg = next((row for row in legacy_loader() if row.get('key') == uid[7:]), None)
        if not cfg:
            raise ValueError('渠道未登记')
        urls = {key: str(cfg.get(key + '_base_url') or '').strip() for key in SOURCES}
        urls = {key: value for key, value in urls.items() if value}
        if source:
            url = urls.get(source, '')
        elif len(set(urls.values())) == 1:
            source, url = next(iter(urls.items()))
            result['source'] = source
        elif len(set(urls.values())) > 1:
            result.update(detail='此渠道有多个地址，请选择具体线路来源', sources=list(urls))
            return result
        else:
            url = ''
    else:
        raise ValueError('渠道 uid 未登记')
    if not url:
        result['detail'] = '此线路未登记可检测的 Base URL'
        result.update(reason='no_address', message='未配置可检测地址')
        return result
    try:
        parsed = urlsplit(url)
        if parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError('地址包含不适合无凭据探测的字段')
    except (ValueError, TypeError):
        result['detail'] = '此线路地址不支持无凭据延迟检测'
        result.update(reason='bad_address', message='地址包含不适合探测的字段')
        return result
    started = time.monotonic()  # excludes configuration lookup and any job queue
    try:
        status = safe_http.request_bytes('HEAD', url, headers={}, timeout=10,
                                         max_bytes=0, head_status_only=True)
        reason, message = _classify(status)
        result.update(http_status=status, network_reachable=True,
                      state='reachable' if 200 <= status < 300 else 'http_error',
                      reason=reason, message=message,
                      detail='已收到 HTTP 响应；不代表鉴权或模型生成可用')
    except safe_http.SafeHttpError as exc:
        if exc.status:
            reason, message = _classify(exc.status)
            result.update(http_status=exc.status, network_reachable=True,
                          state='redirect_blocked' if 300 <= exc.status < 400 else 'http_error',
                          reason=reason, message=message,
                          detail='已收到 HTTP 响应；不跟随重定向，不代表模型可用')
        else:
            timed_out = isinstance(exc.__cause__, (TimeoutError, socket.timeout))
            result.update(state='timeout' if timed_out else 'network_error',
                          reason='timeout' if timed_out else 'unreachable',
                          message='10 秒内未完成' if timed_out else '地址解析失败／连接失败',
                          detail='网络请求超时' if timed_out else '网络连接未成功')
    except (ValueError, OSError):
        result.update(state='network_error', reason='unreachable',
                      message='地址解析失败／连接失败',
                      detail='地址解析或网络连接未成功')
    result['latency_ms'] = round((time.monotonic() - started) * 1000)
    result['checked_at'] = int(time.time())
    return result
