"""Stable identity and timeline contract for short-drama master audio."""

from .short_drama_assembly_audio import (
    CHANNELS,
    SAMPLE_RATE,
    TARGET_I,
    TARGET_LRA,
    TARGET_TP,
)
from .short_drama_assembly_plan import canonical_hash


ENGINE_VERSION = "short_drama_master_audio_v2"
CONTRACT_VERSION = "short_drama_master_timeline_v1"
CODEC = "pcm_s16le"


class MasterAudioContractError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _integer(value, code, message):
    if type(value) is not int:
        raise MasterAudioContractError(code, message)
    return value


def _source_by_line(voice_sources):
    result = {}
    for source in voice_sources or []:
        if not isinstance(source, dict):
            continue
        key = (str(source.get("shot_id") or ""), str(source.get("line_id") or ""))
        result[key] = {
            "version": source.get("version"),
            "sha256": str(source.get("sha256") or ""),
            "size": source.get("size"),
        }
    return result


def build_contract(
    media_plan, voice_sources, bgm_source, bgm_config, sound_sources=None
):
    """Return a deterministic identity that excludes subtitle presentation."""
    if not isinstance(media_plan, dict):
        raise MasterAudioContractError(
            "missing_d1_media_plan", "主音轨缺少有效媒体计划"
        )
    project_duration_ms = _integer(
        media_plan.get("project_duration_ms"),
        "master_audio_timeline_invalid",
        "主音轨项目时长无效",
    )
    if project_duration_ms <= 0:
        raise MasterAudioContractError(
            "master_audio_timeline_invalid", "主音轨项目时长无效"
        )
    sources = _source_by_line(voice_sources)
    cursor = 0
    shots = []
    for raw_shot in media_plan.get("shots") or []:
        if not isinstance(raw_shot, dict):
            raise MasterAudioContractError(
                "master_audio_timeline_invalid", "主音轨镜头时间线无效"
            )
        shot_id = str(raw_shot.get("id") or "")
        start_ms = _integer(
            raw_shot.get("start_ms"),
            "master_audio_timeline_invalid",
            "主音轨镜头开始时间无效",
        )
        end_ms = _integer(
            raw_shot.get("end_ms"),
            "master_audio_timeline_invalid",
            "主音轨镜头结束时间无效",
        )
        duration_ms = _integer(
            raw_shot.get("duration_ms"),
            "master_audio_timeline_invalid",
            "主音轨镜头时长无效",
        )
        if (
            not shot_id
            or start_ms != cursor
            or end_ms != start_ms + duration_ms
            or duration_ms <= 0
            or end_ms > project_duration_ms
        ):
            raise MasterAudioContractError(
                "master_audio_timeline_invalid", "主音轨镜头时间线不连续"
            )
        line_contracts = []
        audio = raw_shot.get("audio") if isinstance(raw_shot.get("audio"), dict) else {}
        for raw_line in audio.get("lines") or []:
            if not isinstance(raw_line, dict):
                raise MasterAudioContractError(
                    "master_audio_timeline_invalid", "主音轨台词时间线无效"
                )
            line_id = str(raw_line.get("id") or "")
            line_start = _integer(
                raw_line.get("start_ms"),
                "master_audio_timeline_invalid",
                "主音轨台词开始时间无效",
            )
            audio_duration_ms = _integer(
                raw_line.get("audio_duration_ms"),
                "master_audio_timeline_invalid",
                "主音轨台词音频时长无效",
            )
            audio_end_ms = line_start + audio_duration_ms
            source = sources.get((shot_id, line_id))
            if (
                not line_id
                or source is None
                or line_start < 0
                or audio_duration_ms <= 0
                or audio_end_ms > duration_ms
            ):
                raise MasterAudioContractError(
                    "master_audio_timeline_invalid",
                    "主音轨台词音频超出所属镜头",
                )
            line_contracts.append({
                "line_id": line_id,
                "start_ms": line_start,
                "audio_duration_ms": audio_duration_ms,
                "audio_end_ms": audio_end_ms,
                "version": source["version"],
                "source_sha256": source["sha256"],
                "source_size": source["size"],
            })
        line_contracts.sort(key=lambda item: (item["start_ms"], item["line_id"]))
        for previous, current in zip(line_contracts, line_contracts[1:]):
            if current["start_ms"] < previous["audio_end_ms"]:
                raise MasterAudioContractError(
                    "master_audio_timeline_invalid",
                    "主音轨台词音频时间发生重叠",
                )
        shot_audio_hash = canonical_hash({
            "engine_version": ENGINE_VERSION,
            "shot_id": shot_id,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "duration_ms": duration_ms,
            "lines": line_contracts,
            "output": {
                "sample_rate": SAMPLE_RATE,
                "channels": CHANNELS,
                "codec": CODEC,
            },
        })
        shots.append({
            "shot_id": shot_id,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "duration_ms": duration_ms,
            "shot_audio_hash": shot_audio_hash,
            "lines": line_contracts,
        })
        cursor = end_ms
    if not shots or cursor != project_duration_ms:
        raise MasterAudioContractError(
            "master_audio_timeline_invalid", "主音轨总时长与镜头时间线不一致"
        )
    normalized_bgm = None
    if bgm_source is not None:
        normalized_bgm = {
            "source": dict(bgm_source),
            "volume": float((bgm_config or {}).get("volume", 0.18)),
            "fade_in_ms": int((bgm_config or {}).get("fade_in_ms", 500)),
            "fade_out_ms": int((bgm_config or {}).get("fade_out_ms", 800)),
        }
    identity = {
        "engine_version": ENGINE_VERSION,
        "contract_version": CONTRACT_VERSION,
        "duration_ms": project_duration_ms,
        "sample_rate": SAMPLE_RATE,
        "channels": CHANNELS,
        "codec": CODEC,
        "loudness": {
            "target_i": TARGET_I,
            "target_tp": TARGET_TP,
            "target_lra": TARGET_LRA,
        },
        "shots": shots,
        "bgm": normalized_bgm,
        "sound_cues": list(sound_sources or []),
        "source_video_audio": "discarded",
    }
    identity["master_audio_hash"] = canonical_hash(identity)
    return identity


def build_snapshot(contract, audio_subtitle):
    contract = dict(contract or {})
    audio_subtitle = dict(audio_subtitle or {})
    artifacts = audio_subtitle.get("artifacts") or []
    master = next(
        (
            dict(item) for item in artifacts
            if isinstance(item, dict) and item.get("kind") == "master_audio"
        ),
        None,
    )
    status = str(audio_subtitle.get("status") or "not_built")
    if status == "ready" and not master:
        status = "stale"
    return {
        "engine_version": ENGINE_VERSION,
        "contract_version": CONTRACT_VERSION,
        "master_audio_hash": contract.get("master_audio_hash"),
        "status": status,
        "cache_hit": status == "ready",
        "duration_ms": contract.get("duration_ms"),
        "sample_rate": SAMPLE_RATE,
        "channels": CHANNELS,
        "codec": CODEC,
        "artifact": master,
        "timeline": {
            "contract_version": CONTRACT_VERSION,
            "duration_ms": contract.get("duration_ms"),
            "sample_rate": SAMPLE_RATE,
            "channels": CHANNELS,
            "codec": CODEC,
            "master_audio_hash": contract.get("master_audio_hash"),
            "shots": contract.get("shots") or [],
        },
        "blockers": list(audio_subtitle.get("blockers") or []),
    }
