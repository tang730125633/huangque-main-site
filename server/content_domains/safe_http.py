"""Pinned, redirect-free outbound HTTP for managed providers and webhooks."""
import http.client
import ipaddress
import json
import socket
import ssl
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit


class SafeHttpError(RuntimeError):
    def __init__(self, message, status=0):
        super().__init__(message)
        self.status = int(status or 0)


@dataclass(frozen=True)
class Target:
    scheme: str
    hostname: str
    port: int
    host_header: str
    request_target: str
    addresses: tuple


def _is_loopback_host(hostname):
    return str(hostname or '').lower() in {'127.0.0.1', 'localhost', '::1'}


def validate_target(url, *, proxy=False, resolver=socket.getaddrinfo):
    """Validate every DNS answer. Connections must use one returned address."""
    try:
        parsed = urlsplit(str(url or '').strip())
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    except (TypeError, ValueError) as exc:
        raise ValueError('地址格式无效') from exc
    allowed_schemes = {'http'} if proxy else {'http', 'https'}
    if (parsed.scheme not in allowed_schemes or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.fragment):
        raise ValueError('地址协议或凭据格式无效')
    local = _is_loopback_host(parsed.hostname)
    if local and parsed.scheme != 'http':
        raise ValueError('本机测试地址仅允许 HTTP')
    if parsed.scheme == 'http' and not (local or proxy):
        raise ValueError('非本机服务必须使用 HTTPS')
    try:
        answers = resolver(parsed.hostname, port, type=socket.SOCK_STREAM)
        addresses = []
        for answer in answers:
            if answer and len(answer) > 4 and answer[4]:
                address = ipaddress.ip_address(answer[4][0])
                if address not in addresses:
                    addresses.append(address)
    except (OSError, TypeError, ValueError) as exc:
        raise ValueError('地址无法安全解析') from exc
    if not addresses:
        raise ValueError('地址无法安全解析')
    if local:
        if any(not address.is_loopback for address in addresses):
            raise ValueError('本机地址解析结果不可信')
    elif any(not address.is_global for address in addresses):
        raise ValueError('地址指向非公网网络')
    host_header = parsed.hostname
    if ':' in host_header:
        host_header = '[' + host_header + ']'
    default_port = 443 if parsed.scheme == 'https' else 80
    if port != default_port:
        host_header += ':' + str(port)
    request_target = urlunsplit(('', '', parsed.path or '/', parsed.query, ''))
    return Target(parsed.scheme, parsed.hostname, port, host_header,
                  request_target, tuple(str(item) for item in addresses))


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, hostname, port, pinned_ip, timeout):
        super().__init__(hostname, port=port, timeout=timeout,
                         context=ssl.create_default_context())
        self.pinned_ip = pinned_ip

    def connect(self):
        raw = socket.create_connection((self.pinned_ip, self.port), self.timeout,
                                       self.source_address)
        try:
            if ipaddress.ip_address(raw.getpeername()[0]) != ipaddress.ip_address(self.pinned_ip):
                raise SafeHttpError('连接地址与已校验地址不一致')
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
            if ipaddress.ip_address(self.sock.getpeername()[0]) != ipaddress.ip_address(self.pinned_ip):
                raise SafeHttpError('TLS 对端与已校验地址不一致')
        except Exception:
            raw.close()
            raise


class PinnedProxyHTTPSConnection(http.client.HTTPSConnection):
    """Connect to one validated HTTP proxy and tunnel to one validated target IP."""
    def __init__(self, target, proxy, timeout):
        super().__init__(target.hostname, port=target.port, timeout=timeout,
                         context=ssl.create_default_context())
        self._proxy = proxy
        self.set_tunnel(target.addresses[0], target.port,
                        headers={'Host': target.host_header})

    def connect(self):
        raw = socket.create_connection((self._proxy.addresses[0], self._proxy.port),
                                       self.timeout, self.source_address)
        try:
            if ipaddress.ip_address(raw.getpeername()[0]) != ipaddress.ip_address(self._proxy.addresses[0]):
                raise SafeHttpError('代理连接地址与已校验地址不一致')
            self.sock = raw
            self._tunnel()
            self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)
        except Exception:
            raw.close()
            self.sock = None
            raise


def request_bytes(method, url, *, body=None, headers=None, timeout=60,
                  max_bytes=8 * 1024 * 1024, proxy='', resolver=socket.getaddrinfo,
                  connection_factory=None):
    target = validate_target(url, resolver=resolver)
    proxy_target = validate_target(proxy, proxy=True, resolver=resolver) if proxy else None
    if proxy_target:
        if target.scheme != 'https':
            raise ValueError('代理仅用于 HTTPS 供应商地址')
        connection = (connection_factory or PinnedProxyHTTPSConnection)(
            target, proxy_target, timeout)
    elif target.scheme == 'https':
        connection = (connection_factory or PinnedHTTPSConnection)(
            target.hostname, target.port, target.addresses[0], timeout)
    else:
        connection = (connection_factory or http.client.HTTPConnection)(
            target.addresses[0], target.port, timeout=timeout)
    request_headers = {str(k): str(v) for k, v in dict(headers or {}).items()
                       if str(k).lower() != 'host'}
    request_headers['Host'] = target.host_header
    request_headers.setdefault('Accept-Encoding', 'identity')
    response = None
    try:
        connection.request(str(method).upper(), target.request_target,
                           body=body, headers=request_headers)
        response = connection.getresponse()
        if 300 <= int(response.status) < 400:
            raise SafeHttpError('禁止跟随重定向响应', response.status)
        raw = response.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise SafeHttpError('响应超过大小限制', response.status)
        if not 200 <= int(response.status) < 300:
            raise SafeHttpError('HTTP %s' % response.status, response.status)
        return raw
    except SafeHttpError:
        raise
    except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
        raise SafeHttpError('网络请求失败') from exc
    finally:
        if response is not None:
            response.close()
        connection.close()


def request_json(method, url, *, body=None, headers=None, timeout=60,
                 max_bytes=8 * 1024 * 1024, proxy=''):
    encoded = None if body is None else json.dumps(body, ensure_ascii=False).encode('utf-8')
    request_headers = dict(headers or {})
    if body is not None:
        request_headers.setdefault('Content-Type', 'application/json')
    raw = request_bytes(method, url, body=encoded, headers=request_headers,
                        timeout=timeout, max_bytes=max_bytes, proxy=proxy)
    try:
        value = json.loads(raw or b'{}')
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SafeHttpError('供应商返回的 JSON 无效') from exc
    if not isinstance(value, dict):
        raise SafeHttpError('供应商返回的 JSON 必须是对象')
    return value
