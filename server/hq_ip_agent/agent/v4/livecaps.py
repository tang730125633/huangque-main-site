"""hq 实时能力目录：`hq capabilities --json` / `hq describe <id> --json` 的
进程内缓存与精简投影。实时结果是唯一权威，绝不凭记忆猜能力。

- capabilities()：全量能力目录（156 项）→ 紧凑目录（id/名称/一句话/扣费/可用性/类别）；
- describe(id)：完整契约 → 子 Agent 需要的精简契约（参数 schema/约束/工作流/恢复策略）。
"""
from __future__ import annotations

import json
import threading
import time

from .. import hq_cli

_TTL = 300  # 5 分钟刷新一次
_lock = threading.Lock()
_caps_cache: dict | None = None
_caps_at = 0.0
_desc_cache: dict[str, dict] = {}
_desc_at: dict[str, float] = {}


def capabilities(force: bool = False) -> list[dict]:
    """全量能力目录（原始 dict 列表）。失败返回空列表。"""
    global _caps_cache, _caps_at
    with _lock:
        now = time.time()
        if _caps_cache is not None and not force and now - _caps_at < _TTL:
            return list(_caps_cache)
    # CLI 子进程在锁外执行：锁只保护缓存 dict，两个子 Agent 并发刷新/describe
    # 不同能力时不再互堵（并发过期只会重复劳动一次，结果以最后写入为准）。
    resp = _raw_capabilities()
    items = resp.get("capabilities") or []
    with _lock:
        if items:
            _caps_cache = list(items)
            _caps_at = time.time()
        return list(items)


def _raw_capabilities() -> dict:
    import subprocess
    from .. import config
    try:
        with hq_cli.hq_semaphore():  # 与 hq_cli 共用全局并发闸
            proc = subprocess.run(
                [config.HQ_BIN, "capabilities", "--json"],
                capture_output=True, text=True, timeout=120,
            )
        raw = (proc.stdout or "").strip() or (proc.stderr or "").strip()
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


def compact_directory(query: str | None = None) -> list[dict]:
    """紧凑能力目录：id + 中文名 + 一句话 + 扣费种类 + 类别 + 可用性。"""
    out = []
    for c in capabilities():
        q = (query or "").strip().lower()
        if q:
            hay = (c.get("id", "") + " " + c.get("name", "") + " " +
                   c.get("description", "")).lower()
            if q not in hay:
                continue
        cost = c.get("cost") or {}
        out.append({
            "id": c.get("id"),
            "name": c.get("name", ""),
            "description": (c.get("description", "") or "")[:120],
            "kind": c.get("kind"),
            "availability": c.get("availability"),
            "cost_kind": cost.get("kind"),
            "confirmation_required": bool(c.get("confirmation_required")),
        })
    return out


def cost_kind(cap_id: str) -> str:
    for c in capabilities():
        if c.get("id") == cap_id:
            return (c.get("cost") or {}).get("kind", "")
    return ""


def confirmation(cap_id: str) -> str:
    """能力扣费确认方式的机器可读说明（如 "quote_token + --confirm + --expected-cost"）。"""
    for c in capabilities():
        if c.get("id") == cap_id:
            return ((c.get("cost") or {}).get("confirmation") or "")
    return ""


def describe(cap_id: str, force: bool = False) -> dict:
    """精简契约：参数 schema + 约束 + 工作流 + 恢复策略 + 费用。"""
    with _lock:
        now = time.time()
        if cap_id in _desc_cache and not force and now - _desc_at.get(cap_id, 0) < _TTL:
            return dict(_desc_cache[cap_id])
    # CLI 子进程在锁外执行：describe 最久要等 120 秒超时，锁内执行会让
    # 并发 describe 其他能力的子 Agent 全部排队干等。
    resp = hq_cli.describe(cap_id)
    d = resp.get("data") or {}
    # describe 返回体：契约包在 capability 字段内（外层是 cli_version/next_actions/schema）
    raw = d.get("capability") if isinstance(d.get("capability"), dict) else d
    if not raw:
        return {"error": "describe 无返回", "id": cap_id}
    out = {
        "id": raw.get("id") or raw.get("api_action") or cap_id,
        "name": raw.get("name", ""),
        "description": raw.get("description", ""),
        "kind": raw.get("kind"),
        "availability": raw.get("availability"),
        "confirmation_required": bool(raw.get("confirmation_required")),
        "cost": raw.get("cost") or {},
        "file_input": raw.get("file_input") or None,
        "input_schema": _compact_schema(raw.get("input_schema") or {}),
        "constraints": raw.get("constraints") or [],
        "workflow": (raw.get("agent") or {}).get("workflow") or [],
        "when_to_use": (raw.get("agent") or {}).get("when_to_use", ""),
        "recovery": (raw.get("agent") or {}).get("recovery") or [],
        "required_inputs": (raw.get("agent") or {}).get("required_inputs") or {},
        "next_actions": raw.get("next_actions") or [],
    }
    with _lock:
        _desc_cache[cap_id] = out
        _desc_at[cap_id] = time.time()
        return dict(out)


def _compact_schema(schema: dict) -> dict:
    """input_schema 精简：只保留 properties 的 type/enum/范围/描述 + required。"""
    props = {}
    for k, v in (schema.get("properties") or {}).items():
        p = {"type": v.get("type")}
        for f in ("description", "enum", "minimum", "maximum", "minLength",
                  "maxLength", "minItems", "maxItems", "default", "format"):
            if f in v:
                p[f] = v[f]
        if v.get("items"):
            p["items"] = v["items"]
        props[k] = p
    return {
        "type": schema.get("type", "object"),
        "properties": props,
        "required": schema.get("required") or [],
        "additionalProperties": schema.get("additionalProperties", False),
    }
