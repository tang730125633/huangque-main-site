# -*- coding: utf-8 -*-
"""Duration-driven digital-human workflow (v2).

Paid provider jobs still pass through the existing job, points, refund and
idempotency boundary.  This module freezes a server-authored plan, binds every
child job to one consent record, and performs only the zero-cost local compose.
"""
import base64
import hashlib
import hmac
import io
import json
import os
import pathlib
import re
import secrets
import sqlite3
import stat
import subprocess
import threading
import time
import urllib.parse

from .core import OUT_DIR, closing, jdb
from . import digital_human_oneclick as legacy
from . import digital_human_timeline as timeline


PIPELINE = timeline.PIPELINE
PLAN_PATH = "/api/gen/digital-human-v2/plan"
CONSENT_PATH = "/api/gen/digital-human-v2/consent"
AUDIO_UPLOAD_PATH = "/api/gen/digital-human-v2/audio-upload"
MATERIAL_RESOLVE_PATH = "/api/gen/digital-human-v2/material-resolve"
HISTORY_PATH = "/api/gen/digital-human-v2/history"
CONSENT_VERSION = "digital-human-material-v3"
CONSENT_PURPOSE = "digital_human_material_v3"
CONSENT_TTL_SECONDS = legacy.CONSENT_TTL_SECONDS
DigitalHumanRequestError = legacy.DigitalHumanRequestError
MATERIAL_SOURCE_PRIORITY = ("customer_upload", "local_library", "ai_optional")

_STAGE_KINDS = {
    "material": "image",
    "talking": "video",
    "compose": "script_to_video",
}
_AUDIO_UPLOAD_ID_RE = re.compile(r"^dha_[0-9a-f]{32}$")
_AUDIO_ADMISSION_ID_RE = re.compile(r"^dhaa_[0-9a-f]{32}$")
_AUDIO_MIMES = {
    "audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/wav": ".wav",
    "audio/x-wav": ".wav", "audio/mp4": ".m4a", "audio/aac": ".m4a",
    "audio/x-m4a": ".m4a",
}
_MAX_AUDIO_UPLOAD_BYTES = 30 * 1024 * 1024
_AUDIO_UPLOAD_TTL_SECONDS = 24 * 60 * 60
_AUDIO_ADMISSION_LEASE_SECONDS = 10 * 60
_AUDIO_USER_ACTIVE_LIMIT = 1
_AUDIO_GLOBAL_ACTIVE_LIMIT = 2
_AUDIO_USER_DAILY_COUNT = 8
_AUDIO_IP_DAILY_COUNT = 24
_AUDIO_USER_DAILY_BYTES = 180 * 1024 * 1024
_AUDIO_MANAGED_DISK_BYTES = 2 * 1024 * 1024 * 1024
_AUDIO_TRANSCRIBE_SEM = threading.BoundedSemaphore(_AUDIO_GLOBAL_ACTIVE_LIMIT)
_MATERIAL_ASSET_ID_RE = re.compile(r"^dhm_[0-9a-f]{32}$")
_MATERIAL_ADMISSION_ID_RE = re.compile(r"^dhma_[0-9a-f]{32}$")
_MAX_MATERIAL_BYTES = 20 * 1024 * 1024
_MATERIAL_TTL_SECONDS = 24 * 60 * 60
# The authenticated material-upload route already performs ownership, hash,
# decode and content-security checks under this approval purpose.  Reuse that
# vetted temporary asset; do not upload the customer's bytes to an AI provider.
_CUSTOMER_MATERIAL_UPLOAD_PURPOSE = "smart_montage"
_LOCAL_LIBRARY_DEFAULT_ROOT = "/home/ubuntu/material-libraries/huangque-media"
_LOCAL_LIBRARY_ENV = "DIGITAL_HUMAN_LOCAL_MATERIAL_LIBRARY_ROOT"
_LOCAL_INDEX_MAX_BYTES = 8 * 1024 * 1024
_LOCAL_INDEX_MAX_LINES = 1000
_LOCAL_INDEX_MAX_LINE_BYTES = 64 * 1024
_LOCAL_MATERIAL_MAX_BYTES = 256 * 1024 * 1024
_LOCAL_MEDIA_TYPES = {"图片": "image", "视频": "video", "BGM": "bgm"}
_LOCAL_EXTENSIONS = {
    ".jpg": ("image", "image/jpeg"), ".jpeg": ("image", "image/jpeg"),
    ".png": ("image", "image/png"), ".webp": ("image", "image/webp"),
    ".mp4": ("video", "video/mp4"), ".webm": ("video", "video/webm"),
    ".mov": ("video", "video/quicktime"), ".mp3": ("bgm", "audio/mpeg"),
    ".m4a": ("bgm", "audio/mp4"), ".wav": ("bgm", "audio/wav"),
    ".aac": ("bgm", "audio/aac"), ".flac": ("bgm", "audio/flac"),
    ".ogg": ("bgm", "audio/ogg"),
}
_LOCAL_MIME_TYPES = {
    mime: media_type for media_type, mime in _LOCAL_EXTENSIONS.values()
}
_MATERIAL_MIMES = {
    "image/jpeg": ("image", ".jpg"), "image/png": ("image", ".png"),
    "image/webp": ("image", ".webp"), "video/mp4": ("video", ".mp4"),
    "video/webm": ("video", ".webm"), "video/quicktime": ("video", ".mov"),
}


def _ensure_audio_table(connection):
    connection.execute(
        """CREATE TABLE IF NOT EXISTS digital_human_audio_uploads(
            asset_id TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            run_id TEXT NOT NULL,
            source_sha256 TEXT NOT NULL,
            source_file TEXT NOT NULL,
            duration REAL NOT NULL,
            transcript TEXT NOT NULL,
            slices_json TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            UNIQUE(username, run_id)
        )"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_digital_human_audio_owner "
        "ON digital_human_audio_uploads(username, created_at DESC)"
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS digital_human_material_assets(
            asset_id TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            run_id TEXT NOT NULL,
            plan_digest TEXT NOT NULL,
            item_index INTEGER NOT NULL,
            file TEXT NOT NULL,
            mime TEXT NOT NULL,
            media_type TEXT NOT NULL,
            provider TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            UNIQUE(username, run_id, item_index)
        )"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_digital_human_material_owner "
        "ON digital_human_material_assets(username, run_id, item_index)"
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS digital_human_audio_admissions(
            admission_id TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            client_ip_hash TEXT NOT NULL,
            requested_bytes INTEGER NOT NULL,
            state TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            lease_until INTEGER NOT NULL
        )"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_digital_human_audio_admission_limits "
        "ON digital_human_audio_admissions(state,username,client_ip_hash,created_at)"
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS digital_human_material_admissions(
            admission_id TEXT PRIMARY KEY,
            asset_id TEXT NOT NULL UNIQUE,
            username TEXT NOT NULL,
            run_id TEXT NOT NULL,
            relative_file TEXT NOT NULL,
            state TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            lease_until INTEGER NOT NULL
        )"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_digital_human_material_admission_state "
        "ON digital_human_material_admissions(state,lease_until,created_at)"
    )


def _audio_db(db_factory=None):
    db_factory = db_factory or legacy.cdb
    connection = db_factory()
    _ensure_audio_table(connection)
    connection.commit()
    return connection


def _managed_asset_disk_bytes():
    total = 0
    for name in ("digital_human_audio", "digital_human_materials"):
        root = OUT_DIR / name
        if not root.exists():
            continue
        for directory, subdirs, files in os.walk(str(root), followlinks=False):
            safe_subdirs = []
            for child in subdirs:
                path = pathlib.Path(directory) / child
                if not stat.S_ISLNK(os.lstat(path).st_mode):
                    safe_subdirs.append(child)
            subdirs[:] = safe_subdirs
            for child in files:
                info = os.lstat(pathlib.Path(directory) / child)
                if stat.S_ISREG(info.st_mode):
                    total += int(info.st_size)
    return total


def _audio_asset_id_for_admission(admission_id):
    admission_id = str(admission_id or "").strip().lower()
    if not _AUDIO_ADMISSION_ID_RE.fullmatch(admission_id):
        raise DigitalHumanRequestError(
            "录音上传租约无效，请重新上传", "audio_upload_admission_invalid", 409,
        )
    return "dha_" + admission_id[5:]


def _audio_asset_directory(username, asset_id):
    owner = hashlib.sha256(str(username or "").encode("utf-8")).hexdigest()[:20]
    return OUT_DIR / "digital_human_audio" / owner / _safe_audio_upload_id(asset_id)


def _remove_orphan_audio_directory(directory):
    """Remove one exact managed orphan without following any link.

    The caller has already proved that the derived asset id has no database,
    consent, or job reference.  Any link, special file, identity change, or
    unexpected filename fails closed and leaves the directory for diagnosis.
    """
    directory = pathlib.Path(directory)
    audio_root = OUT_DIR / "digital_human_audio"
    try:
        directory.relative_to(audio_root)
        if not os.path.lexists(directory):
            return True
        for parent in (OUT_DIR, audio_root, directory.parent, directory):
            info = os.lstat(parent)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                return False
        directories = []
        files = []
        for current, subdirs, names in os.walk(str(directory), followlinks=False):
            current_path = pathlib.Path(current)
            current_info = os.lstat(current_path)
            if stat.S_ISLNK(current_info.st_mode) or not stat.S_ISDIR(current_info.st_mode):
                return False
            directories.append((current_path, current_info.st_dev, current_info.st_ino))
            for name in subdirs:
                child = current_path / name
                info = os.lstat(child)
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                    return False
            for name in names:
                child = current_path / name
                info = os.lstat(child)
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                    return False
                if not (name.startswith("source.") or re.fullmatch(r"slice_[0-9]{2}\.m4a", name)):
                    return False
                files.append((child, info.st_dev, info.st_ino))
        for path, device, inode in files:
            info = os.lstat(path)
            if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
                    or (info.st_dev, info.st_ino) != (device, inode)):
                return False
        for path, _device, _inode in files:
            path.unlink()
        for path, device, inode in reversed(directories):
            info = os.lstat(path)
            if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
                    or (info.st_dev, info.st_ino) != (device, inode)):
                return False
            path.rmdir()
        try:
            directory.parent.rmdir()
        except OSError:
            pass
        return True
    except (OSError, ValueError):
        return False


def _audio_asset_is_referenced(asset_id, db_factory=None, jobs_db_factory=None,
                               now=None):
    now = int(time.time() if now is None else now)
    jobs_db_factory = jobs_db_factory or jdb
    with closing(_audio_db(db_factory)) as connection:
        if connection.execute(
            "SELECT 1 FROM digital_human_audio_uploads WHERE asset_id=? LIMIT 1",
            (asset_id,),
        ).fetchone():
            return True
        consent_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='digital_human_consents'",
        ).fetchone()
        consent_columns = ({row[1] for row in connection.execute(
            "PRAGMA table_info(digital_human_consents)",
        ).fetchall()} if consent_table else set())
        if {"voice_ref", "expires_at"}.issubset(consent_columns) and connection.execute(
            "SELECT 1 FROM digital_human_consents "
            "WHERE voice_ref=? AND expires_at>? LIMIT 1",
            (asset_id, now),
        ).fetchone():
            return True
    with closing(jobs_db_factory()) as connection:
        return bool(connection.execute(
            "SELECT 1 FROM jobs WHERE payload LIKE ? OR result LIKE ? LIMIT 1",
            ("%" + asset_id + "%", "%" + asset_id + "%"),
        ).fetchone())


def _reap_expired_audio_admissions(db_factory=None, jobs_db_factory=None,
                                   now=None, limit=20):
    """Claim and recover expired upload directories in a bounded pass."""
    now = int(time.time() if now is None else now)
    limit = max(1, min(100, int(limit or 20)))
    with closing(_audio_db(db_factory)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE digital_human_audio_admissions SET state='reaping' "
            "WHERE state='active' AND lease_until<=?", (now,),
        )
        connection.execute(
            "UPDATE digital_human_audio_admissions SET state='reaping_committed' "
            "WHERE state='committed' AND NOT EXISTS ("
            "SELECT 1 FROM digital_human_audio_uploads AS upload "
            "WHERE upload.asset_id='dha_' || substr(admission_id,6))",
        )
        rows = connection.execute(
            "SELECT admission_id,username,state FROM digital_human_audio_admissions "
            "WHERE state IN ('reaping','reaping_committed') "
            "ORDER BY created_at LIMIT ?", (limit,),
        ).fetchall()
        connection.commit()
    removed = 0
    for row in rows:
        asset_id = _audio_asset_id_for_admission(row["admission_id"])
        directory = _audio_asset_directory(row["username"], asset_id)
        if (os.path.lexists(directory)
                and _audio_asset_is_referenced(
                    asset_id, db_factory, jobs_db_factory, now=now,
                )):
            with closing(_audio_db(db_factory)) as connection:
                connection.execute(
                    "UPDATE digital_human_audio_admissions "
                    "SET state='committed',lease_until=0 "
                    "WHERE admission_id=? AND state IN ('reaping','reaping_committed')",
                    (row["admission_id"],),
                )
                connection.commit()
            continue
        if not _remove_orphan_audio_directory(directory):
            continue
        with closing(_audio_db(db_factory)) as connection:
            if row["state"] == "reaping_committed":
                completed = connection.execute(
                    "UPDATE digital_human_audio_admissions "
                    "SET state='committed_reaped',lease_until=0 "
                    "WHERE admission_id=? AND state='reaping_committed'",
                    (row["admission_id"],),
                )
            else:
                completed = connection.execute(
                    "DELETE FROM digital_human_audio_admissions "
                    "WHERE admission_id=? AND state='reaping'", (row["admission_id"],),
                )
            connection.commit()
        removed += int(completed.rowcount == 1)
    return removed


def _begin_audio_admission(username, client_ip, requested_bytes,
                           db_factory=None, now=None, jobs_db_factory=None):
    now = int(time.time() if now is None else now)
    username = str(username or "").strip()
    ip_hash = hashlib.sha256(str(client_ip or "unknown").encode("utf-8")).hexdigest()
    admission_id = "dhaa_" + secrets.token_hex(16)
    day_start = now - (now % 86400)
    _reap_expired_audio_admissions(
        db_factory, jobs_db_factory, now=now, limit=_AUDIO_GLOBAL_ACTIVE_LIMIT,
    )
    with closing(_audio_db(db_factory)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        active_user = connection.execute(
            "SELECT COUNT(*) FROM digital_human_audio_admissions "
            "WHERE state='active' AND username=?", (username,),
        ).fetchone()[0]
        active_global = connection.execute(
            "SELECT COUNT(*) FROM digital_human_audio_admissions WHERE state='active'"
        ).fetchone()[0]
        reserved_global = connection.execute(
            "SELECT COALESCE(SUM(requested_bytes),0) "
            "FROM digital_human_audio_admissions WHERE state='active'"
        ).fetchone()[0]
        daily = connection.execute(
            "SELECT COUNT(*),COALESCE(SUM(requested_bytes),0) "
            "FROM digital_human_audio_admissions WHERE username=? AND created_at>=?",
            (username, day_start),
        ).fetchone()
        ip_daily = connection.execute(
            "SELECT COUNT(*) FROM digital_human_audio_admissions "
            "WHERE client_ip_hash=? AND created_at>=?", (ip_hash, day_start),
        ).fetchone()[0]
        if (int(active_user) >= _AUDIO_USER_ACTIVE_LIMIT
                or int(active_global) >= _AUDIO_GLOBAL_ACTIVE_LIMIT):
            connection.rollback()
            raise DigitalHumanRequestError(
                "录音转写任务繁忙，请稍后再试", "audio_upload_concurrency_limit", 429,
            )
        if (int(daily[0]) >= _AUDIO_USER_DAILY_COUNT
                or int(daily[1]) + requested_bytes > _AUDIO_USER_DAILY_BYTES
                or int(ip_daily) >= _AUDIO_IP_DAILY_COUNT):
            connection.rollback()
            raise DigitalHumanRequestError(
                "今日录音上传额度已用完", "audio_upload_daily_limit", 429,
            )
        if (_managed_asset_disk_bytes() + int(reserved_global) + requested_bytes
                > _AUDIO_MANAGED_DISK_BYTES):
            connection.rollback()
            raise DigitalHumanRequestError(
                "录音临时空间不足，请稍后再试", "audio_upload_disk_limit", 429,
            )
        connection.execute(
            "INSERT INTO digital_human_audio_admissions VALUES(?,?,?,?,?,?,?)",
            (admission_id, username, ip_hash, int(requested_bytes), "active", now,
             now + _AUDIO_ADMISSION_LEASE_SECONDS),
        )
        connection.commit()
    return admission_id


def _renew_audio_admission(admission_id, db_factory=None, now=None):
    now = int(time.time() if now is None else now)
    with closing(_audio_db(db_factory)) as connection:
        renewed = connection.execute(
            "UPDATE digital_human_audio_admissions SET lease_until=? "
            "WHERE admission_id=? AND state='active'",
            (now + _AUDIO_ADMISSION_LEASE_SECONDS, admission_id),
        )
        connection.commit()
    if renewed.rowcount != 1:
        raise DigitalHumanRequestError(
            "录音上传租约已失效，请重新上传", "audio_upload_admission_expired", 409,
        )


class _AudioAdmissionHeartbeat:
    def __init__(self, admission_id, db_factory=None, interval=None):
        self.admission_id = admission_id
        self.db_factory = db_factory
        self.interval = float(interval or max(1, _AUDIO_ADMISSION_LEASE_SECONDS // 3))
        self.stop_event = threading.Event()
        self.error = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def _run(self):
        while not self.stop_event.wait(self.interval):
            try:
                _renew_audio_admission(self.admission_id, self.db_factory)
            except Exception as error:
                self.error = error
                return

    def check(self):
        if self.error is not None:
            raise self.error

    def close(self):
        self.stop_event.set()
        self.thread.join(timeout=max(1.0, self.interval + 1.0))


def _finish_audio_admission(admission_id, committed, db_factory=None):
    with closing(_audio_db(db_factory)) as connection:
        if committed:
            connection.execute(
                "UPDATE digital_human_audio_admissions SET state='committed',lease_until=0 "
                "WHERE admission_id=? AND state='active'", (admission_id,),
            )
        else:
            connection.execute(
                "DELETE FROM digital_human_audio_admissions "
                "WHERE admission_id=? AND state='active'", (admission_id,),
            )
        connection.commit()


def _safe_audio_upload_id(value):
    value = str(value or "").strip().lower()
    if not _AUDIO_UPLOAD_ID_RE.fullmatch(value):
        raise DigitalHumanRequestError("录音上传记录无效，请重新上传", "audio_upload_invalid", 409)
    return value


def _audio_run_has_consent(connection, username, run_id, now=None):
    now = int(time.time() if now is None else now)
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='digital_human_consents'"
    ).fetchone()
    if not table:
        return False
    columns = {row[1] for row in connection.execute(
        "PRAGMA table_info(digital_human_consents)",
    ).fetchall()}
    if "expires_at" not in columns:
        return False
    return connection.execute(
        "SELECT 1 FROM digital_human_consents "
        "WHERE username=? AND run_id=? AND expires_at>? LIMIT 1",
        (username, run_id, now),
    ).fetchone() is not None


def _remove_audio_asset_files(source_file):
    import shutil

    root = (OUT_DIR / "digital_human_audio").resolve()
    source = (OUT_DIR / str(source_file or "")).resolve()
    if source.parent == root or root not in source.parents:
        return
    shutil.rmtree(str(source.parent), ignore_errors=True)


def _load_audio_asset(asset_id, username, now=None, db_factory=None):
    asset_id = _safe_audio_upload_id(asset_id)
    now = int(time.time() if now is None else now)
    with closing(_audio_db(db_factory)) as connection:
        row = connection.execute(
            "SELECT * FROM digital_human_audio_uploads WHERE asset_id=? AND username=?",
            (asset_id, str(username or "").strip()),
        ).fetchone()
    if not row:
        raise DigitalHumanRequestError(
            "录音不存在或不属于当前账号，请重新上传", "audio_upload_invalid", 409,
        )
    asset = dict(row)
    if int(asset["expires_at"]) <= now:
        raise DigitalHumanRequestError("录音已过期，请重新上传", "audio_upload_expired", 409)
    try:
        asset["slices"] = json.loads(asset.pop("slices_json"))
    except Exception as exc:
        raise DigitalHumanRequestError(
            "录音切段记录损坏，请重新上传", "audio_upload_invalid", 409,
        ) from exc
    if not isinstance(asset["slices"], list) or not asset["slices"]:
        raise DigitalHumanRequestError("录音切段记录无效，请重新上传", "audio_upload_invalid", 409)
    return asset


def _probe_audio_duration(path):
    process = subprocess.run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ], check=False, timeout=60, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        duration = float((process.stdout or b"").decode("ascii", "replace").strip())
    except (TypeError, ValueError):
        duration = 0.0
    if process.returncode != 0 or duration <= 0:
        raise DigitalHumanRequestError("无法读取录音时长，请更换音频", "audio_probe_failed")
    if duration < timeline.MIN_AUDIO_SECONDS:
        raise DigitalHumanRequestError("完整录音不能少于 6 秒", "audio_duration_invalid")
    if duration > timeline.MAX_DURATION_SECONDS:
        raise DigitalHumanRequestError("完整录音不能超过 180 秒", "audio_duration_invalid")
    return round(duration, 3)


def _transcribe_audio(path):
    try:
        from . import video as video_domain
        video_domain.subtitle_runtime_preflight()
        model = video_domain._get_whisper_model()
        with video_domain._whisper_sem:
            result, _info = model.transcribe(
                str(path), beam_size=1, vad_filter=True, language="zh",
            )
            segments = [{
                "start": max(0.0, float(item.start)),
                "end": max(0.0, float(item.end)),
                "text": str(item.text or "").strip(),
            } for item in result if str(item.text or "").strip()]
    except DigitalHumanRequestError:
        raise
    except Exception as exc:
        raise DigitalHumanRequestError(
            "录音转写失败，请稍后重试", "audio_transcribe_failed", 503,
        ) from exc
    if not segments:
        raise DigitalHumanRequestError("录音中没有识别到有效口播", "audio_transcript_empty")
    return segments


def _slice_intervals(transcript_segments, duration):
    intervals = []
    start = 0.0
    while duration - start > timeline.MAX_APPEARANCE_INTERVAL:
        candidates = [float(item["end"]) for item in transcript_segments
                      if start + timeline.MIN_APPEARANCE_INTERVAL <= float(item["end"])
                      <= start + timeline.MAX_APPEARANCE_INTERVAL]
        cut = (min(candidates, key=lambda value: abs(
            value - (start + timeline.TARGET_APPEARANCE_INTERVAL)
        )) if candidates else min(duration, start + timeline.TARGET_APPEARANCE_INTERVAL))
        intervals.append((round(start, 3), round(cut, 3)))
        start = cut
    intervals.append((round(start, 3), round(duration, 3)))
    if len(intervals) > 1 and intervals[-1][1] - intervals[-1][0] < 8.0:
        previous = intervals[-2]
        intervals[-2:] = [(previous[0], intervals[-1][1])]
    return intervals


def _slice_text(transcript_segments, start, end):
    text = "".join(item["text"] for item in transcript_segments
                   if float(item["end"]) > start and float(item["start"]) < end)
    return re.sub(r"\s+", " ", text).strip()


def store_audio_upload(stream, length, username, run_id, content_type,
                       claimed_sha256, db_factory=None, client_ip="",
                       jobs_db_factory=None):
    run_id = str(run_id or "").strip()
    if not legacy._RUN_ID_RE.fullmatch(run_id):
        raise DigitalHumanRequestError("本次制作流程编号无效，请重新开始")
    if type(length) is not int or length <= 0 or length > _MAX_AUDIO_UPLOAD_BYTES:
        raise DigitalHumanRequestError("录音文件必须小于 30MB", "audio_upload_size_invalid")
    extension = _AUDIO_MIMES.get(str(content_type or "").split(";", 1)[0].strip().lower())
    if not extension:
        raise DigitalHumanRequestError("仅支持 MP3、WAV、M4A 或 AAC 录音", "audio_upload_type_invalid")
    claimed = legacy._required_sha256(claimed_sha256, "完整录音")
    username = str(username or "").strip()
    now = int(time.time())
    expired = None
    with closing(_audio_db(db_factory)) as connection:
        existing = connection.execute(
            "SELECT asset_id,source_sha256,source_file,expires_at "
            "FROM digital_human_audio_uploads WHERE username=? AND run_id=?",
            (username, run_id),
        ).fetchone()
        if existing and int(existing["expires_at"]) <= now:
            if _audio_run_has_consent(connection, username, run_id):
                raise DigitalHumanRequestError(
                    "录音已过期，且本次流程已经授权；请先放弃上次任务并重新设置，再上传录音",
                    "audio_upload_restart_required", 409,
                )
            expired = dict(existing)
    if existing and expired is None:
        if not hmac.compare_digest(str(existing["source_sha256"]), claimed):
            raise DigitalHumanRequestError(
                "同一制作流程不能更换完整录音，请重新开始",
                "audio_upload_binding_conflict", 409,
            )
        return _load_audio_asset(existing["asset_id"], username, db_factory=db_factory)
    admission_id = _begin_audio_admission(
        username, client_ip, length, db_factory=db_factory,
        jobs_db_factory=jobs_db_factory,
    )
    if not _AUDIO_TRANSCRIBE_SEM.acquire(blocking=False):
        _finish_audio_admission(admission_id, False, db_factory)
        raise DigitalHumanRequestError(
            "录音转写任务繁忙，请稍后再试", "audio_upload_concurrency_limit", 429,
        )
    asset_id = _audio_asset_id_for_admission(admission_id)
    directory = _audio_asset_directory(username, asset_id)
    source = directory / ("source" + extension)
    digest = hashlib.sha256()
    remaining = length
    asset_committed = False
    heartbeat = _AudioAdmissionHeartbeat(admission_id, db_factory).start()
    try:
        directory.mkdir(parents=True, exist_ok=False)
        with source.open("wb") as output:
            while remaining:
                heartbeat.check()
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise DigitalHumanRequestError("录音上传不完整，请重新上传", "audio_upload_incomplete")
                output.write(chunk)
                digest.update(chunk)
                remaining -= len(chunk)
        heartbeat.check()
        if not hmac.compare_digest(digest.hexdigest(), claimed):
            raise DigitalHumanRequestError("录音校验失败，请重新上传", "audio_upload_digest_mismatch")
        duration = _probe_audio_duration(source)
        heartbeat.check()
        transcript_segments = _transcribe_audio(source)
        heartbeat.check()
        slices = []
        for index, (start, end) in enumerate(_slice_intervals(transcript_segments, duration)):
            target = directory / ("slice_%02d.m4a" % index)
            legacy._run([
                "ffmpeg", "-y", "-ss", "%.3f" % start, "-to", "%.3f" % end,
                "-i", str(source), "-vn", "-c:a", "aac", "-b:a", "192k",
                "-ar", "48000", "-ac", "2", str(target),
            ], timeout=180)
            heartbeat.check()
            raw = target.read_bytes()
            slices.append({
                "index": index, "start": start, "end": end,
                "duration": round(end - start, 3),
                "text": _slice_text(transcript_segments, start, end),
                "file": target.resolve().relative_to(OUT_DIR.resolve()).as_posix(),
                "sha256": hashlib.sha256(raw).hexdigest(),
            })
        transcript = "".join(item["text"] for item in transcript_segments).strip()
        if len(transcript) < 4:
            raise DigitalHumanRequestError("录音中没有识别到足够的口播内容", "audio_transcript_empty")
        now = int(time.time())
        try:
            with closing(_audio_db(db_factory)) as connection:
                connection.execute("BEGIN IMMEDIATE")
                renewed = connection.execute(
                    "UPDATE digital_human_audio_admissions SET lease_until=? "
                    "WHERE admission_id=? AND state='active'",
                    (int(time.time()) + _AUDIO_ADMISSION_LEASE_SECONDS, admission_id),
                )
                if renewed.rowcount != 1:
                    raise DigitalHumanRequestError(
                        "录音上传租约已失效，请重新上传",
                        "audio_upload_admission_expired", 409,
                    )
                values = (
                    asset_id, claimed,
                    source.resolve().relative_to(OUT_DIR.resolve()).as_posix(), duration,
                    transcript, json.dumps(slices, ensure_ascii=False), now,
                    now + _AUDIO_UPLOAD_TTL_SECONDS,
                )
                if expired is not None:
                    updated = connection.execute(
                        """UPDATE digital_human_audio_uploads SET
                            asset_id=?,source_sha256=?,source_file=?,duration=?,
                            transcript=?,slices_json=?,created_at=?,expires_at=?
                           WHERE asset_id=? AND username=? AND run_id=? AND expires_at<=?""",
                        values + (expired["asset_id"], username, run_id, now),
                    )
                    if updated.rowcount != 1:
                        raise sqlite3.IntegrityError("expired audio upload was replaced concurrently")
                else:
                    connection.execute(
                        """INSERT INTO digital_human_audio_uploads(
                            asset_id,username,run_id,source_sha256,source_file,duration,
                            transcript,slices_json,created_at,expires_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        (asset_id, username, run_id) + values[1:],
                    )
                connection.commit()
            if expired is not None:
                _remove_audio_asset_files(expired["source_file"])
            asset_committed = True
        except sqlite3.IntegrityError:
            # A browser retry can race the first upload. Reuse the committed,
            # owner-bound asset instead of returning a transient server error.
            with closing(_audio_db(db_factory)) as connection:
                winner = connection.execute(
                    "SELECT asset_id,source_sha256 FROM digital_human_audio_uploads "
                    "WHERE username=? AND run_id=?",
                    (username, run_id),
                ).fetchone()
            if winner and hmac.compare_digest(str(winner["source_sha256"]), claimed):
                import shutil
                shutil.rmtree(str(directory), ignore_errors=True)
                result = _load_audio_asset(
                    winner["asset_id"], username, db_factory=db_factory,
                )
                asset_committed = True
                _finish_audio_admission(admission_id, True, db_factory)
                return result
            raise DigitalHumanRequestError(
                "同一制作流程不能更换完整录音，请重新开始",
                "audio_upload_binding_conflict", 409,
            )
        result = _load_audio_asset(asset_id, username, db_factory=db_factory)
        heartbeat.close()
        _finish_audio_admission(admission_id, True, db_factory)
        return result
    except Exception:
        # Leave only committed, owner-bound uploads.  Uncommitted provider input
        # is safe to remove because the browser still owns the source file.
        if not asset_committed:
            import shutil
            shutil.rmtree(str(directory), ignore_errors=True)
            _finish_audio_admission(admission_id, False, db_factory)
        raise
    finally:
        heartbeat.close()
        _AUDIO_TRANSCRIBE_SEM.release()


def audio_upload_response(stream, length, username, run_id, content_type,
                          claimed_sha256, db_factory=None, client_ip=""):
    asset = store_audio_upload(
        stream, length, username, run_id, content_type, claimed_sha256,
        db_factory=db_factory, client_ip=client_ip,
    )
    return {
        "ok": True, "audio_upload_id": asset["asset_id"],
        "duration": round(float(asset["duration"]), 3),
        "transcript": asset["transcript"], "slice_count": len(asset["slices"]),
        "expires_at": int(asset["expires_at"]), "source_sha256": asset["source_sha256"],
    }


def _safe_managed_file(relative, managed_root):
    try:
        root = (OUT_DIR / managed_root).resolve(strict=True)
        candidate = OUT_DIR / str(relative or "")
        current = OUT_DIR.resolve(strict=True)
        for component in candidate.relative_to(OUT_DIR).parts:
            current = current / component
            info = os.lstat(current)
            if stat.S_ISLNK(info.st_mode):
                return None
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
        if not stat.S_ISREG(os.lstat(resolved).st_mode):
            return None
        return resolved
    except (OSError, ValueError):
        return None


def cleanup_expired_assets(db_factory=None, jobs_db_factory=None, now=None,
                           limit=50):
    """Delete only expired, unreferenced managed assets in a bounded pass."""
    now = int(time.time() if now is None else now)
    limit = max(1, min(200, int(limit or 50)))
    jobs_db_factory = jobs_db_factory or jdb
    removed = _reap_expired_audio_admissions(
        db_factory, jobs_db_factory, now=now, limit=limit,
    )
    removed += _reap_expired_material_admissions(
        db_factory, jobs_db_factory, now=now, limit=limit,
    )
    with closing(_audio_db(db_factory)) as connection:
        connection.execute(
            "DELETE FROM digital_human_audio_admissions "
            "WHERE state='committed_reaped' AND created_at<? "
            "AND NOT EXISTS (SELECT 1 FROM digital_human_audio_uploads AS upload "
            "WHERE upload.asset_id='dha_' || substr(admission_id,6))",
            (now - 2 * 86400,),
        )
        candidates = []
        for table, column, root in (
            ("digital_human_audio_uploads", "source_file", "digital_human_audio"),
            ("digital_human_material_assets", "file", "digital_human_materials"),
        ):
            rows = connection.execute(
                "SELECT * FROM %s WHERE expires_at<=? ORDER BY expires_at LIMIT ?" % table,
                (now, limit - len(candidates)),
            ).fetchall()
            candidates.extend((table, column, root, dict(row)) for row in rows)
            if len(candidates) >= limit:
                break
        connection.commit()
    for table, column, root, row in candidates:
        with closing(_audio_db(db_factory)) as connection:
            if _audio_run_has_consent(
                    connection, row["username"], row["run_id"], now=now):
                continue
        asset_id = row["asset_id"]
        with closing(jobs_db_factory()) as connection:
            referenced = connection.execute(
                "SELECT 1 FROM jobs WHERE username=? AND (payload LIKE ? OR result LIKE ?) LIMIT 1",
                (row["username"], "%" + asset_id + "%", "%" + asset_id + "%"),
            ).fetchone()
        if referenced:
            continue
        path = _safe_managed_file(row[column], root)
        if path is None:
            continue
        try:
            path.unlink()
            path.parent.rmdir()
        except OSError:
            if path.exists():
                continue
        with closing(_audio_db(db_factory)) as connection:
            deleted = connection.execute(
                "DELETE FROM %s WHERE asset_id=? AND expires_at<=?" % table,
                (asset_id, now),
            )
            if table == "digital_human_material_assets" and deleted.rowcount == 1:
                connection.execute(
                    "DELETE FROM digital_human_material_admissions WHERE asset_id=?",
                    (asset_id,),
                )
            connection.commit()
        removed += int(deleted.rowcount == 1)
    return removed


def _keywords(text):
    compact = re.sub(r"\s+", "", str(text or "").lower())
    latin = re.findall(r"[a-z0-9]{2,}", compact)
    cjk = "".join(re.findall(r"[\u4e00-\u9fff]", compact))
    return set(latin + [cjk[index:index + 2] for index in range(max(0, len(cjk) - 1))])


def _local_library_root():
    value = str(os.environ.get(
        _LOCAL_LIBRARY_ENV, _LOCAL_LIBRARY_DEFAULT_ROOT,
    ) or "").strip()
    root = os.path.abspath(value)
    if not value or not os.path.isabs(value) or root != value.rstrip(os.sep):
        raise ValueError("本地素材库根目录配置无效")
    return root


def _lstat_real_chain(path):
    absolute = os.path.abspath(path)
    target = pathlib.Path(absolute)
    chain = list(reversed(target.parents)) + [target]
    for current in chain:
        if str(current) == current.anchor:
            continue
        info = os.lstat(current)
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("本地素材库路径包含软链接")
        if current != target and not stat.S_ISDIR(info.st_mode):
            raise ValueError("本地素材库父路径不是目录")
    return os.lstat(absolute)


def _library_relative(value):
    text = str(value or "").replace("\\", "/")
    parts = text.split("/")
    if (not text or text.startswith("/") or parts[0] != "files"
            or len(parts) < 2 or any(part in {"", ".", ".."} for part in parts)
            or any(part.startswith(".") for part in parts[1:])):
        raise ValueError("本地素材索引路径无效")
    return "/".join(parts)


def _open_local_regular(root, relative, maximum):
    path = os.path.join(root, *relative.split("/"))
    try:
        path_info = _lstat_real_chain(path)
    except (OSError, ValueError) as exc:
        raise ValueError("本地素材文件路径不安全") from exc
    if not stat.S_ISREG(path_info.st_mode):
        raise ValueError("本地素材文件不是普通文件")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    opened = os.fstat(descriptor)
    identity = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
    listed = (path_info.st_dev, path_info.st_ino, path_info.st_size, path_info.st_mtime_ns)
    if identity != listed or opened.st_size <= 0 or opened.st_size > maximum:
        os.close(descriptor)
        raise ValueError("本地素材文件身份或体积无效")
    return descriptor, path, identity


def _read_descriptor(descriptor, maximum):
    chunks = []
    total = 0
    while True:
        chunk = os.read(descriptor, min(1024 * 1024, maximum + 1 - total))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > maximum:
            raise ValueError("本地素材文件超过体积上限")


def _actual_media_mime(raw, extension):
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        return "image/webp"
    if raw.startswith(b"\x1aE\xdf\xa3"):
        return "video/webm"
    if len(raw) >= 12 and raw[4:8] == b"ftyp":
        return "audio/mp4" if extension == ".m4a" else (
            "video/quicktime" if extension == ".mov" else "video/mp4")
    if raw.startswith(b"RIFF") and raw[8:12] == b"WAVE":
        return "audio/wav"
    if raw.startswith(b"fLaC"):
        return "audio/flac"
    if raw.startswith(b"OggS"):
        return "audio/ogg"
    if raw.startswith(b"ID3") or (len(raw) >= 2 and raw[0] == 0xff and raw[1] & 0xe0 == 0xe0):
        return "audio/aac" if extension == ".aac" else "audio/mpeg"
    return ""


def _load_local_catalog(expected_count=None):
    root = _local_library_root()
    root_info = _lstat_real_chain(root)
    files_info = _lstat_real_chain(os.path.join(root, "files"))
    if not stat.S_ISDIR(root_info.st_mode) or not stat.S_ISDIR(files_info.st_mode):
        raise ValueError("本地素材库目录不可用")
    descriptor, _, identity = _open_local_regular(
        root, "index.jsonl", _LOCAL_INDEX_MAX_BYTES,
    )
    records = []
    ids = set()
    paths = set()
    try:
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            for line_number in range(1, _LOCAL_INDEX_MAX_LINES + 2):
                line = source.readline(_LOCAL_INDEX_MAX_LINE_BYTES + 1)
                if not line:
                    break
                if len(line) > _LOCAL_INDEX_MAX_LINE_BYTES:
                    raise ValueError("本地素材索引单行超过上限")
                if line_number > _LOCAL_INDEX_MAX_LINES:
                    raise ValueError("本地素材索引行数超过上限")
                try:
                    record = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ValueError("本地素材索引格式无效") from exc
                if not isinstance(record, dict) or record.get("状态") != "可使用":
                    raise ValueError("本地素材索引包含不可用记录")
                relative = _library_relative(record.get("server_relative_path"))
                digest = str(record.get("SHA256") or "").strip().lower()
                media_type = _LOCAL_MEDIA_TYPES.get(record.get("素材类型"))
                extension = os.path.splitext(relative)[1].lower()
                expected = _LOCAL_EXTENSIONS.get(extension)
                if (not re.fullmatch(r"[0-9a-f]{64}", digest)
                        or expected is None or expected[0] != media_type):
                    raise ValueError("本地素材索引类型、扩展名或哈希无效")
                indexed_mime = str(
                    record.get("MIME") or record.get("mime_type")
                    or record.get("mime") or expected[1]
                ).strip().lower()
                if _LOCAL_MIME_TYPES.get(indexed_mime) != media_type:
                    raise ValueError("本地素材索引 MIME 与素材大类不一致")
                opaque_id = "local_" + hashlib.sha256(
                    (relative + "\0" + digest).encode("utf-8")
                ).hexdigest()[:32]
                if opaque_id in ids or relative in paths:
                    raise ValueError("本地素材索引包含重复 ID 或路径")
                material_info = _lstat_real_chain(
                    os.path.join(root, *relative.split("/"))
                )
                if not stat.S_ISREG(material_info.st_mode):
                    raise ValueError("本地素材索引目标不是普通文件")
                ids.add(opaque_id)
                paths.add(relative)
                records.append({
                    "id": opaque_id, "relative": relative, "sha256": digest,
                    "media_type": media_type, "mime": indexed_mime,
                    "search": " ".join(str(record.get(key) or "") for key in (
                        "素材名称", "一级场景", "二级场景", "标签", "使用环节",
                    )),
                })
    finally:
        after = os.fstat(descriptor)
        os.close(descriptor)
    if identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError("本地素材索引读取期间发生变化")
    if expected_count is not None and len(records) != int(expected_count):
        raise ValueError("本地素材索引条目数与发布合同不一致")
    return root, records


def _read_local_record(root, record):
    descriptor, path, identity = _open_local_regular(
        root, record["relative"], _LOCAL_MATERIAL_MAX_BYTES,
    )
    try:
        raw = _read_descriptor(descriptor, _LOCAL_MATERIAL_MAX_BYTES)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    current = os.lstat(path)
    final_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    path_identity = (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
    if identity != final_identity or final_identity != path_identity:
        raise ValueError("本地素材文件读取期间发生变化")
    if not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), record["sha256"]):
        raise ValueError("本地素材文件 SHA-256 与索引不一致")
    extension = os.path.splitext(record["relative"])[1].lower()
    actual_mime = _actual_media_mime(raw[:64], extension)
    if _LOCAL_MIME_TYPES.get(actual_mime) != record["media_type"]:
        raise ValueError("本地素材文件真实 MIME 与索引素材大类不一致")
    return raw, actual_mime


def _local_library_material(query, preferred_type):
    root, records = _load_local_catalog()
    candidates = [item for item in records if item["media_type"] in {"image", "video"}]
    query_words = _keywords(query)
    candidates.sort(key=lambda item: (
        item["media_type"] == preferred_type,
        len(query_words & _keywords(item["search"])), item["id"],
    ), reverse=True)
    if not candidates:
        return None
    raw, mime = _read_local_record(root, candidates[0])
    return raw, mime, "local_library"


def local_material_library_operational_probe(expected_count=318, verify_all=False):
    root, records = _load_local_catalog(expected_count=expected_count)
    counts = {"image": 0, "video": 0, "bgm": 0}
    samples = {}
    for record in records:
        counts[record["media_type"]] += 1
        samples.setdefault(record["media_type"], record)
    if any(counts[kind] <= 0 for kind in counts):
        raise ValueError("本地素材库缺少图片、视频或 BGM")
    verified = records if verify_all else samples.values()
    for record in verified:
        _read_local_record(root, record)
    return {
        "ok": True, "count": len(records), "types": counts,
        "verified_files": len(records) if verify_all else len(samples),
    }


def _customer_material(upload_id, username):
    from . import cli_uploads

    try:
        raw, metadata = cli_uploads.read_image_bytes(upload_id, username)
    except ValueError as exc:
        raise DigitalHumanRequestError(
            str(exc), "customer_material_unavailable", 409,
        ) from exc
    if str(metadata.get("approved_for") or "") != _CUSTOMER_MATERIAL_UPLOAD_PURPOSE:
        raise DigitalHumanRequestError(
            "顾客上传素材未通过当前素材入口校验，请重新上传",
            "customer_material_unapproved", 409,
        )
    mime = str(metadata.get("mime") or "").lower()
    if mime not in _MATERIAL_MIMES or _MATERIAL_MIMES[mime][0] != "image":
        raise DigitalHumanRequestError(
            "顾客上传素材格式不受支持，请重新上传 PNG、JPEG 或 WebP",
            "customer_material_invalid", 409,
        )
    return raw, mime, "customer_upload"


def _material_asset_location(username, run_id, asset_id, extension):
    if (not _MATERIAL_ASSET_ID_RE.fullmatch(str(asset_id or ""))
            or extension not in {value[1] for value in _MATERIAL_MIMES.values()}):
        raise ValueError("素材托管路径无效")
    owner = hashlib.sha256(str(username).encode("utf-8")).hexdigest()[:20]
    run_hash = hashlib.sha256(str(run_id).encode("utf-8")).hexdigest()[:20]
    relative = pathlib.PurePosixPath(
        "digital_human_materials", owner, run_hash, asset_id + extension,
    ).as_posix()
    return OUT_DIR / pathlib.PurePosixPath(relative), relative


def _begin_material_admission(username, run_id, extension, db_factory=None,
                              now=None, jobs_db_factory=None):
    now = int(time.time() if now is None else now)
    _reap_expired_material_admissions(
        db_factory, jobs_db_factory, now=now, limit=20,
    )
    admission_id = "dhma_" + secrets.token_hex(16)
    asset_id = "dhm_" + admission_id[5:]
    target, relative = _material_asset_location(
        username, run_id, asset_id, extension,
    )
    with closing(_audio_db(db_factory)) as connection:
        connection.execute(
            """INSERT INTO digital_human_material_admissions(
                admission_id,asset_id,username,run_id,relative_file,state,
                created_at,lease_until
            ) VALUES(?,?,?,?,?,'active',?,?)""",
            (admission_id, asset_id, username, run_id, relative, now,
             now + _AUDIO_ADMISSION_LEASE_SECONDS),
        )
        connection.commit()
    return admission_id, asset_id, target, relative


def _remove_orphan_material_file(row):
    try:
        asset_id = str(row["asset_id"] or "")
        relative = str(row["relative_file"] or "")
        suffix = pathlib.PurePosixPath(relative).suffix.lower()
        target, expected = _material_asset_location(
            row["username"], row["run_id"], asset_id, suffix,
        )
        if relative != expected:
            return False
        root = OUT_DIR / "digital_human_materials"
        if not os.path.lexists(target):
            return True
        for parent in (OUT_DIR, root, target.parent.parent, target.parent):
            info = os.lstat(parent)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                return False
        before = os.lstat(target)
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            return False
        current = os.lstat(target)
        if ((current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
                or stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode)):
            return False
        target.unlink()
        for directory in (target.parent, target.parent.parent):
            try:
                directory.rmdir()
            except OSError:
                break
        return True
    except (KeyError, OSError, ValueError):
        return False


def _reap_expired_material_admissions(db_factory=None, jobs_db_factory=None,
                                      now=None, limit=20):
    """Recover material writes that ended before their asset transaction."""
    now = int(time.time() if now is None else now)
    limit = max(1, min(100, int(limit or 20)))
    with closing(_audio_db(db_factory)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE digital_human_material_admissions SET state='reaping' "
            "WHERE state='active' AND lease_until<=?", (now,),
        )
        rows = connection.execute(
            "SELECT * FROM digital_human_material_admissions "
            "WHERE state='reaping' ORDER BY created_at LIMIT ?", (limit,),
        ).fetchall()
        connection.commit()
    removed = 0
    for original in rows:
        row = dict(original)
        with closing(_audio_db(db_factory)) as connection:
            winner = connection.execute(
                "SELECT 1 FROM digital_human_material_assets WHERE asset_id=? LIMIT 1",
                (row["asset_id"],),
            ).fetchone()
            if winner:
                connection.execute(
                    "UPDATE digital_human_material_admissions SET state='committed' "
                    "WHERE admission_id=? AND state='reaping'", (row["admission_id"],),
                )
                connection.commit()
                continue
        if not _remove_orphan_material_file(row):
            continue
        with closing(_audio_db(db_factory)) as connection:
            deleted = connection.execute(
                "DELETE FROM digital_human_material_admissions "
                "WHERE admission_id=? AND state='reaping'", (row["admission_id"],),
            )
            connection.commit()
        removed += int(deleted.rowcount == 1)
    return removed


def _store_material_asset(raw, mime, provider, username, run_id, plan_digest,
                          item_index, db_factory=None):
    media_type, extension = _MATERIAL_MIMES[mime]
    try:
        now = int(time.time())
        admission_id, asset_id, target, relative = _begin_material_admission(
            username, run_id, extension, db_factory=db_factory, now=now,
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        probe = subprocess.run([
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=codec_type,width,height", "-of", "json",
            str(target),
        ], check=False, timeout=60, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        streams = json.loads((probe.stdout or b"{}").decode("utf-8")).get("streams") or []
        valid_stream = (probe.returncode == 0 and streams
                        and int(streams[0].get("width") or 0) > 0
                        and int(streams[0].get("height") or 0) > 0)
        if not valid_stream:
            raise ValueError("素材文件无法解码")
        with closing(_audio_db(db_factory)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO digital_human_material_assets(
                    asset_id,username,run_id,plan_digest,item_index,file,mime,
                    media_type,provider,created_at,expires_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (asset_id, username, run_id, plan_digest, item_index, relative,
                 mime, media_type, provider, now, now + _MATERIAL_TTL_SECONDS),
            )
            committed = connection.execute(
                "UPDATE digital_human_material_admissions SET state='committed',lease_until=0 "
                "WHERE admission_id=? AND state='active'", (admission_id,),
            )
            if committed.rowcount != 1:
                raise RuntimeError("素材写入租约已失效")
            connection.commit()
    except sqlite3.IntegrityError:
        target.unlink(missing_ok=True)
        with closing(_audio_db(db_factory)) as connection:
            connection.execute(
                "DELETE FROM digital_human_material_admissions WHERE admission_id=?",
                (admission_id,),
            )
            winner = connection.execute(
                "SELECT asset_id FROM digital_human_material_assets WHERE username=? "
                "AND run_id=? AND plan_digest=? AND item_index=?",
                (username, run_id, plan_digest, int(item_index)),
            ).fetchone()
            connection.commit()
        if winner:
            asset = _load_material_asset(
                winner["asset_id"], username, run_id, plan_digest, item_index,
                db_factory=db_factory,
            )
            return {
                "asset_id": asset["asset_id"], "media_type": asset["media_type"],
                "provider": asset["provider"], "expires_at": asset["expires_at"],
            }
        raise
    except Exception:
        target.unlink(missing_ok=True)
        if "admission_id" in locals():
            with closing(_audio_db(db_factory)) as connection:
                connection.execute(
                    "DELETE FROM digital_human_material_admissions WHERE admission_id=?",
                    (admission_id,),
                )
                connection.commit()
        raise
    return {
        "asset_id": asset_id, "media_type": media_type,
        "provider": provider, "expires_at": now + _MATERIAL_TTL_SECONDS,
    }


def _load_material_asset(asset_id, username, run_id, plan_digest, item_index,
                         now=None, db_factory=None):
    asset_id = str(asset_id or "").strip().lower()
    if not _MATERIAL_ASSET_ID_RE.fullmatch(asset_id):
        raise DigitalHumanRequestError("正文素材记录无效", "material_asset_invalid", 409)
    now = int(time.time() if now is None else now)
    with closing(_audio_db(db_factory)) as connection:
        row = connection.execute(
            "SELECT * FROM digital_human_material_assets WHERE asset_id=? AND username=? "
            "AND run_id=? AND plan_digest=? AND item_index=?",
            (asset_id, username, run_id, plan_digest, int(item_index)),
        ).fetchone()
    if not row or int(row["expires_at"]) <= now:
        raise DigitalHumanRequestError(
            "正文素材不存在、已过期或不属于本次方案", "material_asset_invalid", 409,
        )
    asset = dict(row)
    try:
        path = (OUT_DIR / asset["file"]).resolve()
        path.relative_to(OUT_DIR.resolve())
    except Exception:
        path = None
    if not path or not path.is_file() or path.stat().st_size <= 0:
        raise DigitalHumanRequestError("正文素材文件已不可用", "material_asset_invalid", 409)
    return asset


def resolve_material_response(payload, username, db_factory=None):
    if not isinstance(payload, dict):
        raise DigitalHumanRequestError("请求体必须是 JSON 对象")
    allowed = {
        "digital_human_pipeline", "digital_human_stage", "digital_human_run_id",
        "digital_human_plan_digest", "digital_human_consent_token",
        "digital_human_script",
        "digital_human_narration_mode", "digital_human_audio_upload_id",
        "digital_human_allow_ai_materials", "digital_human_customer_upload_ids",
        "digital_human_item_index",
    }
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise DigitalHumanRequestError("素材检索包含不支持字段：" + ", ".join(unknown))
    if str(payload.get("digital_human_pipeline") or "") != CONSENT_PURPOSE:
        raise DigitalHumanRequestError("素材检索流程标识无效")
    if str(payload.get("digital_human_stage") or "") != "material_resolve":
        raise DigitalHumanRequestError("素材检索步骤无效")
    record = _load_current_consent(username, payload.get("digital_human_consent_token"))
    if (str(payload.get("digital_human_run_id") or "") != record["run_id"]
            or str(payload.get("digital_human_plan_digest") or "") != record["plan_digest"]):
        raise DigitalHumanRequestError("素材检索与授权方案不一致", "consent_binding_mismatch", 403)
    frozen = _authoritative_plan(payload, username)
    if frozen["plan_digest"] != record["plan_digest"]:
        raise DigitalHumanRequestError("素材检索方案已变化", "consent_plan_mismatch", 409)
    try:
        item_index = int(payload.get("digital_human_item_index"))
        material = frozen["materials"][item_index]
    except (TypeError, ValueError, IndexError, KeyError):
        raise DigitalHumanRequestError("正文素材步骤编号无效", "consent_plan_mismatch", 409)
    with closing(_audio_db(db_factory)) as connection:
        existing = connection.execute(
            "SELECT asset_id FROM digital_human_material_assets WHERE username=? "
            "AND run_id=? AND item_index=?", (username, record["run_id"], item_index),
        ).fetchone()
    if existing:
        asset = _load_material_asset(
            existing["asset_id"], username, record["run_id"], record["plan_digest"],
            item_index, db_factory=db_factory,
        )
        return {"ok": True, "source": asset["provider"],
                "material_asset_id": asset["asset_id"], "media_type": asset["media_type"]}
    customer_upload_ids = frozen.get("customer_upload_ids") or []
    if item_index < len(customer_upload_ids):
        fetched = _customer_material(customer_upload_ids[item_index], username)
        raw, mime, provider = fetched
        try:
            asset = _store_material_asset(
                raw, mime, provider, username, record["run_id"], record["plan_digest"],
                item_index, db_factory=db_factory,
            )
        except Exception as exc:
            raise DigitalHumanRequestError(
                "顾客上传素材无法安全读取；因该素材必须进入成片，任务已暂停",
                "customer_material_unavailable", 409,
            ) from exc
        return {"ok": True, "source": provider,
                "material_asset_id": asset["asset_id"], "media_type": asset["media_type"]}
    preferred = "video" if material["scene_type"] == "video" else "image"
    try:
        fetched = _local_library_material(material["material_query"], preferred)
    except DigitalHumanRequestError:
        raise
    except Exception as exc:
        raise DigitalHumanRequestError(
            "本地素材库暂时不可用，已暂停生成且不会创建付费生图任务",
            "local_material_library_unavailable", 503,
        ) from exc
    if not fetched:
        if frozen.get("allow_ai_materials") is not True:
            raise DigitalHumanRequestError(
                "顾客素材和本地素材库均未找到匹配画面；当前方案未允许 AI 补图",
                "material_unavailable_without_ai", 409,
            )
        return {"ok": True, "source": "ai", "ai_fallback": True,
                "retryable_sources": False}
    raw, mime, provider = fetched
    try:
        asset = _store_material_asset(
            raw, mime, provider, username, record["run_id"], record["plan_digest"],
            item_index, db_factory=db_factory,
        )
    except Exception:
        if frozen.get("allow_ai_materials") is not True:
            raise DigitalHumanRequestError(
                "本地素材无法安全读取；当前方案未允许 AI 补图",
                "material_unavailable_without_ai", 409,
            )
        return {"ok": True, "source": "ai", "ai_fallback": True,
                "retryable_sources": True}
    return {"ok": True, "source": provider,
            "material_asset_id": asset["asset_id"], "media_type": asset["media_type"]}


def _as_request_error(exc):
    if isinstance(exc, DigitalHumanRequestError):
        return exc
    return DigitalHumanRequestError(
        str(exc), str(getattr(exc, "code", "invalid_digital_human_plan")),
        int(getattr(exc, "status", 400) or 400),
    )


def _audio_plan(asset):
    duration = round(float(asset["duration"]), 3)
    slices = list(asset["slices"])
    segment_durations = [round(float(item["duration"]), 3) for item in slices]
    windows = timeline.presenter_windows(segment_durations, duration)
    planned_slots = timeline.material_slots(windows, duration)
    infographic_limit = 1 if duration < 75 else 2
    infographic_indexes = {max(0, len(planned_slots) // 3)} if planned_slots else set()
    if infographic_limit == 2 and planned_slots:
        infographic_indexes.add(min(len(planned_slots) - 1, (len(planned_slots) * 2) // 3))
    segments = []
    for index, item in enumerate(slices):
        role = "hook" if index == 0 else "cta" if index == len(slices) - 1 else "explain"
        segments.append({
            "index": index, "text": str(item["text"] or "").strip(),
            "start": round(float(item["start"]), 3),
            "end": round(float(item["end"]), 3),
            "duration": round(float(item["duration"]), 3),
            "role": role,
            "audio_slice_sha256": item["sha256"],
        })
    materials = []
    excerpts = [item["text"] for item in segments if item["text"]] or [asset["transcript"]]
    for slot in planned_slots:
        excerpt = excerpts[slot["index"] % len(excerpts)][:220]
        scene_type = "infographic" if slot["index"] in infographic_indexes else (
            "video" if slot["index"] % 3 == 1 else "image"
        )
        prefix = ("为竖屏知识短视频制作一张简洁的信息图表，只展示本段关键关系，"
                  if scene_type == "infographic" else
                  "为竖屏知识短视频制作真实、自然、具有现场感的内容画面，")
        materials.append(dict(slot, **{
            "scene_type": scene_type, "material_query": excerpt,
            "prompt": prefix + "不要出现数字人口播人物、文字水印或品牌标识。画面准确表达：" + excerpt,
            "source_priority": list(timeline.SOURCE_PRIORITY),
        }))
    core = {
        "pipeline": PIPELINE, "workflow_version": timeline.WORKFLOW_VERSION,
        "narration_mode": "audio", "audio_upload_id": asset["asset_id"],
        "source_audio_sha256": asset["source_sha256"],
        "copy": asset["transcript"], "ratio": "9:16",
        "expected_duration": duration, "segments": segments,
        "presenter_windows": windows, "materials": materials,
        "infographic_limit": infographic_limit,
        "source_priority": list(timeline.SOURCE_PRIORITY),
    }
    return dict(core, segment_count=len(segments), material_count=len(materials),
                plan_digest=timeline._digest(core))


def _material_policy_values(payload):
    allow_key = ("digital_human_allow_ai_materials"
                 if "digital_human_allow_ai_materials" in payload else
                 "allow_ai_materials")
    ids_key = ("digital_human_customer_upload_ids"
               if "digital_human_customer_upload_ids" in payload else
               "customer_upload_ids")
    allow_ai = payload.get(allow_key, False)
    if not isinstance(allow_ai, bool):
        raise DigitalHumanRequestError(
            "AI 补图选项必须为布尔值", "invalid_material_policy",
        )
    raw_ids = payload.get(ids_key, [])
    if not isinstance(raw_ids, list):
        raise DigitalHumanRequestError(
            "顾客上传素材列表格式无效", "invalid_customer_materials",
        )
    upload_ids = []
    for raw_id in raw_ids:
        upload_id = str(raw_id or "").strip().lower()
        if not re.fullmatch(r"img_[0-9a-f]{32}", upload_id):
            raise DigitalHumanRequestError(
                "顾客上传素材编号无效", "invalid_customer_materials",
            )
        if upload_id in upload_ids:
            raise DigitalHumanRequestError(
                "同一张顾客素材不能重复用于多个镜头", "duplicate_customer_material",
            )
        upload_ids.append(upload_id)
    return allow_ai, upload_ids


def _bind_material_policy(base_plan, allow_ai, upload_ids, explicit=True):
    if len(upload_ids) > int(base_plan.get("material_count") or 0):
        raise DigitalHumanRequestError(
            "顾客上传素材超过当前方案的内容镜头数量",
            "customer_material_count_exceeded", 409,
        )
    core = dict(base_plan)
    segment_count = int(core.pop("segment_count"))
    material_count = int(core.pop("material_count"))
    core.pop("plan_digest", None)
    core["materials"] = [
        dict(item, source_priority=list(MATERIAL_SOURCE_PRIORITY))
        for item in core.get("materials") or []
    ]
    core["source_priority"] = list(MATERIAL_SOURCE_PRIORITY)
    core["allow_ai_materials"] = bool(allow_ai)
    core["customer_upload_ids"] = list(upload_ids)
    return dict(
        core, segment_count=segment_count, material_count=material_count,
        plan_digest=timeline._digest(core),
    )


def _validate_customer_uploads(upload_ids, username):
    if not upload_ids:
        return
    from . import cli_uploads

    for upload_id in upload_ids:
        try:
            metadata = cli_uploads.inspect_image(upload_id, username)
        except ValueError as exc:
            raise DigitalHumanRequestError(
                str(exc), "customer_material_unavailable", 409,
            ) from exc
        if str(metadata.get("approved_for") or "") != _CUSTOMER_MATERIAL_UPLOAD_PURPOSE:
            raise DigitalHumanRequestError(
                "顾客上传素材未通过当前素材入口校验，请重新上传",
                "customer_material_unapproved", 409,
            )


def plan_response(payload, username=None):
    if not isinstance(payload, dict):
        raise DigitalHumanRequestError("请求体必须是 JSON 对象")
    mode = str(payload.get("narration_mode") or "text").strip().lower()
    try:
        allow_ai, upload_ids = _material_policy_values(payload)
        request_payload = dict(payload)
        request_payload.pop("allow_ai_materials", None)
        request_payload.pop("customer_upload_ids", None)
        if mode == "audio":
            allowed = {"narration_mode", "audio_upload_id"}
            unknown = sorted(set(request_payload) - allowed)
            if unknown:
                raise DigitalHumanRequestError("方案提交包含不支持字段：" + ", ".join(unknown))
            asset = _load_audio_asset(request_payload.get("audio_upload_id"), username)
            base_plan = _audio_plan(asset)
        else:
            base_plan = timeline.plan_response(request_payload)["plan"]
        plan = _bind_material_policy(base_plan, allow_ai, upload_ids)
        _validate_customer_uploads(upload_ids, username)
        return {"ok": True, "plan": plan}
    except Exception as exc:
        raise _as_request_error(exc) from exc


def _authoritative_plan(payload, username=None):
    try:
        allow_ai, upload_ids = _material_policy_values(payload)
        if str(payload.get("digital_human_narration_mode") or
               payload.get("narration_mode") or "text").strip().lower() == "audio":
            asset = _load_audio_asset(
                payload.get("digital_human_audio_upload_id") or payload.get("audio_upload_id"),
                username,
            )
            base_plan = _audio_plan(asset)
        else:
            base_plan = timeline.plan_text(
                payload.get("digital_human_script") or payload.get("script"),
            )
        return _bind_material_policy(base_plan, allow_ai, upload_ids)
    except Exception as exc:
        raise _as_request_error(exc) from exc


def create_consent(payload, username, signing_secret, now=None, db_factory=None):
    if not isinstance(payload, dict):
        raise DigitalHumanRequestError("请求体必须是 JSON 对象")
    allowed = {
        "confirmed", "consent_version", "purpose", "run_id", "plan_digest",
        "script", "photo_sha256", "voice_mode", "voice_ref",
        "voice_sha256", "narration_mode", "audio_upload_id",
        "allow_ai_materials", "customer_upload_ids",
    }
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise DigitalHumanRequestError("授权提交包含不支持字段：" + ", ".join(unknown))
    if payload.get("confirmed") is not True:
        raise DigitalHumanRequestError("请先确认照片与声音授权", "consent_required", 403)
    if str(payload.get("consent_version") or "") != CONSENT_VERSION:
        raise DigitalHumanRequestError("授权条款版本已更新，请重新确认", "consent_version_mismatch", 409)
    if str(payload.get("purpose") or "") != CONSENT_PURPOSE:
        raise DigitalHumanRequestError("授权用途无效", "consent_purpose_invalid")
    narration_mode = str(payload.get("narration_mode") or "text").strip().lower()
    if narration_mode not in {"text", "audio"}:
        raise DigitalHumanRequestError("声音驱动方式无效")
    username = str(username or "").strip()
    if not username:
        raise DigitalHumanRequestError("未登录或登录已过期", "unauthorized", 401)
    run_id = str(payload.get("run_id") or "").strip()
    if not legacy._RUN_ID_RE.fullmatch(run_id):
        raise DigitalHumanRequestError("本次制作流程编号无效，请重新开始")
    try:
        audio_asset = (_load_audio_asset(payload.get("audio_upload_id"), username)
                       if narration_mode == "audio" else None)
        frozen = _authoritative_plan(payload, username)
        _validate_customer_uploads(frozen.get("customer_upload_ids") or [], username)
    except Exception as exc:
        raise _as_request_error(exc) from exc
    plan_digest = legacy._required_sha256(payload.get("plan_digest"), "制作方案")
    if not hmac.compare_digest(plan_digest, frozen["plan_digest"]):
        raise DigitalHumanRequestError(
            "制作方案与服务端时长拆分结果不一致，请重新分析方案",
            "consent_plan_mismatch", 409,
        )
    photo_sha256 = legacy._required_sha256(payload.get("photo_sha256"), "人物照片")
    if narration_mode == "audio":
        voice_mode = "audio"
        voice_ref = audio_asset["asset_id"]
        voice_sha256 = audio_asset["source_sha256"]
        if payload.get("voice_ref") or payload.get("voice_sha256"):
            raise DigitalHumanRequestError("录音驱动模式不应选择或复刻音色")
    else:
        voice_mode = str(payload.get("voice_mode") or "").strip().lower()
        if voice_mode not in {"existing", "clone"}:
            raise DigitalHumanRequestError("声音授权类型无效")
        voice_ref = str(payload.get("voice_ref") or "").strip()
        if not voice_ref or len(voice_ref) > 180:
            raise DigitalHumanRequestError("声音资产标识无效")
        voice_sha256 = str(payload.get("voice_sha256") or "").strip().lower()
        if voice_mode == "clone":
            voice_sha256 = legacy._required_sha256(voice_sha256, "声音样本")
        elif voice_sha256:
            raise DigitalHumanRequestError("复用已有声音时不应上传样音校验值")
    now = int(time.time() if now is None else now)
    consent_id = "dhc_" + hmac.new(
        str(signing_secret or "").encode("utf-8"),
        (username + "|" + run_id).encode("utf-8"), hashlib.sha256,
    ).hexdigest()[:32]
    candidate = {
        "id": consent_id, "username": username, "run_id": run_id,
        "consent_version": CONSENT_VERSION, "purpose": CONSENT_PURPOSE,
        "plan_digest": plan_digest, "photo_sha256": photo_sha256,
        "voice_mode": voice_mode, "voice_ref": voice_ref,
        "voice_sha256": voice_sha256, "created_at": now,
        "expires_at": now + CONSENT_TTL_SECONDS,
    }
    legacy._consent_signature(candidate, signing_secret)
    db_factory = db_factory or legacy.cdb
    legacy.init_db(db_factory)
    with closing(db_factory()) as connection:
        row = connection.execute(
            "SELECT * FROM digital_human_consents WHERE username=? AND run_id=?",
            (username, run_id),
        ).fetchone()
        if row:
            existing = dict(row)
            comparable = (
                "consent_version", "purpose", "plan_digest", "photo_sha256",
                "voice_mode", "voice_ref", "voice_sha256",
            )
            if any(str(existing[key]) != str(candidate[key]) for key in comparable):
                raise DigitalHumanRequestError(
                    "本次流程的照片、声音或方案已经变化，请重新开始并授权",
                    "consent_binding_conflict", 409,
                )
            if int(existing["expires_at"]) <= now:
                raise DigitalHumanRequestError(
                    "本次授权已过期，请重新开始并授权", "consent_expired", 409,
                )
            candidate = existing
        token = candidate["id"] + "." + legacy._consent_signature(candidate, signing_secret)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        if row:
            if not hmac.compare_digest(str(candidate["token_hash"]), token_hash):
                raise DigitalHumanRequestError(
                    "授权存证签名已变化，请重新开始", "consent_signature_changed", 409,
                )
        else:
            connection.execute(
                """INSERT INTO digital_human_consents(
                    id,username,run_id,consent_version,purpose,plan_digest,
                    photo_sha256,voice_mode,voice_ref,voice_sha256,token_hash,
                    created_at,expires_at,last_used_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    candidate["id"], candidate["username"], candidate["run_id"],
                    candidate["consent_version"], candidate["purpose"],
                    candidate["plan_digest"], candidate["photo_sha256"],
                    candidate["voice_mode"], candidate["voice_ref"],
                    candidate["voice_sha256"], token_hash, candidate["created_at"],
                    candidate["expires_at"], now,
                ),
            )
            connection.commit()
    return legacy._public_consent(candidate, token)


def consent_response(payload, username, signing_secret, db_factory=None):
    return {"ok": True, "consent": create_consent(
        payload, username, signing_secret, db_factory=db_factory,
    )}


def _load_current_consent(username, token):
    record = legacy._load_consent(username, token)
    if (record.get("purpose") != CONSENT_PURPOSE
            or record.get("consent_version") != CONSENT_VERSION):
        raise DigitalHumanRequestError(
            "授权记录不属于当前数字人成片流程", "consent_binding_mismatch", 403,
        )
    return record


def verify_clone_submission(payload, username):
    """Bind a v2 voice-clone submission to its signed consent and plan."""
    if not isinstance(payload, dict):
        raise DigitalHumanRequestError("请求体必须是 JSON 对象")
    if str(payload.get("digital_human_pipeline") or "").strip().lower() != CONSENT_PURPOSE:
        raise DigitalHumanRequestError("数字人成片流程标识无效")
    if str(payload.get("digital_human_stage") or "").strip().lower() != "voice_clone":
        raise DigitalHumanRequestError("声音复刻步骤标识无效")
    record = _load_current_consent(username, payload.get("digital_human_consent_token"))
    if str(payload.get("digital_human_run_id") or "") != record["run_id"]:
        raise DigitalHumanRequestError(
            "授权与本次制作流程不匹配", "consent_binding_mismatch", 403,
        )
    digest = str(payload.get("digital_human_plan_digest") or "").strip().lower()
    if not hmac.compare_digest(digest, record["plan_digest"]):
        raise DigitalHumanRequestError(
            "授权与制作方案不匹配", "consent_binding_mismatch", 403,
        )
    frozen = _authoritative_plan(payload, username)
    if not hmac.compare_digest(frozen["plan_digest"], record["plan_digest"]):
        raise DigitalHumanRequestError(
            "声音复刻文案与本次授权方案不一致，请重新开始",
            "consent_plan_mismatch", 409,
        )
    if record["voice_mode"] != "clone":
        raise DigitalHumanRequestError(
            "当前授权未允许重新复刻声音", "consent_voice_mismatch", 403,
        )
    if str(payload.get("slot_id") or "").strip() != record["voice_ref"]:
        raise DigitalHumanRequestError(
            "音色槽位与授权记录不一致", "consent_voice_mismatch", 403,
        )
    actual = hashlib.sha256(
        legacy._decode_b64_bytes(payload.get("audio"), "声音样本")
    ).hexdigest()
    if not hmac.compare_digest(actual, record["voice_sha256"]):
        raise DigitalHumanRequestError(
            "声音样本与授权记录不一致", "consent_voice_mismatch", 403,
        )
    cleaned = dict(payload)
    cleaned.pop("digital_human_consent_token", None)
    cleaned.pop("allow_ai_materials", None)
    cleaned.pop("customer_upload_ids", None)
    cleaned["digital_human_consent_id"] = record["id"]
    return cleaned


def _binding(payload_text, job_id, expected_stage, record, expected_index=None):
    try:
        payload = json.loads(payload_text or "")
    except Exception as exc:
        raise DigitalHumanRequestError(
            "子任务 #%d 的授权记录损坏，请重新生成" % job_id,
            "child_consent_binding_invalid", 409,
        ) from exc
    expected = {
        "digital_human_pipeline": CONSENT_PURPOSE,
        "digital_human_stage": expected_stage,
        "digital_human_consent_id": record["id"],
        "digital_human_run_id": record["run_id"],
        "digital_human_plan_digest": record["plan_digest"],
    }
    if any(str(payload.get(key) or "") != str(value) for key, value in expected.items()):
        raise DigitalHumanRequestError(
            "子任务 #%d 不属于本次授权制作流程，请重新生成" % job_id,
            "child_consent_binding_mismatch", 409,
        )
    if expected_index is not None:
        try:
            actual = int(payload.get("digital_human_item_index"))
        except (TypeError, ValueError):
            actual = -1
        if actual != int(expected_index):
            raise DigitalHumanRequestError(
                "子任务 #%d 的方案位置不匹配，请重新生成" % job_id,
                "child_consent_binding_mismatch", 409,
            )


def _authorized_portrait_jpeg(value, expected_sha256):
    """Verify the authorized upload and return a canonical HeyGen JPEG.

    The digest is checked against the original bytes before conversion.  This
    lets JPEG, PNG and WebP uploads drive HeyGen directly while preventing a
    client from swapping the portrait after consent.
    """
    raw = legacy._decode_b64_bytes(value, "人物照片")
    if len(raw) > 35 * 1024 * 1024:
        raise DigitalHumanRequestError("人物照片不能超过 35MB", "portrait_too_large")
    actual = hashlib.sha256(raw).hexdigest()
    if not hmac.compare_digest(actual, expected_sha256):
        raise DigitalHumanRequestError(
            "人物照片与授权记录不一致，请重新开始并授权",
            "consent_photo_mismatch", 403,
        )
    try:
        from PIL import Image, ImageOps
        with Image.open(io.BytesIO(raw)) as source:
            if str(source.format or "").upper() not in {"JPEG", "PNG", "WEBP"}:
                raise ValueError("unsupported portrait format")
            if int(source.width) * int(source.height) > 40_000_000:
                raise ValueError("portrait dimensions are too large")
            source.verify()
        with Image.open(io.BytesIO(raw)) as source:
            source.load()
            image = ImageOps.exif_transpose(source).convert("RGB")
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=95, optimize=True)
    except ImportError as exc:
        raise DigitalHumanRequestError(
            "人物照片完整性校验组件不可用，请稍后重试",
            "portrait_validator_unavailable", 503,
        ) from exc
    except Exception as exc:
        raise DigitalHumanRequestError(
            "人物照片已损坏或格式不受支持，请上传 JPEG、PNG 或 WebP",
            "portrait_content_invalid",
        ) from exc
    return "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode("ascii")


def verify_child_submission_with_record(payload, username, kind):
    if not isinstance(payload, dict):
        return payload, None
    if str(payload.get("digital_human_pipeline") or "").strip().lower() != CONSENT_PURPOSE:
        return payload, None
    stage = str(payload.get("digital_human_stage") or "").strip().lower()
    if _STAGE_KINDS.get(stage) != str(kind or ""):
        raise DigitalHumanRequestError("数字人成片步骤与任务类型不匹配")
    record = _load_current_consent(username, payload.get("digital_human_consent_token"))
    if str(payload.get("digital_human_run_id") or "") != record["run_id"]:
        raise DigitalHumanRequestError("授权与本次制作流程不匹配", "consent_binding_mismatch", 403)
    if str(payload.get("digital_human_plan_digest") or "").lower() != record["plan_digest"]:
        raise DigitalHumanRequestError("授权与制作方案不匹配", "consent_binding_mismatch", 403)
    frozen = _authoritative_plan(payload, username)
    if not hmac.compare_digest(frozen["plan_digest"], record["plan_digest"]):
        raise DigitalHumanRequestError(
            "子任务文案与本次授权方案不一致，请重新开始",
            "consent_plan_mismatch", 409,
        )
    cleaned = dict(payload)
    cleaned.pop("digital_human_consent_token", None)
    cleaned["digital_human_consent_id"] = record["id"]
    raw_index = payload.get("digital_human_item_index")
    if isinstance(raw_index, bool):
        raw_index = None
    try:
        item_index = int(raw_index)
    except (TypeError, ValueError):
        item_index = -1
    if stage == "material":
        if not 0 <= item_index < frozen["material_count"]:
            raise DigitalHumanRequestError("正文素材步骤编号无效", "consent_plan_mismatch", 409)
        customer_upload_ids = frozen.get("customer_upload_ids") or []
        if item_index < len(customer_upload_ids):
            raise DigitalHumanRequestError(
                "这个镜头已绑定顾客上传素材，禁止改用 AI 重新生成",
                "customer_material_required", 409,
            )
        if frozen.get("allow_ai_materials") is not True:
            raise DigitalHumanRequestError(
                "当前方案未允许 AI 补图，未创建付费生图任务",
                "ai_material_not_allowed", 409,
            )
        material = frozen["materials"][item_index]
        references = cleaned.get("reference_images")
        cleaned.pop("images", None)
        if references is not None:
            cleaned["reference_images"] = references
        cleaned.update({
            "prompt": material["prompt"], "provider": "seedream",
            "variant": "std", "quality": "std", "count": 1,
            "ratio": "9:16",
            "digital_human_item_index": item_index,
        })
        cleaned.pop("model", None)
    elif stage == "talking":
        if not 0 <= item_index < frozen["segment_count"]:
            raise DigitalHumanRequestError("口播步骤编号无效", "consent_plan_mismatch", 409)
        segment = frozen["segments"][item_index]
        audio_mode = frozen.get("narration_mode") == "audio"
        expected_voice = ""
        audio_asset = None
        if audio_mode:
            if record["voice_mode"] != "audio":
                raise DigitalHumanRequestError(
                    "录音驱动授权与当前方案不一致", "consent_voice_mismatch", 403,
                )
            audio_asset = _load_audio_asset(record["voice_ref"], username)
            if (audio_asset["asset_id"] != frozen.get("audio_upload_id")
                    or not hmac.compare_digest(
                        audio_asset["source_sha256"], record["voice_sha256"])):
                raise DigitalHumanRequestError(
                    "完整录音与授权记录不一致", "consent_voice_mismatch", 403,
                )
        else:
            expected_voice = (record["voice_ref"] if record["voice_mode"] == "existing"
                              else legacy._expected_cloned_voice(record["voice_ref"]))
            if str(payload.get("voice") or "").strip() != expected_voice:
                raise DigitalHumanRequestError(
                    "口播声音与授权记录不一致，请重新开始并授权",
                    "consent_voice_mismatch", 403,
                )
        references = cleaned.pop("reference_images", None)
        if not isinstance(references, list) or len(references) != 1:
            raise DigitalHumanRequestError(
                "数字人口播必须且只能使用本次授权的一张人物照片",
                "consent_photo_mismatch", 403,
            )
        cleaned.update(legacy.natural_mouth_talking_profile())
        cleaned.update({
            "image_data": _authorized_portrait_jpeg(
                references[0], record["photo_sha256"],
            ),
            "digital_human_item_index": item_index,
        })
        if audio_mode:
            audio_slice = audio_asset["slices"][item_index]
            if not hmac.compare_digest(
                    str(audio_slice.get("sha256") or ""),
                    str(segment.get("audio_slice_sha256") or "")):
                raise DigitalHumanRequestError(
                    "录音切段与制作方案不一致，请重新上传",
                    "audio_slice_binding_mismatch", 409,
                )
            try:
                audio_path = (OUT_DIR / audio_slice["file"]).resolve()
                audio_path.relative_to(OUT_DIR.resolve())
            except Exception:
                audio_path = None
            if not audio_path or not audio_path.is_file() or audio_path.stat().st_size <= 0:
                raise DigitalHumanRequestError(
                    "录音切段文件已不可用，请重新上传", "audio_slice_unavailable", 409,
                )
            cleaned.update({
                "mode": "audio", "text": segment["text"], "voice": "",
                "audio_data": "data:audio/mp4;base64," + base64.b64encode(
                    audio_path.read_bytes()).decode("ascii"),
            })
            cleaned.pop("audio_file", None)
        else:
            cleaned.update({
                "mode": "text", "text": segment["text"], "voice": expected_voice,
            })
        cleaned.pop("gesture_job_id", None)
        try:
            from . import video as video_domain
            video_domain.subtitle_runtime_preflight()
        except Exception as exc:
            raise DigitalHumanRequestError(
                str(exc)[:220], str(getattr(exc, "code", "subtitle_runtime_unavailable")),
                int(getattr(exc, "status", 503) or 503),
            ) from exc
    elif stage == "compose":
        if str(payload.get("plan_digest") or "").lower() != record["plan_digest"]:
            raise DigitalHumanRequestError("成片方案与授权记录不一致", "consent_binding_mismatch", 403)
    cleaned.pop("digital_human_script", None)
    return cleaned, record


def _owned_completed_files(username, ids, kind, stage, record, expected):
    if not isinstance(ids, list) or any(isinstance(item, bool) for item in ids):
        raise DigitalHumanRequestError("子任务编号格式无效")
    try:
        normalized = [int(item) for item in ids]
    except (TypeError, ValueError):
        raise DigitalHumanRequestError("子任务编号格式无效")
    if len(normalized) != expected or len(set(normalized)) != expected:
        raise DigitalHumanRequestError("子任务数量不完整或包含重复任务")
    placeholders = ",".join("?" for _ in normalized)
    with closing(jdb()) as connection:
        rows = connection.execute(
            "SELECT id,kind,status,payload,result FROM jobs WHERE username=? "
            "AND COALESCE(deleted,0)=0 AND id IN (%s)" % placeholders,
            [username] + normalized,
        ).fetchall()
    by_id = {int(row["id"]): row for row in rows}
    files = []
    for index, job_id in enumerate(normalized):
        row = by_id.get(job_id)
        if not row or row["kind"] != kind or row["status"] != "done":
            raise DigitalHumanRequestError(
                "子任务 #%d 不存在、未完成或不属于当前账号" % job_id,
                "child_job_unavailable", 409,
            )
        _binding(row["payload"], job_id, stage, record, index)
        try:
            result = json.loads(row["result"] or "{}")
        except Exception:
            result = {}
        rel = legacy._result_file(result, kind)
        try:
            path = (OUT_DIR / rel).resolve()
            path.relative_to(OUT_DIR.resolve())
        except Exception:
            path = None
        if not path or not path.is_file() or path.stat().st_size <= 0:
            raise DigitalHumanRequestError(
                "子任务 #%d 的本地文件已不可用" % job_id,
                "child_file_unavailable", 409,
            )
        files.append(path.relative_to(OUT_DIR.resolve()).as_posix())
    return normalized, files


def _owned_material_files(username, job_ids, asset_ids, record, expected,
                          db_factory=None):
    if not isinstance(job_ids, list) or not isinstance(asset_ids, list):
        raise DigitalHumanRequestError("正文素材提交格式无效")
    if len(job_ids) != expected or len(asset_ids) != expected:
        raise DigitalHumanRequestError("正文素材数量不完整")
    normalized_jobs, normalized_assets, files, media_types = [], [], [], []
    seen_jobs = set()
    for index, (raw_job_id, raw_asset_id) in enumerate(zip(job_ids, asset_ids)):
        asset_id = str(raw_asset_id or "").strip().lower()
        try:
            job_id = int(raw_job_id or 0)
        except (TypeError, ValueError):
            job_id = -1
        if asset_id:
            if job_id != 0:
                raise DigitalHumanRequestError("同一个正文镜头不能同时绑定任务和素材")
            asset = _load_material_asset(
                asset_id, username, record["run_id"], record["plan_digest"], index,
                db_factory=db_factory,
            )
            normalized_jobs.append(0)
            normalized_assets.append(asset_id)
            files.append(asset["file"])
            media_types.append(asset["media_type"])
            continue
        if job_id <= 0 or job_id in seen_jobs:
            raise DigitalHumanRequestError("AI 补图任务编号无效或重复")
        seen_jobs.add(job_id)
        with closing(jdb()) as connection:
            row = connection.execute(
                "SELECT id,kind,status,payload,result FROM jobs WHERE id=? AND username=? "
                "AND COALESCE(deleted,0)=0", (job_id, username),
            ).fetchone()
        if not row or row["kind"] != "image" or row["status"] != "done":
            raise DigitalHumanRequestError(
                "AI 补图任务 #%d 不存在、未完成或不属于当前账号" % job_id,
                "child_job_unavailable", 409,
            )
        _binding(row["payload"], job_id, "material", record, index)
        try:
            result = json.loads(row["result"] or "{}")
        except Exception:
            result = {}
        rel = legacy._result_file(result, "image")
        try:
            path = (OUT_DIR / rel).resolve()
            path.relative_to(OUT_DIR.resolve())
        except Exception:
            path = None
        if not path or not path.is_file() or path.stat().st_size <= 0:
            raise DigitalHumanRequestError(
                "AI 补图任务 #%d 的文件已不可用" % job_id,
                "child_file_unavailable", 409,
            )
        normalized_jobs.append(job_id)
        normalized_assets.append("")
        files.append(path.relative_to(OUT_DIR.resolve()).as_posix())
        media_types.append("image")
    return normalized_jobs, normalized_assets, files, media_types


def prepare_compose_payload(payload, username, consent_record=None):
    if not isinstance(payload, dict):
        raise DigitalHumanRequestError("请求体必须是 JSON 对象")
    allowed = {
        "pipeline", "mode", "script", "plan_digest", "video_job_ids",
        "material_job_ids", "material_asset_ids", "digital_human_pipeline", "digital_human_stage",
        "digital_human_run_id", "digital_human_plan_digest",
        "digital_human_consent_id", "digital_human_script",
        "digital_human_item_index",
        "digital_human_narration_mode", "digital_human_audio_upload_id",
        "digital_human_allow_ai_materials", "digital_human_customer_upload_ids",
    }
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise DigitalHumanRequestError("提交包含不支持字段：" + ", ".join(unknown))
    if str(payload.get("pipeline") or "").strip().lower() != PIPELINE:
        raise DigitalHumanRequestError("pipeline 无效")
    frozen = _authoritative_plan(payload, username)
    if str(payload.get("plan_digest") or "").lower() != frozen["plan_digest"]:
        raise DigitalHumanRequestError("制作方案已变化，请重新开始生成", "plan_digest_mismatch", 409)
    if (not isinstance(consent_record, dict)
            or consent_record.get("purpose") != CONSENT_PURPOSE
            or consent_record.get("consent_version") != CONSENT_VERSION):
        raise DigitalHumanRequestError("缺少服务端已验证的授权记录，请重新确认授权", "consent_required", 403)
    authoritative = {
        "digital_human_pipeline": CONSENT_PURPOSE,
        "digital_human_stage": "compose",
        "digital_human_consent_id": str(consent_record.get("id") or ""),
        "digital_human_run_id": str(consent_record.get("run_id") or ""),
        "digital_human_plan_digest": str(consent_record.get("plan_digest") or "").lower(),
    }
    if (str(consent_record.get("username") or "") != str(username or "")
            or authoritative["digital_human_plan_digest"] != frozen["plan_digest"]
            or any(str(payload.get(key) or "") != value for key, value in authoritative.items())):
        raise DigitalHumanRequestError(
            "成片授权与本次制作流程不匹配，请重新确认授权",
            "consent_binding_mismatch", 403,
        )
    video_ids, video_files = _owned_completed_files(
        username, payload.get("video_job_ids"), "video", "talking", consent_record,
        frozen["segment_count"],
    )
    material_ids, material_asset_ids, material_files, material_types = _owned_material_files(
        username, payload.get("material_job_ids"), payload.get("material_asset_ids"),
        consent_record, frozen["material_count"],
    )
    prepared = dict(frozen)
    prepared.update(authoritative)
    prepared.update({
        "pipeline": PIPELINE, "mode": PIPELINE,
        "video_job_ids": video_ids, "material_job_ids": material_ids,
        "material_asset_ids": material_asset_ids,
        "video_files": video_files, "material_files": material_files,
        "material_types": material_types,
        "material_generate_count": 0,
    })
    return prepared


def _visual_items(windows, slots):
    items = ([{"kind": "presenter", "start": start, "end": end}
              for start, end in windows] +
             [dict(slot, kind="material") for slot in slots])
    return sorted(items, key=lambda item: (float(item["start"]), 0 if item["kind"] == "presenter" else 1))


def _history_video_url(result):
    """Return only a playable URL from a completed private compose result."""
    for key in ("video_url", "url"):
        value = str(result.get(key) or "").strip()
        if value.startswith("/api/gen/file/") or value.startswith("https://"):
            return value
    rel = str(result.get("video_file") or "").strip().replace("\\", "/")
    if not rel:
        return ""
    try:
        path = (OUT_DIR / rel).resolve()
        path.relative_to(OUT_DIR.resolve())
    except Exception:
        return ""
    if not path.is_file() or path.stat().st_size <= 0:
        return ""
    return "/api/gen/file/" + urllib.parse.quote(rel, safe="/")


def _history_number(value, integer=False):
    try:
        parsed = float(value or 0)
    except (TypeError, ValueError):
        parsed = 0.0
    return int(parsed) if integer else round(parsed, 3)


def history_response(username, limit=20, offset=0, db_factory=None):
    """List the authenticated user's completed v2 videos, including old rows.

    Pagination is applied after the exact pipeline filter so another pipeline's
    jobs cannot create gaps or leak through this dedicated endpoint.
    """
    username = str(username or "").strip()
    if not username:
        raise DigitalHumanRequestError("未登录或登录已过期", "authentication_required", 401)
    try:
        limit = int(limit)
        offset = int(offset)
    except (TypeError, ValueError) as exc:
        raise DigitalHumanRequestError("历史记录分页参数无效") from exc
    limit = max(1, min(limit, 50))
    offset = max(0, min(offset, 2000))
    target = offset + limit + 1
    scanned = 0
    matched = []
    factory = db_factory or jdb
    while len(matched) < target and scanned < 2000:
        batch_size = min(120, 2000 - scanned)
        with closing(factory()) as connection:
            rows = connection.execute(
                "SELECT id,status,payload,result,created_at FROM jobs "
                "WHERE username=? AND kind='script_to_video' AND status='done' "
                "AND COALESCE(deleted,0)=0 ORDER BY id DESC LIMIT ? OFFSET ?",
                (username, batch_size, scanned),
            ).fetchall()
        if not rows:
            break
        scanned += len(rows)
        for row in rows:
            try:
                payload = json.loads(row["payload"] or "{}")
                result = json.loads(row["result"] or "{}")
            except Exception:
                continue
            if not isinstance(payload, dict) or not isinstance(result, dict):
                continue
            if str(payload.get("pipeline") or "").strip().lower() != PIPELINE:
                continue
            video_url = _history_video_url(result)
            if not video_url:
                continue
            verification = result.get("verification")
            if not isinstance(verification, dict):
                verification = {}
            text = str(payload.get("copy") or payload.get("script")
                       or payload.get("digital_human_script") or "").strip()
            matched.append({
                "job_id": int(row["id"]),
                "status": "done",
                "video_url": video_url,
                "text": text[:160],
                "duration": _history_number(result.get("duration")),
                "width": _history_number(result.get("width"), integer=True),
                "height": _history_number(result.get("height"), integer=True),
                "created_at": _history_number(row["created_at"], integer=True),
                "subtitle": str(verification.get("subtitle") or ""),
                "mode": PIPELINE,
            })
            if len(matched) >= target:
                break
        if len(rows) < batch_size:
            break
    visible = matched[offset:offset + limit]
    return {
        "items": visible,
        "limit": limit,
        "offset": offset,
        "has_more": len(matched) > offset + len(visible),
    }


def compose(payload, persist_state=None):
    from . import video as video_domain

    job_id = int(payload.get("_job_id") or 0)
    if not job_id:
        raise RuntimeError("数字人成片缺少任务编号")
    videos = [(OUT_DIR / rel).resolve() for rel in payload.get("video_files") or []]
    materials = [(OUT_DIR / rel).resolve() for rel in payload.get("material_files") or []]
    material_types = list(payload.get("material_types") or [])
    if (len(videos) != int(payload.get("segment_count") or 0)
            or len(materials) != int(payload.get("material_count") or 0)
            or len(material_types) != len(materials)
            or any(value not in {"image", "video"} for value in material_types)):
        raise RuntimeError("数字人成片子任务数量不完整")
    for path in videos + materials:
        path.relative_to(OUT_DIR.resolve())
        if not path.is_file() or path.stat().st_size <= 0:
            raise RuntimeError("数字人成片子任务文件不可用")
    if persist_state:
        persist_state("composing")
    out_dir = video_domain.VIDEO_OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    normalized = []
    durations = []
    for index, source in enumerate(videos):
        target = out_dir / ("digital_human_v2_%d_part_%d.mp4" % (job_id, index + 1))
        legacy._run([
            "ffmpeg", "-y", "-i", str(source), "-vf",
            "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,fps=30",
            "-c:v", "libx264", "-preset", "fast", "-crf", "21", "-c:a", "aac",
            "-ar", "48000", "-ac", "2", "-movflags", "+faststart", str(target),
        ])
        normalized.append(target)
        relative = target.resolve().relative_to(OUT_DIR.resolve()).as_posix()
        durations.append(video_domain._probe_video_duration(relative))
    if any(duration <= 0 for duration in durations):
        raise RuntimeError("数字人口播子片段时长无效")
    joined = out_dir / ("digital_human_v2_%d_joined.mp4" % job_id)
    concat_file = out_dir / ("digital_human_v2_%d_concat.txt" % job_id)
    concat_file.write_text("".join(
        "file '%s'\n" % str(path).replace("'", "'\\''") for path in normalized
    ), encoding="utf-8")
    legacy._run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file), "-c", "copy", str(joined)])
    duration = sum(durations)
    windows = timeline.presenter_windows(durations, duration)
    slots = timeline.material_slots(windows, duration, len(materials))
    items = _visual_items(windows, slots)
    command = ["ffmpeg", "-y", "-i", str(joined)]
    for material, media_type, slot in zip(materials, material_types, slots):
        if media_type == "image":
            command.extend(["-loop", "1", "-t", "%.3f" % slot["duration"], "-i", str(material)])
        else:
            command.extend(["-stream_loop", "-1", "-i", str(material)])
    filters = []
    labels = []
    material_input_by_index = {slot["index"]: index + 1 for index, slot in enumerate(slots)}
    for index, item in enumerate(items):
        label = "clip%d" % index
        labels.append("[%s]" % label)
        start, end = float(item["start"]), float(item["end"])
        if item["kind"] == "presenter":
            filters.append(
                "[0:v]trim=start=%.3f:end=%.3f,setpts=PTS-STARTPTS,"
                "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,fps=30,"
                "tpad=stop_mode=clone:stop_duration=1,trim=duration=%.3f,setpts=PTS-STARTPTS[%s]"
                % (start, end, end - start, label)
            )
        else:
            source_index = material_input_by_index[item["index"]]
            if material_types[source_index - 1] == "image":
                filters.append(
                    "[%d:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
                    "zoompan=z='min(zoom+0.00035,1.055)':x='iw/2-(iw/zoom/2)':"
                    "y='ih/2-(ih/zoom/2)':d=1:s=1080x1920:fps=30,"
                    "setsar=1,format=yuv420p,"
                    "tpad=stop_mode=clone:stop_duration=1,trim=duration=%.3f,"
                    "setpts=PTS-STARTPTS[%s]" % (source_index, end - start, label)
                )
            else:
                filters.append(
                    "[%d:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
                    "setsar=1,fps=30,tpad=stop_mode=clone:stop_duration=1,"
                    "trim=duration=%.3f,setpts=PTS-STARTPTS[%s]" %
                    (source_index, end - start, label)
                )
    filters.append("%sconcat=n=%d:v=1:a=0[joinedv]" % ("".join(labels), len(labels)))
    filters.append(
        "[joinedv]tpad=stop_mode=clone:stop_duration=1,trim=duration=%.3f,"
        "setpts=PTS-STARTPTS[outv]" % duration
    )
    composed = out_dir / ("digital_human_v2_%d_composed.mp4" % job_id)
    command.extend([
        "-filter_complex", ";".join(filters), "-map", "[outv]", "-map", "0:a:0",
        "-af", "apad=pad_dur=1",
        "-t", "%.3f" % duration, "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(composed),
    ])
    legacy._run(command)
    rel = composed.resolve().relative_to(OUT_DIR.resolve()).as_posix()
    if persist_state:
        persist_state("subtitle_processing", plain_video_file=rel)
    subtitle_error = ""
    try:
        final_rel = video_domain.burn_subtitle(
            rel, known_text=payload.get("copy"), style_key="white",
            job_id=job_id, position="bottom",
        )
    except Exception as exc:
        final_rel = rel
        subtitle_error = str(exc)[:220] or "字幕处理失败"
    _path, width, height, final_duration = legacy._verify_final_video(
        video_domain, final_rel, duration,
    )
    result = {
        "pipeline": PIPELINE, "mode": PIPELINE, "video_file": final_rel,
        "url": "/api/gen/file/" + final_rel,
        "video_url": "/api/gen/file/" + final_rel,
        "text": str(payload.get("copy") or ""),
        "resolution": "1080p", "ratio": "9:16",
        "duration": round(final_duration, 3),
        "width": width, "height": height,
        "video_count": len(videos), "material_count": len(materials),
        "presenter_windows": windows,
        "child_jobs": {"videos": payload.get("video_job_ids"), "materials": payload.get("material_job_ids")},
        "verification": {
            "resolution": "1080x1920", "frame_rate": 30,
            "subtitle": "whisper" if not subtitle_error else "unavailable",
            "audio_source": "continuous_presenter_narration", "audio_stream": True,
            "duration_sync": True, "black_frame_check": True,
            "presenter_interval_seconds": "20-30", "visible_source_labels": False,
        },
        "subtitle_retryable": bool(subtitle_error),
    }
    if subtitle_error:
        result["subtitle_error"] = subtitle_error
    if persist_state:
        persist_state("completed", plain_video_file=rel, subtitle_video_file=final_rel,
                      subtitle_error=subtitle_error)
    return result
