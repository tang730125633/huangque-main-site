# -*- coding: utf-8 -*-
"""xAI 官方 Grok Imagine Video 异步协议适配。

创建视频是非幂等且可能立即计费：每个任务只选一次出境代理。仅当上游明确
返回可重试 HTTP 状态时原通道短退避重试；结果未知的网络失败绝不重发。
后续 GET 轮询可容忍瞬时网络错误，不会产生第二条付费视频。
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from . import egress, provider_keys


XAI_API_KEY = os.environ.get("XAI_API_KEY", "").strip()
XAI_API_BASE = os.environ.get("XAI_API_BASE", "https://api.x.ai/v1").rstrip("/")
XAI_VIDEO_TIMEOUT = int(os.environ.get("XAI_VIDEO_TIMEOUT", "1200") or 1200)
XAI_VIDEO_POLL_INTERVAL = int(os.environ.get("XAI_VIDEO_POLL_INTERVAL", "5") or 5)
TRANSIENT_HTTP_CODES = {408, 429, 500, 502, 503, 504}
TRANSIENT_BACKOFF = (5, 10, 20, 30)
CREATE_RETRY_HTTP_CODES = {429, 500, 502, 503, 504}
CREATE_RETRY_BACKOFF = (2, 5)


class TransientXaiError(RuntimeError):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


class XaiCreateUnavailableError(RuntimeError):
    """A definite pre-creation billing/auth failure that is safe to fall back from."""


class XaiCreateRejected(RuntimeError):
    """The paid create request was definitively rejected before acceptance."""


class XaiProviderFailed(RuntimeError):
    """The provider confirmed that an already-created task failed."""


class XaiCredentialError(RuntimeError):
    pass


def available():
    return provider_keys.has_candidate("xai")


def _opener():
    """提交前固定通道；有首选出境时使用探活结果，否则保留 urllib 的环境代理行为。"""
    proxy = egress.preferred_proxy()
    if proxy:
        return urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener()


def _redact(value, api_key=None):
    text = str(value or "")
    for secret in (api_key, XAI_API_KEY):
        if secret:
            text = text.replace(secret, "[已隐藏]")
    return text[:500]


def _error_detail(exc, api_key=None):
    try:
        raw = exc.read().decode("utf-8", "replace")[:1000]
        body = json.loads(raw)
        if isinstance(body, dict):
            return _redact(
                body.get("error") or body.get("message") or body.get("detail") or raw,
                api_key,
            )
        return _redact(raw, api_key)
    except Exception:
        return _redact(exc, api_key)


def _request_json(opener, method, path, body=None, timeout=90, api_key=None):
    api_key = XAI_API_KEY if api_key is None else str(api_key).strip()
    if not api_key:
        raise XaiCredentialError("xAI官方视频未配置（XAI_API_KEY）")
    url = path if str(path).startswith("http") else XAI_API_BASE + "/" + str(path).lstrip("/")
    headers = {
        "Authorization": "Bearer " + api_key,
        "Accept": "application/json",
        "User-Agent": "huangque-content/1.0",
    }
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with opener.open(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as exc:
        detail = _error_detail(exc, api_key)
        if exc.code in TRANSIENT_HTTP_CODES:
            raise TransientXaiError(
                "xAI视频临时不可用: HTTP %s %s" % (exc.code, detail),
                status_code=exc.code,
            )
        if exc.code in (401, 403):
            raise XaiCredentialError("xAI鉴权失败: HTTP %s %s" % (exc.code, detail))
        if exc.code == 402:
            raise XaiCredentialError("xAI账户余额不足: %s" % detail)
        raise RuntimeError("xAI视频接口失败: HTTP %s %s" % (exc.code, detail))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TransientXaiError("xAI视频网络异常: %s" % _redact(exc, api_key)[:300])


def _poll(opener, request_id, model, duration, job_id=None, heartbeat=None,
          now=None, sleep=None, api_key=None, provider_key_id=None):
    """轮询已创建的任务；这里只重试 GET，绝不重新提交付费任务。"""
    now = now or time.time
    sleep = sleep or time.sleep
    deadline = now() + XAI_VIDEO_TIMEOUT
    last_status = ""
    last_transient = None
    transient_attempt = 0
    while now() < deadline:
        try:
            result = _request_json(
                opener, "GET", "/videos/" + urllib.parse.quote(request_id),
                timeout=60, api_key=api_key,
            )
            last_transient = None
            transient_attempt = 0
        except TransientXaiError as exc:
            last_transient = exc
            if heartbeat:
                heartbeat(
                    job_id, "xai_retrying", provider_video_id=request_id,
                    provider_key_id=provider_key_id,
                    model=model, error=str(exc)[:300],
                )
            delay = TRANSIENT_BACKOFF[
                min(transient_attempt, len(TRANSIENT_BACKOFF) - 1)
            ]
            transient_attempt += 1
            if now() + delay >= deadline:
                break
            sleep(delay)
            continue
        status = str(result.get("status") or "").strip().lower()
        if status != last_status:
            print("[xai-video] request_id=%s model=%s status=%s" % (request_id, model, status), flush=True)
            last_status = status
        if heartbeat:
            heartbeat(
                job_id, "xai_" + (status or "pending"),
                provider_video_id=request_id, provider_key_id=provider_key_id,
                model=model, error="",
            )
        if status == "done":
            video = result.get("video") or {}
            url = str(video.get("url") or "").strip() if isinstance(video, dict) else ""
            if not url:
                raise RuntimeError("xAI视频已完成但未返回成片URL")
            return {"request_id": request_id, "model": str(result.get("model") or model),
                    "source_video_url": url, "duration": video.get("duration") or duration,
                    "respect_moderation": video.get("respect_moderation")}
        if status in {"failed", "expired"}:
            detail = result.get("error") or result.get("message") or status
            raise XaiProviderFailed(
                "xAI视频生成%s: %s"
                % ("过期" if status == "expired" else "失败", str(detail)[:500])
            )
        sleep(XAI_VIDEO_POLL_INTERVAL)
    if last_transient:
        raise TimeoutError("xAI视频查询超时: %s" % str(last_transient)[:200])
    raise TimeoutError("xAI视频生成超时")


def _create(opener, path, payload, sleep=None, api_key=None):
    """Retry only definite transient HTTP responses from a paid create call.

    Network failures remain single-shot because the provider may have accepted the
    request before the connection was lost; retrying those could double-charge.
    """
    sleep = sleep or time.sleep
    for attempt in range(len(CREATE_RETRY_BACKOFF) + 1):
        try:
            return _request_json(
                opener, "POST", path, payload, timeout=120, api_key=api_key
            )
        except TransientXaiError as exc:
            if (exc.status_code not in CREATE_RETRY_HTTP_CODES or
                    attempt >= len(CREATE_RETRY_BACKOFF)):
                raise
            sleep(CREATE_RETRY_BACKOFF[attempt])
        except XaiCredentialError:
            raise
        except RuntimeError as exc:
            raise XaiCreateRejected(str(exc)) from exc


def resume(request_id, model, duration, job_id=None, heartbeat=None, now=None,
           sleep=None, api_key=None, provider_key_id=None):
    if not str(request_id or "").strip():
        raise ValueError("恢复xAI视频缺少 request_id")
    return _poll(
        _opener(), str(request_id).strip(), model, duration,
        job_id=job_id, heartbeat=heartbeat, now=now, sleep=sleep,
        api_key=api_key, provider_key_id=provider_key_id,
    )


def generate(model, prompt, duration, aspect_ratio, resolution, image_url=None,
             job_id=None, heartbeat=None, now=None, sleep=None, api_key=None,
             provider_key_id=None):
    """创建 xAI 生成任务并轮询到终态。"""
    duration = int(duration)
    if model == "grok-imagine-video-1.5":
        if not str(image_url or "").strip():
            raise ValueError("Grok Video 1.5 仅支持1张首帧图")
        if duration < 1 or duration > 15:
            raise ValueError("Grok Video 1.5 视频时长必须是1-15秒整数")
    elif duration < 1 or duration > 15:
        raise ValueError("Grok Imagine Video 视频时长必须是1-15秒整数")
    opener = _opener()
    payload = {
        "model": model,
        "prompt": str(prompt or "").strip(),
        "duration": duration,
        "aspect_ratio": aspect_ratio,
        "resolution": resolution,
    }
    if image_url:
        payload["image"] = {"url": image_url}

    try:
        created = _create(
            opener, "/videos/generations", payload, sleep=sleep, api_key=api_key
        )
    except XaiCredentialError as exc:
        raise XaiCreateUnavailableError(str(exc)) from exc
    request_id = str(created.get("request_id") or "").strip()
    if not request_id:
        raise RuntimeError("xAI视频服务未返回 request_id")

    if heartbeat:
        heartbeat(
            job_id, "xai_pending", provider_video_id=request_id,
            provider_key_id=provider_key_id, model=model, error="",
        )
    result = _poll(
        opener, request_id, model, duration, job_id, heartbeat, now, sleep,
        api_key=api_key, provider_key_id=provider_key_id,
    )
    result["provider_key_id"] = provider_key_id
    return result


def edit(model, prompt, video_url, duration, job_id=None, heartbeat=None,
         now=None, sleep=None, api_key=None, provider_key_id=None):
    """创建一次 xAI 视频编辑任务；输出时长和比例由输入视频继承。"""
    opener = _opener()
    payload = {"model": model, "prompt": str(prompt or "").strip(), "video": {"url": video_url}}
    try:
        created = _create(
            opener, "/videos/edits", payload, sleep=sleep, api_key=api_key
        )
    except XaiCredentialError as exc:
        raise XaiCreateUnavailableError(str(exc)) from exc
    request_id = str(created.get("request_id") or "").strip()
    if not request_id:
        raise RuntimeError("xAI视频服务未返回 request_id")
    if heartbeat:
        heartbeat(
            job_id, "xai_pending", provider_video_id=request_id,
            provider_key_id=provider_key_id, model=model, error="",
        )
    result = _poll(
        opener, request_id, model, duration, job_id, heartbeat, now, sleep,
        api_key=api_key, provider_key_id=provider_key_id,
    )
    result["provider_key_id"] = provider_key_id
    return result
