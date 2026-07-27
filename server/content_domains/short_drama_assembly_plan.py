"""Pure media planning helpers for short-drama assembly D-1.

This module probes only server-controlled local files.  It never invokes a
shell, mutates project state, submits provider jobs, or starts FFmpeg renders.
"""

import hashlib
import json
import os
import re
import subprocess
from fractions import Fraction
from pathlib import Path, PurePosixPath


PLANNER_VERSION = "short_drama_media_plan_v1"
DURATION_TOLERANCE_MS = 200
PROBE_TIMEOUT_SECONDS = 15
RATIO_TOLERANCE = 0.03

TARGET_PROFILES = {
    "9:16": {
        "preview": {"resolution": {"width": 540, "height": 960}},
        "final": {"resolution": {"width": 1080, "height": 1920}},
    },
    "16:9": {
        "preview": {"resolution": {"width": 960, "height": 540}},
        "final": {"resolution": {"width": 1920, "height": 1080}},
    },
}


class MediaPlanError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _ffprobe():
    return os.environ.get("FFPROBE_BIN", "ffprobe")


def canonical_json(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def canonical_hash(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def duration_action(actual_ms, target_ms, tolerance_ms=DURATION_TOLERANCE_MS):
    actual_ms = int(actual_ms)
    target_ms = int(target_ms)
    delta = actual_ms - target_ms
    if abs(delta) <= tolerance_ms:
        return "keep"
    return "trim_tail" if delta > 0 else "freeze_last_frame"


def _timeline_blocker(code, line_id=None):
    blocker = {"code": code}
    if line_id is not None:
        blocker["line_id"] = line_id
    return blocker


def validate_timeline(lines, duration_limit_ms):
    """Normalize by start time, then validate bounds and adjacent intervals."""
    normalized = sorted(
        [dict(item) for item in lines],
        key=lambda item: (
            item.get("start_ms") if type(item.get("start_ms")) is int else -1,
            item.get("end_ms") if type(item.get("end_ms")) is int else -1,
            str(item.get("id") or ""),
        ),
    )
    blockers = []
    audio_intervals = []
    subtitle_intervals = []
    for line in normalized:
        line_id = line.get("id")
        start_ms = line.get("start_ms")
        end_ms = line.get("end_ms")
        audio_duration_ms = line.get("audio_duration_ms")
        invalid_structure = (
            type(start_ms) is not int
            or type(end_ms) is not int
            or type(audio_duration_ms) is not int
            or start_ms < 0
            or end_ms <= start_ms
            or audio_duration_ms <= 0
        )
        if (
            invalid_structure
            or end_ms > duration_limit_ms
            or start_ms + audio_duration_ms > duration_limit_ms
            or (
                line.get("subtitle_visible")
                and not str(line.get("subtitle_text") or "").strip()
            )
        ):
            blockers.append(_timeline_blocker("timeline_invalid", line_id))
        if invalid_structure:
            continue
        audio_intervals.append(
            (start_ms, start_ms + audio_duration_ms, str(line_id))
        )
        if line.get("subtitle_visible"):
            subtitle_intervals.append((start_ms, end_ms, str(line_id)))
    for previous, current in zip(audio_intervals, audio_intervals[1:]):
        if current[0] < previous[1]:
            blockers.append(_timeline_blocker("audio_overlap", current[2]))
    for previous, current in zip(subtitle_intervals, subtitle_intervals[1:]):
        if current[0] < previous[1]:
            blockers.append(_timeline_blocker("subtitle_overlap", current[2]))
    return normalized, blockers


def resolve_controlled_file(file_key, output_dir=None):
    """Resolve an opaque output key without accepting absolute paths/traversal."""
    value = str(file_key or "").replace("\\", "/").strip()
    if not value:
        raise MediaPlanError("missing_source_file", "服务端媒体源文件不存在")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or Path(value).is_absolute()
        or (path.parts and ":" in path.parts[0])
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise MediaPlanError(
            "source_file_untrusted", "媒体文件不在服务端受控输出目录中"
        )
    if output_dir is None:
        server_dir = Path(__file__).resolve().parents[1]
        output_dir = Path(
            os.environ.get("CONTENT_OUT", str(server_dir / "content_out"))
        )
    root = Path(output_dir).resolve()
    candidate = (root / Path(*path.parts)).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise MediaPlanError(
            "source_file_untrusted", "媒体文件不在服务端受控输出目录中"
        ) from error
    if not candidate.is_file():
        raise MediaPlanError("missing_source_file", "服务端媒体源文件不存在")
    return candidate


def _positive_int(value):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _fps(value):
    try:
        parsed = float(Fraction(str(value)))
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return round(parsed, 6) if parsed > 0 else None


def _duration_ms(payload, streams):
    candidates = [payload.get("format", {}).get("duration")]
    candidates.extend(stream.get("duration") for stream in streams)
    for value in candidates:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return int(round(parsed * 1000))
    return None


def _normalized_rotation(value):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not parsed.is_integer():
        return None
    return int(parsed) % 360


def _video_rotation(video_stream):
    side_data = video_stream.get("side_data_list")
    if isinstance(side_data, list):
        for item in side_data:
            if not isinstance(item, dict) or "rotation" not in item:
                continue
            rotation = _normalized_rotation(item.get("rotation"))
            if rotation is not None:
                return rotation
    tags = video_stream.get("tags")
    tags = tags if isinstance(tags, dict) else {}
    rotation = _normalized_rotation(tags.get("rotate"))
    return rotation if rotation is not None else 0


def probe_media(path, runner=subprocess.run):
    command = [
        _ffprobe(), "-v", "error", "-show_streams", "-show_format",
        "-of", "json", str(path),
    ]
    try:
        result = runner(
            command,
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as error:
        raise MediaPlanError(
            "ffprobe_unavailable", "服务器未安装或无法调用 FFprobe"
        ) from error
    except (OSError, subprocess.TimeoutExpired) as error:
        raise MediaPlanError("media_probe_failed", "媒体探测执行失败") from error
    if result.returncode != 0:
        raise MediaPlanError("media_probe_failed", "媒体文件无法被 FFprobe 解析")
    try:
        payload = json.loads(result.stdout)
    except (TypeError, ValueError) as error:
        raise MediaPlanError("media_probe_failed", "FFprobe 返回了无效数据") from error
    streams = payload.get("streams")
    if not isinstance(streams, list):
        raise MediaPlanError("media_probe_failed", "媒体文件缺少可解析的流")
    video_stream = next(
        (item for item in streams if item.get("codec_type") == "video"), None
    )
    audio_stream = next(
        (item for item in streams if item.get("codec_type") == "audio"), None
    )
    duration_ms = _duration_ms(payload, streams)
    if not duration_ms:
        raise MediaPlanError("media_probe_failed", "媒体时长无法确定")
    video = None
    if video_stream:
        video = {
            "codec": str(video_stream.get("codec_name") or ""),
            "width": _positive_int(video_stream.get("width")),
            "height": _positive_int(video_stream.get("height")),
            "fps": _fps(
                video_stream.get("avg_frame_rate")
                or video_stream.get("r_frame_rate")
            ),
            "pix_fmt": str(video_stream.get("pix_fmt") or ""),
            "sar": str(video_stream.get("sample_aspect_ratio") or ""),
            "rotation": _video_rotation(video_stream),
        }
    audio = None
    if audio_stream:
        audio = {
            "codec": str(audio_stream.get("codec_name") or ""),
            "sample_rate": _positive_int(audio_stream.get("sample_rate")),
            "channels": _positive_int(audio_stream.get("channels")),
        }
    return {"duration_ms": duration_ms, "video": video, "audio": audio}


def inspect_ffprobe(runner=subprocess.run):
    try:
        result = runner(
            [_ffprobe(), "-version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError as error:
        raise MediaPlanError(
            "ffprobe_unavailable", "服务器未安装或无法调用 FFprobe"
        ) from error
    except (OSError, subprocess.TimeoutExpired) as error:
        raise MediaPlanError("media_probe_failed", "FFprobe 检查失败") from error
    first_line = str(result.stdout or "").splitlines()
    first_line = first_line[0].strip() if first_line else ""
    match = re.match(r"^ffprobe version\s+([0-9]+)", first_line, re.I)
    if result.returncode != 0 or not match or int(match.group(1)) < 4:
        raise MediaPlanError(
            "media_probe_failed", "FFprobe 版本不满足 D-2 要求"
        )
    return first_line[:200]


def file_fingerprint(path):
    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as source:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return {"sha256": digest.hexdigest(), "size": size}


def stable_probe(path, probe=probe_media):
    path = Path(path)
    before = path.stat()
    media = probe(path)
    fingerprint = file_fingerprint(path)
    after = path.stat()
    if (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or fingerprint["size"] != after.st_size
    ):
        raise MediaPlanError(
            "source_changed_during_probe", "媒体源文件在探测期间发生变化"
        )
    return {"probe": media, "fingerprint": fingerprint}


def dimensions_for_ratio(probe):
    video = probe.get("video") or {}
    width = video.get("width")
    height = video.get("height")
    if not width or not height:
        return None, None
    if video.get("rotation") in {90, 270}:
        return height, width
    return width, height


def ratio_matches(probe, expected_ratio):
    width, height = dimensions_for_ratio(probe)
    if not width or not height or expected_ratio not in TARGET_PROFILES:
        return False
    expected_width, expected_height = map(int, expected_ratio.split(":"))
    return abs((width / height) - (expected_width / expected_height)) <= RATIO_TOLERANCE


def build_normalization_plan(ratio, target_duration_seconds, shots):
    if ratio not in TARGET_PROFILES:
        raise MediaPlanError("ratio_mismatch", "项目画幅不受媒体计划支持")
    ordered = sorted(
        shots,
        key=lambda item: (item.get("sort_order", 0), str(item.get("id") or "")),
    )
    cursor = 0
    planned_shots = []
    for shot in ordered:
        duration_ms = int(shot["duration_ms"])
        probe = shot["video_probe"]
        planned_shots.append({
            "id": shot["id"],
            "start_ms": cursor,
            "end_ms": cursor + duration_ms,
            "duration_ms": duration_ms,
            "video": {
                "duration_action": duration_action(
                    probe["duration_ms"], duration_ms
                ),
                "source_duration_ms": probe["duration_ms"],
                "discard_source_audio": True,
                "scale": "fit_crop",
                "fps": 30,
                "pix_fmt": "yuv420p",
                "sar": "1:1",
            },
            "audio": {
                "sample_rate": 48000,
                "channels": 2,
                "lines": list(shot.get("voice_lines") or []),
            },
        })
        cursor += duration_ms
    target_duration_ms = int(target_duration_seconds) * 1000
    profiles = {}
    for kind, values in TARGET_PROFILES[ratio].items():
        profiles[kind] = {
            "resolution": dict(values["resolution"]),
            "fps": 30,
            "pix_fmt": "yuv420p",
            "sar": "1:1",
            "audio_sample_rate": 48000,
            "audio_channels": 2,
        }
    return {
        "planner_version": PLANNER_VERSION,
        "ratio": ratio,
        "target_duration_ms": target_duration_ms,
        "project_duration_ms": cursor,
        "profiles": profiles,
        "shots": planned_shots,
    }
