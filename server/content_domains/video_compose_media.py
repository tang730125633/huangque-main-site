# -*- coding: utf-8 -*-
"""Deterministic FFmpeg operations for one-click video clean masters."""

import json
import math
import pathlib
import re
import subprocess


class MediaError(ValueError):
    pass


QUALITY_LONG_SILENCE_MS = 3000
QUALITY_MIN_LUFS = -24.0
QUALITY_MAX_LUFS = -8.0
QUALITY_MAX_TRUE_PEAK_DBFS = -1.0


def _run(command, timeout):
    try:
        return subprocess.run(
            command, check=True, timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    except FileNotFoundError as error:
        raise MediaError("服务器未安装 FFmpeg/FFprobe") from error
    except subprocess.TimeoutExpired as error:
        raise MediaError("视频处理超时") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or b"").decode("utf-8", "replace")[-320:]
        raise MediaError("视频处理失败" + ("：" + detail if detail else "")) from error


def probe_media(path):
    path = pathlib.Path(path)
    if not path.is_file():
        raise MediaError("源视频文件不存在")
    video_result = _run([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,codec_name:format=duration",
        "-of", "json", str(path),
    ], 45)
    audio_result = _run([
        "ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=codec_name", "-of", "json", str(path),
    ], 45)
    try:
        video_payload = json.loads(video_result.stdout.decode("utf-8", "replace"))
        audio_payload = json.loads(audio_result.stdout.decode("utf-8", "replace"))
        duration = float((video_payload.get("format") or {}).get("duration") or 0)
    except Exception as error:
        raise MediaError("无法读取视频信息") from error
    video = next(iter(video_payload.get("streams") or []), None)
    audio = next(iter(audio_payload.get("streams") or []), None)
    if not video or duration <= 0:
        raise MediaError("源文件不是有效视频")
    return {
        "duration_ms": int(round(duration * 1000)),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "has_audio": bool(audio),
        "video_codec": video.get("codec_name"),
        "audio_codec": audio.get("codec_name") if audio else None,
    }


def _parse_silence_ranges(stderr, minimum_ms):
    starts = [float(value) for value in re.findall(r"silence_start:\s*([0-9.]+)", stderr)]
    ends = [(float(end), float(duration)) for end, duration in re.findall(
        r"silence_end:\s*([0-9.]+)\s*\|\s*silence_duration:\s*([0-9.]+)", stderr
    )]
    ranges = []
    for index, start in enumerate(starts):
        if index >= len(ends):
            break
        end, duration = ends[index]
        if end > start and duration * 1000 >= minimum_ms - 2:
            ranges.append({
                "start_ms": int(round(start * 1000)),
                "end_ms": int(round(end * 1000)),
                "duration_ms": int(round(duration * 1000)),
            })
    return ranges


def detect_silence_ranges(source_path, noise_db=-30, minimum_ms=160, timeout=300):
    source_path = pathlib.Path(source_path)
    if not source_path.is_file():
        raise MediaError("源视频文件不存在")
    result = _run([
        "ffmpeg", "-hide_banner", "-i", str(source_path),
        "-af", "silencedetect=n=%ddB:d=%.3f" % (int(noise_db), max(0.05, minimum_ms / 1000.0)),
        "-f", "null", "-",
    ], timeout)
    stderr = (result.stderr or b"").decode("utf-8", "replace")
    return _parse_silence_ranges(stderr, minimum_ms)


def _last_finite_metric(pattern, text):
    values = re.findall(pattern, text, re.MULTILINE)
    if not values:
        return None
    try:
        value = float(values[-1])
    except (TypeError, ValueError):
        return None
    return round(value, 2) if math.isfinite(value) else None


def inspect_quality(path, timeout=600):
    """Return the M1 duration/frame/streams/silence/loudness report."""
    report = probe_media(path)
    width, height = report["width"], report["height"]
    divisor = math.gcd(width, height) if width and height else 1
    aspect_ratio = "%d:%d" % (width // divisor, height // divisor) if width and height else ""
    checks = {
        "duration": {"passed": report["duration_ms"] > 0, "duration_ms": report["duration_ms"]},
        "frame": {"passed": width > 0 and height > 0, "width": width, "height": height,
                  "aspect_ratio": aspect_ratio},
        "streams": {"passed": bool(report["video_codec"] and report["has_audio"]),
                    "video": bool(report["video_codec"]), "audio": report["has_audio"]},
    }
    silence_ranges = []
    integrated_lufs = None
    true_peak_dbfs = None
    if report["has_audio"]:
        result = _run([
            "ffmpeg", "-hide_banner", "-i", str(path),
            "-af", "silencedetect=n=-35dB:d=3.000,ebur128=peak=true",
            "-f", "null", "-",
        ], timeout)
        stderr = (result.stderr or b"").decode("utf-8", "replace")
        silence_ranges = _parse_silence_ranges(stderr, QUALITY_LONG_SILENCE_MS)
        integrated_lufs = _last_finite_metric(r"^\s*I:\s*(-?(?:\d+(?:\.\d+)?|inf))\s+LUFS", stderr)
        true_peak_dbfs = _last_finite_metric(r"^\s*Peak:\s*(-?(?:\d+(?:\.\d+)?|inf))\s+dBFS", stderr)
    checks["silence"] = {
        "passed": not silence_ranges,
        "threshold_ms": QUALITY_LONG_SILENCE_MS,
        "longest_ms": max((item["duration_ms"] for item in silence_ranges), default=0),
    }
    checks["loudness"] = {
        "passed": (integrated_lufs is not None and true_peak_dbfs is not None
                   and QUALITY_MIN_LUFS <= integrated_lufs <= QUALITY_MAX_LUFS
                   and true_peak_dbfs <= QUALITY_MAX_TRUE_PEAK_DBFS),
        "integrated_lufs": integrated_lufs,
        "true_peak_dbfs": true_peak_dbfs,
        "accepted_lufs": [QUALITY_MIN_LUFS, QUALITY_MAX_LUFS],
        "max_true_peak_dbfs": QUALITY_MAX_TRUE_PEAK_DBFS,
    }
    return {
        "decision": "passed" if all(item["passed"] for item in checks.values()) else "failed",
        "checks": checks,
        "silence_ranges": silence_ranges[:20],
        "output": report,
    }


def _keep_ranges(edl, duration_ms):
    if not isinstance(edl, dict) or not isinstance(edl.get("keep_ranges"), list):
        raise MediaError("EDL 格式无效")
    ranges = []
    cursor = 0
    for index, item in enumerate(edl["keep_ranges"]):
        if not isinstance(item, dict):
            raise MediaError("EDL 第%d段格式无效" % (index + 1))
        try:
            start_ms = int(item.get("source_start_ms"))
            end_ms = int(item.get("source_end_ms"))
        except (TypeError, ValueError):
            raise MediaError("EDL 时间格式无效")
        if start_ms < cursor or end_ms <= start_ms or end_ms > duration_ms:
            raise MediaError("EDL 时间范围无效")
        ranges.append((start_ms, end_ms))
        cursor = end_ms
    if not ranges:
        raise MediaError("EDL 没有保留画面")
    return ranges


def build_clean_master(source_path, edl, output_path, fps=30, timeout=1800):
    source_path = pathlib.Path(source_path).resolve()
    output_path = pathlib.Path(output_path).resolve()
    media = probe_media(source_path)
    ranges = _keep_ranges(edl, media["duration_ms"])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    filters = []
    concat_inputs = []
    for index, (start_ms, end_ms) in enumerate(ranges):
        start = start_ms / 1000.0
        end = end_ms / 1000.0
        filters.append(
            "[0:v]trim=start=%.3f:end=%.3f,setpts=PTS-STARTPTS[v%d]" %
            (start, end, index)
        )
        concat_inputs.append("[v%d]" % index)
        if media["has_audio"]:
            span = max(0.001, end - start)
            fade = min(0.012, span / 4.0)
            filters.append(
                "[0:a]atrim=start=%.3f:end=%.3f,asetpts=PTS-STARTPTS,"
                "afade=t=in:st=0:d=%.3f,afade=t=out:st=%.3f:d=%.3f[a%d]" %
                (start, end, fade, max(0.0, span - fade), fade, index)
            )
            concat_inputs.append("[a%d]" % index)
    if media["has_audio"]:
        filters.append("%sconcat=n=%d:v=1:a=1[vout][aout]" %
                       ("".join(concat_inputs), len(ranges)))
    else:
        filters.append("%sconcat=n=%d:v=1:a=0[vout]" %
                       ("".join(concat_inputs), len(ranges)))
    command = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(source_path), "-filter_complex", ";".join(filters),
        "-map", "[vout]",
    ]
    if media["has_audio"]:
        command.extend(["-map", "[aout]"])
    command.extend([
        "-r", str(int(fps)), "-c:v", "libx264", "-preset", "veryfast",
        "-crf", "19", "-pix_fmt", "yuv420p", "-g", str(int(fps)),
        "-keyint_min", str(int(fps)), "-sc_threshold", "0",
    ])
    if media["has_audio"]:
        command.extend(["-c:a", "aac", "-b:a", "192k", "-ar", "48000"])
    command.extend(["-movflags", "+faststart", str(output_path)])
    _run(command, timeout)
    output = probe_media(output_path)
    expected_ms = sum(end - start for start, end in ranges)
    if abs(output["duration_ms"] - expected_ms) > max(180, len(ranges) * 80):
        raise MediaError("粗剪输出时长异常")
    return output


def source_to_output_ms(source_ms, edl):
    try:
        source_ms = int(source_ms)
    except (TypeError, ValueError):
        raise MediaError("源时间格式无效")
    output_cursor = 0
    for item in edl.get("keep_ranges") or []:
        start_ms = int(item["source_start_ms"])
        end_ms = int(item["source_end_ms"])
        if start_ms <= source_ms <= end_ms:
            return output_cursor + source_ms - start_ms
        output_cursor += end_ms - start_ms
    return None


def remap_cues(cues, edl):
    result = []
    for cue in cues or []:
        start_ms = source_to_output_ms(cue.get("start_ms"), edl)
        end_ms = source_to_output_ms(cue.get("end_ms"), edl)
        text = str(cue.get("text") or "").strip()
        if start_ms is None or end_ms is None or end_ms <= start_ms or not text:
            continue
        result.append({"text": text, "start_ms": start_ms, "end_ms": end_ms})
    return result
