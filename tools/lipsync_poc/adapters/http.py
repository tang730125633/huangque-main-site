"""Small stdlib-only HTTP helpers for billable PoC provider adapters."""

import base64
import hashlib
import json
import mimetypes
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlsplit


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


def _safe_download_url(url):
    try:
        parsed = urlsplit(str(url))
    except Exception as exc:
        raise ProviderHttpError(
            0,
            "provider_result_url_invalid",
            "provider result URL is invalid",
        ) from exc
    if parsed.scheme != "https" or not parsed.hostname:
        raise ProviderHttpError(
            0,
            "provider_result_url_invalid",
            "provider result URL must use HTTPS",
        )
    return str(url)


def download_file(
    url,
    destination,
    *,
    headers=None,
    timeout=180,
    max_bytes=DOWNLOAD_LIMIT_BYTES,
    opener=urlrequest.urlopen,
):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".download")
    req = urlrequest.Request(
        _safe_download_url(url),
        headers=dict(headers or {}),
        method="GET",
    )
    digest = hashlib.sha256()
    size = 0
    try:
        try:
            response = opener(req, timeout=timeout)
        except urlerror.HTTPError as exc:
            raise ProviderHttpError(
                exc.code,
                f"provider_download_http_{exc.code}",
                "provider result download failed",
            ) from exc
        except (OSError, urlerror.URLError) as exc:
            raise ProviderHttpError(
                0,
                "provider_download_network_error",
                "provider result download failed",
            ) from exc
        with response, temporary.open("wb") as handle:
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
        if temporary.exists():
            temporary.unlink()
