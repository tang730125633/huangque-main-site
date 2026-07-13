# -*- coding: utf-8 -*-
"""爆款拆解：竞品视频链接 → 下载 → 抽帧 → ASR → GPT-4o 多模态 → 分镜脚本"""
import os, json, time, base64, tempfile, subprocess, shutil
from contextlib import closing

from .core import OPENAI_BASE, OPENAI_KEY, jdb
from . import egress

# 不支持的平台（视频号加密流需要 Isaac64 解密，暂不支持）
_UNSUPPORTED_PLATFORMS = {"channels", "weixin", "wechat"}


def gen_breakdown(payload):
    """下载视频 → 抽帧 → ASR → GPT-4o 多模态分析 → 分镜拆解。
    由 run_job 调用，走标准 job 生命周期（扣点/退点/reaper 全自动）。"""
    url = (payload.get("url") or "").strip()
    if not url:
        raise ValueError("请粘贴抖音/小红书/视频号链接")

    import tikhub

    # ① 解析链接
    info = tikhub.parse_link(url)
    platform = (info.get("platform") or "").lower()
    if platform in _UNSUPPORTED_PLATFORMS:
        raise ValueError("视频号暂不支持拆解，请粘贴抖音/小红书链接")

    return _do_breakdown(payload, info, url)


def _do_breakdown(payload, info, url):
    import tikhub

    det = tikhub.detail(info["platform"], info["id"], info.get("note_type"))
    play_url = det.get("play_url")
    if not play_url:
        if det.get("images"):
            raise ValueError("该链接是图文笔记，不是视频，暂不支持拆解")
        raise ValueError("未找到视频下载地址，可能是私密或已删除")
    duration = det.get("duration") or 30
    title = det.get("title") or det.get("desc") or ""

    job_id = payload.get("_job_id")
    _heartbeat(job_id, "downloading")
    tmp_video = None
    frame_dir = None
    tmp_video = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    try:
        dl_deadline = time.time() + 180
        tikhub.download_to_file(play_url, dl_deadline, tmp_video.name)

        _heartbeat(job_id, "extracting_frames")
        frame_count = max(4, min(10, int(duration / 5)))
        frame_dir, frames = _extract_frames(tmp_video.name, frame_count, duration)

        script_text = ""
        try:
            _heartbeat(job_id, "transcribing")
            segs = tikhub.transcript(det, video_path=tmp_video.name)
            script_text = _format_transcript(segs)
        except Exception:
            pass

        _heartbeat(job_id, "analyzing")
        platform = info.get("platform", "")
        usermsg = (
            "视频标题：" + str(title) + "\n"
            "时长：" + str(duration) + "s\n"
            "平台：" + str(platform) + "\n\n"
            "口播文案（带时间轴）：\n" + str(script_text) + "\n\n"
            '请输出 JSON：{"rhythm":[{"phase":"","time":"","strategy":""}],'
            '"scenes":[{"dur":"","scale":"","camera":"","scene":"","line":""}],'
            '"viral_logic":"","template":""}'
        )
        raw = _chat_multimodal(
            "你是黄雀传媒资深短视频编导。分析以下视频的关键帧和口播文案，"
            "拆解出完整分镜脚本。只输出 JSON，不要解释。",
            usermsg, frames
        )

        s, e = raw.find("{"), raw.rfind("}")
        if s < 0 or e <= s:
            raise ValueError("拆解结果解析失败，请重试")
        result = json.loads(raw[s:e+1])

        return {
            "type": "breakdown",
            "source_url": url,
            "source_title": title,
            "source_platform": platform,
            "duration": duration,
            "rhythm": result.get("rhythm", []),
            "scenes": result.get("scenes", []),
            "viral_logic": result.get("viral_logic", ""),
            "template": result.get("template", ""),
        }
    finally:
        if tmp_video:
            try: os.unlink(tmp_video.name)
            except: pass
        if frame_dir:
            try: shutil.rmtree(frame_dir)
            except: pass


# ============ 辅助函数 ============

def _heartbeat(job_id, phase):
    """刷新 updated_at 防止 reaper 误杀 + 写 phase 供前端展示"""
    try:
        now = int(time.time())
        with closing(jdb()) as c:
            row = c.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row:
                p = json.loads(row["payload"] or "{}")
                p["phase"] = phase
                c.execute("UPDATE jobs SET payload=?, updated_at=? WHERE id=?",
                          (json.dumps(p, ensure_ascii=False), now, job_id))
                c.commit()
    except Exception:
        pass


def _format_transcript(segs):
    """兼容 whisper segment 列表和 SRT 字符串"""
    if not segs:
        return ""
    if isinstance(segs, str):
        return segs
    if isinstance(segs, list) and segs:
        if isinstance(segs[0], dict):
            lines = []
            for s in segs:
                start = s.get("start") or s.get("seek") or 0
                end = s.get("end") or 0
                text = s.get("text") or s.get("transcript") or ""
                if str(text).strip():
                    lines.append("[%ss-%ss] %s" % (start, end, str(text).strip()))
            return "\n".join(lines)
    return str(segs)


def _extract_frames(video_path, count=6, duration=30):
    """ffmpeg 抽帧：场景检测 + 均匀采样兜底。返回 (outdir, [paths])"""
    outdir = tempfile.mkdtemp()
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-i", video_path,
         "-vf", "select='gt(scene,0.15)',scale=512:-1",
         "-vsync", "vfr", "-vframes", str(count),
         "%s/frame_%%d.jpg" % outdir],
        check=True, timeout=60,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    frames = sorted([os.path.join(outdir, f) for f in os.listdir(outdir)
                     if f.endswith(".jpg")],
                    key=lambda p: int(os.path.splitext(os.path.basename(p))[0].split("_")[-1]))
    if len(frames) < max(3, count // 2):
        shutil.rmtree(outdir)
        outdir = tempfile.mkdtemp()
        fps = max(float(count) / max(float(duration or 1), 1.0), 0.001)
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-i", video_path,
             "-vf", "fps=%.6f,scale=512:-1" % fps,
             "-vframes", str(count),
             "%s/frame_%%d.jpg" % outdir],
            check=True, timeout=60,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        frames = sorted([os.path.join(outdir, f) for f in os.listdir(outdir)
                         if f.endswith(".jpg")],
                        key=lambda p: int(os.path.splitext(os.path.basename(p))[0].split("_")[-1]))
    return outdir, frames


def _chat_multimodal(sysmsg, usermsg, image_paths, temp=0.7):
    """GPT-4o 多模态：走 egress 代理链，绕过中转站"""
    from .image import OPENAI_OFFICIAL_BASE

    content = [{"type": "text", "text": usermsg}]
    for path in image_paths:
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        content.append({
            "type": "image_url",
            "image_url": {"url": "data:image/jpeg;base64," + b64, "detail": "low"}
        })

    body = {
        "model": os.environ.get("BREAKDOWN_MODEL", "gpt-4o"),
        "messages": [
            {"role": "system", "content": sysmsg},
            {"role": "user", "content": content}
        ],
        "temperature": temp,
    }

    d = egress.post_json(
        OPENAI_OFFICIAL_BASE, OPENAI_BASE,
        "/v1/chat/completions", json.dumps(body, ensure_ascii=False).encode(),
        {"Authorization": "Bearer " + OPENAI_KEY, "Content-Type": "application/json"},
        log=lambda m: print("[breakdown] %s" % m, flush=True)
    )
    return (d.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()


HANDLERS = {"breakdown": gen_breakdown}
