# -*- coding: utf-8 -*-
"""HTTP contract for non-destructive one-click video analysis and review."""

import hashlib
import json
import pathlib
import re
import threading
from contextlib import closing

from . import video_compose_analysis as analysis
from . import video_compose_asr as asr
from . import video_compose_media as media
from . import video_compose_render as renderer
from . import video_compose_store as store


BASE_PATH = "/api/gen/video-compose/projects"
PROJECT_RE = re.compile(r"^/api/gen/video-compose/projects/(compose_[0-9a-f]{32})$")
ANALYSIS_RE = re.compile(r"^/api/gen/video-compose/projects/(compose_[0-9a-f]{32})/analysis$")
SOURCE_ANALYSIS_RE = re.compile(r"^/api/gen/video-compose/projects/(compose_[0-9a-f]{32})/analyze-source$")
DECISIONS_RE = re.compile(r"^/api/gen/video-compose/projects/(compose_[0-9a-f]{32})/edit-decisions$")
RENDER_RE = re.compile(r"^/api/gen/video-compose/projects/(compose_[0-9a-f]{32})/render$")
OUTPUT_RE = re.compile(r"^/api/gen/video-compose/projects/(compose_[0-9a-f]{32})/output$")
_RENDER_LOCK = threading.BoundedSemaphore(1)


def _body(handler):
    value = handler._json_body_strict()
    if not isinstance(value, dict):
        raise ValueError("请求体必须是 JSON 对象")
    return value


def _only(body, allowed):
    extra = set(body) - set(allowed)
    if extra:
        raise ValueError("请求包含未支持字段")


def _revision(value):
    if isinstance(value, bool):
        raise ValueError("项目版本格式无效")
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise ValueError("项目版本格式无效")
    if value < 1:
        raise ValueError("项目版本格式无效")
    return value


def _auth(handler, verify_token, must_change_password):
    user = verify_token(handler._token())
    if not user:
        handler._send(401, {"detail": "未登录或登录已过期"})
        return None
    if must_change_password(user):
        handler._send(403, {"detail": "请先修改初始密码"})
        return None
    return user


def _source_asset(asset_db_factory, username, source_asset_id):
    if isinstance(source_asset_id, bool):
        raise ValueError("视频资产 ID 格式无效")
    try:
        source_asset_id = int(source_asset_id)
    except (TypeError, ValueError):
        raise ValueError("视频资产 ID 格式无效")
    if source_asset_id < 1:
        raise ValueError("视频资产 ID 格式无效")
    with closing(asset_db_factory()) as connection:
        row = connection.execute(
            """SELECT id,job_id,mode,video_file,video_url,text,resolution,ratio,model,status,updated_at
               FROM video_assets WHERE id=? AND username=? AND status!='deleted'""",
            (source_asset_id, str(username)),
        ).fetchone()
    if not row:
        raise LookupError("视频资产不存在")
    item = dict(row)
    if item.get("status") not in {"done", "completed"}:
        raise ValueError("视频尚未生成完成")
    if not item.get("video_file") and not item.get("video_url"):
        raise ValueError("视频资产没有可用成片")
    snapshot = {
        "asset_id": item["id"],
        "job_id": item.get("job_id"),
        "mode": item.get("mode"),
        "video_file": item.get("video_file"),
        "text": str(item.get("text") or "")[:8000],
        "resolution": item.get("resolution"),
        "ratio": item.get("ratio"),
        "model": item.get("model"),
        "asset_updated_at": item.get("updated_at"),
    }
    revision_raw = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    source_revision = hashlib.sha256(revision_raw.encode("utf-8")).hexdigest()
    return source_asset_id, source_revision, snapshot


def _create_project(handler, user, asset_db_factory):
    body = _body(handler)
    _only(body, {"source_asset_id"})
    source_asset_id, source_revision, snapshot = _source_asset(
        asset_db_factory, user["username"], body.get("source_asset_id")
    )
    project = store.create_project(
        user["username"], source_asset_id, source_revision, snapshot
    )
    return handler._send(201, {"project": project})


def _save_analysis(handler, user, project_id):
    body = _body(handler)
    _only(body, {"expected_revision", "duration_ms", "words"})
    normalized = analysis.detect_candidates(body.get("duration_ms"), body.get("words"))
    project = store.save_analysis(
        user["username"], project_id, _revision(body.get("expected_revision")),
        normalized, analysis.transcript_hash(normalized["duration_ms"], normalized["words"]),
    )
    return handler._send(200, {"project": project})


def _analyze_source(handler, user, project_id, source_resolver):
    body = _body(handler)
    _only(body, {"expected_revision"})
    current = store.get_project(user["username"], project_id)
    source_file = str((current.get("source") or {}).get("video_file") or "").strip()
    source_path = source_resolver(source_file) if source_resolver and source_file else None
    if not source_path:
        raise ValueError("当前视频资产没有可分析的本地源文件")
    transcript = asr.transcribe(source_path)
    source_media = media.probe_media(source_path)
    duration_ms = source_media["duration_ms"]
    detected = analysis.detect_candidates(duration_ms, transcript["words"])
    silences = media.detect_silence_ranges(source_path)
    detected["candidates"] = analysis.enrich_candidates(
        duration_ms, detected["words"], detected["candidates"], silences,
    )
    project = store.save_analysis(
        user["username"], project_id, _revision(body.get("expected_revision")),
        detected, analysis.transcript_hash(duration_ms, detected["words"]),
    )
    return handler._send(200, {"project": project, "transcript_text": transcript.get("text") or ""})


def _confirm_decisions(handler, user, project_id):
    body = _body(handler)
    _only(body, {"expected_revision", "decisions"})
    current = store.get_project(user["username"], project_id)
    decisions = analysis.normalize_decisions(current["candidates"], body.get("decisions"))
    edl = analysis.build_edl(current["duration_ms"], current["candidates"], decisions)
    project = store.confirm_edit_decisions(
        user["username"], project_id, _revision(body.get("expected_revision")), decisions, edl
    )
    return handler._send(200, {"project": project})


def _join_words(parts):
    text = ""
    for value in parts:
        value = str(value or "").strip()
        if not value:
            continue
        if text and re.search(r"[A-Za-z0-9]$", text) and re.match(r"^[A-Za-z0-9]", value):
            text += " "
        text += value
    return text


def _caption_cues(words, edl):
    remapped = []
    for word in words or []:
        start_ms = media.source_to_output_ms(word.get("start_ms"), edl)
        end_ms = media.source_to_output_ms(word.get("end_ms"), edl)
        text = str(word.get("text") or "").strip()
        if start_ms is not None and end_ms is not None and end_ms > start_ms and text:
            remapped.append({"text": text, "start_ms": start_ms, "end_ms": end_ms})
    cues = []
    group = []
    for item in remapped:
        gap = item["start_ms"] - (group[-1]["end_ms"] if group else item["start_ms"])
        current_text = _join_words([part["text"] for part in group])
        if group and (gap > 360 or len(current_text) >= 12 or re.search(r"[。！？!?]$", current_text)):
            cues.append({"text": current_text, "start_ms": group[0]["start_ms"],
                         "end_ms": group[-1]["end_ms"], "keywords": []})
            group = []
        group.append(item)
    if group:
        cues.append({"text": _join_words([part["text"] for part in group]),
                     "start_ms": group[0]["start_ms"], "end_ms": group[-1]["end_ms"],
                     "keywords": []})
    if not cues:
        raise ValueError("粗剪后没有可用字幕")
    return cues


def _default_render_input(project, body):
    cues = _caption_cues(project["words"], project["edl"])
    keywords = ("AI", "人工智能", "自己", "全款", "成交", "增长", "品牌", "流量")
    for cue in cues:
        cue["keywords"] = [value for value in keywords if value in cue["text"]][:2]
    cut_points = []
    cursor = 0
    keep_ranges = project["edl"].get("keep_ranges") or []
    for item in keep_ranges[:-1]:
        cursor += int(item["source_end_ms"]) - int(item["source_start_ms"])
        cut_points.append(cursor)
    hook = body.get("hook") if isinstance(body.get("hook"), dict) else {}
    headlines = body.get("headlines") if isinstance(body.get("headlines"), list) else []
    brand = body.get("brand") if isinstance(body.get("brand"), dict) else {}
    return {
        "template_id": renderer.TEMPLATE_ID,
        "template_version": renderer.TEMPLATE_VERSION,
        "duration_ms": int(project["edl"]["output_duration_ms"]),
        "cues": cues, "hook": hook, "headlines": headlines, "brand": brand,
        "cut_points_ms": cut_points,
    }


def _record_output_asset(asset_db_factory, username, output_rel, render_input):
    import time
    now = int(time.time())
    title = "%s / %s" % (
        str((render_input.get("hook") or {}).get("line_1") or "一键成片"),
        str((render_input.get("hook") or {}).get("line_2") or renderer.TEMPLATE_ID),
    )
    with closing(asset_db_factory()) as connection:
        cursor = connection.execute(
            """INSERT INTO video_assets
               (job_id,username,mode,video_file,video_url,text,resolution,ratio,motion,
                phase,model,status,error,created_at,updated_at)
               VALUES(NULL,?,'video_compose',?,NULL,?,'1080p','9:16','template',
                      'completed',?,'done',NULL,?,?)""",
            (str(username), str(output_rel), title[:8000], renderer.TEMPLATE_ID, now, now),
        )
        connection.commit()
        return int(cursor.lastrowid)


def _render_project(handler, user, project_id, asset_db_factory, source_resolver, out_dir):
    body = _body(handler)
    _only(body, {"expected_revision", "hook", "headlines", "brand"})
    expected_revision = _revision(body.get("expected_revision"))
    with _RENDER_LOCK:
        current = store.get_project(user["username"], project_id)
        if current["status"] == "completed" and current.get("output_file"):
            return handler._send(200, {
                "project": current,
                "output_url": BASE_PATH + "/" + project_id + "/output",
            })
        if current["revision"] != expected_revision:
            raise store.RevisionConflict("项目已更新，请刷新后重试")
        if current["status"] != "review_confirmed" or not current.get("edl"):
            raise ValueError("请先确认粗剪方案")
        source_file = str((current.get("source") or {}).get("video_file") or "").strip()
        source_path = source_resolver(source_file) if source_resolver and source_file else None
        if not source_path:
            raise ValueError("当前视频资产没有可渲染的本地源文件")
        owner_hash = hashlib.sha256(user["username"].encode()).hexdigest()[:16]
        folder = pathlib.Path(out_dir) / "video-compose" / owner_hash
        folder.mkdir(parents=True, exist_ok=True)
        clean_path = folder / (project_id + "-clean.mp4")
        output_path = folder / (project_id + "-viral-v1.mp4")
        media.build_clean_master(source_path, current["edl"], clean_path)
        render_input = _default_render_input(current, body)
        rendered = renderer.render(clean_path, render_input, output_path)
        quality = {"template_id": rendered["template_id"],
                   "template_version": rendered["template_version"],
                   "output": rendered["output"], "render_log": "ok"}
        base = pathlib.Path(out_dir).resolve()
        clean_rel = clean_path.resolve().relative_to(base).as_posix()
        output_rel = output_path.resolve().relative_to(base).as_posix()
        output_asset_id = _record_output_asset(
            asset_db_factory, user["username"], output_rel, render_input)
        try:
            project = store.save_render_result(
                user["username"], project_id, expected_revision, render_input,
                clean_rel, output_rel, output_asset_id, quality,
            )
        except Exception:
            try:
                with closing(asset_db_factory()) as connection:
                    connection.execute("DELETE FROM video_assets WHERE id=? AND username=?",
                                       (output_asset_id, user["username"]))
                    connection.commit()
            except Exception:
                pass
            raise
    return handler._send(200, {"project": project,
                               "output_url": BASE_PATH + "/" + project_id + "/output"})


def _send_output(handler, user, project_id, out_dir):
    project = store.get_project(user["username"], project_id)
    rel = str(project.get("output_file") or "").replace("\\", "/").lstrip("/")
    base = pathlib.Path(out_dir).resolve()
    target = (base / rel).resolve()
    try:
        target.relative_to(base)
    except Exception:
        raise LookupError("成片不存在")
    if not target.is_file():
        raise LookupError("成片不存在")
    handler.send_response(200)
    handler.send_header("Content-Type", "video/mp4")
    handler.send_header("Content-Length", str(target.stat().st_size))
    handler.send_header("Cache-Control", "private, no-store")
    handler.end_headers()
    with target.open("rb") as source:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            handler.wfile.write(chunk)
    return True


def _send_error(handler, error):
    if isinstance(error, store.ProjectNotFound):
        return handler._send(404, {"detail": str(error)})
    if isinstance(error, store.RevisionConflict):
        return handler._send(409, {"detail": str(error), "code": "revision_conflict"})
    if isinstance(error, LookupError):
        return handler._send(404, {"detail": str(error)})
    return handler._send(400, {"detail": str(error)[:220]})


def dispatch_http(handler, method, verify_token, must_change_password, asset_db_factory, source_resolver=None, out_dir=None):
    path = handler.path.split("?", 1)[0]
    if not path.startswith("/api/gen/video-compose/"):
        return False
    user = _auth(handler, verify_token, must_change_password)
    if not user:
        return True
    try:
        if method == "POST" and path == BASE_PATH:
            _create_project(handler, user, asset_db_factory)
            return True
        match = ANALYSIS_RE.match(path)
        if method == "POST" and match:
            _save_analysis(handler, user, match.group(1))
            return True
        match = SOURCE_ANALYSIS_RE.match(path)
        if method == "POST" and match:
            _analyze_source(handler, user, match.group(1), source_resolver)
            return True
        match = DECISIONS_RE.match(path)
        if method == "POST" and match:
            _confirm_decisions(handler, user, match.group(1))
            return True
        match = RENDER_RE.match(path)
        if method == "POST" and match:
            _render_project(handler, user, match.group(1), asset_db_factory, source_resolver, out_dir)
            return True
        match = OUTPUT_RE.match(path)
        if method == "GET" and match:
            return _send_output(handler, user, match.group(1), out_dir)
        if method == "GET" and path == BASE_PATH:
            return handler._send(200, {"items": store.list_projects(user["username"])}) or True
        match = PROJECT_RE.match(path)
        if method == "GET" and match:
            return handler._send(200, {"project": store.get_project(user["username"], match.group(1))}) or True
        handler._send(404, {"detail": "not found"})
        return True
    except (ValueError, LookupError, store.RevisionConflict) as error:
        _send_error(handler, error)
        return True
