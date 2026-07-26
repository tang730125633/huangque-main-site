# -*- coding: utf-8 -*-
"""OpenAI Sora asynchronous video adapter.

Creating a video can charge immediately.  The create POST is therefore sent
exactly once.  Once OpenAI returns an id, callers can persist it through the
heartbeat callback and safely resume polling without submitting another job.
"""
import json
import os
import pathlib
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

from . import provider_keys


OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_BASE = os.environ.get("OPENAI_BASE", "https://api.openai.com").rstrip("/")
OPENAI_VIDEO_TIMEOUT = int(os.environ.get("OPENAI_VIDEO_TIMEOUT", "1800") or 1800)
OPENAI_VIDEO_POLL_INTERVAL = int(os.environ.get("OPENAI_VIDEO_POLL_INTERVAL", "10") or 10)
OPENAI_VIDEO_MAX_BYTES = int(
    os.environ.get("OPENAI_VIDEO_MAX_BYTES", str(512 * 1024 * 1024))
    or (512 * 1024 * 1024)
)
TRANSIENT_BACKOFF = (5, 10, 20, 30)
DOWNLOAD_TRANSIENT_BACKOFF = (2, 5, 10)
_DOWNLOAD_CHUNK_BYTES = 1024 * 1024


class TransientOpenAIError(RuntimeError):
    """An OpenAI error for which retrying an idempotent GET is reasonable."""


class CreateOutcomeUnknown(TransientOpenAIError):
    """The create request may have been accepted, but no video id was received."""


class CreateRejected(RuntimeError):
    """OpenAI explicitly rejected the create request before accepting a job."""


class CredentialRejected(CreateRejected):
    """The selected key was definitively rejected before a job was accepted."""


class ProviderVideoFailed(RuntimeError):
    """OpenAI reported a terminal failure for an accepted video job."""


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Do not forward API credentials when a content URL redirects to a CDN."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        old = urllib.parse.urlsplit(req.full_url)
        new = urllib.parse.urlsplit(redirected.full_url)
        if (old.scheme.lower(), old.netloc.lower()) != (
                new.scheme.lower(), new.netloc.lower()):
            sensitive = {"authorization", "proxy-authorization"}
            for bucket in (redirected.headers, redirected.unredirected_hdrs):
                for name in list(bucket):
                    if name.lower() in sensitive:
                        bucket.pop(name, None)
        return redirected


def available():
    return provider_keys.has_candidate("sora")


def _api_base():
    base = str(OPENAI_BASE or "https://api.openai.com").strip().rstrip("/")
    if not base:
        raise ValueError("OpenAI 视频接口地址未配置（OPENAI_BASE）")
    if base.endswith("/v1"):
        return base
    return base + "/v1"


def _api_url(path):
    return _api_base() + "/" + str(path or "").lstrip("/")


def _opener():
    return urllib.request.build_opener(_SafeRedirectHandler())


def _payload_detail(payload):
    if not isinstance(payload, dict):
        return str(payload)[:500]
    detail = payload.get("error") or payload.get("message") or payload.get("detail")
    if isinstance(detail, dict):
        detail = detail.get("message") or detail.get("detail") or detail.get("code") or detail
    return str(detail or payload)[:500]


def _redact(value, api_key=None):
    text = str(value or "")
    for secret in (api_key, OPENAI_API_KEY):
        if secret:
            text = text.replace(secret, "[已隐藏]")
    return text[:500]


def _error_detail(exc, api_key=None):
    try:
        raw = exc.read().decode("utf-8", "replace")[:2000]
        return _redact(_payload_detail(json.loads(raw or "{}")), api_key)
    except Exception:
        return _redact(exc, api_key)


def _raise_http_error(exc, method, api_key=None):
    detail = _error_detail(exc, api_key)
    rejected = CreateRejected if method == "POST" else RuntimeError
    if exc.code in (401, 403):
        rejected = CredentialRejected if method == "POST" else RuntimeError
        raise rejected(
            "OpenAI 视频鉴权失败: HTTP %s %s" % (exc.code, detail)
        ) from exc
    if exc.code == 402:
        rejected = CredentialRejected if method == "POST" else RuntimeError
        raise rejected("OpenAI 视频账户余额不足: %s" % detail) from exc
    if exc.code == 429:
        if method == "POST":
            raise CredentialRejected(
                "OpenAI 视频请求被限流: HTTP 429 %s" % detail
            ) from exc
        raise TransientOpenAIError(
            "OpenAI 视频请求被限流: HTTP 429 %s" % detail,
        ) from exc
    if 500 <= exc.code <= 599 or exc.code == 408:
        raise TransientOpenAIError(
            "OpenAI 视频服务暂时不可用: HTTP %s %s" % (exc.code, detail),
        ) from exc
    raise rejected(
        "OpenAI 视频接口失败: HTTP %s %s" % (exc.code, detail)
    ) from exc


def _open(opener, request, timeout, api_key=None):
    try:
        return opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        _raise_http_error(exc, request.get_method(), api_key)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TransientOpenAIError(
            "OpenAI 视频网络异常: %s" % str(exc)[:300]
        ) from exc


def _request_json(opener, method, path, body=None, timeout=90, api_key=None):
    api_key = OPENAI_API_KEY if api_key is None else str(api_key).strip()
    if not api_key:
        raise ValueError("OpenAI 视频未配置（OPENAI_API_KEY）")
    request_headers = {
        "Authorization": "Bearer " + api_key,
        "Accept": "application/json",
        "User-Agent": "huangque-content/1.0",
    }
    data = None
    if body is not None:
        request_headers["Content-Type"] = "application/json"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        _api_url(path), data=data, headers=request_headers, method=method
    )
    response = _open(opener, request, timeout, api_key)
    with response:
        try:
            raw = response.read()
            parsed = json.loads(raw.decode("utf-8", "replace") or "{}")
        except (UnicodeError, ValueError) as exc:
            if method == "GET":
                raise TransientOpenAIError("OpenAI 视频查询返回了无效 JSON") from exc
            raise RuntimeError("OpenAI 视频接口返回了无效 JSON") from exc
    if not isinstance(parsed, dict):
        if method == "GET":
            raise TransientOpenAIError("OpenAI 视频查询返回格式异常")
        raise RuntimeError("OpenAI 视频接口返回格式异常")
    return parsed


def _required(value, label):
    value = str(value or "").strip()
    if not value:
        raise ValueError("OpenAI 视频缺少%s" % label)
    return value


def _result(payload, video_id, model, status, seconds, size):
    result = dict(payload or {})
    result.update(
        {
            "video_id": video_id,
            "model": str(result.get("model") or model),
            "status": status,
            "seconds": str(result.get("seconds") or seconds),
            "size": str(result.get("size") or size),
        }
    )
    return result


def _poll(opener, video_id, model, seconds, size, job_id=None, heartbeat=None,
          now=None, sleep=None, api_key=None, provider_key_id=None):
    """Poll an existing video id.  This path never submits a POST."""
    video_id = _required(video_id, " video_id")
    model = _required(model, " model")
    seconds = _required(seconds, " seconds")
    size = _required(size, " size")
    now = now or time.time
    sleep = sleep or time.sleep
    deadline = now() + OPENAI_VIDEO_TIMEOUT
    last_transient = None
    transient_attempt = 0

    while now() < deadline:
        try:
            payload = _request_json(
                opener,
                "GET",
                "/videos/" + urllib.parse.quote(video_id, safe=""),
                timeout=60,
                api_key=api_key,
            )
            last_transient = None
            transient_attempt = 0
        except TransientOpenAIError as exc:
            last_transient = exc
            if heartbeat:
                heartbeat(
                    job_id,
                    "sora_retrying",
                    provider_video_id=video_id,
                    provider_key_id=provider_key_id,
                    model=model,
                    error=str(exc)[:300],
                )
            delay = TRANSIENT_BACKOFF[
                min(transient_attempt, len(TRANSIENT_BACKOFF) - 1)
            ]
            transient_attempt += 1
            if now() + delay >= deadline:
                break
            sleep(delay)
            continue

        status = str(payload.get("status") or "").strip().lower()
        if heartbeat:
            heartbeat(
                job_id,
                "sora_" + (status or "unknown"),
                provider_video_id=video_id,
                provider_key_id=provider_key_id,
                model=str(payload.get("model") or model),
                error="",
            )
        if status == "completed":
            return _result(payload, video_id, model, status, seconds, size)
        if status in {"failed", "cancelled", "canceled", "expired"}:
            detail = _payload_detail(payload)
            raise ProviderVideoFailed("OpenAI 视频生成失败: %s" % detail)
        if status not in {"queued", "in_progress"}:
            raise RuntimeError("OpenAI 视频返回未知状态: %s" % (status or "empty"))
        sleep(OPENAI_VIDEO_POLL_INTERVAL)

    if last_transient:
        raise TransientOpenAIError("OpenAI 视频查询超时: %s" % str(last_transient)[:240])
    raise TransientOpenAIError("OpenAI 视频生成超时")


def generate(model, prompt, seconds, size, job_id=None, heartbeat=None,
             now=None, sleep=None, api_key=None, provider_key_id=None):
    """Submit exactly one Sora job, persist its id, then poll to completion."""
    model = _required(model, " model")
    prompt = _required(prompt, " prompt")
    seconds = _required(seconds, " seconds")
    size = _required(size, " size")
    opener = _opener()
    try:
        created = _request_json(
            opener,
            "POST",
            "/videos",
            {"model": model, "prompt": prompt, "seconds": seconds, "size": size},
            timeout=120,
            api_key=api_key,
        )
    except CreateRejected:
        raise
    except Exception as exc:
        raise CreateOutcomeUnknown("OpenAI 视频提交结果未知: %s" % str(exc)[:300]) from exc
    video_id = str(created.get("id") or "").strip()
    if not video_id:
        raise CreateOutcomeUnknown("OpenAI 视频提交结果未知：未返回 video id")

    # Persist before the first GET.  If the process exits later, resume() can
    # continue this paid job without issuing a second create request.
    if heartbeat:
        heartbeat(
            job_id,
            "sora_" + str(created.get("status") or "queued").strip().lower(),
            provider_video_id=video_id,
            provider_key_id=provider_key_id,
            model=str(created.get("model") or model),
            error="",
        )
    return _poll(
        opener,
        video_id,
        model,
        seconds,
        size,
        job_id=job_id,
        heartbeat=heartbeat,
        now=now,
        sleep=sleep,
        api_key=api_key,
        provider_key_id=provider_key_id,
    )


def resume(video_id, model, seconds, size, job_id=None, heartbeat=None,
           now=None, sleep=None, api_key=None, provider_key_id=None):
    """Resume a paid OpenAI video job using GET requests only."""
    return _poll(
        _opener(),
        video_id,
        model,
        seconds,
        size,
        job_id=job_id,
        heartbeat=heartbeat,
        now=now,
        sleep=sleep,
        api_key=api_key,
        provider_key_id=provider_key_id,
    )


def _content_length(headers):
    try:
        value = headers.get("Content-Length") if headers is not None else None
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _download_once(opener, request, destination, limit, api_key=None):
    """Run one authenticated GET attempt without touching a good destination."""
    response = _open(opener, request, timeout=300, api_key=api_key)
    temp_path = None
    try:
        with response:
            declared = _content_length(getattr(response, "headers", None))
            if declared is not None and declared > limit:
                raise ValueError(
                    "OpenAI 视频文件过大: %d bytes，限制 %d bytes" % (declared, limit)
                )

            destination.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_path = tempfile.mkstemp(
                prefix=".%s." % destination.name,
                suffix=".tmp",
                dir=str(destination.parent),
            )
            total = 0
            signature = bytearray()
            with os.fdopen(fd, "wb") as output:
                while True:
                    try:
                        chunk = response.read(_DOWNLOAD_CHUNK_BYTES)
                    except (urllib.error.URLError, TimeoutError, OSError) as exc:
                        raise TransientOpenAIError(
                            "OpenAI 视频下载网络异常: %s" % str(exc)[:300]
                        ) from exc
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > limit:
                        raise ValueError(
                            "OpenAI 视频文件超过限制: %d bytes" % limit
                        )
                    if len(signature) < 16:
                        signature.extend(chunk[:16 - len(signature)])
                    output.write(chunk)
                if len(signature) < 8 or bytes(signature[4:8]) != b"ftyp":
                    raise ValueError("OpenAI 视频下载结果不是有效 MP4（缺少 ftyp）")
                output.flush()
                os.fsync(output.fileno())

        os.replace(temp_path, str(destination))
        temp_path = None
        return str(destination)
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass


def download_content(video_id, destination, max_bytes=None, api_key=None):
    """Stream an authenticated MP4 download and atomically replace destination.

    The provider task is already complete, so retrying this bounded sequence of
    GET requests is safe.  Creation POSTs never occur on this code path.
    """
    api_key = OPENAI_API_KEY if api_key is None else str(api_key).strip()
    if not api_key:
        raise ValueError("OpenAI 视频未配置（OPENAI_API_KEY）")
    video_id = _required(video_id, " video_id")
    destination = pathlib.Path(destination)
    limit = OPENAI_VIDEO_MAX_BYTES if max_bytes is None else int(max_bytes)
    if limit <= 0:
        raise ValueError("max_bytes 必须大于 0")

    request = urllib.request.Request(
        _api_url("/videos/%s/content" % urllib.parse.quote(video_id, safe="")),
        headers={
            "Authorization": "Bearer " + api_key,
            "Accept": "video/mp4,application/octet-stream",
            "User-Agent": "huangque-content/1.0",
        },
        method="GET",
    )
    opener = _opener()
    for attempt in range(len(DOWNLOAD_TRANSIENT_BACKOFF) + 1):
        try:
            return _download_once(opener, request, destination, limit, api_key)
        except TransientOpenAIError as exc:
            if attempt >= len(DOWNLOAD_TRANSIENT_BACKOFF):
                raise TransientOpenAIError(
                    "OpenAI 视频下载重试耗尽（共 %d 次 GET）: %s"
                    % (attempt + 1, str(exc)[:300]),
                ) from exc
            time.sleep(DOWNLOAD_TRANSIENT_BACKOFF[attempt])
