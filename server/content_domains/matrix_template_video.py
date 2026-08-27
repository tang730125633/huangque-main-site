"""Production-site bridge to the isolated matrix template generation service."""

from __future__ import annotations

import json
import os
import pathlib
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .core import OUT_DIR, public_url
from . import feature_flags, pricing


FEATURE_KEY = "matrix_template_video"
API_URL = os.environ.get("MATRIX_TEMPLATE_API_URL", "http://127.0.0.1:8112").rstrip("/")
API_TOKEN = os.environ.get("MATRIX_TEMPLATE_API_TOKEN", "").strip()
JOB_TIMEOUT = max(60, min(1800, int(os.environ.get("MATRIX_TEMPLATE_JOB_TIMEOUT", "1200"))))
POLL_INTERVAL = max(1, min(10, int(os.environ.get("MATRIX_TEMPLATE_POLL_INTERVAL", "3"))))
MAX_VIDEO_BYTES = 512 * 1024 * 1024
_CACHE = {"at": 0.0, "templates": []}
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class MatrixTemplateHTTPError(RuntimeError):
    def __init__(self, status, detail):
        super().__init__(detail)
        self.status = int(status)


def _validated_base():
    parsed = urllib.parse.urlsplit(API_URL)
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if (
        parsed.scheme not in ({"http", "https"} if loopback else {"https"})
        or not parsed.hostname or parsed.username or parsed.password
        or parsed.query or parsed.fragment
    ):
        raise RuntimeError("模板成片服务地址配置无效")
    return parsed


def _request(method, path, body=None, *, request_id="", timeout=30):
    if not API_TOKEN:
        raise RuntimeError("模板成片服务凭证未配置")
    _validated_base()
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    headers = {"Authorization": "Bearer " + API_TOKEN}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if request_id:
        headers["X-Request-Id"] = request_id
    request = urllib.request.Request(API_URL + path, data=data, headers=headers, method=method)
    try:
        with _NO_PROXY.open(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            value = json.loads(exc.read())
            detail = value.get("detail") or value.get("error")
        except Exception:
            detail = None
        raise MatrixTemplateHTTPError(
            exc.code, str(detail or "模板成片生成服务请求失败")
        ) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError("模板成片生成服务连接失败") from exc


def availability(force=False):
    enabled = feature_flags.is_enabled(FEATURE_KEY)
    if not enabled:
        return {"enabled": False, "ready": False, "available": False}
    try:
        health = _request("GET", "/health", timeout=5)
        ready = health.get("ok") is True and int(health.get("templates") or 0) == 13
    except Exception:
        ready = False
    return {"enabled": True, "ready": ready, "available": ready}


def require_available():
    feature_flags.require_enabled(FEATURE_KEY)
    if not availability().get("ready"):
        raise feature_flags.FeatureDisabled("模板成片服务暂不可用，请稍后重试")


def public_templates(force=False):
    now = time.monotonic()
    if force or now - _CACHE["at"] > 30:
        response = _request("GET", "/v1/templates", timeout=10)
        templates = []
        for raw in response.get("templates") or []:
            if not isinstance(raw, dict):
                continue
            template_id = str(raw.get("id") or "")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", template_id):
                continue
            templates.append({
                "id": template_id,
                "name": str(raw.get("name") or template_id)[:40],
                "description": str(raw.get("description") or "")[:160],
                "tags": [str(item)[:20] for item in (raw.get("tags") or [])[:8]],
            })
        if len(templates) != 13 or len({item["id"] for item in templates}) != 13:
            raise RuntimeError("模板目录不完整")
        _CACHE.update({"at": now, "templates": templates})
    return [dict(item) for item in _CACHE["templates"]]


def validate_payload(raw, username=""):
    require_available()
    body = dict(raw or {})
    top = " ".join(str(body.get("top_text") or "").split())
    bottom = " ".join(str(body.get("bottom_text") or "").split())
    if not 2 <= len(top) <= 60:
        raise ValueError("顶部标题需要 2-60 个字符")
    if not 2 <= len(bottom) <= 80:
        raise ValueError("底部行动文案需要 2-80 个字符")
    template_id = str(body.get("template_id") or "native-bold")
    if template_id not in {item["id"] for item in public_templates()}:
        raise ValueError("请选择有效模板")
    bgm = body.get("bgm", True)
    if not isinstance(bgm, bool):
        raise ValueError("背景音乐设置无效")
    duration = body.get("duration")
    if duration not in (None, ""):
        try:
            duration = float(duration)
        except (TypeError, ValueError) as exc:
            raise ValueError("视频时长设置无效") from exc
        if duration < 8 or duration > 15:
            raise ValueError("视频时长需要 8-15 秒")
    else:
        duration = None
    candidate = {
        "top_text": top, "bottom_text": bottom,
        "template_id": template_id, "bgm": bgm, "duration": duration,
    }
    try:
        response = _request("POST", "/v1/preflight", candidate, timeout=10)
    except MatrixTemplateHTTPError as exc:
        if exc.status == 400:
            raise ValueError(str(exc)) from exc
        raise feature_flags.FeatureDisabled(
            "模板成片服务暂不可用，请稍后重试"
        ) from exc
    except RuntimeError as exc:
        raise feature_flags.FeatureDisabled(
            "模板成片服务暂不可用，请稍后重试"
        ) from exc
    payload = response.get("payload") if isinstance(response, dict) else None
    if not isinstance(payload, dict) or set(payload) != set(candidate):
        raise RuntimeError("模板成片预检结果无效")
    if any(payload.get(key) != candidate[key] for key in (
            "top_text", "bottom_text", "template_id", "bgm")):
        raise RuntimeError("模板成片预检参数不一致")
    authoritative_duration = payload.get("duration")
    if (isinstance(authoritative_duration, bool)
            or not isinstance(authoritative_duration, (int, float))
            or not 8 <= float(authoritative_duration) <= 15):
        raise RuntimeError("模板成片预检时长无效")
    return dict(payload, duration=float(authoritative_duration))


def _safe_file_url(value):
    base = _validated_base()
    parsed = urllib.parse.urlsplit(str(value or ""))
    if parsed.scheme or parsed.netloc:
        if (parsed.scheme, parsed.netloc) != (base.scheme, base.netloc):
            raise RuntimeError("模板成片服务返回了无效文件地址")
        return urllib.parse.urlunsplit(parsed)
    path = "/" + str(value or "").lstrip("/")
    prefix = base.path.rstrip("/")
    return urllib.parse.urlunsplit((base.scheme, base.netloc, prefix + path, "", ""))


def _download(value, job_id):
    url = _safe_file_url(value)
    relative = pathlib.Path("video") / ("matrix_template_%s.mp4" % str(job_id)[:64])
    target = OUT_DIR / relative
    temporary = target.with_suffix(".mp4.part")
    target.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"Authorization": "Bearer " + API_TOKEN})
    total = 0
    try:
        with _NO_PROXY.open(request, timeout=240) as response, temporary.open("wb") as handle:
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_VIDEO_BYTES:
                    raise RuntimeError("模板成片文件超过大小限制")
                handle.write(chunk)
        with temporary.open("rb") as handle:
            if total < 1024 or b"ftyp" not in handle.read(64):
                raise RuntimeError("模板成片文件无效")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return relative.as_posix(), total


def generate(payload):
    raw = dict(payload or {})
    local_job = str(raw.get("_job_id") or uuid.uuid4().hex)
    payload = validate_payload(raw, str(raw.get("_username") or ""))
    request_id = "matrix-template-" + re.sub(r"[^A-Za-z0-9_.:-]", "-", local_job)[:80]
    remote = _request("POST", "/v1/jobs", payload, request_id=request_id, timeout=20)
    remote_id = str(remote.get("job_id") or "")
    if not re.fullmatch(r"[0-9a-f]{32}", remote_id):
        raise RuntimeError("模板成片服务没有返回有效任务 ID")
    deadline = time.monotonic() + JOB_TIMEOUT
    while time.monotonic() < deadline:
        current = _request("GET", "/v1/jobs/" + remote_id, timeout=20)
        status = str(current.get("status") or "")
        if status == "completed":
            result = current.get("result") or {}
            video_file, file_size = _download(result.get("file_url"), local_job)
            return {
                "type": "matrix_template_video",
                "mode": "matrix_template",
                "provider": "matrix-template",
                "provider_task_id": remote_id,
                "status": "done",
                "video_file": video_file,
                "video_url": public_url(video_file, "video/mp4", private=True),
                "duration": float(result.get("duration") or 0),
                "phase": "done",
                "resolution": "1080p",
                "ratio": "9:16",
                "width": int(result.get("width") or 1080),
                "height": int(result.get("height") or 1920),
                "template_id": result.get("template_id") or payload["template_id"],
                "file_size": file_size,
                "material_manifest": result.get("material_manifest") or [],
            }
        if status == "failed":
            raise RuntimeError(str(current.get("error") or "模板成片生成失败")[:500])
        time.sleep(POLL_INTERVAL)
    raise RuntimeError("模板成片生成超时")


def cost(payload):
    return pricing.get_price("video.matrix_template")


HANDLERS = {"matrix_template_video": generate}
