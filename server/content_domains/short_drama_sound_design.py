"""AI-assisted sound design for short-drama assembly.

Analysis and editing are free.  Only confirmed suggestions can be quoted and
submitted.  A generated asset is not added to the assembly timeline until the
user explicitly applies it.
"""

import hashlib
import json
import math
import os
import re
import sqlite3
import time
import uuid
from contextlib import closing

from server.providers.sound_effects import capability as provider_capability

from . import jobs_store


ANALYZER_VERSION = "short-drama-sound-design-rules-v1"
QUOTE_TTL_SECONDS = 300
POINTS_PER_SECOND = max(
    1, int(os.environ.get("SOUND_EFFECT_POINTS_PER_SECOND", "2") or 2)
)
ALLOWED_KINDS = {"ambience", "foley", "transition", "impact"}
EDITABLE_STATUSES = {"suggested", "confirmed", "rejected"}
ACTIVE_JOB_STATES = {"pending", "running"}


class SoundDesignError(ValueError):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.status = int(status)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS short_drama_sound_design_sets (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  project_revision INTEGER NOT NULL,
  source_hash TEXT NOT NULL,
  analyzer_version TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('draft','superseded')),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_short_drama_sound_design_active
  ON short_drama_sound_design_sets(project_id) WHERE status='draft';

CREATE TABLE IF NOT EXISTS short_drama_sound_suggestions (
  id TEXT PRIMARY KEY,
  set_id TEXT NOT NULL REFERENCES short_drama_sound_design_sets(id) ON DELETE CASCADE,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  shot_id TEXT NOT NULL REFERENCES short_drama_shots(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK(kind IN ('ambience','foley','transition','impact')),
  prompt TEXT NOT NULL,
  start_ms INTEGER NOT NULL CHECK(start_ms >= 0),
  end_ms INTEGER NOT NULL CHECK(end_ms > start_ms),
  duration_ms INTEGER NOT NULL CHECK(duration_ms > 0),
  loop INTEGER NOT NULL DEFAULT 0 CHECK(loop IN (0,1)),
  volume REAL NOT NULL DEFAULT 0.5 CHECK(volume >= 0 AND volume <= 1),
  confidence REAL NOT NULL DEFAULT 0.5 CHECK(confidence >= 0 AND confidence <= 1),
  reason TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL CHECK(status IN ('suggested','confirmed','rejected','generated','applied')),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_short_drama_sound_suggestions_project
  ON short_drama_sound_suggestions(project_id,set_id,shot_id,status);

CREATE TABLE IF NOT EXISTS short_drama_sound_quotes (
  token TEXT PRIMARY KEY,
  actor_username TEXT NOT NULL,
  owner_username TEXT NOT NULL,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  project_revision INTEGER NOT NULL,
  assembly_revision INTEGER NOT NULL,
  request_hash TEXT NOT NULL,
  items_json TEXT NOT NULL,
  total_cost INTEGER NOT NULL CHECK(total_cost >= 0),
  expires_at INTEGER NOT NULL,
  consumed_key TEXT,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS short_drama_sound_requests (
  id TEXT PRIMARY KEY,
  actor_username TEXT NOT NULL,
  owner_username TEXT NOT NULL,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  idempotency_key TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  quote_token TEXT NOT NULL REFERENCES short_drama_sound_quotes(token),
  total_cost INTEGER NOT NULL CHECK(total_cost >= 0),
  state TEXT NOT NULL CHECK(state IN ('linked','done','partial','failed')),
  points_left INTEGER,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(actor_username,idempotency_key)
);

CREATE TABLE IF NOT EXISTS short_drama_sound_jobs (
  id TEXT PRIMARY KEY,
  request_id TEXT NOT NULL REFERENCES short_drama_sound_requests(id) ON DELETE CASCADE,
  actor_username TEXT NOT NULL,
  owner_username TEXT NOT NULL,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  suggestion_id TEXT NOT NULL REFERENCES short_drama_sound_suggestions(id),
  job_id INTEGER NOT NULL UNIQUE,
  cost INTEGER NOT NULL CHECK(cost >= 0),
  status TEXT NOT NULL CHECK(status IN ('pending','running','done','manual_review','failed','applied')),
  asset_id INTEGER,
  quality_json TEXT NOT NULL DEFAULT '{}',
  error TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(request_id,suggestion_id)
);
CREATE INDEX IF NOT EXISTS idx_short_drama_sound_jobs_project
  ON short_drama_sound_jobs(project_id,status,updated_at DESC);
"""


def init_db(db_factory):
    with closing(db_factory()) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_SCHEMA)
        conn.commit()


def _canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _hash(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _row(row):
    return dict(row) if row else None


def _project(conn, owner_username, project_id, revision=None):
    conn.row_factory = sqlite3.Row
    project = conn.execute(
        "SELECT * FROM short_drama_projects WHERE id=? AND username=? "
        "AND deleted=0", (project_id, owner_username),
    ).fetchone()
    if not project:
        raise LookupError("短剧项目不存在")
    if project["stage"] != "assembly_review":
        raise SoundDesignError(
            "sound_design_stage_invalid", "请先进入成片确认阶段再设计音效", 409
        )
    if revision is not None and int(project["revision"]) != int(revision):
        raise SoundDesignError(
            "revision_conflict", "项目已更新，请刷新后重试", 409
        )
    return project


def _assembly_revision(conn, project_id):
    row = conn.execute(
        "SELECT assembly_revision FROM short_drama_compositions WHERE project_id=?",
        (project_id,),
    ).fetchone()
    return int(row["assembly_revision"]) if row else 1


def _shot_source(conn, project_id):
    rows = conn.execute(
        "SELECT id,shot_key,duration,scene_description,image_prompt,video_prompt,"
        "camera_description,"
        "character_keys_json FROM short_drama_shots WHERE project_id=? "
        "ORDER BY sort_order,id", (project_id,),
    ).fetchall()
    return [{
        "id": str(item["id"]),
        "shot_key": str(item["shot_key"]),
        "duration": int(item["duration"]),
        "visual_prompt": " ".join([
            str(item["scene_description"] or ""),
            str(item["image_prompt"] or ""),
        ]).strip(),
        "video_prompt": str(item["video_prompt"] or ""),
        "camera": str(item["camera_description"] or ""),
        "characters": json.loads(item["character_keys_json"] or "[]"),
    } for item in rows]


def _analysis_source_hash(project_revision, shots):
    return _hash({
        "revision": int(project_revision),
        "shots": shots,
        "analyzer": ANALYZER_VERSION,
    })


def _current_design_set(conn, project):
    source_hash = _analysis_source_hash(
        project["revision"], _shot_source(conn, project["id"])
    )
    active = conn.execute(
        "SELECT * FROM short_drama_sound_design_sets "
        "WHERE project_id=? AND status='draft' AND project_revision=? "
        "AND source_hash=? AND analyzer_version=?",
        (
            project["id"], int(project["revision"]), source_hash,
            ANALYZER_VERSION,
        ),
    ).fetchone()
    if not active:
        raise SoundDesignError(
            "sound_design_stale",
            "项目内容已更新，请重新执行音效分析",
            409,
        )
    return active


def _keywords(text):
    rules = [
        (r"脚步|奔跑|跑步|走路|冲向|追赶", "foley", "清晰自然的脚步与衣物摩擦声"),
        (r"开门|关门|推门|门响", "foley", "真实的门体开合与门锁轻响"),
        (r"雨|雷|闪电", "ambience", "远近层次自然的雨声与低沉雷声"),
        (r"风|树叶|树林|草地", "ambience", "柔和风声与树叶轻微摩擦的环境声"),
        (r"街道|马路|城市|广场", "ambience", "克制的城市远景环境声，不含人声和音乐"),
        (r"咖啡|餐厅|室内|办公室|教室", "ambience", "安静室内空间底噪与轻微物件声，不含人声"),
        (r"撞|摔|砸|爆炸|冲击", "impact", "短促有力但不过载的电影化冲击声"),
        (r"切换|转场|快速推进|闪回", "transition", "简洁的电影化呼啸转场声"),
    ]
    return [item[1:] for item in rules if re.search(item[0], text, re.I)]


def _suggestions(shots):
    items = []
    for index, shot in enumerate(shots):
        duration_ms = shot["duration"] * 1000
        combined = " ".join([
            shot["visual_prompt"], shot["video_prompt"], shot["camera"],
        ]).strip()
        matches = _keywords(combined)
        if not any(kind == "ambience" for kind, _ in matches):
            matches.insert(0, (
                "ambience",
                "与画面空间一致的轻微环境底噪，不含对白、人声和音乐",
            ))
        seen = set()
        for kind, description in matches[:3]:
            if kind in seen:
                continue
            seen.add(kind)
            if kind == "ambience":
                start_ms, end_ms, loop, volume = 0, duration_ms, True, 0.28
            elif kind == "transition":
                start_ms = max(0, duration_ms - 800)
                end_ms, loop, volume = duration_ms, False, 0.45
            else:
                start_ms = min(max(0, duration_ms // 3), duration_ms - 500)
                end_ms = min(duration_ms, start_ms + min(1500, duration_ms))
                loop, volume = False, 0.5
            prompt = (
                "%s。与短剧镜头语境匹配，干净、无对白、无人声、无音乐，"
                "适合后期混音。" % description
            )
            items.append({
                "id": "sfxs-" + uuid.uuid4().hex,
                "shot_id": shot["id"],
                "shot_key": shot["shot_key"],
                "kind": kind,
                "prompt": prompt[:450],
                "start_ms": start_ms,
                "end_ms": end_ms,
                "duration_ms": end_ms - start_ms,
                "loop": loop,
                "volume": volume,
                "confidence": 0.76 if kind != "ambience" else 0.68,
                "reason": "根据镜头动作、场景和时长自动建议",
            })
        if index == len(shots) - 1:
            continue
    return items[:30]


def analyze(db_factory, owner_username, body):
    if not isinstance(body, dict) or set(body) != {"project_id", "revision"}:
        raise ValueError("音效分析请求字段不正确")
    project_id = str(body.get("project_id") or "").strip()
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        project = _project(conn, owner_username, project_id, body["revision"])
        shots = _shot_source(conn, project_id)
        if not shots:
            raise SoundDesignError(
                "sound_design_source_missing", "项目没有可分析的镜头", 409
            )
        source_hash = _analysis_source_hash(project["revision"], shots)
        existing = conn.execute(
            "SELECT id FROM short_drama_sound_design_sets "
            "WHERE project_id=? AND status='draft' AND source_hash=?",
            (project_id, source_hash),
        ).fetchone()
        if existing:
            conn.commit()
            return workspace(db_factory, owner_username, project_id)
        now = int(time.time())
        conn.execute(
            "UPDATE short_drama_sound_design_sets SET status='superseded',"
            "updated_at=? WHERE project_id=? AND status='draft'",
            (now, project_id),
        )
        set_id = "sfxset-" + uuid.uuid4().hex
        conn.execute(
            "INSERT INTO short_drama_sound_design_sets("
            "id,project_id,project_revision,source_hash,analyzer_version,status,"
            "created_at,updated_at) VALUES(?,?,?,?,?,'draft',?,?)",
            (
                set_id, project_id, int(project["revision"]), source_hash,
                ANALYZER_VERSION, now, now,
            ),
        )
        for suggestion in _suggestions(shots):
            conn.execute(
                "INSERT INTO short_drama_sound_suggestions("
                "id,set_id,project_id,shot_id,kind,prompt,start_ms,end_ms,"
                "duration_ms,loop,volume,confidence,reason,status,created_at,"
                "updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'suggested',?,?)",
                (
                    suggestion["id"], set_id, project_id,
                    suggestion["shot_id"], suggestion["kind"],
                    suggestion["prompt"], suggestion["start_ms"],
                    suggestion["end_ms"], suggestion["duration_ms"],
                    int(suggestion["loop"]), suggestion["volume"],
                    suggestion["confidence"], suggestion["reason"], now, now,
                ),
            )
        conn.commit()
    return workspace(db_factory, owner_username, project_id)


def workspace(db_factory, owner_username, project_id):
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        project = _project(conn, owner_username, project_id)
        set_row = conn.execute(
            "SELECT * FROM short_drama_sound_design_sets "
            "WHERE project_id=? AND status='draft'", (project_id,),
        ).fetchone()
        suggestions = []
        if set_row:
            suggestions = [
                dict(item) for item in conn.execute(
                    "SELECT s.*,shot.shot_key FROM "
                    "short_drama_sound_suggestions s "
                    "JOIN short_drama_shots shot ON shot.id=s.shot_id "
                    "WHERE s.set_id=? ORDER BY shot.sort_order,s.created_at,s.id",
                    (set_row["id"],),
                )
            ]
        jobs = _jobs(conn, project_id)
        return {
            "project_id": project_id,
            "revision": int(project["revision"]),
            "assembly_revision": _assembly_revision(conn, project_id),
            "set": dict(set_row) if set_row else None,
            "suggestions": suggestions,
            "jobs": jobs,
            "provider": provider_capability(os.environ),
            "pricing": {
                "points_per_second": POINTS_PER_SECOND,
                "analysis_cost": 0,
            },
        }


def update_suggestions(db_factory, owner_username, body):
    if not isinstance(body, dict) or set(body) != {
        "project_id", "revision", "items"
    } or not isinstance(body["items"], list):
        raise ValueError("音效建议保存请求字段不正确")
    identities = [
        str(item.get("id") or "") for item in body["items"]
        if isinstance(item, dict)
    ]
    if len(identities) != len(body["items"]) or (
        len(identities) != len(set(identities))
    ):
        raise ValueError("音效建议 ID 必须唯一")
    if len(body["items"]) > 30:
        raise ValueError("一次最多保存 30 条音效建议")
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        project = _project(
            conn, owner_username, body["project_id"], body["revision"]
        )
        active = _current_design_set(conn, project)
        now = int(time.time())
        for raw in body["items"]:
            if not isinstance(raw, dict) or set(raw) != {
                "id", "prompt", "status", "volume", "loop"
            }:
                raise ValueError("音效建议字段不正确")
            prompt = str(raw["prompt"] or "").strip()
            status = str(raw["status"] or "")
            if (
                not prompt or len(prompt) > 450
                or status not in EDITABLE_STATUSES
                or type(raw["loop"]) is not bool
            ):
                raise ValueError("音效建议内容无效")
            try:
                volume = float(raw["volume"])
            except (TypeError, ValueError):
                raise ValueError("音效建议音量无效")
            if volume < 0 or volume > 1:
                raise ValueError("音效建议音量必须在 0 到 1 之间")
            changed = conn.execute(
                "UPDATE short_drama_sound_suggestions SET prompt=?,status=?,"
                "volume=?,loop=?,updated_at=? WHERE id=? AND set_id=? "
                "AND status IN ('suggested','confirmed','rejected')",
                (
                    prompt, status, volume, int(bool(raw["loop"])), now,
                    str(raw["id"]), active["id"],
                ),
            )
            if changed.rowcount != 1:
                raise SoundDesignError(
                    "suggestion_not_editable", "音效建议已生成或不存在", 409
                )
        conn.commit()
    return workspace(db_factory, owner_username, body["project_id"])


def _selected(conn, project, ids):
    if not ids or len(ids) > 30 or len(ids) != len(set(ids)):
        raise ValueError("请选择 1 到 30 条不重复的音效建议")
    active = _current_design_set(conn, project)
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        "SELECT * FROM short_drama_sound_suggestions WHERE project_id=? "
        "AND set_id=? AND id IN (%s)" % placeholders,
        (project["id"], active["id"], *ids),
    ).fetchall()
    if len(rows) != len(ids) or any(row["status"] != "confirmed" for row in rows):
        raise SoundDesignError(
            "suggestion_not_confirmed", "仅可生成已确认的音效建议", 409
        )
    by_id = {row["id"]: row for row in rows}
    return [by_id[item] for item in ids]


def _quote_items(suggestions):
    items = []
    for item in suggestions:
        seconds = max(0.5, int(item["duration_ms"]) / 1000.0)
        cost = max(1, int(math.ceil(seconds * POINTS_PER_SECOND)))
        items.append({
            "suggestion_id": item["id"],
            "set_id": item["set_id"],
            "shot_id": item["shot_id"],
            "kind": item["kind"],
            "prompt": item["prompt"],
            "duration_seconds": round(seconds, 3),
            "loop": bool(item["loop"]),
            "cost": cost,
        })
    return items


def _check_project_budget(conn, project_id, quoted_cost, point_usage):
    project = conn.execute(
        "SELECT point_budget FROM short_drama_projects "
        "WHERE id=? AND deleted=0", (project_id,),
    ).fetchone()
    budget = int(project["point_budget"] or 0) if project else 0
    if budget <= 0 or not callable(point_usage):
        return
    usage = point_usage(conn, project_id)
    spent = max(0, int(usage.get("spent_points") or 0))
    reserved = max(0, int(usage.get("reserved_points") or 0))
    if spent + reserved + int(quoted_cost) > budget:
        raise SoundDesignError(
            "point_budget_exceeded",
            "项目点数预算不足：已用 %d 点、处理中 %d 点、本次 %d 点、预算 %d 点"
            % (spent, reserved, int(quoted_cost), budget),
            409,
        )


def prepare_quote(
    db_factory, actor_username, owner_username, body, *, point_usage=None,
):
    if not isinstance(body, dict) or set(body) != {
        "project_id", "revision", "assembly_revision", "suggestion_ids"
    }:
        raise ValueError("音效报价请求字段不正确")
    if not provider_capability(os.environ)["configured"]:
        raise SoundDesignError(
            "sound_effect_provider_unavailable",
            "AI 音效 Provider 尚未配置，当前不会扣点",
            503,
        )
    if not isinstance(body["suggestion_ids"], list):
        raise ValueError("音效建议 ID 必须是数组")
    ids = [str(item or "").strip() for item in body["suggestion_ids"]]
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        project = _project(
            conn, owner_username, body["project_id"], body["revision"]
        )
        assembly_revision = _assembly_revision(conn, body["project_id"])
        if assembly_revision != body["assembly_revision"]:
            raise SoundDesignError(
                "revision_conflict", "装配版本已更新，请刷新后重试", 409
            )
        suggestions = _selected(conn, project, ids)
        items = _quote_items(suggestions)
        total = sum(item["cost"] for item in items)
        _check_project_budget(
            conn, body["project_id"], total, point_usage
        )
        request_hash = _hash({
            "actor": actor_username, "owner": owner_username,
            "project_id": body["project_id"],
            "revision": body["revision"],
            "assembly_revision": body["assembly_revision"],
            "items": items,
        })
        now = int(time.time())
        token = "sfxq-" + uuid.uuid4().hex
        conn.execute(
            "INSERT INTO short_drama_sound_quotes("
            "token,actor_username,owner_username,project_id,project_revision,"
            "assembly_revision,request_hash,items_json,total_cost,expires_at,"
            "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                token, actor_username, owner_username, body["project_id"],
                body["revision"], body["assembly_revision"], request_hash,
                _canonical(items), total, now + QUOTE_TTL_SECONDS, now,
            ),
        )
        conn.commit()
        return {
            "quote_token": token,
            "items": items,
            "total_cost": total,
            "expires_at": now + QUOTE_TTL_SECONDS,
            "provider": provider_capability(os.environ),
        }


def submit(
    db_factory, actor_username, owner_username, body, idempotency_key,
    *, deduct_points, refund_points, enqueue, point_usage=None,
):
    if not isinstance(body, dict) or set(body) != {
        "project_id", "revision", "assembly_revision", "quote_token"
    }:
        raise ValueError("AI 音效提交请求字段不正确")
    key = str(idempotency_key or "").strip()
    if not key or len(key) > 128:
        raise ValueError("AI 音效提交必须提供 Idempotency-Key")
    request_hash = _hash({
        "actor": actor_username, "owner": owner_username, **body,
    })
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        replay = conn.execute(
            "SELECT * FROM short_drama_sound_requests "
            "WHERE actor_username=? AND idempotency_key=?",
            (actor_username, key),
        ).fetchone()
        if replay:
            if replay["request_hash"] != request_hash:
                raise SoundDesignError(
                    "idempotency_conflict",
                    "同一个 Idempotency-Key 不能用于不同请求",
                    409,
                )
            return {
                "request_id": replay["id"],
                "job_ids": [
                    row["job_id"] for row in conn.execute(
                        "SELECT job_id FROM short_drama_sound_jobs "
                        "WHERE request_id=? ORDER BY created_at,id",
                        (replay["id"],),
                    )
                ],
                "points_left": replay["points_left"],
                "replayed": True,
            }
        project = _project(
            conn, owner_username, body["project_id"], body["revision"]
        )
        if _assembly_revision(conn, body["project_id"]) != body["assembly_revision"]:
            raise SoundDesignError(
                "revision_conflict", "装配版本已更新，请重新报价", 409
            )
        quote = conn.execute(
            "SELECT * FROM short_drama_sound_quotes WHERE token=?",
            (body["quote_token"],),
        ).fetchone()
        now = int(time.time())
        if (
            not quote or quote["actor_username"] != actor_username
            or quote["owner_username"] != owner_username
            or quote["project_id"] != body["project_id"]
            or int(quote["project_revision"]) != body["revision"]
            or int(quote["assembly_revision"]) != body["assembly_revision"]
            or int(quote["expires_at"]) < now
        ):
            raise SoundDesignError(
                "quote_invalid", "报价不存在、已过期或与当前项目不匹配", 409
            )
        if quote["consumed_key"] and quote["consumed_key"] != key:
            raise SoundDesignError(
                "quote_consumed", "该报价已被其他提交使用", 409
            )
        items = json.loads(quote["items_json"])
        current_items = _quote_items(_selected(
            conn,
            project,
            [str(item.get("suggestion_id") or "") for item in items],
        ))
        if current_items != items:
            raise SoundDesignError(
                "quote_invalid",
                "音效建议已更新，请刷新后重新报价",
                409,
            )
        _check_project_budget(
            conn, body["project_id"], int(quote["total_cost"]), point_usage
        )
    request_id = "sfxreq-" + uuid.uuid4().hex
    now = int(time.time())

    def linked(connection, job_ids):
        connection.row_factory = sqlite3.Row
        connection.execute(
            "INSERT INTO short_drama_sound_requests("
            "id,actor_username,owner_username,project_id,idempotency_key,"
            "request_hash,quote_token,total_cost,state,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                request_id, actor_username, owner_username, body["project_id"],
                key, request_hash, body["quote_token"],
                int(quote["total_cost"]), "linked", now, now,
            ),
        )
        for item, job_id in zip(items, job_ids):
            connection.execute(
                "INSERT INTO short_drama_sound_jobs("
                "id,request_id,actor_username,owner_username,project_id,"
                "suggestion_id,job_id,cost,status,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "sfxjob-" + uuid.uuid4().hex, request_id, actor_username,
                    owner_username, body["project_id"], item["suggestion_id"],
                    int(job_id), int(item["cost"]), "pending", now, now,
                ),
            )
        updated = connection.execute(
            "UPDATE short_drama_sound_quotes SET consumed_key=? "
            "WHERE token=? AND consumed_key IS NULL",
            (key, body["quote_token"]),
        )
        if updated.rowcount != 1:
            raise SoundDesignError(
                "quote_consumed", "该报价已被其他提交使用", 409
            )

    payloads = [(
        int(item["cost"]),
        {
            "prompt": item["prompt"],
            "duration_seconds": item["duration_seconds"],
            "loop": item["loop"],
            "suggestion_id": item["suggestion_id"],
            "project_id": body["project_id"],
            "owner_username": owner_username,
            "shot_id": item["shot_id"],
            "kind": item["kind"],
        },
    ) for item in items]
    job_ids, points_left = jobs_store.create_paid_jobs(
        db_factory, deduct_points, refund_points,
        "short_drama_sound_effect", actor_username, payloads, "content",
        reason_kind="short_drama_sound_effect",
        before_commit=linked,
        charge_transaction_key=(
            "short-drama-sound-effect:%s:%s" % (actor_username, key)
        ),
    )
    with closing(db_factory()) as conn:
        conn.execute(
            "UPDATE short_drama_sound_requests SET points_left=?,updated_at=? "
            "WHERE id=?", (int(points_left), int(time.time()), request_id),
        )
        conn.commit()
    for job_id in job_ids:
        if not enqueue(job_id, "short_drama_sound_effect", None):
            # Jobs remain pending and the normal pending scanner will recover.
            break
    return {
        "request_id": request_id,
        "job_ids": list(job_ids),
        "points_left": int(points_left),
        "replayed": False,
    }


def _jobs(conn, project_id):
    rows = conn.execute(
        "SELECT link.*,generic.status AS generic_status,generic.error AS "
        "generic_error FROM short_drama_sound_jobs link JOIN jobs generic "
        "ON generic.id=link.job_id WHERE link.project_id=? "
        "ORDER BY link.created_at DESC,link.id", (project_id,),
    ).fetchall()
    items = []
    for item in rows:
        value = dict(item)
        value["quality"] = json.loads(value.pop("quality_json") or "{}")
        if value["status"] in {"pending", "running"}:
            value["status"] = (
                "running" if value["generic_status"] == "running" else
                "failed" if value["generic_status"] == "error" else "pending"
            )
        value["error"] = value["error"] or value["generic_error"] or ""
        items.append(value)
    return items


def recover_jobs(
    db_factory, owner_username, project_id, audio_asset_by_job,
    record_audio_asset=None,
):
    if not callable(audio_asset_by_job):
        return
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT link.job_id,generic.status,generic.result,generic.error "
            "FROM short_drama_sound_jobs link JOIN jobs generic "
            "ON generic.id=link.job_id WHERE link.project_id=? "
            "AND link.status IN ('pending','running') "
            "AND generic.status IN ('done','error')",
            (project_id,),
        ).fetchall()
    for row in rows:
        if row["status"] == "error":
            fail_job(db_factory, row["job_id"], row["error"] or "生成失败")
            continue
        try:
            result = json.loads(row["result"] or "{}")
        except (TypeError, ValueError):
            result = {}
        asset = audio_asset_by_job(owner_username, row["job_id"])
        if not asset and callable(record_audio_asset):
            try:
                record_audio_asset(row["job_id"], owner_username, result)
                asset = audio_asset_by_job(owner_username, row["job_id"])
            except Exception:
                # The generic job result is the durable recovery anchor. Keep the
                # business job retryable; a later poll can safely replay this
                # idempotent materialization without charging or generating again.
                continue
        if asset:
            reconcile_job(db_factory, row["job_id"], result, asset)


def jobs(
    db_factory, owner_username, project_id, audio_asset_by_job=None,
    record_audio_asset=None,
):
    recover_jobs(
        db_factory, owner_username, project_id, audio_asset_by_job,
        record_audio_asset,
    )
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        _project(conn, owner_username, project_id)
        return {"items": _jobs(conn, project_id)}


def reconcile_job(db_factory, job_id, result, asset):
    if not asset:
        return None
    quality = dict((result or {}).get("quality") or {})
    decision = str(quality.get("decision") or "manual_review")
    status = "done" if decision == "passed" else "manual_review"
    now = int(time.time())
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        linked = conn.execute(
            "SELECT * FROM short_drama_sound_jobs WHERE job_id=?",
            (int(job_id),),
        ).fetchone()
        if not linked:
            return None
        conn.execute(
            "UPDATE short_drama_sound_jobs SET status=?,asset_id=?,"
            "quality_json=?,updated_at=? WHERE job_id=?",
            (
                status, int(asset["id"]) if asset else None,
                _canonical(quality), now, int(job_id),
            ),
        )
        conn.execute(
            "UPDATE short_drama_sound_suggestions SET status='generated',"
            "updated_at=? WHERE id=?", (now, linked["suggestion_id"]),
        )
        _reconcile_request(conn, linked["request_id"], now)
        conn.commit()
        return {"status": status, "asset_id": asset["id"] if asset else None}


def fail_job(db_factory, job_id, error):
    now = int(time.time())
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        linked = conn.execute(
            "SELECT request_id FROM short_drama_sound_jobs WHERE job_id=?",
            (int(job_id),),
        ).fetchone()
        if not linked:
            return None
        conn.execute(
            "UPDATE short_drama_sound_jobs SET status='failed',error=?,"
            "updated_at=? WHERE job_id=?",
            (str(error)[:500], now, int(job_id)),
        )
        _reconcile_request(conn, linked["request_id"], now)
        conn.commit()
        return True


def _reconcile_request(conn, request_id, now):
    states = [
        row["status"] for row in conn.execute(
            "SELECT status FROM short_drama_sound_jobs WHERE request_id=?",
            (request_id,),
        )
    ]
    if any(state in ACTIVE_JOB_STATES for state in states):
        return
    if all(state in {"done", "manual_review", "applied"} for state in states):
        state = "done"
    elif all(state == "failed" for state in states):
        state = "failed"
    else:
        state = "partial"
    conn.execute(
        "UPDATE short_drama_sound_requests SET state=?,updated_at=? WHERE id=?",
        (state, now, request_id),
    )


def apply_generated(
    db_factory, owner_username, body, *, assembly_module, audio_asset_lookup,
):
    allowed = {
        "project_id", "revision", "assembly_revision", "job_ids",
        "approve_manual_review",
    }
    if not isinstance(body, dict) or set(body) != allowed:
        raise ValueError("应用 AI 音效请求字段不正确")
    raw_ids = body["job_ids"]
    if not isinstance(raw_ids, list) or not raw_ids:
        raise ValueError("请选择至少一个已生成音效")
    job_ids = [int(item) for item in raw_ids]
    if len(job_ids) != len(set(job_ids)):
        raise ValueError("AI 音效任务 ID 不能重复")
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        _project(conn, owner_username, body["project_id"], body["revision"])
        placeholders = ",".join("?" for _ in job_ids)
        rows = conn.execute(
            "SELECT link.*,suggestion.shot_id,suggestion.kind,"
            "suggestion.start_ms,suggestion.end_ms,suggestion.loop,"
            "suggestion.volume FROM short_drama_sound_jobs link "
            "JOIN short_drama_sound_suggestions suggestion "
            "ON suggestion.id=link.suggestion_id "
            "WHERE link.project_id=? AND link.job_id IN (%s)" % placeholders,
            (body["project_id"], *job_ids),
        ).fetchall()
        if len(rows) != len(job_ids):
            raise LookupError("AI 音效任务不存在")
        allowed_states = {"done"}
        if bool(body["approve_manual_review"]):
            allowed_states.add("manual_review")
        if any(row["status"] not in allowed_states or not row["asset_id"]
               for row in rows):
            raise SoundDesignError(
                "sound_effect_not_ready",
                "存在未完成或需要人工确认的音效任务",
                409,
            )
    snapshot = assembly_module.get_assembly_workspace(
        db_factory, owner_username, body["project_id"],
        bgm_lookup=audio_asset_lookup,
    )
    source_config = snapshot["config"]
    config = {
        "subtitle": {
            key: source_config["subtitle"][key]
            for key in ("enabled", "preset", "position")
        },
        "bgm": {
            key: source_config["bgm"][key]
            for key in ("asset_id", "volume", "fade_in_ms", "fade_out_ms")
        },
        "sound_cues": json.loads(json.dumps(
            source_config.get("sound_cues") or [], ensure_ascii=False
        )),
    }
    existing = {
        str(item.get("id") or "").removeprefix("ai-sfx-") for item in
        config.get("sound_cues") or []
    }
    for row in rows:
        if str(row["job_id"]) in existing:
            continue
        config.setdefault("sound_cues", []).append({
            "id": "ai-sfx-%s" % row["job_id"],
            "shot_id": row["shot_id"],
            "kind": row["kind"],
            "asset_id": int(row["asset_id"]),
            "start_ms": int(row["start_ms"]),
            "end_ms": int(row["end_ms"]),
            "loop": bool(row["loop"]),
            "volume": float(row["volume"]),
            "fade_in_ms": 0,
            "fade_out_ms": 0,
            "enabled": True,
        })
    saved = assembly_module.save_assembly_config(
        db_factory, owner_username, {
            "project_id": body["project_id"],
            "revision": body["revision"],
            "assembly_revision": body["assembly_revision"],
            "config": config,
        }, audio_asset_lookup,
    )
    now = int(time.time())
    with closing(db_factory()) as conn:
        conn.execute(
            "UPDATE short_drama_sound_jobs SET status='applied',updated_at=? "
            "WHERE project_id=? AND job_id IN (%s)" % placeholders,
            (now, body["project_id"], *job_ids),
        )
        conn.execute(
            "UPDATE short_drama_sound_suggestions SET status='applied',"
            "updated_at=? WHERE id IN (SELECT suggestion_id FROM "
            "short_drama_sound_jobs WHERE project_id=? AND job_id IN (%s))"
            % placeholders,
            (now, body["project_id"], *job_ids),
        )
        conn.commit()
    saved["applied_job_ids"] = job_ids
    return saved
