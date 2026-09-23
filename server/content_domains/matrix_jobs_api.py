"""Unified internal jobs facade; billing and execution use the existing ledger.

No render credential or arbitrary remote URL is accepted from the caller.
"""
from contextlib import closing
import copy
import hashlib
import json
import os
import re
import time
import tempfile
import uuid

PREFIX = "/internal/matrix-template"
ENDPOINT = PREFIX + "/jobs"
CONTRACT = 1
ENABLED = os.environ.get("MATRIX_UNIFIED_API_ENABLED", "0") == "1"


class APIError(ValueError):
    def __init__(self, code, message, status=400, *, retryable=False):
        super().__init__(message)
        self.code, self.status, self.retryable = code, status, retryable


def envelope(error, request_id, *, job_id=None, stage="admission"):
    return {"error": error.code, "http_status": error.status, "message": str(error),
        "details": {"request_id": request_id, "stage": stage,
            "job_created": (None if stage == "submission_unknown" else bool(job_id)), "job_id": job_id,
            "retryable": error.retryable, "retry_after": 3 if error.retryable else 0}}


def normalize(body, user):
    if not isinstance(body, dict):
        raise APIError("INVALID_REQUEST", "请求必须为 JSON 对象")
    allowed = {"template_id", "account", "materials", "texts", "voiceover", "bgm",
        "bgm_volume", "dedupe_key", "text_revision", "text_overrides"}
    if set(body) - allowed:
        raise APIError("INVALID_REQUEST", "未知参数：" + sorted(set(body)-allowed)[0])
    account = body.get("account")
    if not isinstance(account, dict) or not account or set(account)-{"account_id", "username"}:
        raise APIError("INVALID_ACCOUNT", "需要账号身份")
    for field, expected in (("username", user.get("username")), ("account_id", user.get("account_id"))):
        if field in account and (expected is None or str(account[field]) != str(expected)):
            raise APIError("ACCOUNT_FORBIDDEN", "账号身份与登录凭据不一致", 403)
    if not isinstance(body.get("template_id"), str) or not re.fullmatch(r"[a-z0-9-]{1,100}", body["template_id"]):
        raise APIError("TEMPLATE_UNKNOWN", "模板标识无效")
    texts = body.get("texts")
    if not isinstance(texts, dict) or set(texts)-{"top_text", "bottom_text"}:
        raise APIError("TEXT_REQUIRED", "texts 需要 top_text 和 bottom_text")
    if any(not isinstance(texts.get(k), str) or not 2 <= len(texts[k].strip()) <= 4000
           for k in ("top_text", "bottom_text")):
        raise APIError("TEXT_REQUIRED", "请提供标题和底部文案，每项最多4000字")
    materials = body.get("materials", {"mode": "auto"})
    if not isinstance(materials, dict) or materials.get("mode") not in {"auto", "picked"}:
        raise APIError("INVALID_MATERIALS", "当前支持 auto 或 picked；不接受未验证的 URL/SHA 引用")
    if materials["mode"] == "auto":
        if set(materials) != {"mode"}:
            raise APIError("INVALID_MATERIALS", "auto 不接受额外素材字段")
    else:
        ids = materials.get("ids")
        if (set(materials) != {"mode", "ids"} or not isinstance(ids, list)
                or not 1 <= len(ids) <= 20 or any(not isinstance(i, str) or not re.fullmatch(r"(?:(?:vid|img)_)?[0-9a-f]{32}", i) for i in ids)):
            raise APIError("INVALID_MATERIALS", "picked.ids 需要1-20个本人 asset_id 或 upload_id")
    if type(body.get("bgm", True)) is not bool:
        raise APIError("INVALID_BGM", "当前 bgm 使用 true/false，指定音乐ID尚未开放")
    voice = body.get("voiceover")
    if voice is not None and (not isinstance(voice, dict) or set(voice)-{"text", "voice", "speed", "voice_scope"}):
        raise APIError("INVALID_VOICE", "voiceover 参数无效")
    if voice is not None and (not isinstance(voice.get("text"), str)
            or not 1 <= len(voice["text"].strip()) <= 120
            or not isinstance(voice.get("voice"), str) or not voice["voice"].strip()
            or type(voice.get("speed", 1.)) not in (int, float)
            or not .5 <= voice.get("speed", 1.) <= 2.):
        raise APIError("INVALID_VOICE", "配音需要音色、1-120字文案及0.5-2倍语速")
    # Reject NaN, infinities and non-JSON values before any charge.
    try:
        json.dumps(body, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise APIError("INVALID_REQUEST", "参数包含无效数值") from exc
    result = copy.deepcopy(body)
    result["account"] = {"username": user["username"]}
    if user.get("account_id"):
        result["account"]["account_id"] = user["account_id"]
    result["materials"] = materials
    return result


def submit(db, points, user, body, *, owner, enqueue, header_key="", now=None, admission_guard=None):
    from . import matrix_template_submission as ledger, submission_idempotency as idem
    request = normalize(body, user)
    try:
        key = idem.clean_key(request.pop("dedupe_key", ""))
        header_key = idem.clean_key(header_key)
    except ValueError as exc:
        raise APIError("INVALID_REQUEST", str(exc)) from exc
    if key and header_key and key != header_key:
        raise APIError("IDEMPOTENCY_CONFLICT", "请求与请求头幂等键不同", 409)
    key = key or header_key
    if not key:
        # Stable identity plus recent *active* ledger lookup, not a time-bucket
        # hash (which duplicates submissions at bucket boundaries).
        digest = idem._request_hash(request)
        with closing(db()) as connection:
            ledger.ensure_table(connection)
            row = connection.execute(
                "SELECT a.idem_key,a.created_at,a.state,j.status FROM matrix_template_submission_attempts a "
                "LEFT JOIN jobs j ON j.id=a.job_id WHERE a.username=? AND a.endpoint=? "
                "AND a.request_hash=? ORDER BY a.created_at DESC,a.rowid DESC LIMIT 1",
                (user["username"], ENDPOINT, digest)).fetchone()
            connection.commit()
        # Concurrent identical no-key requests share a stable key; terminal
        # attempts get a new operation identity on the next explicit request.
        active = row and row["created_at"] >= int(now or time.time())-300 and (
            row["state"] in {"prepared", "charging", "charged"} or row["status"] in {"pending", "running"})
        key = row["idem_key"] if active else "auto-" + hashlib.sha256(
            (digest + (row["idem_key"] if row else "")).encode()).hexdigest()
    existing = ledger.get(db, user["username"], ENDPOINT, key)
    if existing is None and admission_guard:
        admission_guard(request)
    state, response = idem.begin(db, user["username"], ENDPOINT, key, request)
    if state == "conflict":
        raise APIError("IDEMPOTENCY_CONFLICT", "同一 dedupe_key 不能修改制作参数", 409)
    attempt = ledger.get(db, user["username"], ENDPOINT, key)
    if attempt is None:
        cost = points.cost_of("matrix_template_video", request)
        ledger.prepare(db, user["username"], ENDPOINT, key, request, cost,
            execution_body={"_matrix_unified": request,
                "_matrix_unified_contract": CONTRACT})
    try:
        attempt = ledger.recover(db, points, user["username"], ENDPOINT, key, owner=owner)
    except ledger.AttemptConflict as exc:
        raise APIError("IDEMPOTENCY_CONFLICT", str(exc), 409) from exc
    except (ledger.AttemptInProgress, ledger.AttemptRecoveryPending) as exc:
        raise APIError("SUBMISSION_PENDING", "受理结果确认中，请使用原幂等键重试", 503, retryable=True) from exc
    if attempt["state"] != "linked":
        failure = attempt.get("response") or {}
        raise APIError(failure.get("code", "SUBMISSION_FAILED"),
            failure.get("detail", "任务受理失败"), int(failure.get("_http_status", 503)))
    job_id = int(attempt["job_id"])
    with closing(db()) as connection:
        row = connection.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row and row["status"] == "pending":
        enqueue(job_id, "matrix_template_video", None)
    return {"job_id": job_id, "status": "accepted", "dedupe": state != "new",
        "dedupe_key": key, "request_id": "matrix-template-" + str(job_id),
        "query_url": ENDPOINT + "/" + str(job_id)}


def status(db, username, job_id):
    with closing(db()) as connection:
        row = connection.execute("SELECT * FROM jobs WHERE id=? AND username=? AND kind=?",
            (job_id, username, "matrix_template_video")).fetchone()
    if not row:
        raise APIError("NOT_FOUND", "任务不存在", 404)
    payload = json.loads(row["payload"] or "{}")
    if payload.get("_matrix_unified_contract") != CONTRACT:
        raise APIError("NOT_FOUND", "不是统一 API 任务", 404)
    runtime = payload.get("_matrix_runtime") or {}
    phase = runtime.get("phase", "")
    current = {"pending": "accepted", "running": "preparing", "done": "ready",
        "error": "failed", "failed": "failed", "canceled": "canceled"}.get(row["status"], "preparing")
    if current == "preparing" and phase in {"rendering", "provider_queued", "provider_retrying"}:
        current = "rendering"
    result = json.loads(row["result"] or "null") if current == "ready" else None
    if current == "ready" and not isinstance(result, dict):
        raise APIError("DELIVERY_INCOMPLETE", "缺少最终成片信息", 503, retryable=True)
    if result:
        from . import cos
        if not result.get("cos_key") or not result.get("cover_cos_key"):
            raise APIError("DELIVERY_INCOMPLETE", "成片尚未完成交付", 503, retryable=True)
        result = dict(result, video_url=cos.object_url(result["video_file"], private=True),
            cover_url=cos.object_url(result["cover_file"], private=True),
            url_expires_at=int(time.time()) + cos._SIGN_EXPIRE)
    request_id = "matrix-template-" + str(job_id)
    error = envelope(APIError(runtime.get("unified_error_code", "RENDER_FAILED"), row["error"] or "生成失败"),
        request_id, job_id=job_id, stage=phase or "preparing") if current == "failed" else None
    return {"job_id": job_id, "status": current, "request_id": request_id,
        "result": result, "materials_used": (result or {}).get("material_manifest", []),
        "refunded": row["refunded"] == 1, "error": error}


def prepare_request(request, username, selected=None):
    from . import matrix_template_account_media as media, matrix_account_assets as assets
    materials = request["materials"]
    account_id = request["account"].get("account_id")
    if selected is not None:
        records = selected
    elif account_id:
        records = assets.select(account_id, materials)
    elif materials["mode"] == "auto":
        records = media.available(username, adaptive=True)
    else:
        if any(not i.startswith(("vid_", "img_")) for i in materials["ids"]):
            raise APIError("INVALID_ACCOUNT", "登录凭据缺少长期素材库账号ID")
        records = [{"upload_id": i, "media_type": "video" if i.startswith("vid_") else "image"}
            for i in materials["ids"]]
    records = [{k: v for k, v in r.items() if k in {"upload_id", "media_type"}}
        if r.get("upload_id") else r for r in records]
    if not records:
        raise APIError("MATERIAL_UNAVAILABLE", "没有可用的本人素材，请先上传")
    body = {"template_id": request["template_id"], **request["texts"],
        "user_materials": records, "material_adaptation": "auto-v1"}
    for key in ("voiceover", "bgm", "bgm_volume", "text_revision", "text_overrides"):
        if key in request:
            body[key] = copy.deepcopy(request[key])
    # Preserve originals and disclose truncation in the final result. Explicit
    # typography still follows its exact-size, no-silent-shrink contract.
    changes = []
    for field, limit in (("top_text", 60), ("bottom_text", 80)):
        if len(body[field]) > limit:
            changes.append({"field": field, "action": "truncate", "original": body[field]})
            body[field] = body[field][:limit].rstrip("，。；、 ")
    return body, changes


def execute(payload):
    from . import matrix_template_video as matrix
    try:
        return _execute(payload)
    except Exception as exc:
        code = getattr(exc, "code", None) or (
            "MATERIAL_UNAVAILABLE" if "MATERIAL_UNAVAILABLE" in str(exc) else
            "DELIVERY_FAILED" if "DELIVERY_FAILED" in str(exc) else "RENDER_FAILED")
        matrix._persist_runtime(payload["_job_id"], unified_error_code=code)
        raise


def _execute(payload):
    from . import matrix_template_video as matrix
    from .core import jdb
    job_id = int(payload["_job_id"])
    username = payload["_username"]
    with closing(jdb()) as connection:
        row = connection.execute("SELECT payload,created_at FROM jobs WHERE id=? AND username=?",
            (job_id, username)).fetchone()
    if not row:
        raise RuntimeError("unified job missing")
    frozen = json.loads(row["payload"])
    if not frozen.get("_matrix_unified_prepared"):
        matrix._persist_runtime(job_id, phase="preparing")
        body, changes = prepare_request(frozen["_matrix_unified"], username, frozen.get("_matrix_selected"))
        frozen["_matrix_selected"] = body["user_materials"]
        # Freeze source identities before COS transfer; a retry never redraws.
        with closing(jdb()) as connection:
            if connection.execute("UPDATE jobs SET payload=? WHERE id=? AND username=? AND status='running'",
                    (json.dumps(frozen, ensure_ascii=False), job_id, username)).rowcount != 1:
                raise RuntimeError("job stopped before material preparation")
            connection.commit()
        from . import matrix_account_assets as assets
        with tempfile.TemporaryDirectory(prefix="hq-matrix-owned-") as directory:
            body["user_materials"], provenance = assets.resolve(body["user_materials"], username, directory)
        try:
            prepared = matrix.validate_payload(body, username, allow_shared_materials=False,
                trusted_frozen_execution=True)
        except ValueError as exc:
            message = str(exc).replace("视频任务未创建且未扣点", "视频任务失败并将自动退点")
            raise ValueError(message) from exc
        frozen.update(prepared)
        frozen["_matrix_unified_prepared"] = True
        frozen["_matrix_adaptations"] = changes
        frozen["_matrix_material_provenance"] = provenance
        with closing(jdb()) as connection:
            updated = connection.execute(
                "UPDATE jobs SET payload=? WHERE id=? AND username=? AND status='running'",
                (json.dumps(frozen, ensure_ascii=False), job_id, username)).rowcount
            if updated != 1:
                raise RuntimeError("job stopped before preparation completed")
            connection.commit()
    result = matrix._generate(dict(frozen, _job_id=job_id, _username=username))
    matrix._persist_runtime(job_id, phase="delivering")
    result = deliver(result, int(row["created_at"]) + matrix.TOTAL_TIMEOUT)
    identities = {i["sha256"]: i["asset_id"] for i in frozen.get("_matrix_material_provenance", [])}
    for item in result.get("material_manifest", []):
        if item.get("sha256") in identities:
            item["asset_id"] = identities[item["sha256"]]
    result["adaptations"] = frozen.get("_matrix_adaptations", [])
    return result


def deliver(result, deadline_at):
    from . import cos, matrix_template_video as matrix
    from .task_termination import run_process
    if not cos.enabled():
        raise RuntimeError("DELIVERY_FAILED: COS is not configured")
    video = matrix._owned_output_path(result["video_file"])
    cover = video.with_suffix(".cover.jpg")
    run_process(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-i", str(video), "-frames:v", "1", "-q:v", "2", str(cover)],
        timeout=min(60, matrix._remaining_budget(deadline_at)), check=True)
    if not cover.is_file() or cover.stat().st_size < 100:
        raise RuntimeError("DELIVERY_FAILED: cover missing")
    cover_rel = str(result["video_file"]).rsplit(".", 1)[0] + ".cover.jpg"
    urls = []
    for path, key, mime in ((video, result["video_file"], "video/mp4"), (cover, cover_rel, "image/jpeg")):
        hasher = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024*1024), b""):
                hasher.update(chunk)
        digest = hasher.hexdigest()
        urls.append(cos.upload(path, key, mime, private=True,
            metadata={"x-cos-meta-sha256": digest}))
        head = cos.head(key)
        if int(head.get("Content-Length", -1)) != path.stat().st_size:
            raise RuntimeError("DELIVERY_FAILED: COS size mismatch")
    return dict(result, video_url=urls[0], cover_url=urls[1], cover_file=cover_rel,
        cos_key=cos._object_key(result["video_file"]), cover_cos_key=cos._object_key(cover_rel),
        duration_seconds=result["duration"], url_expires_at=int(time.time()) + cos._SIGN_EXPIRE)


def handle(handler, path, method, core):
    if not (path == PREFIX + "/templates" or path == ENDPOINT or path.startswith(ENDPOINT + "/")):
        return False
    rid = uuid.uuid4().hex
    try:
        if not core.cli_gateway._internal_auth(handler, core.AUTH_INTERNAL_TOKEN):
            raise APIError("UNAUTHORIZED", "需要内部服务鉴权", 403)
        user = core.verify(handler._token())
        if not user or core._must_change_password(user):
            raise APIError("UNAUTHORIZED", "需要有效用户登录凭据", 401)
        if method == "POST" and not ENABLED:
            raise APIError("SERVICE_UNAVAILABLE", "统一 API 尚未启用", 503)
        from . import matrix_template_video as matrix
        if method == "GET" and path == PREFIX + "/templates":
            matrix.require_available()
            templates = [dict(t, text_controls=matrix.public_template_controls(t["id"]).get("text_controls"),
                category="voiceover" if t["id"] == matrix.BILINGUAL_TEMPLATE_ID else "bgm",
                text_fields=["top_text", "bottom_text"]) for t in matrix.public_templates()]
            handler._send(200, {"templates": templates, "total": len(templates),
                "next_cursor": None, "contract_version": CONTRACT,
                "input_contract": {"materials_modes": ["auto", "picked"],
                    "material_id_type": ["asset_id", "upload_id"], "bgm_type": "boolean",
                    "voiceover_text_max": 120, "texts": ["top_text", "bottom_text"],
                    "text_controls": True}})
        elif method == "GET" and re.fullmatch(re.escape(ENDPOINT) + r"/[1-9][0-9]*", path):
            handler._send(200, status(core.jdb, user["username"], int(path.rsplit("/", 1)[1])))
        elif method == "POST" and path == ENDPOINT:
            if core.is_shutting_down():
                raise APIError("QUEUE_BUSY", "服务正在更新，请使用原幂等键重试", 503, retryable=True)
            if matrix.public_only_materials(user):
                raise APIError("ACCOUNT_FORBIDDEN", "当前账号不支持本人素材出片", 403)
            def guard(request):
                matrix.require_available()
                if request["template_id"] not in {t["id"] for t in matrix.public_templates()}:
                    raise APIError("TEMPLATE_UNKNOWN", "模板不存在或暂不可用", 404)
                if core._user_active_job_count(user["username"]) >= core.MAX_USER_ACTIVE_JOBS:
                    raise APIError("QUEUE_BUSY", "当前账号任务已满，请稍后使用原幂等键重试", 429, retryable=True)
            with core._submission_lock:
                response = submit(core.jdb, core._domains()[1], user, handler._json_body_strict(),
                    owner=core.SERVICE_OWNER, enqueue=core.enqueue_job, admission_guard=guard,
                    header_key=handler.headers.get("Idempotency-Key", ""))
            handler._send(202, response)
        else:
            raise APIError("NOT_FOUND", "接口不存在", 404)
    except APIError as exc:
        handler._send(exc.status, envelope(exc, rid,
            stage="submission_unknown" if exc.code == "SUBMISSION_PENDING" else "admission"))
    except (ValueError, TypeError) as exc:
        handler._send(400, envelope(APIError("INVALID_REQUEST", str(exc)), rid))
    except Exception:
        handler._send(503, envelope(APIError("SERVICE_UNAVAILABLE", "服务暂不可用，请使用原幂等键重试", 503, retryable=True), rid,
            stage="submission_unknown" if method == "POST" else "query"))
    return True
