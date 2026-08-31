# -*- coding: utf-8 -*-
"""Persistent server-owned runs for the normal digital-human workflow.

The browser and CLI can both drive this ledger.  Paid child submissions remain
behind the existing content API boundary; callers inject ``submit_job`` so the
same validation, price binding, idempotency, deduction and refund code is used.
"""
import hashlib
import hmac
import json
import re
import threading
import time
from contextlib import closing

from . import cli_uploads
from . import digital_human_oneclick as legacy
from . import digital_human_v2 as workflow
from . import points
from .core import jdb


CAPABILITY_PATH = "/api/gen/digital-human-v2/capability"
QUOTE_PATH = "/api/gen/digital-human-v2/runs/quote"
RUNS_PATH = "/api/gen/digital-human-v2/runs"
RUN_ID_RE = re.compile(r"^dh-run-[A-Za-z0-9._:-]{1,128}$")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
PORTRAIT_UPLOAD_RE = re.compile(r"^img_[0-9a-f]{32}$")
RUN_STATES = (
    "confirmed", "queued", "running", "needs_attention", "recoverable",
    "completed", "failed", "refund_pending", "refunded", "abandoned",
)
TERMINAL_STATES = ("completed", "failed", "refunded", "abandoned")
_RUN_LOCKS = tuple(threading.RLock() for _ in range(64))


def _run_lock(run_id):
    digest = hashlib.sha256(run_id.encode("utf-8")).digest()
    return _RUN_LOCKS[int.from_bytes(digest[:4], "big") % len(_RUN_LOCKS)]


def _canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _ensure_tables(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS digital_human_runs(
        run_id TEXT PRIMARY KEY,
        username TEXT NOT NULL,
        request_id TEXT NOT NULL,
        request_hash TEXT NOT NULL,
        plan_digest TEXT NOT NULL,
        consent_id TEXT NOT NULL,
        status TEXT NOT NULL,
        quoted_cost INTEGER NOT NULL,
        input_json TEXT NOT NULL,
        plan_json TEXT NOT NULL,
        steps_json TEXT NOT NULL,
        result_json TEXT,
        error TEXT NOT NULL DEFAULT '',
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        abandoned_at INTEGER,
        UNIQUE(username, request_id)
    )""")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_dh_runs_owner_updated "
        "ON digital_human_runs(username,updated_at DESC)"
    )


def init_db(db_factory=None):
    factory = db_factory or legacy.cdb
    with closing(factory()) as connection:
        _ensure_tables(connection)
        connection.commit()


def _strict_body(payload, allowed, required=()):
    if not isinstance(payload, dict):
        raise legacy.DigitalHumanRequestError("请求体必须是 JSON 对象")
    unknown = sorted(set(payload) - set(allowed))
    if unknown:
        raise legacy.DigitalHumanRequestError(
            "运行提交包含不支持字段：" + ", ".join(unknown),
        )
    missing = [field for field in required if field not in payload]
    if missing:
        raise legacy.DigitalHumanRequestError("缺少运行字段：" + missing[0])


def _normalized_request(payload, username):
    allowed = {
        "request_id", "consent_token", "plan_digest", "script",
        "narration_mode", "audio_upload_id", "allow_ai_materials",
        "customer_upload_ids", "portrait_upload_id", "voice_key",
    }
    _strict_body(
        payload, allowed,
        ("request_id", "consent_token", "plan_digest", "portrait_upload_id"),
    )
    request_id = str(payload.get("request_id") or "").strip()
    if not REQUEST_ID_RE.fullmatch(request_id):
        raise legacy.DigitalHumanRequestError(
            "request_id 格式无效", "invalid_request_id",
        )
    plan_digest = str(payload.get("plan_digest") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", plan_digest):
        raise legacy.DigitalHumanRequestError("plan_digest 格式无效")
    portrait_upload_id = str(payload.get("portrait_upload_id") or "").strip().lower()
    if not PORTRAIT_UPLOAD_RE.fullmatch(portrait_upload_id):
        raise legacy.DigitalHumanRequestError("portrait_upload_id 格式无效")
    consent_token = str(payload.get("consent_token") or "").strip()
    record = workflow._load_current_consent(username, consent_token)
    if not hmac.compare_digest(record["plan_digest"], plan_digest):
        raise legacy.DigitalHumanRequestError(
            "授权与冻结方案不匹配", "consent_binding_mismatch", 403,
        )
    mode = str(payload.get("narration_mode") or "text").strip().lower()
    if mode not in {"text", "audio"}:
        raise legacy.DigitalHumanRequestError("narration_mode 仅支持 text 或 audio")
    allow_ai = payload.get("allow_ai_materials", False)
    uploads = payload.get("customer_upload_ids", [])
    if not isinstance(allow_ai, bool) or not isinstance(uploads, list):
        raise legacy.DigitalHumanRequestError("素材策略格式无效")
    plan_input = {
        "digital_human_narration_mode": mode,
        "digital_human_script": str(payload.get("script") or "").strip(),
        "digital_human_allow_ai_materials": allow_ai,
        "digital_human_customer_upload_ids": uploads,
    }
    if mode == "audio":
        plan_input["digital_human_audio_upload_id"] = str(
            payload.get("audio_upload_id") or ""
        ).strip()
    frozen = workflow._authoritative_plan(plan_input, username)
    if not hmac.compare_digest(frozen["plan_digest"], plan_digest):
        raise legacy.DigitalHumanRequestError(
            "运行输入与冻结方案不一致，请重新生成方案",
            "plan_digest_mismatch", 409,
        )
    _portrait, portrait_meta = cli_uploads._load_image(
        portrait_upload_id, username, int(time.time()),
    )
    if not hmac.compare_digest(
            str(portrait_meta.get("sha256") or ""), record["photo_sha256"]):
        raise legacy.DigitalHumanRequestError(
            "人物照片与授权记录不一致", "consent_photo_mismatch", 403,
        )
    voice_key = str(payload.get("voice_key") or "").strip()
    if mode == "audio":
        if voice_key:
            raise legacy.DigitalHumanRequestError("录音驱动模式不应提供 voice_key")
    else:
        if record["voice_mode"] != "existing":
            raise legacy.DigitalHumanRequestError(
                "CLI 普通模式请先完成声音复刻，再以已有音色创建授权",
                "voice_not_ready", 409,
            )
        if not voice_key or voice_key != record["voice_ref"]:
            raise legacy.DigitalHumanRequestError(
                "voice_key 与授权音色不一致", "consent_voice_mismatch", 403,
            )
    normalized = {
        "request_id": request_id,
        "consent_token": consent_token,
        "plan_digest": plan_digest,
        "script": frozen["copy"],
        "narration_mode": mode,
        "audio_upload_id": str(payload.get("audio_upload_id") or "").strip(),
        "allow_ai_materials": allow_ai,
        "customer_upload_ids": list(frozen.get("customer_upload_ids") or []),
        "portrait_upload_id": portrait_upload_id,
        "voice_key": voice_key,
    }
    return normalized, frozen, record


def _consent_fields(run, stage, index=None):
    body = {
        "digital_human_pipeline": workflow.CONSENT_PURPOSE,
        "digital_human_stage": stage,
        "digital_human_run_id": run["run_id"],
        "digital_human_plan_digest": run["plan_digest"],
        "digital_human_consent_token": run["input"]["consent_token"],
        "digital_human_script": run["plan"]["copy"],
        "digital_human_narration_mode": run["input"]["narration_mode"],
        "digital_human_allow_ai_materials": run["input"]["allow_ai_materials"],
        "digital_human_customer_upload_ids": run["input"]["customer_upload_ids"],
    }
    if run["input"]["audio_upload_id"]:
        body["digital_human_audio_upload_id"] = run["input"]["audio_upload_id"]
    if index is not None:
        body["digital_human_item_index"] = int(index)
    return body


def _material_payload(run, item, index):
    return dict(_consent_fields(run, "material", index), **{
        "provider": "seedream", "variant": "std", "quality": "std",
        "count": 1, "ratio": "9:16", "prompt": item["prompt"],
    })


def _talking_payload(run, item, index):
    audio_mode = run["input"]["narration_mode"] == "audio"
    return dict(_consent_fields(run, "talking", index), **{
        "mode": "audio" if audio_mode else "text",
        "image_upload_id": run["input"]["portrait_upload_id"],
        "text": item["text"], "voice": "" if audio_mode else run["input"]["voice_key"],
        "resolution": "1080p", "ratio": "9:16", "motion": "low",
        "speed": 1, "pitch": 0, "volume": 1, "delivery": "natural",
        "subtitle": False,
    })


def _cost_breakdown(normalized, plan, record):
    run = {
        "run_id": record["run_id"], "plan_digest": record["plan_digest"],
        "input": normalized, "plan": plan,
    }
    materials = [points.cost_of("image", _material_payload(run, item, index))
                 for index, item in enumerate(plan["materials"])]
    talking = [points.cost_of("video", _talking_payload(run, item, index))
               for index, item in enumerate(plan["segments"])]
    return {
        "materials_max": sum(materials), "materials_each": materials,
        "talking": sum(talking), "talking_each": talking,
        "compose": 0, "total": sum(materials) + sum(talking),
    }


def capability_response():
    return {
        "ok": True,
        "mode": "normal",
        "workflow_version": workflow.timeline.WORKFLOW_VERSION,
        "run_states": list(RUN_STATES),
        "terminal_states": list(TERMINAL_STATES),
        "narration_modes": ["text", "audio"],
        "voice_modes": ["existing"],
        "limits": {
            "ratio": "9:16", "audio_max_bytes": 30 * 1024 * 1024,
            "material_upload_max_bytes": 10 * 1024 * 1024,
        },
        "billing": {
            "kind": "quote_then_confirm",
            "idempotency": "request_id",
            "note": "材料报价按全部需要 AI 补图的上限计算；实际仅对子任务逐项扣点。",
        },
        "providers": {
            "talking": "heygen", "material_ai": "seedream",
            "compose": "local_ffmpeg",
        },
        "precision": {"available": False, "status": "planned"},
    }


def quote_response(payload, username):
    normalized, plan, record = _normalized_request(payload, username)
    breakdown = _cost_breakdown(normalized, plan, record)
    return {
        "ok": True, "kind": "digital_human_oneclick",
        "run_id": record["run_id"], "request_id": normalized["request_id"],
        "plan_digest": plan["plan_digest"], "cost": breakdown["total"],
        "cost_breakdown": breakdown, "confirmation_required": True,
    }


def _new_steps(run):
    materials = []
    for index, item in enumerate(run["plan"]["materials"]):
        materials.append({
            "id": "material:%d" % index, "kind": "image", "index": index,
            "status": "waiting", "job_id": 0, "asset_id": "", "attempt": 0,
            "cost": points.cost_of("image", _material_payload(run, item, index)),
            "refunded": False, "error": "", "result": {},
        })
    talking = []
    for index, item in enumerate(run["plan"]["segments"]):
        talking.append({
            "id": "talking:%d" % index, "kind": "video", "index": index,
            "status": "waiting", "job_id": 0, "attempt": 0,
            "cost": points.cost_of("video", _talking_payload(run, item, index)),
            "refunded": False, "error": "", "result": {},
        })
    return {
        "materials": materials, "talking": talking,
        "compose": {
            "id": "compose", "kind": "script_to_video", "index": 0,
            "status": "waiting", "job_id": 0, "attempt": 0, "cost": 0,
            "refunded": False, "error": "", "result": {},
        },
    }


def _decode_row(row):
    data = dict(row)
    data["input"] = json.loads(data.pop("input_json"))
    data["plan"] = json.loads(data.pop("plan_json"))
    data["steps"] = json.loads(data.pop("steps_json"))
    data["result"] = json.loads(data.pop("result_json") or "{}")
    return data


def _load_run(run_id, username, db_factory=None):
    run_id = str(run_id or "").strip()
    if not RUN_ID_RE.fullmatch(run_id):
        raise legacy.DigitalHumanRequestError("run_id 格式无效")
    factory = db_factory or legacy.cdb
    init_db(factory)
    with closing(factory()) as connection:
        row = connection.execute(
            "SELECT * FROM digital_human_runs WHERE run_id=? AND username=?",
            (run_id, username),
        ).fetchone()
    if not row:
        raise legacy.DigitalHumanRequestError("运行不存在", "run_not_found", 404)
    return _decode_row(row)


def _save_run(run, db_factory=None):
    factory = db_factory or legacy.cdb
    now = int(time.time())
    run["updated_at"] = now
    with closing(factory()) as connection:
        connection.execute(
            "UPDATE digital_human_runs SET status=?,steps_json=?,result_json=?,"
            "error=?,updated_at=?,abandoned_at=? WHERE run_id=? AND username=?",
            (
                run["status"], _canonical(run["steps"]),
                _canonical(run.get("result") or {}), str(run.get("error") or "")[:500],
                now, run.get("abandoned_at"), run["run_id"], run["username"],
            ),
        )
        connection.commit()


def _job_rows(run):
    ids = []
    for step in run["steps"]["materials"] + run["steps"]["talking"] + [run["steps"]["compose"]]:
        if int(step.get("job_id") or 0):
            ids.append(int(step["job_id"]))
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    with closing(jdb()) as connection:
        rows = connection.execute(
            "SELECT id,username,kind,status,result,error,cost,refunded FROM jobs "
            "WHERE id IN (%s) AND username=?" % placeholders,
            tuple(ids) + (run["username"],),
        ).fetchall()
    return {int(row["id"]): dict(row) for row in rows}


def _sync(run):
    was_abandoned = run.get("status") == "abandoned"
    rows = _job_rows(run)
    for step in run["steps"]["materials"] + run["steps"]["talking"] + [run["steps"]["compose"]]:
        job_id = int(step.get("job_id") or 0)
        if not job_id:
            continue
        row = rows.get(job_id)
        if not row:
            step["status"] = "needs_attention"
            step["error"] = "任务记录暂不可用"
            continue
        status = str(row.get("status") or "")
        step["refunded"] = int(row.get("refunded") or 0) == 1
        if status == "done":
            step["status"] = "completed"
            try:
                step["result"] = json.loads(row.get("result") or "{}")
            except Exception:
                step["result"] = {}
            step["error"] = ""
        elif status in {"failed", "error"}:
            if int(row.get("refunded") or 0) == 2:
                step["status"] = "refund_pending"
            elif int(row.get("refunded") or 0) == 1 or int(row.get("cost") or 0) <= 0:
                step["status"] = "recoverable"
            else:
                step["status"] = "needs_attention"
            step["error"] = str(row.get("error") or "任务失败")[:300]
        elif status == "pending":
            step["status"] = "queued"
        else:
            step["status"] = "running"
    all_children = run["steps"]["materials"] + run["steps"]["talking"]
    compose = run["steps"]["compose"]
    if was_abandoned:
        if compose["status"] == "completed":
            run["result"] = dict(compose.get("result") or {})
        run["status"] = "abandoned"
        return run
    if compose["status"] == "completed":
        run["status"] = "completed"
        run["result"] = dict(compose.get("result") or {})
        run["error"] = ""
    elif any(step["status"] == "refund_pending" for step in all_children + [compose]):
        run["status"] = "refund_pending"
    elif any(step["status"] == "needs_attention" for step in all_children + [compose]):
        run["status"] = "needs_attention"
    elif any(step["status"] == "recoverable" for step in all_children + [compose]):
        run["status"] = "recoverable"
    elif any(step["status"] == "failed" for step in all_children + [compose]):
        run["status"] = "failed"
        run["error"] = next(
            (str(step.get("error") or "") for step in all_children + [compose]
             if step.get("status") == "failed"),
            "运行失败",
        )[:500]
    elif any(step["status"] in {"queued", "running"} for step in all_children + [compose]):
        run["status"] = "running"
    elif run["status"] != "abandoned":
        run["status"] = "confirmed"
    return run


def _submit_step(run, step, body, submit_job):
    step["attempt"] = int(step.get("attempt") or 0) + 1
    key = "dh-run:" + _digest({
        "run_id": run["run_id"], "step": step["id"],
        "attempt": step["attempt"], "body": body,
    })[:40]
    status, response = submit_job(step["kind"], body, key, int(step["cost"]))
    if 200 <= int(status) < 300 and int((response or {}).get("job_id") or 0):
        step["job_id"] = int(response["job_id"])
        step["status"] = "queued"
        step["error"] = ""
        return
    step["status"] = (
        "recoverable" if (response or {}).get("operation_terminal") else "needs_attention"
    )
    step["error"] = str((response or {}).get("detail") or "子任务提交结果未知")[:300]


def _resolve_or_submit_material(run, step, submit_job):
    index = int(step["index"])
    resolved = workflow.resolve_material_response(
        _consent_fields(run, "material_resolve", index), run["username"],
    )
    if resolved.get("material_asset_id"):
        step["asset_id"] = resolved["material_asset_id"]
        step["status"] = "completed"
        step["result"] = dict(resolved)
        step["cost"] = 0
        return
    if run["plan"].get("allow_ai_materials") is not True:
        step["status"] = "failed"
        step["error"] = "当前方案未授权 AI 补图，且没有可用的本人或平台素材"
        return
    _submit_step(
        run, step,
        _material_payload(run, run["plan"]["materials"][index], index),
        submit_job,
    )


def _submit_children(run, submit_job, retry=False):
    for step in run["steps"]["materials"]:
        if step["status"] == "waiting" or (retry and step["status"] == "recoverable"):
            _resolve_or_submit_material(run, step, submit_job)
    for step in run["steps"]["talking"]:
        if step["status"] == "waiting" or (retry and step["status"] == "recoverable"):
            index = int(step["index"])
            _submit_step(
                run, step,
                _talking_payload(run, run["plan"]["segments"][index], index),
                submit_job,
            )


def _compose_payload(run):
    materials = run["steps"]["materials"]
    talking = run["steps"]["talking"]
    return dict(_consent_fields(run, "compose", 0), **{
        "pipeline": workflow.PIPELINE, "mode": workflow.PIPELINE,
        "script": run["plan"]["copy"], "plan_digest": run["plan_digest"],
        "video_job_ids": [int(step["job_id"]) for step in talking],
        "material_job_ids": [int(step.get("job_id") or 0) for step in materials],
        "material_asset_ids": [str(step.get("asset_id") or "") for step in materials],
    })


def _maybe_submit_compose(run, submit_job, retry=False):
    if run.get("status") == "abandoned":
        return
    children = run["steps"]["materials"] + run["steps"]["talking"]
    compose = run["steps"]["compose"]
    if not all(step["status"] == "completed" for step in children):
        return
    if compose["status"] == "waiting" or (retry and compose["status"] == "recoverable"):
        _submit_step(run, compose, _compose_payload(run), submit_job)


def _public_run(run):
    return {
        "run_id": run["run_id"], "request_id": run["request_id"],
        "plan_digest": run["plan_digest"], "status": run["status"],
        "quoted_cost": int(run["quoted_cost"]), "steps": run["steps"],
        "result": run.get("result") or {}, "error": run.get("error") or "",
        "created_at": int(run["created_at"]), "updated_at": int(run["updated_at"]),
        "recoverable": run["status"] == "recoverable",
        "terminal": run["status"] in TERMINAL_STATES,
    }


def start_response(payload, username, expected_cost, submit_job, db_factory=None):
    normalized, plan, record = _normalized_request(payload, username)
    breakdown = _cost_breakdown(normalized, plan, record)
    try:
        expected_cost = int(expected_cost)
    except (TypeError, ValueError) as exc:
        raise legacy.DigitalHumanRequestError(
            "启动运行必须绑定报价 cost", "expected_cost_required", 409,
        ) from exc
    if expected_cost != int(breakdown["total"]):
        raise legacy.DigitalHumanRequestError(
            "数字人运行价格已变化，请重新报价", "quote_cost_changed", 409,
        )
    run_id = record["run_id"]
    if not RUN_ID_RE.fullmatch(run_id):
        raise legacy.DigitalHumanRequestError("授权 run_id 不符合服务端运行格式")
    request_hash = _digest(normalized)
    factory = db_factory or legacy.cdb
    init_db(factory)
    now = int(time.time())
    run = {
        "run_id": run_id, "username": username,
        "request_id": normalized["request_id"], "request_hash": request_hash,
        "plan_digest": plan["plan_digest"], "consent_id": record["id"],
        "status": "confirmed", "quoted_cost": int(breakdown["total"]),
        "input": normalized, "plan": plan, "steps": {}, "result": {},
        "error": "", "created_at": now, "updated_at": now, "abandoned_at": None,
    }
    run["steps"] = _new_steps(run)
    with _run_lock(run_id):
        with closing(factory()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM digital_human_runs WHERE username=? AND request_id=?",
                (username, normalized["request_id"]),
            ).fetchone()
            if row:
                existing = _decode_row(row)
                if existing["request_hash"] != request_hash:
                    raise legacy.DigitalHumanRequestError(
                        "request_id 已绑定其他运行输入", "idempotency_conflict", 409,
                    )
                connection.commit()
                _sync(existing)
                _maybe_submit_compose(existing, submit_job)
                _sync(existing)
                _save_run(existing, factory)
                return {"ok": True, "replayed": True, "run": _public_run(existing)}
            conflict = connection.execute(
                "SELECT request_hash FROM digital_human_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if conflict:
                raise legacy.DigitalHumanRequestError(
                    "授权 run_id 已绑定其他 request_id", "run_id_conflict", 409,
                )
            connection.execute(
                """INSERT INTO digital_human_runs(
                   run_id,username,request_id,request_hash,plan_digest,consent_id,status,
                   quoted_cost,input_json,plan_json,steps_json,result_json,error,
                   created_at,updated_at,abandoned_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id, username, run["request_id"], request_hash,
                    run["plan_digest"], run["consent_id"], run["status"],
                    run["quoted_cost"], _canonical(normalized), _canonical(plan),
                    _canonical(run["steps"]), _canonical({}), "", now, now, None,
                ),
            )
            connection.commit()
        _submit_children(run, submit_job)
        _sync(run)
        _maybe_submit_compose(run, submit_job)
        _sync(run)
        _save_run(run, factory)
    return {"ok": True, "replayed": False, "run": _public_run(run)}


def status_response(run_id, username, submit_job, db_factory=None):
    run_id = str(run_id or "").strip()
    if not RUN_ID_RE.fullmatch(run_id):
        raise legacy.DigitalHumanRequestError("run_id 格式无效")
    factory = db_factory or legacy.cdb
    init_db(factory)
    with _run_lock(run_id):
        run = _sync(_load_run(run_id, username, factory))
        _maybe_submit_compose(run, submit_job)
        _sync(run)
        _save_run(run, factory)
    return {"ok": True, "run": _public_run(run)}


def recover_response(run_id, payload, username, submit_job, db_factory=None):
    _strict_body(payload, {"request_id"}, ("request_id",))
    request_id = str(payload.get("request_id") or "").strip()
    run_id = str(run_id or "").strip()
    if not RUN_ID_RE.fullmatch(run_id):
        raise legacy.DigitalHumanRequestError("run_id 格式无效")
    factory = db_factory or legacy.cdb
    init_db(factory)
    with _run_lock(run_id):
        run = _sync(_load_run(run_id, username, factory))
        if request_id != run["request_id"]:
            raise legacy.DigitalHumanRequestError(
                "request_id 与原运行不一致", "idempotency_conflict", 409,
            )
        if run["status"] == "abandoned":
            raise legacy.DigitalHumanRequestError("运行已放弃", "run_abandoned", 409)
        if run["status"] == "completed":
            _save_run(run, factory)
            return {"ok": True, "replayed": True, "run": _public_run(run)}
        if run["status"] in {"needs_attention", "refund_pending"}:
            raise legacy.DigitalHumanRequestError(
                "仍有子任务扣点或退款状态待确认，请稍后查询",
                "run_recovery_pending", 409,
            )
        _submit_children(run, submit_job, retry=True)
        _sync(run)
        _maybe_submit_compose(run, submit_job, retry=True)
        _sync(run)
        _save_run(run, factory)
    return {"ok": True, "replayed": False, "run": _public_run(run)}


def abandon_response(run_id, payload, username, db_factory=None):
    _strict_body(payload, {"request_id"}, ("request_id",))
    run_id = str(run_id or "").strip()
    if not RUN_ID_RE.fullmatch(run_id):
        raise legacy.DigitalHumanRequestError("run_id 格式无效")
    factory = db_factory or legacy.cdb
    init_db(factory)
    with _run_lock(run_id):
        run = _sync(_load_run(run_id, username, factory))
        if str(payload.get("request_id") or "").strip() != run["request_id"]:
            raise legacy.DigitalHumanRequestError(
                "request_id 与原运行不一致", "idempotency_conflict", 409,
            )
        if run["status"] == "completed":
            raise legacy.DigitalHumanRequestError("已完成运行不能放弃", "run_completed", 409)
        if run["status"] != "abandoned":
            run["status"] = "abandoned"
            run["abandoned_at"] = int(time.time())
            run["error"] = "用户已放弃后续恢复；已提交子任务仍按原账务终态处理"
        _save_run(run, factory)
    return {"ok": True, "run": _public_run(run)}
