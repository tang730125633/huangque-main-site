"""Subtitle alignment using real local ASR word timestamps with explicit fallback."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import unicodedata
import uuid
from difflib import SequenceMatcher

from . import short_drama_assembly_subtitles as assembly_subtitles
from . import short_drama_voice


CONTRACT_VERSION = "short_drama_alignment_v1"
PROVIDER_NAME = "faster-whisper-local"
MODEL_VERSION = "word-timestamps-v1"
WRITABLE_STAGE = "voice_review"
INTERRUPTED_JOB_SECONDS = 60
REVIEW_ACTIONS = {"save_adjustments", "confirm_unchanged"}
_WHISPER_MODEL = None
_WHISPER_MODEL_NAME = None
_WHISPER_MODEL_LOCK = threading.Lock()
_ALIGNMENT_CONCURRENCY = max(
    1, int(os.environ.get("SHORT_DRAMA_ALIGNMENT_CONCURRENCY", "1") or 1)
)
_ALIGNMENT_SEMAPHORE = threading.BoundedSemaphore(_ALIGNMENT_CONCURRENCY)


class AlignmentError(ValueError):
    def __init__(self, code, message, *, status=400, blockers=None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.blockers = list(blockers or [])


class AlignmentIdempotencyConflict(AlignmentError):
    def __init__(self):
        super().__init__(
            "idempotency_conflict", "幂等键已绑定到不同的字幕对齐请求", status=409
        )


_SCHEMA = """
CREATE TABLE IF NOT EXISTS short_drama_alignment_jobs (
  id TEXT PRIMARY KEY,
  username TEXT NOT NULL,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  idempotency_key TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  provider TEXT NOT NULL,
  provider_job_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('queued','running','succeeded','failed','canceled')),
  version_id TEXT,
  error_json TEXT NOT NULL DEFAULT '{}',
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(username, project_id, idempotency_key)
);
CREATE TABLE IF NOT EXISTS short_drama_alignment_versions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  version INTEGER NOT NULL CHECK (version >= 1),
  parent_id TEXT REFERENCES short_drama_alignment_versions(id),
  status TEXT NOT NULL CHECK (status IN ('needs_review','ready','locked','stale')),
  revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
  provider TEXT NOT NULL,
  model_version TEXT NOT NULL,
  contract_version TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  master_audio_hash TEXT NOT NULL,
  transcript_hash TEXT NOT NULL,
  alignment_hash TEXT NOT NULL,
  timeline_json TEXT NOT NULL,
  quality_json TEXT NOT NULL,
  manual_reviewed INTEGER NOT NULL DEFAULT 0 CHECK (manual_reviewed IN (0,1)),
  review_action TEXT CHECK (
    review_action IN ('save_adjustments','confirm_unchanged')
  ),
  reviewed_by TEXT,
  reviewed_at INTEGER,
  reviewed_source_revision INTEGER,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(project_id, version),
  UNIQUE(project_id, alignment_hash)
);
CREATE TABLE IF NOT EXISTS short_drama_alignment_artifacts (
  id TEXT PRIMARY KEY,
  version_id TEXT NOT NULL REFERENCES short_drama_alignment_versions(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('alignment_json','webvtt','ass')),
  content TEXT NOT NULL,
  file_hash TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  UNIQUE(version_id, kind)
);
CREATE TABLE IF NOT EXISTS short_drama_alignment_current (
  project_id TEXT PRIMARY KEY REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  version_id TEXT NOT NULL REFERENCES short_drama_alignment_versions(id),
  updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_short_drama_alignment_jobs_project
  ON short_drama_alignment_jobs(project_id, status, updated_at);
CREATE INDEX IF NOT EXISTS idx_short_drama_alignment_versions_project
  ON short_drama_alignment_versions(project_id, version DESC);
"""


def init_db(db_factory):
    conn = db_factory()
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_SCHEMA)
        columns = {
            row[1] for row in conn.execute(
                "PRAGMA table_info(short_drama_alignment_versions)"
            )
        }
        migrations = {
            "review_action": (
                "TEXT CHECK (review_action IN "
                "('save_adjustments','confirm_unchanged'))"
            ),
            "reviewed_by": "TEXT",
            "reviewed_at": "INTEGER",
            "reviewed_source_revision": "INTEGER",
        }
        for name, definition in migrations.items():
            if name not in columns:
                conn.execute(
                    "ALTER TABLE short_drama_alignment_versions "
                    f"ADD COLUMN {name} {definition}"
                )
        conn.commit()
    finally:
        conn.close()


def _canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _hash(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _normalize_text(value):
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split())


def _tokens(value):
    normalized = _normalize_text(value)
    return [character for character in normalized if not character.isspace()]


def _version_for_line(line):
    current = line.get("current_version")
    return next(
        (
            item for item in line.get("versions", [])
            if item.get("version") == current
        ),
        None,
    )


def _input_contract(snapshot):
    blockers = []
    shots = []
    absolute_cursor = 0
    transcript = []
    audio_identity = []
    for shot in snapshot.get("shots") or []:
        if not shot.get("locked"):
            blockers.append({
                "code": "transcript_not_locked",
                "message": "所有镜头必须先完成配音字幕校对并锁定",
                "shot_id": shot.get("id"),
            })
        shot_start = absolute_cursor
        shot_duration = int(shot.get("duration") or 0) * 1000
        lines = []
        for line in shot.get("lines") or []:
            version = _version_for_line(line)
            start_ms = line.get("start_ms")
            duration_ms = version.get("duration_ms") if version else None
            if (
                not version
                or version.get("status") != "done"
                or version.get("input_hash") != line.get("input_hash")
                or type(start_ms) is not int
                or type(duration_ms) is not int
                or duration_ms <= 0
            ):
                blockers.append({
                    "code": "missing_master_audio",
                    "message": "存在未就绪或已失效的配音音频",
                    "shot_id": shot.get("id"),
                    "line_id": line.get("id"),
                })
                continue
            audio_start = shot_start + start_ms
            audio_end = audio_start + duration_ms
            text = _normalize_text(line.get("subtitle_text"))
            if not text:
                blockers.append({
                    "code": "transcript_invalid",
                    "message": "锁定字幕文本不能为空",
                    "shot_id": shot.get("id"),
                    "line_id": line.get("id"),
                })
                continue
            item = {
                "shot_id": str(shot.get("id") or ""),
                "line_id": str(line.get("id") or ""),
                "text": text,
                "audio_start_ms": audio_start,
                "audio_end_ms": audio_end,
                "audio_file": str(version.get("audio_file") or ""),
                "source_version": version.get("version"),
                "source_hash": version.get("input_hash"),
            }
            lines.append(item)
            transcript.append({
                "shot_id": item["shot_id"],
                "line_id": item["line_id"],
                "text": text,
            })
            audio_identity.append({
                "shot_id": item["shot_id"],
                "line_id": item["line_id"],
                "version": item["source_version"],
                "input_hash": item["source_hash"],
                "audio_start_ms": audio_start,
                "audio_end_ms": audio_end,
            })
        shots.append({
            "shot_id": str(shot.get("id") or ""),
            "start_ms": shot_start,
            "end_ms": shot_start + shot_duration,
            "lines": lines,
        })
        absolute_cursor += shot_duration
    transcript_hash = _hash(transcript)
    master_audio_hash = _hash({
        "contract": "short_drama_master_timeline_v1",
        "project_id": snapshot.get("project_id"),
        "duration_ms": absolute_cursor,
        "audio": audio_identity,
    })
    identity_shots = [{
        **{key: value for key, value in shot.items() if key != "lines"},
        "lines": [{
            key: value for key, value in line.items()
            if key != "audio_file"
        } for line in shot["lines"]],
    } for shot in shots]
    identity = {
        "contract_version": CONTRACT_VERSION,
        "project_id": snapshot.get("project_id"),
        "project_revision": snapshot.get("revision"),
        "master_audio_hash": master_audio_hash,
        "transcript_hash": transcript_hash,
        "language": "zh-CN",
        "provider": PROVIDER_NAME,
        "model_version": MODEL_VERSION,
        "shots": identity_shots,
    }
    return {
        "input_hash": _hash(identity),
        "master_audio_hash": master_audio_hash,
        "transcript_hash": transcript_hash,
        "identity": identity,
        "shots": shots,
        "blockers": blockers,
    }


class AlignmentProviderUnavailable(RuntimeError):
    pass


def _transcribe_words(audio_file):
    from .core import _resolve_out_file

    path = _resolve_out_file(audio_file)
    if path is None:
        raise AlignmentProviderUnavailable("配音音频文件不存在或不在受控目录")
    model_name = str(
        os.environ.get("SHORT_DRAMA_ALIGNMENT_MODEL", "small")
    ).strip() or "small"
    try:
        from faster_whisper import WhisperModel
    except (ImportError, OSError) as error:
        raise AlignmentProviderUnavailable("faster-whisper 不可用") from error
    global _WHISPER_MODEL, _WHISPER_MODEL_NAME
    if _WHISPER_MODEL is None or _WHISPER_MODEL_NAME != model_name:
        with _WHISPER_MODEL_LOCK:
            if _WHISPER_MODEL is None or _WHISPER_MODEL_NAME != model_name:
                try:
                    _WHISPER_MODEL = WhisperModel(
                        model_name, device="cpu", compute_type="int8"
                    )
                except Exception as error:
                    raise AlignmentProviderUnavailable(
                        "字幕对齐 ASR 模型加载失败"
                    ) from error
                _WHISPER_MODEL_NAME = model_name
    try:
        with _ALIGNMENT_SEMAPHORE:
            segments, _ = _WHISPER_MODEL.transcribe(
                str(path),
                language="zh",
                vad_filter=True,
                word_timestamps=True,
                beam_size=5,
            )
            words = []
            for segment in segments:
                for word in getattr(segment, "words", None) or []:
                    text = str(getattr(word, "word", "") or "").strip()
                    start = getattr(word, "start", None)
                    end = getattr(word, "end", None)
                    if (
                        not text
                        or not isinstance(start, (int, float))
                        or not isinstance(end, (int, float))
                        or end <= start
                    ):
                        continue
                    probability = getattr(word, "probability", None)
                    words.append({
                        "word": text,
                        "start_ms": max(0, round(float(start) * 1000)),
                        "end_ms": max(1, round(float(end) * 1000)),
                        "confidence": (
                            max(0.0, min(1.0, float(probability)))
                            if isinstance(probability, (int, float))
                            else None
                        ),
                    })
            return words
    except AlignmentProviderUnavailable:
        raise
    except Exception as error:
        raise AlignmentProviderUnavailable("字幕对齐 ASR 执行失败") from error


def _recognized_tokens(words, audio_start_ms, audio_end_ms):
    recognized = []
    duration = audio_end_ms - audio_start_ms
    for word in words or []:
        pieces = _tokens(word.get("word"))
        if not pieces:
            continue
        word_start = max(
            0, min(max(0, duration - 1), int(word.get("start_ms") or 0))
        )
        word_end = max(
            word_start + 1,
            min(duration, int(word.get("end_ms") or word_start + 1)),
        )
        timing_estimated = False
        if word_end - word_start < len(pieces):
            if duration < len(pieces):
                raise AlignmentError(
                    "alignment_resolution_insufficient",
                    "音频时长不足以生成严格递增的 token 时间轴",
                    status=422,
                )
            word_start = min(word_start, duration - len(pieces))
            word_end = word_start + len(pieces)
            timing_estimated = True
        for index, token in enumerate(pieces):
            start = audio_start_ms + round(
                word_start + (word_end - word_start) * index / len(pieces)
            )
            end = audio_start_ms + round(
                word_start
                + (word_end - word_start) * (index + 1) / len(pieces)
            )
            recognized.append({
                "token": token,
                "start_ms": start,
                "end_ms": max(start + 1, end),
                "confidence": (
                    None if timing_estimated else word.get("confidence")
                ),
                "match_type": (
                    "estimated" if timing_estimated
                    else "asr_word" if len(pieces) == 1
                    else "interpolated_within_asr_word"
                ),
            })
    return recognized


def _fill_estimated_ranges(slots, expected, line):
    index = 0
    while index < len(slots):
        if slots[index] is not None:
            index += 1
            continue
        end_index = index
        while end_index < len(slots) and slots[end_index] is None:
            end_index += 1
        left = (
            line["audio_start_ms"]
            if index == 0 else slots[index - 1]["end_ms"]
        )
        right = (
            line["audio_end_ms"]
            if end_index == len(slots) else slots[end_index]["start_ms"]
        )
        count = end_index - index
        if right <= left:
            left = line["audio_start_ms"]
            right = line["audio_end_ms"]
        for offset in range(count):
            start = round(left + (right - left) * offset / count)
            end = round(left + (right - left) * (offset + 1) / count)
            slots[index + offset] = {
                "token": expected[index + offset],
                "start_ms": start,
                "end_ms": max(start + 1, end),
                "confidence": None,
                "match_type": "estimated",
            }
        index = end_index
    return slots


def _align_line(line, words):
    expected = _tokens(line["text"])
    if not expected:
        return [], 0
    duration = line["audio_end_ms"] - line["audio_start_ms"]
    if duration < len(expected):
        raise AlignmentError(
            "alignment_resolution_insufficient",
            "音频时长不足以生成严格递增的 token 时间轴",
            status=422,
        )
    recognized = _recognized_tokens(
        words, line["audio_start_ms"], line["audio_end_ms"]
    )
    slots = [None] * len(expected)
    matcher = SequenceMatcher(
        None,
        [_normalize_text(token) for token in expected],
        [_normalize_text(item["token"]) for item in recognized],
        autojunk=False,
    )
    for block in matcher.get_matching_blocks():
        for offset in range(block.size):
            expected_index = block.a + offset
            source = recognized[block.b + offset]
            slots[expected_index] = {
                **source,
                "token": expected[expected_index],
            }
    tokens = _fill_estimated_ranges(slots, expected, line)
    cursor = line["audio_start_ms"]
    for index, token in enumerate(tokens):
        token["normalized_token"] = _normalize_text(token["token"])
        remaining = len(tokens) - index - 1
        latest_start = line["audio_end_ms"] - remaining - 1
        original_start = int(token["start_ms"])
        original_end = int(token["end_ms"])
        token["start_ms"] = min(
            latest_start, max(cursor, original_start)
        )
        latest_end = line["audio_end_ms"] - remaining
        token["end_ms"] = min(
            latest_end, max(token["start_ms"] + 1, original_end)
        )
        if (
            token["start_ms"] != original_start
            or token["end_ms"] != original_end
        ):
            token["confidence"] = None
            token["match_type"] = "estimated"
        cursor = token["end_ms"]
    matched = sum(
        token["match_type"] != "estimated" for token in tokens
    )
    return tokens, matched


def _align(contract, transcriber=None):
    transcriber = transcriber or _transcribe_words
    timeline = []
    token_count = 0
    matched_count = 0
    confidences = []
    low_confidence_ranges = []
    unmatched_tokens = []
    line_coverages = []
    provider_errors = []
    for shot in contract["shots"]:
        for line in shot["lines"]:
            words = []
            try:
                words = transcriber(line.get("audio_file") or "")
            except AlignmentProviderUnavailable as error:
                provider_errors.append({
                    "line_id": line["line_id"],
                    "message": str(error)[:220],
                })
            tokens, matched = _align_line(line, words)
            token_count += len(tokens)
            matched_count += matched
            coverage = matched / len(tokens) if tokens else 1.0
            line_coverages.append({
                "line_id": line["line_id"],
                "coverage": round(coverage, 4),
            })
            for token in tokens:
                confidence = token.get("confidence")
                if isinstance(confidence, (int, float)):
                    confidences.append(float(confidence))
                    if confidence < 0.80:
                        low_confidence_ranges.append({
                            "line_id": line["line_id"],
                            "token": token["token"],
                            "start_ms": token["start_ms"],
                            "end_ms": token["end_ms"],
                            "confidence": round(float(confidence), 4),
                        })
                if token["match_type"] == "estimated":
                    unmatched_tokens.append({
                        "line_id": line["line_id"],
                        "token": token["token"],
                    })
            timeline.append({
                "shot_id": line["shot_id"],
                "line_id": line["line_id"],
                "text": line["text"],
                "audio_start_ms": line["audio_start_ms"],
                "audio_end_ms": line["audio_end_ms"],
                "subtitle_start_ms": (
                    tokens[0]["start_ms"] if tokens else line["audio_start_ms"]
                ),
                "subtitle_end_ms": (
                    tokens[-1]["end_ms"] if tokens else line["audio_end_ms"]
                ),
                "alignment_source": (
                    "asr_word_timestamps"
                    if matched == len(tokens) and tokens
                    else "estimated_fallback"
                ),
                "tokens": tokens,
            })
    coverage = matched_count / token_count if token_count else 1.0
    mean_confidence = (
        sum(confidences) / len(confidences) if confidences else None
    )
    estimated_count = token_count - matched_count
    blockers = [{
        "code": "manual_review_required",
        "message": "字幕时间轴必须经过人工校对后才能锁定",
    }]
    if provider_errors:
        blockers.append({
            "code": "forced_alignment_unavailable",
            "message": "真实音频对齐不可用，当前仅提供估算时间轴",
        })
    if estimated_count:
        blockers.append({
            "code": "estimated_timing_present",
            "message": "存在没有真实 ASR 时间戳的估算 token",
        })
    if coverage < 0.98:
        blockers.append({
            "code": "project_coverage_low",
            "message": "项目真实时间戳覆盖率低于 98%",
        })
    if any(item["coverage"] < 0.95 for item in line_coverages):
        blockers.append({
            "code": "line_coverage_low",
            "message": "存在真实时间戳覆盖率低于 95% 的台词",
        })
    if mean_confidence is not None and mean_confidence < 0.80:
        blockers.append({
            "code": "mean_confidence_low",
            "message": "真实时间戳平均置信度低于 0.80",
        })
    if not token_count:
        provider_mode = "not_applicable"
    elif not matched_count:
        provider_mode = "estimated_fallback"
    elif estimated_count:
        provider_mode = "mixed"
    else:
        provider_mode = "asr_word_timestamps"
    quality = {
        "provider_mode": provider_mode,
        "coverage": round(coverage, 4),
        "line_coverages": line_coverages,
        "mean_confidence": (
            round(mean_confidence, 4) if mean_confidence is not None else None
        ),
        "low_confidence_ranges": low_confidence_ranges,
        "unmatched_tokens": unmatched_tokens,
        "estimated_token_count": estimated_count,
        "provider_errors": provider_errors,
        "thresholds": {
            "project_coverage": 0.98,
            "line_coverage": 0.95,
            "mean_confidence": 0.80,
        },
        "blockers": blockers,
    }
    return timeline, quality


def _timestamp(ms, *, ass=False):
    value = max(0, int(ms))
    hours, remainder = divmod(value, 3600000)
    minutes, remainder = divmod(remainder, 60000)
    seconds, millis = divmod(remainder, 1000)
    if ass:
        return f"{hours}:{minutes:02d}:{seconds:02d}.{millis // 10:02d}"
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def _artifact_payloads(version):
    timeline = version["timeline"]
    alignment_json = _canonical({
        "contract_version": CONTRACT_VERSION,
        "alignment_hash": version["alignment_hash"],
        "master_audio_hash": version["master_audio_hash"],
        "transcript_hash": version["transcript_hash"],
        "timeline": timeline,
        "quality": version["quality"],
        "review": (
            {
                "action": version.get("review_action"),
                "reviewed_by": version.get("reviewed_by"),
                "reviewed_at": version.get("reviewed_at"),
                "source_version_id": version.get("parent_id"),
                "source_revision": version.get("reviewed_source_revision"),
            }
            if version.get("manual_reviewed")
            else None
        ),
    })
    vtt_lines = ["WEBVTT", ""]
    ass_lines = [
        "[Script Info]", "ScriptType: v4.00+", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, Alignment",
        (
            "Style: Default,"
            f"{assembly_subtitles.FONT_NAME},42,&H00FFFFFF,2"
        ),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Text",
    ]
    for index, line in enumerate(timeline, 1):
        text = str(line["text"]).replace("\n", " ").replace("\r", " ")
        vtt_lines.extend([
            str(index),
            f"{_timestamp(line['subtitle_start_ms'])} --> "
            f"{_timestamp(line['subtitle_end_ms'])}",
            text,
            "",
        ])
        ass_text = text.replace("{", "｛").replace("}", "｝")
        ass_lines.append(
            "Dialogue: 0,"
            f"{_timestamp(line['subtitle_start_ms'], ass=True)},"
            f"{_timestamp(line['subtitle_end_ms'], ass=True)},Default,{ass_text}"
        )
    return {
        "alignment_json": alignment_json,
        "webvtt": "\n".join(vtt_lines),
        "ass": "\n".join(ass_lines) + "\n",
    }


def _row_version(row):
    result = dict(row)
    result["timeline"] = json.loads(result.pop("timeline_json"))
    result["quality"] = json.loads(result.pop("quality_json"))
    result["manual_reviewed"] = bool(result["manual_reviewed"])
    result["review"] = (
        {
            "action": result.get("review_action"),
            "reviewed_by": result.get("reviewed_by"),
            "reviewed_at": result.get("reviewed_at"),
            "source_version_id": result.get("parent_id"),
            "source_revision": result.get("reviewed_source_revision"),
        }
        if result["manual_reviewed"]
        else None
    )
    return result


def _review_audit_complete(version):
    return bool(
        version.get("manual_reviewed")
        and version.get("review_action") in REVIEW_ACTIONS
        and version.get("reviewed_by")
        and type(version.get("reviewed_at")) is int
        and type(version.get("reviewed_source_revision")) is int
        and version.get("parent_id")
    )


def _review_audit_complete_in_db(conn, version):
    if not _review_audit_complete(version):
        return False
    parent = conn.execute(
        "SELECT project_id,revision FROM short_drama_alignment_versions "
        "WHERE id=?",
        (version.get("parent_id"),),
    ).fetchone()
    return bool(
        parent
        and parent[0] == version.get("project_id")
        and int(parent[1]) == version.get("reviewed_source_revision")
    )


def _store_artifacts(conn, version):
    now = int(time.time())
    for kind, content in _artifact_payloads(version).items():
        conn.execute(
            "INSERT INTO short_drama_alignment_artifacts "
            "(id,version_id,kind,content,file_hash,created_at) VALUES (?,?,?,?,?,?)",
            (
                str(uuid.uuid4()), version["id"], kind, content,
                hashlib.sha256(content.encode("utf-8")).hexdigest(), now,
            ),
        )


def _reconcile_interrupted_jobs(conn, project_id):
    cutoff = int(time.time()) - INTERRUPTED_JOB_SECONDS
    terminal = _canonical({
        "code": "local_alignment_interrupted",
        "type": "ProcessInterrupted",
    })
    conn.execute(
        "UPDATE short_drama_alignment_jobs "
        "SET status='failed',error_json=?,updated_at=? "
        "WHERE project_id=? AND status IN ('queued','running') AND updated_at<=?",
        (terminal, int(time.time()), project_id, cutoff),
    )


def _project(conn, username, project_id):
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM short_drama_projects "
        "WHERE id=? AND username=? AND deleted=0",
        (project_id, username),
    ).fetchone()
    if not row:
        raise LookupError("短剧项目不存在")
    return row


def _current_contract(conn, project):
    short_drama_voice.ensure_voice_workspace(conn, project["id"])
    short_drama_voice.reconcile_voice_jobs(conn, project["id"])
    snapshot = short_drama_voice.build_voice_snapshot(conn, project)
    return snapshot, _input_contract(snapshot)


def _workspace(conn, project):
    snapshot, contract = _current_contract(conn, project)
    _reconcile_interrupted_jobs(conn, project["id"])
    versions = [
        _row_version(row) for row in conn.execute(
            "SELECT * FROM short_drama_alignment_versions "
            "WHERE project_id=? ORDER BY version DESC LIMIT 20",
            (project["id"],),
        )
    ]
    current_row = conn.execute(
        "SELECT version_id FROM short_drama_alignment_current WHERE project_id=?",
        (project["id"],),
    ).fetchone()
    current_id = current_row[0] if current_row else None
    for version in versions:
        if (
            version["input_hash"] != contract["input_hash"]
            and version["status"] != "stale"
        ):
            version["effective_status"] = "stale"
        else:
            version["effective_status"] = version["status"]
        version["current"] = version["id"] == current_id
        version["artifacts"] = [
            dict(row) for row in conn.execute(
                "SELECT kind,file_hash,created_at "
                "FROM short_drama_alignment_artifacts WHERE version_id=? "
                "ORDER BY kind",
                (version["id"],),
            )
        ]
    latest_job = conn.execute(
        "SELECT * FROM short_drama_alignment_jobs WHERE project_id=? "
        "ORDER BY updated_at DESC LIMIT 1",
        (project["id"],),
    ).fetchone()
    active = conn.execute(
        "SELECT 1 FROM short_drama_alignment_jobs WHERE project_id=? "
        "AND status IN ('queued','running') LIMIT 1",
        (project["id"],),
    ).fetchone()
    blockers = list(contract["blockers"])
    if active:
        blockers.append({
            "code": "active_alignment_job",
            "message": "字幕对齐任务仍在处理中",
        })
    current = next((item for item in versions if item["current"]), None)
    alignment_started = bool(
        versions
        or active
        or latest_job and latest_job["status"] == "succeeded"
    )
    handoff_ready = (
        not alignment_started
        or bool(current and current["effective_status"] == "locked")
    )
    handoff_blockers = []
    if alignment_started and not handoff_ready:
        handoff_blockers.append({
            "code": (
                "active_alignment_job"
                if active
                else "stale_alignment"
                if current and current["effective_status"] == "stale"
                else "alignment_not_locked"
            ),
            "message": (
                "字幕对齐任务仍在处理中"
                if active
                else "锁定的字幕对齐版本已失效，请重新生成"
                if current and current["effective_status"] == "stale"
                else "请先完成人工校对并锁定字幕对齐版本"
            ),
        })
    return {
        "project_id": project["id"],
        "project_revision": project["revision"],
        "stage": project["stage"],
        "provider": {
            "name": PROVIDER_NAME,
            "model_version": MODEL_VERSION,
            "real_forced_alignment": True,
            "supports_cancel": True,
            "supports_resume": False,
        },
        "input": {
            "input_hash": contract["input_hash"],
            "master_audio_hash": contract["master_audio_hash"],
            "transcript_hash": contract["transcript_hash"],
        },
        "readiness": {
            "ready": not blockers and project["stage"] == WRITABLE_STAGE,
            "blockers": blockers,
        },
        "handoff": {
            "required": alignment_started,
            "ready": handoff_ready,
            "blockers": handoff_blockers,
        },
        "versions": versions,
        "current_version": current,
        "job": dict(latest_job) if latest_job else None,
        "actions": {
            "generate": (
                not blockers and project["stage"] == WRITABLE_STAGE
            ),
            "save": bool(
                current and current["effective_status"] == "needs_review"
                and project["stage"] == WRITABLE_STAGE
            ),
            "lock": bool(
                current and current["effective_status"] == "ready"
                and _review_audit_complete_in_db(conn, current)
                and project["stage"] == WRITABLE_STAGE
            ),
        },
        "voice": {
            "shot_count": len(snapshot.get("shots") or []),
            "locked_shot_count": sum(
                1 for shot in snapshot.get("shots") or [] if shot.get("locked")
            ),
        },
    }


def get_workspace(db_factory, username, project_id):
    conn = db_factory()
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        project = _project(conn, username, project_id)
        result = _workspace(conn, project)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def require_current_locked_in_transaction(conn, project):
    """Require a current locked version using the caller's transaction."""
    _, contract = _current_contract(conn, project)
    row = conn.execute(
        "SELECT v.* FROM short_drama_alignment_current c "
        "JOIN short_drama_alignment_versions v ON v.id=c.version_id "
        "WHERE c.project_id=? AND v.status='locked'",
        (project["id"],),
    ).fetchone()
    if not row or not _review_audit_complete_in_db(conn, dict(row)):
        raise AlignmentError(
            "alignment_not_locked", "请先完成人工校对并锁定字幕对齐版本",
            status=422,
        )
    if row["input_hash"] != contract["input_hash"]:
        raise AlignmentError(
            "stale_alignment", "锁定的字幕对齐版本已失效，请重新生成",
            status=409,
        )
    return _row_version(row)


def require_current_locked(db_factory, username, project_id):
    conn = db_factory()
    try:
        conn.row_factory = sqlite3.Row
        project = _project(conn, username, project_id)
        return require_current_locked_in_transaction(conn, project)
    finally:
        conn.close()


def require_locked_if_started_in_transaction(conn, project):
    """Gate handoff once a durable alignment job or version has started."""
    active = conn.execute(
        "SELECT 1 FROM short_drama_alignment_jobs WHERE project_id=? "
        "AND status IN ('queued','running') LIMIT 1",
        (project["id"],),
    ).fetchone()
    if active:
        raise AlignmentError(
            "active_alignment_job", "字幕对齐任务仍在处理中", status=409
        )
    started = conn.execute(
        "SELECT 1 FROM short_drama_alignment_versions WHERE project_id=? "
        "UNION ALL "
        "SELECT 1 FROM short_drama_alignment_jobs "
        "WHERE project_id=? AND status='succeeded' LIMIT 1",
        (project["id"], project["id"]),
    ).fetchone()
    if started:
        return require_current_locked_in_transaction(conn, project)
    return None


def require_locked_if_started(db_factory, username, project_id):
    """Preserve legacy projects while checking handoff in one write transaction."""
    conn = db_factory()
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        project = _project(conn, username, project_id)
        result = require_locked_if_started_in_transaction(conn, project)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def create_job(db_factory, username, payload, idempotency_key):
    if not isinstance(payload, dict) or set(payload) != {"project_id", "revision"}:
        raise AlignmentError("invalid_request", "字幕对齐请求字段不正确")
    project_id = str(payload.get("project_id") or "").strip()
    revision = payload.get("revision")
    key = str(idempotency_key or "").strip()
    if not project_id or type(revision) is not int or not key or len(key) > 160:
        raise AlignmentError("invalid_request", "字幕对齐请求无效")
    conn = db_factory()
    job_id = None
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        project = _project(conn, username, project_id)
        if int(project["revision"]) != revision:
            from .short_drama import RevisionConflict
            raise RevisionConflict("项目已更新，请刷新后重试")
        if project["stage"] != WRITABLE_STAGE:
            raise AlignmentError("stage_invalid", "当前阶段不能生成字幕对齐")
        _, contract = _current_contract(conn, project)
        _reconcile_interrupted_jobs(conn, project_id)
        if contract["blockers"]:
            raise AlignmentError(
                contract["blockers"][0]["code"],
                contract["blockers"][0]["message"],
                status=422,
                blockers=contract["blockers"],
            )
        request_hash = _hash({
            "username": username,
            "project_id": project_id,
            "revision": revision,
            "input_hash": contract["input_hash"],
            "operation": "generate",
        })
        existing = conn.execute(
            "SELECT * FROM short_drama_alignment_jobs "
            "WHERE username=? AND project_id=? AND idempotency_key=?",
            (username, project_id, key),
        ).fetchone()
        if existing:
            if existing["request_hash"] != request_hash:
                raise AlignmentIdempotencyConflict()
            result = {
                "replayed": True,
                "reused": False,
                "job": dict(existing),
                "workspace": _workspace(conn, project),
            }
            conn.commit()
            return result
        if conn.execute(
            "SELECT 1 FROM short_drama_alignment_jobs WHERE project_id=? "
            "AND status IN ('queued','running') LIMIT 1",
            (project_id,),
        ).fetchone():
            raise AlignmentError(
                "active_alignment_job", "已有字幕对齐任务正在处理", status=409
            )
        now = int(time.time())
        job_id = str(uuid.uuid4())
        provider_job_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO short_drama_alignment_jobs "
            "(id,username,project_id,idempotency_key,request_hash,input_hash,"
            "provider,provider_job_id,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                job_id, username, project_id, key, request_hash,
                contract["input_hash"], PROVIDER_NAME, provider_job_id,
                "running", now, now,
            ),
        )
        # A real provider may start billing as soon as it returns a job id.
        # Commit the recovery identity before polling or materializing results.
        conn.commit()
        timeline, quality = _align(contract)
        conn.execute("BEGIN IMMEDIATE")
        project = _project(conn, username, project_id)
        if (
            project["stage"] != WRITABLE_STAGE
            or int(project["revision"]) != revision
        ):
            raise AlignmentError(
                "stage_changed",
                "项目阶段或版本已更新，对齐结果不再可用",
                status=409,
            )
        _, current_contract = _current_contract(conn, project)
        if current_contract["input_hash"] != contract["input_hash"]:
            raise AlignmentError(
                "stale_alignment", "对齐输入已变更，请重新生成", status=409
            )
        alignment_hash = _hash({
            "contract_version": CONTRACT_VERSION,
            "input_hash": contract["input_hash"],
            "provider": PROVIDER_NAME,
            "model_version": MODEL_VERSION,
            "timeline": timeline,
        })
        reusable = conn.execute(
            "SELECT * FROM short_drama_alignment_versions "
            "WHERE project_id=? AND alignment_hash=?",
            (project_id, alignment_hash),
        ).fetchone()
        if reusable:
            conn.execute(
                "INSERT INTO short_drama_alignment_current"
                "(project_id,version_id,updated_at) VALUES (?,?,?) "
                "ON CONFLICT(project_id) DO UPDATE SET "
                "version_id=excluded.version_id,updated_at=excluded.updated_at",
                (project_id, reusable["id"], now),
            )
            conn.execute(
                "UPDATE short_drama_alignment_jobs "
                "SET status='succeeded',version_id=?,updated_at=? WHERE id=?",
                (reusable["id"], now, job_id),
            )
            result = {
                "replayed": False,
                "reused": True,
                "job": dict(conn.execute(
                    "SELECT * FROM short_drama_alignment_jobs WHERE id=?",
                    (job_id,),
                ).fetchone()),
                "workspace": _workspace(conn, project),
            }
            conn.commit()
            return result
        previous = conn.execute(
            "SELECT COALESCE(MAX(version),0) FROM short_drama_alignment_versions "
            "WHERE project_id=?",
            (project_id,),
        ).fetchone()[0]
        version = {
            "id": str(uuid.uuid4()),
            "project_id": project_id,
            "version": int(previous) + 1,
            "parent_id": None,
            "status": "needs_review",
            "revision": 1,
            "provider": PROVIDER_NAME,
            "model_version": MODEL_VERSION,
            "contract_version": CONTRACT_VERSION,
            "input_hash": contract["input_hash"],
            "master_audio_hash": contract["master_audio_hash"],
            "transcript_hash": contract["transcript_hash"],
            "alignment_hash": alignment_hash,
            "timeline": timeline,
            "quality": quality,
            "manual_reviewed": False,
            "created_at": now,
            "updated_at": now,
        }
        conn.execute(
            "INSERT INTO short_drama_alignment_versions "
            "(id,project_id,version,parent_id,status,revision,provider,model_version,"
            "contract_version,input_hash,master_audio_hash,transcript_hash,"
            "alignment_hash,timeline_json,quality_json,manual_reviewed,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                version["id"], project_id, version["version"], None,
                version["status"], 1, version["provider"], version["model_version"],
                CONTRACT_VERSION, version["input_hash"], version["master_audio_hash"],
                version["transcript_hash"], version["alignment_hash"],
                _canonical(timeline), _canonical(quality), 0, now, now,
            ),
        )
        _store_artifacts(conn, version)
        conn.execute(
            "INSERT INTO short_drama_alignment_current(project_id,version_id,updated_at) "
            "VALUES (?,?,?) ON CONFLICT(project_id) DO UPDATE SET "
            "version_id=excluded.version_id,updated_at=excluded.updated_at",
            (project_id, version["id"], now),
        )
        conn.execute(
            "UPDATE short_drama_alignment_jobs "
            "SET status='succeeded',version_id=?,updated_at=? WHERE id=?",
            (version["id"], now, job_id),
        )
        result = {
            "replayed": False,
            "reused": False,
            "job": dict(conn.execute(
                "SELECT * FROM short_drama_alignment_jobs WHERE id=?", (job_id,)
            ).fetchone()),
            "workspace": _workspace(conn, project),
        }
        conn.commit()
        return result
    except Exception as error:
        conn.rollback()
        if job_id:
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "UPDATE short_drama_alignment_jobs "
                    "SET status='failed',error_json=?,updated_at=? "
                    "WHERE id=? AND status IN ('queued','running')",
                    (
                        _canonical({
                            "code": getattr(error, "code", "alignment_failed"),
                            "type": error.__class__.__name__,
                        }),
                        int(time.time()),
                        job_id,
                    ),
                )
                conn.commit()
            except Exception:
                conn.rollback()
        raise
    finally:
        conn.close()


def _timeline_request(payload):
    if not isinstance(payload, dict) or set(payload) != {
        "project_id", "version_id", "revision", "review_action", "lines",
    }:
        raise AlignmentError("invalid_request", "字幕校对请求字段不正确")
    review_action = str(payload.get("review_action") or "").strip()
    if review_action not in REVIEW_ACTIONS:
        raise AlignmentError("invalid_request", "字幕校对确认方式无效")
    lines = payload.get("lines")
    if not isinstance(lines, list):
        raise AlignmentError("invalid_request", "字幕校对时间线必须为数组")
    normalized = []
    for item in lines:
        if not isinstance(item, dict) or set(item) != {
            "line_id", "subtitle_start_ms", "subtitle_end_ms",
        }:
            raise AlignmentError("invalid_request", "字幕校对条目字段不正确")
        start = item.get("subtitle_start_ms")
        end = item.get("subtitle_end_ms")
        if type(start) is not int or type(end) is not int or start < 0 or end <= start:
            raise AlignmentError("timeline_invalid", "字幕校对时间无效", status=422)
        normalized.append({
            "line_id": str(item.get("line_id") or ""),
            "subtitle_start_ms": start,
            "subtitle_end_ms": end,
        })
    return {
        "project_id": str(payload.get("project_id") or ""),
        "version_id": str(payload.get("version_id") or ""),
        "revision": payload.get("revision"),
        "review_action": review_action,
        "lines": normalized,
    }


def save_timeline(db_factory, username, payload, actor_username=None):
    request = _timeline_request(payload)
    reviewer = str(actor_username or username or "").strip()
    if not reviewer:
        raise AlignmentError("invalid_request", "字幕校对审核人无效")
    conn = db_factory()
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        project = _project(conn, username, request["project_id"])
        if project["stage"] != WRITABLE_STAGE:
            raise AlignmentError("stage_invalid", "当前阶段不能校对字幕")
        source_row = conn.execute(
            "SELECT * FROM short_drama_alignment_versions "
            "WHERE id=? AND project_id=?",
            (request["version_id"], request["project_id"]),
        ).fetchone()
        if not source_row:
            raise LookupError("字幕对齐版本不存在")
        source = _row_version(source_row)
        if type(request["revision"]) is not int or source["revision"] != request["revision"]:
            from .short_drama import RevisionConflict
            raise RevisionConflict("字幕对齐版本已更新，请刷新后重试")
        _, contract = _current_contract(conn, project)
        if source["input_hash"] != contract["input_hash"]:
            raise AlignmentError("stale_alignment", "字幕对齐版本已失效", status=409)
        updates = {item["line_id"]: item for item in request["lines"]}
        if set(updates) != {item["line_id"] for item in source["timeline"]}:
            raise AlignmentError("timeline_invalid", "必须提交完整且不重复的字幕时间线")
        timeline = []
        for line in source["timeline"]:
            changed = dict(line)
            changed.update(updates[line["line_id"]])
            if (
                changed["subtitle_start_ms"] < changed["audio_start_ms"]
                or changed["subtitle_end_ms"] > changed["audio_end_ms"]
            ):
                raise AlignmentError(
                    "timeline_boundary_invalid",
                    "字幕时间必须位于对应音频区间内",
                    status=422,
                )
            timeline.append(changed)
        unchanged = all(
            item["subtitle_start_ms"] == original["subtitle_start_ms"]
            and item["subtitle_end_ms"] == original["subtitle_end_ms"]
            for item, original in zip(timeline, source["timeline"])
        )
        if (
            request["review_action"] == "save_adjustments"
            and unchanged
        ):
            raise AlignmentError(
                "review_action_mismatch",
                "保存调整要求至少修改一条字幕边界",
                status=422,
            )
        if (
            request["review_action"] == "confirm_unchanged"
            and not unchanged
        ):
            raise AlignmentError(
                "review_action_mismatch",
                "原样确认不能包含字幕边界修改",
                status=422,
            )
        ordered = sorted(timeline, key=lambda item: (
            item["subtitle_start_ms"], item["subtitle_end_ms"], item["line_id"]
        ))
        for previous, current in zip(ordered, ordered[1:]):
            if current["subtitle_start_ms"] < previous["subtitle_end_ms"]:
                raise AlignmentError("subtitle_overlap", "字幕时间发生重叠", status=422)
        quality = dict(source["quality"])
        quality["blockers"] = []
        quality["manual_reviewed"] = True
        quality["review_action"] = request["review_action"]
        alignment_hash = _hash({
            "contract_version": CONTRACT_VERSION,
            "input_hash": source["input_hash"],
            "parent_id": source["id"],
            "source_revision": source["revision"],
            "timeline": timeline,
            "manual_reviewed": True,
            "review_action": request["review_action"],
        })
        existing = conn.execute(
            "SELECT * FROM short_drama_alignment_versions "
            "WHERE project_id=? AND alignment_hash=?",
            (request["project_id"], alignment_hash),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE short_drama_alignment_current "
                "SET version_id=?,updated_at=? WHERE project_id=?",
                (
                    existing["id"], int(time.time()),
                    request["project_id"],
                ),
            )
            result = _workspace(conn, project)
            conn.commit()
            return result
        current = conn.execute(
            "SELECT version_id FROM short_drama_alignment_current "
            "WHERE project_id=?",
            (request["project_id"],),
        ).fetchone()
        if (
            not current
            or current["version_id"] != source["id"]
            or source["status"] != "needs_review"
        ):
            raise AlignmentError(
                "alignment_version_not_current",
                "字幕对齐版本已被其他审核结果替换，请刷新后重试",
                status=409,
            )
        next_number = conn.execute(
            "SELECT COALESCE(MAX(version),0)+1 "
            "FROM short_drama_alignment_versions WHERE project_id=?",
            (request["project_id"],),
        ).fetchone()[0]
        now = int(time.time())
        version = {
            **source,
            "id": str(uuid.uuid4()),
            "version": int(next_number),
            "parent_id": source["id"],
            "status": "ready",
            "revision": 1,
            "alignment_hash": alignment_hash,
            "timeline": timeline,
            "quality": quality,
            "manual_reviewed": True,
            "review_action": request["review_action"],
            "reviewed_by": reviewer,
            "reviewed_at": now,
            "reviewed_source_revision": source["revision"],
            "created_at": now,
            "updated_at": now,
        }
        conn.execute(
            "INSERT INTO short_drama_alignment_versions "
            "(id,project_id,version,parent_id,status,revision,provider,model_version,"
            "contract_version,input_hash,master_audio_hash,transcript_hash,"
            "alignment_hash,timeline_json,quality_json,manual_reviewed,"
            "review_action,reviewed_by,reviewed_at,reviewed_source_revision,"
            "created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                version["id"], request["project_id"], version["version"],
                source["id"], "ready", 1, source["provider"],
                source["model_version"], CONTRACT_VERSION, source["input_hash"],
                source["master_audio_hash"], source["transcript_hash"],
                alignment_hash, _canonical(timeline), _canonical(quality), 1,
                request["review_action"], reviewer, now, source["revision"],
                now, now,
            ),
        )
        _store_artifacts(conn, version)
        conn.execute(
            "UPDATE short_drama_alignment_current SET version_id=?,updated_at=? "
            "WHERE project_id=?",
            (version["id"], now, request["project_id"]),
        )
        result = _workspace(conn, project)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def lock_version(db_factory, username, payload):
    if not isinstance(payload, dict) or set(payload) != {
        "project_id", "version_id", "revision",
    }:
        raise AlignmentError("invalid_request", "字幕锁定请求字段不正确")
    conn = db_factory()
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        project = _project(conn, username, str(payload.get("project_id") or ""))
        if project["stage"] != WRITABLE_STAGE:
            raise AlignmentError("stage_invalid", "当前阶段不能锁定字幕对齐")
        row = conn.execute(
            "SELECT * FROM short_drama_alignment_versions "
            "WHERE id=? AND project_id=?",
            (str(payload.get("version_id") or ""), project["id"]),
        ).fetchone()
        if not row:
            raise LookupError("字幕对齐版本不存在")
        version = _row_version(row)
        if type(payload.get("revision")) is not int or version["revision"] != payload["revision"]:
            from .short_drama import RevisionConflict
            raise RevisionConflict("字幕对齐版本已更新，请刷新后重试")
        _, contract = _current_contract(conn, project)
        if version["input_hash"] != contract["input_hash"]:
            raise AlignmentError("stale_alignment", "字幕对齐版本已失效", status=409)
        if (
            version["status"] != "ready"
            or not _review_audit_complete_in_db(conn, version)
            or version["quality"].get("blockers")
        ):
            raise AlignmentError(
                "quality_gate_blocked", "字幕对齐尚未通过人工校对和质量门禁", status=422
            )
        now = int(time.time())
        conn.execute(
            "UPDATE short_drama_alignment_versions "
            "SET status='locked',revision=revision+1,updated_at=? WHERE id=?",
            (now, version["id"]),
        )
        conn.execute(
            "UPDATE short_drama_alignment_current SET version_id=?,updated_at=? "
            "WHERE project_id=?",
            (version["id"], now, project["id"]),
        )
        result = _workspace(conn, project)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def cancel_job(db_factory, username, payload):
    if not isinstance(payload, dict) or set(payload) != {"project_id", "job_id"}:
        raise AlignmentError("invalid_request", "字幕任务取消请求字段不正确")
    conn = db_factory()
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        _project(conn, username, str(payload.get("project_id") or ""))
        row = conn.execute(
            "SELECT * FROM short_drama_alignment_jobs "
            "WHERE id=? AND project_id=? AND username=?",
            (
                str(payload.get("job_id") or ""),
                str(payload.get("project_id") or ""),
                username,
            ),
        ).fetchone()
        if not row:
            raise LookupError("字幕对齐任务不存在")
        if row["status"] in {"queued", "running"}:
            conn.execute(
                "UPDATE short_drama_alignment_jobs "
                "SET status='canceled',updated_at=? WHERE id=?",
                (int(time.time()), row["id"]),
            )
        result = dict(conn.execute(
            "SELECT * FROM short_drama_alignment_jobs WHERE id=?", (row["id"],)
        ).fetchone())
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def apply_locked_timeline(conn, project_id, media_plan):
    """Return a copy of a media plan using the current locked alignment."""
    row = conn.execute(
        "SELECT v.* FROM short_drama_alignment_current c "
        "JOIN short_drama_alignment_versions v ON v.id=c.version_id "
        "WHERE c.project_id=? AND v.status='locked'",
        (project_id,),
    ).fetchone()
    if not row or not _review_audit_complete_in_db(conn, dict(row)):
        return media_plan
    version = _row_version(row)
    by_line = {item["line_id"]: item for item in version["timeline"]}
    copied = json.loads(json.dumps(media_plan))
    for shot in copied.get("shots") or []:
        shot_start = int(shot.get("start_ms") or 0)
        audio = shot.get("audio") if isinstance(shot.get("audio"), dict) else {}
        for line in audio.get("lines") or []:
            aligned = by_line.get(str(line.get("id") or ""))
            if not aligned:
                continue
            line["subtitle_start_ms"] = (
                aligned["subtitle_start_ms"] - shot_start
            )
            line["subtitle_end_ms"] = aligned["subtitle_end_ms"] - shot_start
            line["alignment_hash"] = version["alignment_hash"]
    copied["subtitle_alignment"] = {
        "version_id": version["id"],
        "alignment_hash": version["alignment_hash"],
        "master_audio_hash": version["master_audio_hash"],
    }
    return copied
