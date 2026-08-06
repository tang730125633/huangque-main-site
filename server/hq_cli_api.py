"""Scoped device authorization and fixed action plans for the Huangque CLI."""

import base64
import hashlib
import hmac
import http.client
import json
import math
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


PUBLIC_ORIGIN = os.environ.get("HQ_CLI_PUBLIC_ORIGIN", "https://huangquechuanmei.com").strip().rstrip("/")
DEVICE_TTL = 10 * 60
TOKEN_TTL = 8 * 60 * 60
POLL_INTERVAL = 3
BRIDGE_TOKEN_TTL = 60
QUOTE_TTL = 5 * 60
ACTION_REQUEST_TTL = 30 * 24 * 60 * 60
ACTION_INFLIGHT_TTL = 10 * 60
CLI_CHAT_REQUESTS_PER_MINUTE = 6
CONTENT_BASE = "http://127.0.0.1:8096"
IMGGEN_BASE = "http://127.0.0.1:8101"
HERMES_BASE = "http://127.0.0.1:3102"

SCOPES = {
    "profile:read": "读取账号公开资料与点数",
    "ip12:read": "读取本人 IP12 项目与报告",
    "ip12:write": "创建本人 IP12 项目",
    "ip12:chat": "向本人 IP12 项目提交回答并调用 AI 教练",
    "prompt:optimize": "把提示词发送给黄雀 AI 优化",
    "canvas:read": "读取本人可访问的画布",
    "canvas:write": "创建本人画布",
    "canvas:agent": "把画布快照发送给 AI 生成可确认的操作方案",
    "canvas:edit": "经确认后编辑本人有编辑权限的画布",
    "assets:upload": "上传本人生成所需的临时参考图",
    "tasks:read": "读取本人任务状态与点数流水",
    "assets:read": "读取本人资产与音色",
    "assets:write": "收藏资产并管理本人资产标签",
    "generation:quote": "查询图片、视频、音频所需点数",
    "generation:submit": "经二次确认后提交生成并扣点",
    "video-compose:read": "读取本人一键成片项目",
    "video-compose:write": "经确认后创建、分析、审核或渲染本人一键成片项目",
    "digital-presenter:read": "读取本人画布中的数字人口播项目",
    "digital-presenter:write": "经确认后创建或更新本人画布中的数字人口播项目",
}
DEFAULT_SCOPES = tuple(SCOPES)
CHANNEL_CATALOG = (
    {"id": "xai", "provider": "xAI API", "category": "视频生成", "features": ["果肉视频生成"],
     "access": "direct", "capabilities": ["video-generate"], "selector": {"channel": "grok"}},
    {"id": "openai", "provider": "OpenAI API", "category": "图片 / 视频", "features": ["黄雀引擎 2", "Sora 2"],
     "access": "mixed", "capabilities": ["image-generate"], "selector": {"provider": "openai"}},
    {"id": "gemini", "provider": "Google Gemini API", "category": "图片 / 视频", "features": ["纳米香蕉", "Omni 视频"],
     "access": "mixed", "capabilities": ["video-generate"], "selector": {"channel": "omni"}},
    {"id": "seedance", "provider": "火山方舟 API", "category": "图片 / 视频", "features": ["Seedream", "Seedance 视频"],
     "access": "direct", "capabilities": ["image-generate", "video-generate"], "selector": {"provider": "seedream", "channel": "micro"}},
    {"id": "minimax", "provider": "MiniMax 中国区 API", "category": "视频生成", "features": ["麦克视频"],
     "access": "direct", "capabilities": ["video-generate"], "selector": {"channel": "minimax"}},
    {"id": "zelong", "provider": "小乐 AI API", "category": "图片生成", "features": ["黄雀引擎 2 备用线路"],
     "access": "routed", "capabilities": ["image-generate"], "selector": {"provider": "xiaole"}},
    {"id": "zelong2", "provider": "泽龙 API", "category": "图片生成", "features": ["泽龙 2 备用线路（维护中）"],
     "access": "registered", "capabilities": [], "selector": {}},
    {"id": "heygen", "provider": "HeyGen API", "category": "数字化 IP / 视频", "features": ["电影化身", "数字人口播", "数字人形象"],
     "access": "managed", "capabilities": ["digital-presenter-capability", "digital-presenter-create"], "selector": {}},
    {"id": "heygen_relay", "provider": "HeyGen 中转 API", "category": "数字化 IP / 视频", "features": ["中转与下载兜底"],
     "access": "routed", "capabilities": ["tasks", "assets"], "selector": {}},
    {"id": "xiaolevideo", "provider": "小乐视频 API", "category": "图片 / 视频", "features": ["果肉生图", "历史兼容线路"],
     "access": "routed", "capabilities": ["image-generate", "video-generate"], "selector": {}},
    {"id": "runninghub", "provider": "RunningHub API", "category": "视频处理", "features": ["换装换背景 · 线路一"],
     "access": "registered", "capabilities": [], "selector": {}},
    {"id": "wavespeed", "provider": "WaveSpeed API", "category": "视频处理", "features": ["换装换背景 · 线路二", "Seedance AI 超清"],
     "access": "registered", "capabilities": [], "selector": {}},
    {"id": "cosyvoice", "provider": "阿里百炼 API", "category": "音频生成", "features": ["公共音色", "声音克隆"],
     "access": "direct", "capabilities": ["voices", "audio-generate"], "selector": {}},
    {"id": "tikhub", "provider": "TikHub API", "category": "内容采集 / 获客", "features": ["抖音 / 小红书 / 视频号", "评论与线索"],
     "access": "navigation", "capabilities": ["collect", "leads"], "selector": {}},
    {"id": "cos", "provider": "腾讯云 COS", "category": "基础设施", "features": ["生成结果存储", "参考素材与成片存储"],
     "access": "managed", "capabilities": ["image-upload", "assets"], "selector": {}},
)
CONFIRMATION_ACTIONS = frozenset({
    "ip12-create", "ip12-message", "prompt-optimize", "canvas-create", "canvas-ops",
    "asset-favorite", "asset-tags", "video-compose-create", "video-compose-analyze",
    "video-compose-review", "video-compose-render", "digital-presenter-create",
    "digital-presenter-update",
})

_START_HITS = {}
_START_HITS_LOCK = threading.Lock()
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,160}$")
_UPLOAD_ID_RE = re.compile(r"^img_[0-9a-f]{32}$")
_CANVAS_NODE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_CANVAS_OP_NODE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_CANVAS_PROJECT_RE = re.compile(r"^(local|collab):[A-Za-z0-9_-]{1,120}$")
_CANVAS_OP_ID_RE = re.compile(r"^hqcli-[A-Za-z0-9_-]{11,122}$")
_VIDEO_COMPOSE_PROJECT_RE = re.compile(r"^compose_[0-9a-f]{32}$")
_VIDEO_COMPOSE_CANDIDATE_RE = re.compile(r"^candidate_[0-9a-f]{16}$")
_DIGITAL_PRESENTER_PROJECT_RE = re.compile(r"^dp_[0-9a-f]{32}$")
_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
_CANVAS_BASE64_RE = re.compile(r"(?<![A-Za-z0-9+/_-])[A-Za-z0-9+/_-]{512,}={0,2}(?![A-Za-z0-9+/_=-])")
IMAGE_UPLOAD_MAX_BYTES = 10 * 1024 * 1024
IMAGE_UPLOAD_SLOTS = threading.BoundedSemaphore(2)
_TASK_KINDS = {
    "", "image", "audio", "video", "xiaole_video", "copy", "collect", "leads",
    "tryon", "cinematic", "avatar", "breakdown", "script_to_video", "sora_video",
}


class CLIAPIError(Exception):
    def __init__(self, status, detail, code="invalid_request"):
        super().__init__(detail)
        self.status = int(status)
        self.detail = str(detail)
        self.code = str(code)


def init_schema(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS cli_device_grants(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_code_hash TEXT NOT NULL UNIQUE,
        user_code_hash TEXT NOT NULL UNIQUE,
        client_name TEXT NOT NULL,
        requested_scopes_json TEXT NOT NULL,
        approved_scopes_json TEXT,
        username TEXT,
        status TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        approved_at INTEGER,
        last_poll_at INTEGER NOT NULL DEFAULT 0,
        token_hash TEXT UNIQUE,
        token_expires_at INTEGER,
        revoked_at INTEGER
    )""")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_cli_grants_user ON cli_device_grants(username, token_expires_at)")
    connection.execute("""CREATE TABLE IF NOT EXISTS cli_action_requests(
        username TEXT NOT NULL,
        action TEXT NOT NULL,
        request_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        request_hash TEXT NOT NULL,
        status TEXT NOT NULL,
        http_status INTEGER,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        PRIMARY KEY(username, action, request_id)
    )""")
    connection.execute("""CREATE INDEX IF NOT EXISTS idx_cli_action_active
        ON cli_action_requests(username, action, project_id, status, updated_at)""")


def _hash(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def begin_action_request(db_factory, username, action, request_id, project_id, request_hash, now=None):
    """Claim one persistent CLI action or describe the existing claim."""
    now = int(time.time() if now is None else now)
    connection = db_factory()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM cli_action_requests WHERE updated_at<?", (now - ACTION_REQUEST_TTL,))
        connection.execute(
            "UPDATE cli_action_requests SET status='uncertain',updated_at=? "
            "WHERE status='in_progress' AND updated_at<?",
            (now, now - ACTION_INFLIGHT_TTL),
        )
        row = connection.execute(
            "SELECT request_hash,status,http_status FROM cli_action_requests "
            "WHERE username=? AND action=? AND request_id=?",
            (username, action, request_id),
        ).fetchone()
        if row:
            connection.commit()
            if row["request_hash"] != request_hash:
                return "conflict", row["http_status"]
            return row["status"], row["http_status"]
        recent = connection.execute(
            "SELECT COUNT(*) FROM cli_action_requests WHERE username=? AND action=? AND created_at>=?",
            (username, action, now - 60),
        ).fetchone()[0]
        if int(recent) >= CLI_CHAT_REQUESTS_PER_MINUTE:
            connection.commit()
            return "rate_limited", None
        active = connection.execute(
            "SELECT status FROM cli_action_requests WHERE username=? AND action=? AND project_id=? "
            "AND (status='in_progress' OR (status='uncertain' AND updated_at>=?)) "
            "ORDER BY updated_at DESC LIMIT 1",
            (username, action, project_id, now - ACTION_INFLIGHT_TTL),
        ).fetchone()
        if active:
            connection.commit()
            return ("uncertain" if active["status"] == "uncertain" else "busy"), None
        connection.execute(
            "INSERT INTO cli_action_requests(username,action,request_id,project_id,request_hash,status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,'in_progress',?,?)",
            (username, action, request_id, project_id, request_hash, now, now),
        )
        connection.commit()
        return "new", None
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def finish_action_request(db_factory, username, action, request_id, http_status=None, uncertain=False, now=None):
    now = int(time.time() if now is None else now)
    connection = db_factory()
    try:
        connection.execute(
            "UPDATE cli_action_requests SET status=?,http_status=?,updated_at=? "
            "WHERE username=? AND action=? AND request_id=? AND status='in_progress'",
            ("uncertain" if uncertain else "completed", http_status, now, username, action, request_id),
        )
        connection.commit()
    finally:
        connection.close()


def _strict_object(value, allowed, required=()):
    if not isinstance(value, dict):
        raise CLIAPIError(400, "请求体必须是 JSON 对象")
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise CLIAPIError(400, "不支持的参数：" + unknown[0])
    missing = [key for key in required if key not in value]
    if missing:
        raise CLIAPIError(400, "缺少参数：" + missing[0])
    return value


def _string(value, field, minimum=0, maximum=2000):
    if not isinstance(value, str):
        raise CLIAPIError(400, field + " 必须是字符串")
    value = value.strip()
    if len(value) < minimum or len(value) > maximum or any(ord(ch) < 32 for ch in value):
        raise CLIAPIError(400, field + " 长度或内容不合法")
    return value


def _integer(value, field, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise CLIAPIError(400, "%s 必须是 %d-%d 的整数" % (field, minimum, maximum))
    return value


def _enum(value, field, choices):
    value = _string(value, field, 1, 80)
    if value not in choices:
        raise CLIAPIError(400, field + " 仅支持：" + "、".join(choices))
    return value


def _identifier(value, field):
    value = _string(value, field, 1, 160)
    if not _ID_RE.fullmatch(value):
        raise CLIAPIError(400, field + " 格式不合法")
    return value


def _upload_id(value, field):
    value = _string(value, field, 1, 64).lower()
    if not _UPLOAD_ID_RE.fullmatch(value):
        raise CLIAPIError(400, field + " 格式不合法")
    return value


def _number(value, field, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise CLIAPIError(400, field + " 必须是有限数字")
    if not minimum <= value <= maximum:
        raise CLIAPIError(400, field + " 超出允许范围")
    return value


def _canvas_node_id(value, field="节点标识"):
    value = _string(value, field, 1, 128)
    if not _CANVAS_NODE_ID_RE.fullmatch(value):
        raise CLIAPIError(400, field + " 格式不合法")
    return value


def _canvas_op_node_id(value, field="节点标识"):
    value = _string(value, field, 1, 64)
    if not _CANVAS_OP_NODE_ID_RE.fullmatch(value):
        raise CLIAPIError(400, field + " 格式不合法")
    return value


def _canvas_agent_payload(value):
    _strict_object(value, {
        "prompt", "project_id", "snapshot_digest", "scope", "nodes", "edges",
        "selected_node_ids", "history", "page_context", "ip12_context",
    }, ("prompt", "project_id", "snapshot_digest", "scope", "nodes", "edges", "selected_node_ids"))
    project_id = _string(value["project_id"], "project_id", 1, 128)
    scope = _enum(value["scope"], "scope", ("local", "collab"))
    if not _CANVAS_PROJECT_RE.fullmatch(project_id) or not project_id.startswith(scope + ":"):
        raise CLIAPIError(400, "project_id 与 scope 不匹配")
    digest = _string(value["snapshot_digest"], "snapshot_digest", 8, 32).lower()
    if not re.fullmatch(r"[a-f0-9]{8,32}", digest):
        raise CLIAPIError(400, "snapshot_digest 格式不合法")
    raw_nodes = value["nodes"]
    if not isinstance(raw_nodes, list) or len(raw_nodes) > 60:
        raise CLIAPIError(400, "nodes 必须是最多 60 项的数组")
    nodes, node_ids, total_content = [], set(), 0
    for raw in raw_nodes:
        _strict_object(raw, {"id", "type", "title", "content", "selected"},
                       ("id", "type", "title", "content", "selected"))
        node_id = _canvas_node_id(raw["id"])
        if node_id in node_ids:
            raise CLIAPIError(400, "nodes 包含重复节点")
        if not isinstance(raw["selected"], bool):
            raise CLIAPIError(400, "selected 必须是布尔值")
        node = {
            "id": node_id,
            "type": _enum(raw["type"], "节点类型", ("text", "image", "reverse", "gen", "video", "shortDrama")),
            "title": _string(raw["title"], "节点标题", 0, 120),
            "content": _string(raw["content"], "节点内容", 0, 5000),
            "selected": raw["selected"],
        }
        total_content += len(node["title"]) + len(node["content"])
        node_ids.add(node_id)
        nodes.append(node)
    if total_content > 30000:
        raise CLIAPIError(400, "画布上下文文本超过限制")
    selected = value["selected_node_ids"]
    if not isinstance(selected, list) or len(selected) > 30:
        raise CLIAPIError(400, "selected_node_ids 必须是最多 30 项的数组")
    selected = [_canvas_node_id(item, "选中节点标识") for item in selected]
    if len(selected) != len(set(selected)) or any(item not in node_ids for item in selected):
        raise CLIAPIError(400, "selected_node_ids 引用了无效节点")
    raw_edges = value["edges"]
    if not isinstance(raw_edges, list) or len(raw_edges) > 120:
        raise CLIAPIError(400, "edges 必须是最多 120 项的数组")
    edges = []
    for raw in raw_edges:
        _strict_object(raw, {"from_node_id", "to_node_id"}, ("from_node_id", "to_node_id"))
        source = _canvas_node_id(raw["from_node_id"], "连线起点")
        target = _canvas_node_id(raw["to_node_id"], "连线终点")
        if source == target or source not in node_ids or target not in node_ids:
            raise CLIAPIError(400, "edges 引用了无效节点")
        edges.append({"from_node_id": source, "to_node_id": target})
    raw_history = value.get("history", [])
    if not isinstance(raw_history, list) or len(raw_history) > 10:
        raise CLIAPIError(400, "history 必须是最多 10 项的数组")
    history = []
    for raw in raw_history:
        _strict_object(raw, {"role", "content"}, ("role", "content"))
        role = _enum(raw["role"], "历史角色", ("user", "assistant"))
        content = _string(raw["content"], "历史消息", 0, 2000)
        if content:
            history.append({"role": role, "content": content})
    page_context = value.get("page_context")
    if page_context is not None:
        _strict_object(page_context, {"page", "path", "title", "can_edit", "selected_count"},
                       ("page", "path", "title", "can_edit", "selected_count"))
        if page_context["page"] != "canvas" or page_context["path"] not in {
                "/workbench/canvas", "/workbench/canvas.html"}:
            raise CLIAPIError(400, "page_context 不属于黄雀画布")
        if not isinstance(page_context["can_edit"], bool):
            raise CLIAPIError(400, "page_context.can_edit 必须是布尔值")
        page_context = {
            "page": "canvas", "path": page_context["path"],
            "title": _string(page_context["title"], "page_context.title", 0, 120),
            "can_edit": page_context["can_edit"],
            "selected_count": _integer(page_context["selected_count"], "page_context.selected_count", 0, 30),
        }
    ip12_context = value.get("ip12_context")
    if ip12_context is not None:
        _strict_object(ip12_context, {"project_id", "title", "status", "foundation_status", "facts"},
                       ("project_id", "title", "status", "foundation_status", "facts"))
        raw_facts = ip12_context["facts"]
        if not isinstance(raw_facts, list) or len(raw_facts) > 20:
            raise CLIAPIError(400, "ip12_context.facts 必须是最多 20 项的数组")
        facts = []
        for raw in raw_facts:
            _strict_object(raw, {"label", "value"}, ("label", "value"))
            facts.append({
                "label": _string(raw["label"], "ip12_context.facts.label", 1, 80),
                "value": _string(raw["value"], "ip12_context.facts.value", 1, 800),
            })
        ip12_context = {
            "project_id": _identifier(ip12_context["project_id"], "ip12_context.project_id"),
            "title": _string(ip12_context["title"], "ip12_context.title", 0, 120),
            "status": _enum(ip12_context["status"], "ip12_context.status", ("draft", "candidate_ready", "confirmed")),
            "foundation_status": _enum(ip12_context["foundation_status"], "ip12_context.foundation_status",
                                       ("missing", "pending_confirmation", "confirmed", "stale", "legacy")),
            "facts": facts,
        }
    payload = {
        "prompt": _string(value["prompt"], "prompt", 1, 2000), "project_id": project_id,
        "snapshot_digest": digest, "scope": scope, "nodes": nodes, "edges": edges,
        "selected_node_ids": selected, "history": history,
    }
    if page_context is not None:
        payload["page_context"] = page_context
    if ip12_context is not None:
        payload["ip12_context"] = ip12_context
    raw = json.dumps(payload, ensure_ascii=False).lower()
    if any(marker in raw for marker in ("data:image/", "data:video/", ";base64,", "blob:")) or _CANVAS_BASE64_RE.search(raw):
        raise CLIAPIError(400, "画布上下文不能包含媒体数据或 Blob 地址")
    return payload


def _canvas_params(value, require_text=False):
    _strict_object(value, {"title", "text"}, ("text",) if require_text else ())
    if not value:
        raise CLIAPIError(400, "params 不能为空")
    params = {}
    if "title" in value:
        params["title"] = _string(value["title"], "params.title", 0, 120)
    if "text" in value:
        params["text"] = _string(value["text"], "params.text", 1 if require_text else 0, 5000)
    return params


def _canvas_ops_payload(value):
    _strict_object(value, {"board_id", "base_version", "op_id", "ops"},
                   ("board_id", "base_version", "op_id", "ops"))
    op_id = _string(value["op_id"], "op_id", 17, 128)
    if not _CANVAS_OP_ID_RE.fullmatch(op_id):
        raise CLIAPIError(400, "op_id 必须以 hqcli- 开头并包含足够的随机字符")
    raw_ops = value["ops"]
    if not isinstance(raw_ops, list) or not 1 <= len(raw_ops) <= 12:
        raise CLIAPIError(400, "ops 必须包含 1-12 项")
    ops = []
    for raw in raw_ops:
        if not isinstance(raw, dict):
            raise CLIAPIError(400, "画布操作必须是对象")
        kind = raw.get("type")
        if kind == "node.create":
            _strict_object(raw, {"type", "node"}, ("type", "node"))
            node = raw["node"]
            _strict_object(node, {"id", "type", "x", "y", "params"}, ("id", "type", "x", "y", "params"))
            ops.append({"type": kind, "node": {
                "id": _canvas_op_node_id(node["id"]),
                "type": _enum(node["type"], "node.type", ("text", "gen", "video")),
                "x": _number(node["x"], "node.x", 0, 100000),
                "y": _number(node["y"], "node.y", 0, 100000),
                "params": _canvas_params(node["params"], require_text=True),
            }})
        elif kind == "node.patch":
            _strict_object(raw, {"type", "id", "fields"}, ("type", "id", "fields"))
            fields = raw["fields"]
            _strict_object(fields, {"x", "y", "params"})
            if not fields:
                raise CLIAPIError(400, "node.patch fields 不能为空")
            clean = {}
            if "x" in fields:
                clean["x"] = _number(fields["x"], "fields.x", 0, 100000)
            if "y" in fields:
                clean["y"] = _number(fields["y"], "fields.y", 0, 100000)
            if "params" in fields:
                clean["params"] = _canvas_params(fields["params"])
            ops.append({"type": kind, "id": _canvas_op_node_id(raw["id"]), "fields": clean})
        elif kind == "edge.create":
            _strict_object(raw, {"type", "edge"}, ("type", "edge"))
            edge = raw["edge"]
            _strict_object(edge, {"from", "to"}, ("from", "to"))
            endpoints = {}
            for name in ("from", "to"):
                endpoint = edge[name]
                _strict_object(endpoint, {"node", "port"}, ("node", "port"))
                endpoints[name] = {
                    "node": _canvas_op_node_id(endpoint["node"], "edge.%s.node" % name),
                    "port": _enum(endpoint["port"], "edge.%s.port" % name, ("prompt", "image")),
                }
            if endpoints["from"]["node"] == endpoints["to"]["node"]:
                raise CLIAPIError(400, "画布连线不能形成自环")
            ops.append({"type": kind, "edge": endpoints})
        else:
            raise CLIAPIError(400, "CLI 不允许该画布操作")
    return {
        "board_id": _identifier(value["board_id"], "board_id"),
        "base_version": _integer(value["base_version"], "base_version", 1, 2**63 - 1),
        "op_id": op_id, "ops": ops,
    }


def _tags(value):
    if not isinstance(value, list) or len(value) > 8:
        raise CLIAPIError(400, "tags 必须是最多 8 项的数组")
    clean = []
    for item in value:
        tag = _string(item, "tags", 1, 24)
        if tag not in clean:
            clean.append(tag)
    return clean


def _normalize_scopes(value):
    if not isinstance(value, list) or not value or len(value) > len(SCOPES):
        raise CLIAPIError(400, "requested_scopes 必须是非空权限数组")
    scopes = []
    for item in value:
        if not isinstance(item, str) or item not in SCOPES:
            raise CLIAPIError(400, "包含未知权限范围")
        if item not in scopes:
            scopes.append(item)
    return scopes


def _allow_device_start(client_key, now):
    with _START_HITS_LOCK:
        hits = [stamp for stamp in _START_HITS.get(client_key, []) if now - stamp < 600]
        _START_HITS[client_key] = hits
        if len(hits) >= 10:
            return False
        hits.append(now)
        return True


def _user_code():
    alphabet = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    raw = "".join(secrets.choice(alphabet) for _ in range(8))
    return raw[:4] + "-" + raw[4:]


def _normalize_user_code(value):
    raw = re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()
    if len(raw) != 8:
        raise CLIAPIError(400, "授权码格式不正确")
    return raw[:4] + "-" + raw[4:]


def start_device(db_factory, body, client_key, now=None):
    now = int(time.time() if now is None else now)
    _strict_object(body, {"client_name", "requested_scopes"}, ("client_name", "requested_scopes"))
    if not _allow_device_start(str(client_key or "unknown"), now):
        raise CLIAPIError(429, "授权请求过于频繁，请稍后重试", "rate_limited")
    client_name = _string(body["client_name"], "client_name", 1, 80)
    scopes = _normalize_scopes(body["requested_scopes"])
    for _ in range(16):
        device_code, user_code = secrets.token_urlsafe(32), _user_code()
        try:
            with db_factory() as connection:
                connection.execute(
                    """INSERT INTO cli_device_grants(
                       device_code_hash,user_code_hash,client_name,requested_scopes_json,status,created_at,expires_at
                       ) VALUES(?,?,?,?,?,?,?)""",
                    (_hash(device_code), _hash(user_code), client_name,
                     json.dumps(scopes, separators=(",", ":")), "pending", now, now + DEVICE_TTL),
                )
            break
        except Exception as exc:
            if "UNIQUE" not in str(exc).upper():
                raise
    else:
        raise CLIAPIError(503, "暂时无法创建授权码，请重试")
    return {
        "device_code": device_code,
        "user_code": user_code,
        "verification_uri": PUBLIC_ORIGIN + "/workbench/device?user_code=" + urllib.parse.quote(user_code),
        "expires_in": DEVICE_TTL,
        "interval": POLL_INTERVAL,
        "scopes": scopes,
        "scope_details": [{"scope": scope, "description": SCOPES[scope]} for scope in scopes],
    }


def approve_device(db_factory, username, body, now=None):
    now = int(time.time() if now is None else now)
    _strict_object(body, {"user_code", "approve"}, ("user_code", "approve"))
    if not isinstance(body["approve"], bool):
        raise CLIAPIError(400, "approve 必须是布尔值")
    code_hash = _hash(_normalize_user_code(body["user_code"]))
    with db_factory() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM cli_device_grants WHERE user_code_hash=?", (code_hash,)).fetchone()
        if not row:
            raise CLIAPIError(404, "授权码不存在", "not_found")
        if int(row["expires_at"]) <= now:
            connection.execute("UPDATE cli_device_grants SET status='expired' WHERE id=?", (row["id"],))
            raise CLIAPIError(410, "授权码已过期", "expired_token")
        if row["status"] != "pending":
            if row["status"] == "approved" and row["username"] == username:
                return {"ok": True, "status": "approved"}
            raise CLIAPIError(409, "授权码已处理", "already_processed")
        status = "approved" if body["approve"] else "denied"
        scopes = row["requested_scopes_json"] if body["approve"] else None
        connection.execute(
            """UPDATE cli_device_grants
               SET status=?,username=?,approved_scopes_json=?,approved_at=? WHERE id=?""",
            (status, username, scopes, now, row["id"]),
        )
    return {"ok": True, "status": status}


def device_info(db_factory, body, now=None):
    now = int(time.time() if now is None else now)
    _strict_object(body, {"user_code"}, ("user_code",))
    code_hash = _hash(_normalize_user_code(body["user_code"]))
    with db_factory() as connection:
        row = connection.execute(
            "SELECT client_name,requested_scopes_json,status,expires_at FROM cli_device_grants WHERE user_code_hash=?",
            (code_hash,),
        ).fetchone()
    if not row:
        raise CLIAPIError(404, "授权码不存在", "not_found")
    status = row["status"]
    if status == "pending" and int(row["expires_at"]) <= now:
        status = "expired"
    try:
        scopes = json.loads(row["requested_scopes_json"] or "[]")
    except Exception:
        raise CLIAPIError(500, "授权请求数据无效", "invalid_grant")
    return {
        "client_name": row["client_name"], "status": status, "expires_at": int(row["expires_at"]),
        "scopes": scopes,
        "scope_details": [{"scope": scope, "description": SCOPES[scope]} for scope in scopes if scope in SCOPES],
    }


def poll_device(db_factory, body, now=None):
    now = int(time.time() if now is None else now)
    _strict_object(body, {"device_code"}, ("device_code",))
    device_code = _string(body["device_code"], "device_code", 20, 200)
    with db_factory() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM cli_device_grants WHERE device_code_hash=?", (_hash(device_code),)).fetchone()
        if not row:
            raise CLIAPIError(400, "设备授权请求无效", "invalid_grant")
        status = row["status"]
        if status in {"pending", "approved"} and int(row["expires_at"]) <= now:
            connection.execute("UPDATE cli_device_grants SET status='expired' WHERE id=?", (row["id"],))
            raise CLIAPIError(410, "设备授权已过期", "expired_token")
        if status == "approved":
            token = secrets.token_urlsafe(32)
            scopes = json.loads(row["approved_scopes_json"] or "[]")
            connection.execute(
                """UPDATE cli_device_grants SET status='issued',token_hash=?,token_expires_at=?,last_poll_at=?
                   WHERE id=? AND status='approved'""",
                (_hash(token), now + TOKEN_TTL, now, row["id"]),
            )
            return {"access_token": token, "token_type": "Bearer", "expires_in": TOKEN_TTL, "scopes": scopes}
        if status == "pending":
            last_poll = int(row["last_poll_at"] or 0)
            if last_poll and now - last_poll < POLL_INTERVAL:
                raise CLIAPIError(429, "轮询过快，请按 interval 重试", "slow_down")
            connection.execute("UPDATE cli_device_grants SET last_poll_at=? WHERE id=?", (now, row["id"]),)
            raise CLIAPIError(202, "等待用户授权", "authorization_pending")
        if status == "denied":
            raise CLIAPIError(403, "用户拒绝了授权", "access_denied")
        if status == "expired":
            raise CLIAPIError(410, "设备授权已过期", "expired_token")
        raise CLIAPIError(409, "访问令牌已经签发，请重新登录", "already_issued")


def authenticate(db_factory, token, now=None):
    now = int(time.time() if now is None else now)
    token = str(token or "").strip()
    if not 20 <= len(token) <= 200:
        return None
    with db_factory() as connection:
        row = connection.execute(
            """SELECT u.*,g.approved_scopes_json AS cli_scopes,g.token_expires_at AS cli_expires_at
               FROM cli_device_grants g JOIN users u ON u.username=g.username
               WHERE g.token_hash=? AND g.status='issued' AND g.revoked_at IS NULL
                 AND g.token_expires_at>? AND COALESCE(u.account_status,'active')='active'""",
            (_hash(token), now),
        ).fetchone()
    if not row:
        return None
    try:
        scopes = tuple(json.loads(row["cli_scopes"] or "[]"))
    except Exception:
        return None
    return row, scopes


def revoke(db_factory, token, now=None):
    now = int(time.time() if now is None else now)
    token = str(token or "").strip()
    if not token:
        return False
    with db_factory() as connection:
        cursor = connection.execute(
            "UPDATE cli_device_grants SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
            (now, _hash(token)),
        )
    return cursor.rowcount > 0


def origin_allowed(origin):
    return bool(origin) and hmac.compare_digest(str(origin).strip().rstrip("/"), PUBLIC_ORIGIN)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def proxy_json(plan, web_token, internal_token=""):
    headers = {
        "Authorization": "Bearer " + web_token,
        "User-Agent": "huangque-auth-cli-gateway/1",
        "Accept": "application/json",
    }
    headers.update(plan.get("headers") or {})
    if plan.get("internal"):
        if not internal_token:
            raise CLIAPIError(503, "CLI 内部授权未配置", "not_configured")
        headers["X-HQ-Internal-Token"] = internal_token
    body = plan.get("body")
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        plan["base"] + plan["path"], data=data, headers=headers, method=plan.get("method", "GET"),
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=plan.get("timeout", 30)) as response:
            raw, status = response.read(2 * 1024 * 1024 + 1), response.getcode()
    except urllib.error.HTTPError as exc:
        raw, status = exc.read(2 * 1024 * 1024 + 1), exc.code
    except (urllib.error.URLError, OSError) as exc:
        raise CLIAPIError(502, "黄雀业务服务暂时不可用：" + str(exc)[:120], "upstream_unavailable")
    if len(raw) > 2 * 1024 * 1024:
        raise CLIAPIError(502, "黄雀业务服务响应过大", "upstream_response_too_large")
    try:
        payload = json.loads(raw or b"{}")
    except Exception:
        payload = {"detail": "黄雀业务服务返回了无效响应"}
        status = 502
    if isinstance(payload, dict) and "detail" not in payload and isinstance(payload.get("error"), str):
        payload = dict(payload, detail=payload["error"])
    return int(status), payload


def proxy_image_upload(stream, length, web_token, internal_token, content_type, digest):
    if not internal_token:
        raise CLIAPIError(503, "CLI 内部授权未配置", "not_configured")
    target = urllib.parse.urlsplit(CONTENT_BASE)
    if target.scheme != "http" or target.hostname not in {"127.0.0.1", "localhost"} or target.path not in {"", "/"}:
        raise CLIAPIError(503, "CLI 图片上传目标配置不安全", "not_configured")
    connection = http.client.HTTPConnection(target.hostname, target.port or 80, timeout=60)
    try:
        connection.putrequest("POST", "/api/gen/cli/image-upload", skip_accept_encoding=True)
        connection.putheader("Authorization", "Bearer " + web_token)
        connection.putheader("X-HQ-Internal-Token", internal_token)
        connection.putheader("X-HQ-Image-SHA256", digest)
        connection.putheader("Content-Type", content_type)
        connection.putheader("Content-Length", str(length))
        connection.putheader("Accept", "application/json")
        connection.putheader("User-Agent", "huangque-auth-cli-upload/1")
        connection.endheaders()
        remaining = length
        while remaining:
            chunk = stream.read(min(64 * 1024, remaining))
            if not chunk:
                raise CLIAPIError(400, "图片上传不完整", "invalid_image_upload")
            connection.send(chunk)
            remaining -= len(chunk)
        response = connection.getresponse()
        raw, status = response.read(2 * 1024 * 1024 + 1), response.status
    except CLIAPIError:
        raise
    except (OSError, http.client.HTTPException) as exc:
        raise CLIAPIError(502, "图片上传服务暂时不可用：" + str(exc)[:120], "upstream_unavailable")
    finally:
        connection.close()
    if len(raw) > 2 * 1024 * 1024:
        raise CLIAPIError(502, "图片上传服务响应过大", "upstream_response_too_large")
    try:
        payload = json.loads(raw or b"{}")
    except Exception:
        raise CLIAPIError(502, "图片上传服务返回了无效响应", "invalid_upstream_response")
    if not isinstance(payload, dict):
        raise CLIAPIError(502, "图片上传服务返回了无效响应", "invalid_upstream_response")
    return int(status), payload


def _plan(scope, kind, **values):
    return {"scope": scope, "kind": kind, **values}


def _matched_string(value, field, pattern, maximum=160):
    value = _string(value, field, 1, maximum)
    if not pattern.fullmatch(value):
        raise CLIAPIError(400, field + " 格式不合法")
    return value


def _video_compose_decisions(value):
    if not isinstance(value, dict) or not 1 <= len(value) <= 200:
        raise CLIAPIError(400, "decisions 必须是包含 1-200 项的对象")
    decisions = {}
    for candidate_id, decision in value.items():
        candidate_id = _matched_string(candidate_id, "候选片段 ID", _VIDEO_COMPOSE_CANDIDATE_RE)
        decisions[candidate_id] = _enum(decision, "剪辑决定", ("keep", "remove"))
    return decisions


def _digital_presenter_fields(value):
    fields = {}
    if "title" in value:
        fields["title"] = _string(value["title"], "title", 1, 80)
    if "script_text" in value:
        fields["script_text"] = _string(value["script_text"], "script_text", 0, 20000)
    if "ratio" in value:
        fields["ratio"] = _enum(value["ratio"], "ratio", ("9:16", "16:9"))
    if "resolution" in value:
        fields["resolution"] = _enum(value["resolution"], "resolution", ("1080p",))
    if "voice_key" in value:
        fields["voice_key"] = _string(value["voice_key"], "voice_key", 0, 200)
    if "target_duration" in value:
        fields["target_duration"] = _integer(value["target_duration"], "target_duration", 30, 180)
    return fields


def action_plan(action, value):
    if not isinstance(value, dict):
        raise CLIAPIError(400, "input 必须是 JSON 对象")
    if action == "account":
        _strict_object(value, set())
        return _plan("profile:read", "account")
    if action == "channels":
        _strict_object(value, set())
        return _plan("profile:read", "channels")
    if action == "ip12-projects":
        _strict_object(value, set())
        return _plan("ip12:read", "proxy", base=HERMES_BASE, path="/api/conversations")
    if action in {"ip12-project", "ip12-report"}:
        _strict_object(value, {"project_id"}, ("project_id",))
        project_id = _identifier(value["project_id"], "project_id")
        suffix = "/reports" if action == "ip12-report" else ""
        return _plan("ip12:read", "proxy", base=HERMES_BASE,
                     path="/api/conversations/" + urllib.parse.quote(project_id, safe="") + suffix)
    if action == "ip12-create":
        _strict_object(value, {"title"}, ("title",))
        title = _string(value["title"], "title", 1, 120)
        return _plan("ip12:write", "proxy", base=HERMES_BASE, path="/api/conversations",
                     method="POST", body={"title": title})
    if action == "ip12-message":
        _strict_object(value, {"project_id", "message", "request_id"}, ("project_id", "message", "request_id"))
        project_id = _identifier(value["project_id"], "project_id")
        message = _string(value["message"], "message", 1, 4000)
        request_id = _identifier(value["request_id"], "request_id")
        request_hash = _hash(json.dumps(
            {"project_id": project_id, "message": message}, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ))
        return _plan("ip12:chat", "proxy", base=HERMES_BASE, path="/api/chat-complete",
                     method="POST", body={"conversation_id": project_id, "message": message}, timeout=290,
                     headers={"Idempotency-Key": request_id}, request_id=request_id,
                     project_id=project_id, request_hash=request_hash)
    if action == "prompt-optimize":
        _strict_object(value, {"prompt", "kind"}, ("prompt", "kind"))
        prompt = _string(value["prompt"], "prompt", 1, 2000)
        kind = _enum(value["kind"], "kind", ("image", "video"))
        return _plan("prompt:optimize", "proxy", base=IMGGEN_BASE, path="/api/gen/reverse", method="POST",
                     body={"action": "optimize", "prompt": prompt, "kind": kind}, timeout=90)
    if action == "canvas-list":
        _strict_object(value, {"limit", "offset"})
        limit = _integer(value.get("limit", 20), "limit", 1, 100)
        offset = _integer(value.get("offset", 0), "offset", 0, 100000)
        return _plan("canvas:read", "canvas-list", limit=limit, offset=offset)
    if action == "canvas-get":
        _strict_object(value, {"board_id"}, ("board_id",))
        return _plan("canvas:read", "canvas-get", board_id=_identifier(value["board_id"], "board_id"))
    if action == "canvas-create":
        _strict_object(value, {"name", "prompt"}, ("name",))
        name = _string(value["name"], "name", 1, 48)
        prompt = _string(value.get("prompt", ""), "prompt", 0, 2000)
        nodes = []
        if prompt:
            nodes.append({"id": "n1", "type": "text", "x": 80, "y": 80, "collapsed": False,
                          "params": {"text": prompt}, "outputs": {"prompt": prompt}, "image": None,
                          "state": "", "note": ""})
        data = {"nid": len(nodes), "runLabel": "就绪", "zoom": 1,
                "scroll": {"left": 0, "top": 0}, "edges": [], "nodes": nodes}
        return _plan("canvas:write", "canvas-create", name=name, data=data)
    if action == "canvas-agent-plan":
        payload = _canvas_agent_payload(value)
        headers = {}
        if payload["scope"] == "collab":
            headers["X-Canvas-Board-Id"] = payload["project_id"].split(":", 1)[1]
        return _plan(
            "canvas:agent", "generation", generation_kind="canvas_agent",
            endpoint="/api/gen/canvas_agent", quote_endpoint="/api/gen/canvas-agent/quote",
            quote_body={}, payload=payload, quoted_cost_field="quoted_cost", submit_headers=headers,
        )
    if action == "canvas-ops":
        payload = _canvas_ops_payload(value)
        return _plan("canvas:edit", "canvas-ops", board_id=payload.pop("board_id"), payload=payload)
    if action == "tasks":
        _strict_object(value, {"days", "kind", "page", "page_size"})
        days = _integer(value.get("days", 30), "days", 1, 365)
        page = _integer(value.get("page", 1), "page", 1, 100000)
        page_size = _integer(value.get("page_size", 20), "page_size", 5, 50)
        kind = _string(value.get("kind", ""), "kind", 0, 32)
        if kind not in _TASK_KINDS:
            raise CLIAPIError(400, "kind 不是可查询的任务类型")
        query = urllib.parse.urlencode({"days": days, "kind": kind, "page": page, "page_size": page_size})
        return _plan("tasks:read", "proxy", base=CONTENT_BASE, path="/api/gen/points/history?" + query)
    if action == "task":
        _strict_object(value, {"job_id"}, ("job_id",))
        job_id = _integer(value["job_id"], "job_id", 1, 2**63 - 1)
        return _plan("tasks:read", "proxy", base=CONTENT_BASE, path="/api/gen/job/%d" % job_id)
    if action == "assets":
        _strict_object(value, {"kind", "limit", "offset"}, ("kind",))
        kind = _enum(value["kind"], "kind", ("image", "audio", "video", "copy", "collect", "leads", "breakdown"))
        limit = _integer(value.get("limit", 60), "limit", 1, 120)
        offset = _integer(value.get("offset", 0), "offset", 0, 100000)
        if kind == "image":
            path = "/api/gen/history?" + urllib.parse.urlencode({"kind": "image", "limit": limit, "offset": offset})
        elif kind in {"audio", "video"}:
            path = "/api/gen/%s/assets?" % kind + urllib.parse.urlencode({"limit": limit, "offset": offset})
        else:
            path = "/api/gen/assets?" + urllib.parse.urlencode({"kind": kind, "limit": limit, "offset": offset})
        return _plan("assets:read", "proxy", base=CONTENT_BASE, path=path)
    if action == "voices":
        _strict_object(value, set())
        return _plan("assets:read", "proxy", base=CONTENT_BASE, path="/api/gen/audio/voices")
    if action == "asset-favorite":
        _strict_object(value, {"kind", "key", "favorite"}, ("kind", "key", "favorite"))
        if not isinstance(value["favorite"], bool):
            raise CLIAPIError(400, "favorite 必须是布尔值")
        body = {
            "kind": _enum(value["kind"], "kind", ("image", "audio", "video", "avatar", "copy", "collect", "leads", "breakdown")),
            "key": _string(value["key"], "key", 1, 500), "favorite": value["favorite"],
        }
        return _plan("assets:write", "proxy", base=CONTENT_BASE, path="/api/gen/asset/favorite",
                     method="POST", body=body)
    if action == "asset-tags":
        _strict_object(value, {"kind", "key", "tags"}, ("kind", "key", "tags"))
        body = {
            "kind": _enum(value["kind"], "kind", ("image", "audio", "video", "avatar", "copy", "collect", "leads", "breakdown")),
            "key": _string(value["key"], "key", 1, 500), "tags": _tags(value["tags"]),
        }
        return _plan("assets:write", "proxy", base=CONTENT_BASE, path="/api/gen/asset/tags",
                     method="POST", body=body)
    if action in {"video-compose-projects", "video-compose-project"}:
        allowed = {"project_id"} if action.endswith("project") else set()
        _strict_object(value, allowed, allowed)
        path = "/api/gen/video-compose/projects"
        if allowed:
            project_id = _matched_string(value["project_id"], "project_id", _VIDEO_COMPOSE_PROJECT_RE)
            path += "/" + project_id
        return _plan("video-compose:read", "proxy", base=CONTENT_BASE, path=path)
    if action == "video-compose-create":
        _strict_object(value, {"source_asset_id"}, ("source_asset_id",))
        body = {"source_asset_id": _integer(value["source_asset_id"], "source_asset_id", 1, 2**63 - 1)}
        return _plan("video-compose:write", "proxy", base=CONTENT_BASE,
                     path="/api/gen/video-compose/projects", method="POST", body=body)
    if action in {"video-compose-analyze", "video-compose-review", "video-compose-render"}:
        allowed = {"project_id", "expected_revision"}
        if action == "video-compose-review":
            allowed.add("decisions")
        _strict_object(value, allowed, tuple(allowed))
        project_id = _matched_string(value["project_id"], "project_id", _VIDEO_COMPOSE_PROJECT_RE)
        body = {"expected_revision": _integer(value["expected_revision"], "expected_revision", 1, 2**63 - 1)}
        suffix = {"video-compose-analyze": "analyze-source", "video-compose-review": "edit-decisions",
                  "video-compose-render": "render"}[action]
        if action == "video-compose-review":
            body["decisions"] = _video_compose_decisions(value["decisions"])
        return _plan("video-compose:write", "proxy", base=CONTENT_BASE,
                     path="/api/gen/video-compose/projects/%s/%s" % (project_id, suffix),
                     method="POST", body=body, timeout=300 if action != "video-compose-review" else 30)
    if action == "digital-presenter-capability":
        _strict_object(value, set())
        return _plan("digital-presenter:read", "proxy", base=CONTENT_BASE,
                     path="/api/gen/digital-presenter/capability")
    if action == "digital-presenter-project":
        _strict_object(value, {"board_id", "project_id"}, ("board_id", "project_id"))
        board_id = _identifier(value["board_id"], "board_id")
        project_id = _matched_string(value["project_id"], "project_id", _DIGITAL_PRESENTER_PROJECT_RE)
        return _plan("digital-presenter:read", "proxy", base=CONTENT_BASE,
                     path="/api/gen/digital-presenter/project?id=" + urllib.parse.quote(project_id),
                     headers={"X-Canvas-Board-Id": board_id})
    if action in {"digital-presenter-create", "digital-presenter-update"}:
        control = {"board_id", "request_id"} if action.endswith("create") else {"board_id", "project_id", "revision"}
        editable = {"title", "script_text", "ratio", "resolution", "voice_key", "target_duration"}
        _strict_object(value, control | editable, tuple(control))
        board_id = _identifier(value["board_id"], "board_id")
        body = _digital_presenter_fields(value)
        headers = {"X-Canvas-Board-Id": board_id}
        if action == "digital-presenter-create":
            headers["Idempotency-Key"] = _matched_string(
                value["request_id"], "request_id", _IDEMPOTENCY_KEY_RE, 128)
            path, method = "/api/gen/digital-presenter/projects", "POST"
        else:
            if not body:
                raise CLIAPIError(400, "数字人口播更新至少需要一个字段")
            body.update({
                "project_id": _matched_string(value["project_id"], "project_id", _DIGITAL_PRESENTER_PROJECT_RE),
                "revision": _integer(value["revision"], "revision", 1, 2**63 - 1),
            })
            path, method = "/api/gen/digital-presenter/project", "PUT"
        return _plan("digital-presenter:write", "proxy", base=CONTENT_BASE,
                     path=path, method=method, body=body, headers=headers)
    if action in {"image-generate", "video-generate", "audio-generate"}:
        payload, generation_kind, endpoint = _generation_payload(action, value)
        return _plan("generation:quote", "generation", generation_kind=generation_kind,
                     endpoint=endpoint, payload=payload)
    raise CLIAPIError(404, "未知 CLI 能力", "unknown_action")


def _generation_payload(action, value):
    if action == "image-generate":
        _strict_object(value, {
            "prompt", "provider", "ratio", "quality", "count", "variant",
            "image_upload_id", "mask_upload_id", "reference_upload_ids",
        }, ("prompt",))
        body = {
            "prompt": _string(value["prompt"], "prompt", 1, 2000),
            "provider": _enum(value.get("provider", "openai"), "provider", ("openai", "xiaole", "seedream")),
            "ratio": _enum(value.get("ratio", "1:1"), "ratio", ("1:1", "9:16", "16:9", "3:4")),
            "quality": _enum(value.get("quality", "hd"), "quality", ("std", "hd")),
            "count": _integer(value.get("count", 1), "count", 1, 4),
        }
        if "variant" in value:
            if body["provider"] != "seedream":
                raise CLIAPIError(400, "variant 仅用于 seedream")
            body["variant"] = _enum(value["variant"], "variant", ("std", "pro"))
        if "image_upload_id" in value:
            body["image_upload_id"] = _upload_id(value["image_upload_id"], "image_upload_id")
        if "mask_upload_id" in value:
            body["mask_upload_id"] = _upload_id(value["mask_upload_id"], "mask_upload_id")
        if "reference_upload_ids" in value:
            references = value["reference_upload_ids"]
            limit = {"openai": 16, "seedream": 10, "xiaole": 4}[body["provider"]]
            if not isinstance(references, list) or not 1 <= len(references) <= limit:
                raise CLIAPIError(400, "reference_upload_ids 必须包含 1-%d 项" % limit)
            body["reference_upload_ids"] = []
            for item in references:
                clean = _upload_id(item, "reference_upload_ids")
                if clean not in body["reference_upload_ids"]:
                    body["reference_upload_ids"].append(clean)
        if body.get("image_upload_id") and body.get("reference_upload_ids"):
            raise CLIAPIError(400, "单参考图和多参考图不能同时使用")
        if body.get("mask_upload_id") and not body.get("image_upload_id"):
            raise CLIAPIError(400, "蒙版必须同时提供 image_upload_id")
        if body.get("mask_upload_id") and body["provider"] != "openai":
            raise CLIAPIError(400, "蒙版局部修改仅支持 openai")
        if body.get("mask_upload_id") and body["count"] != 1:
            raise CLIAPIError(400, "蒙版局部修改 count 必须为 1")
        return body, "image", "/api/gen/image"
    if action == "video-generate":
        _strict_object(value, {"prompt", "channel", "ratio", "duration", "resolution", "model", "generate_audio", "reference_upload_ids"}, ("prompt",))
        channel = _enum(value.get("channel", "grok"), "channel", ("grok", "micro", "omni", "minimax"))
        body = {
            "prompt": _string(value["prompt"], "prompt", 1, 2000),
            "channel": channel,
            "ratio": _enum(value.get("ratio", "16:9" if channel in {"grok", "omni"} else "9:16"),
                           "ratio", ("1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3")),
            "duration": _integer(value.get("duration", 10 if channel == "grok" else 5), "duration", 1, 15),
            "resolution": _enum(value.get("resolution", "768p" if channel == "minimax" else "720p"),
                                "resolution", ("480p", "720p", "768p", "1080p")),
        }
        if "model" in value:
            body["model"] = _enum(value["model"], "model", ("grok-imagine-video", "grok-imagine-video-1.5"))
            if channel != "grok":
                raise CLIAPIError(400, "model 参数仅用于 grok")
        if "generate_audio" in value:
            if not isinstance(value["generate_audio"], bool) or channel != "micro":
                raise CLIAPIError(400, "generate_audio 仅用于 micro 且必须是布尔值")
            body["generate_audio"] = value["generate_audio"]
        if "reference_upload_ids" in value:
            references = value["reference_upload_ids"]
            limit = {"grok": 7, "micro": 9, "omni": 6, "minimax": 5}[channel]
            if not isinstance(references, list) or not 1 <= len(references) <= limit:
                raise CLIAPIError(400, "reference_upload_ids 必须包含 1-%d 项" % limit)
            body["reference_upload_ids"] = []
            for item in references:
                clean = _upload_id(item, "reference_upload_ids")
                if clean not in body["reference_upload_ids"]:
                    body["reference_upload_ids"].append(clean)
        return body, "xiaole_video", "/api/gen/xiaole_video"
    _strict_object(value, {"text", "voice", "speed", "pitch", "volume"}, ("text",))
    body = {"text": _string(value["text"], "text", 1, 1000)}
    if "voice" in value:
        body["voice"] = _string(value["voice"], "voice", 1, 128)
    for field, default, minimum, maximum in (("pitch", 0, -12, 12), ("volume", 0, -50, 100)):
        body[field] = _integer(value.get(field, default), field, minimum, maximum)
    speed = value.get("speed", 1.0)
    if isinstance(speed, bool) or not isinstance(speed, (int, float)) or not 0.5 <= float(speed) <= 2:
        raise CLIAPIError(400, "speed 必须是 0.5-2 的数字")
    body["speed"] = round(float(speed), 1)
    return body, "audio", "/api/gen/audio"


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def issue_quote(secret, username, generation_kind, payload, cost, now=None):
    if not secret:
        raise CLIAPIError(503, "CLI 报价签名未配置", "not_configured")
    now = int(time.time() if now is None else now)
    cost = int(cost)
    if cost <= 0:
        raise CLIAPIError(502, "生成费用无效", "invalid_quote")
    claims = {
        "v": 1, "u": username, "k": generation_kind,
        "h": hashlib.sha256(_canonical(payload)).hexdigest(),
        "c": cost, "e": now + QUOTE_TTL, "n": secrets.token_hex(16),
    }
    encoded = base64.urlsafe_b64encode(_canonical(claims)).decode("ascii").rstrip("=")
    signature = hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    return encoded + "." + signature, claims


def verify_quote(secret, token, username, generation_kind, payload, now=None):
    if not secret:
        raise CLIAPIError(503, "CLI 报价签名未配置", "not_configured")
    now = int(time.time() if now is None else now)
    try:
        encoded, signature = str(token or "").split(".", 1)
        expected = hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("signature")
        claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    except Exception:
        raise CLIAPIError(400, "报价凭证无效，请重新报价", "invalid_quote")
    payload_hash = hashlib.sha256(_canonical(payload)).hexdigest()
    if (claims.get("v") != 1 or claims.get("u") != username or claims.get("k") != generation_kind
            or claims.get("h") != payload_hash or not isinstance(claims.get("c"), int)
            or not isinstance(claims.get("e"), int) or not isinstance(claims.get("n"), str)):
        raise CLIAPIError(409, "报价与当前账号或参数不匹配，请重新报价", "quote_mismatch")
    if claims["e"] <= now:
        raise CLIAPIError(409, "报价已过期，请重新报价", "quote_expired")
    return claims
