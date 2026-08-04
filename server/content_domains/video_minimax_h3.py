# -*- coding: utf-8 -*-
"""MiniMax 中国区 H3 官方异步视频适配器。"""
import base64
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from . import provider_keys


MODEL = "MiniMax-H3"
API_BASE = os.environ.get("MINIMAX_API_BASE", "https://api.minimaxi.com").rstrip("/")
API_KEY = os.environ.get("MINIMAX_API_KEY", "").strip()
TIMEOUT = max(120, int(os.environ.get("MINIMAX_H3_TIMEOUT", "1800") or 1800))
POLL_INTERVAL = max(5, int(os.environ.get("MINIMAX_H3_POLL_INTERVAL", "10") or 10))
RATIOS = {"21:9", "16:9", "4:3", "1:1", "3:4", "9:16", "adaptive"}
IMAGE_MIMES = {"image/jpeg", "image/jpg", "image/png", "image/webp"}
MAX_REFERENCE_IMAGES = 5  # 官方前 5 张免费；产品价格据此保持 20% 成本加价。
MAX_IMAGE_BYTES = 30 * 1024 * 1024
TRANSIENT_CODES = {408, 429} | set(range(500, 600))


class CreateOutcomeUnknown(RuntimeError):
    """创建请求可能已被接受；不得自动重发。"""


class MiniMaxRejected(RuntimeError):
    """创建请求被明确拒绝，没有可恢复的 task id。"""


class MiniMaxCredentialRejected(MiniMaxRejected):
    """当前密钥被明确拒绝，可安全切换下一条线路。"""


class MiniMaxProviderFailed(RuntimeError):
    """已取得 task id，但上游返回失败终态。"""


class TransientMiniMaxError(RuntimeError):
    """已知 task id 的幂等查询暂时失败。"""


def available():
    return provider_keys.has_candidate("minimax")


def _opener():
    # 中国区接口直连，避免继承进程级海外代理。
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _safe(value, api_key=None, limit=500):
    text = str(value or "")
    for secret in (api_key, API_KEY):
        if secret:
            text = text.replace(secret, "[已隐藏]")
    return text[:limit]


def _error_detail(exc, api_key=None):
    try:
        raw = exc.read().decode("utf-8", "replace")[:2000]
        payload = json.loads(raw or "{}")
        return _safe(payload.get("base_resp") or payload.get("error") or payload, api_key)
    except Exception:
        return _safe(exc, api_key)


def _human_error(code, detail):
    low = str(detail or "").lower()
    if code in {401, 403}:
        return "麦克视频鉴权失败，请联系管理员检查通道配置"
    if code == 402 or any(x in low for x in ("balance", "insufficient", "余额", "欠费")):
        return "麦克视频通道余额不足，请联系管理员"
    if any(x in low for x in ("moderation", "sensitive", "risk", "审核", "敏感")):
        return "麦克视频内容未通过安全审核，请调整提示词或参考图"
    if code == 429:
        return "麦克视频当前并发繁忙，请稍后重试"
    return "麦克视频接口失败：HTTP %s %s" % (code, detail)


def _request_json(opener, method, path, body=None, timeout=90, api_key=None):
    api_key = API_KEY if api_key is None else str(api_key).strip()
    if not api_key:
        raise ValueError("麦克视频服务未配置")
    headers = {
        "Authorization": "Bearer " + api_key,
        "Accept": "application/json",
        "User-Agent": "huangque-content/1.0",
    }
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        API_BASE + "/" + path.lstrip("/"), data=data, headers=headers, method=method
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = _error_detail(exc, api_key)
        if method == "POST" and (exc.code == 408 or 500 <= exc.code <= 599):
            raise CreateOutcomeUnknown(
                "麦克视频提交结果未知，请勿重复提交：HTTP %s %s" % (exc.code, detail)
            ) from exc
        if method == "GET" and exc.code in TRANSIENT_CODES:
            raise TransientMiniMaxError(_human_error(exc.code, detail)) from exc
        rejected = MiniMaxCredentialRejected if exc.code in {401, 403} else MiniMaxRejected
        raise rejected(_human_error(exc.code, detail)) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        message = "麦克视频网络异常：" + _safe(exc, api_key, 300)
        if method == "POST":
            raise CreateOutcomeUnknown(message + "；提交结果未知，已禁止自动重发") from exc
        raise TransientMiniMaxError(message) from exc
    try:
        payload = json.loads(raw.decode("utf-8", "replace") or "{}")
    except (UnicodeError, ValueError) as exc:
        if method == "POST":
            raise CreateOutcomeUnknown("麦克视频提交返回无法确认，已禁止自动重发") from exc
        raise TransientMiniMaxError("麦克视频查询返回格式异常") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("麦克视频返回格式异常")
    return payload


def _image_item(value):
    value = str(value or "").strip()
    match = re.fullmatch(
        r"data:(image/(?:jpeg|jpg|png|webp));base64,([A-Za-z0-9+/=\s]+)",
        value,
        re.IGNORECASE,
    )
    if match:
        try:
            raw = base64.b64decode(re.sub(r"\s+", "", match.group(2)), validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("麦克视频参考图数据无效") from exc
        if not raw or len(raw) > MAX_IMAGE_BYTES:
            raise ValueError("麦克视频单张参考图必须小于 30MB")
    else:
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("麦克视频参考图必须是图片数据或公网 URL")
    return {
        "type": "image_url",
        "image_url": {"url": value},
        "role": "reference_image",
    }


def build_request(prompt, reference_images, ratio="9:16", duration=5, resolution="768P"):
    prompt = str(prompt or "").strip()
    if not prompt or len(prompt) > 7000:
        raise ValueError("麦克视频提示词必须为 1～7000 个字符")
    refs = list(reference_images or [])
    if not 1 <= len(refs) <= MAX_REFERENCE_IMAGES:
        raise ValueError("麦克视频需要 1～5 张人物参考图")
    if isinstance(duration, bool):
        raise ValueError("麦克视频时长必须为 4～15 秒整数")
    try:
        duration = int(duration)
    except (TypeError, ValueError) as exc:
        raise ValueError("麦克视频时长必须为 4～15 秒整数") from exc
    if duration < 4 or duration > 15:
        raise ValueError("麦克视频时长必须为 4～15 秒整数")
    ratio = str(ratio or "").strip()
    if ratio not in RATIOS:
        raise ValueError("麦克视频不支持该画面比例")
    if str(resolution or "").strip().upper() != "768P":
        raise ValueError("麦克视频当前固定使用 768P")
    return {
        "model": MODEL,
        "content": [{"type": "text", "text": prompt}] + [_image_item(x) for x in refs],
        "duration": duration,
        "resolution": "768P",
        "ratio": ratio,
    }


def _poll(opener, task_id, duration, ratio, job_id=None, heartbeat=None,
          now=None, sleep=None, api_key=None, provider_key_id=None):
    now, sleep = now or time.time, sleep or time.sleep
    deadline = now() + TIMEOUT
    last_error = None
    while now() < deadline:
        try:
            payload = _request_json(
                opener, "GET", "/v2/query/video_generation/" +
                urllib.parse.quote(str(task_id), safe=""), timeout=60, api_key=api_key
            )
            last_error = None
        except TransientMiniMaxError as exc:
            last_error = exc
            if heartbeat:
                heartbeat(job_id, "minimax_retrying", provider_video_id=task_id,
                          provider_key_id=provider_key_id, model=MODEL,
                          error=_safe(exc, api_key=api_key, limit=300))
            sleep(POLL_INTERVAL)
            continue
        task = payload.get("task") or {}
        status = str(task.get("status") or "").strip().lower()
        if heartbeat:
            heartbeat(job_id, "minimax_" + (status or "unknown"),
                      provider_video_id=task_id, provider_key_id=provider_key_id,
                      model=MODEL, error="")
        if status == "succeeded":
            content = task.get("content") or {}
            url = str(content.get("url") or "").strip() if isinstance(content, dict) else ""
            if not url:
                raise MiniMaxProviderFailed("麦克视频已完成但没有返回成片地址")
            return {
                "request_id": task_id,
                "source_video_url": url,
                "model": MODEL,
                "duration": task.get("duration") or duration,
                "ratio": task.get("ratio") or ratio,
                "resolution": "768p",
                "provider": "minimax_h3_cn",
            }
        if status in {"failed", "cancelled", "canceled"}:
            raise MiniMaxProviderFailed("麦克视频生成失败：" + _safe(task.get("error") or payload, api_key=api_key))
        if status not in {"preparing", "queueing", "queued", "processing", "running"}:
            raise MiniMaxProviderFailed("麦克视频返回未知状态：" + (status or "空"))
        sleep(POLL_INTERVAL)
    if last_error:
        raise TimeoutError("麦克视频查询超时：" + _safe(last_error, api_key=api_key, limit=240))
    raise TimeoutError("麦克视频生成超时")


def generate(prompt, reference_images, ratio="9:16", duration=5, resolution="768P",
             job_id=None, heartbeat=None, now=None, sleep=None, api_key=None,
             provider_key_id=None):
    body = build_request(prompt, reference_images, ratio, duration, resolution)
    opener = _opener()
    created = _request_json(opener, "POST", "/v2/video_generation", body,
                            timeout=120, api_key=api_key)
    task_id = str(created.get("task_id") or "").strip()
    if not task_id:
        raise CreateOutcomeUnknown("麦克视频提交结果未知：未返回任务编号")
    if heartbeat:
        heartbeat(job_id, "minimax_queued", provider_video_id=task_id,
                  provider_key_id=provider_key_id, model=MODEL, error="")
    return _poll(opener, task_id, body["duration"], body["ratio"], job_id,
                 heartbeat, now, sleep, api_key, provider_key_id)


def resume(task_id, duration=5, ratio="9:16", job_id=None, heartbeat=None,
           now=None, sleep=None, api_key=None, provider_key_id=None):
    task_id = str(task_id or "").strip()
    if not task_id:
        raise ValueError("恢复麦克视频缺少任务编号")
    return _poll(_opener(), task_id, int(duration), ratio, job_id, heartbeat,
                 now, sleep, api_key, provider_key_id)
