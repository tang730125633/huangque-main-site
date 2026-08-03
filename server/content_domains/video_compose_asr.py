# -*- coding: utf-8 -*-
"""Timestamped ASR adapter for one-click-video analysis."""

import json
import mimetypes
import os
import pathlib
import subprocess
import tempfile
import threading
import urllib.error
import urllib.request
import uuid


OPENAI_BASE = os.environ.get("OPENAI_BASE", "https://api.openai.com").rstrip("/")
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")
ASR_MODEL = os.environ.get("VIDEO_COMPOSE_ASR_MODEL", "whisper-1").strip() or "whisper-1"
MAX_SOURCE_SECONDS = max(10, min(600, int(os.environ.get("VIDEO_COMPOSE_MAX_SECONDS", "180") or 180)))
MAX_AUDIO_BYTES = 24 * 1024 * 1024
_ASR_LOCK = threading.BoundedSemaphore(1)


class AsrError(ValueError):
    pass


def _run(command, timeout):
    try:
        return subprocess.run(command, check=True, timeout=timeout,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError as error:
        raise AsrError("服务器未安装 FFmpeg/FFprobe") from error
    except subprocess.TimeoutExpired as error:
        raise AsrError("音频预处理超时") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or b"").decode("utf-8", "replace")[-220:]
        raise AsrError("音频预处理失败" + ("：" + detail if detail else "")) from error


def _duration_seconds(source_path):
    result = _run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(source_path),
    ], 30)
    try:
        value = float(result.stdout.decode().strip())
    except Exception as error:
        raise AsrError("无法读取视频时长") from error
    if value <= 0 or value > MAX_SOURCE_SECONDS + 0.05:
        raise AsrError("首版只支持 %d 秒以内的口播视频" % MAX_SOURCE_SECONDS)
    return value


def extract_audio(source_path, output_path):
    source_path = pathlib.Path(source_path)
    output_path = pathlib.Path(output_path)
    if not source_path.is_file():
        raise AsrError("源视频文件不存在")
    duration = _duration_seconds(source_path)
    _run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(source_path),
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "48k",
        str(output_path),
    ], max(90, int(duration * 4)))
    if not output_path.is_file() or output_path.stat().st_size <= 0:
        raise AsrError("音频提取结果为空")
    if output_path.stat().st_size > MAX_AUDIO_BYTES:
        raise AsrError("音频超过 ASR 上传限制")
    return duration


def _multipart(fields, file_field, file_path):
    boundary = "----hq-compose-" + uuid.uuid4().hex
    chunks = []
    for name, value in fields:
        chunks.extend([
            ("--%s\r\n" % boundary).encode(),
            ('Content-Disposition: form-data; name="%s"\r\n\r\n' % name).encode(),
            str(value).encode("utf-8"), b"\r\n",
        ])
    mime = mimetypes.guess_type(str(file_path))[0] or "audio/mpeg"
    chunks.extend([
        ("--%s\r\n" % boundary).encode(),
        ('Content-Disposition: form-data; name="%s"; filename="audio.mp3"\r\n' % file_field).encode(),
        ("Content-Type: %s\r\n\r\n" % mime).encode(),
        pathlib.Path(file_path).read_bytes(), b"\r\n",
        ("--%s--\r\n" % boundary).encode(),
    ])
    return b"".join(chunks), "multipart/form-data; boundary=" + boundary


def _milliseconds(value):
    try:
        return max(0, int(round(float(value) * 1000)))
    except (TypeError, ValueError):
        return 0


def parse_verbose_response(payload):
    if not isinstance(payload, dict):
        raise AsrError("ASR 返回格式无效")
    words = []
    for item in payload.get("words") or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("word") or item.get("text") or "").strip()
        start_ms = _milliseconds(item.get("start"))
        end_ms = _milliseconds(item.get("end"))
        if text and end_ms > start_ms:
            words.append({"text": text, "start_ms": start_ms, "end_ms": end_ms,
                          "confidence": item.get("confidence")})
    segments = []
    for item in payload.get("segments") or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        start_ms = _milliseconds(item.get("start"))
        end_ms = _milliseconds(item.get("end"))
        if text and end_ms > start_ms:
            segments.append({"text": text, "start_ms": start_ms, "end_ms": end_ms})
    if not words and segments:
        for segment in segments:
            chars = [char for char in segment["text"] if not char.isspace()]
            if not chars:
                continue
            span = segment["end_ms"] - segment["start_ms"]
            for index, char in enumerate(chars):
                start_ms = segment["start_ms"] + int(span * index / len(chars))
                end_ms = segment["start_ms"] + int(span * (index + 1) / len(chars))
                words.append({"text": char, "start_ms": start_ms, "end_ms": end_ms,
                              "confidence": None})
    if not words:
        raise AsrError("ASR 没有识别到有效语音")
    return {"text": str(payload.get("text") or "").strip(), "words": words,
            "segments": segments, "language": payload.get("language")}


def transcribe(source_path, opener=None, api_key=None, base_url=None):
    key = str(api_key if api_key is not None else OPENAI_KEY).strip()
    if not key:
        raise AsrError("一键成片 ASR 未配置")
    opener = opener or urllib.request.build_opener()
    base_url = str(base_url or OPENAI_BASE).rstrip("/")
    with tempfile.TemporaryDirectory(prefix="hq-compose-asr-") as directory:
        audio = pathlib.Path(directory) / "audio.mp3"
        duration = extract_audio(source_path, audio)
        body, content_type = _multipart([
            ("model", ASR_MODEL), ("language", "zh"),
            ("response_format", "verbose_json"),
            ("timestamp_granularities[]", "word"),
            ("timestamp_granularities[]", "segment"),
        ], "file", audio)
        endpoint = base_url + ("/audio/transcriptions" if base_url.endswith("/v1") else "/v1/audio/transcriptions")
        request = urllib.request.Request(
            endpoint, data=body, method="POST",
            headers={"Authorization": "Bearer " + key, "Content-Type": content_type},
        )
        try:
            with _ASR_LOCK:
                with opener.open(request, timeout=max(120, int(duration * 5))) as response:
                    payload = json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as error:
            try:
                detail = json.loads(error.read().decode("utf-8", "replace"))
                message = str((detail.get("error") or {}).get("message") or "")
            except Exception:
                message = ""
            raise AsrError("语音识别服务失败" + ("：" + message[:160] if message else "")) from error
        except Exception as error:
            raise AsrError("语音识别服务暂时不可用") from error
    result = parse_verbose_response(payload)
    result["duration_ms"] = int(round(duration * 1000))
    result["model"] = ASR_MODEL
    return result
