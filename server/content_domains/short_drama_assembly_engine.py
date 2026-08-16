"""Atomic D-2 audio/subtitle bundle builder consumed by future D-3 jobs."""

import hashlib
import hmac
import json
import re
import shutil
import tempfile
from pathlib import Path, PurePosixPath

from . import short_drama_assembly_audio as audio
from . import short_drama_assembly_subtitles as subtitles


SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
CLAIM_TOKEN = re.compile(r"^[0-9a-f]{32}$")
RECOVERABLE_ARTIFACT_KINDS = {
    "shot_voice", "dialogue", "bgm", "master_audio", "subtitles_ass",
}


class BundleBuildError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class ReusableAudioCacheError(BundleBuildError):
    def __init__(self, message="主音轨缓存文件校验失败"):
        super().__init__("audio_cache_hash_mismatch", message)


def _hash_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _audio_artifact(kind, path, output_root, duration_ms, shot_id=""):
    return {
        "kind": kind,
        "shot_id": shot_id,
        "file": Path(path).relative_to(output_root).as_posix(),
        "file_hash": _hash_file(path),
        "duration_ms": duration_ms,
        "sample_rate": audio.SAMPLE_RATE,
        "channels": audio.CHANNELS,
    }


def _plain_artifact(kind, path, output_root):
    return {
        "kind": kind,
        "shot_id": "",
        "file": Path(path).relative_to(output_root).as_posix(),
        "file_hash": _hash_file(path),
        "duration_ms": None,
        "sample_rate": None,
        "channels": None,
    }


def _execute(command, runner):
    try:
        return audio.run_ffmpeg(command, runner=runner)
    except audio.AudioEngineError as error:
        raise BundleBuildError(error.code, str(error)) from error


def _probe_audio(path, duration_ms, probe):
    try:
        result = probe(path)
        audio.validate_audio_probe(result, duration_ms)
    except audio.AudioEngineError as error:
        raise BundleBuildError(error.code, str(error)) from error
    except Exception as error:
        raise BundleBuildError(
            "artifact_hash_mismatch", "D-2 音频产物校验失败"
        ) from error
    return result


def _check_identity(identity_check):
    try:
        current = identity_check()
    except Exception as error:
        raise BundleBuildError(
            "source_changed_during_audio_build", "无法确认 D-2 输入身份"
        ) from error
    if current is not True:
        raise BundleBuildError(
            "source_changed_during_audio_build", "D-2 构建期间输入已变化"
        )


def _check_claim(claim_check):
    try:
        current = claim_check()
    except Exception as error:
        raise BundleBuildError(
            "build_claim_lost", "无法确认 D-2 构建租约"
        ) from error
    if current is not True:
        raise BundleBuildError("build_claim_lost", "D-2 构建租约已失效")


def _artifact_path(item):
    kind = str(item.get("kind") or "")
    shot_id = str(item.get("shot_id") or "")
    if kind == "shot_voice" and SAFE_ID.fullmatch(shot_id):
        return PurePosixPath("shots", f"{shot_id}.voice.wav")
    names = {
        "dialogue": "dialogue.wav",
        "bgm": "bgm.wav",
        "master_audio": "master.wav",
        "subtitles_ass": "subtitles.ass",
    }
    value = names.get(kind)
    return PurePosixPath(value) if value else None


def _recover_bundle(
    target,
    output_root,
    project_id,
    d1_input_hash,
    input_hash,
    ratio,
    media_plan,
    probe,
    expect_bgm,
):
    try:
        manifest_path = target / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            not isinstance(manifest, dict)
            or manifest.get("project_id") != project_id
            or manifest.get("d1_input_hash") != d1_input_hash
            or manifest.get("input_hash") != input_hash
            or manifest.get("ratio") != ratio
            or manifest.get("project_duration_ms")
            != media_plan.get("project_duration_ms")
        ):
            raise ValueError("bundle identity mismatch")
        values = manifest.get("artifacts")
        if not isinstance(values, list):
            raise ValueError("bundle artifacts missing")
        expected_shots = {
            str(item.get("id") or "") for item in media_plan.get("shots") or []
        }
        actual_shots = {
            str(item.get("shot_id") or "")
            for item in values
            if isinstance(item, dict) and item.get("kind") == "shot_voice"
        }
        kinds = {
            str(item.get("kind") or "")
            for item in values
            if isinstance(item, dict)
        }
        required = {
            "shot_voice", "dialogue", "master_audio", "subtitles_ass",
        }
        if (
            not required.issubset(kinds)
            or actual_shots != expected_shots
            or ("bgm" in kinds) is not bool(expect_bgm)
        ):
            raise ValueError("bundle artifacts incomplete")
        seen = set()
        artifacts = []
        relative_directory = target.relative_to(output_root).as_posix()
        for value in values:
            if not isinstance(value, dict):
                raise ValueError("bundle artifact invalid")
            item = dict(value)
            kind = str(item.get("kind") or "")
            shot_id = str(item.get("shot_id") or "")
            identity = (kind, shot_id)
            relative = _artifact_path(item)
            file_hash = str(item.get("file_hash") or "")
            if (
                kind not in RECOVERABLE_ARTIFACT_KINDS
                or identity in seen
                or relative is None
                or not SHA256.fullmatch(file_hash)
            ):
                raise ValueError("bundle artifact invalid")
            seen.add(identity)
            path = target.joinpath(*relative.parts)
            if not path.is_file() or _hash_file(path) != file_hash:
                raise ValueError("bundle artifact hash mismatch")
            if kind in {"shot_voice", "dialogue", "bgm", "master_audio"}:
                duration_ms = item.get("duration_ms")
                if type(duration_ms) is not int or duration_ms <= 0:
                    raise ValueError("bundle artifact duration invalid")
                _probe_audio(path, duration_ms, probe)
            item["file"] = str(
                PurePosixPath(relative_directory).joinpath(relative)
            )
            artifacts.append(item)
        artifacts.append(
            _plain_artifact("manifest", manifest_path, output_root)
        )
        return {
            "directory": relative_directory,
            "artifacts": artifacts,
            "manifest": manifest,
            "recovered": True,
            "quarantined_directory": None,
        }
    except Exception as error:
        raise BundleBuildError(
            "artifact_hash_mismatch", "D-2 已有产物校验失败"
        ) from error


def _line_inputs(shot, shot_inputs):
    inputs = {
        str(item.get("id") or ""): item
        for item in shot_inputs.get(shot["id"], [])
    }
    result = []
    for line in (shot.get("audio") or {}).get("lines") or []:
        source = inputs.get(str(line.get("id") or ""))
        if source is None:
            # Hidden subtitles still require their voice file when the D-1 line
            # carries an audio duration. Silent shots have no lines at all.
            raise BundleBuildError(
                "voice_timeline_invalid", "D-2 配音文件与时间线不一致"
            )
        if source.get("start_ms") != line.get("start_ms"):
            raise BundleBuildError(
                "voice_timeline_invalid", "D-2 配音开始时间不一致"
            )
        audio_duration_ms = line.get("audio_duration_ms")
        if (
            type(audio_duration_ms) is not int
            or audio_duration_ms <= 0
            or line["start_ms"] + audio_duration_ms > shot["duration_ms"]
        ):
            raise BundleBuildError(
                "voice_audio_overflow", "D-2 配音超出镜头范围"
            )
        result.append({
            "id": line.get("id"),
            "start_ms": line.get("start_ms"),
            "file": source.get("file"),
        })
    if set(inputs) != {str(line.get("id") or "") for line in (
        (shot.get("audio") or {}).get("lines") or []
    )}:
        raise BundleBuildError(
            "voice_timeline_invalid", "D-2 存在未绑定的配音文件"
        )
    return result


def _ordered_plan(media_plan, shot_inputs):
    if not isinstance(media_plan, dict):
        raise BundleBuildError("voice_timeline_invalid", "D-2 媒体计划无效")
    project_duration = media_plan.get("project_duration_ms")
    if type(project_duration) is not int or project_duration <= 0:
        raise BundleBuildError(
            "voice_timeline_invalid", "项目配音时长无效"
        )
    ordered = sorted(
        media_plan.get("shots") or [],
        key=lambda item: (item.get("start_ms", -1), str(item.get("id") or "")),
    )
    if not ordered:
        raise BundleBuildError("voice_timeline_invalid", "D-2 缺少镜头")
    cursor = 0
    ids = set()
    for shot in ordered:
        shot_id = str(shot.get("id") or "")
        duration_ms = shot.get("duration_ms")
        if (
            not SAFE_ID.fullmatch(shot_id)
            or shot_id in ids
            or type(duration_ms) is not int
            or duration_ms <= 0
            or shot.get("start_ms") != cursor
            or shot.get("end_ms") != cursor + duration_ms
        ):
            raise BundleBuildError(
                "voice_timeline_invalid", "D-2 镜头时间线不连续"
            )
        ids.add(shot_id)
        cursor += duration_ms
    if cursor != project_duration or set(shot_inputs) != ids:
        raise BundleBuildError(
            "voice_timeline_invalid", "D-2 项目时间线或配音输入不一致"
        )
    return ordered, project_duration


def build_bundle(
    *,
    output_root,
    project_id,
    d1_input_hash,
    input_hash,
    ratio,
    config,
    media_plan,
    shot_inputs,
    runner,
    probe,
    identity_check,
    claim_token,
    claim_check,
    toolchain,
    bgm_source=None,
    sound_cues=None,
    master_audio_contract=None,
    cached_audio_files=None,
):
    """Build an immutable bundle; caller owns DB claim/finalization."""
    if not SAFE_ID.fullmatch(str(project_id or "")):
        raise BundleBuildError("artifact_hash_mismatch", "项目产物标识无效")
    if not SHA256.fullmatch(str(input_hash or "")):
        raise BundleBuildError("artifact_hash_mismatch", "D-2 输入哈希无效")
    if not CLAIM_TOKEN.fullmatch(str(claim_token or "")):
        raise BundleBuildError("build_claim_lost", "D-2 构建租约无效")
    toolchain = dict(toolchain or {})
    if not str(toolchain.get("ffmpeg") or "") or not str(
        toolchain.get("ffprobe") or ""
    ):
        raise BundleBuildError(
            "ffmpeg_version_unsupported", "D-2 工具链尚未通过版本检查"
        )
    if not str(toolchain.get("font") or ""):
        raise BundleBuildError(
            "subtitle_font_unavailable", "D-2 字幕字体尚未通过检查"
        )
    output_root = Path(output_root).resolve()
    assembly_root = output_root / "short_drama_assembly"
    assembly_root.mkdir(parents=True, exist_ok=True)
    project_root = assembly_root / project_id
    target = project_root / input_hash
    _check_claim(claim_check)
    _check_identity(identity_check)
    quarantined = None
    if target.exists():
        try:
            recovered = _recover_bundle(
                target,
                output_root,
                project_id,
                d1_input_hash,
                input_hash,
                ratio,
                media_plan,
                probe,
                bgm_source is not None,
            )
            _check_claim(claim_check)
            _check_identity(identity_check)
            return recovered
        except BundleBuildError as error:
            if error.code != "artifact_hash_mismatch":
                raise
        _check_claim(claim_check)
        _check_identity(identity_check)
        quarantined = project_root / (
            f".stale-{input_hash}-{claim_token}"
        )
        if quarantined.exists():
            raise BundleBuildError(
                "artifact_hash_mismatch", "D-2 隔离产物目录已存在"
            )
        target.rename(quarantined)
    temp = Path(tempfile.mkdtemp(prefix=".tmp-d2-", dir=assembly_root))
    artifacts = []
    try:
        shots_dir = temp / "shots"
        shots_dir.mkdir()
        ordered_shots, project_duration = _ordered_plan(
            media_plan, shot_inputs
        )
        shot_files = []
        has_dialogue = False
        cached_audio_files = dict(cached_audio_files or {})
        expected_cache = {
            ("shot_voice", str(shot.get("id") or ""))
            for shot in ordered_shots
        } | {("dialogue", ""), ("master_audio", "")}
        if bgm_source is not None:
            expected_cache.add(("bgm", ""))
        use_audio_cache = bool(cached_audio_files)
        if use_audio_cache and expected_cache != set(cached_audio_files):
            raise ReusableAudioCacheError("主音轨缓存产物集合不完整")
        bgm_config = config.get("bgm") if isinstance(config, dict) else {}
        dialogue = temp / "dialogue.wav"
        bgm = temp / "bgm.wav" if bgm_source is not None else None
        soundscape = temp / "soundscape.wav" if sound_cues else None
        master = temp / "master.wav"
        if use_audio_cache:
            destinations = {
                ("dialogue", ""): dialogue,
                ("master_audio", ""): master,
            }
            if bgm is not None:
                destinations[("bgm", "")] = bgm
            for shot in ordered_shots:
                shot_id = str(shot.get("id") or "")
                destinations[("shot_voice", shot_id)] = (
                    shots_dir / f"{shot_id}.voice.wav"
                )
            for key, destination in destinations.items():
                record = cached_audio_files[key]
                if not isinstance(record, dict):
                    raise ReusableAudioCacheError()
                source = Path(record.get("path") or "")
                expected_hash = str(record.get("file_hash") or "")
                if (
                    not SHA256.fullmatch(expected_hash)
                    or not source.is_file()
                    or not hmac.compare_digest(
                        _hash_file(source), expected_hash
                    )
                ):
                    raise ReusableAudioCacheError()
                shutil.copy2(source, destination)
                if not hmac.compare_digest(
                    _hash_file(destination), expected_hash
                ):
                    raise ReusableAudioCacheError(
                        "主音轨缓存在复制期间发生变化"
                    )
            for shot in ordered_shots:
                shot_id = str(shot.get("id") or "")
                duration_ms = shot.get("duration_ms")
                output = destinations[("shot_voice", shot_id)]
                _probe_audio(output, duration_ms, probe)
                shot_files.append(output)
                artifacts.append(
                    _audio_artifact(
                        "shot_voice", output, temp, duration_ms, shot_id
                    )
                )
            _probe_audio(dialogue, project_duration, probe)
            artifacts.append(
                _audio_artifact("dialogue", dialogue, temp, project_duration)
            )
            if bgm is not None:
                _probe_audio(bgm, project_duration, probe)
                artifacts.append(
                    _audio_artifact("bgm", bgm, temp, project_duration)
                )
            _probe_audio(master, project_duration, probe)
            artifacts.append(
                _audio_artifact(
                    "master_audio", master, temp, project_duration
                )
            )
            measured = None
            loudness_mode = "cache_reuse"
        else:
            for shot in ordered_shots:
                shot_id = str(shot.get("id") or "")
                duration_ms = shot.get("duration_ms")
                output = shots_dir / f"{shot_id}.voice.wav"
                lines = _line_inputs(shot, shot_inputs)
                has_dialogue = has_dialogue or bool(lines)
                _execute(
                    audio.build_shot_voice_command(lines, duration_ms, output),
                    runner,
                )
                _probe_audio(output, duration_ms, probe)
                shot_files.append(output)
                artifacts.append(
                    _audio_artifact(
                        "shot_voice", output, temp, duration_ms, shot_id
                    )
                )
            _execute(
                audio.build_dialogue_concat_command(
                    shot_files, project_duration, dialogue
                ),
                runner,
            )
            _probe_audio(dialogue, project_duration, probe)
            artifacts.append(
                _audio_artifact(
                    "dialogue", dialogue, temp, project_duration
                )
            )
            if bgm_source is not None:
                _execute(
                    audio.build_bgm_command(
                        bgm_source, project_duration, bgm_config, bgm
                    ),
                    runner,
                )
                _probe_audio(bgm, project_duration, probe)
                artifacts.append(
                    _audio_artifact("bgm", bgm, temp, project_duration)
                )
            if soundscape is not None:
                try:
                    _execute(
                        audio.build_soundscape_command(
                            sound_cues, project_duration, soundscape
                        ),
                        runner,
                    )
                except audio.AudioEngineError as error:
                    raise BundleBuildError(error.code, str(error)) from error
                _probe_audio(soundscape, project_duration, probe)
            if not has_dialogue and bgm is None and soundscape is None:
                measured = None
                loudness_mode = "silence_bypass"
                _execute(
                    audio.build_silent_master_command(
                        dialogue, project_duration, master
                    ),
                    runner,
                )
            else:
                analysis = _execute(
                    audio.build_loudness_analysis_command(
                        dialogue, bgm, project_duration, soundscape
                    ),
                    runner,
                )
                try:
                    measured = audio.parse_loudnorm(analysis.stderr)
                except audio.AudioEngineError as error:
                    raise BundleBuildError(error.code, str(error)) from error
                loudness_mode = "two_pass"
                _execute(
                    audio.build_master_command(
                        dialogue, bgm, project_duration, measured, master,
                        soundscape,
                    ),
                    runner,
                )
            _probe_audio(master, project_duration, probe)
            artifacts.append(
                _audio_artifact(
                    "master_audio", master, temp, project_duration
                )
            )
        subtitle_config = (
            config.get("subtitle") if isinstance(config, dict) else {}
        )
        try:
            ass_text = subtitles.generate_ass(
                ratio,
                str(subtitle_config.get("position") or "bottom"),
                media_plan if subtitle_config.get("enabled", True) else {
                    **media_plan,
                    "shots": [
                        {
                            **shot,
                            "audio": {
                                **(shot.get("audio") or {}),
                                "lines": [],
                            },
                        }
                        for shot in media_plan.get("shots") or []
                    ],
                },
            )
        except subtitles.SubtitleError as error:
            raise BundleBuildError(error.code, str(error)) from error
        ass_path = temp / "subtitles.ass"
        ass_path.write_text(ass_text, encoding="utf-8", newline="\n")
        artifacts.append(
            _plain_artifact("subtitles_ass", ass_path, temp)
        )
        subtitle_events = sum(
            1 for line in ass_text.splitlines() if line.startswith("Dialogue:")
        )
        manifest = {
            "engine_version": audio.ENGINE_VERSION,
            "project_id": project_id,
            "d1_input_hash": d1_input_hash,
            "input_hash": input_hash,
            "ratio": ratio,
            "project_duration_ms": project_duration,
            "audio": {
                "sample_rate": audio.SAMPLE_RATE,
                "channels": audio.CHANNELS,
                "codec": "pcm_s16le",
                "target_i": audio.TARGET_I,
                "target_tp": audio.TARGET_TP,
                "target_lra": audio.TARGET_LRA,
                "loudness_mode": loudness_mode,
                "loudness_measurements": measured,
                "sound_cue_count": len(sound_cues or []),
                "source_video_audio": "discarded",
            },
            "master_audio": dict(master_audio_contract or {}),
            "subtitle_events": subtitle_events,
            "toolchain": toolchain,
            "artifacts": [
                {
                    key: value
                    for key, value in item.items()
                    if key != "file"
                }
                for item in artifacts
            ],
        }
        manifest_path = temp / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                manifest, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
            newline="\n",
        )
        artifacts.append(_plain_artifact("manifest", manifest_path, temp))
        _check_claim(claim_check)
        _check_identity(identity_check)
        project_root.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise BundleBuildError(
                "artifact_hash_mismatch", "D-2 目标产物目录发生竞争"
            )
        temp.rename(target)
        relative_directory = target.relative_to(output_root).as_posix()
        for item in artifacts:
            suffix = PurePosixPath(item["file"])
            item["file"] = str(
                PurePosixPath(relative_directory).joinpath(suffix)
            )
        return {
            "directory": relative_directory,
            "artifacts": artifacts,
            "manifest": manifest,
            "recovered": False,
            "quarantined_directory": (
                quarantined.relative_to(output_root).as_posix()
                if quarantined is not None else None
            ),
        }
    except BundleBuildError:
        raise
    except Exception as error:
        raise BundleBuildError(
            "artifact_hash_mismatch", "D-2 产物构建失败"
        ) from error
    finally:
        if temp.exists():
            shutil.rmtree(temp)
