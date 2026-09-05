"""采集成果交付 + 后台任务自愈 + 状态速览（从 app.py 抽出）。

- 采集交付：任务完成后把图片下载到本地并「贴」进对话（带图消息 + SSE 实时推送），
  看护线程/status 帧/轮次收尾三路触发，入口原子抢占防并发重复；
- 后台自愈：把停在 running 的异步任务补查一遍，发现完成/失败就刷新六态（20s 节流）；
- 状态速览：把各域最新六态整理成一句话，注入主 Agent 上下文（「还在跑」错报的根修）。
"""
from __future__ import annotations

import logging
import os
import threading
import time

from .. import hq_cli
from . import observability
from . import state as v4_state
from . import streaming

log = logging.getLogger("hq.v4.delivery")

# 采集图片本地化目录（/api/v4/media 路由从此目录读，路由校验用同一常量）
COLLECT_MEDIA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "output", "collected",
)

_COLLECT_WATCHERS_GUARD = threading.Lock()
_COLLECT_WATCHERS = set()   # (sid, job_id)：采集任务看护线程（running → 完成交付）
_FINALIZING = set()         # (sid, job_id)：正在执行交付（防并发重复）

# 状态缓存失效回调（app.py 注入：交付/轮次完成时清 status 缓存，下一帧状态立即更新）
_STATUS_BUST = lambda sid: None


def set_status_bust(fn):
    """app.py 启动时注入：fn(sid) 清掉该 sid 的状态载荷缓存。"""
    global _STATUS_BUST
    _STATUS_BUST = fn


def collect_marker_delivered(sid: str, job_id) -> bool:
    """该采集任务是否已交付过（历史里查 media_job 标记，重启后依然有效）。"""
    for m in reversed(v4_state.get_main_history(sid)):
        if m.get("media_job") == job_id:
            return True
    return False


def finalize_collect(sid: str, job_id, content: dict) -> list:
    """采集任务完成交付（幂等且并发安全）：下载图片到本地 → 刷新 collect 六态 →
    追加带图消息进历史（页面刷新后仍可见）→ SSE 推送给在线页面。返回本地图片相对 URL。

    入口原子抢占 _FINALIZING：看护线程与 status/work 触发的交付并发同跑时只有一个执行，
    绝不重复下载、重复贴图、并发写盘。"""
    from . import media as v4_media
    from . import protocol as v4_protocol
    try:
        job_id = int(job_id)
    except (TypeError, ValueError):
        return []
    if not job_id or collect_marker_delivered(sid, job_id):
        return []
    key = (sid, job_id)
    with _COLLECT_WATCHERS_GUARD:
        if key in _FINALIZING:
            return []  # 另一条路径正在交付同一个任务
        _FINALIZING.add(key)
    try:
        content = content or {}
        urls = v4_media.extract_image_urls(content)
        names = v4_media.download_images(urls, sid, job_id, COLLECT_MEDIA_DIR) if urls else []
        rel = ["api/v4/media/%s/%s/%s" % (sid, job_id, n) for n in names]

        # 六态刷新为 completed（看护线程/自愈路径时原状态还是 running）
        sess = v4_state.get_subagent(sid, "collect")
        prev = (sess or {}).get("last_result") or {}
        new_result = dict(prev.get("result") or {})
        new_result.update({"job_id": job_id, "status": "done", "images_local": rel})
        title = ""
        author = ""
        if isinstance(content, dict):
            title = (content.get("title") or "").strip()
            video = content.get("video")
            if isinstance(video, dict):
                author = (video.get("author") or "").strip()
        label = ("《%s》" % title) if title else ("任务 %s" % job_id)
        summary = ("%s采集完成：%d 张图片已下载到本地并直接贴给用户（链接不过期）。"
                   % (label, len(rel))) if rel else ("%s采集完成。" % label)
        v4_state.save_subagent(sid, "collect", last_result=v4_protocol.make(
            v4_protocol.COMPLETED, summary=summary, result=new_result,
        ))

        # 带图消息写进历史：成对追加（user 系统事件 + assistant 带图），保持 user/assistant 交替合法
        n_comments = len(content.get("comments")) if isinstance(content, dict) \
            and isinstance(content.get("comments"), list) else 0
        text = ("📥 %s扒好了%s：正文、%d 张图、%d 条评论都拿到了。"
                "图片在下面 👇（已存到本地，链接不过期）。"
                % (label, ("（作者：%s）" % author) if author else "", len(rel), n_comments))
        v4_state.append_main_history(sid, [
            {"role": "user", "content": "（系统事件，用户不可见：采集任务 %s 完成交付）" % job_id},
            {"role": "assistant", "content": text, "images": rel, "media_job": job_id},
        ])
        v4_state.persist(sid)
        _STATUS_BUST(sid)
        streaming.emit(sid, "delivery", {"reply": text, "images": rel})
        log.info("collect job %s 完成交付：%d 张图片", job_id, len(rel), extra=observability.ctx(sid))
        return rel
    finally:
        with _COLLECT_WATCHERS_GUARD:
            _FINALIZING.discard(key)


def maybe_spawn_finalize(sid: str, job_id, content: dict):
    """后台线程执行交付（下载耗时，不阻塞轮询/轮次响应）。并发去重由 finalize_collect 入口原子抢占保证。"""
    try:
        job_id = int(job_id)
    except (TypeError, ValueError):
        return
    key = (sid, job_id)
    with _COLLECT_WATCHERS_GUARD:
        if key in _FINALIZING:
            return
    if collect_marker_delivered(sid, job_id):
        return

    def _run():
        try:
            finalize_collect(sid, job_id, content)
        except Exception as err:
            log.error("collect finalize job %s 失败: %s", job_id, err, extra=observability.ctx(sid))
    threading.Thread(target=_run, daemon=True).start()


def spawn_collect_watcher(sid: str, job_id):
    """采集任务看护：后台轮询（8s 间隔，最长 10 分钟），完成即自动交付图片。
    解决「任务结束却没人主动把图发给用户」。"""
    try:
        job_id = int(job_id)
    except (TypeError, ValueError):
        return
    key = (sid, job_id)
    with _COLLECT_WATCHERS_GUARD:
        if key in _COLLECT_WATCHERS:
            return
        _COLLECT_WATCHERS.add(key)

    def _watch():
        deadline = time.time() + 600
        try:
            while time.time() < deadline:
                time.sleep(8)
                r = hq_cli.run("task", {"job_id": job_id})
                task_res = ((r or {}).get("data") or {}).get("result") or {}
                phase = task_res.get("phase")
                if phase == "done":
                    finalize_collect(sid, job_id, task_res.get("result") or {})
                    break
                if phase in ("failed", "error", "cancelled"):
                    break
        except Exception as err:
            log.error("collect watcher job %s 失败: %s", job_id, err, extra=observability.ctx(sid))
        finally:
            with _COLLECT_WATCHERS_GUARD:
                _COLLECT_WATCHERS.discard(key)
    threading.Thread(target=_watch, daemon=True).start()


# ---------------------------------------------------------------------------
# 后台任务自愈
# ---------------------------------------------------------------------------

_JOB_POLL_INTERVAL = 20          # 秒：同一 running 任务两次补查的最小间隔（防每轮轰炸 hq API）
_LAST_JOB_POLL = {}              # (sid, domain) -> (job_id, 上次补查时间)


def resume_stale_jobs(sid: str):
    """后台任务自愈：把还停在 running 的异步子 Agent 任务补查一遍，
    发现已完成就刷新 last_result 为 completed 并落盘。

    解决「任务早完成了，对话里还在说『在跑/还在爬』」的错报——子 Agent 提交后
    返回 running，之后没有任何机制刷新状态，主 Agent 只能凭旧状态回话。"""
    from . import protocol as v4_protocol
    refreshed = False
    for domain in v4_state.all_domains(sid):
        sess = v4_state.get_subagent(sid, domain)
        last = (sess or {}).get("last_result") or {}
        if last.get("state") != v4_protocol.RUNNING:
            continue
        job_id = (last.get("result") or {}).get("job_id")
        if not job_id:
            continue
        key = (sid, domain)
        now = time.time()
        prev = _LAST_JOB_POLL.get(key)
        if prev and prev[0] == job_id and now - prev[1] < _JOB_POLL_INTERVAL:
            continue  # 刚查过且任务号没变：节流
        _LAST_JOB_POLL[key] = (job_id, now)
        try:
            r = hq_cli.run("task", {"job_id": int(job_id)})
        except (TypeError, ValueError):
            continue
        task_res = ((r or {}).get("data") or {}).get("result") or {}
        phase = task_res.get("phase")
        if not phase:
            continue
        if phase == "done":
            if domain == "collect":
                # 采集完成：图片本地化 + 带图消息 + SSE 推送（后台线程，不阻塞状态响应）
                maybe_spawn_finalize(sid, job_id, task_res.get("result") or {})
                refreshed = True
                continue
            content = task_res.get("result") or {}
            bits = []
            if isinstance(content, dict):
                for k in ("title", "prompt", "ctype", "dur", "platform"):
                    v = content.get(k)
                    if v:
                        bits.append(str(v)[:40])
                if content.get("scenes"):
                    bits.append("%d 个分镜" % len(content["scenes"]))
                if content.get("comments") is not None:
                    bits.append("%d 条评论" % len(content["comments"]))
                if content.get("images") is not None:
                    bits.append("%d 张图片" % len(content["images"]))
                if content.get("video_url") or content.get("url"):
                    bits.append("链接已可访问")
            summary = ("后台任务 %s 已完成：%s。" % (job_id, "、".join(bits))) if bits \
                else ("后台任务 %s 已完成。" % job_id)
            v4_state.save_subagent(sid, domain, last_result=v4_protocol.make(
                v4_protocol.COMPLETED,
                summary=summary,
                result={"job_id": job_id, "status": "done", "kind": task_res.get("kind"),
                        "detail": task_res.get("result") or {}},
            ))
            refreshed = True
        elif phase in ("failed", "error", "cancelled"):
            v4_state.save_subagent(sid, domain, last_result=v4_protocol.make(
                v4_protocol.FAILED if phase != "cancelled" else v4_protocol.CANCELLED,
                summary="后台任务 %s 已结束（%s）：%s" % (
                    job_id, phase, str(task_res.get("error") or "")[:120]),
                result={"job_id": job_id, "status": phase},
                retryable=False,
            ))
            refreshed = True
    if refreshed:
        v4_state.persist(sid)


def trim_job_poll(now: float, cap: int = 2000):
    """看护线程回收补查节流表（条目数封顶，按最旧优先踢）。"""
    if len(_LAST_JOB_POLL) <= cap:
        return
    for key in sorted(_LAST_JOB_POLL, key=lambda k: _LAST_JOB_POLL[k][1])[: len(_LAST_JOB_POLL) - cap]:
        _LAST_JOB_POLL.pop(key, None)


def job_status_note(sid: str) -> str:
    """把当前各域子 Agent 的最新六态整理成一句话速览，注入主 Agent 本轮上下文
    （只进 LLM 上下文、不进对话历史）：主 Agent 按真实状态回答，
    绝不凭旧印象说「还在跑/还在生成」。"""
    lines = []
    for domain in v4_state.all_domains(sid):
        sess = v4_state.get_subagent(sid, domain)
        last = (sess or {}).get("last_result") or {}
        st = last.get("state")
        if not st:
            continue
        job_id = (last.get("result") or {}).get("job_id")
        summary = (last.get("summary") or "").strip().replace("\n", " ")
        if len(summary) > 90:
            summary = summary[:90] + "…"
        if job_id:
            lines.append("「%s」任务 %s：%s｜%s" % (domain, job_id, st, summary))
        else:
            lines.append("「%s」：%s｜%s" % (domain, st, summary))
    if not lines:
        return ""
    return ("（以下是最新后台任务状态，仅供参考：回答时如涉及这些任务，务必按此状态说，"
            "绝不要凭旧印象说还在跑/还在生成）\n" + "\n".join(lines))
