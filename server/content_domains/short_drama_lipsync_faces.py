"""Versioned, project-scoped face analysis for PR-J.

This module deliberately stops at proposals and an audited manual confirmation.
It never submits a provider job and never stores face embeddings.
"""

from contextlib import closing
import hashlib
import json
import math
import os
import sqlite3
import time
import uuid

from providers.faces import FakeFaceAnalysisProvider

from . import short_drama_lipsync_snapshot


CONTRACT_VERSION = "short-drama-lipsync-face-analysis-v2"
TRACK_CONTRACT_VERSION = "short-drama-lipsync-face-tracks-v1"
_ENABLED_ENV = "HQ_SHORT_DRAMA_FACE_ANALYSIS_ENABLED"
MAX_PROVIDER_RESULT_BYTES = 512 * 1024
MAX_DETECTIONS = 2000
MAX_TRACKS = 64
MAX_MATCHES = 256
MAX_PROPOSALS = 240
MAX_CANDIDATES = 64
MAX_LIMITATIONS = 32

_SCHEMA = """
CREATE TABLE IF NOT EXISTS short_drama_face_analyses (
  id TEXT PRIMARY KEY,
  owner TEXT NOT NULL,
  project_id TEXT NOT NULL,
  shot_id TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  timeline_hash TEXT NOT NULL,
  visual_source_hash TEXT NOT NULL,
  reference_set_hash TEXT NOT NULL,
  reference_set_json TEXT NOT NULL,
  provider TEXT NOT NULL,
  detector_version TEXT NOT NULL,
  tracker_version TEXT NOT NULL,
  matcher_version TEXT NOT NULL,
  params_json TEXT NOT NULL,
  result_json TEXT NOT NULL,
  result_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK (
    status IN ('proposed','manual_review','failed')
  ),
  created_by TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  UNIQUE(project_id,shot_id,input_hash,provider)
);
CREATE INDEX IF NOT EXISTS idx_face_analyses_project
  ON short_drama_face_analyses(project_id,created_at DESC);

CREATE TABLE IF NOT EXISTS short_drama_face_track_versions (
  id TEXT PRIMARY KEY,
  owner TEXT NOT NULL,
  project_id TEXT NOT NULL,
  shot_id TEXT NOT NULL,
  analysis_id TEXT NOT NULL,
  source_result_hash TEXT NOT NULL,
  source_input_hash TEXT NOT NULL,
  mapping_json TEXT NOT NULL,
  mapping_hash TEXT NOT NULL,
  revision INTEGER NOT NULL,
  review_mode TEXT NOT NULL CHECK (
    review_mode IN ('manual_confirmed','manual_adjusted')
  ),
  reviewed_by TEXT NOT NULL,
  review_reason TEXT NOT NULL,
  reviewed_at INTEGER NOT NULL,
  created_at INTEGER NOT NULL,
  UNIQUE(project_id,shot_id,revision)
);
CREATE TABLE IF NOT EXISTS short_drama_face_track_current (
  project_id TEXT NOT NULL,
  shot_id TEXT NOT NULL,
  version_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY(project_id,shot_id)
);
"""


class FaceAnalysisError(RuntimeError):
    def __init__(self, code, message, *, status=422, blockers=None):
        super().__init__(message)
        self.code = str(code)
        self.status = int(status)
        self.blockers = list(blockers or [])


def init_db(db_factory):
    with closing(db_factory()) as conn:
        conn.executescript(_SCHEMA)
        conn.commit()


def _canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _hash(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _forbidden_result_key(value):
    biometric_markers = {
        "embedding", "embeddings", "vector", "vectors",
        "biometric", "template", "descriptor", "feature",
    }
    secrets = {
        "authorization", "cookie", "set-cookie", "api_key", "apikey",
        "access_token", "refresh_token", "password", "secret",
    }
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            key_parts = {
                part for part in normalized.split("_") if part
            }
            if key_parts & biometric_markers:
                return "biometric"
            if normalized in secrets:
                return "secret"
            found = _forbidden_result_key(item)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _forbidden_result_key(item)
            if found:
                return found
    return None


def _enabled():
    return str(os.getenv(_ENABLED_ENV, "")).strip().lower() in {
        "1", "true", "yes", "on",
    }


def _require_enabled():
    if not _enabled():
        raise FaceAnalysisError(
            "face_analysis_disabled",
            "多人对白人脸分析功能尚未开放",
            status=503,
        )


def _project(conn, owner, project_id):
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM short_drama_projects "
        "WHERE id=? AND username=? AND deleted=0",
        (project_id, owner),
    ).fetchone()
    if not row:
        raise LookupError("short drama project does not exist")
    if str(row["stage"] or "") != "video_review":
        raise FaceAnalysisError(
            "project_stage_readonly",
            "当前项目阶段不能创建或确认人脸跟踪版本",
            status=409,
        )
    return row


def _references(conn, project_id, payload, segments):
    segment_keys = {
        str(item.get("character_key") or "").strip()
        for item in segments
        if str(item.get("character_key") or "").strip()
    }
    raw = payload.get("character_references")
    if raw is None:
        requested_keys = set(segment_keys)
    else:
        if not isinstance(raw, list) or not raw:
            raise FaceAnalysisError(
                "missing_character_references",
                "当前镜头没有可用于项目内匹配的人物参考集合",
            )
        requested_keys = set()
        for item in raw:
            if (
                not isinstance(item, dict)
                or not set(item).issubset({
                    "character_key", "reference_asset_ids",
                })
            ):
                raise FaceAnalysisError(
                    "invalid_character_references",
                    "人物参考集合格式无效",
                )
            key = str(item.get("character_key") or "").strip()
            assets = item.get("reference_asset_ids") or []
            if not isinstance(assets, list):
                raise FaceAnalysisError(
                    "invalid_character_references",
                    "人物参考资产格式无效",
                )
            if assets:
                raise FaceAnalysisError(
                    "client_reference_assets_forbidden",
                    "人物参考资产必须由服务端从项目锁定版本解析",
                )
            if not key or key in requested_keys:
                raise FaceAnalysisError(
                    "invalid_character_references",
                    "人物参考集合必须包含唯一的角色标识",
                )
            requested_keys.add(key)
    if not requested_keys or requested_keys != segment_keys:
        raise FaceAnalysisError(
            "invalid_character_references",
            "人物参考集合必须与当前镜头的项目角色完全一致",
        )
    placeholders = ",".join("?" for _ in requested_keys)
    rows = conn.execute(
        "SELECT character_key,reference_job_id,reference_file,"
        "reference_url,reference_version,reference_locked "
        "FROM short_drama_characters WHERE project_id=? "
        "AND character_key IN (" + placeholders + ")",
        (project_id, *sorted(requested_keys)),
    ).fetchall()
    by_key = {str(row["character_key"]): row for row in rows}
    if set(by_key) != requested_keys:
        raise FaceAnalysisError(
            "project_character_not_found",
            "人物参考只能使用当前项目中存在的角色",
        )
    result = []
    for key in sorted(requested_keys):
        row = by_key[key]
        version = int(row["reference_version"] or 0)
        reference_file = str(row["reference_file"] or "").strip()
        reference_url = str(row["reference_url"] or "").strip()
        if (
            not bool(row["reference_locked"])
            or version < 1
            or not (reference_file or reference_url)
        ):
            raise FaceAnalysisError(
                "character_reference_not_locked",
                "项目角色缺少已锁定的人物参考版本",
            )
        identity = {
            "project_id": project_id,
            "character_key": key,
            "reference_job_id": row["reference_job_id"],
            "reference_file": reference_file,
            "reference_url": reference_url,
            "reference_version": version,
        }
        result.append({
            "character_key": key,
            "reference_version": version,
            "reference_identity_hash": _hash(identity),
        })
    return result


def _shot_contract(conn, project, payload):
    snapshot = short_drama_lipsync_snapshot.build_snapshot(
        conn, project, can_write=True
    )
    shot_id = str(payload.get("shot_id") or "").strip()
    visible = [
        item for item in snapshot["dependencies"]["timeline"]["visible_segments"]
        if str(item["shot_id"]) == shot_id
    ]
    if not shot_id or not visible:
        raise FaceAnalysisError(
            "missing_visible_dialogue",
            "指定镜头没有可见对白，不能启动多人跟踪分析",
        )
    ordered = sorted(visible, key=lambda item: (item["start_ms"], item["id"]))
    for previous, current in zip(ordered, ordered[1:]):
        if int(current["start_ms"]) < int(previous["end_ms"]):
            raise FaceAnalysisError(
                "overlapping_visible_speech",
                "PR-J 首版仅支持不重叠的多人轮流对白",
                blockers=[{
                    "code": "overlapping_visible_speech",
                    "shot_id": shot_id,
                    "segment_ids": [previous["id"], current["id"]],
                }],
            )
    visual = next((
        item for item in snapshot["dependencies"]["visual_sources"]
        if str(item["shot_id"]) == shot_id
    ), None)
    if not visual:
        raise FaceAnalysisError(
            "missing_locked_visual", "镜头缺少已锁定的画面素材"
        )
    references = _references(
        conn, project["id"], payload, ordered
    )
    params = payload.get("params") or {}
    if not isinstance(params, dict):
        raise FaceAnalysisError("invalid_analysis_params", "分析参数格式无效")
    return snapshot, shot_id, ordered, visual, references, params


def _row(row):
    item = dict(row)
    item["contract_version"] = CONTRACT_VERSION
    item["params"] = json.loads(item.pop("params_json"))
    item["character_references"] = json.loads(
        item.pop("reference_set_json")
    )
    item["result"] = json.loads(item.pop("result_json"))
    item["manual_confirmation_required"] = True
    item["can_create_paid_job"] = False
    return item


def _invalid_provider_result(message):
    raise FaceAnalysisError("invalid_provider_result", message)


def _strict_object(value, required, *, label):
    if not isinstance(value, dict) or set(value) != set(required):
        _invalid_provider_result(
            "%s 字段结构无效或包含未知字段" % label
        )
    return value


def _bounded_list(value, maximum, *, label, required=True):
    if not isinstance(value, list):
        _invalid_provider_result("%s 必须是数组" % label)
    if len(value) > maximum or (required and not value):
        _invalid_provider_result("%s 数量无效" % label)
    return value


def _text(value, maximum, *, label, choices=None):
    if not isinstance(value, str) or not value or len(value) > maximum:
        _invalid_provider_result("%s 文本无效" % label)
    if choices is not None and value not in choices:
        _invalid_provider_result("%s 取值无效" % label)
    return value


def _integer(value, minimum, maximum, *, label):
    if (
        type(value) is not int
        or value < minimum
        or value > maximum
    ):
        _invalid_provider_result("%s 整数无效" % label)
    return value


def _number(value, minimum, maximum, *, label):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < minimum
        or float(value) > maximum
    ):
        _invalid_provider_result("%s 数值无效" % label)
    return round(float(value), 6)


def _bbox(value, *, label):
    if not isinstance(value, list) or len(value) != 4:
        _invalid_provider_result("%s 边界框无效" % label)
    result = [
        _number(item, 0.0, 1.0, label="%s[%d]" % (label, index))
        for index, item in enumerate(value)
    ]
    if result[2] <= 0 or result[3] <= 0:
        _invalid_provider_result("%s 宽高必须大于零" % label)
    if result[0] + result[2] > 1.000001:
        _invalid_provider_result("%s 横向越界" % label)
    if result[1] + result[3] > 1.000001:
        _invalid_provider_result("%s 纵向越界" % label)
    return result


def _normalize_provider_result(
    value, *, segment_ids, references, reference_set_hash
):
    if not isinstance(value, dict):
        _invalid_provider_result("人脸分析 Provider 返回格式无效")
    forbidden = _forbidden_result_key(value)
    if forbidden == "biometric":
        raise FaceAnalysisError(
            "biometric_payload_rejected",
            "分析结果不得包含原始人脸向量或生物特征模板",
        )
    if forbidden == "secret":
        raise FaceAnalysisError(
            "sensitive_provider_payload_rejected",
            "分析结果不得包含 Provider 凭据或认证头",
        )
    _strict_object(
        value,
        {"detections", "tracks", "matches", "proposals", "limitations"},
        label="provider result",
    )
    characters = {
        str(item["character_key"]) for item in references
    }
    expected_segments = {str(item) for item in segment_ids}

    tracks = []
    track_ids = set()
    for index, item in enumerate(_bounded_list(
        value["tracks"], MAX_TRACKS, label="tracks"
    )):
        item = _strict_object(item, {
            "track_id", "spans", "first_ms", "last_ms", "coverage",
            "gap_ms", "stability", "bbox", "tracker_version",
        }, label="tracks[%d]" % index)
        track_id = _text(
            item["track_id"], 120, label="track_id"
        )
        if track_id in track_ids:
            _invalid_provider_result("track_id 不得重复")
        track_ids.add(track_id)
        first_ms = _integer(
            item["first_ms"], 0, 86_400_000, label="first_ms"
        )
        last_ms = _integer(
            item["last_ms"], first_ms, 86_400_000, label="last_ms"
        )
        spans = []
        for span_index, span in enumerate(_bounded_list(
            item["spans"], 256, label="track spans"
        )):
            span = _strict_object(
                span, {"start_ms", "end_ms"},
                label="track span[%d]" % span_index,
            )
            start_ms = _integer(
                span["start_ms"], first_ms, last_ms, label="span start"
            )
            end_ms = _integer(
                span["end_ms"], start_ms, last_ms, label="span end"
            )
            spans.append({"start_ms": start_ms, "end_ms": end_ms})
        tracks.append({
            "track_id": track_id,
            "spans": spans,
            "first_ms": first_ms,
            "last_ms": last_ms,
            "coverage": _number(
                item["coverage"], 0.0, 1.0, label="coverage"
            ),
            "gap_ms": _integer(
                item["gap_ms"], 0, 86_400_000, label="gap_ms"
            ),
            "stability": _number(
                item["stability"], 0.0, 1.0, label="stability"
            ),
            "bbox": _bbox(item["bbox"], label="track bbox"),
            "tracker_version": _text(
                item["tracker_version"], 120, label="tracker_version"
            ),
        })

    detections = []
    detected_tracks = set()
    for index, item in enumerate(_bounded_list(
        value["detections"], MAX_DETECTIONS, label="detections"
    )):
        item = _strict_object(item, {
            "time_ms", "frame", "track_id", "bbox", "landmarks",
            "pose", "occlusion", "blur", "visibility", "confidence",
        }, label="detections[%d]" % index)
        track_id = _text(item["track_id"], 120, label="detection track")
        if track_id not in track_ids:
            _invalid_provider_result("detection 引用了未知 track")
        if item["landmarks"] != []:
            _invalid_provider_result(
                "首版结果不得保存人脸 landmarks"
            )
        pose = _strict_object(
            item["pose"], {"yaw", "pitch", "roll"}, label="pose"
        )
        detected_tracks.add(track_id)
        detections.append({
            "time_ms": _integer(
                item["time_ms"], 0, 86_400_000, label="time_ms"
            ),
            "frame": _integer(
                item["frame"], 0, 10_000_000, label="frame"
            ),
            "track_id": track_id,
            "bbox": _bbox(item["bbox"], label="detection bbox"),
            "landmarks": [],
            "pose": {
                key: _number(
                    pose[key], -180.0, 180.0, label="pose " + key
                )
                for key in ("yaw", "pitch", "roll")
            },
            "occlusion": _number(
                item["occlusion"], 0.0, 1.0, label="occlusion"
            ),
            "blur": _number(item["blur"], 0.0, 1.0, label="blur"),
            "visibility": _number(
                item["visibility"], 0.0, 1.0, label="visibility"
            ),
            "confidence": _number(
                item["confidence"], 0.0, 1.0, label="confidence"
            ),
        })
    if detected_tracks != track_ids:
        _invalid_provider_result("每条 track 必须至少有一个 detection")

    matches = []
    match_pairs = set()
    for index, item in enumerate(_bounded_list(
        value["matches"], MAX_MATCHES, label="matches"
    )):
        item = _strict_object(item, {
            "track_id", "character_key", "score", "margin_to_second",
            "reference_set_hash", "model_version",
        }, label="matches[%d]" % index)
        track_id = _text(item["track_id"], 120, label="match track")
        character_key = _text(
            item["character_key"], 120, label="match character"
        )
        pair = (track_id, character_key)
        if (
            track_id not in track_ids
            or character_key not in characters
            or pair in match_pairs
            or item["reference_set_hash"] != reference_set_hash
        ):
            _invalid_provider_result(
                "match 必须引用唯一的项目内 track/角色/参考版本"
            )
        match_pairs.add(pair)
        matches.append({
            "track_id": track_id,
            "character_key": character_key,
            "score": _number(item["score"], 0.0, 1.0, label="match score"),
            "margin_to_second": _number(
                item["margin_to_second"], 0.0, 1.0,
                label="margin_to_second",
            ),
            "reference_set_hash": reference_set_hash,
            "model_version": _text(
                item["model_version"], 120, label="model_version"
            ),
        })

    proposals = []
    proposed_segments = set()
    for index, item in enumerate(_bounded_list(
        value["proposals"], MAX_PROPOSALS, label="proposals"
    )):
        item = _strict_object(item, {
            "segment_id", "candidates", "confidence", "reason_codes",
            "recommended_action",
        }, label="proposals[%d]" % index)
        segment_id = _text(
            item["segment_id"], 160, label="proposal segment"
        )
        if (
            segment_id not in expected_segments
            or segment_id in proposed_segments
        ):
            _invalid_provider_result(
                "proposal 必须唯一覆盖当前可见对白"
            )
        proposed_segments.add(segment_id)
        candidates = []
        candidate_pairs = set()
        for candidate_index, candidate in enumerate(_bounded_list(
            item["candidates"], MAX_CANDIDATES,
            label="proposal candidates",
        )):
            candidate = _strict_object(candidate, {
                "face_track_id", "character_key", "score",
            }, label="candidate[%d]" % candidate_index)
            pair = (
                _text(
                    candidate["face_track_id"], 120,
                    label="candidate track",
                ),
                _text(
                    candidate["character_key"], 120,
                    label="candidate character",
                ),
            )
            if pair not in match_pairs or pair in candidate_pairs:
                _invalid_provider_result(
                    "candidate 必须引用唯一的已验证 match 关系"
                )
            candidate_pairs.add(pair)
            candidates.append({
                "face_track_id": pair[0],
                "character_key": pair[1],
                "score": _number(
                    candidate["score"], 0.0, 1.0,
                    label="candidate score",
                ),
            })
        reasons = [
            _text(reason, 120, label="reason_code")
            for reason in _bounded_list(
                item["reason_codes"], 16,
                label="reason_codes", required=False,
            )
        ]
        proposals.append({
            "segment_id": segment_id,
            "candidates": sorted(
                candidates,
                key=lambda candidate: (
                    -candidate["score"],
                    candidate["face_track_id"],
                    candidate["character_key"],
                ),
            ),
            "confidence": _number(
                item["confidence"], 0.0, 1.0,
                label="proposal confidence",
            ),
            "reason_codes": reasons,
            "recommended_action": _text(
                item["recommended_action"], 32,
                label="recommended_action",
                choices={"confirm", "manual_review"},
            ),
        })
    if proposed_segments != expected_segments:
        _invalid_provider_result(
            "proposals 必须完整覆盖当前可见对白"
        )

    limitations = sorted({
        _text(item, 200, label="limitation")
        for item in _bounded_list(
            value["limitations"], MAX_LIMITATIONS,
            label="limitations", required=False,
        )
    })
    normalized = {
        "detections": sorted(
            detections,
            key=lambda item: (item["time_ms"], item["track_id"]),
        ),
        "tracks": sorted(tracks, key=lambda item: item["track_id"]),
        "matches": sorted(
            matches,
            key=lambda item: (item["track_id"], item["character_key"]),
        ),
        "proposals": sorted(
            proposals, key=lambda item: item["segment_id"]
        ),
        "limitations": limitations,
    }
    if len(_canonical(normalized).encode("utf-8")) > MAX_PROVIDER_RESULT_BYTES:
        _invalid_provider_result("Provider 结果超过允许大小")
    return normalized


def _analysis_contract(conn, owner, payload, capabilities):
    project = _project(
        conn, owner, str(payload.get("project_id") or "")
    )
    snapshot, shot_id, segments, visual, references, params = (
        _shot_contract(conn, project, payload)
    )
    reference_set_hash = _hash(references)
    identity = {
        "contract_version": CONTRACT_VERSION,
        "project_id": project["id"],
        "shot_id": shot_id,
        "timeline_hash": (
            snapshot["dependencies"]["timeline"]["timeline_hash"]
        ),
        "visual_source_hash": visual["source_hash"],
        "reference_set_hash": reference_set_hash,
        "provider": capabilities.to_dict(),
        "params": params,
    }
    return {
        "project_id": project["id"],
        "shot_id": shot_id,
        "segments": segments,
        "visual": visual,
        "references": references,
        "reference_set_hash": reference_set_hash,
        "params": params,
        "identity": identity,
        "input_hash": _hash(identity),
    }


def _existing_analysis(conn, contract, provider_name):
    return conn.execute(
        "SELECT * FROM short_drama_face_analyses "
        "WHERE project_id=? AND shot_id=? AND input_hash=? AND provider=?",
        (
            contract["project_id"], contract["shot_id"],
            contract["input_hash"], provider_name,
        ),
    ).fetchone()


def analyze(db_factory, owner, actor, payload, *, provider=None):
    _require_enabled()
    provider = provider or FakeFaceAnalysisProvider()
    capabilities = provider.capabilities
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        contract = _analysis_contract(
            conn, owner, payload, capabilities
        )
        existing = _existing_analysis(
            conn, contract, capabilities.name
        )
        if existing:
            result = _row(existing)
            result["reused"] = True
            return result

    provider_result = provider.analyze({
        "project_id": contract["project_id"],
        "shot_id": contract["shot_id"],
        "segments": contract["segments"],
        "character_references": contract["references"],
        "reference_set_hash": contract["reference_set_hash"],
        "visual_source": contract["visual"],
        "params": contract["params"],
    })
    normalized = _normalize_provider_result(
        provider_result,
        segment_ids=[
            item["id"] for item in contract["segments"]
        ],
        references=contract["references"],
        reference_set_hash=contract["reference_set_hash"],
    )
    serialized = _canonical(normalized)
    result_hash = _hash(normalized)
    status = (
        "manual_review"
        if any(
            item["recommended_action"] != "confirm"
            for item in normalized["proposals"]
        )
        else "proposed"
    )
    analysis_id = uuid.uuid4().hex
    now = int(time.time())
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        current = _analysis_contract(
            conn, owner, payload, capabilities
        )
        if current["input_hash"] != contract["input_hash"]:
            raise FaceAnalysisError(
                "stale_face_analysis_input",
                "Provider 分析期间项目依赖已变化，请重新分析",
                status=409,
            )
        existing = _existing_analysis(
            conn, current, capabilities.name
        )
        if existing:
            conn.commit()
            result = _row(existing)
            result["reused"] = True
            return result
        conn.execute(
            "INSERT INTO short_drama_face_analyses "
            "(id,owner,project_id,shot_id,input_hash,timeline_hash,"
            "visual_source_hash,reference_set_hash,reference_set_json,"
            "provider,detector_version,"
            "tracker_version,matcher_version,params_json,result_json,"
            "result_hash,status,created_by,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                analysis_id, owner, current["project_id"],
                current["shot_id"], current["input_hash"],
                current["identity"]["timeline_hash"],
                current["visual"]["source_hash"],
                current["reference_set_hash"],
                _canonical(current["references"]), capabilities.name,
                capabilities.detector_version,
                capabilities.tracker_version,
                capabilities.matcher_version,
                _canonical(current["params"]), serialized,
                result_hash, status, actor, now,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM short_drama_face_analyses WHERE id=?",
            (analysis_id,),
        ).fetchone()
        value = _row(row)
        value["reused"] = False
        return value


def get_analysis(db_factory, owner, analysis_id):
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM short_drama_face_analyses WHERE id=? AND owner=?",
            (analysis_id, owner),
        ).fetchone()
        if not row:
            raise LookupError("face analysis does not exist")
        return _row(row)


def analysis_project_id(db_factory, analysis_id):
    with closing(db_factory()) as conn:
        row = conn.execute(
            "SELECT project_id FROM short_drama_face_analyses WHERE id=?",
            (analysis_id,),
        ).fetchone()
        if not row:
            raise LookupError("face analysis does not exist")
        return str(row[0])


def _validated_mapping(result, payload):
    mapping = payload.get("mapping")
    if not isinstance(mapping, list) or not mapping:
        raise FaceAnalysisError(
            "incomplete_face_mapping", "必须逐段确认人物与人脸轨迹的映射"
        )
    proposals = {
        str(item["segment_id"]): item
        for item in result.get("proposals") or []
    }
    candidate_relations = {
        (
            str(proposal["segment_id"]),
            str(candidate["face_track_id"]),
            str(candidate["character_key"]),
        )
        for proposal in result.get("proposals") or []
        for candidate in proposal.get("candidates") or []
    }
    normalized = []
    seen = set()
    for item in mapping:
        if (
            not isinstance(item, dict)
            or set(item) != {
                "segment_id", "face_track_id", "character_key",
            }
        ):
            raise FaceAnalysisError("invalid_face_mapping", "映射格式无效")
        segment_id = str(item.get("segment_id") or "")
        track_id = str(item.get("face_track_id") or "")
        character_key = str(item.get("character_key") or "")
        if (
            segment_id not in proposals or segment_id in seen
            or (segment_id, track_id, character_key)
            not in candidate_relations
        ):
            raise FaceAnalysisError(
                "invalid_face_mapping",
                "映射必须使用该对白候选中的项目角色与人脸轨迹组合",
            )
        seen.add(segment_id)
        normalized.append({
            "segment_id": segment_id,
            "face_track_id": track_id,
            "character_key": character_key[:120],
        })
    if seen != set(proposals):
        raise FaceAnalysisError(
            "incomplete_face_mapping", "所有可见对白都必须由人工确认"
        )
    return sorted(normalized, key=lambda item: item["segment_id"])


def confirm(db_factory, owner, actor, payload):
    _require_enabled()
    analysis_id = str(payload.get("analysis_id") or "")
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        analysis = conn.execute(
            "SELECT * FROM short_drama_face_analyses WHERE id=? AND owner=?",
            (analysis_id, owner),
        ).fetchone()
        if not analysis:
            raise LookupError("face analysis does not exist")
        project = _project(conn, owner, analysis["project_id"])
        supplied_references = payload.get("character_references")
        snapshot, shot_id, _, visual, references, _ = _shot_contract(
            conn, project, {
                "shot_id": analysis["shot_id"],
                **(
                    {"character_references": supplied_references}
                    if supplied_references is not None else {}
                ),
            },
        )
        expected = {
            "input_hash": str(payload.get("expected_input_hash") or ""),
            "result_hash": str(payload.get("expected_result_hash") or ""),
        }
        if (
            expected["input_hash"] != analysis["input_hash"]
            or expected["result_hash"] != analysis["result_hash"]
            or snapshot["dependencies"]["timeline"]["timeline_hash"]
            != analysis["timeline_hash"]
            or visual["source_hash"] != analysis["visual_source_hash"]
            or _hash(references) != analysis["reference_set_hash"]
        ):
            raise FaceAnalysisError(
                "stale_face_analysis",
                "人物、时间线或画面素材已变化，请重新分析",
                status=409,
            )
        result = json.loads(analysis["result_json"])
        mapping = _validated_mapping(result, payload)
        mode = str(payload.get("review_mode") or "")
        if mode not in {"manual_confirmed", "manual_adjusted"}:
            raise FaceAnalysisError(
                "manual_review_required", "必须明确选择人工确认方式"
            )
        reason = str(payload.get("review_reason") or "").strip()
        if not reason:
            raise FaceAnalysisError(
                "review_reason_required", "人工确认必须填写审核说明"
            )
        current = conn.execute(
            "SELECT * FROM short_drama_face_track_current "
            "WHERE project_id=? AND shot_id=?",
            (analysis["project_id"], shot_id),
        ).fetchone()
        expected_revision = int(payload.get("expected_revision") or 0)
        actual_revision = int(current["revision"]) if current else 0
        if expected_revision != actual_revision:
            raise FaceAnalysisError(
                "face_track_revision_changed",
                "人脸跟踪版本已变化，请刷新后重试",
                status=409,
            )
        revision = actual_revision + 1
        now = int(time.time())
        version_id = uuid.uuid4().hex
        mapping_hash = _hash({
            "contract_version": TRACK_CONTRACT_VERSION,
            "analysis_id": analysis_id,
            "source_result_hash": analysis["result_hash"],
            "mapping": mapping,
        })
        conn.execute(
            "INSERT INTO short_drama_face_track_versions "
            "(id,owner,project_id,shot_id,analysis_id,source_result_hash,"
            "source_input_hash,mapping_json,mapping_hash,revision,review_mode,"
            "reviewed_by,review_reason,reviewed_at,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                version_id, owner, analysis["project_id"], shot_id, analysis_id,
                analysis["result_hash"], analysis["input_hash"],
                _canonical(mapping), mapping_hash, revision, mode, actor,
                reason[:500], now, now,
            ),
        )
        conn.execute(
            "INSERT INTO short_drama_face_track_current "
            "(project_id,shot_id,version_id,revision,updated_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(project_id,shot_id) DO UPDATE SET "
            "version_id=excluded.version_id,revision=excluded.revision,"
            "updated_at=excluded.updated_at",
            (analysis["project_id"], shot_id, version_id, revision, now),
        )
        conn.commit()
        return {
            "id": version_id,
            "project_id": analysis["project_id"],
            "shot_id": shot_id,
            "analysis_id": analysis_id,
            "mapping": mapping,
            "mapping_hash": mapping_hash,
            "revision": revision,
            "review_mode": mode,
            "reviewed_by": actor,
            "reviewed_at": now,
            "locked": True,
            "creates_paid_job": False,
        }


def get_current(db_factory, owner, project_id, shot_id):
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        project = conn.execute(
            "SELECT id FROM short_drama_projects "
            "WHERE id=? AND username=? AND deleted=0",
            (project_id, owner),
        ).fetchone()
        if not project:
            raise LookupError("short drama project does not exist")
        row = conn.execute(
            "SELECT version.* FROM short_drama_face_track_current current "
            "JOIN short_drama_face_track_versions version "
            "ON version.id=current.version_id "
            "WHERE current.project_id=? AND current.shot_id=?",
            (project_id, shot_id),
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["mapping"] = json.loads(result.pop("mapping_json"))
        return result
