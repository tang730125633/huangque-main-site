# -*- coding: utf-8 -*-
"""爆款拆解：竞品视频链接 → 下载 → 抽帧 → ASR → GLM-4V 多模态 → 分镜脚本"""
import os, json, time, base64, tempfile, subprocess, shutil, mimetypes, io
import http.client
import re
import socket
import ssl
import urllib.error
import urllib.parse
from contextlib import closing

from .core import jdb
from . import egress

ZHIPU_API_BASE = "https://open.bigmodel.cn/api/paas/v4"
ZHIPU_API_KEY = (os.environ.get("REVERSE_ZHIPU_KEY") or "").strip()
ZHIPU_MODEL = (os.environ.get("REVERSE_ZHIPU_MODEL") or "glm-4v-plus").strip()
BREAKDOWN_DOWNLOAD_BUDGET = max(
    30, int(os.environ.get("BREAKDOWN_DOWNLOAD_BUDGET", "180") or "180")
)
BREAKDOWN_MAX_DOWNLOAD_BYTES = max(
    25 * 1024 * 1024,
    int(os.environ.get("BREAKDOWN_MAX_DOWNLOAD_BYTES", str(200 * 1024 * 1024))
        or str(200 * 1024 * 1024)),
)

# 不支持的平台（视频号加密流需要 Isaac64 解密，暂不支持）
_UNSUPPORTED_PLATFORMS = {"channels", "weixin", "wechat"}
_SUPPORTED_LINK_HOSTS = (
    "douyin.com", "iesdouyin.com", "xiaohongshu.com", "xhslink.com",
)
_SHARE_URL_RE = re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+")
_UPLOAD_TOKEN_RE = __import__("re").compile(r"^[0-9a-f]{32}$")
_THUMBNAIL_MAX_EDGE = 768
_THUMBNAIL_MAX_BYTES = 240 * 1024
_THUMBNAIL_MAX_PIXELS = 40_000_000
_AI_FRAME_MAX_EDGE = 640
_AI_FRAME_MAX_BYTES = 128 * 1024
_AI_FRAMES_TOTAL_MAX_BYTES = 1024 * 1024
_AI_MAX_FRAMES = 4


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


def _normalize_supported_link(value):
    match = _SHARE_URL_RE.search(str(value or ""))
    if not match:
        raise ValueError("请粘贴抖音或小红书的完整 http(s) 分享链接")
    url = match.group(0)
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in {"http", "https"} or not any(
            host == suffix or host.endswith("." + suffix)
            for suffix in _SUPPORTED_LINK_HOSTS):
        raise ValueError("仅支持抖音或小红书公开视频链接")
    return url


def _resolved_link(url):
    """Resolve a supported share URL before charging and validate its work ID."""
    import tikhub

    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    path = parsed.path or "/"
    expected_platform = "xhs" if (
        host == "xhslink.com" or host.endswith(".xhslink.com")
        or host == "xiaohongshu.com" or host.endswith(".xiaohongshu.com")
    ) else "douyin"

    if expected_platform == "douyin":
        direct = re.search(r"/video/(\d{15,21})(?:/|$)", path)
        if direct:
            info = {
                "platform": "douyin",
                "id": direct.group(1),
                "note_type": "video",
            }
        elif not (
                host == "v.douyin.com" or host.endswith(".v.douyin.com")):
            raise ValueError("抖音链接缺少具体作品 ID")
        else:
            try:
                info = tikhub.parse_link(url)
            except Exception as exc:
                raise ValueError("抖音短链无法解析，请确认链接公开且未失效") from exc
    else:
        direct = re.search(
            r"/(?:explore|discovery/item|item)/([0-9a-fA-F]{16,64})(?:/|$)",
            path,
        )
        if direct:
            info = {
                "platform": "xhs",
                "id": direct.group(1),
                "note_type": None,
            }
        elif not (host == "xhslink.com" or host.endswith(".xhslink.com")):
            raise ValueError("小红书链接缺少具体笔记 ID")
        else:
            try:
                info = tikhub.parse_link(url)
            except Exception as exc:
                raise ValueError("小红书短链无法解析，请确认链接公开且未失效") from exc

    if not isinstance(info, dict):
        raise ValueError("无法解析该视频链接，请确认链接公开且未失效")
    platform = str(info.get("platform") or "").strip().lower()
    work_id = str(info.get("id") or "").strip()
    valid_id = (
        platform == "douyin" and bool(re.fullmatch(r"\d{15,21}", work_id))
    ) or (
        platform == "xhs" and bool(re.fullmatch(r"[0-9a-fA-F]{16,64}", work_id))
    )
    if platform != expected_platform or not valid_id:
        raise ValueError("无法解析该视频链接，请确认链接公开且未失效")
    return {
        "url": url,
        "platform": platform,
        "id": work_id,
        "note_type": info.get("note_type"),
    }


def validate_breakdown_payload(payload):
    """在扣点和入队前完成链接及作品 ID 校验。"""
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象")
    if payload.get("local_path") or payload.get("upload_token"):
        raise ValueError("本地素材只能通过专用上传接口提交")
    body = dict(payload)
    body.pop("_resolved_link", None)
    body.pop("_resolved_links", None)
    mode = str(body.get("mode") or "scenes").strip().lower()
    if mode not in {"scenes", "reverse_prompt"}:
        raise ValueError("不支持的拆解模式")
    raw_urls = body.get("urls")
    if isinstance(raw_urls, list):
        if not raw_urls:
            raise ValueError("请至少提供一个视频链接")
        if len(raw_urls) > 5:
            raise ValueError("一次最多提交 5 条链接")
        urls = [_normalize_supported_link(item) for item in raw_urls]
        if mode == "reverse_prompt" and len(urls) != 1:
            raise ValueError("提示词反推暂仅支持单条视频链接")
        body.pop("url", None)
        body["urls"] = urls
        body["_resolved_links"] = [_resolved_link(url) for url in urls]
    else:
        body["url"] = _normalize_supported_link(body.get("url"))
        body.pop("urls", None)
        body["_resolved_link"] = _resolved_link(body["url"])
    body["mode"] = mode
    return body


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
        resolved_links = payload.get("_resolved_links")
        for index, item_url in enumerate(cleaned, 1):
            _heartbeat(payload.get("_job_id"), "batch_%d_%d" % (index, len(cleaned)))
            try:
                item_payload = dict(payload, url=item_url)
                item_payload.pop("urls", None)
                item_payload.pop("_resolved_links", None)
                if (
                    isinstance(resolved_links, list)
                    and len(resolved_links) == len(cleaned)
                ):
                    item_payload["_resolved_link"] = resolved_links[index - 1]
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

    # 新任务使用扣点前保存的解析结果；兼容部署前已经排队的旧任务。
    resolved = payload.get("_resolved_link")
    if isinstance(resolved, dict) and resolved.get("url") == url:
        info = {
            "platform": resolved.get("platform"),
            "id": resolved.get("id"),
            "note_type": resolved.get("note_type"),
        }
    else:
        info = tikhub.parse_link(url)
    platform = (info.get("platform") or "").lower()
    if platform in _UNSUPPORTED_PLATFORMS:
        raise ValueError("视频号暂不支持拆解，请粘贴抖音/小红书链接")
    if not info.get("id"):
        raise ValueError("无法解析该视频链接，请确认链接公开且未失效")

    return _do_breakdown(payload, info, url)


def _do_breakdown(payload, info, url):
    import tikhub

    det = tikhub.detail(info["platform"], info["id"], info.get("note_type"))
    if det.get("images"):
        raise ValueError("该链接是图文笔记，不是视频，暂不支持拆解")
    job_id = payload.get("_job_id")
    _heartbeat(job_id, "downloading")
    tmp_video = None
    frame_dir = None
    tmp_video = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    try:
        det = _download_breakdown_video(tikhub, info, det, tmp_video.name)
        duration = _normalize_duration_seconds(det.get("duration"), fallback=30)
        title = det.get("title") or det.get("desc") or ""

        _heartbeat(job_id, "extracting_frames")
        # TikHub 的抖音 duration 有时是毫秒（如 10034），有时是秒。
        # 下载后的媒体时长才是抽帧的权威值，避免把 10 秒误当成 10034 秒，
        # 导致均匀采样间隔超过整段视频并向视觉模型发送 0 张关键帧。
        probed_duration = _probe_duration(tmp_video.name)
        if probed_duration > 0:
            duration = probed_duration
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
            return _reverse_from_frames(
                payload, frames, url, title, platform, duration,
                script_text=script_text,
            )

        context = (
            "视频标题：" + str(title) + "\n"
            "时长：" + str(duration) + "s\n"
            "平台：" + str(platform) + "\n\n"
            "口播文案（带时间轴）：\n"
            + (str(script_text) if script_text else "（无人物口播或转写不可用，请根据画面判断）")
        )
        usermsg = context + (
            '\n\n请严格输出 JSON：{"rhythm":[{"phase":"","time":"","strategy":""}],'
            '"scenes":[{"dur":"3s","scale":"","camera":"","scene":"详细画面描述(80-140字)",'
            '"line":"口播台词"}],"viral_logic":"","template":""}。'
            "输出 4-6 个分镜，各 dur 之和约等于总时长。每个 scene 必须结合关键帧，至少写清："
            "主体可见外观或产品特征、动作起点—过程—终点及道具互动、表情视线和身体姿态、"
            "场景前中后景与主体相对位置、景别机位、镜头运动的起止路线、构图、光线方向、"
            "色温色调、画面质感、环境音/音效、与前后镜的动作或视线衔接。"
            "每个 scene 写 80-140 字，形成可直接拍摄或输入视频生成模型的执行指令；"
            "不要写“人物出现”“展示产品”“镜头切换”等笼统结论。"
            "所有字段必须使用简体中文；没有人物口播时 line 输出空串。"
            "只输出 JSON，不要解释或 markdown。"
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


def _download_breakdown_video(tikhub, info, detail, destination):
    """轮换 CDN 播放地址；全部失败时刷新一次详情后再试。"""
    current = detail
    retryable = (
        TimeoutError,
        ConnectionError,
        urllib.error.URLError,
        http.client.IncompleteRead,
    )
    last_error = None
    for refresh_attempt in range(2):
        alternate_urls = current.get("play_urls")
        if not isinstance(alternate_urls, (list, tuple)):
            alternate_urls = []
        play_urls = list(dict.fromkeys(
            [url for url in alternate_urls if url]
            + ([current.get("play_url")] if current.get("play_url") else [])
        ))[:4]
        if not play_urls:
            if current.get("images"):
                raise ValueError("该链接是图文笔记，不是视频，暂不支持拆解")
            if refresh_attempt:
                raise ValueError("未找到视频下载地址，可能是私密或已删除")
            current = tikhub.detail(
                info["platform"], info["id"], info.get("note_type"), fresh=True
            )
            continue
        for play_index, play_url in enumerate(play_urls, 1):
            try:
                tikhub.download_to_file(
                    play_url, time.time() + BREAKDOWN_DOWNLOAD_BUDGET, destination,
                    max_bytes=BREAKDOWN_MAX_DOWNLOAD_BYTES,
                )
                current["play_url"] = play_url
                return current
            except ValueError as error:
                last_error = error
                if play_index >= len(play_urls):
                    raise
                print(
                    "[breakdown] 视频下载地址 %d/%d 超限，尝试备用地址: %s"
                    % (play_index, len(play_urls), str(error)[:160]),
                    flush=True,
                )
            except retryable as error:
                last_error = error
                print(
                    "[breakdown] 视频下载地址 %d/%d 失败: %s"
                    % (play_index, len(play_urls), str(error)[:160]),
                    flush=True,
                )
        if refresh_attempt == 0:
            current = tikhub.detail(
                info["platform"], info["id"], info.get("note_type"), fresh=True
            )
    if isinstance(last_error, ValueError):
        raise last_error
    if last_error is not None:
        raise TimeoutError(
            "视频源下载过慢或地址已失效，切换地址并刷新地址后重试仍失败"
        ) from last_error
    raise RuntimeError("视频下载重试状态异常")


def _reverse_from_frames(
    payload, frames, source_url="", title="", platform="", duration=0,
    script_text="",
):
    duration = max(0.0, float(duration or 0))
    duration_text = ("%.3f" % duration).rstrip("0").rstrip(".")
    if duration > 0:
        source_context = (
            "视频标题：%s\n平台：%s\n总时长：%s 秒\n口播时间轴：\n%s\n\n"
            % (
                str(title or "（无）"),
                str(platform or "（未知）"),
                duration_text,
                str(script_text or "（无可靠口播，请仅依据可见画面）"),
            )
        )
    else:
        source_context = (
            "素材名称：%s\n素材类型：静态图片\n\n"
            % str(title or "（无）")
        )
    if duration > 0:
        sysmsg = (
            "你是黄雀传媒资深短视频复刻编导。程序已经负责时间轴，你只负责依据连续关键帧"
            "为每个既定分段撰写详细、可执行的中文画面内容。不得输出时间范围或重新划分分段。"
            "严格区分可见事实与不确定信息，不臆造身份、品牌文字、人物、道具或情节。"
            "只输出 JSON，不要解释或 markdown。"
        )
        timeline_ranges = _fixed_reverse_ranges(duration)
        usermsg = (
            source_context + "程序已经固定好时间轴，不要输出或改写任何时间范围。"
            "请重新观察全部参考图片，直接输出一个 JSON 对象；这个对象只能有 segments 字段。"
            "segments 必须是长度恰好为 %d 的对象数组，也就是一共有 %d 个分段对象。"
            "每个对象必须同时包含 subject、action、scene、camera、light、audio 六个字符串字段，"
            "六个字段属于同一个分段对象，绝对不能拆成六个数组元素。"
            "禁止写示例文字，禁止只写时间码、序号或占位符。"
            "分段对象依次对应这些时间区间（区间仅用于理解画面顺序，绝对不要写进字段中）：%s。"
            "全部内容必须达到 300-600 个中文字符，每段必须达到 80-150 字，少于要求会被拒绝。"
            "subject 写主体与位置，action 写动作与表情，scene 写场景空间，"
            "camera 写镜头构图，light 写光线质感，audio 写声音字幕。"
            "subject、scene、light 各写至少15字，action 至少25字，camera 至少20字；"
            "action 要写起点、连续过程、终点及道具互动，"
            "camera 要写景别、机位高度、视角和运镜起止路线。"
            "subject、action、scene、camera、light 五个视觉字段都必须依据画面填写具体可见事实，"
            "不得把“未确认”“无”“没有”“略”“待补充”“占位”或“内容”作为整个字段。"
            "没有明显动作时，action 要写清主体保持的姿态和“未观察到明显动作变化”；"
            "没有明显运镜时，camera 要写清景别、视角、构图和“固定镜头，无明显运镜”；"
            "光线方向不明显时，light 仍要描述整体明暗、色调和对比关系。"
            "audio 只写可确认的声音、口播或字幕摘要，最多40个有效字符；"
            "不得把画面中的长段文字、文章或整屏字幕逐字复制到 audio。"
            "身份、品牌文字、具体地点等无法确认的信息不得猜测，可以在可见事实之后说明无法确认。"
            "仅补充连接相邻关键帧必需的过渡动作，不得臆造人物、道具或情节。"
            "不得用“略”“待补充”“内容”等占位词，不得复制同一段内容。"
            % (
                len(timeline_ranges),
                len(timeline_ranges),
                "、".join(timeline_ranges),
            )
        )
    else:
        sysmsg = (
            "你是黄雀传媒资深视觉编导。根据参考图片写成图像生成模型可执行的中文提示词。"
            "严格区分可见事实与不确定信息，不臆造身份、品牌文字、人物、道具或情节。"
            "只输出 JSON，不要解释或 markdown。"
        )
        timeline_ranges = []
        usermsg = (
            source_context
            + "请输出 JSON：{\"prompt\":\"一段可直接用于图像生成的中文执行提示词\"}。"
            "这是静态图片，不要编造时间轴、镜头运动或后续情节。请写清可见主体、姿态、"
            "场景层次、构图视角、光线方向、色温色调、材质和画面风格；"
            "无法确认的身份、品牌文字或细节写“未确认”。"
        )
    last_error = None
    last_raw = ""
    for attempt in range(3):
        raw = ""
        message = usermsg
        if attempt:
            message += (
                "\n\n上一次输出校验失败：%s。请修正后重新输出完整 JSON，"
                "重新观察图片后，确保 segments 恰好包含指定数量的对象；"
                "每个对象都逐项填写 subject、action、scene、camera、light、audio，"
                "六个字段合计至少80个中文字符，不要缩短已有描述；"
                "subject、action、scene、camera、light 不得只写“未确认”“无”“没有”"
                "“略”“待补充”“占位”或“内容”；必须改写为画面中可见的具体事实。"
                "无明显动作时描述保持的姿态，无明显运镜时写明景别、视角、构图及"
                "“固定镜头，无明显运镜”，光线方向不明时描述整体明暗、色调和对比关系；"
                "audio 最多40个有效字符，只保留声音、口播或字幕摘要，不得复制长段屏幕文字；"
                "不要返回时间码、序号、示例文字、占位符、解释或 markdown。"
                "\n上一次草稿如下，请逐段扩写并修正结构：\n%s"
                % (last_error, last_raw[:5000])
            )
        try:
            if duration > 0:
                raw = _chat_multimodal(
                    sysmsg, message, frames, max_tokens=1800,
                )
                contents = _coerce_reverse_segments(
                    raw,
                    len(timeline_ranges),
                    allow_duplicates=bool(attempt),
                    allow_short=bool(attempt),
                )
                if attempt:
                    contents = _annotate_repeated_reverse_segments(contents)
                    contents = _expand_short_reverse_segments(contents)
                    _validate_reverse_segment_contents(
                        contents, len(timeline_ranges),
                    )
                prompt = "\n".join(
                    "%s %s" % (label, content)
                    for label, content in zip(timeline_ranges, contents)
                )
                _validate_reverse_timeline(prompt, duration)
            else:
                raw = _chat_multimodal(sysmsg, message, frames)
                prompt = _coerce_reverse_prompt(raw)
                if not prompt:
                    raise ValueError("提示词反推结果为空")
            break
        except ValueError as error:
            last_error = error
            last_raw = str(raw or "")
            print(
                "[breakdown] reverse output rejected attempt=%d error=%s raw=%s"
                % (
                    attempt + 1,
                    str(error)[:180],
                    re.sub(r"\s+", " ", str(raw or ""))[:500],
                ),
                flush=True,
            )
    else:
        raise ValueError("提示词内容校验失败：%s" % last_error)
    return {
        "type": "breakdown_reverse",
        "source_url": source_url,
        "source_title": title,
        "source_platform": platform,
        "duration": duration,
        "prompt": prompt,
        "frame_thumbnails": _frame_thumbnails(frames),
    }


def _timeline_label(seconds):
    total_milliseconds = max(
        0, int(max(0.0, float(seconds or 0)) * 1000 + 0.5),
    )
    return _timeline_label_milliseconds(total_milliseconds)


def _timeline_label_milliseconds(total_milliseconds):
    minutes, remaining_milliseconds = divmod(
        max(0, int(total_milliseconds or 0)), 60_000,
    )
    whole_seconds, milliseconds = divmod(remaining_milliseconds, 1000)
    seconds_text = "%02d" % whole_seconds
    if milliseconds:
        seconds_text += (".%03d" % milliseconds).rstrip("0")
    return "%02d:%s" % (minutes, seconds_text)


def _fixed_reverse_ranges(duration, max_segments=4):
    duration = max(0.0, float(duration or 0))
    if duration <= 0:
        return []
    total_milliseconds = max(1, int(duration * 1000 + 0.5))
    count = min(
        max(1, int(max_segments or 1)),
        3 if total_milliseconds <= 9000 else 4,
        total_milliseconds,
    )
    values = [
        (index * total_milliseconds + count // 2) // count
        for index in range(count + 1)
    ]

    return [
        "[%s-%s]" % (
            _timeline_label_milliseconds(values[index]),
            _timeline_label_milliseconds(values[index + 1]),
        )
        for index in range(count)
    ]


def _coerce_reverse_segments(
    raw, expected_count, allow_duplicates=False, allow_short=False,
):
    try:
        value = (_parse_breakdown_json(raw) or {}).get("segments")
    except ValueError as error:
        raise ValueError("分段内容无法解析") from error
    if not isinstance(value, list):
        raise ValueError("segments 必须是数组")
    field_labels = (
        ("subject", "主体与位置"),
        ("action", "动作与表情"),
        ("scene", "场景空间"),
        ("camera", "镜头构图"),
        ("light", "光线质感"),
        ("audio", "声音字幕"),
    )
    visual_fields = {"subject", "action", "scene", "camera", "light"}
    contents = []
    for index, item in enumerate(value, 1):
        if isinstance(item, dict):
            missing = [
                field for field, _ in field_labels
                if not str(item.get(field) or "").strip()
            ]
            if missing:
                raise ValueError(
                    "第%d段缺少字段：%s" % (index, "、".join(missing))
                )
            placeholder_fields = [
                field for field, _ in field_labels
                if field in visual_fields
                and _is_reverse_placeholder(item.get(field))
            ]
            if placeholder_fields:
                raise ValueError(
                    "第%d段视觉字段包含占位内容：%s"
                    % (index, "、".join(placeholder_fields))
                )
            audio_text = str(item.get("audio") or "").strip()
            if len(_reverse_segment_fingerprint(audio_text)) > 40:
                raise ValueError(
                    "第%d段声音字幕字段过长，最多允许40个有效字符" % index
                )
            contents.append("；".join(
                "%s：%s" % (label, str(item.get(field) or "").strip())
                for field, label in field_labels
            ))
        else:
            contents.append(str(item or "").strip())
    return _validate_reverse_segment_contents(
        contents,
        expected_count,
        allow_duplicates=allow_duplicates,
        allow_short=allow_short,
    )


def _reverse_segment_fingerprint(content):
    return re.sub(
        r"[\s，。；：、,.!！?？…~\-—_]+", "", str(content or ""),
    ).lower()


def _is_reverse_placeholder(content):
    return _reverse_segment_fingerprint(content) in {
        "", "无", "没有", "未确认", "略", "待补充", "占位", "内容",
    }


def _annotate_repeated_reverse_segments(contents):
    seen = {}
    annotated = []
    for index, content in enumerate(contents, 1):
        fingerprint = _reverse_segment_fingerprint(content)
        previous_index = seen.get(fingerprint)
        if previous_index is not None:
            content = (
                str(content).rstrip("；。 ")
                + "；连续性：与第%d段保持同一主体、动作状态、场景、机位和光线，"
                "未观察到可确认变化。" % previous_index
            )
        annotated.append(content)
        seen[fingerprint] = index
    return annotated


def _expand_short_reverse_segments(contents):
    compact_lengths = [
        len(_reverse_segment_fingerprint(content)) for content in contents
    ]
    expand_all = sum(compact_lengths) < 240
    expanded = []
    for index, (content, compact_length) in enumerate(
        zip(contents, compact_lengths), 1,
    ):
        if expand_all or compact_length < 40:
            content = (
                str(content).rstrip("；。 ")
                + "；事实边界：仅保留关键帧中可见信息，人物身份、品牌文字及遮挡细节"
                "均未确认；执行约束：第%d段保持当前主体位置、已有道具、场景层次、"
                "机位方向和光线状态，不新增人物、物体或情节。" % index
            )
        expanded.append(content)
    return expanded


def _validate_reverse_segment_contents(
    contents, expected_count, allow_duplicates=False, allow_short=False,
):
    if len(contents) != int(expected_count):
        raise ValueError(
            "分段数量应为%d，实际为%d" % (int(expected_count), len(contents))
        )
    normalized = []
    total_length = 0
    for index, content in enumerate(contents, 1):
        compact = _reverse_segment_fingerprint(content)
        if _is_reverse_placeholder(content):
            raise ValueError("第%d段是空内容或占位内容" % index)
        if not allow_short and len(compact) < 40:
            raise ValueError("第%d段内容过短，至少需要40个有效字符" % index)
        normalized.append(compact.lower())
        total_length += len(compact)
    if not allow_duplicates and len(set(normalized)) != len(normalized):
        raise ValueError("分段内容存在完全重复")
    if not allow_short and total_length < 240:
        raise ValueError("全部分段内容过短，至少需要240个有效字符")
    if total_length > 800:
        raise ValueError("全部分段内容过长，最多允许800个有效字符")
    return contents


def _coerce_reverse_prompt(raw):
    """兼容模型偶发把 prompt 返回为数组或漏写数组逗号。"""
    try:
        value = (_parse_breakdown_json(raw) or {}).get("prompt")
    except ValueError:
        value = None
    if isinstance(value, list):
        prompt = "\n".join(str(item or "").strip() for item in value if str(item or "").strip())
    else:
        prompt = str(value or "").strip()
    if prompt:
        return prompt
    entries = re.findall(
        r'"(\s*(?:[-*]\s*|\d+[.)]\s*)?\[[^"\]]+\]\s*[^"]+)"',
        _strip_json_code_fence(raw),
    )
    if entries:
        return "\n".join(
            item.replace(r"\n", "\n").replace(r"\"", '"').strip()
            for item in entries
        )
    raise ValueError("提示词反推结果无法解析")


_TIMELINE_LINE_RE = re.compile(
    r"^\s*(?:[-*]\s*|\d+[.)]\s*)?"
    r"\[\s*(?P<start>(?:\d{1,2}:){1,2}\d{1,2}(?:\.\d+)?|\d+(?:\.\d+)?s)"
    r"\s*[-–—~至]\s*"
    r"(?P<end>(?:\d{1,2}:){1,2}\d{1,2}(?:\.\d+)?|\d+(?:\.\d+)?s)\s*\]"
    r"\s*(?P<body>.*?)\s*$"
)


def _timeline_seconds(value):
    text = str(value or "").strip().lower()
    if text.endswith("s"):
        return float(text[:-1])
    parts = text.split(":")
    if len(parts) == 2:
        seconds = float(parts[1])
        if seconds >= 60:
            raise ValueError("秒数必须小于60")
        return int(parts[0]) * 60 + seconds
    if len(parts) == 3:
        minutes, seconds = int(parts[1]), float(parts[2])
        if minutes >= 60 or seconds >= 60:
            raise ValueError("分和秒必须小于60")
        return int(parts[0]) * 3600 + minutes * 60 + seconds
    raise ValueError("无法识别时间格式 %s" % value)


def _validate_reverse_timeline(
    prompt, duration, tolerance=0.05, endpoint_tolerance=0.25,
):
    duration = float(duration)
    lines = [line.strip() for line in str(prompt or "").splitlines() if line.strip()]
    if not lines:
        raise ValueError("时间轴为空")
    parsed = []
    for line in lines:
        match = _TIMELINE_LINE_RE.match(line)
        if not match:
            if parsed:
                if line.startswith("[") and re.search(r"\d", line):
                    raise ValueError("存在无法识别的[开始-结束]时间范围")
                parsed[-1]["body"].append(line)
            continue
        parsed.append({
            "start": match.group("start"),
            "end": match.group("end"),
            "body": [match.group("body")] if match.group("body") else [],
        })
    if not parsed:
        raise ValueError("未找到合法的[开始-结束]时间范围")
    segments = []
    for index, item in enumerate(parsed, 1):
        body_text = " ".join(item["body"]).strip()
        meaningful = re.sub(r"[\s。.!！?？]+", "", body_text)
        if not body_text or meaningful in {"无", "没有", "未确认"}:
            raise ValueError("第%d段缺少画面描述" % index)
        start = _timeline_seconds(item["start"])
        end = _timeline_seconds(item["end"])
        if end <= start:
            raise ValueError("第%d段结束时间必须大于开始时间" % index)
        if end > duration + endpoint_tolerance:
            raise ValueError("第%d段结束时间超出视频总时长" % index)
        segments.append((start, end))
    if abs(segments[0][0]) > tolerance:
        raise ValueError("时间轴必须从00:00开始")
    previous_start, previous_end = segments[0]
    for index, (start, end) in enumerate(segments[1:], 2):
        if start < previous_start - tolerance:
            raise ValueError("第%d段时间范围乱序" % index)
        if start < previous_end - tolerance:
            raise ValueError("第%d段与上一段重叠" % index)
        if start > previous_end + tolerance:
            raise ValueError("第%d段与上一段之间存在空档" % index)
        previous_start, previous_end = start, end
    if abs(previous_end - duration) > endpoint_tolerance:
        raise ValueError("末段结束时间未对齐视频总时长")
    return segments


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


def _normalize_duration_seconds(value, fallback=0):
    """把上游秒/毫秒混合时长统一成秒；真实媒体时长仍以 ffprobe 为准。"""
    try:
        duration = float(value)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        return float(fallback or 0)
    # 短视频平台偶尔返回毫秒。超过一小时按毫秒解释，可覆盖
    # 10034ms 这类已观测值，同时保留常见的长视频秒数。
    if duration > 3600:
        duration /= 1000.0
    return duration


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
            encoded = base64.b64encode(_bounded_thumbnail(path)).decode()
            thumbs.append("data:image/jpeg;base64," + encoded)
        except Exception:
            pass
    return thumbs


def _bounded_thumbnail(
    path, max_edge=_THUMBNAIL_MAX_EDGE, max_bytes=_THUMBNAIL_MAX_BYTES,
):
    """Create a small JPEG reference; never persist the original upload bytes."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        with Image.open(path) as source:
            width, height = source.size
            if width <= 0 or height <= 0 or width * height > _THUMBNAIL_MAX_PIXELS:
                raise ValueError("参考图片分辨率过大")
            source.seek(0)
            image = ImageOps.exif_transpose(source)
            if image.mode in {"RGBA", "LA"} or (
                    image.mode == "P" and "transparency" in image.info):
                rgba = image.convert("RGBA")
                background = Image.new("RGB", rgba.size, (255, 255, 255))
                background.paste(rgba, mask=rgba.getchannel("A"))
                image = background
            else:
                image = image.convert("RGB")
            image.load()
    except (UnidentifiedImageError, OSError, ValueError) as error:
        raise ValueError("参考图片无法生成缩略图") from error

    resampling = getattr(Image, "Resampling", Image).LANCZOS
    edges = []
    for edge in (max_edge, 640, 512, 384, 320, 256):
        edge = min(max(1, int(edge)), max(1, int(max_edge)))
        if edge not in edges:
            edges.append(edge)
    for edge in edges:
        candidate = image.copy()
        candidate.thumbnail((edge, edge), resampling)
        for quality in (82, 72, 62, 52, 42):
            output = io.BytesIO()
            candidate.save(
                output, format="JPEG", quality=quality, optimize=True, progressive=True,
            )
            thumbnail = output.getvalue()
            if len(thumbnail) <= max_bytes:
                return thumbnail
    raise ValueError("参考图片压缩后仍然过大")


def _bounded_ai_frame(path, max_bytes):
    """Return bounded image bytes without making Pillow a hard dependency."""
    try:
        return _bounded_thumbnail(
            path, max_edge=_AI_FRAME_MAX_EDGE, max_bytes=max_bytes,
        ), "image/jpeg"
    except ModuleNotFoundError:
        with open(path, "rb") as source:
            frame = source.read(max_bytes + 1)
        if len(frame) > max_bytes:
            raise ValueError("视频关键帧数据过大，请降低素材分辨率")
        media_type = mimetypes.guess_type(path)[0] or "image/jpeg"
        if media_type not in {"image/jpeg", "image/png", "image/webp"}:
            raise ValueError("视频关键帧格式不受支持")
        return frame, media_type


def _evenly_spaced_frames(paths, limit=_AI_MAX_FRAMES):
    paths = list(paths or [])
    limit = max(1, int(limit or 1))
    if len(paths) <= limit:
        return paths
    if limit == 1:
        return [paths[len(paths) // 2]]
    indexes = [
        round(index * (len(paths) - 1) / float(limit - 1))
        for index in range(limit)
    ]
    return [paths[index] for index in indexes]


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
        normalized = dict(scene)
        scene_text = str(
            scene.get("scene")
            or scene.get("description")
            or scene.get("visual_description")
            or scene.get("visual")
            or ""
        ).strip()
        if not scene_text:
            details = [
                scene.get("action"), scene.get("subject"), scene.get("setting"),
                scene.get("composition"), scene.get("lighting"),
                scene.get("color"), scene.get("mood"),
            ]
            scene_text = "；".join(
                str(value).strip() for value in details if str(value or "").strip()
            )
        line_text = str(
            scene.get("line")
            or scene.get("dialogue")
            or scene.get("narration")
            or ""
        ).strip()
        if not scene_text:
            continue
        if any(marker in scene_text or marker in line_text for marker in placeholders):
            raise ValueError("拆解结果包含模板占位内容，请重试")
        normalized["scene"] = scene_text
        normalized["line"] = line_text
        normalized["dur"] = str(
            scene.get("dur") or scene.get("duration") or "3s"
        ).strip()
        normalized["scale"] = str(
            scene.get("scale") or scene.get("shot_size") or ""
        ).strip()
        normalized["camera"] = str(
            scene.get("camera") or scene.get("composition") or ""
        ).strip()
        valid.append(normalized)
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
            '所有字段必须使用简体中文，禁止代码围栏、解释和模板占位文字。',
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
    if not frames:
        shutil.rmtree(outdir, ignore_errors=True)
        raise ValueError("视频未能提取有效关键帧，请检查视频内容后重试")
    return outdir, frames


def _chat_multimodal(sysmsg, usermsg, image_paths, temp=0.7, max_tokens=None):
    """编导视觉理解统一走智谱 GLM-4V。"""
    if not ZHIPU_API_KEY:
        raise RuntimeError("REVERSE_ZHIPU_KEY is not configured")

    content = [{"type": "text", "text": usermsg}]
    image_paths = _evenly_spaced_frames(image_paths, _AI_MAX_FRAMES)
    per_frame_budget = min(
        _AI_FRAME_MAX_BYTES,
        max(32 * 1024, _AI_FRAMES_TOTAL_MAX_BYTES // max(1, len(image_paths))),
    )
    image_bytes = 0
    for path in image_paths:
        frame, media_type = _bounded_ai_frame(path, per_frame_budget)
        if image_bytes + len(frame) > _AI_FRAMES_TOTAL_MAX_BYTES:
            raise ValueError("视频关键帧压缩后仍然过大，请缩短视频或降低素材分辨率")
        image_bytes += len(frame)
        b64 = base64.b64encode(frame).decode()
        content.append({
            "type": "image_url",
            "image_url": {
                "url": "data:" + media_type + ";base64," + b64,
                "detail": "low",
            },
        })

    body = {
        "model": ZHIPU_MODEL,
        "messages": [
            {"role": "system", "content": sysmsg},
            {"role": "user", "content": content}
        ],
        "temperature": temp,
    }
    if max_tokens is not None:
        body["max_tokens"] = int(max_tokens)

    request_data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    try:
        d = egress.post_json_idempotent(
            ZHIPU_API_BASE, ZHIPU_API_BASE, "/chat/completions",
            request_data,
            {
                "Authorization": "Bearer " + ZHIPU_API_KEY,
                "Content-Type": "application/json",
            },
            log=lambda message: print("[breakdown] %s" % message, flush=True),
            max_attempts=2,
        )
    except Exception as error:
        code = int(getattr(error, "code", 0) or 0)
        reason = getattr(error, "reason", None)
        detail = str(error or "").lower()
        upstream_detail = ""
        if isinstance(error, urllib.error.HTTPError):
            try:
                upstream_detail = error.read(1024).decode("utf-8", "replace")
            except Exception:
                upstream_detail = ""
        timed_out = (
            isinstance(error, (TimeoutError, socket.timeout))
            or isinstance(reason, (TimeoutError, socket.timeout))
            or "timed out" in detail
            or "timeout" in detail
        )
        print(
            "[breakdown] ai request failed type=%s http=%s request_bytes=%d image_bytes=%d upstream=%s"
            % (
                type(error).__name__, code or "-", len(request_data), image_bytes,
                re.sub(r"\s+", " ", upstream_detail)[:500] or "-",
            ),
            flush=True,
        )
        if code == 413:
            message = "AI 分析素材数据过大，请缩短视频或降低素材分辨率，本次点数已自动退回"
        elif code == 429:
            message = "AI 分析服务请求过多，请稍后重试，本次点数已自动退回"
        elif code in (401, 403):
            message = "AI 分析服务鉴权异常，本次点数已自动退回，请联系管理员"
        elif code == 400:
            message = "AI 分析请求被上游拒绝，可能是素材格式或内容不兼容，本次点数已自动退回"
        elif code >= 500:
            message = "AI 分析服务暂时不可用，本次点数已自动退回，请稍后重试"
        elif timed_out:
            message = "AI 分析响应超时，本次未生成结果，点数已自动退回，请稍后重试"
        elif isinstance(
            error,
            (
                urllib.error.URLError,
                http.client.RemoteDisconnected,
                http.client.IncompleteRead,
                ConnectionError,
                ssl.SSLError,
            ),
        ):
            message = "AI 分析连接中断，本次点数已自动退回，请稍后重试"
        else:
            message = "AI 分析服务暂时不可用，本次未生成结果，点数已自动退回，请稍后重试"
        raise RuntimeError(message) from error
    return (d.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()


HANDLERS = {"breakdown": gen_breakdown}
