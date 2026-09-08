"""Owner-scoped multi-asset timeline rendering for matrix-template jobs."""

from __future__ import annotations

import hashlib
import json
import math
import os
import pathlib
import shutil
import subprocess
import tempfile
import textwrap
import time


RATIOS = {
    "9:16": (1080, 1920),
    "16:9": (1920, 1080),
    "1:1": (1080, 1080),
}
TRANSITIONS = {"none", "fade"}
TEXT_STYLES = {"full-overlay-bold"}
MAX_SEGMENTS = 20
MAX_TOTAL_SECONDS = 180.0
MAX_FILE_BYTES = 1024 * 1024 * 1024
FADE_SECONDS = 0.5
FFMPEG = os.environ.get("TIMELINE_COMPOSE_FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("TIMELINE_COMPOSE_FFPROBE", "ffprobe")
FONT_PATH = os.environ.get(
    "TIMELINE_COMPOSE_FONT_PATH", "/home/ubuntu/.fonts/NotoSansSC.ttf"
)


def _finite(value, field, minimum, maximum):
    if isinstance(value, bool):
        raise ValueError(field + " 需要填写数字")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(field + " 需要填写数字") from exc
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise ValueError("%s 需要在 %s-%s 之间" % (field, minimum, maximum))
    return round(number, 3)


def _asset_id(value, field="asset_id"):
    if isinstance(value, bool):
        raise ValueError(field + " 无效")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(field + " 无效") from exc
    if result != value and str(value).strip() != str(result):
        raise ValueError(field + " 无效")
    if result < 1:
        raise ValueError(field + " 无效")
    return result


def _asset_index(value):
    if isinstance(value, bool):
        raise ValueError("asset_index 无效")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("asset_index 无效") from exc
    if result != value and str(value).strip() != str(result):
        raise ValueError("asset_index 无效")
    if not 0 <= result <= 3:
        raise ValueError("asset_index 需要在 0-3 之间")
    return result


def _snapshot(path):
    stat = pathlib.Path(path).stat()
    return "%d:%d" % (stat.st_size, stat.st_mtime_ns)


def _owned_file(username, kind, asset_id, asset_index=0):
    from . import asset_batch, core

    record = asset_batch._asset_download_record(core, username, kind, asset_id)
    candidates = []
    for relative, _label in record.get("files") or []:
        path = core._resolve_out_file(relative)
        if path:
            candidates.append(path.resolve())
    if not candidates:
        raise ValueError("素材文件已不在服务器，请重新上传后再报价")
    if not 0 <= asset_index < len(candidates):
        raise ValueError("asset_index 超出该图片任务的成品数量")
    path = candidates[asset_index]
    suffix = path.suffix.lower()
    allowed = {
        "image": {".jpg", ".jpeg", ".png", ".webp"},
        "video": {".mp4", ".mov", ".webm", ".mkv"},
        "audio": {".mp3", ".wav", ".m4a", ".aac", ".ogg"},
    }[kind]
    if suffix not in allowed:
        raise ValueError("素材文件格式不支持时间轴拼接")
    if not 0 < path.stat().st_size <= MAX_FILE_BYTES:
        raise ValueError("素材文件大小无效")
    return str(path), _snapshot(path)


def _probe(path):
    try:
        completed = subprocess.run(
            [
                FFPROBE, "-v", "error", "-show_entries",
                "format=duration:stream=codec_type,width,height",
                "-of", "json", str(path),
            ],
            check=True, capture_output=True, text=True, timeout=30,
        )
        value = json.loads(completed.stdout or "{}")
        duration = float((value.get("format") or {}).get("duration") or 0)
    except (OSError, subprocess.SubprocessError, TypeError, ValueError) as exc:
        raise ValueError("素材文件无法读取") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("素材时长无效")
    streams = value.get("streams") or []
    video = next((item for item in streams if item.get("codec_type") == "video"), {})
    return {
        "duration": duration,
        "has_audio": any(item.get("codec_type") == "audio" for item in streams),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
    }


def _probe_image(path):
    try:
        completed = subprocess.run(
            [
                FFPROBE, "-v", "error", "-show_entries",
                "stream=codec_type,width,height", "-of", "json", str(path),
            ],
            check=True, capture_output=True, text=True, timeout=30,
        )
        streams = json.loads(completed.stdout or "{}").get("streams") or []
        image = next((item for item in streams if item.get("codec_type") == "video"), {})
        width = int(image.get("width") or 0)
        height = int(image.get("height") or 0)
    except (OSError, subprocess.SubprocessError, TypeError, ValueError) as exc:
        raise ValueError("图片素材无法读取") from exc
    if width <= 0 or height <= 0:
        raise ValueError("图片素材无法读取")


def _normalize_voiceover(value, username):
    if value in (None, ""):
        return None
    if not isinstance(value, dict):
        raise ValueError("配音设置无效")
    allowed = {"text", "voice", "voice_scope", "speed"}
    if set(value) - allowed or not {"text", "voice"}.issubset(value):
        raise ValueError("配音只允许 text、voice、voice_scope、speed")
    from . import matrix_template_video
    return matrix_template_video._normalize_voiceover(value, username)


def validate_payload(raw, username):
    if not isinstance(raw, dict):
        raise ValueError("时间轴请求体不是合法 JSON")
    allowed = {
        "mode", "segments", "ratio", "preserve_source_audio",
        "bgm", "bgm_asset_id", "bgm_volume", "voiceover",
    }
    if set(raw) - allowed:
        raise ValueError("时间轴请求包含无效字段")
    if raw.get("mode") != "timeline":
        raise ValueError("时间轴 mode 必须是 timeline")
    if not shutil.which(FFMPEG) or not shutil.which(FFPROBE):
        raise ValueError("时间轴渲染服务暂不可用")
    values = raw.get("segments")
    if not isinstance(values, list) or not 2 <= len(values) <= MAX_SEGMENTS:
        raise ValueError("segments 必须包含 2-20 段")
    ratio = str(raw.get("ratio") or "9:16")
    if ratio not in RATIOS:
        raise ValueError("ratio 只支持 9:16、16:9、1:1")
    preserve_audio = raw.get("preserve_source_audio", True)
    if not isinstance(preserve_audio, bool):
        raise ValueError("preserve_source_audio 必须是布尔值")
    bgm = raw.get("bgm", bool(raw.get("bgm_asset_id")))
    if not isinstance(bgm, bool):
        raise ValueError("bgm 必须是布尔值")
    bgm_asset_id = raw.get("bgm_asset_id")
    if bgm and bgm_asset_id in (None, ""):
        raise ValueError("bgm=true 时必须选择当前账号的 bgm_asset_id")
    if not bgm and bgm_asset_id not in (None, ""):
        raise ValueError("bgm_asset_id 仅在 bgm=true 时使用")

    segments = []
    total = 0.0
    has_text_card = False
    for index, item in enumerate(values):
        if not isinstance(item, dict):
            raise ValueError("第 %d 段格式无效" % (index + 1))
        kind = str(item.get("type") or "")
        transition = str(item.get("transition") or "none")
        if transition not in TRANSITIONS:
            raise ValueError("transition 只支持 none、fade")
        if kind == "image":
            if set(item) - {"type", "asset_id", "asset_index", "duration", "transition"}:
                raise ValueError("图片段包含无效字段")
            asset_id = _asset_id(item.get("asset_id"))
            asset_index = _asset_index(item.get("asset_index", 0))
            duration = _finite(item.get("duration", 3), "图片段 duration", 1, 10)
            source, revision = _owned_file(username, "image", asset_id, asset_index)
            _probe_image(source)
            segment = {
                "type": kind, "asset_id": asset_id, "asset_index": asset_index,
                "duration": duration, "transition": transition,
                "_source_file": source, "_source_revision": revision,
            }
        elif kind == "video":
            if set(item) - {"type", "asset_id", "trim_start", "trim_end", "transition"}:
                raise ValueError("视频段包含无效字段")
            asset_id = _asset_id(item.get("asset_id"))
            source, revision = _owned_file(username, "video", asset_id)
            media = _probe(source)
            start = _finite(item.get("trim_start", 0), "视频段 trim_start", 0, media["duration"])
            end = _finite(item.get("trim_end", media["duration"]), "视频段 trim_end", 0.01, media["duration"] + 0.05)
            if end <= start:
                raise ValueError("视频段 trim_end 必须大于 trim_start")
            duration = round(end - start, 3)
            if duration > 60:
                raise ValueError("单个视频段裁剪后不能超过 60 秒")
            segment = {
                "type": kind, "asset_id": asset_id,
                "trim_start": start, "trim_end": end,
                "duration": duration, "transition": transition,
                "_source_file": source, "_source_revision": revision,
                "_has_audio": media["has_audio"],
            }
        elif kind == "text_card":
            if set(item) - {"type", "text", "style", "duration", "transition"}:
                raise ValueError("文字卡包含无效字段")
            text = " ".join(str(item.get("text") or "").split())
            if not 1 <= len(text) <= 120:
                raise ValueError("文字卡 text 需要 1-120 个字符")
            style = str(item.get("style") or "full-overlay-bold")
            if style not in TEXT_STYLES:
                raise ValueError("文字卡首版只支持 full-overlay-bold")
            duration = _finite(item.get("duration", 3), "文字卡 duration", 1, 8)
            segment = {
                "type": kind, "text": text, "style": style,
                "duration": duration, "transition": transition,
            }
            has_text_card = True
        else:
            raise ValueError("segment.type 只支持 image、video、text_card")
        total += duration
        segments.append(segment)
    if segments[-1]["transition"] != "none":
        raise ValueError("最后一段 transition 必须是 none")
    if has_text_card and not pathlib.Path(FONT_PATH).is_file():
        raise ValueError("时间轴中文字卡字体未配置")
    fade_count = sum(item["transition"] == "fade" for item in segments[:-1])
    output_duration = round(total - fade_count * FADE_SECONDS, 3)
    if output_duration <= 0 or output_duration > MAX_TOTAL_SECONDS:
        raise ValueError("时间轴总时长不能超过 180 秒")

    result = {
        "mode": "timeline", "segments": segments, "ratio": ratio,
        "preserve_source_audio": preserve_audio, "bgm": bgm,
        "duration": output_duration, "_timeline_validated": True,
    }
    if bgm:
        bgm_id = _asset_id(bgm_asset_id, "bgm_asset_id")
        source, revision = _owned_file(username, "audio", bgm_id)
        if not _probe(source)["has_audio"]:
            raise ValueError("背景音乐素材缺少音轨")
        result.update({
            "bgm_asset_id": bgm_id,
            "bgm_volume": _finite(raw.get("bgm_volume", 0.18), "bgm_volume", 0.05, 0.8),
            "_bgm_file": source, "_bgm_revision": revision,
        })
    result["voiceover"] = _normalize_voiceover(raw.get("voiceover"), username)
    result["cost_breakdown"] = cost_breakdown(result)
    return result


def cost_breakdown(payload):
    from . import pricing

    segments = len(payload.get("segments") or [])
    duration = float(payload.get("duration") or 0)
    base = pricing.get_price("video.timeline_compose.base")
    segment_cost = segments * pricing.get_price("video.timeline_compose.segment")
    duration_blocks = max(1, int(math.ceil(duration / 30.0)))
    duration_cost = duration_blocks * pricing.get_price("video.timeline_compose.30s")
    return {
        "base": base, "segments": segment_cost,
        "duration_blocks": duration_blocks, "duration": duration_cost,
        "total": base + segment_cost + duration_cost,
    }


def cost(payload):
    return int((payload.get("cost_breakdown") or cost_breakdown(payload))["total"])


def _run(command, timeout=300):
    try:
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("时间轴渲染程序不可用或超时") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "")[-400:].strip()
        raise RuntimeError("时间轴渲染失败：" + detail) from exc


def _video_filter(width, height):
    return (
        "scale=%d:%d:force_original_aspect_ratio=increase,"
        "crop=%d:%d,setsar=1,fps=30,format=yuv420p"
    ) % (width, height, width, height)


def _encode_tail(output):
    return [
        "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "veryfast",
        "-crf", "20", "-c:a", "aac", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart", "-shortest", str(output),
    ]


def _normalize_segment(segment, output, width, height, workspace):
    duration = float(segment["duration"])
    if segment["type"] == "image":
        command = [
            FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-loop", "1", "-t", "%.3f" % duration, "-i", segment["_source_file"],
            "-f", "lavfi", "-t", "%.3f" % duration,
            "-i", "anullsrc=r=48000:cl=stereo", "-filter_complex",
            "[0:v]%s[v];[1:a]atrim=0:%.3f,asetpts=PTS-STARTPTS[a]" % (
                _video_filter(width, height), duration,
            ),
        ] + _encode_tail(output)
    elif segment["type"] == "video":
        command = [
            FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-ss", "%.3f" % segment["trim_start"], "-t", "%.3f" % duration,
            "-i", segment["_source_file"],
        ]
        if segment.get("_has_audio"):
            graph = "[0:v]%s[v];[0:a]aresample=48000,atrim=0:%.3f,asetpts=PTS-STARTPTS[a]" % (
                _video_filter(width, height), duration,
            )
        else:
            command.extend([
                "-f", "lavfi", "-t", "%.3f" % duration,
                "-i", "anullsrc=r=48000:cl=stereo",
            ])
            graph = "[0:v]%s[v];[1:a]atrim=0:%.3f,asetpts=PTS-STARTPTS[a]" % (
                _video_filter(width, height), duration,
            )
        command.extend(["-filter_complex", graph] + _encode_tail(output))
    else:
        font = pathlib.Path(FONT_PATH)
        if not font.is_file():
            raise RuntimeError("时间轴中文字卡字体未配置")
        text = segment["text"]
        size = 82 if len(text) <= 20 else 66 if len(text) <= 48 else 52
        lines = "\n".join(textwrap.wrap(text, width=12, break_long_words=True))
        text_file = pathlib.Path(workspace) / (output.stem + ".txt")
        text_file.write_text(lines, encoding="utf-8")
        def esc(path):
            return str(path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
        draw = (
            "drawtext=fontfile='%s':textfile='%s':fontcolor=white:fontsize=%d:"
            "line_spacing=22:x=(w-text_w)/2:y=(h-text_h)/2:"
            "box=1:boxcolor=0x111827cc:boxborderw=34"
        ) % (esc(font), esc(text_file), size)
        command = [
            FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-f", "lavfi", "-i", "color=c=0x0b1220:s=%dx%d:r=30:d=%.3f" % (
                width, height, duration,
            ),
            "-f", "lavfi", "-t", "%.3f" % duration,
            "-i", "anullsrc=r=48000:cl=stereo", "-filter_complex",
            "[0:v]%s,fade=t=in:st=0:d=0.25,fade=t=out:st=%.3f:d=0.25[v];"
            "[1:a]atrim=0:%.3f,asetpts=PTS-STARTPTS[a]" % (
                draw, max(0, duration - 0.25), duration,
            ),
        ] + _encode_tail(output)
    _run(command)


def _join_segments(files, segments, output):
    command = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    for path in files:
        command.extend(["-i", str(path)])
    filters = []
    for index in range(len(files)):
        filters.extend([
            "[%d:v]settb=AVTB,setpts=PTS-STARTPTS[v%d]" % (index, index),
            "[%d:a]aresample=48000,asetpts=PTS-STARTPTS[a%d]" % (index, index),
        ])
    current_v, current_a = "v0", "a0"
    current_duration = float(segments[0]["duration"])
    for index in range(1, len(files)):
        fade = FADE_SECONDS if segments[index - 1]["transition"] == "fade" else 0.001
        offset = max(0, current_duration - fade)
        next_v, next_a = "vx%d" % index, "ax%d" % index
        filters.append(
            "[%s][v%d]xfade=transition=fade:duration=%.3f:offset=%.3f[%s]"
            % (current_v, index, fade, offset, next_v)
        )
        filters.append(
            "[%s][a%d]acrossfade=d=%.3f:c1=tri:c2=tri[%s]"
            % (current_a, index, fade, next_a)
        )
        current_duration += float(segments[index]["duration"]) - fade
        current_v, current_a = next_v, next_a
    command.extend([
        "-filter_complex", ";".join(filters),
        "-map", "[%s]" % current_v, "-map", "[%s]" % current_a,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart", str(output),
    ])
    _run(command, timeout=600)
    return round(current_duration, 3)


def _mix_audio(video, output, duration, payload, voiceover_audio=None):
    preserve = bool(payload.get("preserve_source_audio"))
    bgm_file = payload.get("_bgm_file") if payload.get("bgm") else None
    if preserve and not bgm_file and not voiceover_audio:
        shutil.copy2(video, output)
        return
    command = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(video)]
    inputs = []
    if preserve:
        inputs.append("[0:a]volume=0.7,apad,atrim=0:%.3f[src]" % duration)
    next_index = 1
    if bgm_file:
        command.extend(["-stream_loop", "-1", "-i", str(bgm_file)])
        inputs.append(
            "[%d:a]volume=%.3f,apad,atrim=0:%.3f[bgm]" % (
                next_index, float(payload.get("bgm_volume") or 0.18), duration,
            )
        )
        next_index += 1
    if voiceover_audio:
        command.extend(["-i", str(voiceover_audio["path"])])
        inputs.append("[%d:a]volume=1,apad,atrim=0:%.3f[voice]" % (next_index, duration))
    filters = list(inputs)
    labels = "".join("[%s]" % name for name in (
        (["src"] if preserve else [])
        + (["bgm"] if bgm_file else [])
        + (["voice"] if voiceover_audio else [])
    ))
    if labels:
        filters.append(
            labels + "amix=inputs=%d:duration=longest:dropout_transition=0:normalize=0,"
            "alimiter=limit=.95:level=false[aout]" % len(inputs)
        )
        command.extend([
            "-filter_complex", ";".join(filters), "-map", "0:v:0", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-t", "%.3f" % duration,
            "-movflags", "+faststart", str(output),
        ])
    else:
        command.extend(["-map", "0:v:0", "-c:v", "copy", "-an", str(output)])
    _run(command, timeout=300)


def _verify_sources(payload):
    for segment in payload.get("segments") or []:
        source = segment.get("_source_file")
        revision = segment.get("_source_revision")
        if source and (not pathlib.Path(source).is_file() or _snapshot(source) != revision):
            raise RuntimeError("时间轴素材在报价后发生变化，请重新报价")
    if payload.get("_bgm_file") and (
        not pathlib.Path(payload["_bgm_file"]).is_file()
        or _snapshot(payload["_bgm_file"]) != payload.get("_bgm_revision")
    ):
        raise RuntimeError("背景音乐在报价后发生变化，请重新报价")


def generate(raw):
    payload = dict(raw or {})
    username = str(payload.get("_username") or "")
    if not payload.get("_timeline_validated"):
        payload = validate_payload(payload, username)
    _verify_sources(payload)
    job_id = str(raw.get("_job_id") or hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()[:16])
    width, height = RATIOS[payload["ratio"]]
    from . import core, matrix_template_video
    deadline = time.time() + matrix_template_video.TOTAL_TIMEOUT
    if not matrix_template_video._persist_runtime(
        job_id, phase="timeline_preparing", deadline_at=int(deadline),
    ):
        raise RuntimeError("时间轴任务进度保存失败")
    voiceover_audio = matrix_template_video._prepare_voiceover_audio(
        job_id, username, payload.get("voiceover"), "", deadline,
    )
    last_error = None
    for attempt in range(2):
        try:
            with tempfile.TemporaryDirectory(prefix="timeline-compose-") as temp:
                workspace = pathlib.Path(temp)
                normalized = []
                for index, segment in enumerate(payload["segments"]):
                    if not matrix_template_video._persist_runtime(
                        job_id, phase="timeline_segment_%d" % (index + 1),
                    ):
                        raise RuntimeError("时间轴任务进度保存失败")
                    output = workspace / ("segment-%02d.mp4" % index)
                    _normalize_segment(segment, output, width, height, workspace)
                    normalized.append(output)
                if not matrix_template_video._persist_runtime(
                    job_id, phase="timeline_joining",
                ):
                    raise RuntimeError("时间轴任务进度保存失败")
                joined = workspace / "joined.mp4"
                duration = _join_segments(normalized, payload["segments"], joined)
                if not matrix_template_video._persist_runtime(
                    job_id, phase="timeline_mixing",
                ):
                    raise RuntimeError("时间轴任务进度保存失败")
                mixed = workspace / "final.mp4"
                _mix_audio(joined, mixed, duration, payload, voiceover_audio)
                probe = _probe(mixed)
                if (
                    probe["width"] != width or probe["height"] != height
                    or abs(probe["duration"] - duration) > 0.35
                    or not 1024 <= mixed.stat().st_size <= MAX_FILE_BYTES
                ):
                    raise RuntimeError("时间轴成片校验失败")
                relative = pathlib.Path("video") / ("timeline_compose_%s.mp4" % job_id[:64])
                target = core.OUT_DIR / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(mixed, target)
            if not matrix_template_video._persist_runtime(
                job_id, phase="done",
            ):
                raise RuntimeError("时间轴任务进度保存失败")
            return {
                "type": "matrix_template_video", "mode": "timeline_compose",
                "provider": "local-ffmpeg", "status": "done", "phase": "done",
                "video_file": relative.as_posix(),
                "video_url": core.public_url(relative.as_posix(), "video/mp4", private=True),
                "duration": duration, "resolution": "1080p", "ratio": payload["ratio"],
                "width": width, "height": height, "file_size": target.stat().st_size,
                "segment_count": len(payload["segments"]),
                "cost_breakdown": payload.get("cost_breakdown") or cost_breakdown(payload),
            }
        except Exception as exc:
            last_error = exc
            if attempt == 0:
                matrix_template_video._persist_runtime(
                    job_id, phase="timeline_retrying", last_error=str(exc)[:300],
                )
                continue
    raise RuntimeError("时间轴拼接失败（已自动重试一次）：%s" % str(last_error)[:300])
