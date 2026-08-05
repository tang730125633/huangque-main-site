#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""黄雀公开错误契约：稳定业务码、安全文案、请求号与重试语义。"""

import json
import hashlib
import re
import uuid


CATALOG = {
    "HQ-REQUEST-001": {"type": "invalid_request", "status": 400, "message": "请求参数不正确，请检查后重试", "retryable": False},
    "HQ-AUTH-001": {"type": "unauthorized", "status": 401, "message": "登录或授权已过期，请重新登录", "retryable": False},
    "HQ-BILLING-001": {"type": "payment_required", "status": 402, "message": "当前账号余额或额度不足", "retryable": False},
    "HQ-AUTH-002": {"type": "forbidden", "status": 403, "message": "当前账号没有执行此操作的权限", "retryable": False},
    "HQ-RESOURCE-001": {"type": "not_found", "status": 404, "message": "请求的内容不存在或已失效", "retryable": False},
    "HQ-CONFLICT-001": {"type": "conflict", "status": 409, "message": "内容已发生变化，请刷新后重试", "retryable": False},
    "HQ-ASSET-001": {"type": "payload_too_large", "status": 413, "message": "提交的文件或数据超过大小限制", "retryable": False},
    "HQ-RATE-001": {"type": "rate_limited", "status": 429, "message": "请求过于频繁，请稍后再试", "retryable": True},
    "HQ-SYSTEM-001": {"type": "internal_error", "status": 500, "message": "黄雀服务暂时异常，请稍后再试", "retryable": True},
    "HQ-UPSTREAM-001": {"type": "upstream_error", "status": 502, "message": "生成渠道暂时不可用，请稍后再试", "retryable": True},
    "HQ-UPSTREAM-002": {"type": "upstream_unavailable", "status": 503, "message": "生成渠道正在繁忙或维护，请稍后再试", "retryable": True},
    "HQ-UPSTREAM-003": {"type": "upstream_timeout", "status": 504, "message": "生成渠道响应超时，请稍后查询任务状态", "retryable": True},
}

STATUS_CODES = {item["status"]: code for code, item in CATALOG.items()}
LEGACY_CODES = {
    "cli_unauthorized": "HQ-AUTH-001",
    "insufficient_scope": "HQ-AUTH-002",
    "forbidden": "HQ-AUTH-002",
    "not_found": "HQ-RESOURCE-001",
    "confirmation_required": "HQ-CONFLICT-001",
    "idempotency_conflict": "HQ-CONFLICT-001",
    "revision_conflict": "HQ-CONFLICT-001",
    "rate_limited": "HQ-RATE-001",
    "upstream_unavailable": "HQ-UPSTREAM-002",
    "upstream_response_too_large": "HQ-UPSTREAM-001",
    "invalid_upstream_response": "HQ-UPSTREAM-001",
    "content_security_unavailable": "HQ-UPSTREAM-002",
}
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SECRET_RE = re.compile(
    r"(?i)(\"?(?:api[_-]?key|token|secret|password|authorization|credential)\"?\s*[:=]\s*\"?)([^\s,;\"']+)"
)
_BEARER_RE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]+")


def request_id(headers=None):
    headers = headers or {}
    value = str(headers.get("X-Request-ID") or "").strip()
    if _REQUEST_ID_RE.fullmatch(value):
        return value
    idempotency_key = str(headers.get("Idempotency-Key") or "").strip()
    if idempotency_key:
        return "hq_i_" + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:24]
    return "hq_" + uuid.uuid4().hex


def _redact(value, limit=600):
    text = str(value or "").replace("\x00", " ").strip()
    return _SECRET_RE.sub(r"\1***", _BEARER_RE.sub(r"\1***", text))[:limit]


def code_for(status, legacy_code=""):
    legacy_code = str(legacy_code or "").strip()
    if legacy_code in CATALOG:
        return legacy_code
    if legacy_code in LEGACY_CODES:
        return LEGACY_CODES[legacy_code]
    status = int(status or 500)
    return STATUS_CODES.get(status, "HQ-SYSTEM-001" if status >= 500 else "HQ-REQUEST-001")


def normalize(status, payload, req_id=""):
    """兼容旧 detail/code，同时增加稳定的 error 对象与黄雀码。"""
    status = int(status or 500)
    if status < 400:
        return payload, ""
    data = dict(payload) if isinstance(payload, dict) else {"detail": str(payload or "")}
    legacy_code = str(data.get("code") or "").strip()
    hq_code = code_for(status, data.get("hq_code") or legacy_code)
    spec = CATALOG[hq_code]
    detail = _redact(data.get("detail") or data.get("message"))
    message = spec["message"] if status >= 500 or not detail else detail
    req_id = req_id or request_id()
    data.update({
        "detail": message,
        "code": legacy_code or spec["type"],
        "hq_code": hq_code,
        "request_id": req_id,
        "retryable": bool(spec["retryable"]),
        "error": {
            "code": hq_code,
            "type": legacy_code or spec["type"],
            "message": message,
            "retryable": bool(spec["retryable"]),
            "request_id": req_id,
        },
    })
    return data, hq_code


def audit(status, payload, req_id, hq_code):
    """只写脱敏摘要到服务日志，原始上游正文不进入公开响应。"""
    if not hq_code:
        return
    detail = payload.get("detail") if isinstance(payload, dict) else payload
    print("[hq-error] " + json.dumps({
        "http_status": int(status), "hq_code": hq_code,
        "request_id": req_id, "internal_detail": _redact(detail),
    }, ensure_ascii=False, separators=(",", ":")), flush=True)


def public_catalog():
    return [{"code": code, **item} for code, item in sorted(CATALOG.items())]
