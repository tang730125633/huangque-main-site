"""Flask Web 服务：IP 人设定位对话 Agent 的浏览器体验入口。

运行：python app.py   （默认 http://127.0.0.1:8000）
"""
import json
import logging
import os
import time
import uuid
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory, stream_with_context

from agent import config, hq_cli, report, state
from agent.v4 import delivery as v4_delivery
from agent.v4 import main_agent as v4_main
from agent.v4 import observability
from agent.v4 import skills as v4_skills
from agent.v4 import state as v4_state
from agent.v4 import streaming as v4_streaming
from agent.v4 import subagent as v4_subagent
from agent.v4 import tunnel as v4_tunnel
from agent.v4 import vision as v4_vision

log = logging.getLogger("hq.app")

# Existing standalone deployments keep static/ next to app.py. The GitHub
# monorepo keeps workbench assets in the canonical site/workbench directory.
_page_root = Path(__file__).resolve().parent / "static"
_static_root = _page_root
if not (_page_root / "v4.html").is_file():
    _page_root = Path(__file__).resolve().parents[2] / "site/workbench/hq-ip-agent"
    _static_root = _page_root / "static"
app = Flask(__name__, static_folder=str(_static_root), static_url_path="/static")


@app.get("/")
def index():
    # v3 已合并进 v4：所有旧入口（/、/v3）统一落到 v4 页面，不再提供旧版 UI。
    return send_from_directory(_page_root, "v4.html")


@app.get("/v3")
def index_v3():
    return send_from_directory(_page_root, "v4.html")


@app.get("/v3/")
def index_v3_slash():
    return send_from_directory(_page_root, "v4.html")


@app.get("/v4")
def index_v4():
    return send_from_directory(_page_root, "v4.html")


@app.get("/api/health")
def health():
    status = {"ok": False, "detail": "未检测"}
    try:
        r = hq_cli.status()
        status = {"ok": r.get("exit_code") == 0, "detail": r.get("data")}
    except Exception as e:  # pragma: no cover
        status = {"ok": False, "detail": str(e)}
    return jsonify({
        "llm_mode": config.LLM_MODE,
        "llm_model": config.LLM_MODEL if config.LLM_MODE == "openai" else None,
        "hq_bin": config.HQ_BIN,
        "hq_status": status,
    })


def _visible_history(messages: list) -> list:
    """只回传 UI 要展示的消息：过滤 system、内部注入事件与纯工具调用条目。"""
    out = []
    for m in messages or []:
        role = m.get("role")
        content = (m.get("content") or "").strip()
        if role == "assistant":
            if content:
                item = {"role": "assistant", "content": content}
                if m.get("images"):
                    item["images"] = list(m["images"])  # 采集交付的本地图片：恢复时直接渲染
                out.append(item)
        elif role == "user":
            if not content:
                continue
            if content.startswith(("（用户刚进入对话", "（系统事件")):
                continue  # 内部注入，不展示
            out.append({"role": "user", "content": content})
    return out


def _drop_session_file(sid: str):
    import os
    for prefix in ("", "v4-"):
        p = os.path.join(state.SESSION_DIR, f"{prefix}{sid}.json")
        try:
            os.remove(p)
        except OSError:
            pass


def _list_sessions(prefix: str, limit: int = 12) -> list:
    """列出落盘的最近会话（按更新时间倒序），带预览。prefix='' 为 v3，'v4-' 为 v4。"""
    import glob
    import os as _os
    import time as _time
    out = []
    for p in glob.glob(_os.path.join(state.SESSION_DIR, f"{prefix}*.json")):
        name = _os.path.basename(p)[len(prefix):-5]
        if not name:
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                snap = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        hist = snap.get("main") if prefix == "v4-" else snap.get("history")
        preview, turns = _session_preview(hist)
        if not preview:
            continue
        out.append({
            "sid": name,
            "updated_at": int(_os.path.getmtime(p)),
            "turns": turns,
            "preview": preview,
        })
    out.sort(key=lambda x: x["updated_at"], reverse=True)
    return out[:limit]


def _session_preview(messages: list):
    """取会话预览：第一条用户消息 + 最后一条助手消息 + 用户发言数。"""
    if not messages:
        return "", 0
    first_user = ""
    last_assistant = ""
    user_turns = 0
    for m in messages:
        role = m.get("role")
        content = (m.get("content") or "").strip()
        if not content:
            continue
        if content.startswith(("（用户刚进入对话", "（系统事件")):
            continue
        if role == "user":
            user_turns += 1
            if not first_user:
                first_user = content
        elif role == "assistant":
            last_assistant = content
    preview = first_user or last_assistant or "（空会话）"
    if len(preview) > 42:
        preview = preview[:42] + "…"
    return preview, user_turns


@app.get("/api/v4/sessions")
def v4_sessions_list():
    return jsonify({"sessions": _list_sessions("v4-")})


@app.get("/api/report/<sid>")
def report_status(sid):
    """报告生成进度轮询（生成是同步长任务，前端边等边刷进度）。"""
    state.restore(sid)
    return jsonify(_report_summary(sid))


def _report_summary(sid: str) -> dict:
    meta = state.get_report(sid)
    if not meta:
        return {}
    out = {
        "status": meta.get("status"),
        "phase": meta.get("phase"),
        "round": meta.get("round"),
        "rounds": meta.get("rounds"),
        "gaps": meta.get("gaps", []),
        "chosen": meta.get("chosen"),
        "chosen_title": meta.get("chosen_title"),
        "title": meta.get("title"),
        "confirmed": bool(meta.get("confirmed")),
        "error": meta.get("error"),
        "files": _file_links(meta.get("files")),
    }
    for key in ("m5", "m6"):
        mod = meta.get(key) or {}
        out[key] = {
            "status": mod.get("status"),
            "phase": mod.get("phase"),
            "rounds": mod.get("rounds"),
            "gaps": mod.get("gaps", []),
            "topic": mod.get("topic"),
            "files": _file_links(mod.get("files")),
        }
    return out


def _file_links(files: dict) -> dict:
    """产物下载链接。用相对路径：页面挂在 /workbench/ip12/ 或 /hermes-ip12/ 等前缀下，
    写死根路径 /api/download 会跳去主站其他后端导致 404，相对路径随页面前缀正确解析。"""
    files = files or {}
    return {
        kind: f"api/download/{files[kind]}" if files.get(kind) else None
        for kind in ("pdf", "md", "json")
    }


@app.get("/api/download/<path:filename>")
def download(filename):
    os.makedirs(report.OUTPUT_DIR, exist_ok=True)
    return send_from_directory(
        report.OUTPUT_DIR, filename, as_attachment=True,
        download_name=filename,
    )


_MEDIA_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
               ".webp": "image/webp", ".gif": "image/gif"}


@app.get("/api/v4/media/<path:subpath>")
def v4_media(subpath: str):
    """采集成果图片：本地化后长期可访问（CDN 直链会过期/防盗链，页面直连常 403）。"""
    base = os.path.abspath(v4_delivery.COLLECT_MEDIA_DIR)
    full = os.path.abspath(os.path.join(base, subpath))
    if not full.startswith(base + os.sep) or not os.path.isfile(full):
        return jsonify({"error": "not found"}), 404
    ext = os.path.splitext(full)[1].lower()
    return send_from_directory(base, subpath, max_age=31536000,
                               mimetype=_MEDIA_MIME.get(ext, "application/octet-stream"))


# ---------------------------------------------------------------------------
# v4：主 Agent（路由）+ 12 个业务子 Agent
# ---------------------------------------------------------------------------

def _same_origin_ok() -> bool:
    """防 CSRF：浏览器跨站请求会带 Origin 头，与本服务 Host 不一致即拒绝；
    无 Origin（同源导航、curl、自家脚本）放行。写操作（reset/confirm）入口校验。"""
    origin = request.headers.get("Origin")
    if not origin:
        return True
    host = request.headers.get("Host", "")
    try:
        from urllib.parse import urlparse
        return urlparse(origin).netloc == host
    except Exception:
        return False


@app.post("/api/v4/start")
def v4_start():
    """新会话：立即返回 session_id 与确认，开场白在后台轮次生成（首屏不再白等 10-18 秒）。"""
    sid = uuid.uuid4().hex
    v4_state.reset(sid)
    seq = _spawn_turn(sid, None)  # message=None：开场轮（走 main_agent 开场分支）
    return jsonify({
        "session_id": sid,
        "async": True,
        "seq": seq,
        "ack": "正在准备，稍等一下 👀",
        "mode": config.LLM_MODE,
    })


@app.post("/api/v4/chat")
def v4_chat():
    """收消息：立即返回「已收到」确认，主 Agent 轮次在后台线程执行，
    前端用 /api/v4/poll/<sid>?seq=N 轮询取结果。"""
    body = request.get_json(force=True, silent=True) or {}
    sid = (body.get("session_id") or "").strip()
    message = (body.get("message") or "").strip()
    if not sid:
        return jsonify({"error": "缺少 session_id"}), 400
    if not message:
        return jsonify({"error": "消息不能为空"}), 400
    # 附件：先解析出本地路径（便宜），视觉描述等重活在后台线程做
    paths = []
    for fid in (body.get("attachments") or []):
        p = _resolve_upload(sid, fid)
        if p:
            paths.append(p)
    approval = body.get("approval")
    if approval is not None:
        if (not isinstance(approval, dict) or approval.get("domain") not in v4_skills.DOMAINS
                or approval.get("decision") not in ("confirm", "cancel")
                or not isinstance(approval.get("quote_id"), str) or len(approval["quote_id"]) != 64):
            return jsonify({"error": "报价信息无效，请刷新当前报价卡。"}), 400
    seq = _spawn_turn(sid, message, paths, approval=approval)
    return jsonify({
        "async": True,
        "seq": seq,
        "ack": "收到，这就去处理，稍等一下 👀",
        "mode": config.LLM_MODE,
    })


# ---------------------------------------------------------------------------
# 后台轮次：即时确认 + 后台执行 + 轮询取结果
# ---------------------------------------------------------------------------

import threading

_TURN_LOCK = threading.Lock()      # 保护下列注册表
_TURN_RESULTS = {}                 # sid -> {seq: {"state": working|done|error, ...}}
_CONFIRM_LOCKS = {}                # sid -> {"lock": Lock, "refs": 使用计数, "touch": 最近使用}
                                   #（自动确认锁：防并发轮次重复触发模块5；引用计数让看护线程安全回收）
_CONFIRM_LOCKS_GUARD = threading.Lock()
_GLOBAL_SEQ = 0                    # 全局单调轮次号（毫秒时间戳保证重启后不倒退，
                                   # 前端凭「结果 seq >= 我发起的 seq」判断等到了哪轮）
_TURN_STALE_SECONDS = 600          # working 超过该时长视为中断（> TURN_BUDGET + 收尾余量），
                                   # poll 兜底返回 error，前端不会无限干等

# ---------------------------------------------------------------------------
# SSE 实时推送：前端挂一条长连接收「turn 完成」与「状态快照」事件，
# 替代 2 秒轮询（轮询接口保留作降级）。有订阅者才入队，无订阅直接丢弃。
# ---------------------------------------------------------------------------

_STATUS_CACHE_GUARD = threading.Lock()
_STATUS_CACHE = {}        # sid -> (ts, payload)：状态载荷进程内短缓存
_STATUS_CACHE_TTL = 2.5   # 秒：SSE 心跳/轮询都是 2 秒节奏，2.5s 让连续心跳至少命中一次
_STATUS_CACHE_MAX = 500   # 缓存条目上限（超出踢最旧），防 sid 数量异常时缓存自己膨胀


def _status_cache_bust(sid: str):
    """轮次/交付完成时清掉该 sid 的状态缓存：下一帧状态立即是新世界，不等 TTL 自然过期。"""
    with _STATUS_CACHE_GUARD:
        _STATUS_CACHE.pop(sid, None)


# 交付模块（v4_delivery）完成采集交付时也走同一失效通道
v4_delivery.set_status_bust(_status_cache_bust)


def _v4_status_payload(sid: str) -> dict:
    """实时进度载荷（带短缓存）：同一 sid 多个标签页各挂 SSE/轮询时，
    2 秒节奏下每 2.5 秒最多真正组装一次载荷（自愈重活、报告/六态重建都只跑这一次），
    其余心跳直接复用缓存结果。turn/delivery 事件会主动清缓存保证时效。"""
    now = time.time()
    with _STATUS_CACHE_GUARD:
        hit = _STATUS_CACHE.get(sid)
        if hit and now - hit[0] < _STATUS_CACHE_TTL:
            return hit[1]
    payload = _v4_status_payload_raw(sid)
    with _STATUS_CACHE_GUARD:
        _STATUS_CACHE[sid] = (time.time(), payload)
        if len(_STATUS_CACHE) > _STATUS_CACHE_MAX:
            oldest = min(_STATUS_CACHE, key=lambda s: _STATUS_CACHE[s][0])
            _STATUS_CACHE.pop(oldest, None)
    return payload


def _v4_status_payload_raw(sid: str) -> dict:
    """实时进度载荷：进行中的轮次（含已耗时）、子 Agent 正在调用的工具、
    各域六态、后台报告任务与报告状态。轮询与 SSE 共用。
    顺带触发报告任务中断自愈（卡死的 generating 自动重跑）。"""
    try:
        v4_main.resume_stale_reports(sid)
    except Exception:
        pass  # 自愈失败不影响状态返回
    # 服务重启后内存态为空：仅当未加载时从磁盘恢复（已加载则以内存为准，避免与在跑轮次竞态）
    try:
        if not v4_state.is_loaded(sid):
            v4_state.restore(sid)
    except Exception:
        pass
    # 后台任务自愈（内部 20s 节流）：running 的任务补查后六态即时刷新，页面不落灰
    try:
        v4_delivery.resume_stale_jobs(sid)
    except Exception:
        pass
    # 已完成但未交付的采集任务：立即交付图片（刷新页面/常开 SSE 都能收到，不用先发消息）
    try:
        collect_sess = v4_state.get_subagent(sid, "collect")
        last_c = (collect_sess or {}).get("last_result") or {}
        if last_c.get("state") == "completed":
            jid = (last_c.get("result") or {}).get("job_id")
            if jid and not v4_delivery.collect_marker_delivered(sid, jid):
                from agent.v4 import media as v4_media
                content = last_c.get("result") or {}
                if v4_media.extract_image_urls(content):
                    v4_delivery.maybe_spawn_finalize(sid, jid, content)
    except Exception:
        pass
    now = time.time()
    with _TURN_LOCK:
        results = _TURN_RESULTS.get(sid) or {}
        turns = [
            {"seq": k, "state": v.get("state"), "elapsed": round(now - v.get("ts", now), 1)}
            for k, v in sorted(results.items())
        ]
    prog = v4_subagent.PROGRESS.get(sid) or {}
    tool = None
    if prog and now - prog.get("ts", 0) < 20:  # 20 秒内的工具心跳才算「正在调用」
        tool = {"domain": prog.get("domain"), "name": prog.get("tool")}
    # 最近交付（采集带图消息）：SSE 掉线窗口内完成的交付，前端 status 帧按内容去重补渲染，
    # 不用等刷新页面从历史恢复（贴图断线补偿）。
    deliveries = []
    for m in reversed(v4_state.get_main_history(sid)):
        if m.get("images") and m.get("media_job"):
            deliveries.append({
                "reply": m.get("content") or "",
                "images": list(m.get("images") or []),
                "job": m.get("media_job"),
            })
        if len(deliveries) >= 3:
            break
    return {
        "turns": turns,
        "tool": tool,
        "jobs": v4_main.list_running_jobs(sid),
        "delegations": _v4_delegations(sid),
        "report": _report_summary(sid),
        "film": v4_state.get_last_film(sid),
        "deliveries": deliveries,
    }


# ---------------------------------------------------------------------------
# 注册表看护：内存注册表（轮次结果/确认锁/补查节流/异步任务/域锁/进度/写锁）
# 都是「按 sid 增长、用完没人清」的结构，长期运行会无界膨胀。看护线程每分钟
# 回收一次「无人持有且久未使用」的纯内存项——只动内存登记，落盘数据不受影响。
# ---------------------------------------------------------------------------

_JANITOR_INTERVAL = 60          # 秒：看护节奏
_REGISTRY_IDLE_SECONDS = 1800   # 纯内存项闲置 30 分钟即可回收
_REGISTRY_CAPS = {              # 各注册表兜底上限（超出按最旧优先踢闲置项）
    "turn_results": 300,        # sid 数
    "confirm_locks": 300,       # sid 数
    "job_poll": 2000,           # (sid, domain) 条目数
}


def _trim_registries():
    """一轮看护：所有注册表回收一遍。任何一步出错都不影响其他表（下一轮再试）。"""
    now = time.time()
    # 轮次结果：全部轮次已结束且 30 分钟没动过的 sid 整体清掉（刷新恢复走落盘历史，
    # 轮次结果的唯一用途是给刷新窗口内的轮询兜底回放，超时后不再需要）。
    try:
        with _TURN_LOCK:
            for sid in list(_TURN_RESULTS.keys()):
                results = _TURN_RESULTS[sid]
                if not results:
                    _TURN_RESULTS.pop(sid, None)
                    continue
                newest = max(v.get("ts", 0) for v in results.values())
                if all(v.get("state") != "working" for v in results.values()) \
                        and now - newest > _REGISTRY_IDLE_SECONDS:
                    _TURN_RESULTS.pop(sid, None)
            if len(_TURN_RESULTS) > _REGISTRY_CAPS["turn_results"]:
                ranked = sorted(
                    _TURN_RESULTS.keys(),
                    key=lambda s: max((v.get("ts", 0) for v in _TURN_RESULTS[s].values()), default=0),
                )
                for sid in ranked[: len(_TURN_RESULTS) - _REGISTRY_CAPS["turn_results"]]:
                    if all(v.get("state") != "working" for v in _TURN_RESULTS[sid].values()):
                        _TURN_RESULTS.pop(sid, None)
    except Exception:
        pass
    # 自动确认锁：无人持有（refs=0）且久未使用才回收
    try:
        with _CONFIRM_LOCKS_GUARD:
            for sid in list(_CONFIRM_LOCKS.keys()):
                e = _CONFIRM_LOCKS[sid]
                if e["refs"] == 0 and now - e["touch"] > _REGISTRY_IDLE_SECONDS:
                    _CONFIRM_LOCKS.pop(sid, None)
            if len(_CONFIRM_LOCKS) > _REGISTRY_CAPS["confirm_locks"]:
                for sid in sorted(_CONFIRM_LOCKS, key=lambda s: _CONFIRM_LOCKS[s]["touch"])\
                        [: len(_CONFIRM_LOCKS) - _REGISTRY_CAPS["confirm_locks"]]:
                    if _CONFIRM_LOCKS[sid]["refs"] == 0:
                        _CONFIRM_LOCKS.pop(sid, None)
    except Exception:
        pass
    # 补查节流表：条目数封顶（值里带 ts，按最旧踢；实现在 v4_delivery）
    try:
        v4_delivery.trim_job_poll(now, cap=_REGISTRY_CAPS["job_poll"])
    except Exception:
        pass
    # 各模块自己的注册表（域锁/进度/异步任务/写锁）
    try:
        v4_subagent.trim_progress(now)
    except Exception:
        pass
    try:
        v4_subagent.trim_domain_locks(now, max_idle=_REGISTRY_IDLE_SECONDS)
    except Exception:
        pass
    try:
        v4_main.trim_async_jobs(now)
    except Exception:
        pass
    try:
        v4_state.trim_persist_guards(now, max_idle=_REGISTRY_IDLE_SECONDS)
    except Exception:
        pass


def _start_janitor():
    """启动注册表看护线程（daemon）：进程活着就一直轮转，看护失败绝不带崩主服务。"""
    def loop():
        while True:
            time.sleep(_JANITOR_INTERVAL)
            try:
                _trim_registries()
            except Exception:
                pass
    threading.Thread(target=loop, daemon=True, name="registry-janitor").start()


# ---------------------------------------------------------------------------
# 采集成果交付：任务完成后把图片下载到本地并「贴」进对话（带图消息 + SSE 实时推送）
# ---------------------------------------------------------------------------

def _spawn_turn(sid: str, message: str, paths: list = None, approval: dict = None) -> int:
    """登记一个后台轮次并启动工作线程。返回轮次号 seq（前端轮询用）。"""
    paths = paths or []
    global _GLOBAL_SEQ
    with _TURN_LOCK:
        _GLOBAL_SEQ = max(int(time.time() * 1000), _GLOBAL_SEQ + 1)
        seq = _GLOBAL_SEQ
        _TURN_RESULTS.setdefault(sid, {})[seq] = {"state": "working", "ts": time.time()}

    def work():
        try:
            msg = message
            if paths:
                # 图片：视觉模型描述内容；音频等：只标注类型，路径交给需要它的子 Agent
                img_paths = [p for p in paths
                             if os.path.splitext(p)[1].lower() in _ALLOWED_ATTACH_EXT - _AUDIO_EXT]
                audio_paths = [p for p in paths
                               if os.path.splitext(p)[1].lower() in _AUDIO_EXT]
                other_paths = [p for p in paths if p not in img_paths and p not in audio_paths]
                parts = []
                if img_paths:
                    descs = [d for d in (v4_vision.describe_image(p) for p in img_paths) if d]
                    if descs:
                        lines = "\n".join("%d. %s" % (i + 1, d) for i, d in enumerate(descs))
                        parts.append("图片视觉描述（视觉模型生成，可据此讨论图片内容）：\n" + lines)
                file_lines = []
                if img_paths:
                    file_lines.append("图片：" + "、".join(img_paths))
                if audio_paths:
                    file_lines.append("录音/音频：" + "、".join(audio_paths) +
                                      "（音频文件无法直接听取，需要克隆音色/上传给黄雀时把路径交给子 Agent）")
                if other_paths:
                    file_lines.append("其他文件：" + "、".join(other_paths))
                parts.append(
                    "服务器本地绝对路径：\n" + "\n".join(file_lines)
                    + "\n需要把文件传给黄雀时用这些路径，不要向用户索要路径。"
                )
                msg += "\n\n（用户随消息上传了附件。" + "；".join(parts) + "）"
            if not v4_state.is_loaded(sid):
                v4_state.restore(sid)  # 重启后首次加载；已加载则以内存为准（避免并发轮次竞态丢消息）
            state.restore(sid)
            try:
                v4_main.resume_stale_reports(sid)  # 报告生成中断自愈（重启后卡死的任务自动重跑）
            except Exception:
                pass
            # 后台任务自愈：把停在 running 的异步任务补查一遍并刷新六态（「还在跑」错报的根修）
            try:
                v4_delivery.resume_stale_jobs(sid)
            except Exception:
                pass
            try:
                turn_options = {"context_note": v4_delivery.job_status_note(sid)}
                if approval is not None:
                    turn_options["approval"] = approval
                reply, tool_log, routing = v4_main.run_turn(sid, msg, **turn_options)
                # 意图门控权威信号：本轮派发了 digital-human = 出片轮。
                # 前端据此决定出片区挂载/卸载——卡片跟当前意图走，不跟会话历史走。
                film = any((r or {}).get("domain") == "digital-human" for r in (routing or []))
                v4_state.set_last_film(sid, film)
                result = {
                    "state": "done",
                    "reply": reply or "",
                    "tool_log": tool_log,
                    "routing": routing,
                    "delegations": _v4_delegations(sid),
                    "report": _report_summary(sid),
                    "widgets": v4_state.get_widgets(sid),
                    "film": film,
                    "mode": config.LLM_MODE,
                }
            except Exception as err:
                # 错误必须可见：进 journalctl（按 sid/seq 过滤）+ reply 带错误类型（三次吞错教训）
                log.exception("turn %s run_turn failed", seq, extra=observability.ctx(sid, seq))
                result = {
                    "state": "error",
                    "reply": "这轮处理出错了（%s）。你可以再发一次试试，任务不会重复扣费。" % type(err).__name__,
                }
                # 出错轮也带上当前报告/派发/出片状态：前端绝不用「缺字段」去隐藏报告栏、清出片选择
                try:
                    result["report"] = _report_summary(sid)
                    result["delegations"] = _v4_delegations(sid)
                    result["film"] = v4_state.get_last_film(sid)
                except Exception:
                    pass
                # 防御：本轮用户消息 + 错误回复成对补写历史，不让消息凭空消失
                # （message=None 的开场轮没有用户消息，只补错误回复）
                try:
                    if msg is None:
                        v4_state.append_main_history(sid, [
                            {"role": "assistant", "content": result["reply"]},
                        ])
                    else:
                        v4_state.append_main_history(sid, [
                            {"role": "user", "content": msg},
                            {"role": "assistant", "content": result["reply"]},
                        ])
                except Exception:
                    pass
            v4_state.persist(sid)
            state.persist(sid)
            # 采集成果交付：running 的任务挂看护线程（完成自动贴图）；已完成未交付的立即交付
            try:
                collect_sess = v4_state.get_subagent(sid, "collect")
                last_c = (collect_sess or {}).get("last_result") or {}
                if last_c.get("state") == "running":
                    jid = (last_c.get("result") or {}).get("job_id")
                    if jid:
                        v4_delivery.spawn_collect_watcher(sid, jid)
                elif last_c.get("state") == "completed":
                    jid = (last_c.get("result") or {}).get("job_id")
                    if jid and not v4_delivery.collect_marker_delivered(sid, jid):
                        from agent.v4 import media as v4_media
                        content = last_c.get("result") or {}
                        if v4_media.extract_image_urls(content):
                            v4_delivery.maybe_spawn_finalize(sid, jid, content)
            except Exception:
                pass  # 交付失败不影响本轮响应
            # 体验优先：报告定稿后自动确认并续跑模块5，不打断用户。
            # 只认 final：draft_ready 是「三套方案待用户选」阶段，绝不能跳过用户的选择。
            # 确认锁 + 锁内二次检查：并发轮次同时看到 final+未确认时也只触发一次模块5。
            # refs 计数：看护线程只回收无人持有且久未使用的锁，回收瞬间起新调用拿新锁，互斥不破。
            confirm_entry = None
            try:
                with _CONFIRM_LOCKS_GUARD:
                    confirm_entry = _CONFIRM_LOCKS.setdefault(
                        sid, {"lock": threading.Lock(), "refs": 0, "touch": time.time()})
                    confirm_entry["refs"] += 1
                    confirm_entry["touch"] = time.time()
                with confirm_entry["lock"]:
                    meta = _report_summary(sid)
                    if meta.get("status") == "final" and not meta.get("confirmed"):
                        state.set_report(sid, {"confirmed": True})
                        _spawn_turn(
                            sid,
                            "（系统事件，用户不可见：报告已定稿，用户已授权自动确认。"
                            "请立即启动模块5：先调用 get_report 确认已确认状态，然后调用 m5_topics 生成选题并完整输出。"
                            "全程自动推进，不要停下来问用户要不要继续。）",
                        )
            except Exception:
                pass  # 自动续跑失败不影响本轮交付
            finally:
                if confirm_entry is not None:
                    with _CONFIRM_LOCKS_GUARD:
                        confirm_entry["refs"] -= 1
            with _TURN_LOCK:
                _TURN_RESULTS.setdefault(sid, {})[seq] = result
                # 只保留最近 10 个轮次，防内存膨胀
                for k in sorted(_TURN_RESULTS[sid].keys())[:-10]:
                    _TURN_RESULTS[sid].pop(k, None)
            _status_cache_bust(sid)
            v4_streaming.emit(sid, "turn", dict(result, seq=seq))
        except Exception:
            # 收尾段任何意外（锁/落盘异常）都不能让轮次永远停在 working
            log.exception("turn %s fatal", seq, extra=observability.ctx(sid, seq))
            try:
                result = {
                    "state": "error",
                    "reply": "这轮后台处理中断了，请重新发送刚才那条消息。",
                }
                # 同 run_turn 异常路径：带上当前界面状态，前端不会误隐藏报告栏/清出片选择
                try:
                    result["report"] = _report_summary(sid)
                    result["delegations"] = _v4_delegations(sid)
                    result["film"] = v4_state.get_last_film(sid)
                except Exception:
                    pass
                with _TURN_LOCK:
                    _TURN_RESULTS.setdefault(sid, {})[seq] = result
                _status_cache_bust(sid)
                v4_streaming.emit(sid, "turn", dict(result, seq=seq))
            except Exception:
                pass

    threading.Thread(target=work, daemon=True, name="turn-%s-%d" % (sid[:8], seq)).start()
    return seq


# 报告类重活在后台线程跑，完成后经此入口把「系统事件」回注成新一轮对话
v4_main.set_turn_spawner(_spawn_turn)


@app.get("/api/v4/poll/<sid>")
def v4_poll(sid):
    """轮询后台轮次结果：按轮次号顺序交付所有已完成结果（FIFO），响应带 seq。
    working 超过 _TURN_STALE_SECONDS 的轮次视为中断（线程崩溃/服务重启），兜底返回 error；
    无任何轮次记录时返回 idle——前端据此提示重发而不是干等。"""
    with _TURN_LOCK:
        results = _TURN_RESULTS.get(sid) or {}
        now = time.time()
        stale = [
            k for k, v in results.items()
            if v.get("state") == "working" and now - v.get("ts", 0) > _TURN_STALE_SECONDS
        ]
        for k in stale:
            results[k] = {
                "state": "error",
                "reply": "这轮后台处理中断了（等待过久），请重新发送刚才那条消息。",
            }
        done_keys = sorted(k for k, v in results.items() if v.get("state") != "working")
        if done_keys:
            k = done_keys[0]
            res = dict(results[k])
            res["seq"] = k
            for kk in list(results.keys()):
                # 只清理已交付的旧轮次；并发下更早的轮次可能仍在 working，必须保留等它完成
                if kk <= k and results[kk].get("state") != "working":
                    results.pop(kk, None)
            return jsonify(res)
        if any(v.get("state") == "working" for v in results.values()):
            return jsonify({"state": "working"})
    return jsonify({"state": "idle"})


@app.get("/api/v4/status/<sid>")
def v4_status(sid):
    """实时进度：进行中的轮次（含已耗时）、子 Agent 正在调用的工具、各域六态与报告状态。
    前端可 2 秒一刷（SSE 的降级路径），把「正在干什么」画在对话气泡里，用户不用猜。"""
    return jsonify(_v4_status_payload(sid))


@app.get("/api/v4/stream/<sid>")
def v4_stream(sid):
    """SSE 实时推送：turn 完成事件 + 每 2 秒一次状态快照（兼作心跳）。
    前端优先走这条长连接，连接失败自动降级回轮询接口。"""
    def gen():
        sub = v4_streaming.subscribe(sid)
        try:
            # 首帧即当前状态，前端不用再单独拉一次
            yield v4_streaming.sse_event("status", _v4_status_payload(sid))
            while True:
                with sub["cond"]:
                    sub["cond"].wait(timeout=2.0)
                    events = []
                    while sub["q"]:
                        events.append(sub["q"].popleft())
                if events:
                    for ev, data in events:
                        yield v4_streaming.sse_event(ev, data)
                else:
                    yield v4_streaming.sse_event("status", _v4_status_payload(sid))
        finally:
            v4_streaming.unsubscribe(sid, sub)

    return Response(
        stream_with_context(gen()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/v4/restore/<sid>")
def v4_restore(sid):
    """恢复一个 v4 会话：可见对话历史 + 各域子 Agent 六态 + 报告状态。"""
    if not v4_state.restore(sid):
        return jsonify({"ok": False, "error": "会话不存在或已过期"}), 404
    state.restore(sid)  # 本地 IP 管线的报告状态一并恢复
    return jsonify({
        "ok": True,
        "history": _visible_history(v4_state.get_main_history(sid)),
        "delegations": _v4_delegations(sid),
        "report": _report_summary(sid),
        "widgets": v4_state.get_widgets(sid),
        "film": v4_state.get_last_film(sid),
        "mode": config.LLM_MODE,
    })


@app.post("/api/v4/reset")
def v4_reset():
    if not _same_origin_ok():
        return jsonify({"error": "跨站请求被拒绝"}), 403
    body = request.get_json(force=True, silent=True) or {}
    sid = (body.get("session_id") or "").strip()
    if sid:
        v4_state.reset(sid)
        _drop_session_file(sid)
    return jsonify({"ok": True})


@app.post("/api/v4/confirm")
def v4_confirm():
    """v4 页面的「确认报告」：锁定报告 → 后台轮次注入系统事件，主 Agent 自主启动模块5。
    立即返回确认（不再同步等 10-18 秒），模块5 进展走 SSE/轮询。"""
    if not _same_origin_ok():
        return jsonify({"error": "跨站请求被拒绝"}), 403
    body = request.get_json(force=True, silent=True) or {}
    sid = (body.get("session_id") or "").strip()
    if not sid:
        return jsonify({"error": "缺少 session_id"}), 400
    state.restore(sid)
    meta = state.get_report(sid)
    if not meta or meta.get("status") not in ("final", "draft_ready"):
        return jsonify({"error": "还没有可确认的报告，请先完成模块1-4 报告生成"}), 400
    state.set_report(sid, {"confirmed": True})
    state.persist(sid)
    seq = _spawn_turn(
        sid,
        "（系统事件，用户不可见：用户已在网页上点击「确认报告」，对当前模块1-4 报告表示满意。"
        "请立即启动模块5：先调用 get_report 确认已确认状态，然后调用 m5_topics 生成选题并完整输出。"
        "全程自动推进，不要停下来问用户要不要继续。）",
    )
    return jsonify({
        "ok": True,
        "async": True,
        "seq": seq,
        "ack": "已确认，模块5 启动中，稍等一下 👀",
        "mode": config.LLM_MODE,
    })


@app.get("/api/v4/state/<sid>")
def v4_state_view(sid):
    return jsonify(_v4_delegations(sid))


# ---------------------------------------------------------------------------
# v4 附件上传：输入框贴图 → 缩略图预览 → 随文字发送（发送时把本地路径给主 Agent）
# ---------------------------------------------------------------------------

UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "uploads")

_ALLOWED_ATTACH_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif",
                       ".mp3", ".wav", ".m4a", ".aac", ".ogg"}
_AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".ogg"}
_MAX_UPLOAD_BYTES = 10 * 1024 * 1024


def _upload_dir(sid: str) -> str:
    return os.path.join(UPLOAD_DIR, sid)


def _resolve_upload(sid: str, file_id: str):
    """把 file_id 解析成服务器本地绝对路径（防目录穿越，只认本会话目录内文件）。"""
    if not file_id or "/" in file_id or "\\" in file_id or ".." in file_id:
        return None
    path = os.path.realpath(os.path.join(_upload_dir(sid), file_id))
    base = os.path.realpath(_upload_dir(sid))
    if not path.startswith(base + os.sep) or not os.path.isfile(path):
        return None
    return path


@app.post("/api/v4/upload")
def v4_upload():
    sid = (request.form.get("session_id") or "").strip()
    f = request.files.get("file")
    if not sid:
        return jsonify({"error": "缺少 session_id"}), 400
    if not f or not f.filename:
        return jsonify({"error": "缺少文件"}), 400
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in _ALLOWED_ATTACH_EXT:
        return jsonify({"error": "只支持图片（png/jpg/jpeg/webp/gif）和音频（mp3/wav/m4a/aac/ogg）"}), 400
    data = f.read()
    if len(data) > _MAX_UPLOAD_BYTES:
        return jsonify({"error": "文件超过 10MB"}), 400
    os.makedirs(_upload_dir(sid), exist_ok=True)
    file_id = f"{uuid.uuid4().hex}{ext}"
    with open(os.path.join(_upload_dir(sid), file_id), "wb") as out:
        out.write(data)
    return jsonify({
        "ok": True,
        "file_id": file_id,
        "name": f.filename,
        "kind": "audio" if ext in _AUDIO_EXT else "image",
        "url": f"/api/v4/file/{sid}/{file_id}",
    })


@app.get("/api/v4/file/<sid>/<file_id>")
def v4_file(sid, file_id):
    path = _resolve_upload(sid, file_id)
    if not path:
        return jsonify({"error": "文件不存在"}), 404
    ext = os.path.splitext(path)[1].lower()
    mime = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".webp": "image/webp", ".gif": "image/gif",
        ".mp3": "audio/mpeg", ".wav": "audio/wav",
        ".m4a": "audio/mp4", ".aac": "audio/aac", ".ogg": "audio/ogg",
    }.get(ext, "application/octet-stream")
    resp = send_from_directory(os.path.dirname(path), os.path.basename(path),
                               max_age=0)
    resp.headers["Content-Type"] = mime
    resp.headers["Cache-Control"] = "no-cache"
    return resp


def _v4_delegations(sid: str) -> dict:
    """当前会话各业务域子 Agent 的最新六态（不含工具参数细节）。"""
    out = {}
    for domain in v4_state.all_domains(sid):
        sess = v4_state.get_subagent(sid, domain)
        if not sess:
            continue
        last = sess.get("last_result") or {}
        pending = sess.get("pending_quote") or {}
        summary = last.get("summary", "")
        if pending and last.get("state") == "needs_approval":
            summary = v4_subagent.quote_summary(pending)
        out[domain] = {
            "agent_id": v4_skills.DOMAINS.get(domain, domain),
            "state": last.get("state"),
            "summary": summary,
            "question": last.get("question", ""),
            "quote": {k: pending.get(k) for k in ("capability", "cost", "points", "expires_in")},
            "quote_id": v4_subagent.approval_id(pending),
        }
    return out


# ---------------------------------------------------------------------------
# 出境代理隧道：OpenAI 等境外 API 国内直连不通，走 dapeng-server 的 mihomo 出境代理
# （服务器 127.0.0.1:7897，经本机 SSH 隧道映射到 127.0.0.1:<本地端口>）。
# 服务启动时自动建立并保活；HQ_PROXY_TUNNEL=0 禁用，HQ_PROXY_SSH_HOST / HQ_PROXY_LOCAL_PORT 覆盖。
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    observability.setup_logging()
    # 业务 skill 安装副本校验：本机子 Agent 副本与项目源不同步时告警提醒 push
    # （服务器无 Penguin 副本目录会自动跳过；HQ_SKILL_SYNC_WARN=0 关闭）
    if os.environ.get("HQ_SKILL_SYNC_WARN", "1") != "0":
        _stale = [d for d, s in v4_skills.business_skill_sync_status() if s in ("missing", "diff")]
        if _stale:
            log.warning("业务 skill 安装副本与项目源不同步（%s），请运行 scripts/sync_skills.py --push",
                        ", ".join(_stale))
    v4_tunnel.start_proxy_tunnel()
    _start_janitor()
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    try:
        from waitress import serve
        threads = int(os.environ.get("WAITRESS_THREADS", "16"))
        log.info("waitress serving on %s:%d (threads=%d)", host, port, threads)
        # waitress：线程模型兼容现有内存态单进程约束；比 Flask dev server 多请求队列、
        # 背压与连接治理。channel_timeout 放宽到 180s，SSE 心跳每 2 秒一帧不会触顶。
        serve(app, host=host, port=port, threads=threads, channel_timeout=180)
    except ImportError:
        log.warning("waitress 未安装，回退 Flask dev server（生产环境请 pip install waitress）")
        app.run(host=host, port=port, debug=False, threaded=True)  # threaded: SSE 长连接不能堵住其他请求
