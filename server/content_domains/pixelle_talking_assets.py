# -*- coding: utf-8 -*-
"""Owner-scoped Pixelle talking plans and short-lived avatar images."""

import base64
import binascii
import hashlib
import io
import json
import os
import pathlib
import re
import sqlite3
import stat
import threading
import time
import uuid
from contextlib import closing, contextmanager

from PIL import Image, UnidentifiedImageError

from . import error_contract


BASE = pathlib.Path(__file__).resolve().parents[1]
DB_PATH = str(BASE / "content_jobs.db")
OUT_DIR = pathlib.Path(os.environ.get("CONTENT_OUT", str(BASE / "content_out")))
TTL = 24 * 60 * 60
MAX_IMAGE_BYTES = 12 * 1024 * 1024


def _bounded_env_int(name, default, minimum, maximum):
    try:
        value = int(os.environ.get(name, str(default)) or default)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


MAX_ACTIVE_AVATARS_PER_USER = _bounded_env_int(
    "PIXELLE_AVATAR_MAX_ACTIVE_PER_USER", 20, 1, 100)
MAX_ACTIVE_AVATAR_BYTES_PER_USER = _bounded_env_int(
    "PIXELLE_AVATAR_MAX_BYTES_PER_USER", 64 * 1024 * 1024,
    MAX_IMAGE_BYTES, 1024 * 1024 * 1024)
AVATAR_UPLOAD_RATE_MAX_REQUESTS = _bounded_env_int(
    "PIXELLE_AVATAR_UPLOADS_PER_MINUTE", 6, 1, 60)
AVATAR_UPLOAD_RATE_WINDOW_SECONDS = 60.0
_AVATAR_UPLOAD_RATE_REQUESTS = {}
_AVATAR_UPLOAD_RATE_LOCK = threading.Lock()
TERMINAL_JOB_STATES = frozenset({"done", "error", "failed", "cancelled", "canceled"})
ACTIVE_JOB_STATES = frozenset({"pending", "running"})
PLAN_ID_RE = re.compile(r"^talking_plan_[0-9a-f]{32}$")
ASSET_ID_RE = re.compile(r"^local_avatar_[0-9a-f]{32}$")
DATA_URL_RE = re.compile(
    r"^data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/]*={0,2})$",
    re.ASCII,
)
MIME_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}
PIL_FORMATS = {
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/webp": "WEBP",
}
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
VALID_PAID_JOB_STATES = frozenset({"pending", "running", "done"})


class AvatarUploadLimited(Exception):
    """Public-safe owner upload boundary."""


class AvatarRateLimited(AvatarUploadLimited):
    pass


class AvatarQuotaExceeded(AvatarUploadLimited):
    pass


def check_avatar_upload_rate_limit(username, now=None):
    owner = _require_username(username)
    stamp = float(time.time() if now is None else now)
    with _AVATAR_UPLOAD_RATE_LOCK:
        recent = [item for item in _AVATAR_UPLOAD_RATE_REQUESTS.get(owner, [])
                  if stamp - item < AVATAR_UPLOAD_RATE_WINDOW_SECONDS]
        if len(recent) >= AVATAR_UPLOAD_RATE_MAX_REQUESTS:
            _AVATAR_UPLOAD_RATE_REQUESTS[owner] = recent
            raise AvatarRateLimited("avatar upload rate limited")
        recent.append(stamp)
        _AVATAR_UPLOAD_RATE_REQUESTS[owner] = recent


def _connect(db_path=None):
    connection = sqlite3.connect(str(db_path or DB_PATH), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    return connection


def init_db(db_path=None, out_dir=None):
    root = _ensure_avatar_root(out_dir, create=True)
    try:
        os.chmod(root, 0o700, follow_symlinks=False)
    except (OSError, NotImplementedError):
        pass
    _ensure_avatar_root(out_dir)
    with closing(_connect(db_path)) as connection:
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS pixelle_talking_plans(
              id TEXT PRIMARY KEY,
              username TEXT NOT NULL,
              source_hash TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              status TEXT NOT NULL CHECK(status IN ('active','consumed')),
              job_id INTEGER,
              created_at INTEGER NOT NULL,
              updated_at INTEGER NOT NULL,
              consumed_at INTEGER,
              expires_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pixelle_avatar_assets(
              id TEXT PRIMARY KEY,
              username TEXT NOT NULL,
              file_path TEXT NOT NULL,
              mime TEXT NOT NULL,
              sha256 TEXT NOT NULL,
              bytes INTEGER NOT NULL,
              created_at INTEGER NOT NULL,
              expires_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pixelle_talking_plan_avatars(
              plan_id TEXT NOT NULL,
              asset_id TEXT NOT NULL,
              PRIMARY KEY(plan_id, asset_id),
              FOREIGN KEY(plan_id) REFERENCES pixelle_talking_plans(id) ON DELETE CASCADE,
              FOREIGN KEY(asset_id) REFERENCES pixelle_avatar_assets(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_pixelle_talking_plan_owner
              ON pixelle_talking_plans(username, id);
            CREATE INDEX IF NOT EXISTS idx_pixelle_talking_plan_expiry
              ON pixelle_talking_plans(status, expires_at);
            CREATE INDEX IF NOT EXISTS idx_pixelle_talking_plan_job
              ON pixelle_talking_plans(job_id);
            CREATE INDEX IF NOT EXISTS idx_pixelle_avatar_owner
              ON pixelle_avatar_assets(username, id);
            CREATE INDEX IF NOT EXISTS idx_pixelle_avatar_expiry
              ON pixelle_avatar_assets(expires_at);
        """)
        connection.commit()


def _require_username(username):
    value = str(username or "").strip()
    if not value or len(value) > 255:
        raise ValueError("缺少有效的用户")
    return value


def _canonical_json(value):
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("分镜方案不是有效 JSON") from exc


def _normalize_snapshot(source, scenes):
    if not isinstance(source, dict):
        raise ValueError("方案来源必须是对象")
    if not isinstance(scenes, list) or not scenes:
        raise ValueError("分镜方案不能为空")
    normalized = []
    for index, scene in enumerate(scenes, 1):
        if not isinstance(scene, dict):
            raise ValueError("分镜必须是对象")
        item = dict(scene)
        text = str(item.get("text") or "").strip()
        if not text:
            raise ValueError("分镜文本不能为空")
        item["text"] = text
        item["scene_id"] = "scene_%02d" % index
        normalized.append(item)
    snapshot = {"source": dict(source), "scenes": normalized}
    payload_json = _canonical_json(snapshot)
    return snapshot, payload_json, hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


def _row_to_plan(row):
    snapshot = json.loads(row["payload_json"])
    return {
        "plan_id": row["id"],
        "source_hash": row["source_hash"],
        "source": snapshot["source"],
        "scenes": snapshot["scenes"],
        "status": row["status"],
        "job_id": row["job_id"],
        "created_at": int(row["created_at"]),
        "expires_at": int(row["expires_at"]),
        "consumed_at": row["consumed_at"],
    }


def create_plan(username: str, source: dict, scenes: list[dict]) -> dict:
    username = _require_username(username)
    _snapshot, payload_json, source_hash = _normalize_snapshot(source, scenes)
    now = int(time.time())
    plan_id = "talking_plan_" + uuid.uuid4().hex
    init_db()
    with closing(_connect()) as connection:
        connection.execute("""INSERT INTO pixelle_talking_plans(
            id,username,source_hash,payload_json,status,job_id,
            created_at,updated_at,consumed_at,expires_at
        ) VALUES(?,?,?,?, 'active',NULL,?,?,NULL,?)""",
        (plan_id, username, source_hash, payload_json, now, now, now + TTL))
        connection.commit()
        row = connection.execute(
            "SELECT * FROM pixelle_talking_plans WHERE id=?", (plan_id,)).fetchone()
    return _row_to_plan(row)


def _owned_plan(connection, username, plan_id):
    if not PLAN_ID_RE.fullmatch(str(plan_id or "")):
        raise LookupError("分镜方案不存在或已失效")
    row = connection.execute(
        "SELECT * FROM pixelle_talking_plans WHERE id=? AND username=?",
        (plan_id, username),
    ).fetchone()
    if not row:
        raise LookupError("分镜方案不存在或已失效")
    return row


def get_plan(username: str, plan_id: str) -> dict:
    username = _require_username(username)
    init_db()
    now = int(time.time())
    with closing(_connect()) as connection:
        row = _owned_plan(connection, username, plan_id)
        if row["status"] == "active" and int(row["expires_at"]) <= now:
            raise LookupError("分镜方案不存在或已失效")
        return _row_to_plan(row)


def consume_plan(username: str, plan_id: str, expected_hash: str) -> dict:
    username = _require_username(username)
    expected_hash = str(expected_hash or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        raise ValueError("分镜方案摘要不匹配")
    init_db()
    now = int(time.time())
    with closing(_connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = _owned_plan(connection, username, plan_id)
        if row["source_hash"] != expected_hash:
            raise ValueError("分镜方案摘要不匹配")
        if row["status"] == "active" and int(row["expires_at"]) <= now:
            raise LookupError("分镜方案不存在或已失效")
        if row["status"] == "active":
            connection.execute("""UPDATE pixelle_talking_plans
                SET status='consumed', consumed_at=?, updated_at=?
                WHERE id=? AND username=? AND status='active'""",
                (now, now, plan_id, username))
        connection.commit()
        row = _owned_plan(connection, username, plan_id)
        return _row_to_plan(row)


def _detect_mime(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def _decode_avatar(data_url):
    match = DATA_URL_RE.fullmatch(str(data_url or ""))
    if not match:
        raise ValueError("人物图片格式无效，只支持 JPG、PNG 或 WebP")
    mime, encoded = match.groups()
    if len(encoded) > ((MAX_IMAGE_BYTES + 2) // 3) * 4 + 4:
        raise error_contract.RequestBodyTooLarge(
            "人物图片解码后不能超过 12 MiB")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("人物图片编码无效") from exc
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise error_contract.RequestBodyTooLarge(
            "人物图片解码后不能超过 12 MiB")
    if _detect_mime(raw) != mime:
        raise ValueError("人物图片内容与声明格式不一致")
    try:
        with Image.open(io.BytesIO(raw)) as image:
            if image.format != PIL_FORMATS[mime] or image.width < 1 or image.height < 1:
                raise ValueError("人物图片无法解码")
            image.verify()
    except ValueError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError) as exc:
        raise ValueError("人物图片无法解码") from exc
    return mime, raw


def _avatar_root(out_dir=None):
    return pathlib.Path(out_dir or OUT_DIR) / "pixelle_avatar"


def _path_is_reparse(_path, metadata=None):
    metadata = metadata if metadata is not None else os.lstat(_path)
    return bool(
        getattr(metadata, "st_file_attributes", 0)
        & FILE_ATTRIBUTE_REPARSE_POINT
    )


def _same_file_identity(left, right):
    return (int(left.st_dev), int(left.st_ino)) == (
        int(right.st_dev), int(right.st_ino))


def _ensure_avatar_root(out_dir=None, create=False):
    root = _avatar_root(out_dir)
    if create:
        try:
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            raise RuntimeError("avatar storage root is unavailable") from exc
    try:
        metadata = os.lstat(root)
    except OSError as exc:
        raise RuntimeError("avatar storage root is unavailable") from exc
    if (stat.S_ISLNK(metadata.st_mode)
            or _path_is_reparse(root, metadata)
            or not stat.S_ISDIR(metadata.st_mode)):
        raise RuntimeError("avatar storage root is unsafe")
    return root


def _avatar_candidate(file_name, out_dir=None):
    file_name = str(file_name or "")
    if (not file_name or pathlib.PurePath(file_name).name != file_name
            or file_name in {".", ".."}):
        raise LookupError("avatar is unavailable")
    return _ensure_avatar_root(out_dir) / file_name


def _require_regular_file(path, metadata):
    if (stat.S_ISLNK(metadata.st_mode)
            or _path_is_reparse(path, metadata)
            or not stat.S_ISREG(metadata.st_mode)):
        raise LookupError("avatar is unavailable")


@contextmanager
def _open_avatar_descriptor(file_name, out_dir=None):
    root = _ensure_avatar_root(out_dir)
    root_before = os.lstat(root)
    path = _avatar_candidate(file_name, out_dir)
    try:
        path_before = os.lstat(path)
        _require_regular_file(path, path_before)
    except OSError as exc:
        raise LookupError("avatar is unavailable") from exc
    flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0)
             | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    descriptor = None
    try:
        descriptor = os.open(str(path), flags)
        opened = os.fstat(descriptor)
        _require_regular_file(path, opened)
        path_after = os.lstat(path)
        root_after = os.lstat(root)
        _require_regular_file(path, path_after)
        if (not _same_file_identity(path_before, opened)
                or not _same_file_identity(opened, path_after)
                or not _same_file_identity(root_before, root_after)
                or _path_is_reparse(root, root_after)
                or not stat.S_ISDIR(root_after.st_mode)):
            raise LookupError("avatar is unavailable")
        yield descriptor
    except LookupError:
        raise
    except OSError as exc:
        raise LookupError("avatar is unavailable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_avatar_row(row, out_dir=None):
    expected_bytes = int(row["bytes"])
    with _open_avatar_descriptor(row["file_path"], out_dir) as descriptor:
        chunks = []
        remaining = min(MAX_IMAGE_BYTES, expected_bytes) + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    data = b"".join(chunks)
    if (len(data) != expected_bytes
            or len(data) > MAX_IMAGE_BYTES
            or hashlib.sha256(data).hexdigest() != str(row["sha256"])):
        raise LookupError("avatar is unavailable")
    return data


def store_avatar(username: str, data_url: str) -> dict:
    username = _require_username(username)
    mime, raw = _decode_avatar(data_url)
    init_db()
    now = int(time.time())
    opaque = uuid.uuid4().hex
    asset_id = "local_avatar_" + opaque
    extension = MIME_EXTENSIONS[mime]
    file_name = opaque + extension
    root = _ensure_avatar_root(create=True)
    temp_path = root / ("." + opaque + ".tmp")
    final_path = root / file_name
    expired_files = []
    quota_error = None
    try:
        with closing(_connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            expired = connection.execute("""SELECT a.id,a.file_path
                FROM pixelle_avatar_assets AS a
                WHERE a.expires_at<=?
                  AND NOT EXISTS(
                    SELECT 1 FROM pixelle_talking_plan_avatars AS r
                    WHERE r.asset_id=a.id
                  )""", (now,)).fetchall()
            if expired:
                connection.executemany(
                    "DELETE FROM pixelle_avatar_assets WHERE id=?",
                    [(row["id"],) for row in expired],
                )
                expired_files = [row["file_path"] for row in expired]
            usage = connection.execute("""SELECT COUNT(*) AS item_count,
                    COALESCE(SUM(bytes),0) AS total_bytes
                FROM pixelle_avatar_assets
                WHERE username=? AND expires_at>?""", (username, now)).fetchone()
            if int(usage["item_count"]) >= MAX_ACTIVE_AVATARS_PER_USER:
                quota_error = AvatarQuotaExceeded(
                    "avatar asset count quota exceeded")
            elif (int(usage["total_bytes"]) + len(raw)
                    > MAX_ACTIVE_AVATAR_BYTES_PER_USER):
                quota_error = AvatarQuotaExceeded(
                    "avatar asset byte quota exceeded")
            if quota_error is None:
                descriptor = os.open(
                    str(temp_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_path, final_path)
                _ensure_avatar_root()
                final_before = os.lstat(final_path)
                _require_regular_file(final_path, final_before)
                try:
                    os.chmod(final_path, 0o600, follow_symlinks=False)
                except (OSError, NotImplementedError):
                    pass
                final_after = os.lstat(final_path)
                _require_regular_file(final_path, final_after)
                if not _same_file_identity(final_before, final_after):
                    raise RuntimeError("avatar storage file changed")
                digest = hashlib.sha256(raw).hexdigest()
                connection.execute("""INSERT INTO pixelle_avatar_assets(
                    id,username,file_path,mime,sha256,bytes,created_at,expires_at
                ) VALUES(?,?,?,?,?,?,?,?)""", (
                    asset_id, username, file_name, mime, digest, len(raw), now,
                    now + TTL,
                ))
            connection.commit()
    except Exception:
        temp_path.unlink(missing_ok=True)
        final_path.unlink(missing_ok=True)
        raise
    for expired_file in expired_files:
        try:
            _avatar_candidate(expired_file).unlink(missing_ok=True)
        except (LookupError, OSError):
            pass
    if quota_error is not None:
        raise quota_error
    return {
        "asset_id": asset_id,
        "mime": mime,
        "sha256": digest,
        "bytes": len(raw),
        "extension": extension,
        "created_at": now,
        "expires_at": now + TTL,
    }


def _avatar_is_retained(connection, username, asset_id, now):
    plans = connection.execute("""SELECT p.status,p.job_id,p.expires_at
        FROM pixelle_talking_plan_avatars AS r
        JOIN pixelle_talking_plans AS p ON p.id=r.plan_id
        WHERE r.asset_id=? AND p.username=?""", (asset_id, username)).fetchall()
    jobs_table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'"
    ).fetchone()
    for plan in plans:
        if plan["status"] == "active":
            if int(plan["expires_at"]) > now:
                return True
            continue
        if plan["job_id"] is None:
            if int(plan["expires_at"]) > now:
                return True
            continue
        if jobs_table:
            job = connection.execute(
                "SELECT status FROM jobs WHERE id=?", (plan["job_id"],)).fetchone()
            if job and str(job["status"]).lower() in ACTIVE_JOB_STATES:
                return True
        elif int(plan["expires_at"]) > now:
            return True
    return False


def _owned_avatar(connection, username, asset_id, now=None, allow_retained=False):
    if not ASSET_ID_RE.fullmatch(str(asset_id or "")):
        raise LookupError("人物图片不存在或已失效")
    row = connection.execute(
        "SELECT * FROM pixelle_avatar_assets WHERE id=? AND username=?",
        (asset_id, username),
    ).fetchone()
    now = int(time.time() if now is None else now)
    if not row or (int(row["expires_at"]) <= now and not (
            allow_retained and _avatar_is_retained(
                connection, username, asset_id, now))):
        raise LookupError("人物图片不存在或已失效")
    return row


def _row_to_avatar(row):
    return {
        "asset_id": row["id"],
        "mime": row["mime"],
        "sha256": row["sha256"],
        "bytes": int(row["bytes"]),
        "extension": MIME_EXTENSIONS[row["mime"]],
        "created_at": int(row["created_at"]),
        "expires_at": int(row["expires_at"]),
    }


def get_avatar(username: str, asset_id: str) -> dict:
    username = _require_username(username)
    init_db()
    with closing(_connect()) as connection:
        row = _owned_avatar(connection, username, asset_id, allow_retained=True)
        _read_avatar_row(row)
        return _row_to_avatar(row)


def read_avatar(username: str, asset_id: str) -> dict:
    """Read an owned avatar from one verified descriptor without exposing a path."""
    username = _require_username(username)
    init_db()
    with closing(_connect()) as connection:
        row = _owned_avatar(connection, username, asset_id, allow_retained=True)
        result = _row_to_avatar(row)
        result["data"] = _read_avatar_row(row)
        return result


def bind_plan_avatars(username: str, plan_id: str, asset_ids) -> dict:
    username = _require_username(username)
    unique_ids = list(dict.fromkeys(str(item or "") for item in (asset_ids or [])))
    init_db()
    with closing(_connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        plan = _owned_plan(connection, username, plan_id)
        for asset_id in unique_ids:
            _owned_avatar(connection, username, asset_id)
        connection.execute(
            "DELETE FROM pixelle_talking_plan_avatars WHERE plan_id=?", (plan_id,))
        connection.executemany(
            "INSERT INTO pixelle_talking_plan_avatars(plan_id,asset_id) VALUES(?,?)",
            [(plan_id, asset_id) for asset_id in unique_ids],
        )
        connection.commit()
        return _row_to_plan(plan)


def bind_plan_job(username: str, plan_id: str, job_id: int) -> dict:
    username = _require_username(username)
    try:
        job_id = int(job_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("付费任务 ID 无效") from exc
    if job_id <= 0:
        raise ValueError("付费任务 ID 无效")
    init_db()
    now = int(time.time())
    with closing(_connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = _owned_plan(connection, username, plan_id)
        if row["status"] != "consumed":
            raise ValueError("分镜方案尚未消费")
        if row["job_id"] not in (None, job_id):
            raise ValueError("分镜方案已绑定其他任务")
        jobs_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'"
        ).fetchone()
        valid_states = ",".join("?" for _ in VALID_PAID_JOB_STATES)
        job = connection.execute(
            "SELECT id,payload FROM jobs WHERE id=? AND username=? "
            "AND kind='script_to_video' AND COALESCE(cost,0)>0 "
            "AND status IN (%s) AND COALESCE(refunded,0)=0 "
            "AND COALESCE(owner,'content')='content'" % valid_states,
            (job_id, username) + tuple(sorted(VALID_PAID_JOB_STATES)),
        ).fetchone() if jobs_table else None
        if not job:
            raise LookupError("付费任务不存在或不属于当前用户")
        try:
            paid_payload = json.loads(job["payload"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            paid_payload = None
        talking = (paid_payload.get("talking_material")
                   if isinstance(paid_payload, dict) else None)
        if (not isinstance(talking, dict)
                or paid_payload.get("pipeline") != "pixelle"
                or talking.get("enabled") is not True
                or talking.get("plan_id") != plan_id
                or talking.get("source_hash") != row["source_hash"]):
            raise LookupError("付费任务与分镜方案不匹配")
        connection.execute("""UPDATE pixelle_talking_plans
            SET job_id=?,updated_at=? WHERE id=? AND username=?
              AND status='consumed' AND (job_id IS NULL OR job_id=?)""",
            (job_id, now, plan_id, username, job_id))
        connection.commit()
        return _row_to_plan(_owned_plan(connection, username, plan_id))


def consume_and_bind_paid_plan(connection, username: str, plan_id: str,
                               expected_hash: str, job_id: int) -> None:
    """Atomically consume a confirmed plan inside the paid job transaction."""
    username = _require_username(username)
    expected_hash = str(expected_hash or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        raise ValueError("分镜方案摘要不匹配")
    row = _owned_plan(connection, username, plan_id)
    now = int(time.time())
    if row["source_hash"] != expected_hash:
        raise ValueError("分镜方案摘要不匹配")
    if row["status"] == "active" and int(row["expires_at"]) <= now:
        raise LookupError("分镜方案不存在或已失效")
    if row["job_id"] not in (None, int(job_id)):
        raise ValueError("分镜方案已绑定其他任务")
    job = connection.execute(
        "SELECT id,payload FROM jobs WHERE id=? AND username=? "
        "AND kind='script_to_video' AND COALESCE(cost,0)>0 "
        "AND status='pending' AND COALESCE(refunded,0)=0 "
        "AND COALESCE(owner,'content')='content'",
        (int(job_id), username),
    ).fetchone()
    if not job:
        raise LookupError("付费任务不存在或不属于当前用户")
    try:
        paid_payload = json.loads(job["payload"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise LookupError("付费任务与分镜方案不匹配") from exc
    talking = paid_payload.get("talking_material")
    if (paid_payload.get("pipeline") != "pixelle"
            or not isinstance(talking, dict)
            or talking.get("enabled") is not True
            or talking.get("plan_id") != plan_id
            or talking.get("source_hash") != expected_hash):
        raise LookupError("付费任务与分镜方案不匹配")
    connection.execute(
        """UPDATE pixelle_talking_plans
           SET status='consumed',job_id=?,consumed_at=COALESCE(consumed_at,?),updated_at=?
           WHERE id=? AND username=? AND (job_id IS NULL OR job_id=?)""",
        (int(job_id), now, now, plan_id, username, int(job_id)),
    )


def cleanup_expired(db_path=None, out_dir=None, now=None):
    """Release expired previews while retaining consumed plans for paid recovery."""
    db_path = str(db_path or DB_PATH)
    out_dir = pathlib.Path(out_dir or OUT_DIR)
    now = int(time.time() if now is None else now)
    init_db(db_path, out_dir)
    files_to_delete = []
    with closing(_connect(db_path)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        plans = connection.execute(
            "SELECT id,status,job_id,expires_at FROM pixelle_talking_plans"
        ).fetchall()
        delete_plan_ids = []
        jobs_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'"
        ).fetchone()
        for plan in plans:
            if plan["status"] == "active":
                if int(plan["expires_at"]) <= now:
                    delete_plan_ids.append(plan["id"])
                continue
            job_id = plan["job_id"]
            if job_id is None:
                if int(plan["expires_at"]) <= now:
                    delete_plan_ids.append(plan["id"])
                continue
            job = None
            if jobs_table:
                job = connection.execute(
                    "SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if job and str(job["status"]).lower() in ACTIVE_JOB_STATES:
                continue
            if not job and int(plan["expires_at"]) > now:
                continue
            delete_plan_ids.append(plan["id"])
        if delete_plan_ids:
            connection.executemany(
                "DELETE FROM pixelle_talking_plans WHERE id=?",
                [(plan_id,) for plan_id in delete_plan_ids],
            )
        avatars = connection.execute("""SELECT a.id,a.file_path
            FROM pixelle_avatar_assets AS a
            WHERE a.expires_at<=?
              AND NOT EXISTS(
                SELECT 1 FROM pixelle_talking_plan_avatars AS r
                WHERE r.asset_id=a.id
              )""", (now,)).fetchall()
        if avatars:
            connection.executemany(
                "DELETE FROM pixelle_avatar_assets WHERE id=?",
                [(row["id"],) for row in avatars],
            )
            files_to_delete = [row["file_path"] for row in avatars]
        connection.commit()
    for file_name in files_to_delete:
        try:
            _avatar_candidate(file_name, out_dir).unlink(missing_ok=True)
        except (LookupError, OSError):
            pass
    return {"plans": len(delete_plan_ids), "avatars": len(files_to_delete)}
