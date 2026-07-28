"""D-3 preview and D-4 resumable formal-export renderers."""

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

from . import short_drama_assembly as assembly
from . import short_drama_assembly_artifacts as artifacts
from . import short_drama_assembly_engine as d2_engine
from . import short_drama_assembly_plan as media_plan
from . import short_drama_assembly_subtitles as subtitles


RENDER_TIMEOUT_SECONDS = 1200
PREVIEW_PROFILE = {
    "9:16": (720, 1280),
    "16:9": (1280, 720),
}
FINAL_PROFILE = {
    "9:16": (1080, 1920),
    "16:9": (1920, 1080),
}


class PreviewRenderError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _output_root():
    server_dir = Path(__file__).resolve().parents[1]
    return Path(os.environ.get(
        "CONTENT_OUT", str(server_dir / "content_out")
    )).resolve()


def _run(command, timeout=RENDER_TIMEOUT_SECONDS):
    try:
        result = subprocess.run(
            [str(item) for item in command],
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError as error:
        raise PreviewRenderError(
            "ffmpeg_unavailable", "服务器未安装或无法调用 FFmpeg"
        ) from error
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PreviewRenderError(
            "preview_render_failed", "预览合成执行失败或超时"
        ) from error
    if result.returncode != 0:
        detail = str(result.stderr or "").strip()[-500:]
        raise PreviewRenderError(
            "preview_render_failed", detail or "预览合成失败"
        )
    return result


def _toolchain():
    ffmpeg = os.environ.get("FFMPEG_BIN", "ffmpeg")
    ffprobe = os.environ.get("FFPROBE_BIN", "ffprobe")
    version = _run([ffmpeg, "-version"], timeout=10)
    probe_version = media_plan.inspect_ffprobe()
    font = subtitles.inspect_font()
    return {
        "ffmpeg": str(version.stdout or "").splitlines()[0][:200],
        "ffprobe": probe_version,
        "font": font["file"],
        "font_family": font["family"],
        "font_dir": font["font_dir"],
        "ffmpeg_bin": ffmpeg,
        "ffprobe_bin": ffprobe,
    }


def _ready_bundle_valid(project_id, input_hash):
    files = artifacts.ready_files(_jdb(), project_id, input_hash)
    root = _output_root()
    required = {("master_audio", ""), ("subtitles_ass", "")}
    return required.issubset(files) and all(
        (root / Path(value)).is_file() for value in files.values()
    )


def _jdb():
    # Imported lazily to avoid registry -> core -> registry cycles.
    from . import core
    return core.jdb


def _ensure_d2(context, tools):
    db_factory = _jdb()
    project_id = context["project"]["id"]
    snapshot = context["snapshot"]
    d1_hash = snapshot["input_hash"]
    d2_hash = snapshot["audio_subtitle"]["input_hash"]
    claim = artifacts.claim_build(
        db_factory, project_id, d1_hash, d2_hash,
        # A project-level active-job mutex guarantees one D-3 builder. Reclaim
        # immediately so a restarted local job can adopt a bundle that was
        # renamed to its final directory just before the process died.
        stale_after_seconds=0,
        ready_validator=_ready_bundle_valid,
    )
    if claim["status"] == "ready":
        return artifacts.ready_files(db_factory, project_id, d2_hash)
    if claim["status"] != "claimed":
        raise PreviewRenderError(
            "active_composition_job", "音频与字幕中间产物正在构建"
        )
    token = claim["claim_token"]
    root = _output_root().resolve()
    reusable_source = artifacts.reusable_audio_files(
        db_factory,
        project_id,
        snapshot.get("master_audio", {}).get("master_audio_hash"),
    )
    source_input_hash = reusable_source.get("source_input_hash")
    reusable = {}
    cache_path_invalid = False
    for key, record in reusable_source.get("files", {}).items():
        try:
            path = (root / Path(record.get("file") or "")).resolve()
            path.relative_to(root)
        except (OSError, RuntimeError, ValueError):
            cache_path_invalid = True
            break
        reusable[key] = {**record, "path": path}
    if cache_path_invalid:
        artifacts.mark_reusable_audio_stale(
            db_factory, project_id, source_input_hash,
            "audio_cache_path_invalid",
        )
        reusable = {}

    def build(reusable_files):
        return d2_engine.build_bundle(
            output_root=root,
            project_id=project_id,
            d1_input_hash=d1_hash,
            input_hash=d2_hash,
            ratio=context["project"]["ratio"],
            config=snapshot["config"],
            media_plan=snapshot["media_plan"],
            shot_inputs=context["shot_inputs"],
            runner=subprocess.run,
            probe=media_plan.probe_media,
            identity_check=lambda: (
                assembly.preview_render_context(
                    db_factory, context["payload"]["_job_id"]
                )["snapshot"]["input_hash"] == d1_hash
            ),
            claim_token=token,
            claim_check=lambda: artifacts.claim_is_current(
                db_factory, project_id, d2_hash, token
            ),
            toolchain=tools,
            bgm_source=context.get("bgm_source"),
            master_audio_contract=snapshot.get("master_audio"),
            cached_audio_files=reusable_files,
        )

    try:
        try:
            result = build(reusable)
        except d2_engine.ReusableAudioCacheError:
            artifacts.mark_reusable_audio_stale(
                db_factory, project_id, source_input_hash,
                "audio_cache_hash_mismatch",
            )
            result = build({})
        artifacts.record_ready(
            db_factory, project_id, d1_hash, d2_hash,
            result["artifacts"], result["manifest"], token,
        )
    except Exception as error:
        code = getattr(error, "code", "preview_render_failed")
        artifacts.mark_failed(db_factory, project_id, d2_hash, code, token)
        raise
    return artifacts.ready_files(db_factory, project_id, d2_hash)


def _ass_filter(path, font_dir):
    value = Path(path).resolve().as_posix()
    value = value.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    directory = Path(font_dir).resolve().as_posix()
    directory = directory.replace(
        "\\", "\\\\"
    ).replace(":", "\\:").replace("'", "\\'")
    return "subtitles=filename='%s':fontsdir='%s'" % (value, directory)


def build_preview_command(
    videos, master_audio, subtitles_ass, ratio, output, font_dir
):
    width, height = PREVIEW_PROFILE[ratio]
    ffmpeg = os.environ.get("FFMPEG_BIN", "ffmpeg")
    command = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
    for item in videos:
        command.extend(["-i", str(item["file"])])
    command.extend(["-i", str(master_audio)])
    filters = []
    labels = []
    for index, item in enumerate(videos):
        duration = int(item["duration_ms"]) / 1000
        label = "v%d" % index
        filters.append(
            "[%d:v]setpts=PTS-STARTPTS,"
            "tpad=stop_mode=clone:stop_duration=%.3f,trim=duration=%.3f,"
            "scale=%d:%d:force_original_aspect_ratio=increase,"
            "crop=%d:%d,fps=30,setsar=1,format=yuv420p[%s]"
            % (
                index, duration, duration, width, height,
                width, height, label,
            )
        )
        labels.append("[%s]" % label)
    filters.append(
        "%sconcat=n=%d:v=1:a=0[joined]" % ("".join(labels), len(labels))
    )
    filters.append("[joined]%s[vout]" % _ass_filter(subtitles_ass, font_dir))
    duration = sum(int(item["duration_ms"]) for item in videos) / 1000
    command.extend([
        "-filter_complex", ";".join(filters),
        "-map", "[vout]", "-map", "%d:a:0" % len(videos),
        "-t", "%.3f" % duration,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
        "-pix_fmt", "yuv420p", "-r", "30",
        "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "160k",
        "-movflags", "+faststart", str(output),
    ])
    return command


def build_final_command(
    videos, master_audio, subtitles_ass, ratio, output, font_dir
):
    command = build_preview_command(
        videos, master_audio, subtitles_ass, ratio, output, font_dir
    )
    width, height = FINAL_PROFILE[ratio]
    preview_width, preview_height = PREVIEW_PROFILE[ratio]
    filter_value = command[command.index("-filter_complex") + 1]
    filter_value = filter_value.replace(
        "scale=%d:%d" % (preview_width, preview_height),
        "scale=%d:%d" % (width, height),
    ).replace(
        "crop=%d:%d" % (preview_width, preview_height),
        "crop=%d:%d" % (width, height),
    )
    command[command.index("-filter_complex") + 1] = filter_value
    command[command.index("-preset") + 1] = "medium"
    command[command.index("-crf") + 1] = "20"
    command[command.index("-b:a") + 1] = "192k"
    command.insert(command.index("-pix_fmt"), "-profile:v")
    command.insert(command.index("-pix_fmt"), "high")
    return command


def _validate_preview(path, ratio, expected_duration_ms):
    probe = media_plan.probe_media(path)
    video = probe.get("video") or {}
    audio = probe.get("audio") or {}
    expected_width, expected_height = PREVIEW_PROFILE[ratio]
    if (
        video.get("width") != expected_width
        or video.get("height") != expected_height
        or video.get("codec") != "h264"
        or video.get("pix_fmt") != "yuv420p"
        or audio.get("codec") != "aac"
        or audio.get("sample_rate") != 48000
        or audio.get("channels") != 2
        or abs(probe["duration_ms"] - expected_duration_ms) > 300
    ):
        raise PreviewRenderError(
            "preview_render_failed", "预览成片规格校验失败"
        )
    return probe


def _validate_final(path, ratio, expected_duration_ms):
    probe = media_plan.probe_media(path)
    video = probe.get("video") or {}
    audio = probe.get("audio") or {}
    expected_width, expected_height = FINAL_PROFILE[ratio]
    if (
        video.get("width") != expected_width
        or video.get("height") != expected_height
        or video.get("codec") != "h264"
        or video.get("pix_fmt") != "yuv420p"
        or audio.get("codec") != "aac"
        or audio.get("sample_rate") != 48000
        or audio.get("channels") != 2
        or abs(probe["duration_ms"] - expected_duration_ms) > 300
    ):
        raise PreviewRenderError(
            "render_failed", "1080p 正式成片规格校验失败"
        )
    return probe


def _file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verified_private_upload(path, object_key, content_type, sha256):
    from . import cos
    if not cos.enabled():
        raise PreviewRenderError(
            "export_unavailable", "正式导出对象存储未配置"
        )
    def inspect_remote():
        head = cos.head(object_key)
        normalized = {
            str(key).lower(): value for key, value in dict(head).items()
        }
        return (
            int(
                normalized.get("content-length")
                or normalized.get("content_length")
                or -1
            ),
            str(
                normalized.get("x-cos-meta-sha256")
                or normalized.get("sha256")
                or ""
            ).strip('"'),
        )

    try:
        remote_size, remote_hash = inspect_remote()
    except Exception:
        remote_size, remote_hash = -1, ""
    if remote_size == path.stat().st_size and remote_hash == sha256:
        return cos.object_url(object_key, private=True)
    url = cos.upload(
        path, object_key, content_type, private=True,
        metadata={"sha256": sha256},
    )
    remote_size, remote_hash = inspect_remote()
    if remote_size != path.stat().st_size or (
        remote_hash != sha256
    ):
        raise PreviewRenderError(
            "upload_failed", "对象存储上传完整性校验失败"
        )
    return url


def run_preview_job(payload):
    db_factory = _jdb()
    job_id = int(payload.get("_job_id") or 0)
    payload = dict(payload)
    payload["_job_id"] = job_id
    assembly.set_preview_progress(db_factory, job_id, "preparing", 5)
    context = assembly.preview_render_context(db_factory, job_id)
    context["payload"]["_job_id"] = job_id
    tools = _toolchain()
    d2_files = _ensure_d2(context, tools)
    root = _output_root()
    master = root / Path(d2_files[("master_audio", "")])
    subtitles_ass = root / Path(d2_files[("subtitles_ass", "")])
    project_id = context["project"]["id"]
    target_dir = root / "short_drama_preview" / project_id / str(job_id)
    temp_dir = target_dir.with_name(".%s.tmp" % target_dir.name)
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)
    preview = temp_dir / "preview.mp4"
    cover = temp_dir / "cover.jpg"
    try:
        assembly.set_preview_progress(
            db_factory, job_id, "rendering_shots", 20
        )
        _run(build_preview_command(
            context["videos"], master, subtitles_ass,
            context["project"]["ratio"], preview, tools["font_dir"],
        ))
        assembly.set_preview_progress(db_factory, job_id, "concatenating", 88)
        expected_duration = sum(
            item["duration_ms"] for item in context["videos"]
        )
        probe = _validate_preview(
            preview, context["project"]["ratio"], expected_duration
        )
        assembly.set_preview_progress(db_factory, job_id, "cover", 94)
        _run([
            tools["ffmpeg_bin"], "-y", "-hide_banner", "-loglevel", "error",
            "-ss", "%.3f" % min(1.0, expected_duration / 2000),
            "-i", str(preview), "-frames:v", "1", "-q:v", "2", str(cover),
        ], timeout=60)
        assembly.set_preview_progress(db_factory, job_id, "finalizing", 98)
        if target_dir.exists():
            shutil.rmtree(target_dir)
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        temp_dir.rename(target_dir)
        relative = target_dir.relative_to(root).as_posix()
        video = probe["video"]
        audio = probe["audio"]
        return {
            "file": relative + "/preview.mp4",
            "url": "/api/gen/file/" + relative + "/preview.mp4",
            "cover_file": relative + "/cover.jpg",
            "duration_ms": probe["duration_ms"],
            "width": video["width"], "height": video["height"],
            "fps": video["fps"], "video_codec": video["codec"],
            "audio_codec": audio["codec"],
        }
    finally:
        if temp_dir.exists():
            shutil.rmtree(temp_dir)


def run_final_job(payload):
    db_factory = _jdb()
    job_id = int(payload.get("_job_id") or 0)
    assembly.set_final_progress(db_factory, job_id, "preparing", 5)
    context = assembly.final_render_context(db_factory, job_id)
    context["payload"]["_job_id"] = job_id
    tools = _toolchain()
    d2_files = _ensure_d2(context, tools)
    root = _output_root()
    master = root / Path(d2_files[("master_audio", "")])
    subtitles_ass = root / Path(d2_files[("subtitles_ass", "")])
    project_id = context["project"]["id"]
    target_dir = root / "short_drama_final" / project_id / str(job_id)
    temp_dir = target_dir.with_name(".%s.tmp" % target_dir.name)
    temp_dir.mkdir(parents=True, exist_ok=True)
    published_local = (
        (target_dir / "final.mp4").is_file()
        and (target_dir / "cover.jpg").is_file()
    )
    working_dir = target_dir if published_local else temp_dir
    part = temp_dir / "final.part.mp4"
    final = working_dir / "final.mp4"
    cover = working_dir / "cover.jpg"
    expected_duration = sum(item["duration_ms"] for item in context["videos"])
    try:
        assembly.set_final_progress(db_factory, job_id, "rendering", 12)
        if not final.is_file():
            _run(build_final_command(
                context["videos"], master, subtitles_ass,
                context["project"]["ratio"], part, tools["font_dir"],
            ))
            os.replace(part, final)
        assembly.set_final_progress(db_factory, job_id, "probing", 72)
        probe = _validate_final(
            final, context["project"]["ratio"], expected_duration
        )
        max_bytes = int(
            os.environ.get(
                "SHORT_DRAMA_FINAL_MAX_BYTES", str(500 * 1024 * 1024)
            )
        )
        if final.stat().st_size > max(1, max_bytes):
            raise PreviewRenderError(
                "export_too_large", "正式成片超过文件大小上限"
            )
        assembly.set_final_progress(db_factory, job_id, "cover", 78)
        if not cover.is_file():
            _run([
                tools["ffmpeg_bin"], "-y", "-hide_banner", "-loglevel", "error",
                "-ss", "%.3f" % (context["payload"]["cover_time_ms"] / 1000),
                "-i", str(final), "-frames:v", "1", "-q:v", "2",
                "-vf", "format=yuvj420p", str(cover),
            ], timeout=60)
        video_hash = _file_sha256(final)
        cover_hash = _file_sha256(cover)
        owner_hash = hashlib.sha256(
            context["payload"]["owner_username"].encode("utf-8")
        ).hexdigest()[:16]
        prefix = "short-drama/%s/%s/final/v%d" % (
            owner_hash, project_id, context["final_version"]
        )
        video_key = "%s/%s.mp4" % (prefix, video_hash)
        cover_key = "%s/%s-cover.jpg" % (prefix, cover_hash)
        assembly.set_final_progress(
            db_factory, job_id, "uploading_video", 83
        )
        video_url = _verified_private_upload(
            final, video_key, "video/mp4", video_hash
        )
        assembly.set_final_progress(
            db_factory, job_id, "uploading_cover", 92
        )
        cover_url = _verified_private_upload(
            cover, cover_key, "image/jpeg", cover_hash
        )
        if not published_local:
            if target_dir.exists():
                shutil.rmtree(target_dir)
            target_dir.parent.mkdir(parents=True, exist_ok=True)
            temp_dir.rename(target_dir)
        relative = target_dir.relative_to(root).as_posix()
        video = probe["video"]
        audio = probe["audio"]
        result = {
            "mode": "short_drama_final",
            "file": relative + "/final.mp4",
            "url": video_url,
            "video_file": relative + "/final.mp4",
            "video_url": video_url,
            "cover_file": relative + "/cover.jpg",
            "cover_url": cover_url,
            "object_key": video_key, "cover_key": cover_key,
            "duration_ms": probe["duration_ms"],
            "width": video["width"], "height": video["height"],
            "resolution": "%dx%d" % (video["width"], video["height"]),
            "ratio": context["project"]["ratio"],
            "fps": video["fps"], "video_codec": video["codec"],
            "audio_codec": audio["codec"],
            "size": (target_dir / "final.mp4").stat().st_size,
            "sha256": video_hash,
            "phase": "completed", "status": "done",
            "asset_owner": context["payload"]["owner_username"],
        }
        assembly.set_final_progress(db_factory, job_id, "archiving", 96)
        asset = assembly.archive_final_asset(db_factory, job_id, result)
        result["asset_id"] = asset["id"]
        return result
    finally:
        if part.exists():
            part.unlink()


HANDLERS = {
    "short_drama_preview": run_preview_job,
    "short_drama_final": run_final_job,
}
