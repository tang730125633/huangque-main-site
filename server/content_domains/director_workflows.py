"""Owner-scoped durable Director workflow and storyboard storage."""

import hashlib
import hmac
import json
import os
import re
import sqlite3
import time
import urllib.parse
import urllib.error
import urllib.request
import uuid
from contextlib import closing


WORKFLOW_RE = re.compile(r"^dw_[0-9a-f]{32}$")
REQUEST_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
SCHEMA = """
CREATE TABLE IF NOT EXISTS director_workflows(
  id TEXT PRIMARY KEY,
  username TEXT NOT NULL,
  request_id TEXT NOT NULL,
  title TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft',
  revision INTEGER NOT NULL DEFAULT 1,
  source_job_id INTEGER,
  storyboard_json TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(username, request_id)
);
CREATE INDEX IF NOT EXISTS idx_director_workflows_owner_updated
  ON director_workflows(username, updated_at DESC);
CREATE TABLE IF NOT EXISTS director_workflow_plans(
  id TEXT PRIMARY KEY,
  username TEXT NOT NULL,
  workflow_id TEXT NOT NULL REFERENCES director_workflows(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  workflow_revision INTEGER NOT NULL,
  plan_digest TEXT NOT NULL,
  action TEXT NOT NULL,
  input_json TEXT NOT NULL,
  execution_json TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  UNIQUE(username, workflow_id, kind, plan_digest)
);
CREATE TABLE IF NOT EXISTS director_workflow_runs(
  run_id TEXT PRIMARY KEY,
  username TEXT NOT NULL,
  workflow_id TEXT NOT NULL REFERENCES director_workflows(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  request_id TEXT NOT NULL,
  plan_digest TEXT NOT NULL,
  cost INTEGER NOT NULL,
  state TEXT NOT NULL,
  job_id INTEGER,
  response_json TEXT,
  error TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(username, request_id)
);
CREATE INDEX IF NOT EXISTS idx_director_workflow_runs_lookup
  ON director_workflow_runs(username, workflow_id, kind, updated_at DESC);
"""

CONTENT_BASE = os.environ.get("HQ_DIRECTOR_CONTENT_BASE", "http://127.0.0.1:8096").rstrip("/")


class WorkflowError(ValueError):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.status = status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


def init_db(db_factory):
    with closing(db_factory()) as connection:
        connection.executescript(SCHEMA)
        connection.commit()


def _text(value, field, minimum=1, maximum=160):
    if not isinstance(value, str):
        raise WorkflowError("invalid_%s" % field, "%s 必须是字符串" % field)
    value = value.strip()
    if not minimum <= len(value) <= maximum or any(ord(char) < 32 for char in value):
        raise WorkflowError("invalid_%s" % field, "%s 长度或内容不合法" % field)
    return value


def _storyboard(value):
    if not isinstance(value, list) or len(value) > 60:
        raise WorkflowError("invalid_storyboard", "storyboard 必须是最多60项的数组")
    result = []
    seen = set()
    for index, raw in enumerate(value, 1):
        if not isinstance(raw, dict) or not set(raw) <= {"id", "title", "scene", "line", "dur"}:
            raise WorkflowError("invalid_storyboard", "分镜字段不正确")
        identifier = _text(raw.get("id") or "scene_%02d" % index, "scene_id", 1, 80)
        if identifier in seen:
            raise WorkflowError("invalid_storyboard", "分镜 id 不能重复")
        seen.add(identifier)
        scene = _text(raw.get("scene") or "", "scene", 0, 2000)
        line = _text(raw.get("line") or "", "line", 0, 2000)
        if not scene and not line:
            raise WorkflowError("invalid_storyboard", "每个分镜必须包含画面或台词")
        dur = raw.get("dur", 3)
        if isinstance(dur, bool) or not isinstance(dur, (int, float)) or not 0.1 <= dur <= 180:
            raise WorkflowError("invalid_storyboard", "分镜 dur 必须为0.1至180秒")
        result.append({
            "id": identifier,
            "title": _text(raw.get("title") or "镜头%d" % index, "title", 1, 120),
            "scene": scene, "line": line, "dur": round(float(dur), 3),
        })
    return result


def _job_storyboard(connection, username, job_id):
    if isinstance(job_id, bool) or not isinstance(job_id, int) or job_id < 1:
        raise WorkflowError("invalid_source_job", "source_job_id 不合法")
    row = connection.execute(
        "SELECT kind,status,result FROM jobs WHERE id=? AND username=? AND COALESCE(deleted,0)=0",
        (job_id, username),
    ).fetchone()
    if not row or row["status"] != "done" or row["kind"] not in {"copy", "breakdown"}:
        raise WorkflowError("source_job_not_ready", "本人脚本或拆解任务尚未完成", 409)
    try:
        result = json.loads(row["result"] or "{}")
    except (TypeError, ValueError):
        raise WorkflowError("invalid_source_job", "任务结果不是有效 JSON")
    candidates = result.get("scenes") or result.get("shots")
    if not candidates and isinstance(result.get("script"), dict):
        candidates = result["script"].get("scenes") or result["script"].get("shots")
    if not isinstance(candidates, list):
        raise WorkflowError("source_storyboard_missing", "任务结果没有结构化分镜", 409)
    normalized = []
    for index, item in enumerate(candidates, 1):
        if not isinstance(item, dict):
            continue
        normalized.append({
            "id": item.get("id") or item.get("shot_id") or "scene_%02d" % index,
            "title": item.get("title") or item.get("name") or "镜头%d" % index,
            "scene": item.get("scene") or item.get("visual") or item.get("image_prompt") or "",
            "line": item.get("line") or item.get("dialogue") or item.get("text") or "",
            "dur": item.get("dur") or item.get("duration") or 3,
        })
    return _storyboard(normalized)


def _public(row):
    return {
        "workflow_id": row["id"], "title": row["title"],
        "status": row["status"], "revision": int(row["revision"]),
        "source_job_id": row["source_job_id"],
        "storyboard": json.loads(row["storyboard_json"]),
        "created_at": int(row["created_at"]), "updated_at": int(row["updated_at"]),
    }


def create(db_factory, username, body, request_id):
    if not isinstance(body, dict) or not set(body) <= {"title", "source_job_id", "storyboard"}:
        raise WorkflowError("invalid_request", "创建工作流字段不正确")
    request_id = _text(request_id, "request_id", 8, 128)
    if not REQUEST_RE.fullmatch(request_id):
        raise WorkflowError("invalid_request_id", "request_id 格式不合法")
    title = _text(body.get("title"), "title", 1, 120)
    with closing(db_factory()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        source_job_id = body.get("source_job_id")
        storyboard = (
            _storyboard(body["storyboard"])
            if "storyboard" in body
            else _job_storyboard(connection, username, source_job_id)
        )
        if not storyboard:
            raise WorkflowError("storyboard_required", "工作流至少需要一个分镜")
        previous = connection.execute(
            "SELECT * FROM director_workflows WHERE username=? AND request_id=?",
            (username, request_id),
        ).fetchone()
        if previous:
            if (
                previous["title"] != title
                or previous["source_job_id"] != source_job_id
                or json.loads(previous["storyboard_json"]) != storyboard
            ):
                raise WorkflowError(
                    "idempotency_conflict", "request_id 已绑定其他工作流输入", 409,
                )
            connection.commit()
            result = _public(previous)
            result["replayed"] = True
            return result
        now = int(time.time())
        workflow_id = "dw_" + uuid.uuid4().hex
        connection.execute(
            "INSERT INTO director_workflows"
            "(id,username,request_id,title,status,revision,source_job_id,storyboard_json,created_at,updated_at) "
            "VALUES(?,?,?,?,'draft',1,?,?,?,?)",
            (workflow_id, username, request_id, title, source_job_id,
             json.dumps(storyboard, ensure_ascii=False, separators=(",", ":")), now, now),
        )
        row = connection.execute(
            "SELECT * FROM director_workflows WHERE id=?", (workflow_id,),
        ).fetchone()
        connection.commit()
    result = _public(row)
    result["replayed"] = False
    return result


def list_workflows(db_factory, username, limit=20, offset=0):
    with closing(db_factory()) as connection:
        rows = connection.execute(
            "SELECT * FROM director_workflows WHERE username=? "
            "ORDER BY updated_at DESC,id DESC LIMIT ? OFFSET ?",
            (username, limit, offset),
        ).fetchall()
        total = connection.execute(
            "SELECT COUNT(*) FROM director_workflows WHERE username=?", (username,),
        ).fetchone()[0]
    return {"items": [_public(row) for row in rows], "total": int(total), "limit": limit, "offset": offset}


def get_workflow(db_factory, username, workflow_id):
    if not WORKFLOW_RE.fullmatch(str(workflow_id or "")):
        raise WorkflowError("invalid_workflow_id", "workflow_id 格式不合法")
    with closing(db_factory()) as connection:
        row = connection.execute(
            "SELECT * FROM director_workflows WHERE id=? AND username=?",
            (workflow_id, username),
        ).fetchone()
    if not row:
        raise WorkflowError("workflow_not_found", "编导工作流不存在", 404)
    return _public(row)


def update_storyboard(db_factory, username, workflow_id, body):
    if not isinstance(body, dict) or set(body) != {"revision", "storyboard"}:
        raise WorkflowError("invalid_request", "分镜更新字段不正确")
    revision = body.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise WorkflowError("invalid_revision", "revision 不合法")
    storyboard = _storyboard(body["storyboard"])
    if not storyboard:
        raise WorkflowError("storyboard_required", "工作流至少需要一个分镜")
    with closing(db_factory()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            "UPDATE director_workflows SET storyboard_json=?,revision=revision+1,"
            "status='draft',updated_at=? WHERE id=? AND username=? AND revision=?",
            (json.dumps(storyboard, ensure_ascii=False, separators=(",", ":")),
             int(time.time()), workflow_id, username, revision),
        )
        if cursor.rowcount != 1:
            exists = connection.execute(
                "SELECT 1 FROM director_workflows WHERE id=? AND username=?",
                (workflow_id, username),
            ).fetchone()
            raise WorkflowError(
                "revision_conflict" if exists else "workflow_not_found",
                "工作流已更新，请读取最新 revision" if exists else "编导工作流不存在",
                409 if exists else 404,
            )
        row = connection.execute(
            "SELECT * FROM director_workflows WHERE id=?", (workflow_id,),
        ).fetchone()
        connection.commit()
    return _public(row)


def export_storyboard(db_factory, username, workflow_id):
    workflow = get_workflow(db_factory, username, workflow_id)
    lines = ["# " + workflow["title"], ""]
    for index, scene in enumerate(workflow["storyboard"], 1):
        lines.extend([
            "## %d. %s" % (index, scene["title"]), "",
            "- 画面：" + (scene["scene"] or "无"),
            "- 台词：" + (scene["line"] or "无"),
            "- 时长：%s 秒" % scene["dur"], "",
        ])
    return {**workflow, "markdown": "\n".join(lines).rstrip() + "\n"}


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def create_plan(db_factory, username, workflow_id, kind, body):
    if kind not in {"production", "remake"} or not isinstance(body, dict):
        raise WorkflowError("invalid_plan", "工作流计划不合法")
    workflow = get_workflow(db_factory, username, workflow_id)
    options = body.get("options") or {}
    if not isinstance(options, dict):
        raise WorkflowError("invalid_options", "options 必须是 JSON 对象")
    from hq_cli_api import action_plan
    if kind == "production":
        # ponytail: one paid child job per frozen run; add a child-job ledger when per-scene batch output is required.
        if set(body) != {"output_kind", "options"}:
            raise WorkflowError("invalid_plan", "生产计划字段不正确")
        output_kind = body.get("output_kind")
        action = {
            "image": "director-scene-image-generate",
            "video": "director-scene-video-generate",
        }.get(output_kind)
        if not action:
            raise WorkflowError("invalid_output_kind", "output_kind 仅支持 image 或 video")
        action_input = {
            "scenes": [
                {key: scene[key] for key in ("scene", "line", "dur")}
                for scene in workflow["storyboard"]
            ],
            **options,
        }
    else:
        if set(body) != {"mode", "instruction", "options"}:
            raise WorkflowError("invalid_plan", "复刻计划字段不正确")
        mode = body.get("mode")
        instruction = _text(body.get("instruction"), "instruction", 1, 2000)
        source = "，".join(
            item["scene"] for item in workflow["storyboard"] if item.get("scene")
        )
        prompt = "%s；复刻要求：%s" % (source, instruction)
        if mode == "cinematic":
            action = "cinematic-open-generate"
            action_input = {"prompt": prompt, **options}
        elif mode in {"grok", "micro"}:
            action = "video-generate"
            action_input = {"prompt": prompt, "channel": mode, **options}
        else:
            raise WorkflowError("invalid_remake_mode", "mode 仅支持 cinematic、grok 或 micro")
    try:
        execution = action_plan(action, action_input)
    except Exception as error:
        raise WorkflowError("invalid_execution_plan", str(error)[:220]) from error
    if execution.get("kind") != "generation":
        raise WorkflowError("invalid_execution_plan", "计划没有落到付费生成动作")
    frozen = {
        "workflow_id": workflow_id, "workflow_revision": workflow["revision"],
        "kind": kind, "action": action, "input": action_input,
        "generation_kind": execution["generation_kind"],
        "endpoint": execution["endpoint"], "payload": execution["payload"],
        "submit_base": execution.get("submit_base") or CONTENT_BASE,
    }
    digest = hashlib.sha256(_canonical(frozen).encode("utf-8")).hexdigest()
    now = int(time.time())
    plan_id = "dwp_" + uuid.uuid4().hex
    with closing(db_factory()) as connection:
        connection.execute(
            "INSERT OR IGNORE INTO director_workflow_plans"
            "(id,username,workflow_id,kind,workflow_revision,plan_digest,action,input_json,execution_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (plan_id, username, workflow_id, kind, workflow["revision"], digest,
             action, _canonical(body), _canonical(frozen), now),
        )
        row = connection.execute(
            "SELECT * FROM director_workflow_plans WHERE username=? AND workflow_id=? AND kind=? AND plan_digest=?",
            (username, workflow_id, kind, digest),
        ).fetchone()
        connection.execute(
            "UPDATE director_workflows SET status='planned',updated_at=? WHERE id=? AND username=?",
            (now, workflow_id, username),
        )
        connection.commit()
    return {
        "plan_id": row["id"], "workflow_id": workflow_id,
        "workflow_revision": int(row["workflow_revision"]), "kind": kind,
        "plan_digest": digest, "action": action, "input": action_input,
    }


def _plan_row(connection, username, workflow_id, kind, plan_digest):
    row = connection.execute(
        "SELECT p.*,w.revision AS current_revision FROM director_workflow_plans p "
        "JOIN director_workflows w ON w.id=p.workflow_id AND w.username=p.username "
        "WHERE p.username=? AND p.workflow_id=? AND p.kind=? AND p.plan_digest=?",
        (username, workflow_id, kind, plan_digest),
    ).fetchone()
    if not row:
        raise WorkflowError("plan_not_found", "冻结计划不存在", 404)
    if int(row["workflow_revision"]) != int(row["current_revision"]):
        raise WorkflowError("plan_stale", "分镜已更新，请重新生成计划", 409)
    return row


def quote_plan(db_factory, username, workflow_id, kind, body, cost_of):
    if not isinstance(body, dict) or set(body) != {"plan_digest", "request_id"}:
        raise WorkflowError("invalid_quote", "报价字段不正确")
    request_id = _text(body["request_id"], "request_id", 8, 128)
    if not REQUEST_RE.fullmatch(request_id):
        raise WorkflowError("invalid_request_id", "request_id 格式不合法")
    with closing(db_factory()) as connection:
        row = _plan_row(connection, username, workflow_id, kind, body["plan_digest"])
        execution = json.loads(row["execution_json"])
    if not callable(cost_of):
        raise WorkflowError("pricing_unavailable", "生产报价暂不可用", 503)
    cost = int(cost_of(execution["generation_kind"], execution["payload"]))
    if cost < 0:
        raise WorkflowError("invalid_cost", "生产报价无效", 503)
    return {
        "workflow_id": workflow_id, "kind": kind,
        "plan_digest": row["plan_digest"], "request_id": request_id,
        "cost": cost, "confirmation_required": True,
    }


def _run_payload(row):
    result = {
        "run_id": row["run_id"], "workflow_id": row["workflow_id"],
        "kind": row["kind"], "request_id": row["request_id"],
        "plan_digest": row["plan_digest"], "cost": int(row["cost"]),
        "state": row["state"], "job_id": row["job_id"],
        "created_at": int(row["created_at"]), "updated_at": int(row["updated_at"]),
    }
    if row["response_json"]:
        result["result"] = json.loads(row["response_json"])
    if row["error"]:
        result["error"] = row["error"]
    return result


def _submit_child(execution, token, internal_token, request_id, expected_cost):
    if execution["submit_base"].rstrip("/") != CONTENT_BASE:
        raise WorkflowError("invalid_execution_target", "生产目标不是本机受控服务", 500)
    url = execution["submit_base"].rstrip("/") + execution["endpoint"]
    request = urllib.request.Request(
        url, data=_canonical(execution["payload"]).encode("utf-8"), method="POST",
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "X-HQ-Internal-Token": internal_token,
            "X-HQ-Expected-Cost": str(expected_cost),
            "Idempotency-Key": request_id,
        },
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=35) as response:
            status, raw = response.getcode(), response.read(2 * 1024 * 1024 + 1)
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read(2 * 1024 * 1024 + 1)
    except OSError as error:
        raise WorkflowError("submission_unknown", "生产提交结果未知：" + str(error)[:120], 503)
    if len(raw) > 2 * 1024 * 1024:
        raise WorkflowError("invalid_submission_response", "生产响应过大", 502)
    try:
        payload = json.loads(raw or b"{}")
    except (UnicodeDecodeError, ValueError):
        raise WorkflowError("invalid_submission_response", "生产返回无效 JSON", 502)
    if not isinstance(payload, dict):
        raise WorkflowError("invalid_submission_response", "生产返回无效 JSON", 502)
    if not 200 <= status < 300:
        raise WorkflowError(
            str(payload.get("code") or "production_rejected"),
            str(payload.get("detail") or "生产提交失败")[:220], status,
        )
    return payload


def start_run(db_factory, username, workflow_id, kind, body, expected_cost,
              token, internal_token, cost_of, retry=False):
    if not isinstance(body, dict) or set(body) != {"plan_digest", "request_id"}:
        raise WorkflowError("invalid_start", "启动字段不正确")
    request_id = _text(body["request_id"], "request_id", 8, 128)
    if not REQUEST_RE.fullmatch(request_id):
        raise WorkflowError("invalid_request_id", "request_id 格式不合法")
    quote = quote_plan(db_factory, username, workflow_id, kind, body, cost_of)
    if int(expected_cost) != int(quote["cost"]):
        raise WorkflowError("quote_cost_changed", "生产价格已变化，请重新报价", 409)
    with closing(db_factory()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        plan = _plan_row(connection, username, workflow_id, kind, body["plan_digest"])
        existing = connection.execute(
            "SELECT * FROM director_workflow_runs WHERE username=? AND request_id=?",
            (username, request_id),
        ).fetchone()
        if existing:
            if existing["workflow_id"] != workflow_id or existing["kind"] != kind or existing["plan_digest"] != body["plan_digest"]:
                raise WorkflowError("idempotency_conflict", "request_id 已绑定其他生产", 409)
            if existing["job_id"] or not retry:
                connection.commit()
                return _run_payload(existing)
            run_id = existing["run_id"]
        else:
            run_id = "dwr_" + uuid.uuid4().hex
            now = int(time.time())
            connection.execute(
                "INSERT INTO director_workflow_runs"
                "(run_id,username,workflow_id,kind,request_id,plan_digest,cost,state,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,'submitting',?,?)",
                (run_id, username, workflow_id, kind, request_id,
                 body["plan_digest"], int(expected_cost), now, now),
            )
        execution = json.loads(plan["execution_json"])
        connection.commit()
    try:
        response = _submit_child(
            execution, token, internal_token, request_id, int(expected_cost),
        )
        job_id = response.get("job_id")
        if isinstance(job_id, bool) or not isinstance(job_id, int) or job_id < 1:
            raise WorkflowError("job_id_missing", "生产已响应但缺少 job_id", 502)
        state, error = "queued", ""
    except WorkflowError as failure:
        response, job_id = None, None
        state = "uncertain" if failure.status >= 500 else "failed"
        error = str(failure)
        stored_failure = failure
    with closing(db_factory()) as connection:
        connection.execute(
            "UPDATE director_workflow_runs SET state=?,job_id=?,response_json=?,error=?,updated_at=? "
            "WHERE run_id=? AND username=?",
            (state, job_id, _canonical(response) if response else None, error,
             int(time.time()), run_id, username),
        )
        row = connection.execute(
            "SELECT * FROM director_workflow_runs WHERE run_id=?", (run_id,),
        ).fetchone()
        connection.execute(
            "UPDATE director_workflows SET status=?,updated_at=? WHERE id=? AND username=?",
            ("running" if job_id else state, int(time.time()), workflow_id, username),
        )
        connection.commit()
    if state in {"uncertain", "failed"}:
        raise stored_failure
    return _run_payload(row)


def run_status(db_factory, username, workflow_id, kind):
    with closing(db_factory()) as connection:
        row = connection.execute(
            "SELECT * FROM director_workflow_runs WHERE username=? AND workflow_id=? AND kind=? "
            "ORDER BY updated_at DESC,run_id DESC LIMIT 1",
            (username, workflow_id, kind),
        ).fetchone()
        if not row:
            raise WorkflowError("run_not_found", "生产运行不存在", 404)
        result = _run_payload(row)
        if row["job_id"]:
            job = connection.execute(
                "SELECT id,status,cost,result,error,refunded,updated_at FROM jobs WHERE id=? AND username=?",
                (row["job_id"], username),
            ).fetchone()
            if job:
                result["job"] = dict(job)
                result["state"] = {
                    "pending": "queued", "running": "running", "done": "completed",
                    "error": "failed", "failed": "failed",
                }.get(job["status"], job["status"])
                connection.execute(
                    "UPDATE director_workflows SET status=?,updated_at=? WHERE id=? AND username=?",
                    (result["state"], int(job["updated_at"] or time.time()), workflow_id, username),
                )
                connection.commit()
    return result


def recover_run(db_factory, username, workflow_id, kind, body, token,
                internal_token, cost_of):
    if not isinstance(body, dict) or set(body) != {"plan_digest", "request_id"}:
        raise WorkflowError("invalid_recover", "恢复字段不正确")
    with closing(db_factory()) as connection:
        row = connection.execute(
            "SELECT * FROM director_workflow_runs WHERE username=? AND workflow_id=? "
            "AND kind=? AND request_id=?",
            (username, workflow_id, kind, body["request_id"]),
        ).fetchone()
    if not row or row["plan_digest"] != body["plan_digest"]:
        raise WorkflowError("run_not_found", "原生产运行不存在", 404)
    if row["job_id"]:
        return run_status(db_factory, username, workflow_id, kind)
    if row["state"] not in {"submitting", "uncertain"}:
        raise WorkflowError("run_not_recoverable", "原生产运行不可恢复", 409)
    return start_run(
        db_factory, username, workflow_id, kind, body, int(row["cost"]),
        token, internal_token, cost_of, retry=True,
    )


def dispatch_http(handler, method, db_factory, verify_token, cost_of=None, internal_token=""):
    path = handler.path.split("?", 1)[0]
    if not path.startswith("/api/gen/director/workflows"):
        return False
    user = verify_token(handler._token())
    if not user:
        handler._send(401, {"detail": "未登录或登录已过期"})
        return True
    if user.get("must_change"):
        handler._send(403, {"detail": "请先修改初始密码"})
        return True
    username = user["username"]
    try:
        suffix = path[len("/api/gen/director/workflows"):].strip("/")
        parts = suffix.split("/") if suffix else []
        if method == "GET" and not parts:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(handler.path).query)
            limit = max(1, min(50, int((query.get("limit") or [20])[0])))
            offset = max(0, min(2000, int((query.get("offset") or [0])[0])))
            result = list_workflows(db_factory, username, limit, offset)
        elif method == "POST" and not parts:
            result = create(
                db_factory, username, handler._json_body_strict(),
                handler.headers.get("Idempotency-Key") or "",
            )
        elif method == "GET" and len(parts) == 1:
            result = get_workflow(db_factory, username, parts[0])
        elif method == "GET" and len(parts) == 2 and parts[1] in {"production", "remake"}:
            result = run_status(db_factory, username, parts[0], parts[1])
        elif method == "PUT" and len(parts) == 2 and parts[1] == "storyboard":
            result = update_storyboard(
                db_factory, username, parts[0], handler._json_body_strict(),
            )
        elif method == "GET" and len(parts) == 3 and parts[1:] == ["storyboard", "export"]:
            result = export_storyboard(db_factory, username, parts[0])
        elif method == "POST" and len(parts) == 3 and parts[1] in {"production", "remake"}:
            workflow_id, kind, operation = parts
            body = handler._json_body_strict()
            if operation == "plan":
                result = create_plan(db_factory, username, workflow_id, kind, body)
            elif operation == "quote":
                result = quote_plan(db_factory, username, workflow_id, kind, body, cost_of)
            elif operation in {"start", "recover"}:
                supplied = handler.headers.get("X-HQ-Internal-Token") or ""
                if not internal_token or not hmac.compare_digest(supplied, internal_token):
                    raise WorkflowError("forbidden", "forbidden", 403)
                if operation == "start":
                    try:
                        expected_cost = int(handler.headers.get("X-HQ-Expected-Cost") or "")
                    except (TypeError, ValueError):
                        raise WorkflowError("invalid_expected_cost", "expected cost is invalid")
                    result = start_run(
                        db_factory, username, workflow_id, kind, body,
                        expected_cost, handler._token(), internal_token, cost_of,
                    )
                else:
                    result = recover_run(
                        db_factory, username, workflow_id, kind, body,
                        handler._token(), internal_token, cost_of,
                    )
            else:
                handler._send(404, {"detail": "not found"})
                return True
        else:
            handler._send(404, {"detail": "not found"})
            return True
        handler._send(200, result)
    except WorkflowError as error:
        handler._send(error.status, {"detail": str(error), "code": error.code})
    except (TypeError, ValueError):
        handler._send(400, {"detail": "请求参数不合法", "code": "invalid_request"})
    except Exception:
        handler._send(500, {"detail": "编导工作流暂时不可用", "code": "workflow_unavailable"})
    return True
