# -*- coding: utf-8 -*-
"""Topview Seedance 2.5 async adapter for the existing Huangque video job."""
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from . import provider_keys


TOPVIEW_BASE = os.environ.get("TOPVIEW_API_BASE", "https://api.topview.ai").rstrip("/")
TOPVIEW_UID = os.environ.get("TOPVIEW_UID", "").strip()
SEEDANCE_MODEL = "doubao-seedance-2-0-260128"
SEEDANCE_FAST_MODEL = "doubao-seedance-2-0-fast-260128"
MODEL_NAMES = {SEEDANCE_MODEL: "seedance-2.5"}
RATIOS = {"21:9", "16:9", "4:3", "1:1", "3:4", "9:16", "adaptive"}
RESOLUTIONS = {"480p", "720p", "1080p"}
TIMEOUT = int(os.environ.get("TOPVIEW_VIDEO_TIMEOUT", "1200") or 1200)
POLL_INTERVAL = int(os.environ.get("TOPVIEW_VIDEO_POLL_INTERVAL", "10") or 10)
MAX_IMAGE_BYTES = 30 * 1024 * 1024


class CreateOutcomeUnknown(RuntimeError):
    """The paid submit may have succeeded; callers must not submit again."""


class SeedanceRejected(RuntimeError):
    pass


class SeedanceCredentialRejected(SeedanceRejected):
    pass


class SeedanceProviderFailed(RuntimeError):
    pass


class TransientSeedanceError(RuntimeError):
    pass


def available():
    return bool(TOPVIEW_UID and provider_keys.has_candidate("topview"))


def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _safe_text(value, limit=500, api_key=None):
    text = str(value or "")
    for secret in (api_key, os.environ.get("TOPVIEW_API_KEY", ""), TOPVIEW_UID):
        if secret:
            text = re.sub(re.escape(secret), "***", text, flags=re.IGNORECASE)
    text = re.sub(
        r"(?i)(https?://[^\s?#\"'<>]+)\?[^\s\"'<>]+",
        lambda match: match.group(1) + "?[REDACTED]",
        text,
    )
    return text[:limit]


def _detail(payload, api_key=None):
    if not isinstance(payload, dict):
        return _safe_text(payload, api_key=api_key)
    result = payload.get("result")
    detail = payload.get("message") or payload.get("detail") or payload.get("error")
    if isinstance(result, dict):
        detail = result.get("errorMsg") or detail
    return _safe_text(detail or payload, api_key=api_key)


def _headers(api_key, json_body=False):
    if not str(api_key or "").strip() or not TOPVIEW_UID:
        raise ValueError("Topview Seedance 未配置")
    headers = {
        "Authorization": "Bearer " + str(api_key).strip(),
        "Topview-Uid": TOPVIEW_UID,
        "Accept": "application/json",
        "User-Agent": "huangque-content/1.0",
    }
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


def _request_json(opener, method, path, body=None, timeout=90, api_key=None,
                  submit=False):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        TOPVIEW_BASE + "/" + str(path).lstrip("/"), data=data,
        headers=_headers(api_key, body is not None), method=method,
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        try:
            detail = _detail(json.loads(exc.read().decode("utf-8", "replace") or "{}"), api_key)
        except Exception:
            detail = _safe_text(exc, api_key=api_key)
        if submit and exc.code >= 500:
            raise CreateOutcomeUnknown("Topview 提交结果未知，请勿重复提交: " + detail) from exc
        if method == "GET" and (exc.code in {408, 429} or exc.code >= 500):
            raise TransientSeedanceError("Topview 查询暂时失败: " + detail) from exc
        error = SeedanceCredentialRejected if exc.code in {401, 403} else SeedanceRejected
        raise error("Topview Seedance 请求失败: " + detail) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        message = _safe_text(exc, 300, api_key)
        if submit:
            raise CreateOutcomeUnknown("Topview 提交结果未知，请勿重复提交: " + message) from exc
        raise TransientSeedanceError("Topview 网络异常: " + message) from exc
    try:
        payload = json.loads(raw.decode("utf-8", "replace") or "{}")
    except (UnicodeError, ValueError) as exc:
        if submit:
            raise CreateOutcomeUnknown("Topview 提交结果未知，请勿重复提交：返回内容无法解析") from exc
        raise TransientSeedanceError("Topview 查询返回了无效 JSON") from exc
    if not isinstance(payload, dict):
        if submit:
            raise CreateOutcomeUnknown("Topview 提交结果未知，请勿重复提交：返回格式异常")
        raise TransientSeedanceError("Topview 查询返回格式异常")
    if str(payload.get("code")) != "200":
        detail = _detail(payload, api_key)
        error = SeedanceCredentialRejected if any(
            word in detail.lower() for word in ("unauthorized", "api key", "authentication")
        ) else SeedanceRejected
        raise error("Topview Seedance 请求被拒绝: " + detail)
    return payload


def _build_payload(model, prompt, duration, ratio, resolution, generate_audio,
                   reference_images=None):
    if str(model or "").strip() not in MODEL_NAMES:
        raise ValueError("Topview 测试通道仅支持 Seedance 2.5")
    prompt = str(prompt or "").strip()
    if not prompt:
        raise ValueError("请输入 Seedance 视频提示词")
    try:
        duration = int(duration)
    except (TypeError, ValueError):
        raise ValueError("Seedance 视频时长必须为 4～15 秒") from None
    ratio = str(ratio or "").strip()
    resolution = str(resolution or "").strip().lower()
    refs = [str(item or "").strip() for item in (reference_images or []) if str(item or "").strip()]
    if duration < 4 or duration > 15:
        raise ValueError("Seedance 视频时长必须为 4～15 秒")
    if ratio not in RATIOS:
        raise ValueError("Seedance 不支持该画面比例")
    if resolution not in RESOLUTIONS:
        raise ValueError("Seedance 不支持该分辨率")
    if not isinstance(generate_audio, bool):
        raise ValueError("Seedance 声音选项必须为布尔值")
    if len(refs) > 1:
        raise ValueError("Topview 人脸测试首期仅支持 1 张首帧参考图")
    if not refs and ratio == "adaptive":
        raise ValueError("Topview 文生视频需要选择明确画面比例")
    body = {
        "model": MODEL_NAMES[model], "prompt": prompt,
        "resolution": int(resolution[:-1]), "duration": duration,
        "sound": "on" if generate_audio else "off", "generatingCount": 1,
    }
    if ratio != "adaptive":
        body["aspectRatio"] = ratio
    return body


def _download_image(opener, url):
    parsed = urllib.parse.urlsplit(str(url or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Topview 参考图必须是泽龙暂存后的 HTTPS 地址")
    request = urllib.request.Request(url, headers={"User-Agent": "huangque-content/1.0"})
    with opener.open(request, timeout=90) as response:
        content_type = str(response.headers.get_content_type() or "").lower()
        data = response.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Topview 参考图不能超过30MB")
    formats = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
    if content_type not in formats:
        raise ValueError("Topview 参考图格式不支持（jpg/png/webp）")
    return data, content_type, formats[content_type]


def _upload_image(opener, url, api_key):
    data, content_type, image_format = _download_image(opener, url)
    credential = _request_json(
        opener, "GET", "/v1/upload/credential?format=" + image_format,
        timeout=60, api_key=api_key,
    ).get("result") or {}
    file_id = str(credential.get("fileId") or "").strip()
    upload_url = str(credential.get("uploadUrl") or "").strip()
    if not file_id or not upload_url:
        raise RuntimeError("Topview 未返回图片上传凭据")
    try:
        request = urllib.request.Request(
            upload_url, data=data, headers={"Content-Type": content_type}, method="PUT"
        )
        with opener.open(request, timeout=120) as response:
            response.read()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
        raise RuntimeError("Topview 参考图上传失败: " + _safe_text(exc, 240, api_key)) from exc
    checked = _request_json(
        opener, "GET", "/v1/upload/check?fileId=" + urllib.parse.quote(file_id, safe=""),
        timeout=60, api_key=api_key,
    )
    if checked.get("result") is not True:
        raise RuntimeError("Topview 参考图上传校验失败")
    return file_id


def _poll(opener, request_id, model, duration, ratio, resolution, generate_audio,
          job_id=None, heartbeat=None, now=None, sleep=None, api_key=None,
          provider_key_id=None):
    now, sleep = now or time.time, sleep or time.sleep
    deadline = now() + TIMEOUT
    route, separator, task_id = str(request_id or "").partition(":")
    if separator != ":" or route not in {"i2v", "t2v"} or not task_id:
        raise ValueError("恢复 Topview Seedance 缺少有效 task id")
    query_path = (
        "/v2/common_task/image2video/task/query" if route == "i2v"
        else "/v1/common_task/text2video/task/query"
    )
    while now() < deadline:
        payload = _request_json(
            opener, "GET", query_path + "?taskId=" + urllib.parse.quote(task_id, safe="")
            + "&needCloudFrontUrl=true", timeout=60, api_key=api_key,
        )
        result = payload.get("result") or {}
        status = str(result.get("status") or "").strip().lower()
        if heartbeat:
            heartbeat(job_id, "seedance_" + (status or "unknown"),
                      provider_video_id=request_id, provider_key_id=provider_key_id,
                      model=model, error="")
        if status == "success":
            videos = result.get("videos") or []
            video_url = str((videos[0] if videos else {}).get("filePath") or "").strip()
            if not video_url:
                raise SeedanceProviderFailed("Topview 任务成功但未返回成片 URL")
            return {
                "request_id": request_id, "model": model,
                "source_video_url": video_url, "duration": duration,
                "ratio": ratio, "resolution": resolution,
                "generate_audio": generate_audio,
                "provider_cost_credit": result.get("costCredit"),
            }
        if status in {"fail", "failed", "cancelled", "canceled", "expired"}:
            raise SeedanceProviderFailed(
                "Topview Seedance 生成失败: " + _safe_text(result.get("errorMsg"), 300, api_key)
            )
        if status not in {"init", "queued", "waiting", "running", "processing"}:
            raise SeedanceProviderFailed("Topview Seedance 返回未知状态: " + (status or "空"))
        sleep(POLL_INTERVAL)
    raise TimeoutError("Topview Seedance 视频生成超时")


def generate(model=SEEDANCE_MODEL, prompt="", duration=5, ratio="9:16",
             resolution="720p", generate_audio=True, reference_images=None,
             job_id=None, heartbeat=None, now=None, sleep=None, api_key=None,
             provider_key_id=None):
    refs = [str(item or "").strip() for item in (reference_images or []) if str(item or "").strip()]
    body = _build_payload(model, prompt, duration, ratio, resolution, generate_audio, refs)
    opener = _opener()
    if refs:
        body.pop("aspectRatio", None)
        body["firstFrameFileId"] = _upload_image(opener, refs[0], api_key)
        route = "i2v"
        path = "/v2/common_task/image2video/task/submit"
    else:
        route = "t2v"
        path = "/v1/common_task/text2video/task/submit"
    created = _request_json(opener, "POST", path, body, timeout=120,
                            api_key=api_key, submit=True)
    result = created.get("result") or {}
    task_id = str(result.get("taskId") or "").strip()
    if not task_id:
        raise CreateOutcomeUnknown("Topview 提交结果未知，请勿重复提交：未返回 taskId")
    request_id = route + ":" + task_id
    if heartbeat:
        heartbeat(job_id, "seedance_" + str(result.get("status") or "queued").lower(),
                  provider_video_id=request_id, provider_key_id=provider_key_id,
                  model=model, error="")
    return _poll(opener, request_id, model, int(duration), ratio, resolution,
                 generate_audio, job_id, heartbeat, now, sleep, api_key,
                 provider_key_id)


def resume(task_id, model=SEEDANCE_MODEL, duration=5, ratio="9:16",
           resolution="720p", generate_audio=True, job_id=None, heartbeat=None,
           now=None, sleep=None, api_key=None, provider_key_id=None):
    return _poll(_opener(), task_id, model, duration, ratio, resolution,
                 generate_audio, job_id, heartbeat, now, sleep, api_key,
                 provider_key_id)
