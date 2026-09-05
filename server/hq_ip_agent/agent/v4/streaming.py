"""SSE 实时推送的进程内发布/订阅：前端挂一条长连接收「turn 完成」与「状态快照」事件，
替代 2 秒轮询（轮询接口保留作降级）。有订阅者才入队，无订阅直接丢弃。

从 app.py 抽出：订阅表与路由解耦，交付/轮次模块直接 emit，不再依赖 Flask 层。
"""
from __future__ import annotations

import collections
import json
import threading

_SUBS_GUARD = threading.Lock()
_SUBS = {}   # sid -> {"cond": Condition, "q": deque, "refs": 订阅数}


def subscribe(sid: str) -> dict:
    with _SUBS_GUARD:
        sub = _SUBS.get(sid)
        if sub is None:
            sub = {"cond": threading.Condition(), "q": collections.deque(), "refs": 0}
            _SUBS[sid] = sub
        sub["refs"] += 1
        return sub


def unsubscribe(sid: str, sub: dict):
    with _SUBS_GUARD:
        sub["refs"] -= 1
        if sub["refs"] <= 0:
            _SUBS.pop(sid, None)


def emit(sid: str, event: str, data: dict):
    """把事件推给该 sid 的 SSE 订阅者；无订阅者时静默丢弃。"""
    with _SUBS_GUARD:
        sub = _SUBS.get(sid)
    if not sub:
        return
    with sub["cond"]:
        sub["q"].append((event, data))
        if len(sub["q"]) > 100:  # 慢客户端兜底：丢弃最旧事件，防内存膨胀
            sub["q"].popleft()
        sub["cond"].notify_all()


def sse_event(event: str, data: dict) -> str:
    return "event: %s\ndata: %s\n\n" % (event, json.dumps(data, ensure_ascii=False))
