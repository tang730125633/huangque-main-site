"""Video versions and review state for short-drama shots."""

import base64
import hashlib
import json
import mimetypes
import sqlite3
import subprocess
import time
import uuid
from contextlib import closing


VIDEO_STAGES = {"video_review", "assembly_review", "completed"}
CHANNELS = {"micro", "omni", "grok"}
QUOTE_TTL_SECONDS = 300
REQUEST_FIELDS = {
    "project_id", "revision", "shot_id", "channel", "model", "prompt",
    "resolution", "upscale", "generate_audio",
}


_SCHEMA = """
CREATE TABLE IF NOT EXISTS short_drama_video_assets (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  shot_id TEXT NOT NULL REFERENCES short_drama_shots(id) ON DELETE CASCADE,
  current_version INTEGER,
  locked INTEGER NOT NULL DEFAULT 0 CHECK (locked IN (0,1)),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(project_id, shot_id)
);
CREATE TABLE IF NOT EXISTS short_drama_video_versions (
  id TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL REFERENCES short_drama_video_assets(id) ON DELETE CASCADE,
  version INTEGER NOT NULL CHECK (version >= 1),
  job_id INTEGER NOT NULL UNIQUE,
  channel TEXT NOT NULL CHECK (channel IN ('micro','omni','grok')),
  model TEXT NOT NULL,
  prompt TEXT NOT NULL,
  duration INTEGER NOT NULL CHECK (duration IN (5,10)),
  ratio TEXT NOT NULL CHECK (ratio IN ('9:16','16:9')),
  resolution TEXT NOT NULL,
  file TEXT NOT NULL,
  url TEXT NOT NULL,
  cost INTEGER NOT NULL DEFAULT 0 CHECK (cost >= 0),
  created_at INTEGER NOT NULL,
  UNIQUE(asset_id, version)
);
CREATE TABLE IF NOT EXISTS short_drama_video_jobs (
  id TEXT PRIMARY KEY,
  username TEXT NOT NULL,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  shot_id TEXT NOT NULL REFERENCES short_drama_shots(id) ON DELETE CASCADE,
  job_id INTEGER NOT NULL UNIQUE,
  idempotency_key TEXT NOT NULL,
  quoted_cost INTEGER NOT NULL CHECK (quoted_cost >= 0),
  channel TEXT NOT NULL CHECK (channel IN ('micro','omni','grok')),
  model TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending','running','done','failed')),
  error TEXT NOT NULL DEFAULT '',
  refunded INTEGER NOT NULL DEFAULT 0 CHECK (refunded IN (0,1,2)),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(username, idempotency_key)
);
CREATE TABLE IF NOT EXISTS short_drama_video_quotes (
  token TEXT PRIMARY KEY,
  username TEXT NOT NULL,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  shot_id TEXT NOT NULL REFERENCES short_drama_shots(id) ON DELETE CASCADE,
  request_hash TEXT NOT NULL,
  cost INTEGER NOT NULL CHECK (cost >= 0),
  expires_at INTEGER NOT NULL,
  consumed_idempotency_key TEXT,
  consumed_job_id INTEGER,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_short_drama_video_jobs_project
  ON short_drama_video_jobs(username, project_id, status, updated_at);
CREATE INDEX IF NOT EXISTS idx_short_drama_video_quotes_lookup
  ON short_drama_video_quotes(username, project_id, shot_id, expires_at);
CREATE TRIGGER IF NOT EXISTS short_drama_video_assets_project_shot_guard
BEFORE INSERT ON short_drama_video_assets
FOR EACH ROW WHEN NOT EXISTS (
  SELECT 1 FROM short_drama_shots
  WHERE id=NEW.shot_id AND project_id=NEW.project_id
)
BEGIN
  SELECT RAISE(ABORT, 'short drama video shot must belong to project');
END;
CREATE TRIGGER IF NOT EXISTS short_drama_video_jobs_project_shot_guard
BEFORE INSERT ON short_drama_video_jobs
FOR EACH ROW WHEN NOT EXISTS (
  SELECT 1 FROM short_drama_shots
  WHERE id=NEW.shot_id AND project_id=NEW.project_id
)
BEGIN
  SELECT RAISE(ABORT, 'short drama video job shot must belong to project');
END;
"""


def init_db(db_factory):
    with closing(db_factory()) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_SCHEMA)
        conn.commit()


def _json_object(raw, message):
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        raise ValueError(message)
    if not isinstance(value, dict):
        raise ValueError(message)
    return value


def normalize_request(body, require_quote=False):
    expected = REQUEST_FIELDS | ({"quote_token"} if require_quote else set())
    if not isinstance(body, dict) or set(body) != expected:
        raise ValueError("短剧视频请求字段不正确")
    project_id = str(body.get("project_id") or "").strip()
    shot_id = str(body.get("shot_id") or "").strip()
    channel = str(body.get("channel") or "").strip().lower()
    prompt = str(body.get("prompt") or "").strip()
    model = str(body.get("model") or "").strip()
    resolution = str(body.get("resolution") or "").strip().lower()
    if not project_id or not shot_id:
        raise ValueError("短剧项目和镜头不能为空")
    if type(body.get("revision")) is not int or body["revision"] < 1:
        raise ValueError("短剧项目版本无效")
    if channel not in CHANNELS:
        raise ValueError("短剧视频仅支持 Seedance、Omni 和 Grok")
    if not prompt or len(prompt) > 8000:
        raise ValueError("短剧视频提示词不能为空且不超过 8000 字")
    if type(body.get("upscale")) is not bool or type(body.get("generate_audio")) is not bool:
        raise ValueError("短剧视频音频或超清选项无效")
    if channel == "micro":
        if resolution not in {"480p", "720p"}:
            raise ValueError("Seedance 仅支持 480p 或 720p")
        if body["upscale"] and resolution != "480p":
            raise ValueError("AI 超清必须先生成 Seedance 480p")
    elif channel == "omni":
        if resolution != "720p" or body["upscale"]:
            raise ValueError("Omni 当前固定 720p 且不使用 Seedance 超清")
    elif resolution not in {"480p", "720p"} or body["upscale"]:
        raise ValueError("Grok 当前支持 480p/720p 且不使用 Seedance 超清")
    cleaned = {
        "project_id": project_id,
        "revision": body["revision"],
        "shot_id": shot_id,
        "channel": channel,
        "model": model,
        "prompt": prompt,
        "resolution": resolution,
        "upscale": body["upscale"],
        "generate_audio": body["generate_audio"],
    }
    if require_quote:
        token = str(body.get("quote_token") or "").strip()
        if not token:
            raise ValueError("短剧视频 quote 必填")
        cleaned["quote_token"] = token
    return cleaned


def _authorized_project(conn, username, project_id, access=None, write=False):
    from . import short_drama_production
    return short_drama_production._authorized_project(
        conn, username, project_id, access, write=write
    )


def _locked_still(conn, project_id, shot_id):
    row = conn.execute(
        "SELECT v.version,v.file,v.url FROM short_drama_assets a "
        "JOIN short_drama_asset_versions v "
        "ON v.asset_id=a.id AND v.version=a.current_version "
        "WHERE a.project_id=? AND a.shot_id=? AND a.type='still' "
        "AND a.locked=1 AND v.status='done'",
        (project_id, shot_id),
    ).fetchone()
    if not row:
        raise ValueError("请先锁定当前镜头的关键帧")
    return row


def _omni_inline_reference(file_name):
    from . import video as video_domain, video_gemini_omni
    path = video_domain._resolve_out_file(str(file_name or ""))
    if not path:
        raise ValueError("Omni 关键帧本地文件不存在")
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ValueError("Omni 关键帧本地文件无法读取") from error
    content_type = mimetypes.guess_type(path.name)[0] or ""
    if (not raw or len(raw) > video_gemini_omni.MAX_IMAGE_BYTES
            or content_type not in video_gemini_omni.IMAGE_MIMES):
        try:
            converted = subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path),
                "-frames:v", "1", "-f", "image2pipe", "-vcodec", "mjpeg",
                "-q:v", "3", "pipe:1",
            ], check=True, timeout=120, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except (FileNotFoundError, subprocess.CalledProcessError,
                subprocess.TimeoutExpired, OSError) as error:
            raise ValueError("Omni 关键帧无法转换为 JPEG") from error
        raw, content_type = converted.stdout, "image/jpeg"
    if not raw or len(raw) > video_gemini_omni.MAX_IMAGE_BYTES:
        raise ValueError("Omni 关键帧压缩后仍超过 8MB")
    return "data:%s;base64,%s" % (
        content_type, base64.b64encode(raw).decode("ascii")
    )


def _provider_payload(conn, project, shot, request):
    still = _locked_still(conn, project["id"], shot["id"])
    if request["channel"] == "omni":
        references = [_omni_inline_reference(still["file"])]
    else:
        reference = str(still["url"] or "").strip()
        if not reference.startswith(("https://", "http://")):
            from . import video as video_domain
            content_type = mimetypes.guess_type(str(still["file"] or ""))[0] or "image/jpeg"
            reference = video_domain.public_url(still["file"], content_type)
        if not str(reference).startswith(("https://", "http://")):
            raise ValueError("关键帧尚未转存到可供视频模型读取的安全地址")
        references = [reference]
    payload = {
        "channel": request["channel"],
        "prompt": request["prompt"],
        "ratio": project["ratio"],
        "duration": int(shot["duration"]),
        "resolution": request["resolution"],
        "reference_images": references,
        "generate_audio": request["generate_audio"],
        "upscale": request["upscale"],
    }
    if request["model"]:
        payload["model"] = request["model"]
    from . import video as video_domain
    payload = video_domain.validate_xiaole_video_payload(payload)
    descriptor = {
        "project_id": project["id"],
        "revision": int(project["revision"]),
        "shot_id": shot["id"],
        "still_version": int(still["version"]),
        "channel": payload["channel"],
        "model": payload.get("model") or "",
        "prompt": payload["prompt"],
        "ratio": payload["ratio"],
        "duration": int(payload["duration"]),
        "resolution": payload["resolution"],
        "upscale": bool(payload.get("upscale")),
        "generate_audio": bool(payload.get("generate_audio", True)),
    }
    return payload, descriptor


def _request_hash(descriptor):
    encoded = json.dumps(
        descriptor, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _reserved_points(conn, project_id):
    row = conn.execute(
        "SELECT COALESCE(SUM(v.quoted_cost),0) FROM short_drama_video_jobs v "
        "JOIN jobs j ON j.id=v.job_id AND j.username=v.username "
        "WHERE v.project_id=? AND j.status IN ('pending','running','done') "
        "AND NOT EXISTS (SELECT 1 FROM short_drama_video_versions x WHERE x.job_id=v.job_id)",
        (project_id,),
    ).fetchone()
    return int(row[0] or 0)


def _check_budget(conn, project, cost):
    reserved = _reserved_points(conn, project["id"])
    budget = int(project["point_budget"] or 0)
    if budget and int(project["spent_points"] or 0) + reserved + int(cost) > budget:
        from .short_drama import PointBudgetExceeded
        raise PointBudgetExceeded("短剧项目点数预算不足")
    return reserved


def _shot_and_project(conn, username, request, access=None, write=False):
    project = _authorized_project(
        conn, username, request["project_id"], access, write=write
    )
    if int(project["revision"]) != request["revision"]:
        from .short_drama import RevisionConflict
        raise RevisionConflict("项目已更新，请刷新后重试")
    if project["stage"] != "video_review":
        raise ValueError("短剧项目尚未进入视频确认阶段")
    shot = conn.execute(
        "SELECT * FROM short_drama_shots WHERE id=? AND project_id=?",
        (request["shot_id"], project["id"]),
    ).fetchone()
    if not shot:
        raise LookupError("短剧镜头不存在")
    active = conn.execute(
        "SELECT 1 FROM short_drama_video_jobs v JOIN jobs j ON j.id=v.job_id "
        "WHERE v.project_id=? AND v.shot_id=? AND j.status IN ('pending','running') LIMIT 1",
        (project["id"], shot["id"]),
    ).fetchone()
    if active:
        raise ValueError("当前镜头已有视频正在生成")
    locked = conn.execute(
        "SELECT locked FROM short_drama_video_assets WHERE project_id=? AND shot_id=?",
        (project["id"], shot["id"]),
    ).fetchone()
    if locked and locked[0]:
        raise ValueError("请先解锁当前视频版本再重试")
    return project, shot


def prepare_quote(db_factory, username, body, cost_of, access=None):
    request = normalize_request(body)
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        project, shot = _shot_and_project(conn, username, request, access, write=True)
        payload, descriptor = _provider_payload(conn, project, shot, request)
        cost = int(cost_of("xiaole_video", payload))
        if cost < 0:
            raise ValueError("短剧视频报价无效")
        _check_budget(conn, project, cost)
        token = uuid.uuid4().hex
        now = int(time.time())
        expires_at = now + QUOTE_TTL_SECONDS
        conn.execute(
            "INSERT INTO short_drama_video_quotes "
            "(token,username,project_id,shot_id,request_hash,cost,expires_at,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (token, username, project["id"], shot["id"],
             _request_hash(descriptor), cost, expires_at, now),
        )
        conn.commit()
        return {
            "quote_token": token,
            "cost": cost,
            "expires_at": expires_at,
            "project_id": project["id"],
            "shot_id": shot["id"],
            "channel": payload["channel"],
            "model": payload.get("model") or "",
            "duration": payload["duration"],
            "resolution": payload["resolution"],
        }


def prepare_submission(db_factory, username, body, access=None):
    request = normalize_request(body, require_quote=True)
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        project, shot = _shot_and_project(conn, username, request, access, write=True)
        payload, descriptor = _provider_payload(conn, project, shot, request)
        request_hash = _request_hash(descriptor)
        quote = conn.execute(
            "SELECT * FROM short_drama_video_quotes WHERE token=? AND username=? "
            "AND project_id=? AND shot_id=?",
            (request["quote_token"], username, project["id"], shot["id"]),
        ).fetchone()
        if not quote or quote["request_hash"] != request_hash:
            raise ValueError("短剧视频报价与当前请求不一致，请重新报价")
        if quote["expires_at"] < int(time.time()):
            raise ValueError("短剧视频报价已过期，请重新报价")
        if quote["consumed_idempotency_key"]:
            raise ValueError("短剧视频报价已使用")
        _check_budget(conn, project, int(quote["cost"]))
        return {
            "project": dict(project),
            "shot": dict(shot),
            "video_payload": payload,
            "quoted_cost": int(quote["cost"]),
            "quote_token": quote["token"],
            "request_hash": request_hash,
        }


def submitted_job_callback(db_factory, *, username, prepared, idempotency_key):
    def link(connection, job_id):
        record_submitted_job(
            connection, username=username, prepared=prepared,
            idempotency_key=idempotency_key, job_id=job_id,
        )
    return link


def record_submitted_job(conn, *, username, prepared, idempotency_key, job_id):
    conn.row_factory = sqlite3.Row
    now = int(time.time())
    project_id = prepared["project"]["id"]
    shot_id = prepared["shot"]["id"]
    quote = conn.execute(
        "SELECT * FROM short_drama_video_quotes WHERE token=? AND username=? "
        "AND project_id=? AND shot_id=?",
        (prepared["quote_token"], username, project_id, shot_id),
    ).fetchone()
    if (not quote or quote["request_hash"] != prepared["request_hash"]
            or int(quote["cost"]) != int(prepared["quoted_cost"])
            or quote["consumed_idempotency_key"]):
        raise ValueError("短剧视频报价不可用")
    conn.execute(
        "INSERT OR IGNORE INTO short_drama_video_assets "
        "(id,project_id,shot_id,created_at,updated_at) VALUES (?,?,?,?,?)",
        (str(uuid.uuid4()), project_id, shot_id, now, now),
    )
    payload = prepared["video_payload"]
    conn.execute(
        "INSERT INTO short_drama_video_jobs "
        "(id,username,project_id,shot_id,job_id,idempotency_key,quoted_cost,"
        "channel,model,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,'pending',?,?)",
        (str(uuid.uuid4()), username, project_id, shot_id, int(job_id),
         idempotency_key, int(prepared["quoted_cost"]), payload["channel"],
         payload.get("model") or "", now, now),
    )
    updated = conn.execute(
        "UPDATE short_drama_video_quotes SET consumed_idempotency_key=?,consumed_job_id=? "
        "WHERE token=? AND consumed_idempotency_key IS NULL",
        (idempotency_key, int(job_id), prepared["quote_token"]),
    )
    if updated.rowcount != 1:
        raise ValueError("短剧视频报价已使用")


def recover_submitted_response(db_factory, username, idempotency_key):
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT project_id,shot_id,job_id,quoted_cost FROM short_drama_video_jobs "
            "WHERE username=? AND idempotency_key=?",
            (username, idempotency_key),
        ).fetchone()
    if not row:
        return None
    return {
        "project_id": row["project_id"], "shot_id": row["shot_id"],
        "job_id": int(row["job_id"]), "cost": int(row["quoted_cost"]),
    }


def handle_generate(handler, db_factory, username, access, points_domain,
                    core_dependencies):
    """Submit one paid clip while keeping short-drama billing out of core."""
    endpoint = "/api/gen/short-drama/generate-video"
    feature_flags = core_dependencies["feature_flags"]
    security = core_dependencies["miniprogram_security"]
    clean_key = core_dependencies["_idempotency_key"]
    idem_begin = core_dependencies["_idempotency_begin"]
    idem_abort = core_dependencies["_idempotency_abort"]
    idem_complete = core_dependencies["_idempotency_complete"]
    submission_lock = core_dependencies["_submission_lock"]
    is_shutting_down = core_dependencies["is_shutting_down"]
    active_job_count = core_dependencies["_user_active_job_count"]
    max_active_jobs = core_dependencies["MAX_USER_ACTIVE_JOBS"]
    submit_limit = core_dependencies["_user_video_submit_limit"]
    jobs_store = core_dependencies["jobs_store"]
    service_owner = core_dependencies["SERVICE_OWNER"]
    enqueue_job = core_dependencies["enqueue_job"]
    reject_job = core_dependencies["_reject_pending_job"]
    from . import miniprogram_security, short_drama, upstream_guard, video

    try:
        feature_flags.require_enabled("xiaole_video")
        request = handler._json_body_strict()
        security.check_payload(request)
        idem_key = clean_key(handler.headers.get("Idempotency-Key"))
        if not idem_key:
            raise ValueError("短剧视频提交必须提供 Idempotency-Key")
    except feature_flags.FeatureDisabled as error:
        return handler._send(503, {"detail": str(error)})
    except miniprogram_security.ContentRejected as error:
        return handler._send(400, {
            "detail": str(error), "code": "content_rejected",
            "operation_terminal": True,
        })
    except miniprogram_security.SecurityUnavailable as error:
        return handler._send(503, {
            "detail": str(error), "code": "content_security_unavailable",
            "retry_after_ms": 5000,
        })
    except ValueError as error:
        short_drama._http_error(handler, error, operation_terminal=True)
        return

    state, response = idem_begin(username, endpoint, idem_key, request)
    if state == "replay":
        replay = dict(response or {})
        return handler._send(int(replay.pop("_http_status", 200)), replay)
    if state == "conflict":
        return handler._send(409, {
            "detail": "同一个 Idempotency-Key 不能用于不同请求",
            "code": "idempotency_conflict",
        })
    if state == "processing":
        recovered = recover_submitted_response(db_factory, username, idem_key)
        if recovered:
            recovered["points_left"] = points_domain.get_points(username)
            idem_complete(username, endpoint, idem_key, recovered)
            return handler._send(200, recovered)
        return handler._send(409, {
            "detail": "相同请求正在受理，请稍后查询",
            "code": "idempotency_in_progress", "retry_after_ms": 1000,
        })

    try:
        with submission_lock:
            if is_shutting_down():
                idem_abort(username, endpoint, idem_key)
                return handler._send(503, {
                    "detail": "服务正在更新，请稍等几秒后重试（未扣点）",
                    "code": "shutting_down", "retry_after_ms": 5000,
                })
            prepared = prepare_submission(
                db_factory, username, request, access
            )
            payload = prepared["video_payload"]
            blocked = upstream_guard.exhausted_reason("xiaole_video", payload)
            if blocked:
                idem_abort(username, endpoint, idem_key)
                return handler._send(503, {
                    "detail": blocked, "code": "upstream_exhausted",
                    "retry_after_ms": 60000,
                })
            cost = int(points_domain.cost_of("xiaole_video", payload))
            if cost != int(prepared["quoted_cost"]):
                idem_abort(username, endpoint, idem_key)
                return handler._send(409, {
                    "detail": "短剧视频价格已变化，请重新报价",
                    "code": "video_quote_changed",
                })
            limit = submit_limit("xiaole_video", payload, username, cost)
            if limit:
                idem_abort(username, endpoint, idem_key)
                return handler._send(429, limit)
            active = active_job_count(username)
            if active >= max_active_jobs:
                idem_abort(username, endpoint, idem_key)
                return handler._send(429, {
                    "detail": "您有 %d 个任务正在排队/生成，完成后再提交" % active,
                    "code": "active_job_cap", "active_jobs": active,
                    "max_active_jobs": max_active_jobs, "retry_after_ms": 4000,
                    "need": cost,
                })
            association = submitted_job_callback(
                db_factory, username=username, prepared=prepared,
                idempotency_key=idem_key,
            )
            try:
                job_id, points_left = jobs_store.create_paid_job(
                    db_factory, points_domain.deduct_points,
                    points_domain.refund_points, "xiaole_video", username,
                    cost, payload, service_owner, before_commit=association,
                    charge_transaction_key="job-charge:%s:%s:%s" % (
                        username, endpoint, idem_key
                    ),
                )
            except points_domain.AuthPointsError as error:
                idem_abort(username, endpoint, idem_key)
                return handler._send(
                    error.status if error.status in (402, 403) else 502,
                    points_domain.public_error_body(error, cost),
                )
            except jobs_store.PaidJobInsertError as error:
                failed = {
                    "detail": {
                        "refunded": "任务创建失败，点数已退回",
                        "queued": "任务创建失败，退款正在自动重试",
                    }.get(error.compensation, "任务创建失败，退款需人工核对"),
                    "submission_ref": error.submission_ref,
                }
                idem_complete(username, endpoint, idem_key,
                              dict(failed, _http_status=500))
                return handler._send(500, failed)
            try:
                video.record_video_pending_asset(job_id, username, payload)
            except Exception:
                reject_job(job_id, username, cost, "视频资产登记失败")
                failed = {"detail": "任务创建失败，退款正在自动处理",
                          "job_id": job_id}
                idem_complete(username, endpoint, idem_key,
                              dict(failed, _http_status=500))
                return handler._send(500, failed)
            if not enqueue_job(job_id, "xiaole_video", payload.get("mode")):
                reject_job(job_id, username, cost, "任务队列已满，请稍后再试")
                video.update_video_asset_phase(
                    job_id, "failed", status="failed",
                    error="任务队列已满，请稍后再试",
                )
                failed = {
                    "detail": "任务队列已满，请稍后再试", "code": "queue_full",
                    "retry_after_ms": 4000, "need": cost,
                }
                idem_complete(username, endpoint, idem_key,
                              dict(failed, _http_status=429))
                return handler._send(429, failed)
            result = {
                "project_id": prepared["project"]["id"],
                "shot_id": prepared["shot"]["id"], "job_id": job_id,
                "cost": cost, "points_left": points_left,
            }
            idem_complete(username, endpoint, idem_key, result)
            return handler._send(200, result)
    except (LookupError, PermissionError, ValueError,
            short_drama.RevisionConflict) as error:
        idem_abort(username, endpoint, idem_key)
        short_drama._http_error(handler, error, operation_terminal=True)
    except Exception:
        if not recover_submitted_response(db_factory, username, idem_key):
            idem_abort(username, endpoint, idem_key)
        raise


def _trusted_video_result(username, result):
    file_name = str(result.get("video_file") or "").strip()
    if not file_name:
        raise ValueError("视频任务没有返回本地成片")
    from . import video as video_domain
    if not video_domain._user_owns_output_file(username, file_name):
        raise ValueError("视频成片不属于当前用户")
    if not video_domain._resolve_out_file(file_name):
        raise ValueError("视频成片文件不存在")
    url = str(result.get("video_url") or "").strip()
    if not url:
        url = video_domain.public_url(file_name, "video/mp4", private=True)
    return file_name, url


def reconcile_jobs(conn, username, project_id):
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT v.*,j.status job_status,j.payload,j.result,j.error job_error,"
        "COALESCE(j.refunded,0) job_refunded FROM short_drama_video_jobs v "
        "JOIN jobs j ON j.id=v.job_id AND j.username=v.username AND j.kind='xiaole_video' "
        "WHERE v.project_id=? ORDER BY v.created_at,v.id",
        (project_id,),
    ).fetchall()
    now = int(time.time())
    for link in rows:
        status = link["job_status"]
        normalized = status if status in {"pending", "running", "done"} else "failed"
        conn.execute(
            "UPDATE short_drama_video_jobs SET status=?,error=?,refunded=?,updated_at=? WHERE id=?",
            (normalized, str(link["job_error"] or "")[:300],
             int(link["job_refunded"] or 0), now, link["id"]),
        )
        if status != "done":
            continue
        existing = conn.execute(
            "SELECT 1 FROM short_drama_video_versions WHERE job_id=?",
            (link["job_id"],),
        ).fetchone()
        if existing:
            continue
        payload = _json_object(link["payload"], "视频任务参数无效")
        result = _json_object(link["result"], "视频任务结果无效")
        shot = conn.execute(
            "SELECT duration FROM short_drama_shots WHERE id=? AND project_id=?",
            (link["shot_id"], project_id),
        ).fetchone()
        project = conn.execute(
            "SELECT ratio FROM short_drama_projects WHERE id=?", (project_id,)
        ).fetchone()
        if not shot or not project:
            raise ValueError("短剧视频任务归属无效")
        if int(payload.get("duration") or 0) != int(shot["duration"]):
            raise ValueError("视频任务时长与镜头不一致")
        if payload.get("ratio") != project["ratio"]:
            raise ValueError("视频任务比例与项目不一致")
        file_name, url = _trusted_video_result(link["username"], result)
        asset = conn.execute(
            "SELECT * FROM short_drama_video_assets WHERE project_id=? AND shot_id=?",
            (project_id, link["shot_id"]),
        ).fetchone()
        if not asset:
            raise ValueError("短剧视频资产槽位不存在")
        version = int(conn.execute(
            "SELECT COALESCE(MAX(version),0)+1 FROM short_drama_video_versions WHERE asset_id=?",
            (asset["id"],),
        ).fetchone()[0])
        inserted = conn.execute(
            "INSERT OR IGNORE INTO short_drama_video_versions "
            "(id,asset_id,version,job_id,channel,model,prompt,duration,ratio,resolution,"
            "file,url,cost,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), asset["id"], version, int(link["job_id"]),
             link["channel"], result.get("model") or link["model"],
             payload.get("prompt") or "", int(shot["duration"]), project["ratio"],
             result.get("resolution") or payload.get("resolution") or "",
             file_name, url, int(link["quoted_cost"]), now),
        )
        if inserted.rowcount:
            conn.execute(
                "UPDATE short_drama_projects SET spent_points=spent_points+? WHERE id=?",
                (int(link["quoted_cost"]), project_id),
            )
            conn.execute(
                "UPDATE short_drama_video_assets SET current_version=COALESCE(current_version,?),"
                "updated_at=? WHERE id=?",
                (version, now, asset["id"]),
            )


def _model_options():
    from . import feature_flags, video as video_domain
    from . import video_gemini_omni, video_seedance, video_xai
    seedance = bool(
        feature_flags.is_enabled("seedance_video") and video_seedance.available()
    )
    return [
        {
            "channel": "micro", "label": "Seedance 480P + AI 超清",
            "model": video_seedance.SEEDANCE_MODEL, "resolution": "480p",
            "upscale": True, "enabled": seedance and video_domain.seedance_upscale_is_open(),
        },
        {
            "channel": "omni", "label": "Omni 720P",
            "model": video_gemini_omni.MODEL, "resolution": "720p",
            "upscale": False,
            "enabled": bool(feature_flags.is_enabled("omni_video") and video_gemini_omni.available()),
        },
        {
            "channel": "grok", "label": "Grok 720P",
            "model": "grok-imagine-video", "resolution": "720p",
            "upscale": False, "enabled": video_xai.available(),
        },
    ]


def _version_snapshot(row):
    url = row["url"]
    try:
        from . import cos
        if row["file"] and cos.enabled():
            url = cos.object_url(row["file"], private=True)
    except Exception:
        pass
    return {
        "id": row["id"], "version": int(row["version"]),
        "job_id": int(row["job_id"]), "channel": row["channel"],
        "model": row["model"], "prompt": row["prompt"],
        "duration": int(row["duration"]), "ratio": row["ratio"],
        "resolution": row["resolution"], "url": url,
        "cost": int(row["cost"]), "created_at": int(row["created_at"]),
    }


def build_snapshot(conn, project, username):
    reconcile_jobs(conn, username, project["id"])
    project = conn.execute(
        "SELECT * FROM short_drama_projects WHERE id=?", (project["id"],)
    ).fetchone()
    assets = {
        row["shot_id"]: row for row in conn.execute(
            "SELECT * FROM short_drama_video_assets WHERE project_id=?",
            (project["id"],),
        )
    }
    versions = {}
    for row in conn.execute(
        "SELECT v.* FROM short_drama_video_versions v "
        "JOIN short_drama_video_assets a ON a.id=v.asset_id WHERE a.project_id=? "
        "ORDER BY a.shot_id,v.version DESC",
        (project["id"],),
    ):
        versions.setdefault(row["asset_id"], []).append(_version_snapshot(row))
    latest_jobs = {}
    for row in conn.execute(
        "SELECT * FROM short_drama_video_jobs WHERE project_id=? ORDER BY created_at DESC,id DESC",
        (project["id"],),
    ):
        latest_jobs.setdefault(row["shot_id"], row)
    shots = []
    for shot in conn.execute(
        "SELECT * FROM short_drama_shots WHERE project_id=? ORDER BY sort_order,id",
        (project["id"],),
    ):
        asset = assets.get(shot["id"])
        job = latest_jobs.get(shot["id"])
        job_data = None
        if job:
            from . import video as video_domain
            phase = video_domain.get_video_job_phase(job["job_id"])
            job_data = {
                "job_id": int(job["job_id"]), "status": job["status"],
                "phase": phase or job["status"], "error": job["error"],
                "refunded": int(job["refunded"] or 0) == 1,
                "quoted_cost": int(job["quoted_cost"]),
            }
        shots.append({
            "id": shot["id"], "shot_key": shot["shot_key"],
            "sort_order": int(shot["sort_order"]), "duration": int(shot["duration"]),
            "scene_description": shot["scene_description"],
            "video_prompt": shot["video_prompt"],
            "video": {
                "asset_id": asset["id"] if asset else None,
                "current_version": int(asset["current_version"]) if asset and asset["current_version"] else None,
                "locked": bool(asset["locked"]) if asset else False,
                "versions": versions.get(asset["id"], []) if asset else [],
                "job": job_data,
            },
        })
    reserved = _reserved_points(conn, project["id"])
    return {
        "project_id": project["id"], "revision": int(project["revision"]),
        "stage": project["stage"], "ratio": project["ratio"],
        "target_duration": int(project["target_duration"]),
        "point_budget": int(project["point_budget"]),
        "spent_points": int(project["spent_points"]),
        "reserved_points": reserved, "models": _model_options(), "shots": shots,
        "ready": bool(shots) and all(
            item["video"]["locked"] and item["video"]["current_version"]
            for item in shots
        ) and reserved == 0,
    }


def get_workspace(db_factory, username, project_id):
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        project = conn.execute(
            "SELECT * FROM short_drama_projects WHERE id=? AND username=? AND deleted=0",
            (project_id, username),
        ).fetchone()
        if not project:
            raise LookupError("短剧项目不存在")
        if project["stage"] not in VIDEO_STAGES:
            raise ValueError("短剧项目尚未进入视频确认阶段")
        snapshot = build_snapshot(conn, project, username)
        conn.commit()
        return snapshot


def select_version(db_factory, username, body, access=None):
    required = {"project_id", "revision", "asset_id", "version", "lock"}
    if not isinstance(body, dict) or set(body) != required:
        raise ValueError("短剧视频版本请求字段无效")
    if (not isinstance(body["project_id"], str) or not body["project_id"].strip()
            or not isinstance(body["asset_id"], str) or not body["asset_id"].strip()
            or type(body["revision"]) is not int or body["revision"] < 1
            or type(body["version"]) is not int or body["version"] < 1
            or type(body["lock"]) is not bool):
        raise ValueError("短剧视频版本参数无效")
    project_id = body["project_id"].strip()
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        project = _authorized_project(conn, username, project_id, access, write=True)
        owner = project["username"]
        if int(project["revision"]) != body["revision"]:
            from .short_drama import RevisionConflict
            raise RevisionConflict("项目已更新，请刷新后重试")
        if project["stage"] != "video_review":
            raise ValueError("当前阶段不能选择视频版本")
        version = conn.execute(
            "SELECT 1 FROM short_drama_video_versions v "
            "JOIN short_drama_video_assets a ON a.id=v.asset_id "
            "WHERE a.id=? AND a.project_id=? AND v.version=?",
            (body["asset_id"], project_id, body["version"]),
        ).fetchone()
        if not version:
            raise LookupError("短剧视频版本不存在")
        now = int(time.time())
        conn.execute(
            "UPDATE short_drama_video_assets SET current_version=?,locked=?,updated_at=? "
            "WHERE id=? AND project_id=?",
            (body["version"], int(body["lock"]), now, body["asset_id"], project_id),
        )
        updated = conn.execute(
            "UPDATE short_drama_projects SET revision=revision+1,updated_at=? "
            "WHERE id=? AND revision=? AND stage='video_review'",
            (now, project_id, body["revision"]),
        )
        if updated.rowcount != 1:
            from .short_drama import RevisionConflict
            raise RevisionConflict("项目已更新，请刷新后重试")
        conn.commit()
    return get_workspace(db_factory, owner, project_id)


def confirm_stage(db_factory, username, body, access=None):
    if not isinstance(body, dict) or set(body) != {"project_id", "revision", "stage"}:
        raise ValueError("短剧视频阶段确认字段无效")
    if body.get("stage") != "video_review" or type(body.get("revision")) is not int:
        raise ValueError("只能确认视频阶段")
    project_id = str(body.get("project_id") or "").strip()
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        project = _authorized_project(conn, username, project_id, access, write=True)
        owner = project["username"]
        if int(project["revision"]) != body["revision"]:
            from .short_drama import RevisionConflict
            raise RevisionConflict("项目已更新，请刷新后重试")
        if project["stage"] != "video_review":
            raise ValueError("短剧阶段不能跳过")
        snapshot = build_snapshot(conn, project, username)
        if not snapshot["ready"]:
            raise ValueError("请先为全部镜头选择并锁定视频版本")
        now = int(time.time())
        updated = conn.execute(
            "UPDATE short_drama_projects SET stage='assembly_review',revision=revision+1,updated_at=? "
            "WHERE id=? AND revision=? AND stage='video_review'",
            (now, project_id, body["revision"]),
        )
        if updated.rowcount != 1:
            from .short_drama import RevisionConflict
            raise RevisionConflict("项目已更新，请刷新后重试")
        conn.commit()
    return get_workspace(db_factory, owner, project_id)
