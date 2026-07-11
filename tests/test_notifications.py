# -*- coding: utf-8 -*-
"""服务端主动推送的用户通知（音色回收提醒、系统公告…）。

现有通知中心只反映本人任务/点数历史，服务端无法主动塞消息。本模块补最小后端：
push(带 dedup 幂等) / clear_dedup(用户用过就撤，可再提醒) / list_for。
"""
import importlib
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
notif = importlib.import_module("content_domains.notifications")


class _TempJobsDB:
    def __init__(self):
        self.path = tempfile.mktemp(suffix=".db")
        c = sqlite3.connect(self.path)
        c.execute("""CREATE TABLE user_notifications(id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL, kind TEXT, title TEXT, detail TEXT, action TEXT, href TEXT,
            dedup_key TEXT, created_at INTEGER)""")
        c.execute("""CREATE UNIQUE INDEX idx ON user_notifications(username, dedup_key) WHERE dedup_key IS NOT NULL""")
        c.commit(); c.close()

    def conn(self):
        c = sqlite3.connect(self.path); c.row_factory = sqlite3.Row; return c


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.db = _TempJobsDB()
        self.p = patch.object(notif, "jdb", self.db.conn); self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_push_and_list(self):
        self.assertTrue(notif.push("u", "标题", "详情", kind="voice", action="去配音", href="audio.html"))
        items = notif.list_for("u")["items"]
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual(it["title"], "标题")
        self.assertEqual(it["kind"], "voice")
        self.assertEqual(it["action"], "去配音")
        self.assertTrue(it["id"].startswith("notif-"))

    def test_dedup_prevents_duplicate(self):
        self.assertTrue(notif.push("u", "音色即将回收", dedup_key="cosy-idle-vip_x"))
        self.assertFalse(notif.push("u", "音色即将回收", dedup_key="cosy-idle-vip_x"))  # 第二次不插
        self.assertEqual(len(notif.list_for("u")["items"]), 1)

    def test_dedup_is_per_user(self):
        notif.push("a", "x", dedup_key="k")
        self.assertTrue(notif.push("b", "x", dedup_key="k"))   # 不同用户互不影响
        self.assertEqual(len(notif.list_for("a")["items"]), 1)
        self.assertEqual(len(notif.list_for("b")["items"]), 1)

    def test_clear_dedup_allows_repush(self):
        notif.push("u", "x", dedup_key="k")
        notif.clear_dedup("u", "k")
        self.assertTrue(notif.push("u", "x", dedup_key="k"))   # 撤销后可再推

    def test_empty_username_or_title_ignored(self):
        self.assertFalse(notif.push("", "t"))
        self.assertFalse(notif.push("u", ""))
        self.assertEqual(notif.list_for("")["items"], [])

    def test_list_only_returns_own(self):
        notif.push("a", "属于a")
        notif.push("b", "属于b")
        items = notif.list_for("a")["items"]
        self.assertEqual([x["title"] for x in items], ["属于a"])

    def test_list_newest_first(self):
        base = int(time.time())
        with patch.object(notif.time, "time", lambda: base - 3600):
            notif.push("u", "旧")
        with patch.object(notif.time, "time", lambda: base):
            notif.push("u", "新")
        titles = [x["title"] for x in notif.list_for("u")["items"]]
        self.assertEqual(titles, ["新", "旧"])


if __name__ == "__main__":
    unittest.main()
