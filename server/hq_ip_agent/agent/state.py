"""内部信息表（用户不可见）、报告状态的会话级存储。

纯内存、进程内；演示版够用。真正的采集内容由主 Agent 通过
update_profile / get_profile 两个工具读写，代码不做字段校验。
报告状态由报告生成工具写入，供前端轮询与下载链接展示。
"""
import json
import os
import threading
from collections import defaultdict

_lock = threading.Lock()
_profiles = defaultdict(dict)      # session_id -> {规范字段键: 值}
_reports = defaultdict(dict)       # session_id -> 报告元信息


def get_profile(session_id: str) -> dict:
    with _lock:
        return dict(_profiles.get(session_id, {}))


def update_profile(session_id: str, facts: dict) -> dict:
    with _lock:
        p = _profiles[session_id]
        for k, v in (facts or {}).items():
            p[k] = v
        return dict(p)


def get_report(session_id: str) -> dict:
    """报告元信息（含 status/file/rounds/gaps/chosen 等，不含报告全文）。"""
    with _lock:
        meta = dict(_reports.get(session_id, {}))
        for key in ("_json", "_m5_json", "_m6_json"):
            meta.pop(key, None)  # 全文只留在服务端，不外发
        return meta


def get_report_full(session_id: str) -> dict:
    """服务端内部用：含报告全文与模块5/6全文。"""
    with _lock:
        return dict(_reports.get(session_id, {}))


def get_report_json(session_id: str):
    """服务端内部取当前报告全文（生成/定稿循环用）。"""
    with _lock:
        return _reports.get(session_id, {}).get("_json")


def set_report(session_id: str, meta: dict) -> dict:
    with _lock:
        _reports[session_id].update(meta or {})
        return dict(_reports[session_id])


def reset(session_id: str):
    with _lock:
        _profiles.pop(session_id, None)
        _reports.pop(session_id, None)


# ---------------------------------------------------------------------------
# 磁盘持久化：会话数据（信息表/报告状态）落盘到 data/sessions/，
# 页面刷新或服务重启后可按 sid 恢复，对话记录不再丢。
# ---------------------------------------------------------------------------

SESSION_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "sessions"
)


def _session_path(session_id: str) -> str:
    return os.path.join(SESSION_DIR, f"{session_id}.json")


def persist(session_id: str):
    """把会话快照原子写入磁盘（tmp + replace）。
    快照与写盘在同一把锁内：并发轮次各自 persist，最终盘上一定是最新完整快照，
    不会出现旧快照后写覆盖新快照的丢消息问题。"""
    os.makedirs(SESSION_DIR, exist_ok=True)
    path = _session_path(session_id)
    tmp = path + ".tmp"
    with _lock:
        snap = {
            "profile": dict(_profiles.get(session_id, {})),
            "report": dict(_reports.get(session_id, {})),
        }
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False)
        os.replace(tmp, path)


def restore(session_id: str) -> bool:
    """从磁盘恢复会话（内存里已有则不动）。返回是否成功恢复。"""
    with _lock:
        if _profiles.get(session_id) or _reports.get(session_id):
            return True
    path = _session_path(session_id)
    try:
        with open(path, "r", encoding="utf-8") as f:
            snap = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False
    with _lock:
        # 二次检查：并发轮次可能已建起会话，此时以内存为准，绝不用旧盘覆盖
        if _profiles.get(session_id) or _reports.get(session_id):
            return True
        _profiles[session_id] = dict(snap.get("profile") or {})
        _reports[session_id] = dict(snap.get("report") or {})
    return True
