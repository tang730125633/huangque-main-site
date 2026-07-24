#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""线路二 · WaveSpeed 渠道（换装 ai-virtual-outfit-tryon）。

换装的线路一（RunningHub）并列，由 gen_tryon 按 line 参数分流。
WaveSpeed 只收公网 URL 素材，故先把本地素材转存 COS 拿直链再喂给它。
返回结构与线路一的 generate_tryon_video 对齐，供上层无差别使用。
"""

import json
import ipaddress
import os
import socket
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request

from . import cos
from .core import VIDEO_GEN_DEADLINE, _out_path, _resolve_out_file, public_url

WAVESPEED_KEY = os.environ.get("WAVESPEED_API_KEY", "")
# 出境通道：原来是裸 urlopen，继承进程级 HTTPS_PROXY（mihomo）。改为 VPS 隧道优先、mihomo 备选。
# 显式 WAVESPEED_PROXY 可覆盖。素材上传走 COS（国内直连，不经代理），走代理的只有
# api.wavespeed.ai 的小 JSON 和 _download_to_lib 拉成片 —— 后者是重活。
# ⚠ 隧道实测被整形在 ~1.3 MB/s，成片下载会撞这个天花板；待 VPS 升级带宽后消失。
WAVESPEED_PROXY = (os.environ.get("WAVESPEED_PROXY") or "").strip()
WS_API = "https://api.wavespeed.ai/api/v3"
WS_TRYON = "/wavespeed-ai/ai-virtual-outfit-tryon"
WS_SEEDVR2 = "/wavespeed-ai/seedvr2/video"
WS_POLL_INTERVAL = int(os.environ.get("WAVESPEED_POLL_INTERVAL", "5"))
# 单任务最长等待(秒)。跟 content 的 VIDEO_GEN_DEADLINE 走 —— 全站视频生成统一 15 分钟死线。
# 动作模仿实测生成 392~511s，原来的 600s 贴得太近，撞上一次抖动就误判失败。
WS_DEADLINE = int(os.environ.get("WAVESPEED_DEADLINE", "") or VIDEO_GEN_DEADLINE)
TRANSIENT_HTTP_CODES = {408, 429, 500, 502, 503, 504}


class WaveSpeedCreateOutcomeUnknown(RuntimeError):
    """付费 POST 可能已被接受；没有 prediction id 时不得自动重发。"""


class WaveSpeedRejected(RuntimeError):
    """付费 POST 被明确拒绝，没有创建 prediction。"""


class WaveSpeedTransientRead(RuntimeError):
    """已有 prediction id 的幂等 GET 暂时失败。"""


class WaveSpeedQueryUnavailable(RuntimeError):
    """查询被拒或任务暂不可见；不能据此认定已付费 prediction 失败。"""


class WaveSpeedProviderFailed(RuntimeError):
    """已有 prediction id，但供应商返回明确失败终态。"""


def available():
    return bool(WAVESPEED_KEY)


def _safe_text(value, limit=200):
    text = str(value or "")
    if WAVESPEED_KEY:
        text = text.replace(WAVESPEED_KEY, "***")
    return text[:limit]


def _public_http_url_state(url):
    parsed = urllib.parse.urlsplit(str(url or ""))
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme not in {"http", "https"}
        or not host
        or host == "localhost"
        or host.endswith(".localhost")
    ):
        return "blocked"
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return "blocked"
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(
                host, port, type=socket.SOCK_STREAM
            )
        }
    except OSError:
        return "unresolved"
    if not addresses:
        return "unresolved"
    try:
        if any(not ipaddress.ip_address(address).is_global for address in addresses):
            return "blocked"
    except ValueError:
        return "blocked"
    return "ok"


def _phase(job_id, phase):
    """心跳刷 updated_at 防 reaper。update_video_asset_phase 在 video.py，延迟 import 避免循环依赖。"""
    if not job_id:
        return
    try:
        from .video import update_video_asset_phase
        update_video_asset_phase(job_id, phase)
    except Exception:
        pass


def _opener():
    """通道在发请求前选定。WaveSpeed 的提交(POST)是非幂等的——一旦发出就不换通道重发，
    否则会生成两条片、计两次费。备选只在「发之前探到隧道不可达」时生效。"""
    if WAVESPEED_PROXY:
        proxy = WAVESPEED_PROXY
    else:
        from . import egress
        proxy = egress.preferred_proxy()
    if proxy:
        return urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    # 隧道与备选都没配 → build_opener() 自带的 ProxyHandler 仍读进程级 HTTP(S)_PROXY，即改动前的老行为
    return urllib.request.build_opener()


def _ws_req(method, url, body=None, timeout=60, classify_paid=False):
    headers = {"Authorization": "Bearer " + WAVESPEED_KEY}
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _opener().open(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        detail = (e.read() or b"").decode("utf-8", "replace")[:300]
        if WAVESPEED_KEY:
            detail = detail.replace(WAVESPEED_KEY, "***")
        if classify_paid and method == "POST":
            error = WaveSpeedCreateOutcomeUnknown if e.code in TRANSIENT_HTTP_CODES - {429} else WaveSpeedRejected
            raise error("WaveSpeed超分提交失败: HTTP %s %s" % (e.code, detail)) from e
        if classify_paid and method == "GET" and e.code in TRANSIENT_HTTP_CODES:
            raise WaveSpeedTransientRead(
                "WaveSpeed超分查询失败: HTTP %s %s" % (e.code, detail)
            ) from e
        if classify_paid and method == "GET":
            raise WaveSpeedQueryUnavailable(
                "WaveSpeed超分查询被拒绝: HTTP %s %s" % (e.code, detail)
            ) from e
        raise RuntimeError("WaveSpeed接口失败: HTTP %s %s" % (e.code, detail)) from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        if classify_paid and method == "POST":
            raise WaveSpeedCreateOutcomeUnknown(
                "WaveSpeed超分提交结果未知，请勿重复提交: %s" % str(e)[:180]
            ) from e
        if classify_paid and method == "GET":
            raise WaveSpeedTransientRead(
                "WaveSpeed超分查询网络异常: %s" % str(e)[:180]
            ) from e
        raise
    try:
        return json.loads(raw or b"{}")
    except (UnicodeError, ValueError) as e:
        if classify_paid and method == "POST":
            raise WaveSpeedCreateOutcomeUnknown(
                "WaveSpeed超分提交结果未知：返回内容无法解析"
            ) from e
        if classify_paid and method == "GET":
            raise WaveSpeedTransientRead("WaveSpeed超分查询返回无效 JSON") from e
        raise


def _material_url(local_rel, private=False):
    """本地素材(相对路径) → 转存 COS 拿公网直链喂 WaveSpeed。COS 未启用则无法走线路二。"""
    fp = _resolve_out_file(local_rel)
    if not fp:
        raise ValueError("素材文件不存在: %s" % local_rel)
    if not cos.enabled():
        raise RuntimeError("线路二(WaveSpeed)需要 COS 存素材直链，当前未启用 COS")
    suffix = os.path.splitext(str(fp))[1] or ".bin"
    key = "wavespeed-input/%s%s" % (uuid.uuid4().hex, suffix)  # 不可猜键
    return cos.upload(str(fp), key, private=private)


def _run_and_wait(model_path, body, job_id=None):
    r = _ws_req("POST", WS_API + model_path, body)
    if r.get("code") != 200:
        raise RuntimeError("WaveSpeed提交失败: %s" % json.dumps(r, ensure_ascii=False)[:200])
    data = r.get("data") or {}
    pid = data.get("id")
    if not pid:
        raise RuntimeError("WaveSpeed未返回任务id: %s" % json.dumps(r, ensure_ascii=False)[:200])
    poll_url = (data.get("urls") or {}).get("get") or (WS_API + "/predictions/%s/result" % pid)
    deadline = time.time() + WS_DEADLINE
    while time.time() < deadline:
        time.sleep(WS_POLL_INTERVAL)
        _phase(job_id, "ws_running")  # 心跳
        res = (_ws_req("GET", poll_url) or {}).get("data") or {}
        status = str(res.get("status") or "").lower()
        if status == "completed":
            outs = res.get("outputs") or []
            if not outs:
                raise RuntimeError("WaveSpeed完成但无产出")
            return outs[0]
        if status in ("failed", "error"):
            raise RuntimeError("WaveSpeed生成失败: %s" % str(res.get("error") or "")[:200])
    raise TimeoutError("WaveSpeed生成超时")


def run_seedvr2(
    video_url=None,
    prediction_id=None,
    job_id=None,
    on_submitted=None,
    heartbeat=None,
    now=None,
    sleep=None,
):
    """创建一次或恢复同一条 SeedVR2 prediction；恢复路径永不 POST。"""
    if not WAVESPEED_KEY:
        raise ValueError("WaveSpeed 超分未配置（WAVESPEED_API_KEY）")
    now = now or time.time
    sleep = sleep or time.sleep
    pid = str(prediction_id or "").strip()
    if not pid:
        video_url = str(video_url or "").strip()
        if not video_url.startswith(("http://", "https://")):
            raise ValueError("WaveSpeed 超分输入必须是公网视频 URL")
        response = _ws_req(
            "POST",
            WS_API + WS_SEEDVR2,
            {"video": video_url, "target_resolution": "1080p"},
            timeout=120,
            classify_paid=True,
        )
        if not isinstance(response, dict):
            raise WaveSpeedCreateOutcomeUnknown(
                "WaveSpeed超分提交结果未知：返回格式异常"
            )
        try:
            response_code = int(response.get("code"))
        except (TypeError, ValueError):
            response_code = None
        if response_code != 200:
            error = (
                WaveSpeedRejected
                if (
                    response_code is not None
                    and 400 <= response_code < 500
                    and response_code != 408
                )
                else WaveSpeedCreateOutcomeUnknown
            )
            raise error(
                "WaveSpeed超分提交失败: %s"
                % _safe_text(response.get("message") or response.get("error"))
            )
        pid = str((response.get("data") or {}).get("id") or "").strip()
        if not pid:
            raise WaveSpeedCreateOutcomeUnknown(
                "WaveSpeed超分提交结果未知：未返回 prediction id"
            )
        if on_submitted:
            on_submitted(pid)

    poll_url = (
        WS_API + "/predictions/%s/result"
        % urllib.parse.quote(pid, safe="")
    )
    deadline = now() + WS_DEADLINE
    while now() < deadline:
        if heartbeat:
            heartbeat(job_id, "seedance_upscale_running")
        response = _ws_req(
            "GET", poll_url, timeout=60, classify_paid=True
        )
        if not isinstance(response, dict):
            raise WaveSpeedTransientRead("WaveSpeed超分查询返回格式异常")
        try:
            response_code = int(response.get("code", 200))
        except (TypeError, ValueError):
            response_code = None
        if response_code not in (None, 200):
            error = (
                WaveSpeedTransientRead
                if response_code in TRANSIENT_HTTP_CODES
                else WaveSpeedQueryUnavailable
            )
            raise error(
                "WaveSpeed超分查询失败: %s"
                % _safe_text(response.get("message") or response.get("error"))
            )
        data = response.get("data") or {}
        status = str(data.get("status") or "").strip().lower()
        if status == "completed":
            outputs = data.get("outputs") or []
            output = outputs[0] if outputs else ""
            if isinstance(output, dict):
                output = output.get("url") or output.get("video") or ""
            output = str(output or "").strip()
            output_state = _public_http_url_state(output)
            if output_state == "unresolved":
                raise WaveSpeedQueryUnavailable(
                    "WaveSpeed超分成片地址暂时无法解析"
                )
            if output_state != "ok":
                raise WaveSpeedProviderFailed("WaveSpeed超分完成但未返回成片")
            return {"prediction_id": pid, "source_video_url": output}
        if status in {"failed", "error", "cancelled", "canceled"}:
            raise WaveSpeedProviderFailed(
                "WaveSpeed超分失败: %s" % _safe_text(data.get("error"))
            )
        if status and status not in {
            "created", "pending", "queued", "processing", "running",
        }:
            raise WaveSpeedProviderFailed(
                "WaveSpeed超分返回未知状态: " + status
            )
        sleep(WS_POLL_INTERVAL)
    raise TimeoutError("WaveSpeed超分生成超时")


def _download_to_lib(url, prefix):
    req = urllib.request.Request(url, headers={"User-Agent": "huangque-content/1.0"})
    with _opener().open(req, timeout=360) as r:   # 拉成片：整个流程里最重的一腿
        data = r.read()
    if not data:
        raise RuntimeError("WaveSpeed成片下载为空")
    fn = "video/%s_%s.mp4" % (prefix, uuid.uuid4().hex)  # 不可猜键
    _out_path(fn).write_bytes(data)
    return fn


def generate_tryon(person_image_file, clothes_file, duration, job_id=None):
    """线路二·换装：人物图 + 衣服图 → outfit-tryon。返回 {video_file, video_url, provider}。"""
    _phase(job_id, "ws_uploading")
    person_url = _material_url(person_image_file)
    clothes_url = _material_url(clothes_file)
    dur = max(5, min(15, int(duration or 5)))
    _phase(job_id, "ws_running")
    out_url = _run_and_wait(
        WS_TRYON,
        {"image": person_url, "clothes_images": [clothes_url], "duration": dur},
        job_id=job_id,
    )
    _phase(job_id, "downloading")
    vf = _download_to_lib(out_url, "ws_tryon")
    return {"video_file": vf, "video_url": public_url(vf, "video/mp4", private=True), "provider": "wavespeed"}
