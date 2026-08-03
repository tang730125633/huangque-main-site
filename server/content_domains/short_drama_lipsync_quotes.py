"""Simulation-only lipsync quotes with durable business idempotency."""

import json
import math
import os
import time
import uuid

from providers.lipsync import get_provider
from providers.lipsync.catalog import PRICING_VERSION

from . import short_drama_lipsync_inputs
from .short_drama_lipsync_snapshot import (
    canonical_hash,
    canonical_json,
    shot_contract,
)


QUOTE_TTL_SECONDS = 300


class LipsyncQuoteError(ValueError):
    def __init__(self, code, message, *, status=400, blockers=None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.blockers = list(blockers or [])


def _validate_payload(payload):
    expected = {
        "project_id", "shot_id", "expected_revision", "expected_input_hash",
        "provider", "profile", "face_target", "idempotency_key",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        raise LipsyncQuoteError("invalid_request", "口型报价请求字段不正确")
    if (
        not isinstance(payload["project_id"], str)
        or not payload["project_id"].strip()
        or not isinstance(payload["shot_id"], str)
        or not payload["shot_id"].strip()
        or type(payload["expected_revision"]) is not int
        or not isinstance(payload["expected_input_hash"], str)
        or len(payload["expected_input_hash"]) != 64
        or not isinstance(payload["provider"], str)
        or not isinstance(payload["profile"], str)
        or not isinstance(payload["face_target"], dict)
        or not isinstance(payload["idempotency_key"], str)
        or not payload["idempotency_key"].strip()
    ):
        raise LipsyncQuoteError("invalid_request", "口型报价参数无效")
    return payload


def _row_response(row, *, replayed):
    cost = json.loads(row["cost_json"])
    media_spec = json.loads(row["media_spec_json"])
    return {
        "quote_id": row["id"],
        "business_key": row["business_key"],
        "quote_revision": int(row["quote_revision"]),
        "status": row["status"],
        "quote_mode": "paid" if int(cost.get("points") or 0) > 0 else "simulation",
        "chargeable": int(cost.get("points") or 0) > 0,
        "provider": row["provider"],
        "provider_capability_version": row["provider_capability_version"],
        "profile": row["profile"],
        "pricing_version": row["pricing_version"],
        "duration_ms": int(row["duration_ms"]),
        "actual_duration_ms": int(cost["actual_duration_ms"]),
        "billable_duration_ms": int(cost["billable_duration_ms"]),
        "media_spec": media_spec,
        "cost": cost,
        "input_hash": row["input_hash"],
        "expires_at": int(row["expires_at"]),
        "replayed": bool(replayed),
    }


def create_quote(
        conn, *, actor, owner, payload, snapshot, now=None,
        resolver=None):
    payload = _validate_payload(payload)
    now = int(time.time()) if now is None else int(now)
    if payload["expected_revision"] != snapshot["revision"]:
        raise LipsyncQuoteError(
            "revision_conflict", "项目版本已经变化，请刷新后重新报价",
            status=409,
        )
    if payload["expected_input_hash"] != snapshot["input_hash"]:
        raise LipsyncQuoteError(
            "dependency_changed", "口型依赖已经变化，请刷新后重新报价",
            status=409,
        )
    if snapshot["blockers"]:
        raise LipsyncQuoteError(
            "dependency_blocked", "口型依赖尚未就绪",
            status=422, blockers=snapshot["blockers"],
        )
    shot_segments = [
        item
        for item in snapshot["dependencies"]["timeline"]["visible_segments"]
        if item["shot_id"] == payload["shot_id"]
    ]
    if not shot_segments:
        raise LipsyncQuoteError(
            "project_or_shot_not_found", "口型报价镜头不存在", status=404
        )
    segment_targets = {
        canonical_json(item["face_target"]) for item in shot_segments
    }
    if canonical_json(payload["face_target"]) not in segment_targets:
        raise LipsyncQuoteError(
            "invalid_request", "人物脸部目标与主时间轴不一致"
        )
    contract = shot_contract(
        snapshot, payload["shot_id"], payload["face_target"]
    )
    if contract is None:
        raise LipsyncQuoteError(
            "dependency_blocked", "口型依赖尚未就绪", status=422
        )
    capability = get_provider(payload["provider"], payload["profile"])
    if capability is not None and capability.name == "musetalk":
        # MuseTalk consumes the complete video even where the drive WAV is silent.
        contract["duration_ms"] = int(contract["visual_duration_ms"])
    try:
        freeze_options = {}
        if resolver is not None:
            freeze_options["resolver"] = resolver
        contract = short_drama_lipsync_inputs.freeze_provider_contract(
            contract, **freeze_options
        )
    except short_drama_lipsync_inputs.LipsyncInputError as error:
        raise LipsyncQuoteError(
            "dependency_blocked", str(error), status=422
        ) from error
    media_spec = contract["visual"]["media_spec"]
    if capability is None or not capability.supports(
        duration_ms=contract["duration_ms"],
        width=int(media_spec["width"]),
        height=int(media_spec["height"]),
        source_format=media_spec["format"],
    ):
        raise LipsyncQuoteError(
            "provider_unsupported", "Provider 不支持当前媒体规格",
            status=422,
        )
    estimate = capability.estimate(contract["duration_ms"])
    billing_enabled = str(os.environ.get(
        "HQ_SHORT_DRAMA_LIPSYNC_BILLING_ENABLED", "0"
    )).strip() == "1"
    points_per_usd = max(1, int(os.environ.get(
        "HQ_SHORT_DRAMA_LIPSYNC_POINTS_PER_USD", "100"
    ) or 100))
    points = (
        max(1, int(math.ceil(estimate["external_estimate"] * points_per_usd)))
        if billing_enabled else 0
    )
    cost = {
        "points": points,
        "currency": "USD",
        "external_estimate": estimate["external_estimate"],
        "actual_duration_ms": estimate["actual_duration_ms"],
        "billable_duration_ms": estimate["billable_duration_ms"],
        "breakdown": [{
            "kind": "provider_execution" if billing_enabled else "provider_simulation",
            "quantity_ms": estimate["billable_duration_ms"],
            "amount": estimate["external_estimate"],
        }],
    }
    business_key = canonical_hash({
        "project_id": snapshot["project_id"],
        "shot_id": payload["shot_id"],
        "operation": "lipsync.generate",
        "input_hash": snapshot["input_hash"],
        "provider": capability.name,
        "provider_capability_version": capability.capability_version,
        "profile": capability.profile,
        "face_target": payload["face_target"],
        "media_fingerprint_hash": contract["media_fingerprint_hash"],
        "pricing_version": PRICING_VERSION,
    })
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    existing = conn.execute(
        "SELECT * FROM short_drama_lipsync_quotes "
        "WHERE actor_username=? AND business_key=? AND pricing_version=? "
        "AND status='issued' AND expires_at>? "
        "ORDER BY quote_revision DESC LIMIT 1",
        (actor, business_key, PRICING_VERSION, now),
    ).fetchone()
    if existing:
        conn.commit()
        return _row_response(existing, replayed=True)
    conn.execute(
        "UPDATE short_drama_lipsync_quotes SET status='expired' "
        "WHERE actor_username=? AND business_key=? AND pricing_version=? "
        "AND status='issued' AND expires_at<=?",
        (actor, business_key, PRICING_VERSION, now),
    )
    revision = int(conn.execute(
        "SELECT COALESCE(MAX(quote_revision),0)+1 "
        "FROM short_drama_lipsync_quotes "
        "WHERE business_key=? AND pricing_version=?",
        (business_key, PRICING_VERSION),
    ).fetchone()[0])
    quote_id = str(uuid.uuid4())
    expires_at = now + QUOTE_TTL_SECONDS
    conn.execute(
        "INSERT INTO short_drama_lipsync_quotes "
        "(id,actor_username,owner_username,project_id,shot_id,business_key,"
        "quote_revision,pricing_version,input_hash,provider,"
        "provider_capability_version,profile,face_target_json,duration_ms,"
        "provider_contract_json,media_spec_json,cost_json,status,expires_at,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            quote_id, actor, owner, snapshot["project_id"], payload["shot_id"],
            business_key, revision, PRICING_VERSION, snapshot["input_hash"],
            capability.name, capability.capability_version, capability.profile,
            canonical_json(payload["face_target"]), contract["duration_ms"],
            canonical_json(contract),
            canonical_json(media_spec), canonical_json(cost), "issued",
            expires_at, now,
        ),
    )
    row = conn.execute(
        "SELECT * FROM short_drama_lipsync_quotes WHERE id=?", (quote_id,)
    ).fetchone()
    conn.commit()
    return _row_response(row, replayed=False)
