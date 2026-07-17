# -*- coding: utf-8 -*-
"""爆款拆解：竞品视频链接 → 下载 → 抽帧 → ASR → GPT-4o 多模态 → 分镜脚本"""
import os, json, time, base64, tempfile, subprocess, shutil
from contextlib import closing

from .core import OPENAI_BASE, OPENAI_KEY, jdb
from . import egress

# 不支持的平台（视频号加密流需要 Isaac64 解密，暂不支持）
_UNSUPPORTED_PLATFORMS = {"channels", "weixin", "wechat"}
_BREAKDOWN_MODE_SCENES = "scenes"
_BREAKDOWN_MODE_REVERSE_PROMPT = "reverse_prompt"
_BREAKDOWN_SUPPORTED_MODES = {_BREAKDOWN_MODE_SCENES, _BREAKDOWN_MODE_REVERSE_PROMPT}


def gen_breakdown(payload):
    """下载视频 → 抽帧 → ASR → GPT-4o 多模态分析 → 分镜拆解。
    由 run_job 调用，走标准 job 生命周期（扣点/退点/reaper 全自动）。
    支持单个 url 或批量 urls（≤5 条，顺序处理）。"""
    import tikhub

    mode = _normalize_mode(payload)
    urls = payload.get("urls")
    if urls and isinstance(urls, list):
        urls = [u.strip() for u in urls if isinstance(u, str) and u.strip()]
        if not urls:
            raise ValueError("请粘贴抖音/小红书/视频号链接")
        if len(urls) > 5:
            raise ValueError("批量拆解最多 5 条链接")
        return _do_batch_breakdown(payload, urls)

    url = (payload.get("url") or "").strip()
    if not url:
        raise ValueError("请粘贴抖音/小红书/视频号链接")

    # ① 解析链接
    info = tikhub.parse_link(url)
    platform = (info.get("platform") or "").lower()
    if platform in _UNSUPPORTED_PLATFORMS:
        raise ValueError("视频号暂不支持拆解，请粘贴抖音/小红书链接")

    return _do_breakdown(payload, info, url, mode)


def _normalize_mode(payload):
    mode = str((payload or {}).get("mode") or _BREAKDOWN_MODE_SCENES).strip().lower()
    if not mode:
        mode = _BREAKDOWN_MODE_SCENES
    if mode not in _BREAKDOWN_SUPPORTED_MODES:
        raise ValueError("mode 仅支持 scenes / reverse_prompt")
    return mode


def _do_batch_breakdown(payload, urls):
    """批量拆解：逐个处理，收拢结果。"""
    import tikhub

    job_id = payload.get("_job_id")
    results = []
    errors = []
    for idx, url in enumerate(urls):
        _heartbeat(job_id, "batch_%d_%d" % (idx + 1, len(urls)))
        try:
            info = tikhub.parse_link(url)
            platform = (info.get("platform") or "").lower()
            if platform in _UNSUPPORTED_PLATFORMS:
                errors.append({"url": url, "error": "视频号暂不支持"})
                continue
            r = _do_breakdown(payload, info, url)
            results.append(r)
        except ValueError as e:
            errors.append({"url": url, "error": str(e)})
        except Exception as e:
            errors.append({"url": url, "error": "拆解失败：" + str(e)[:200]})

    return {
        "type": "breakdown_batch",
        "results": results,
        "errors": errors,
        "total": len(urls),
    }


def _do_breakdown(payload, info, url, mode=None):
    import tikhub

    mode = mode or _normalize_mode(payload)
    det = tikhub.detail(info["platform"], info["id"], info.get("note_type"))
    play_url = det.get("play_url")
    if not play_url:
        if det.get("images"):
            raise ValueError("该链接是图文笔记，不是视频，暂不支持拆解")
        raise ValueError("未找到视频下载地址，可能是私密或已删除")
    duration = det.get("duration") or 30
    try:
        duration = int(float(duration))
    except Exception:
        duration = 30
    if duration > 1000:
        duration = max(1, round(duration / 1000.0))  # 热修(20260717)：tikhub 返回毫秒，统一转秒
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
        asr_failed = False
        try:
            _heartbeat(job_id, "transcribing")
            segs = tikhub.transcript(det, video_path=tmp_video.name)
            script_text = _format_transcript(segs)
            if _speech_chars(script_text) < 8:
                script_text = ""  # 热修(20260717)：实际口播字数过短≈无人声（纯音乐/歌舞），按无口播处理
        except Exception:
            asr_failed = True

        _heartbeat(job_id, "analyzing")
        platform = info.get("platform", "")
        frame_thumbnails = _frame_thumbnails(frames)
        if mode == _BREAKDOWN_MODE_REVERSE_PROMPT:
            prompt = _reverse_prompt_from_frames(title, duration, platform, script_text, frames)
            return {
                "type": "breakdown_reverse",
                "source_url": url,
                "source_title": title,
                "source_platform": platform,
                "duration": duration,
                "prompt": prompt,
                "frame_count": len(frames or []),
                "frame_thumbnails": frame_thumbnails,
                "asr_failed": asr_failed,
            }

        result = _breakdown_scenes_from_frames(title, duration, platform, script_text, frames)

        return {
            "type": "breakdown",
            "source_url": url,
            "source_title": title,
            "source_platform": platform,
            "duration": duration,
            "scenes": result.get("scenes", []),
            "analysis": result.get("analysis", ""),
            "asr_failed": asr_failed,
            "frame_thumbnails": frame_thumbnails,
        }
    finally:
        if tmp_video:
            try: os.unlink(tmp_video.name)
            except: pass
        if frame_dir:
            try: shutil.rmtree(frame_dir)
            except: pass


# ============ 辅助函数 ============


def _frame_thumbnails(frames, limit=4):
    thumbs = []
    for fp in (frames or [])[:max(0, int(limit or 0))]:
        try:
            with open(fp, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            thumbs.append("data:image/jpeg;base64," + b64)
        except Exception:
            pass
    return thumbs


def _breakdown_source_context(title, duration, platform, script_text):
    return (
        "视频标题：" + str(title) + "\n"
        "时长：" + str(duration) + "s\n"
        "平台：" + str(platform) + "\n\n"
        "口播文案（带时间轴）：\n" + (str(script_text) if script_text else "（无人物口播或转写不可用，请根据画面帧判断内容）")
    )


def _breakdown_scenes_from_frames(title, duration, platform, script_text, frames):
    usermsg = (
        _breakdown_source_context(title, duration, platform, script_text) + "\n\n"
        "请严格输出 JSON：{\"scenes\":[{\"dur\":\"3s\",\"scene\":\"画面描述(10字内)\",\"line\":\"口播台词\"}],"
        "\"analysis\":\"视频内容综合分析(含视频主题、背景、构图运镜、人物特征、产品细节、情绪氛围、字幕建议等)\"}，"
        "只输出 JSON 本身，不要解释、不要 markdown 代码块。"
        "每个 scene 一句话说清画面，line 是原视频对应的口播内容。"
        "若原视频没有人物口播（纯音乐/歌舞/背景乐），或上方口播文案实为歌词、听写乱码、与画面无关的内容，"
        "所有 line 输出空串\"\"，不要编造台词。"
    )
    sysmsg = (
        "你是黄雀传媒资深短视频编导。分析视频关键帧和口播，拆解为简洁的分镜脚本，同时输出一份视频内容综合分析。"
        "只输出 JSON，不要多余内容。"
    )
    raw = _chat_multimodal(sysmsg, usermsg, frames)
    try:
        return _parse_breakdown_json(raw)
    except ValueError:
        print("[breakdown] parse failed, raw(%d)=%s" % (len(raw or ""), str(raw)[:400].replace("\n", " ")))
        raw = _chat_multimodal(sysmsg, usermsg, frames)
        return _parse_breakdown_json(raw)


def _reverse_prompt_from_frames(title, duration, platform, script_text, frames):
    usermsg = (
        _breakdown_source_context(title, duration, platform, script_text) + "\n\n"
        "请基于关键帧和口播，反推出一条适合后续作图/创作的中文提示词。"
        "目标不是逐字复刻原视频，而是保留它的主体设定、镜头语言、场景道具、光线氛围、色调质感、构图与文案钩子，"
        "用于生成同风格但原创的新内容。"
        "直接输出 1 条完整提示词，80-160 字，不要 JSON、不要标题、不要解释、不要 markdown 代码块。"
    )
    sysmsg = (
        "你是黄雀传媒资深短视频创意总监。你擅长根据视频关键帧和口播，提炼出可直接用于后续创作的中文提示词。"
        "只输出提示词本身，不要任何多余内容。"
    )
    raw = _chat_multimodal(sysmsg, usermsg, frames, temp=0.6)
    try:
        return _clean_reverse_prompt(raw)
    except ValueError:
        print("[breakdown] reverse prompt parse failed, raw(%d)=%s" % (len(raw or ""), str(raw)[:400].replace("\n", " ")))
        raw = _chat_multimodal(sysmsg, usermsg, frames, temp=0.6)
        return _clean_reverse_prompt(raw)


def _clean_reverse_prompt(raw):
    text = str(raw or "").replace("\r", "").strip()
    if text.startswith("```"):
        lines = [line for line in text.splitlines() if not line.strip().startswith("```")]
        text = "\n".join(lines).strip()
    text = " ".join(line.strip() for line in text.splitlines() if line.strip()).strip().strip('"“”')
    if not text:
        raise ValueError("反推结果解析失败，请重试")
    return text[:600]


def _strip_json_code_fence(raw):
    text = str(raw or "").strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if len(lines) < 3:
        return text
    first = lines[0].strip().lower()
    last = lines[-1].strip()
    if not last.startswith("```"):
        return text
    if first not in ("```", "```json"):
        return text
    return "\n".join(lines[1:-1]).strip()


def _iter_json_objects(raw):
    """扫描文本中所有 JSON 对象。超长输入跳过扫描直接返回空（防 O(n²) 卡死）。"""
    text = str(raw or "")
    n = len(text)
    if n > 50000:   # 超长文本不逐字符扫描，交给外层 json.loads 直接试
        return
    for start in range(n):
        if text[start] != "{":
            continue
        depth = 0
        in_str = False
        escape = False
        for i in range(start, n):
            ch = text[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    yield text[start:i + 1]
                    break


def _parse_breakdown_json(raw):
    candidates = []
    seen = set()
    for candidate in (str(raw or "").strip(), _strip_json_code_fence(raw)):
        if candidate and candidate not in seen:
            candidates.append(candidate)
            seen.add(candidate)
    for candidate in list(candidates):
        for obj in _iter_json_objects(candidate):
            if obj not in seen:
                candidates.append(obj)
                seen.add(obj)
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except Exception:
            pass
    raise ValueError("拆解结果解析失败，请重试")


def _heartbeat(job_id, phase):
    """刷新 updated_at 防止 reaper 误杀 + 写 _hb_phase 供前端展示（用前缀防与用户 payload 字段冲突）"""
    try:
        now = int(time.time())
        with closing(jdb()) as c:
            row = c.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row:
                p = json.loads(row["payload"] or "{}")
                p["_hb_phase"] = phase
                c.execute("UPDATE jobs SET payload=?, updated_at=? WHERE id=?",
                          (json.dumps(p, ensure_ascii=False), now, job_id))
                c.commit()
    except Exception:
        pass


def _speech_chars(transcript_text):
    """量转写里的实际口播字数（剥掉 [0s-3s] 时间轴标记），过短≈无人声"""
    import re as _re
    return len(_re.sub(r"\[[^\]]*\]", "", transcript_text or "").strip())


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
    count = max(2, min(count, 12))  # 限制 2-12 帧，防止异常参数
    outdir = tempfile.mkdtemp()
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-i", video_path,
             "-vf", "select='gt(scene,0.15)',scale=512:-1",
             "-vsync", "vfr", "-vframes", str(count),
             "%s/frame_%%d.jpg" % outdir],
            check=True, timeout=60,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError:
        pass  # 场景检测失败 → 退到均匀采样
    frames = sorted([os.path.join(outdir, f) for f in os.listdir(outdir)
                     if f.endswith(".jpg")],
                    key=lambda p: int(os.path.splitext(os.path.basename(p))[0].split("_")[-1]))
    if len(frames) < max(2, count // 2):
        shutil.rmtree(outdir)
        outdir = tempfile.mkdtemp()
        fps = max(float(count) / max(float(duration or 1), 1.0), 0.001)
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                 "-i", video_path,
                 "-vf", "fps=%.6f,scale=512:-1" % fps,
                 "-vframes", str(count),
                 "%s/frame_%%d.jpg" % outdir],
                check=True, timeout=60,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except subprocess.CalledProcessError:
            pass  # 均匀采样也失败 → 返回已有帧（可能 0 张，GPT-4o 仍可纯文本分析）
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
