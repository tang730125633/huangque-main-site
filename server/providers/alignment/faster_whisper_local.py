"""Local faster-whisper adapter with real word timestamps."""

from __future__ import annotations

import os
import threading
import uuid

from .base import (
    AlignmentCapabilities,
    AlignmentProviderError,
    ForcedAlignmentProvider,
    ProviderJob,
    ProviderResult,
)


_MODEL = None
_MODEL_NAME = None
_MODEL_LOCK = threading.Lock()
_CONCURRENCY = max(
    1, int(os.environ.get("SHORT_DRAMA_ALIGNMENT_CONCURRENCY", "1") or 1)
)
_SEMAPHORE = threading.BoundedSemaphore(_CONCURRENCY)


def _resolve_audio_file(value):
    try:
        from content_domains.core import _resolve_out_file
    except ImportError:
        from server.content_domains.core import _resolve_out_file
    path = _resolve_out_file(value)
    if path is None:
        raise AlignmentProviderError(
            "alignment_audio_unavailable",
            "配音音频文件不存在或不在受控目录",
        )
    return path


def _model():
    global _MODEL, _MODEL_NAME
    name = str(
        os.environ.get("SHORT_DRAMA_ALIGNMENT_MODEL", "small")
    ).strip() or "small"
    try:
        from faster_whisper import WhisperModel
    except (ImportError, OSError) as error:
        raise AlignmentProviderError(
            "alignment_provider_unavailable",
            "faster-whisper 不可用",
            retryable=True,
        ) from error
    if _MODEL is None or _MODEL_NAME != name:
        with _MODEL_LOCK:
            if _MODEL is None or _MODEL_NAME != name:
                try:
                    _MODEL = WhisperModel(
                        name, device="cpu", compute_type="int8"
                    )
                except Exception as error:
                    raise AlignmentProviderError(
                        "alignment_provider_unavailable",
                        "字幕对齐 ASR 模型加载失败",
                        retryable=True,
                    ) from error
                _MODEL_NAME = name
    return _MODEL, name


class FasterWhisperLocalProvider(ForcedAlignmentProvider):
    name = "faster-whisper-local"
    model_version = "word-timestamps-v1"

    def __init__(self):
        self._results = {}

    def capabilities(self):
        return AlignmentCapabilities(
            provider=self.name,
            model_version=self.model_version,
            supports_word_timestamps=True,
            supports_cancel=False,
            supports_resume=False,
            supports_result_refetch=False,
            max_audio_seconds=3600,
            accepted_formats=("wav", "mp3", "m4a"),
            language_models=("zh-CN",),
            real_forced_alignment=True,
        )

    def _words(self, audio_file, offset_ms, end_ms):
        path = _resolve_audio_file(audio_file)
        model, model_name = _model()
        try:
            with _SEMAPHORE:
                segments, _ = model.transcribe(
                    str(path),
                    language="zh",
                    vad_filter=True,
                    word_timestamps=True,
                    beam_size=5,
                )
                words = []
                for segment in segments:
                    for item in getattr(segment, "words", None) or []:
                        text = str(getattr(item, "word", "") or "").strip()
                        start = getattr(item, "start", None)
                        end = getattr(item, "end", None)
                        if (
                            not text
                            or not isinstance(start, (int, float))
                            or not isinstance(end, (int, float))
                            or end <= start
                        ):
                            continue
                        start_ms = max(
                            offset_ms,
                            min(end_ms - 1, offset_ms + round(start * 1000)),
                        )
                        word_end_ms = max(
                            start_ms + 1,
                            min(end_ms, offset_ms + round(end * 1000)),
                        )
                        probability = getattr(item, "probability", None)
                        pieces = [
                            character for character in text
                            if not character.isspace()
                        ]
                        duration = word_end_ms - start_ms
                        for index, token in enumerate(pieces):
                            token_start = start_ms + round(
                                duration * index / len(pieces)
                            )
                            token_end = start_ms + round(
                                duration * (index + 1) / len(pieces)
                            )
                            words.append({
                                "token": token,
                                "start_ms": token_start,
                                "end_ms": max(token_start + 1, token_end),
                                "confidence": (
                                    max(0.0, min(1.0, float(probability)))
                                    if isinstance(
                                        probability, (int, float)
                                    )
                                    else 0.0
                                ),
                            })
                if end_ms - offset_ms < len(words):
                    raise AlignmentProviderError(
                        "alignment_resolution_insufficient",
                        "音频时长不足以生成严格递增的 token 时间轴",
                    )
                cursor = offset_ms
                for index, word in enumerate(words):
                    remaining = len(words) - index - 1
                    original_start = int(word["start_ms"])
                    original_end = int(word["end_ms"])
                    latest_start = end_ms - remaining - 1
                    word["start_ms"] = min(
                        latest_start, max(cursor, original_start)
                    )
                    latest_end = end_ms - remaining
                    word["end_ms"] = min(
                        latest_end,
                        max(word["start_ms"] + 1, original_end),
                    )
                    if (
                        word["start_ms"] != original_start
                        or word["end_ms"] != original_end
                    ):
                        word["confidence"] = 0.0
                    cursor = word["end_ms"]
                return words, model_name
        except AlignmentProviderError:
            raise
        except Exception as error:
            raise AlignmentProviderError(
                "alignment_provider_error",
                "字幕对齐 ASR 执行失败",
                retryable=True,
            ) from error

    def create_job(self, request):
        job_id = str(uuid.uuid4())
        segments = []
        model_name = ""
        for shot in request.get("shots") or []:
            for line in shot.get("lines") or []:
                start_ms = int(line.get("audio_start_ms") or 0)
                end_ms = int(line.get("audio_end_ms") or 0)
                if end_ms <= start_ms:
                    raise AlignmentProviderError(
                        "alignment_timeline_invalid",
                        "字幕对齐音频时间范围无效",
                    )
                words, model_name = self._words(
                    line.get("audio_file"), start_ms, end_ms
                )
                segments.append({
                    "line_id": str(line.get("line_id") or ""),
                    "transcript": "".join(
                        str(item.get("token") or "") for item in words
                    ),
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "words": words,
                })
        self._results[job_id] = ProviderResult(
            provider_job_id=job_id,
            status="succeeded",
            segments=tuple(segments),
            diagnostics={
                "provider": self.name,
                "model": model_name,
                "real_word_timestamps": True,
            },
        )
        return ProviderJob(job_id, "succeeded")

    def get_job(self, provider_job_id):
        status = "succeeded" if provider_job_id in self._results else "failed"
        return ProviderJob(provider_job_id, status)

    def cancel_job(self, provider_job_id):
        return ProviderJob(provider_job_id, "failed")

    def fetch_result(self, provider_job_id):
        try:
            return self._results[provider_job_id]
        except KeyError as error:
            raise AlignmentProviderError(
                "alignment_result_unavailable",
                "字幕对齐结果不可用",
            ) from error
