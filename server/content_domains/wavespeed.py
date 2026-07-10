#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""线路二 · WaveSpeed 渠道（动作模仿 wan-2.2/animate + 换装 ai-virtual-outfit-tryon）。

与线路一（HeyGen 动作模仿 / RunningHub 换装）并列，由 gen_video/gen_tryon 按 line 参数分流。
WaveSpeed 只收公网 URL 素材，故先把本地素材转存 COS 拿直链再喂给它。
返回结构与线路一的 generate_heygen_motion_video / generate_tryon_video 对齐，供上层无差别使用。
"""

import json
import os
import time
import uuid
import urllib.error
import urllib.request

from . import cos
from .core import _out_path, _resolve_out_file, public_url

WAVESPEED_KEY = os.environ.get("WAVESPEED_API_KEY", "")
WS_API = "https://api.wavespeed.ai/api/v3"
WS_MOTION = "/wavespeed-ai/wan-2.2/animate"
WS_TRYON = "/wavespeed-ai/ai-virtual-outfit-tryon"
WS_POLL_INTERVAL = int(os.environ.get("WAVESPEED_POLL_INTERVAL", "5"))
WS_DEADLINE = int(os.environ.get("WAVESPEED_DEADLINE", "600"))  # 单任务最长等待(秒)


def available():
    return bool(WAVESPEED_KEY)


def _phase(job_id, phase):
    """心跳刷 updated_at 防 reaper。update_video_asset_phase 在 video.py，延迟 import 避免循环依赖。"""
    if not job_id:
        return
    try:
        from .video import update_video_asset_phase
        update_video_asset_phase(job_id, phase)
    except Exception:
        pass


def _ws_req(method, url, body=None, timeout=60):
    headers = {"Authorization": "Bearer " + WAVESPEED_KEY}
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        detail = (e.read() or b"").decode("utf-8", "replace")[:300]
        raise RuntimeError("WaveSpeed接口失败: HTTP %s %s" % (e.code, detail)) from e


def _material_url(local_rel):
    """本地素材(相对路径) → 转存 COS 拿公网直链喂 WaveSpeed。COS 未启用则无法走线路二。"""
    fp = _resolve_out_file(local_rel)
    if not fp:
        raise ValueError("素材文件不存在: %s" % local_rel)
    if not cos.enabled():
        raise RuntimeError("线路二(WaveSpeed)需要 COS 存素材直链，当前未启用 COS")
    suffix = os.path.splitext(str(fp))[1] or ".bin"
    key = "wavespeed-input/%s%s" % (uuid.uuid4().hex, suffix)  # 不可猜键
    cos.upload(str(fp), key)
    return cos.object_url(key)


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


def _download_to_lib(url, prefix):
    req = urllib.request.Request(url, headers={"User-Agent": "huangque-content/1.0"})
    with urllib.request.urlopen(req, timeout=360) as r:
        data = r.read()
    if not data:
        raise RuntimeError("WaveSpeed成片下载为空")
    fn = "video/%s_%s.mp4" % (prefix, uuid.uuid4().hex)  # 不可猜键
    _out_path(fn).write_bytes(data)
    return fn


def generate_motion(image_file, reference_video_file, resolution, job_id=None):
    """线路二·动作模仿：人物图 + 驱动视频 → animate。返回 {video_file, video_url, provider}。"""
    _phase(job_id, "ws_uploading")
    img_url = _material_url(image_file)
    vid_url = _material_url(reference_video_file)
    res = "720p" if str(resolution) in ("720p", "1080p", "4k") else "720p"  # 最低720p，480p抽帧明显
    _phase(job_id, "ws_running")
    out_url = _run_and_wait(
        WS_MOTION,
        {"image": img_url, "video": vid_url, "mode": "animate", "resolution": res},
        job_id=job_id,
    )
    _phase(job_id, "downloading")
    vf = _download_to_lib(out_url, "ws_motion")
    return {"video_file": vf, "video_url": public_url(vf, "video/mp4", private=True), "provider": "wavespeed"}


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
