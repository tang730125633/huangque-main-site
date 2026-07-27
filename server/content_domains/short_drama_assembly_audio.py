"""FFmpeg command and validation engine for short-drama D-2 audio."""

import json
import math
import os
import re
import subprocess
from pathlib import Path


ENGINE_VERSION = "short_drama_audio_subtitle_v1"
SAMPLE_RATE = 48000
CHANNELS = 2
AUDIO_TOLERANCE_MS = 20
FFMPEG_TIMEOUT_SECONDS = 180
TARGET_I = -16.0
TARGET_TP = -1.5
TARGET_LRA = 11.0


class AudioEngineError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _ffmpeg():
    return os.environ.get("FFMPEG_BIN", "ffmpeg")


def _seconds(milliseconds):
    return f"{int(milliseconds) / 1000:.3f}"


def _base_command(loglevel="error"):
    return [_ffmpeg(), "-y", "-hide_banner", "-loglevel", loglevel]


def build_shot_voice_command(lines, duration_ms, output_path):
    if type(duration_ms) is not int or duration_ms <= 0:
        raise AudioEngineError("voice_timeline_invalid", "镜头时长无效")
    duration = _seconds(duration_ms)
    ordered = sorted(
        [dict(item) for item in lines],
        key=lambda item: (
            item.get("start_ms", -1), str(item.get("id") or "")
        ),
    )
    command = _base_command()
    command.extend([
        "-f", "lavfi", "-i",
        f"anullsrc=r={SAMPLE_RATE}:cl=stereo:d={duration}",
    ])
    if not ordered:
        command.extend([
            "-t", duration, "-c:a", "pcm_s16le", str(output_path)
        ])
        return command
    filters = []
    labels = ["[0:a]"]
    for index, line in enumerate(ordered, 1):
        start_ms = line.get("start_ms")
        source = line.get("file")
        if (
            type(start_ms) is not int
            or start_ms < 0
            or not isinstance(source, (str, Path))
            or not str(source)
        ):
            raise AudioEngineError("voice_timeline_invalid", "配音输入无效")
        command.extend(["-i", str(source)])
        label = f"[voice{index}]"
        filters.append(
            f"[{index}:a]aresample={SAMPLE_RATE},"
            "aformat=sample_fmts=s16:channel_layouts=stereo,"
            f"adelay={start_ms}|{start_ms}{label}"
        )
        labels.append(label)
    filters.append(
        "".join(labels)
        + f"amix=inputs={len(labels)}:duration=first:dropout_transition=0,"
        f"atrim=duration={duration},asetpts=N/SR/TB[out]"
    )
    command.extend([
        "-filter_complex", ";".join(filters),
        "-map", "[out]", "-c:a", "pcm_s16le", str(output_path),
    ])
    return command


def build_dialogue_concat_command(shot_files, duration_ms, output_path):
    if not shot_files or type(duration_ms) is not int or duration_ms <= 0:
        raise AudioEngineError("voice_timeline_invalid", "项目配音输入无效")
    command = _base_command()
    for path in shot_files:
        command.extend(["-i", str(path)])
    labels = "".join(f"[{index}:a]" for index in range(len(shot_files)))
    graph = (
        f"{labels}concat=n={len(shot_files)}:v=0:a=1,"
        f"atrim=duration={_seconds(duration_ms)},asetpts=N/SR/TB[out]"
    )
    command.extend([
        "-filter_complex", graph, "-map", "[out]",
        "-c:a", "pcm_s16le", str(output_path),
    ])
    return command


def _bgm_config(config, duration_ms):
    try:
        volume = float(config.get("volume"))
        fade_in_ms = int(config.get("fade_in_ms"))
        fade_out_ms = int(config.get("fade_out_ms"))
    except (AttributeError, TypeError, ValueError) as error:
        raise AudioEngineError("bgm_probe_failed", "背景音乐配置无效") from error
    if (
        not math.isfinite(volume)
        or volume < 0
        or volume > 1
        or fade_in_ms < 0
        or fade_out_ms < 0
        or fade_in_ms + fade_out_ms > duration_ms
    ):
        raise AudioEngineError("bgm_probe_failed", "背景音乐配置无效")
    return volume, fade_in_ms, fade_out_ms


def build_bgm_command(source_path, duration_ms, config, output_path):
    volume, fade_in_ms, fade_out_ms = _bgm_config(config, duration_ms)
    duration = _seconds(duration_ms)
    fade_out_start = _seconds(duration_ms - fade_out_ms)
    graph = (
        f"aresample={SAMPLE_RATE},"
        "aformat=sample_fmts=s16:channel_layouts=stereo,"
        f"volume={volume:.6f},"
        f"afade=t=in:st=0:d={_seconds(fade_in_ms)},"
        f"afade=t=out:st={fade_out_start}:d={_seconds(fade_out_ms)},"
        f"atrim=duration={duration},asetpts=N/SR/TB"
    )
    return _base_command() + [
        "-stream_loop", "-1", "-i", str(source_path),
        "-af", graph, "-t", duration,
        "-c:a", "pcm_s16le", str(output_path),
    ]


def _premaster_filter(has_bgm):
    if not has_bgm:
        return (
            f"[0:a]aresample={SAMPLE_RATE},"
            "aformat=sample_fmts=s16:channel_layouts=stereo[premaster]"
        )
    return (
        "[0:a]asplit=2[voice][side];"
        "[1:a][side]sidechaincompress="
        "threshold=0.02:ratio=8:attack=20:release=300[ducked];"
        "[voice][ducked]amix=inputs=2:duration=first:"
        "dropout_transition=0[premaster]"
    )


def _loudnorm_filter(measured=None):
    base = f"loudnorm=I={TARGET_I}:TP={TARGET_TP}:LRA={TARGET_LRA}"
    if measured is None:
        return base + ":print_format=json"
    return (
        base
        + f":measured_I={measured['input_i']}"
        + f":measured_TP={measured['input_tp']}"
        + f":measured_LRA={measured['input_lra']}"
        + f":measured_thresh={measured['input_thresh']}"
        + f":offset={measured['target_offset']}"
        + ":linear=true:print_format=summary"
    )


def _mix_graph(has_bgm, duration_ms, measured=None):
    return (
        _premaster_filter(has_bgm)
        + f";[premaster]{_loudnorm_filter(measured)},"
        f"aresample={SAMPLE_RATE},"
        "aformat=sample_fmts=s16:channel_layouts=stereo,"
        f"atrim=duration={_seconds(duration_ms)},asetpts=N/SR/TB[out]"
    )


def build_loudness_analysis_command(dialogue_path, bgm_path, duration_ms):
    # loudnorm writes its JSON measurements at info level. Keeping the
    # default error level here makes a successful analysis look empty.
    command = _base_command("info") + ["-i", str(dialogue_path)]
    if bgm_path is not None:
        command.extend(["-i", str(bgm_path)])
    command.extend([
        "-filter_complex",
        _mix_graph(bgm_path is not None, duration_ms),
        "-map", "[out]", "-f", "null", "-",
    ])
    return command


def _validated_measurements(measured):
    required = {
        "input_i", "input_tp", "input_lra", "input_thresh", "target_offset"
    }
    if not isinstance(measured, dict) or not required.issubset(measured):
        raise AudioEngineError(
            "loudness_analysis_failed", "响度分析结果不完整"
        )
    result = {}
    for key in required:
        value = str(measured[key])
        try:
            parsed = float(value)
        except (TypeError, ValueError) as error:
            raise AudioEngineError(
                "loudness_analysis_failed", "响度分析结果无效"
            ) from error
        if not math.isfinite(parsed):
            raise AudioEngineError(
                "loudness_analysis_failed", "响度分析结果无效"
            )
        result[key] = value
    return result


def parse_loudnorm(stderr):
    candidates = re.findall(r"\{[^{}]*\}", str(stderr or ""), re.DOTALL)
    for candidate in reversed(candidates):
        try:
            parsed = json.loads(candidate)
            return _validated_measurements(parsed)
        except (json.JSONDecodeError, AudioEngineError):
            continue
    raise AudioEngineError("loudness_analysis_failed", "响度分析失败")


def build_master_command(
    dialogue_path, bgm_path, duration_ms, measured, output_path
):
    measured = _validated_measurements(measured)
    command = _base_command() + ["-i", str(dialogue_path)]
    if bgm_path is not None:
        command.extend(["-i", str(bgm_path)])
    command.extend([
        "-filter_complex",
        _mix_graph(bgm_path is not None, duration_ms, measured),
        "-map", "[out]", "-c:a", "pcm_s16le",
        "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS),
        str(output_path),
    ])
    return command


def build_silent_master_command(dialogue_path, duration_ms, output_path):
    graph = (
        f"aresample={SAMPLE_RATE},"
        "aformat=sample_fmts=s16:channel_layouts=stereo,"
        f"atrim=duration={_seconds(duration_ms)},asetpts=N/SR/TB"
    )
    return _base_command() + [
        "-i", str(dialogue_path),
        "-af", graph,
        "-c:a", "pcm_s16le",
        "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS),
        str(output_path),
    ]


def run_ffmpeg(command, runner=subprocess.run, timeout=FFMPEG_TIMEOUT_SECONDS):
    try:
        result = runner(
            list(command),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as error:
        raise AudioEngineError(
            "ffmpeg_unavailable", "服务器未安装或无法调用 FFmpeg"
        ) from error
    except (OSError, subprocess.TimeoutExpired) as error:
        raise AudioEngineError("audio_mix_failed", "音频处理执行失败") from error
    if result.returncode != 0:
        raise AudioEngineError("audio_mix_failed", "音频处理失败")
    return result


def inspect_ffmpeg(runner=subprocess.run):
    result = run_ffmpeg([_ffmpeg(), "-version"], runner=runner, timeout=10)
    first_line = str(result.stdout or "").splitlines()
    first_line = first_line[0].strip() if first_line else ""
    match = re.match(r"^ffmpeg version\s+([0-9]+)", first_line, re.I)
    if not match or int(match.group(1)) < 4:
        raise AudioEngineError(
            "ffmpeg_version_unsupported", "FFmpeg 版本不满足 D-2 要求"
        )
    return first_line[:200]


def validate_audio_probe(probe, expected_duration_ms):
    if not isinstance(probe, dict) or probe.get("audio") is None:
        raise AudioEngineError("audio_stream_missing", "输出缺少音频流")
    if probe.get("video") is not None:
        raise AudioEngineError("audio_mix_failed", "音频输出包含视频流")
    duration = probe.get("duration_ms")
    if (
        type(duration) is not int
        or abs(duration - expected_duration_ms) > AUDIO_TOLERANCE_MS
    ):
        raise AudioEngineError("audio_duration_mismatch", "音频时长不一致")
    stream = probe["audio"]
    if (
        stream.get("sample_rate") != SAMPLE_RATE
        or stream.get("channels") != CHANNELS
    ):
        raise AudioEngineError("audio_mix_failed", "音频输出规格不一致")
