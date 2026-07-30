"""Small stdlib-only HTTP helpers for billable PoC provider adapters."""

import base64
import hashlib
import http.client
import ipaddress
import json
import mimetypes
import os
import secrets
import socket
import ssl
from dataclasses import dataclass
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlsplit, urlunsplit


JSON_LIMIT_BYTES = 4 * 1024 * 1024
DOWNLOAD_LIMIT_BYTES = 512 * 1024 * 1024


class ProviderHttpError(RuntimeError):
    def __init__(self, status, code, message, payload=None):
        super().__init__(str(message or code or "provider HTTP request failed"))
        self.status = int(status or 0)
        self.code = str(code or "provider_http_error")
        self.payload = payload if isinstance(payload, dict) else {}


@dataclass(frozen=True)
class HttpJsonResponse:
    status: int
    headers: dict
    payload: dict


def _read_limited(response, limit):
    data = response.read(limit + 1)
    if len(data) > limit:
        raise ProviderHttpError(
            0,
            "provider_response_too_large",
            "provider response exceeded the configured size limit",
        )
    return data


def _json_payload(data):
    if not data:
        return {}
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderHttpError(
            0,
            "provider_invalid_json",
            "provider returned invalid JSON",
        ) from exc
    if not isinstance(value, dict):
        raise ProviderHttpError(
            0,
            "provider_invalid_json",
            "provider JSON response must be an object",
        )
    return value


def request_json(
    method,
    url,
    *,
    headers=None,
    json_body=None,
    body=None,
    content_type=None,
    timeout=60,
    opener=urlrequest.urlopen,
):
    if json_body is not None and body is not None:
        raise ValueError("json_body and body are mutually exclusive")
    request_headers = dict(headers or {})
    if json_body is not None:
        body = json.dumps(
            json_body,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        content_type = "application/json"
    if content_type:
        request_headers["Content-Type"] = content_type
    req = urlrequest.Request(
        str(url),
        data=body,
        headers=request_headers,
        method=str(method).upper(),
    )
    try:
        response = opener(req, timeout=timeout)
        with response:
            data = _read_limited(response, JSON_LIMIT_BYTES)
            return HttpJsonResponse(
                status=int(response.getcode() or 200),
                headers=dict(response.headers.items()),
                payload=_json_payload(data),
            )
    except urlerror.HTTPError as exc:
        try:
            payload = _json_payload(_read_limited(exc, JSON_LIMIT_BYTES))
        except ProviderHttpError:
            payload = {}
        message = (
            payload.get("message")
            or payload.get("error")
            or payload.get("detail")
            or f"provider HTTP {exc.code}"
        )
        if isinstance(message, dict):
            message = message.get("message") or "provider request failed"
        raise ProviderHttpError(
            exc.code,
            payload.get("errorCode")
            or payload.get("code")
            or f"provider_http_{exc.code}",
            message,
            payload,
        ) from exc
    except (OSError, urlerror.URLError) as exc:
        raise ProviderHttpError(
            0,
            "provider_network_error",
            "provider network request failed",
        ) from exc


def encode_multipart(fields, files):
    boundary = "----huangque-lipsync-" + secrets.token_hex(16)
    chunks = []
    for name, value in fields.items():
        chunks.extend((
            f"--{boundary}\r\n".encode("ascii"),
            (
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            ).encode("ascii"),
            str(value).encode("utf-8"),
            b"\r\n",
        ))
    for name, file_path in files.items():
        path = Path(file_path)
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        safe_name = path.name.replace('"', "_")
        chunks.extend((
            f"--{boundary}\r\n".encode("ascii"),
            (
                f'Content-Disposition: form-data; name="{name}"; '
                f'filename="{safe_name}"\r\n'
            ).encode("utf-8"),
            f"Content-Type: {mime}\r\n\r\n".encode("ascii"),
            path.read_bytes(),
            b"\r\n",
        ))
    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def file_data_uri(path, max_bytes):
    path = Path(path)
    size = path.stat().st_size
    if size > max_bytes:
        raise ValueError("input file is too large for an inline data URI")
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


@dataclass(frozen=True)
class ValidatedDownloadTarget:
    url: str
    hostname: str
    port: int
    host_header: str
    request_target: str
    addresses: tuple


def _validated_download_target(url, resolver=socket.getaddrinfo):
    try:
        parsed = urlsplit(str(url))
    except Exception as exc:
        raise ProviderHttpError(
            0,
            "provider_result_url_invalid",
            "provider result URL is invalid",
        ) from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ProviderHttpError(
            0,
            "provider_result_url_invalid",
            "provider result URL must use HTTPS",
        )
    try:
        port = parsed.port or 443
        resolved = resolver(
            parsed.hostname,
            port,
            type=socket.SOCK_STREAM,
        )
        resolved_ips = []
        for item in resolved:
            if not item or len(item) <= 4 or not item[4]:
                continue
            address = ipaddress.ip_address(item[4][0])
            if address not in resolved_ips:
                resolved_ips.append(address)
    except (OSError, TypeError, ValueError) as exc:
        raise ProviderHttpError(
            0,
            "provider_result_url_invalid",
            "provider result URL cannot be resolved safely",
        ) from exc
    if not resolved_ips or any(
        not address.is_global for address in resolved_ips
    ):
        raise ProviderHttpError(
            0,
            "provider_result_url_forbidden",
            "provider result URL resolves to a non-public address",
        )
    request_target = urlunsplit((
        "",
        "",
        parsed.path or "/",
        parsed.query,
        "",
    ))
    host_header = parsed.hostname
    if ":" in host_header:
        host_header = f"[{host_header}]"
    if port != 443:
        host_header = f"{host_header}:{port}"
    return ValidatedDownloadTarget(
        url=str(url),
        hostname=parsed.hostname,
        port=port,
        host_header=host_header,
        request_target=request_target,
        addresses=tuple(str(address) for address in resolved_ips),
    )


def _safe_download_url(url, resolver=socket.getaddrinfo):
    return _validated_download_target(url, resolver=resolver).url


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect to a validated IP while verifying TLS for the URL host."""

    def __init__(self, host, port, pinned_ip, timeout):
        super().__init__(
            host,
            port=port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
        self.pinned_ip = str(pinned_ip)

    def connect(self):
        sock = None
        try:
            sock = socket.create_connection(
                (self.pinned_ip, self.port),
                self.timeout,
                self.source_address,
            )
            connected_ip = ipaddress.ip_address(sock.getpeername()[0])
            if connected_ip != ipaddress.ip_address(self.pinned_ip):
                raise ProviderHttpError(
                    0,
                    "provider_result_peer_mismatch",
                    "provider result connection did not use the pinned IP",
                )
            self.sock = self._context.wrap_socket(
                sock,
                server_hostname=self.host,
            )
            connected_ip = ipaddress.ip_address(
                self.sock.getpeername()[0]
            )
            if connected_ip != ipaddress.ip_address(self.pinned_ip):
                raise ProviderHttpError(
                    0,
                    "provider_result_peer_mismatch",
                    "provider result TLS peer did not use the pinned IP",
                )
        except Exception:
            if self.sock is not None:
                self.sock.close()
                self.sock = None
            elif sock is not None:
                sock.close()
            raise


def download_file(
    url,
    destination,
    *,
    headers=None,
    timeout=180,
    max_bytes=DOWNLOAD_LIMIT_BYTES,
    resolver=socket.getaddrinfo,
    connection_factory=PinnedHTTPSConnection,
):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".download")
    target = _validated_download_target(url, resolver=resolver)
    request_headers = {
        key: value
        for key, value in dict(headers or {}).items()
        if str(key).lower() != "host"
    }
    request_headers["Host"] = target.host_header
    request_headers.setdefault("Accept-Encoding", "identity")
    connection = connection_factory(
        target.hostname,
        target.port,
        target.addresses[0],
        timeout,
    )
    digest = hashlib.sha256()
    size = 0
    response = None
    try:
        try:
            connection.request(
                "GET",
                target.request_target,
                headers=request_headers,
            )
            response = connection.getresponse()
        except ProviderHttpError:
            raise
        except (
            OSError,
            ssl.SSLError,
            http.client.HTTPException,
        ) as exc:
            raise ProviderHttpError(
                0,
                "provider_download_network_error",
                "provider result download failed",
            ) from exc
        if 300 <= int(response.status) < 400:
            raise ProviderHttpError(
                int(response.status),
                "provider_result_redirect_forbidden",
                "provider result download redirects are not allowed",
            )
        if int(response.status) < 200 or int(response.status) >= 300:
            raise ProviderHttpError(
                int(response.status),
                f"provider_download_http_{response.status}",
                "provider result download failed",
            )
        with temporary.open("wb") as handle:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise ProviderHttpError(
                        0,
                        "provider_result_too_large",
                        "provider result exceeded the download limit",
                    )
                digest.update(chunk)
                handle.write(chunk)
        if size == 0:
            raise ProviderHttpError(
                0,
                "provider_result_empty",
                "provider result download was empty",
            )
        os.replace(temporary, destination)
        return {
            "size_bytes": size,
            "sha256": digest.hexdigest(),
        }
    finally:
        if response is not None:
            response.close()
        connection.close()
        if temporary.exists():
            temporary.unlink()
