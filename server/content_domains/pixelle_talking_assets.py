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
import time
import uuid
from contextlib import closing

from PIL import Image, UnidentifiedImageError


BASE = pathlib.Path(__file__).resolve().parents[1]
DB_PATH = str(BASE / "content_jobs.db")
OUT_DIR = pathlib.Path(os.environ.get("CONTENT_OUT", str(BASE / "content_out")))
TTL = 24 * 60 * 60
MAX_IMAGE_BYTES = 12 * 1024 * 1024
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


def _connect(db_path=None):
    connection = sqlite3.connect(str(db_path or DB_PATH), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    return connection


def init_db(db_path=None, out_dir=None):
    root = pathlib.Path(out_dir or OUT_DIR) / "pixelle_avatar"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass
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
        raise ValueError("人物图片解码后不能超过 12 MiB")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("人物图片编码无效") from exc
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise ValueError("人物图片解码后不能超过 12 MiB")
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


def _safe_avatar_path(file_name, out_dir=None):
    if pathlib.PurePath(str(file_name or "")).name != str(file_name or ""):
        raise LookupError("人物图片不存在或已失效")
    root = _avatar_root(out_dir).resolve()
    candidate = (root / str(file_name)).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise LookupError("人物图片不存在或已失效") from exc
    return candidate


def store_avatar(username: str, data_url: str) -> dict:
    username = _require_username(username)
    mime, raw = _decode_avatar(data_url)
    init_db()
    now = int(time.time())
    opaque = uuid.uuid4().hex
    asset_id = "local_avatar_" + opaque
    extension = MIME_EXTENSIONS[mime]
    file_name = opaque + extension
    root = _avatar_root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp_path = root / ("." + opaque + ".tmp")
    final_path = root / file_name
    descriptor = os.open(str(temp_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, final_path)
        try:
            os.chmod(final_path, 0o600)
        except OSError:
            pass
        digest = hashlib.sha256(raw).hexdigest()
        with closing(_connect()) as connection:
            connection.execute("""INSERT INTO pixelle_avatar_assets(
                id,username,file_path,mime,sha256,bytes,created_at,expires_at
            ) VALUES(?,?,?,?,?,?,?,?)""", (
                asset_id, username, file_name, mime, digest, len(raw), now, now + TTL,
            ))
            connection.commit()
    except Exception:
        temp_path.unlink(missing_ok=True)
        final_path.unlink(missing_ok=True)
        raise
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
        path = _safe_avatar_path(row["file_path"])
        if not path.is_file():
            raise LookupError("人物图片不存在或已失效")
        return _row_to_avatar(row)


def resolve_avatar_path(username: str, asset_id: str) -> pathlib.Path:
    """Resolve an owned asset for internal upload; never return this from HTTP APIs."""
    username = _require_username(username)
    init_db()
    with closing(_connect()) as connection:
        row = _owned_avatar(connection, username, asset_id, allow_retained=True)
        path = _safe_avatar_path(row["file_path"])
        if not path.is_file():
            raise LookupError("人物图片不存在或已失效")
        return path


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
        job = connection.execute(
            "SELECT username FROM jobs WHERE id=?", (job_id,)
        ).fetchone() if jobs_table else None
        if not job or str(job["username"] or "") != username:
            raise LookupError("付费任务不存在或不属于当前用户")
        connection.execute("""UPDATE pixelle_talking_plans
            SET job_id=?,updated_at=? WHERE id=? AND username=?
              AND status='consumed' AND (job_id IS NULL OR job_id=?)""",
            (job_id, now, plan_id, username, job_id))
        connection.commit()
        return _row_to_plan(_owned_plan(connection, username, plan_id))


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
            _safe_avatar_path(file_name, out_dir).unlink(missing_ok=True)
        except (LookupError, OSError):
            pass
    return {"plans": len(delete_plan_ids), "avatars": len(files_to_delete)}
