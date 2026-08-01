"""PR-H deterministic lipsync source selection for assembly and delivery."""

import hashlib
import hmac
import json
import os
import time

from . import short_drama_assembly_plan as media_plan
from . import short_drama_lipsync_snapshot


CONTRACT_VERSION = "short-drama-lipsync-assembly-v1"
MANIFEST_CONTRACT_VERSION = "short-drama-composition-manifest-v1"
_PLAN_HASH_NOT_PROVIDED = object()
UNSETTLED_ATTEMPT_STATES = {
    "accepted", "charged", "linked", "refund_pending", "manual_review",
}
ACTIVE_JOB_STATES = {
    "prepared", "queued", "running", "cancel_pending", "manual_review",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS short_drama_lipsync_assembly_plans (
  project_id TEXT PRIMARY KEY
    REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  contract_version TEXT NOT NULL,
  captured_revision INTEGER NOT NULL,
  source_input_hash TEXT NOT NULL,
  dependency_hash TEXT NOT NULL,
  plan_hash TEXT NOT NULL,
  plan_json TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
"""


class LipsyncAssemblyBlocked(ValueError):
    def __init__(self, code, message, *, blockers=None):
        super().__init__(message)
        self.code = code
        self.blockers = list(blockers or [])


def canonical_json(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def canonical_hash(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _secure_equal(left, right):
    left = str(left or "")
    right = str(right or "")
    return bool(left and right) and hmac.compare_digest(left, right)


def _default_source_inspector(file_key):
    path = media_plan.resolve_controlled_file(file_key)
    return media_plan.stable_probe(path)


def validate_source_fingerprint(source, inspected):
    """Match consumed bytes to the immutable plan evidence."""
    expected = str((source or {}).get("file_hash") or "")
    actual = str(
        ((inspected or {}).get("fingerprint") or {}).get("sha256") or ""
    )
    valid_hash = lambda value: (
        len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )
    if (
        not valid_hash(expected)
        or not valid_hash(actual)
        or not _secure_equal(actual, expected)
    ):
        raise LipsyncAssemblyBlocked(
            "lipsync_source_hash_mismatch",
            "口型素材实际文件哈希与不可变合成清单不一致",
        )
    return actual


def validate_composition_manifest(
    manifest, *, stored_manifest_hash=None, expected_kind=None,
    expected_project_id=None, expected_input_hash=None, plan=None,
    persisted_plan_hash=_PLAN_HASH_NOT_PROVIDED,
):
    """Validate a composition manifest without trusting its stored hash."""
    if isinstance(manifest, str):
        try:
            manifest = json.loads(manifest)
        except (TypeError, ValueError):
            manifest = None
    if not isinstance(manifest, dict):
        raise LipsyncAssemblyBlocked(
            "lipsync_manifest_invalid", "口型合成证据清单格式无效"
        )
    required = {
        "contract_version", "kind", "project_id", "input_hash",
        "plan_hash", "selected_sources", "manifest_hash",
    }
    if (
        not required.issubset(manifest)
        or manifest.get("contract_version") != MANIFEST_CONTRACT_VERSION
        or manifest.get("kind") not in {"preview", "final"}
        or not isinstance(manifest.get("selected_sources"), list)
    ):
        raise LipsyncAssemblyBlocked(
            "lipsync_manifest_invalid", "口型合成证据清单字段无效"
        )
    declared_hash = str(manifest.get("manifest_hash") or "")
    canonical = {
        key: value for key, value in manifest.items()
        if key != "manifest_hash"
    }
    calculated_hash = canonical_hash(canonical)
    if (
        not _secure_equal(declared_hash, calculated_hash)
        or (
            stored_manifest_hash is not None
            and not _secure_equal(stored_manifest_hash, calculated_hash)
        )
    ):
        raise LipsyncAssemblyBlocked(
            "lipsync_manifest_mismatch", "口型合成证据清单哈希不一致"
        )
    expected = {
        "kind": expected_kind,
        "project_id": expected_project_id,
        "input_hash": expected_input_hash,
    }
    if any(
        value is not None and manifest.get(key) != value
        for key, value in expected.items()
    ):
        raise LipsyncAssemblyBlocked(
            "lipsync_manifest_mismatch", "口型合成证据清单上下文不一致"
        )
    if (
        persisted_plan_hash is not _PLAN_HASH_NOT_PROVIDED
        and (
            not _secure_equal(
                persisted_plan_hash, manifest.get("plan_hash")
            )
            or (
                plan is not None
                and not _secure_equal(
                    persisted_plan_hash, plan.get("plan_hash")
                )
            )
        )
    ):
        raise LipsyncAssemblyBlocked(
            "lipsync_manifest_mismatch",
            "正式版本、合成证据清单与不可变素材清单不一致",
        )
    if plan is not None and (
        not _secure_equal(manifest.get("plan_hash"), plan.get("plan_hash"))
        or canonical_json(manifest.get("selected_sources"))
        != canonical_json(plan.get("selected_sources") or [])
    ):
        raise LipsyncAssemblyBlocked(
            "lipsync_manifest_mismatch",
            "口型合成证据清单与不可变素材清单不一致",
        )
    return calculated_hash


def enabled():
    return str(os.environ.get(
        "HQ_SHORT_DRAMA_LIPSYNC_ASSEMBLY_ENABLED", "0"
    ) or "0").strip() == "1"


def completion_gate_enabled():
    return str(os.environ.get(
        "HQ_SHORT_DRAMA_LIPSYNC_COMPLETION_GATE_ENABLED", "0"
    ) or "0").strip() == "1"


def init_db(db_factory):
    conn = db_factory()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def _decode(value, fallback):
    try:
        result = json.loads(value or "")
    except (TypeError, ValueError):
        return fallback
    return result if isinstance(result, type(fallback)) else fallback


def _blocker(code, message, shot_id=None):
    item = {"code": code, "message": message}
    if shot_id:
        item["shot_id"] = shot_id
    return item


def _dependency_identity(snapshot):
    dependencies = snapshot.get("dependencies") or {}
    return {
        "timeline": {
            key: (dependencies.get("timeline") or {}).get(key)
            for key in (
                "version_id", "timeline_version", "timeline_revision",
                "contract_version", "timeline_hash", "source_hashes",
                "visible_segments",
            )
        },
        "audio": dependencies.get("audio") or {},
        "alignment": {
            key: (dependencies.get("alignment") or {}).get(key)
            for key in (
                "version_id", "version", "status", "effective_status",
                "input_hash", "alignment_hash", "review_audit_complete",
            )
        },
        "visual_sources": dependencies.get("visual_sources") or [],
    }


def _selected_rows(conn, project_id):
    rows = conn.execute(
        "SELECT current.shot_id,current.version_id,current.revision,"
        "current.locked_at,current.locked_by,version.version,version.job_id,"
        "version.input_hash,version.provider,version.model_version,"
        "version.dependency_hashes_json,version.media_spec_json,version.file,"
        "version.file_hash,version.cost_json,job.state AS job_state,"
        "attempt.id AS attempt_id,attempt.state AS attempt_state "
        "FROM short_drama_lipsync_current current "
        "JOIN short_drama_lipsync_versions version "
        "ON version.id=current.version_id "
        "JOIN short_drama_lipsync_jobs job ON job.id=version.job_id "
        "JOIN short_drama_lipsync_attempts attempt ON attempt.id=job.attempt_id "
        "WHERE current.project_id=? ORDER BY current.shot_id",
        (project_id,),
    ).fetchall()
    return {row["shot_id"]: dict(row) for row in rows}


def _runtime_blockers(conn, project_id):
    blockers = []
    active = conn.execute(
        "SELECT id,state FROM short_drama_lipsync_jobs WHERE project_id=? "
        "AND state IN ('prepared','queued','running','cancel_pending',"
        "'manual_review') ORDER BY created_at LIMIT 1",
        (project_id,),
    ).fetchone()
    if active:
        blockers.append(_blocker(
            "active_lipsync_job", "仍有口型任务未进入稳定终态"
        ))
    unsettled = conn.execute(
        "SELECT attempt.id,attempt.state FROM short_drama_lipsync_attempts attempt "
        "JOIN short_drama_lipsync_quotes quote ON quote.id=attempt.quote_id "
        "WHERE quote.project_id=? AND attempt.state IN "
        "('accepted','charged','linked','refund_pending','manual_review') "
        "ORDER BY attempt.updated_at LIMIT 1",
        (project_id,),
    ).fetchone()
    if unsettled:
        blockers.append(_blocker(
            "lipsync_billing_unsettled", "口型任务仍有未结算扣点或退款"
        ))
    return blockers


def capture_for_handoff(conn, project, source_inspector=None):
    """Freeze locked PR-G selections before video_review advances."""
    if not enabled():
        return None
    source_inspector = source_inspector or _default_source_inspector
    snapshot = short_drama_lipsync_snapshot.build_snapshot(
        conn, project, can_write=True
    )
    visible = (
        snapshot.get("dependencies", {}).get("timeline", {})
        .get("visible_segments") or []
    )
    if not visible:
        return None
    required_shots = sorted({item["shot_id"] for item in visible})
    selected = _selected_rows(conn, project["id"])
    blockers = _runtime_blockers(conn, project["id"])
    sources = []
    for shot_id in required_shots:
        row = selected.get(shot_id)
        if not row:
            blockers.append(_blocker(
                "missing_lipsync_version", "可见对白镜头尚未选择口型版本", shot_id
            ))
            continue
        if row["locked_at"] is None:
            blockers.append(_blocker(
                "lipsync_version_unlocked", "可见对白镜头的口型版本尚未锁定", shot_id
            ))
        if row["input_hash"] != snapshot["input_hash"]:
            blockers.append(_blocker(
                "stale_lipsync_version", "口型版本依赖已变化，请重新生成", shot_id
            ))
        if row["job_state"] != "succeeded":
            blockers.append(_blocker(
                "lipsync_job_not_succeeded", "口型任务尚未成功完成", shot_id
            ))
        if row["attempt_state"] != "settled":
            blockers.append(_blocker(
                "lipsync_billing_unsettled", "口型任务账务尚未结算", shot_id
            ))
        try:
            inspected = source_inspector(row["file"])
            validate_source_fingerprint(row, inspected)
        except (
            media_plan.MediaPlanError, OSError, TypeError, ValueError,
            LipsyncAssemblyBlocked,
        ):
            blockers.append(_blocker(
                "lipsync_source_hash_mismatch",
                "口型素材实际文件与版本记录不一致",
                shot_id,
            ))
        sources.append({
            "shot_id": shot_id,
            "source_kind": "lipsync",
            "version_id": row["version_id"],
            "version": int(row["version"]),
            "job_id": row["job_id"],
            "attempt_id": row["attempt_id"],
            "provider": row["provider"],
            "model_version": row["model_version"],
            "input_hash": row["input_hash"],
            "file": row["file"],
            "file_hash": row["file_hash"],
            "dependency_hashes": _decode(row["dependency_hashes_json"], {}),
            "media_spec": _decode(row["media_spec_json"], {}),
            "cost": _decode(row["cost_json"], {}),
            "pointer_revision": int(row["revision"]),
            "locked_at": int(row["locked_at"]) if row["locked_at"] else None,
            "locked_by": row["locked_by"],
        })
    if blockers:
        raise LipsyncAssemblyBlocked(
            blockers[0]["code"], blockers[0]["message"], blockers=blockers
        )
    dependency_identity = _dependency_identity(snapshot)
    plan = {
        "contract_version": CONTRACT_VERSION,
        "project_id": project["id"],
        "captured_revision": int(project["revision"]),
        "source_input_hash": snapshot["input_hash"],
        "dependency_hash": canonical_hash(dependency_identity),
        "required_shot_ids": required_shots,
        "selected_sources": sources,
    }
    plan["plan_hash"] = canonical_hash(plan)
    now = int(time.time())
    conn.execute(
        "INSERT INTO short_drama_lipsync_assembly_plans "
        "(project_id,contract_version,captured_revision,source_input_hash,"
        "dependency_hash,plan_hash,plan_json,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(project_id) DO UPDATE SET "
        "contract_version=excluded.contract_version,"
        "captured_revision=excluded.captured_revision,"
        "source_input_hash=excluded.source_input_hash,"
        "dependency_hash=excluded.dependency_hash,plan_hash=excluded.plan_hash,"
        "plan_json=excluded.plan_json,updated_at=excluded.updated_at",
        (
            project["id"], CONTRACT_VERSION, int(project["revision"]),
            snapshot["input_hash"], plan["dependency_hash"], plan["plan_hash"],
            canonical_json(plan), now, now,
        ),
    )
    return plan


def load_plan(conn, project, *, require=False):
    row = conn.execute(
        "SELECT * FROM short_drama_lipsync_assembly_plans WHERE project_id=?",
        (project["id"],),
    ).fetchone()
    if not row:
        if require:
            raise LipsyncAssemblyBlocked(
                "lipsync_plan_missing", "项目缺少已固化的口型合成清单"
            )
        return None
    plan = _decode(row["plan_json"], {})
    if (
        not plan or plan.get("contract_version") != CONTRACT_VERSION
        or canonical_hash({
            key: value for key, value in plan.items() if key != "plan_hash"
        }) != plan.get("plan_hash")
    ):
        raise LipsyncAssemblyBlocked(
            "lipsync_plan_invalid", "口型合成清单校验失败"
        )
    current_snapshot = short_drama_lipsync_snapshot.build_snapshot(
        conn, project, can_write=False
    )
    current_dependency_hash = canonical_hash(
        _dependency_identity(current_snapshot)
    )
    if current_dependency_hash != plan.get("dependency_hash"):
        raise LipsyncAssemblyBlocked(
            "lipsync_dependency_changed",
            "口型版本依赖的时间线、主音轨或字幕对齐已变化",
        )
    blockers = _runtime_blockers(conn, project["id"])
    selected = _selected_rows(conn, project["id"])
    for source in plan.get("selected_sources") or []:
        current = selected.get(source["shot_id"])
        if (
            not current or current["version_id"] != source["version_id"]
            or current["locked_at"] is None
        ):
            blockers.append(_blocker(
                "lipsync_selection_changed",
                "已固化的口型版本选择发生变化",
                source["shot_id"],
            ))
            continue
        if current["job_state"] != "succeeded":
            blockers.append(_blocker(
                "lipsync_job_not_succeeded", "口型任务不再处于成功终态",
                source["shot_id"],
            ))
        if current["attempt_state"] != "settled":
            blockers.append(_blocker(
                "lipsync_billing_unsettled", "口型任务账务不再处于结算终态",
                source["shot_id"],
            ))
    if blockers:
        raise LipsyncAssemblyBlocked(
            blockers[0]["code"], blockers[0]["message"], blockers=blockers
        )
    return plan


def apply_to_sources(sources, plan, project_ratio):
    """Replace visible-dialogue ordinary video with immutable lipsync media."""
    if not plan:
        return sources
    by_shot = {
        item["shot_id"]: item for item in plan.get("selected_sources") or []
    }
    for shot in sources:
        selected = by_shot.get(shot["id"])
        if not selected:
            continue
        media_spec = selected.get("media_spec") or {}
        shot["video_version"] = {
            "id": selected["version_id"],
            "version": selected["version"],
            "file": selected["file"],
            "duration_ms": int(
                media_spec.get("duration_ms")
                or int(shot["duration"]) * 1000
            ),
            "ratio": media_spec.get("ratio") or project_ratio,
            "input_hash": selected["input_hash"],
            "status": "done",
        }
        shot["lipsync_source"] = selected
    return sources
