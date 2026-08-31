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
from . import feature_flags, matrix_template_semantics, pricing


FEATURE_KEY = "matrix_template_video"
TRANSITION_TEMPLATE_COUNTS = frozenset({2, 15, 19})
APPROVED_TEMPLATE_IDS = ("full-overlay-bold", "poster-split")
REQUIRED_TEMPLATE_IDS = frozenset(APPROVED_TEMPLATE_IDS)
REFERENCE_TEMPLATE_RE = re.compile(r"ref-[0-9]{2}-[a-z0-9-]{1,48}\Z")
REFERENCE_TEMPLATE_COUNT = 17
API_URL = os.environ.get("MATRIX_TEMPLATE_API_URL", "http://127.0.0.1:8112").rstrip("/")
API_TOKEN = os.environ.get("MATRIX_TEMPLATE_API_TOKEN", "").strip()
JOB_TIMEOUT = max(60, min(1800, int(os.environ.get("MATRIX_TEMPLATE_JOB_TIMEOUT", "1200"))))
POLL_INTERVAL = max(1, min(10, int(os.environ.get("MATRIX_TEMPLATE_POLL_INTERVAL", "3"))))
MAX_VIDEO_BYTES = 512 * 1024 * 1024
_CACHE = {
    "at": 0.0,
    "templates": [],
    "fonts": [],
    "max_batch_size": 1,
    "engine_concurrency": {"ffmpeg": 1, "hyperframes": 1},
}
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
        ready = (
            health.get("ok") is True
            and int(health.get("templates") or 0) in TRANSITION_TEMPLATE_COUNTS
        )
    except Exception:
        ready = False
    return {"enabled": True, "ready": ready, "available": ready}


def require_available():
    feature_flags.require_enabled(FEATURE_KEY)
    if not availability().get("ready"):
        raise feature_flags.FeatureDisabled("模板成片服务暂不可用，请稍后重试")


_SEMANTIC_CONTRACTS = {
    "v01": {
        "top1": (70, 2), "top2": (64, 2),
        "top3": (52, 2), "bottom2": (74, 2),
    },
    "v02": {
        "top1": (86, 2), "top2": (62, 4), "bottom2": (78, 2),
    },
    "v03": {
        "top1": (86, 2), "top2": (62, 4), "bottom2": (78, 2),
    },
    "v04": {
        "top1": (88, 2), "top2": (72, 2),
        "top3": (48, 2), "bottom2": (52, 2),
    },
    "v05": {
        "top1": (102, 2), "top2": (104, 2),
        "top3": (68, 2), "bottom2": (70, 2),
    },
    "v06": {
        "top1": (104, 2), "top2": (76, 2),
        "top3": (60, 2), "bottom2": (76, 2),
    },
    "v07": {
        "top1": (104, 2), "top2": (68, 2),
        "top3": (62, 2), "bottom2": (84, 2),
    },
    "v08": {
        "top1": (92, 2), "top2": (62, 2),
        "top3": (54, 2), "bottom2": (64, 2),
    },
    "v09": {
        "top1": (78, 2), "top2": (50, 4), "bottom2": (66, 2),
    },
    "v10": {
        "top1": (70, 2), "top2": (78, 2),
        "top3": (54, 2), "bottom2": (80, 2),
    },
    "v11": {
        "top1": (86, 2), "top2": (80, 2),
        "top3": (54, 2), "bottom2": (76, 2),
    },
    "v12": {
        "top1": (72, 2), "top2": (62, 2),
        "top3": (50, 2), "bottom2": (62, 2),
    },
    "v13": {
        "top1": (76, 2), "top2": (68, 4), "bottom2": (80, 2),
    },
    "v14": {
        "top1": (72, 2), "top2": (50, 4), "bottom2": (64, 2),
    },
    "v15": {
        "top1": (80, 2), "top2": (64, 4), "bottom2": (92, 2),
    },
    "v16": {
        "top1": (48, 2), "top2": (68, 2),
        "top3": (52, 2), "bottom2": (70, 2),
    },
    "v17": {
        "top1": (74, 2), "top2": (64, 2),
        "top3": (118, 2), "bottom2": (84, 2),
    },
}
_ALL_REFERENCE_VARIANTS = {
    f"v{index:02d}" for index in range(1, 18)
}
_ALLOWED_SEMANTIC_VARIANT_SETS = (
    {"v02"}, {"v02", "v05"}, _ALL_REFERENCE_VARIANTS,
)


def _semantic_contract(value, variant):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {
        "version", "max_width_px", "layers",
    }:
        raise RuntimeError("HyperFrames 语义排版能力无效")
    layers = value.get("layers")
    expected_layers = _SEMANTIC_CONTRACTS.get(str(variant or ""))
    if (
        value.get("version") != 1
        or value.get("max_width_px") != 996
        or not isinstance(layers, dict)
        or expected_layers is None
        or set(layers) != set(expected_layers)
    ):
        raise RuntimeError("HyperFrames 语义排版能力无效")
    normalized = {}
    for layer, expected in expected_layers.items():
        item = layers.get(layer)
        if not isinstance(item, dict) or set(item) != {
            "font_size_px", "max_lines",
        }:
            raise RuntimeError("HyperFrames 语义排版能力无效")
        pair = (item.get("font_size_px"), item.get("max_lines"))
        if pair != expected:
            raise RuntimeError("HyperFrames 语义排版能力无效")
        normalized[layer] = {
            "font_size_px": int(pair[0]), "max_lines": int(pair[1]),
        }
    return {"version": 1, "max_width_px": 996, "layers": normalized}


def _refresh_catalog(force=False):
    now = time.monotonic()
    if force or now - _CACHE["at"] > 30:
        response = _request("GET", "/v1/templates", timeout=10)
        try:
            max_batch_size = int(response.get("max_batch_size") or 1)
            raw_engine_concurrency = response.get("engine_concurrency") or {}
            if not isinstance(raw_engine_concurrency, dict):
                raise TypeError("engine concurrency must be an object")
            engine_concurrency = {
                "ffmpeg": int(
                    raw_engine_concurrency.get("ffmpeg") or max_batch_size
                ),
                "hyperframes": int(
                    raw_engine_concurrency.get("hyperframes")
                    or response.get("hyperframes_concurrency") or 1
                ),
            }
        except (TypeError, ValueError) as exc:
            raise RuntimeError("模板批量能力无效") from exc
        if (
            not 1 <= max_batch_size <= 5
            or any(
                not 1 <= value <= max_batch_size
                for value in engine_concurrency.values()
            )
        ):
            raise RuntimeError("模板批量能力无效")
        templates = []
        for raw in response.get("templates") or []:
            if not isinstance(raw, dict):
                continue
            template_id = str(raw.get("id") or "")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", template_id):
                continue
            engine = str(raw.get("engine") or "ffmpeg")
            font_selectable = raw.get("font_selectable") is not False
            font_mode = str(raw.get("font_mode") or (
                "selectable" if font_selectable else "template_locked"
            ))
            variant = str(raw.get("variant") or "")
            semantic_layout = _semantic_contract(
                raw.get("semantic_layout"), variant,
            )
            if engine not in {"ffmpeg", "hyperframes"}:
                continue
            if font_mode not in {"selectable", "template_locked"}:
                continue
            if engine == "hyperframes" and not re.fullmatch(
                r"v(?:0[1-9]|1[0-7])", variant
            ):
                continue
            template = {
                "id": template_id,
                "name": str(raw.get("name") or template_id)[:40],
                "description": str(raw.get("description") or "")[:160],
                "tags": [str(item)[:20] for item in (raw.get("tags") or [])[:8]],
                "engine": engine,
                "font_mode": font_mode,
                "font_selectable": font_selectable,
                "variant": variant,
            }
            if semantic_layout is not None:
                template["semantic_layout"] = semantic_layout
            templates.append(template)
        template_ids = {item["id"] for item in templates}
        if (
            len(templates) not in TRANSITION_TEMPLATE_COUNTS
            or len(template_ids) != len(templates)
            or not REQUIRED_TEMPLATE_IDS.issubset(template_ids)
        ):
            raise RuntimeError("模板目录不完整")
        approved = {
            item["id"]: item for item in templates
            if item["id"] in REQUIRED_TEMPLATE_IDS
        }
        if len(templates) == 19:
            references = [
                item for item in templates
                if REFERENCE_TEMPLATE_RE.fullmatch(item["id"])
            ]
            if (
                len(references) != REFERENCE_TEMPLATE_COUNT
                or any(
                    item["engine"] != "hyperframes"
                    or item["font_selectable"] is not False
                    or item["font_mode"] != "template_locked"
                    for item in references
                )
                or {item["variant"] for item in references}
                != {f"v{index:02d}" for index in range(1, 18)}
                or {
                    item["variant"] for item in references
                    if item.get("semantic_layout")
                } not in _ALLOWED_SEMANTIC_VARIANT_SETS
            ):
                raise RuntimeError("HyperFrames 模板目录不完整")
            templates = [approved[template_id] for template_id in APPROVED_TEMPLATE_IDS] + references
        else:
            templates = [approved[template_id] for template_id in APPROVED_TEMPLATE_IDS]
        fonts = [{"value": "", "label": "自动搭配", "source": "automatic"}]
        seen = {""}
        for raw in response.get("fonts") or []:
            if not isinstance(raw, dict):
                continue
            value = str(raw.get("value") or "").strip()
            if not value or value in seen or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9 ._+-]{0,79}", value
            ):
                continue
            source = str(raw.get("source") or "")
            if source not in {"bundled", "private"}:
                continue
            fonts.append({
                "value": value,
                "label": str(raw.get("label") or value)[:40],
                "source": source,
            })
            seen.add(value)
        _CACHE.update({
            "at": now,
            "templates": templates,
            "fonts": fonts,
            "max_batch_size": max_batch_size,
            "engine_concurrency": engine_concurrency,
        })


def public_templates(force=False):
    _refresh_catalog(force)
    return [dict(item) for item in _CACHE["templates"]]


def public_fonts(force=False):
    _refresh_catalog(force)
    return [dict(item) for item in _CACHE["fonts"]]


def public_batch_capability(force=False):
    _refresh_catalog(force)
    return {
        "max_batch_size": int(_CACHE["max_batch_size"]),
        "engine_concurrency": dict(_CACHE["engine_concurrency"]),
    }


def validate_payload(raw, username=""):
    require_available()
    body = dict(raw or {})
    top = " ".join(str(body.get("top_text") or "").split())
    bottom = " ".join(str(body.get("bottom_text") or "").split())
    if not 2 <= len(top) <= 60:
        raise ValueError("顶部标题需要 2-60 个字符")
    if not 2 <= len(bottom) <= 80:
        raise ValueError("底部行动文案需要 2-80 个字符")
    template_id = str(body.get("template_id") or APPROVED_TEMPLATE_IDS[0])
    template = next(
        (item for item in public_templates() if item["id"] == template_id), None
    )
    if template is None:
        raise ValueError("请选择有效模板")
    font_family = str(body.get("font_family") or "").strip()
    font_selectable = template.get("font_selectable") is not False
    if (
        font_selectable and font_family
        and font_family not in {item["value"] for item in public_fonts()}
    ):
        raise ValueError("请选择当前可用字体")
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
    if font_family and font_selectable:
        candidate["font_family"] = font_family
    semantic_contract = template.get("semantic_layout")
    batch_id = str(body.get("batch_id") or "").strip().lower()
    batch_index = body.get("batch_index")
    batch_size = body.get("batch_size")
    if batch_id or batch_index is not None or batch_size is not None:
        if (
            not re.fullmatch(r"[0-9a-f]{32}", batch_id)
            or isinstance(batch_index, bool) or not isinstance(batch_index, int)
            or isinstance(batch_size, bool) or not isinstance(batch_size, int)
            or not 1 <= batch_index <= batch_size <= 5
        ):
            raise ValueError("批量任务参数无效")
        candidate.update({
            "batch_id": batch_id,
            "batch_index": batch_index,
            "batch_size": batch_size,
        })
    response = None
    if semantic_contract is not None:
        def validate_semantic_layout(semantic_layout):
            candidate["semantic_layout"] = semantic_layout
            try:
                value = _request("POST", "/v1/preflight", candidate, timeout=10)
                payload = value.get("payload") if isinstance(value, dict) else None
                if (
                    not isinstance(payload, dict)
                    or payload.get("semantic_layout") != semantic_layout
                ):
                    return False, "生成端回显的 semantic_layout 与候选不一致"
                return True, value
            except MatrixTemplateHTTPError as exc:
                if exc.status == 400 and (
                    "语义" in str(exc) or "完整词组" in str(exc)
                ):
                    return False, str(exc)
                raise

        try:
            semantic_layout, response = matrix_template_semantics.resolve(
                top, bottom, template_id, semantic_contract,
                validate_semantic_layout,
            )
            candidate["semantic_layout"] = semantic_layout
        except MatrixTemplateHTTPError as exc:
            if exc.status == 400:
                raise ValueError(str(exc)) from exc
            raise feature_flags.FeatureDisabled(
                "模板成片服务暂不可用，请稍后重试"
            ) from exc
        except RuntimeError as exc:
            raise feature_flags.FeatureDisabled(
                "AI 语义排版或模板预检服务暂不可用，请稍后重试"
            ) from exc
    if response is None:
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
    if any(payload.get(key) != value for key, value in candidate.items()
           if key != "duration"):
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
                "font_selection": result.get("font_selection") or {},
                "font_files": result.get("font_files") or [],
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
