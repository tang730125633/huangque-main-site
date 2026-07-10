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
# 出境通道：原来是裸 urlopen，继承进程级 HTTPS_PROXY（mihomo）。改为 VPS 隧道优先、mihomo 备选。
# 显式 WAVESPEED_PROXY 可覆盖。素材上传走 COS（国内直连，不经代理），走代理的只有
# api.wavespeed.ai 的小 JSON 和 _download_to_lib 拉成片 —— 后者是重活。
# ⚠ 隧道实测被整形在 ~1.3 MB/s，成片下载会撞这个天花板；待 VPS 升级带宽后消失。
WAVESPEED_PROXY = (os.environ.get("WAVESPEED_PROXY") or "").strip()
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


def _ws_req(method, url, body=None, timeout=60):
    headers = {"Authorization": "Bearer " + WAVESPEED_KEY}
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _opener().open(req, timeout=timeout) as r:
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
    with _opener().open(req, timeout=360) as r:   # 拉成片：整个流程里最重的一腿
        data = r.read()
    if not data:
        raise RuntimeError("WaveSpeed成片下载为空")
    fn = "video/%s_%s.mp4" % (prefix, uuid.uuid4().hex)  # 不可猜键
    _out_path(fn).write_bytes(data)
    return fn


def generate_motion(image_file, reference_video_file, resolution, job_id=None):
    """线路二·动作模仿：人物图 + 驱动视频 → animate。返回 {video_file, video_url, provider}。"""
    if str(resolution or "").strip().lower() != "720p":
        raise ValueError("线路二动作模仿仅支持 720p")
    _phase(job_id, "ws_uploading")
    img_url = _material_url(image_file)
    vid_url = _material_url(reference_video_file)
    _phase(job_id, "ws_running")
    out_url = _run_and_wait(
        WS_MOTION,
        {"image": img_url, "video": vid_url, "mode": "animate", "resolution": "720p"},
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
