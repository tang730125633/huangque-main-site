"""v4 会话状态：主 Agent 对话历史 + 各业务域子 Agent 会话（独立 LLM 实例）。

- 主会话：`sid` 级，只存主 Agent 自己的 message 历史；
- 子会话：`(sid, domain)` 级，存子 Agent 自己的 message 历史 +
  pending quote（先报价后确认的运行时凭据）+ 最近一次 SpecialistResult。
- 子 Agent 会话跨轮保留：needs_approval / needs_user_input / running 时，
  下一轮用户回应后继续同一会话，不重新开始。
"""
from __future__ import annotations
import json
import os
import threading
import time

_lock = threading.Lock()
_main = {}   # sid -> [message dict]
_subs = {}   # sid -> {domain: {"messages": [...], "pending_quote": dict|None, "last_result": dict|None}}
_domains_used = {}  # sid -> [domain 使用顺序]（对话记忆/调试用）
_widgets = {}  # sid -> [交互卡片 widget dict]（形象/音色/文案点选等，前端渲染）
_last_film = {}  # sid -> bool：最近一轮是否是出片轮（派发了 digital-human）。
                 # 前端意图门控的唯一权威信号：卡片跟当前意图走，不跟会话历史走。


def set_last_film(sid: str, film: bool):
    with _lock:
        _last_film[sid] = bool(film)


def get_last_film(sid: str) -> bool:
    with _lock:
        return bool(_last_film.get(sid))


def is_loaded(sid: str) -> bool:
    """内存里是否已加载该会话（服务重启后为空；已加载则以内存为准，避免重复 restore
    与正在运行的轮次竞态——commit 后、persist 前被磁盘旧版覆盖会丢消息）。"""
    with _lock:
        return sid in _main or sid in _subs or sid in _widgets


def get_main_history(sid: str) -> list:
    with _lock:
        return list(_main.get(sid, []))


def set_main_history(sid: str, messages: list):
    with _lock:
        _main[sid] = list(messages)


def append_main_history(sid: str, messages: list):
    """原子追加消息：并发轮次各自追加、互不覆盖（聊天不排队的关键）。"""
    with _lock:
        _main.setdefault(sid, []).extend(list(messages))


def ensure_main_seed(sid: str, system_msg: dict) -> None:
    """保证主会话存在（system 开头只写一次，并发首发也只会留一份）；
    提示词升级时（内容变化）旧会话的 system 消息自动原地跟进，不丢历史。"""
    with _lock:
        msgs = _main.get(sid)
        if not msgs:
            _main[sid] = [dict(system_msg)]
            return
        if msgs[0].get("role") == "system" and msgs[0].get("content") != system_msg.get("content"):
            msgs[0] = dict(system_msg)


def _snap_sess(s: dict) -> dict:
    """单个子 Agent 会话的深拷贝样板（get_subagent/快照/恢复三处共用）。"""
    return {
        "messages": list(s.get("messages") or []),
        "pending_quote": dict(s.get("pending_quote") or {}),
        "last_result": dict(s.get("last_result") or {}) if s.get("last_result") else None,
    }


def get_subagent(sid: str, domain: str) -> dict | None:
    with _lock:
        sess = _subs.get(sid, {}).get(domain)
        if sess is None:
            return None
        return _snap_sess(sess)


def save_subagent(sid: str, domain: str, messages: list | None = None,
                  pending_quote: dict | None = None, last_result: dict | None = None):
    """更新子 Agent 会话。messages=None 表示保留原历史（只更新 quote/result）。"""
    with _lock:
        sess = _subs.setdefault(sid, {}).setdefault(domain, {})
        if messages is not None:
            sess["messages"] = list(messages)
        if pending_quote is not None:
            sess["pending_quote"] = dict(pending_quote)
        if last_result is not None:
            sess["last_result"] = dict(last_result)
        if domain not in _domains_used.setdefault(sid, []):
            _domains_used[sid].append(domain)


def clear_pending_quote(sid: str, domain: str):
    with _lock:
        sess = _subs.get(sid, {}).get(domain)
        if sess:
            sess["pending_quote"] = None


def update_subagent(sid: str, domain: str, update):
    """Atomic read/modify/write for runtime receipts, including cross-domain polls.

    The callback must only transform the supplied dict (no I/O or state calls).
    This shares save_subagent's lock, so a task observation cannot erase a quote
    or receipt concurrently published by the owning domain.
    """
    with _lock:
        sess = _subs.setdefault(sid, {}).setdefault(domain, {})
        update(sess)
        if domain not in _domains_used.setdefault(sid, []):
            _domains_used[sid].append(domain)
        return _snap_sess(sess)


def all_domains(sid: str) -> list:
    with _lock:
        return list(_domains_used.get(sid, []))


# ---------------------------------------------------------------------------
# 交互卡片（widgets）：形象/音色/文案选项等，供前端渲染成可点击组件
# ---------------------------------------------------------------------------

def get_widgets(sid: str) -> list:
    with _lock:
        return list(_widgets.get(sid, []))


def add_widgets(sid: str, widgets: list):
    """追加卡片；同 id 的旧卡片会被替换（避免重复渲染）。
    每次注册同 id 卡片 gen+1：前端按 id@gen 记忆「已关闭」，
    用户明确要求（子 Agent 重新查询注册）时 gen 变化，卡片才会重新出现。"""
    with _lock:
        cur = _widgets.setdefault(sid, [])
        by_id = {w.get("id"): w for w in cur}
        for w in widgets or []:
            w = dict(w)
            wid = w.get("id")
            old = by_id.get(wid)
            w["gen"] = (old.get("gen") or 0) + 1 if old else 1
            by_id[wid] = w
        _widgets[sid] = list(by_id.values())


def clear_widgets(sid: str):
    with _lock:
        _widgets.pop(sid, None)


def reset(sid: str):
    with _lock:
        _main.pop(sid, None)
        _subs.pop(sid, None)
        _domains_used.pop(sid, None)
        _widgets.pop(sid, None)
        _last_film.pop(sid, None)


# ---------------------------------------------------------------------------
# 磁盘持久化：主会话 + 子 Agent 会话落盘到 data/sessions/v4-<sid>.json，
# 页面刷新或服务重启后按 sid 恢复，对话记录与子 Agent 六态不丢。
# ---------------------------------------------------------------------------

SESSION_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "sessions",
)


def _session_path(sid: str) -> str:
    return os.path.join(SESSION_DIR, f"v4-{sid}.json")


def _snap_dict(sid: str) -> dict:
    """锁内快照：纯内存 dict 拷贝，快且绝不做磁盘 I/O。"""
    with _lock:
        return {
            "main": list(_main.get(sid, [])),
            "subs": {d: _snap_sess(s) for d, s in _subs.get(sid, {}).items()},
            "domains_used": list(_domains_used.get(sid, [])),
            "widgets": list(_widgets.get(sid, [])),
            "last_film": bool(_last_film.get(sid)),
        }


_persist_guards = {}       # sid -> Lock：同一 sid 的写盘串行（不同 sid 并行，互不阻塞）
_persist_guards_at = {}    # sid -> 最近写盘时间（看护线程回收闲置 guard 用）
_persist_guards_guard = threading.Lock()


def persist(sid: str):
    """把会话快照原子写入磁盘（tmp + replace）。
    快照（锁内 dict 拷贝）与写盘分离：全局锁只在快照时短暂持有，磁盘 I/O 在
    sid 级写锁内进行——同一 sid 的写盘串行且快照在写锁内取，后写者的快照必然
    不旧于先写者，不会出现旧快照后写覆盖新快照的丢消息问题；不同 sid 的写盘
    完全并行，不再全体排队等一个会话落盘。"""
    os.makedirs(SESSION_DIR, exist_ok=True)
    path = _session_path(sid)
    tmp = path + ".tmp"
    with _persist_guards_guard:
        guard = _persist_guards.setdefault(sid, threading.Lock())
        _persist_guards_at[sid] = time.time()
    with guard:
        snap = _snap_dict(sid)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False)
        os.replace(tmp, path)


def trim_persist_guards(now: float, max_idle: float = 1800):
    """回收长时间未用的 sid 级写锁（看护线程定期调用）。先摘字典再等锁：
    摘走瞬间起新写盘会拿新锁，互斥不破；等锁确保摘走时没有写盘还在进行。"""
    removed = []
    with _persist_guards_guard:
        for sid in list(_persist_guards_at.keys()):
            if now - _persist_guards_at[sid] > max_idle:
                _persist_guards_at.pop(sid, None)
                g = _persist_guards.pop(sid, None)
                if g is not None:
                    removed.append(g)
    for g in removed:
        with g:
            pass


def restore(sid: str) -> bool:
    """从磁盘恢复会话（内存里已有则不动）。返回是否成功恢复。"""
    with _lock:
        if _main.get(sid) or _subs.get(sid):
            return True
    path = _session_path(sid)
    try:
        with open(path, "r", encoding="utf-8") as f:
            snap = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False
    with _lock:
        # 二次检查：并发轮次可能已建起会话，此时以内存为准，绝不用旧盘覆盖
        if _main.get(sid) or _subs.get(sid):
            return True
        _main[sid] = list(snap.get("main") or [])
        _subs[sid] = {
            d: _snap_sess(s)
            for d, s in (snap.get("subs") or {}).items()
        }
        _domains_used[sid] = list(snap.get("domains_used") or [])
        _widgets[sid] = list(snap.get("widgets") or [])
        _last_film[sid] = bool(snap.get("last_film"))
    return True
