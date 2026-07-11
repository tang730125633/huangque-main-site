# -*- coding: utf-8 -*-
"""服务端主动推送的用户通知（音色回收提醒、余额告警、系统公告…）。

现有通知中心只是把「本人任务/点数历史」在前端拼成通知，服务端无法主动塞消息给某个用户。
这里补上最小后端：一张 user_notifications 表 + 写入/读取。已读态仍由前端 localStorage 管
（与现有中心一致），后端只负责内容。dedup_key 保证同一件事不重复推（如"音色X将回收"每天扫但只推一次）。
"""
import time

from .core import closing, jdb


def push(username, title, detail="", kind="system", action="", href="", dedup_key=None):
    """给用户推一条通知。带 dedup_key 时同一 (username, dedup_key) 只会存一条(幂等)。返回是否新插入。"""
    username = (username or "").strip()
    if not username or not title:
        return False
    now = int(time.time())
    with closing(jdb()) as c:
        if dedup_key:
            exists = c.execute("SELECT 1 FROM user_notifications WHERE username=? AND dedup_key=?",
                               (username, dedup_key)).fetchone()
            if exists:
                return False
        try:
            c.execute("""INSERT INTO user_notifications(username, kind, title, detail, action, href, dedup_key, created_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                (username, kind, title, detail, action or None, href or None, dedup_key, now))
            c.commit()
            return True
        except Exception:
            # 唯一索引竞态：另一个进程刚插了同 dedup_key → 视为已存在
            return False


def clear_dedup(username, dedup_key):
    """撤掉某个 dedup 记录，使同一件事以后能再次推送（如用户重新使用了音色 → 允许下次再提醒）。"""
    username = (username or "").strip()
    if not username or not dedup_key:
        return
    with closing(jdb()) as c:
        c.execute("DELETE FROM user_notifications WHERE username=? AND dedup_key=?", (username, dedup_key))
        c.commit()


def list_for(username, days=90, limit=50):
    """返回该用户的通知，新的在前。前端 buildNotices 合并这一路。"""
    username = (username or "").strip()
    if not username:
        return {"items": []}
    days = max(1, min(int(days or 90), 365))
    limit = max(1, min(int(limit or 50), 200))
    since = int(time.time()) - days * 86400
    with closing(jdb()) as c:
        rows = c.execute("""SELECT id, kind, title, detail, action, href, created_at
            FROM user_notifications WHERE username=? AND created_at>=?
            ORDER BY created_at DESC, id DESC LIMIT ?""", (username, since, limit)).fetchall()
    return {"items": [{
        "id": "notif-%d" % r["id"],
        "kind": r["kind"] or "system",
        "title": r["title"],
        "detail": r["detail"] or "",
        "action": r["action"] or "",
        "href": r["href"] or "",
        "time": int(r["created_at"] or 0),
    } for r in rows]}
