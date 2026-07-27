# -*- coding: utf-8 -*-
"""爆款拆解：竞品视频链接 → 下载 → 抽帧 → ASR → GPT-4o 多模态 → 分镜脚本"""
import os, json, time, base64, tempfile, subprocess, shutil, mimetypes
from contextlib import closing

from .core import OPENAI_BASE, OPENAI_KEY, jdb
from . import egress

# 不支持的平台（视频号加密流需要 Isaac64 解密，暂不支持）
_UNSUPPORTED_PLATFORMS = {"channels", "weixin", "wechat"}
_UPLOAD_TOKEN_RE = __import__("re").compile(r"^[0-9a-f]{32}$")


def _ensure_upload_table(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS breakdown_uploads(
        token TEXT PRIMARY KEY,
        username TEXT NOT NULL,
        suffix TEXT NOT NULL,
        job_id INTEGER NOT NULL UNIQUE,
        created_at INTEGER NOT NULL
    )""")


def _upload_root():
    from . import core
    root = (core.OUT_DIR / "_breakdown_uploads").resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def handle_local_upload(handler, user):
    """Validate a raw local-media upload, charge once, and enqueue a breakdown job."""
    from . import core
    _, points_domain, _ = core._domains()
    try:
        core.feature_flags.require_enabled("breakdown")
    except core.feature_flags.FeatureDisabled as exc:
        return handler._send(503, {"detail": str(exc)})
    if core.is_shutting_down():
        return handler._send(503, {
            "detail": "服务正在更新，请稍后重试", "code": "shutting_down",
            "retry_after_ms": 5000,
        })

    query = core.urllib.parse.parse_qs(core.urllib.parse.urlparse(handler.path).query)
    media_type = str((query.get("media_type") or [""])[0]).strip().lower()
    allowed = {
        "image": {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"},
        "video": {"video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm"},
    }
    content_type = str(handler.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    if media_type not in allowed or content_type not in allowed[media_type]:
        return handler._send(415, {"detail": "仅支持 JPG/PNG/WEBP 图片或 MP4/MOV/WEBM 视频"})
    try:
        content_length = int(handler.headers.get("Content-Length") or 0)
    except (TypeError, ValueError):
        content_length = 0
    maximum = 20 * 1024 * 1024 if media_type == "image" else 200 * 1024 * 1024
    if content_length <= 0 or content_length > maximum:
        return handler._send(413, {"detail": "图片最大 20MB，视频最大 200MB"})
    active_jobs = core._user_active_job_count(user["username"])
    if active_jobs >= core.MAX_USER_ACTIVE_JOBS:
        return handler._send(429, {
            "detail": "当前生成任务较多，请完成后再提交", "code": "active_job_cap",
            "active_jobs": active_jobs, "max_active_jobs": core.MAX_USER_ACTIVE_JOBS,
            "retry_after_ms": 4000,
        })

    temp_path = ""
    upload_token = __import__("uuid").uuid4().hex
    suffix = allowed[media_type][content_type]
    try:
        root = _upload_root()
        temp_path = str(root / (upload_token + suffix))
        with open(temp_path, "xb") as uploaded:
            remaining = content_length
            while remaining:
                chunk = handler.rfile.read(min(65536, remaining))
                if not chunk:
                    raise ValueError("上传文件读取不完整")
                uploaded.write(chunk)
                remaining -= len(chunk)
        with open(temp_path, "rb") as uploaded:
            signature = uploaded.read(16)
        valid_signature = {
            "image/jpeg": signature.startswith(b"\xff\xd8\xff"),
            "image/png": signature.startswith(b"\x89PNG\r\n\x1a\n"),
            "image/webp": signature.startswith(b"RIFF") and signature[8:12] == b"WEBP",
            "video/mp4": len(signature) >= 12 and signature[4:8] == b"ftyp",
            "video/quicktime": len(signature) >= 12 and signature[4:8] == b"ftyp",
            "video/webm": signature.startswith(b"\x1a\x45\xdf\xa3"),
        }[content_type]
        if not valid_signature:
            raise ValueError("文件内容与声明格式不一致")
        body = {"upload_token": upload_token, "media_type": media_type, "mode": "reverse_prompt"}
        cost = points_domain.cost_of("breakdown", body)
        with core._submission_lock:
            with closing(core.jdb()) as connection:
                _ensure_upload_table(connection)
                connection.commit()
            def record_upload(connection, job_id):
                _ensure_upload_table(connection)
                connection.execute(
                    "INSERT INTO breakdown_uploads(token,username,suffix,job_id,created_at)"
                    " VALUES(?,?,?,?,?)",
                    (upload_token, user["username"], suffix, job_id, int(time.time())),
                )
            job_id, points_left = core.jobs_store.create_paid_job(
                core.jdb, points_domain.deduct_points, points_domain.refund_points,
                "breakdown", user["username"], cost, body, core.SERVICE_OWNER,
                before_commit=record_upload,
            )
            if not core.enqueue_job(job_id, "breakdown", "reverse_prompt"):
                core._reject_pending_job(job_id, user["username"], cost, "任务队列已满，请稍后再试")
                _remove_trusted_upload(upload_token, user["username"], job_id, temp_path)
                return handler._send(429, {
                    "detail": "任务队列已满，请稍后再试", "code": "queue_full",
                    "retry_after_ms": 4000,
                })
        return handler._send(200, {"job_id": job_id, "cost": cost, "points_left": points_left})
    except points_domain.AuthPointsError as exc:
        _remove_upload(temp_path)
        return handler._send(
            exc.status if exc.status in (402, 403) else 502,
            points_domain.public_error_body(exc, 20),
        )
    except core.jobs_store.PaidJobInsertError as exc:
        _remove_upload(temp_path)
        return handler._send(500, {
            "detail": "任务创建失败，点数已退回", "submission_ref": exc.submission_ref,
        })
    except Exception as exc:
        _remove_upload(temp_path)
        return handler._send(400, {"detail": str(exc)[:180]})


def _remove_upload(path):
    if path:
        try: os.unlink(path)
        except Exception: pass


def gen_breakdown(payload):
    """下载视频 → 抽帧 → ASR → GPT-4o 多模态分析 → 分镜拆解。
    由 run_job 调用，走标准 job 生命周期（扣点/退点/reaper 全自动）。"""
    upload_token = str(payload.get("upload_token") or "").strip().lower()
    if upload_token:
        return _do_local_reverse(payload, upload_token)
    if payload.get("local_path"):
        raise ValueError("禁止提交服务器本地路径")

    urls = payload.get("urls")
    if isinstance(urls, list):
        cleaned = [str(url).strip() for url in urls if str(url).strip()][:5]
        if not cleaned:
            raise ValueError("请至少提供一个视频链接")
        results, errors = [], []
        for index, item_url in enumerate(cleaned, 1):
            _heartbeat(payload.get("_job_id"), "batch_%d_%d" % (index, len(cleaned)))
            try:
                item_payload = dict(payload, url=item_url)
                item_payload.pop("urls", None)
                results.append(gen_breakdown(item_payload))
            except Exception as exc:
                errors.append({"url": item_url, "detail": str(exc)[:200]})
        return {
            "type": "breakdown_batch",
            "total": len(cleaned),
            "results": results,
            "errors": errors,
        }

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
        if payload.get("mode") == "reverse_prompt":
            return _reverse_from_frames(payload, frames, url, title, platform, duration)

        context = (
            "视频标题：" + str(title) + "\n"
            "时长：" + str(duration) + "s\n"
            "平台：" + str(platform) + "\n\n"
            "口播文案（带时间轴）：\n"
            + (str(script_text) if script_text else "（无人物口播或转写不可用，请根据画面判断）")
        )
        usermsg = context + (
            '\n\n请严格输出 JSON：{"rhythm":[{"phase":"","time":"","strategy":""}],'
            '"scenes":[{"dur":"3s","scale":"","camera":"","scene":"详细画面描述(60-100字)",'
            '"line":"口播台词"}],"viral_logic":"","template":""}。'
            "输出 4-6 个分镜，各 dur 之和约等于总时长。每个 scene 必须结合关键帧，至少写清："
            "主体外观或产品特征、连续动作及道具互动、表情视线和身体姿态、场景前中后景关系、"
            "景别机位与运镜、光线色调和氛围。不要写“人物出现”“展示产品”等笼统结论。"
            "没有人物口播时 line 输出空串。只输出 JSON，不要解释或 markdown。"
        )
        sysmsg = (
            "你是黄雀传媒资深短视频编导。分析视频关键帧和口播，拆解为可直接拍摄或生成的完整分镜脚本。"
            "只输出 JSON，不要多余内容。"
        )
        result = _request_breakdown_result(sysmsg, usermsg, context, frames)

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
            "frame_thumbnails": _frame_thumbnails(frames),
        }
    finally:
        if tmp_video:
            try: os.unlink(tmp_video.name)
            except: pass
        if frame_dir:
            try: shutil.rmtree(frame_dir)
            except: pass


def _reverse_from_frames(payload, frames, source_url="", title="", platform="", duration=0):
    raw = _chat_multimodal(
        "你是黄雀传媒资深短视频复刻编导。根据连续关键帧恢复镜头时间轴、动作节点与空间连续性，"
        "写成视频生成模型可执行的中文提示词。只输出 JSON，不要解释或 markdown。",
        (
            "请严格按照参考画面的时间顺序，输出 JSON："
            "{\"prompt\":\"一段完整、可直接用于视频生成的中文执行提示词\"}。"
            "提示词必须按总时长写连续时间轴，逐段说明主体、动作起止、景别、机位、运镜、构图和转场；"
            "写清场景道具、光线色调、节奏和情绪钩子。仅补充连接相邻关键帧所需的过渡动作，"
            "不得新增人物、道具、镜头或无关情节；人物具体身份、面部和不可确认的品牌文字不得臆造。"
        ),
        frames,
    )
    prompt = str((_parse_breakdown_json(raw) or {}).get("prompt") or "").strip()
    if not prompt:
        raise ValueError("提示词反推结果为空，请重试")
    return {
        "type": "breakdown_reverse",
        "source_url": source_url,
        "source_title": title,
        "source_platform": platform,
        "duration": duration,
        "prompt": prompt,
        "frame_thumbnails": _frame_thumbnails(frames),
    }


def _do_local_reverse(payload, upload_token):
    media_type = str(payload.get("media_type") or "").strip().lower()
    job_id = payload.get("_job_id")
    username = str(payload.get("_username") or "").strip()
    if media_type not in {"image", "video"}:
        raise ValueError("不支持的本地素材类型")
    if not _UPLOAD_TOKEN_RE.fullmatch(upload_token) or not username or not job_id:
        raise ValueError("无效的上传凭证")
    from . import core
    with closing(core.jdb()) as connection:
        _ensure_upload_table(connection)
        row = connection.execute(
            "SELECT suffix FROM breakdown_uploads WHERE token=? AND username=? AND job_id=?",
            (upload_token, username, int(job_id)),
        ).fetchone()
        connection.commit()
    if not row:
        raise ValueError("上传凭证不存在或不属于当前任务")
    root = _upload_root()
    candidate = (root / (upload_token + str(row["suffix"]))).resolve()
    if candidate.parent != root or not candidate.is_file():
        raise ValueError("上传文件不存在或已过期")
    path = str(candidate)
    frame_dir = None
    try:
        _heartbeat(job_id, "extracting_frames")
        if media_type == "image":
            frames = [path]
            duration = 0
        else:
            duration = _probe_duration(path)
            if duration > 120.05:
                raise ValueError("视频最长支持 2 分钟")
            frame_dir, frames = _extract_frames(path, 8, duration or 30)
        _heartbeat(job_id, "analyzing")
        return _reverse_from_frames(
            payload, frames, "", os.path.basename(path), "local", duration,
        )
    finally:
        if frame_dir:
            try: shutil.rmtree(frame_dir)
            except Exception: pass
        _remove_trusted_upload(upload_token, username, job_id, path)


def _probe_duration(path):
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            check=True, timeout=20, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        )
        return max(0.0, float((proc.stdout or "0").strip() or 0))
    except Exception:
        raise ValueError("无法读取视频时长，请上传有效的视频文件")


def _remove_trusted_upload(token, username, job_id, path):
    from . import core
    try:
        with closing(core.jdb()) as connection:
            _ensure_upload_table(connection)
            connection.execute(
                "DELETE FROM breakdown_uploads WHERE token=? AND username=? AND job_id=?",
                (token, username, int(job_id)),
            )
            connection.commit()
    finally:
        root = _upload_root()
        candidate = __import__("pathlib").Path(path).resolve()
        if candidate.parent == root:
            try: candidate.unlink()
            except Exception: pass


# ============ 辅助函数 ============

def _frame_thumbnails(frames, limit=4):
    thumbs = []
    for path in (frames or [])[:max(0, int(limit or 0))]:
        try:
            with open(path, "rb") as source:
                encoded = base64.b64encode(source.read()).decode()
            media_type = mimetypes.guess_type(path)[0] or "image/jpeg"
            if media_type not in {"image/jpeg", "image/png", "image/webp"}:
                media_type = "image/jpeg"
            thumbs.append("data:" + media_type + ";base64," + encoded)
        except Exception:
            pass
    return thumbs


def _strip_json_code_fence(raw):
    text = str(raw or "").strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].strip().lower() in {"```", "```json"}:
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _iter_json_objects(raw):
    text = str(raw or "")
    if len(text) > 50000:
        return
    for start, opening in enumerate(text):
        if opening != "{":
            continue
        depth, in_string, escaped = 0, False, False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    yield text[start:index + 1]
                    break


def _parse_breakdown_json(raw):
    candidates, seen = [], set()
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


def _validate_scene_breakdown(result):
    if not isinstance(result, dict) or not isinstance(result.get("scenes"), list):
        raise ValueError("拆解结果为空，请重试")
    placeholders = ("画面描述", "具体画面", "口播台词", "对应口播")
    valid = []
    for scene in result["scenes"]:
        if not isinstance(scene, dict):
            continue
        scene_text = str(scene.get("scene") or "").strip()
        line_text = str(scene.get("line") or "").strip()
        if not scene_text:
            continue
        if any(marker in scene_text or marker in line_text for marker in placeholders):
            raise ValueError("拆解结果包含模板占位内容，请重试")
        valid.append(scene)
    if not valid:
        raise ValueError("拆解结果为空，请重试")
    result["scenes"] = valid
    return result


def _request_breakdown_result(sysmsg, usermsg, context, frames):
    attempts = [
        (usermsg, 0.2),
        (
            context + '\n\n上一次输出不完整。请只返回闭合、可解析的 JSON，固定输出 4 个有效分镜；'
            '每个 scene 50-80 字，写清主体特征、连续动作、场景道具、构图运镜和光影氛围，'
            '禁止代码围栏、解释和模板占位文字。',
            0.1,
        ),
    ]
    last_error = None
    for index, (message, temperature) in enumerate(attempts, 1):
        raw = _chat_multimodal(sysmsg, message, frames, temp=temperature)
        try:
            return _validate_scene_breakdown(_parse_breakdown_json(raw))
        except ValueError as error:
            last_error = error
            print(
                "[breakdown] attempt %d invalid output: %s raw(%d)=%s"
                % (index, error, len(raw or ""), str(raw)[:400].replace("\n", " ")),
                flush=True,
            )
    raise last_error or ValueError("拆解结果解析失败，请重试")

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
        media_type = mimetypes.guess_type(path)[0] or "image/jpeg"
        if media_type not in {"image/jpeg", "image/png", "image/webp"}:
            media_type = "image/jpeg"
        content.append({
            "type": "image_url",
            "image_url": {"url": "data:" + media_type + ";base64," + b64, "detail": "low"}
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
