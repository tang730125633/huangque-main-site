"""Build paid 2K deliveries directly from verified native shot snapshots."""

import hashlib
import os
import shutil
import subprocess
import time
from pathlib import Path

from . import short_drama_assembly_plan as media_plan
from . import short_drama_native_audio


class FormalRenderError(RuntimeError):
    def __init__(self, code, message, status=409):
        super().__init__(message)
        self.code = str(code)
        self.status = int(status)


def _dimensions(ratio):
    if ratio == "16:9":
        return 2560, 1440
    if ratio == "9:16":
        return 1440, 2560
    raise FormalRenderError("delivery_ratio_invalid", "项目画幅不受支持")


def _srt_time(milliseconds):
    value = max(0, int(milliseconds))
    hours, value = divmod(value, 3600000)
    minutes, value = divmod(value, 60000)
    seconds, millis = divmod(value, 1000)
    return "%02d:%02d:%02d,%03d" % (hours, minutes, seconds, millis)


def _write_subtitles(path, subtitles):
    content = "\n\n".join(
        "%d\n%s --> %s\n%s" % (
            index, _srt_time(item["start_ms"]), _srt_time(item["end_ms"]),
            str(item["text"]).replace("\r", " ").replace("\n", " "),
        )
        for index, item in enumerate(subtitles, 1)
    )
    path.write_text(content + ("\n" if content else ""), encoding="utf-8")


def _stop(process):
    try:
        process.terminate()
    except OSError:
        pass
    try:
        return process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass
        return process.communicate()


def _run(command, cancel_event=None, timeout=1800):
    try:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
    except OSError as error:
        raise FormalRenderError(
            "formal_renderer_unavailable", "2K 导出执行器不可用", 503,
        ) from error
    deadline = time.monotonic() + max(1, int(timeout))
    while True:
        if cancel_event is not None and cancel_event.is_set():
            _stop(process)
            raise FormalRenderError("formal_render_cancelled", "2K 正式导出已取消")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _stop(process)
            raise FormalRenderError(
                "formal_renderer_unavailable", "2K 正式导出超时", 503,
            )
        try:
            stdout, stderr = process.communicate(timeout=min(0.25, remaining))
            return subprocess.CompletedProcess(
                command, process.returncode, stdout, stderr,
            )
        except subprocess.TimeoutExpired:
            continue


def render_native_2k(
    sources, ratio, duration_ms, media_contract, output, cancel_event=None,
    shot_durations_ms=None,
):
    """Concatenate ordered native shots; never consumes a composed preview."""
    paths = [Path(item) for item in sources]
    if not paths or any(not item.is_file() for item in paths):
        raise FormalRenderError(
            "delivery_native_sources_missing", "正式成片缺少已锁定的原生镜头",
        )
    width, height = _dimensions(str(ratio or ""))
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        os.environ.get("FFMPEG_BIN", "ffmpeg"), "-y", "-hide_banner",
        "-loglevel", "error",
    ]
    probes = []
    for source in paths:
        command.extend(["-i", str(source)])
        try:
            probes.append(media_plan.probe_media(source))
        except media_plan.MediaPlanError as error:
            raise FormalRenderError(error.code, str(error)) from error

    locked_durations = [int(value or 0) for value in (shot_durations_ms or [])]
    if (
        len(locked_durations) != len(paths)
        or any(value <= 0 for value in locked_durations)
        or sum(locked_durations) != int(duration_ms or 0)
    ):
        raise FormalRenderError(
            "delivery_locked_timeline_invalid",
            "正式成片缺少完整且连续的锁定镜头时间线",
        )

    subtitles = list((media_contract or {}).get("subtitles") or [])
    subtitle_input = None
    if subtitles:
        subtitle_input = output.parent / "locked-subtitles.srt"
        _write_subtitles(subtitle_input, subtitles)
        command.extend(["-f", "srt", "-i", str(subtitle_input)])

    filters = []
    for index, probe in enumerate(probes):
        if not probe.get("video") or not probe.get("audio"):
            raise FormalRenderError(
                "delivery_native_streams_missing",
                "原生 2K 镜头必须同时包含画面和声音",
            )
        shot_duration = locked_durations[index] / 1000.0
        filters.append(
            "[%d:v:0]scale=%d:%d:force_original_aspect_ratio=increase:flags=lanczos,"
            "crop=%d:%d,fps=25,setsar=1,"
            "tpad=stop_mode=clone:stop_duration=%.3f,trim=duration=%.3f,"
            "setpts=PTS-STARTPTS[v%d]" % (
                index, width, height, width, height,
                shot_duration, shot_duration, index,
            )
        )
        filters.append(
            "[%d:a:0]aresample=48000,aformat=channel_layouts=stereo,"
            "apad=whole_dur=%.3f,atrim=duration=%.3f,"
            "asetpts=PTS-STARTPTS[a%d]" % (
                index, shot_duration, shot_duration, index,
            )
        )
    paired_labels = "".join(
        "[v%d][a%d]" % (index, index) for index in range(len(paths))
    )
    filters.append(
        "%sconcat=n=%d:v=1:a=1[outv][outa]" % (paired_labels, len(paths))
    )
    command.extend([
        "-filter_complex", ";".join(filters), "-map", "[outv]", "-map", "[outa]",
    ])
    if subtitle_input:
        command.extend(["-map", "%d:0" % len(paths)])
    command.extend([
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
    ])
    if subtitle_input:
        command.extend(["-c:s", "mov_text"])
    if int(duration_ms or 0) > 0:
        command.extend(["-t", "%.3f" % (int(duration_ms) / 1000.0)])
    command.extend(["-movflags", "+faststart", str(output)])

    completed = _run(command, cancel_event=cancel_event)
    if completed.returncode != 0 or not output.is_file():
        raise FormalRenderError(
            "formal_render_failed",
            str(completed.stderr or "2K 正式导出失败").strip()[-500:],
        )
    try:
        native_audio = short_drama_native_audio.inspect_native_audio(output)
        probe = media_plan.probe_media(output)
    except (short_drama_native_audio.NativeAudioError, media_plan.MediaPlanError) as error:
        raise FormalRenderError(
            "delivery_output_media_invalid", str(error),
        ) from error
    actual = media_plan.dimensions_for_ratio(probe)
    if actual != (width, height):
        raise FormalRenderError(
            "delivery_dimensions_invalid", "正式成片尺寸与项目画幅不一致",
        )
    if int(duration_ms or 0) and abs(int(probe.get("duration_ms") or 0) - int(duration_ms)) > 1500:
        raise FormalRenderError(
            "delivery_duration_invalid", "正式成片时长与锁定时间线不一致",
        )
    subtitle_count = 0
    try:
        checked = subprocess.run([
            os.environ.get("FFPROBE_BIN", "ffprobe"), "-v", "error",
            "-select_streams", "s", "-show_entries", "stream=index",
            "-of", "csv=p=0", str(output),
        ], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise FormalRenderError("media_probe_failed", "字幕流验证失败") from error
    if checked.returncode == 0:
        subtitle_count = len([line for line in checked.stdout.splitlines() if line.strip()])
    if (media_contract or {}).get("subtitle_required") and subtitle_count < 1:
        raise FormalRenderError("delivery_subtitle_missing", "正式成片缺少锁定字幕流")
    digest = hashlib.sha256()
    with output.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "probe": probe, "subtitle_streams": subtitle_count,
        "native_audio": native_audio, "sha256": digest.hexdigest(),
    }


def _digest_and_identity(path):
    path = Path(path)
    try:
        before = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        after = path.stat()
    except OSError as error:
        raise FormalRenderError(
            "delivery_output_identity_changed",
            "正式成片在发布前已被删除或替换",
        ) from error
    identity_before = (
        before.st_dev, before.st_ino, before.st_size,
        before.st_mtime_ns, before.st_ctime_ns,
    )
    identity_after = (
        after.st_dev, after.st_ino, after.st_size,
        after.st_mtime_ns, after.st_ctime_ns,
    )
    if identity_before != identity_after:
        raise FormalRenderError(
            "delivery_output_identity_changed",
            "正式成片在校验期间发生变化",
        )
    return digest.hexdigest(), identity_after


def publish_validated_output(temp_dir, target_dir, filename, expected_sha256):
    """Atomically publish exactly the bytes validated by the renderer."""
    temp_dir = Path(temp_dir)
    target_dir = Path(target_dir)
    source = temp_dir / str(filename)
    expected = str(expected_sha256 or "").lower()
    digest, identity = _digest_and_identity(source)
    if not expected or digest != expected:
        raise FormalRenderError(
            "delivery_output_identity_changed",
            "正式成片发布前哈希与渲染结果不一致",
        )
    if target_dir.exists():
        raise FormalRenderError(
            "delivery_output_publish_conflict", "正式成片发布目标已存在",
        )
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    published = False
    try:
        temp_dir.rename(target_dir)
        published = True
        published_digest, published_identity = _digest_and_identity(
            target_dir / str(filename),
        )
        if published_identity != identity or published_digest != expected:
            raise FormalRenderError(
                "delivery_output_identity_changed",
                "正式成片在原子发布期间被替换",
            )
    except FormalRenderError:
        if published:
            shutil.rmtree(target_dir, ignore_errors=True)
        raise
    except OSError as error:
        if published:
            shutil.rmtree(target_dir, ignore_errors=True)
        raise FormalRenderError(
            "delivery_output_publish_failed", "正式成片原子发布失败", 503,
        ) from error
    return {"sha256": published_digest, "identity": published_identity}
