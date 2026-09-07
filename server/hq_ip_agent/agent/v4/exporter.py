"""把一段 IP12 会话导出为可回放、安全的 JSONL 证据。"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import subprocess
import time

from .. import config, report, state as report_state
from . import delivery, state

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
UPLOAD_DIR = os.path.join(ROOT, "data", "uploads")
_SECRET_KEY = re.compile(r"(authorization|cookie|password|secret|api[_-]?key|access[_-]?token|refresh[_-]?token|quote[_-]?token)", re.I)
_BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+\-/=]+")
_SK_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")
_SECRET_QUERY = re.compile(
    r"([?&][^=&#\s]*(?:token|signature|credential|secret|key|q-sign)[^=&#\s]*=)[^&#\s)\]]+",
    re.I,
)


def _append_unique(bucket: list, value) -> None:
    if value is None or isinstance(value, (dict, list)):
        return
    value = _safe(value)
    if value not in ("", "[REDACTED]") and value not in bucket:
        bucket.append(value)


def _trace_index(sid: str, owner: dict, release: str, lines: list[dict], artifacts: list[dict]) -> dict:
    """从已脱敏导出内容中提取内部排错入口，不复制文件、不查询外部系统。"""
    identifiers = {}
    capabilities, domains, urls, tool_names = [], [], [], []

    def collect(value):
        if isinstance(value, dict):
            for key, item in value.items():
                name = str(key).lower()
                if name.endswith("_id") and not _SECRET_KEY.search(name):
                    _append_unique(identifiers.setdefault(name, []), item)
                if name in ("capability", "capability_name"):
                    _append_unique(capabilities, item)
                if name in ("domain", "task_domain"):
                    _append_unique(domains, item)
                if name == "url" or name.endswith("_url"):
                    _append_unique(urls, item)
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    for line in lines:
        collect(line)
        if line.get("type") == "tool_call":
            _append_unique(tool_names, line.get("name"))

    return {
        "type": "trace_index",
        "purpose": "internal_troubleshooting",
        "session_id": sid,
        "conversation_url": config.HQ_SITE_BASE.rstrip("/") + f"/workbench/ip12/?sid={sid}",
        "owner": {
            "username": str(owner.get("username") or ""),
            "account_id": str(owner.get("account_id") or ""),
        },
        "release": release,
        "domains": domains,
        "capabilities": capabilities,
        "tool_names": tool_names,
        "identifiers": identifiers,
        "urls": urls,
        "subagent_states": {
            str(line.get("domain")): line.get("state")
            for line in lines if line.get("type") == "subagent_state" and line.get("domain")
        },
        "artifacts": [{
            key: item.get(key) for key in (
                "artifact_id", "source", "kind", "name", "storage_key",
                "download_url", "sha256", "exists_at_export",
            )
        } for item in artifacts],
    }


def _release() -> str:
    explicit = os.environ.get("HQ_RELEASE_SHA") or os.environ.get("GIT_COMMIT")
    if explicit:
        return explicit[:40]
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True,
            stderr=subprocess.DEVNULL, timeout=2,
        ).strip()
    except Exception:
        return "unknown"


def _safe(value):
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if _SECRET_KEY.search(str(key)) else _safe(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_safe(item) for item in value]
    if isinstance(value, str):
        text = value.replace(ROOT, "server://hq-ip-agent")
        text = _BEARER.sub("Bearer [REDACTED]", text)
        text = _SK_KEY.sub("[REDACTED_API_KEY]", text)
        return _SECRET_QUERY.sub(r"\1[REDACTED]", text)
    return value


def _hash_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact(path: str, storage_key: str, download_url: str, source: str) -> dict:
    name = os.path.basename(path)
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    kind = mime.split("/", 1)[0] if mime.startswith(("image/", "audio/", "video/")) \
        else os.path.splitext(name)[1].lstrip(".").lower() or "file"
    return {
        "type": "artifact",
        "artifact_id": "file_" + hashlib.sha256(storage_key.encode()).hexdigest()[:16],
        "source": source,
        "kind": kind,
        "name": name,
        "mime": mime,
        "size_bytes": os.path.getsize(path),
        "sha256": _hash_file(path),
        "storage_key": storage_key,
        "download_path": download_url,
        "download_url": config.HQ_SITE_BASE.rstrip("/") + "/workbench/ip12/" + download_url,
        "exists_at_export": True,
    }


def _local_artifacts(sid: str) -> list[dict]:
    found = {}

    def add(path: str, storage_key: str, download_url: str, source: str):
        real = os.path.realpath(path)
        if os.path.isfile(real):
            found.setdefault(storage_key, _artifact(real, storage_key, download_url, source))

    full = report_state.get_report_full(sid) or {}
    for section in (full, full.get("m5") or {}, full.get("m6") or {}):
        for key in ("pdf", "md", "json"):
            filename = (section.get("files") or {}).get(key)
            if not filename:
                continue
            name = os.path.basename(str(filename))
            add(
                os.path.join(report.OUTPUT_DIR, name),
                "report/" + name,
                f"api/download/{sid}/{name}",
                "report",
            )

    upload_root = os.path.realpath(os.path.join(UPLOAD_DIR, sid))
    if os.path.isdir(upload_root):
        for name in sorted(os.listdir(upload_root)):
            if name == os.path.basename(name):
                add(
                    os.path.join(upload_root, name),
                    f"upload/{sid}/{name}",
                    f"api/v4/file/{sid}/{name}",
                    "upload",
                )

    collected_root = os.path.realpath(os.path.join(delivery.COLLECT_MEDIA_DIR, sid))
    if os.path.isdir(collected_root):
        for root, _dirs, names in os.walk(collected_root):
            for name in sorted(names):
                path = os.path.join(root, name)
                rel = os.path.relpath(path, collected_root).replace(os.sep, "/")
                add(
                    path,
                    f"collected/{sid}/{rel}",
                    f"api/v4/media/{sid}/{rel}",
                    "collected",
                )
    return list(found.values())


def build_jsonl(sid: str) -> str:
    session_path = os.path.join(state.SESSION_DIR, f"v4-{sid}.json")
    snapshot_updated_at = int(os.path.getmtime(session_path) * 1000) if os.path.isfile(session_path) else None
    owner = state.get_owner(sid) or {}
    owner_ref = hashlib.sha256(str(owner.get("account_id") or "").encode()).hexdigest()[:12]
    history = state.get_main_history_with_meta(sid)
    audits = state.get_turn_audits(sid)
    timestamps = [
        pair["meta"].get("created_at") for pair in history
        if pair["meta"].get("created_at") is not None
    ] + [
        item.get("started_at") for item in audits if item.get("started_at") is not None
    ]
    artifacts = _local_artifacts(sid)
    artifact_by_url = {}
    for item in artifacts:
        artifact_by_url[item["download_url"]] = item["artifact_id"]
        artifact_by_url[item["download_path"]] = item["artifact_id"]

    release = _release()
    lines = [{
        "type": "session",
        "schema": "hq.agent-conversation/v1",
        "session_id": sid,
        "owner": {
            "username": str(owner.get("username") or ""),
            "account_id": str(owner.get("account_id") or ""),
        },
        "owner_ref": owner_ref,
        "release": release,
        "created_at": min(timestamps) if timestamps else None,
        "snapshot_updated_at": snapshot_updated_at,
        "exported_at": int(time.time() * 1000),
        "legacy_timestamps": any(pair["meta"].get("legacy") for pair in history),
    }]

    visible_seq = 0
    for index, pair in enumerate(history, 1):
        message, meta = pair["message"], pair["meta"]
        if message.get("role") == "system":
            continue
        content = str(message.get("content") or "")
        if content.startswith(("（用户刚进入对话", "（系统事件")):
            continue
        visible_seq += 1
        refs = []
        for url in message.get("images") or []:
            normalized = str(url).lstrip("/")
            ref = artifact_by_url.get(normalized) or artifact_by_url.get(str(url))
            if ref:
                refs.append(ref)
        item = {
            "type": "message",
            "seq": visible_seq,
            "source_index": index,
            "event_id": meta.get("event_id"),
            "created_at": meta.get("created_at"),
            "legacy": bool(meta.get("legacy")),
            "role": message.get("role"),
            "text": _safe(content),
            "artifact_refs": refs,
        }
        for key in ("task_job", "task_domain", "media_job"):
            if message.get(key) is not None:
                item[key] = _safe(message[key])
        lines.append(item)

    for audit in audits:
        lines.append({"type": "turn", **_safe(audit)})

    for domain in state.all_domains(sid):
        session = state.get_subagent(sid, domain) or {}
        for index, message in enumerate(session.get("messages") or [], 1):
            role = message.get("role")
            if role == "assistant" and message.get("tool_calls"):
                for call in message.get("tool_calls") or []:
                    function = call.get("function") or {}
                    try:
                        args = json.loads(function.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        args = {"unparsed": True}
                    lines.append({
                        "type": "tool_call", "domain": domain, "source_index": index,
                        "tool_call_id": call.get("id"), "name": function.get("name"),
                        "arguments": _safe(args),
                    })
            elif role == "tool":
                raw = message.get("content") or ""
                try:
                    result = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    result = raw
                lines.append({
                    "type": "tool_result", "domain": domain, "source_index": index,
                    "tool_call_id": message.get("tool_call_id"), "result": _safe(result),
                })
        pending = session.get("pending_quote") or {}
        last = session.get("last_result") or {}
        lines.append({
            "type": "subagent_state", "domain": domain,
            "state": last.get("state"), "summary": _safe(last.get("summary")),
            "question": _safe(last.get("question")),
            "quote": _safe({key: pending.get(key) for key in ("capability", "cost", "points", "expires_in")}),
            "result": _safe(last.get("result") or {}),
        })

    for artifact in artifacts:
        lines.append(artifact)
    lines.insert(1, _trace_index(sid, owner, release, lines, artifacts))
    return "".join(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n" for item in lines)
