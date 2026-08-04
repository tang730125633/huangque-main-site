import http.cookiejar
import importlib
import json
import os
import pathlib
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer


PASSWORD = "secret123"
INTERNAL_TOKEN = "test-internal-token"
FIXED_NOW = 2_000_000_000


def http_client():
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
    )


def request_json(client, base, path, method="GET", payload=None, internal=False):
    headers = {}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload, ensure_ascii=False).encode()
    if internal:
        headers["X-HQ-Internal-Token"] = INTERNAL_TOKEN
    if getattr(client, "hq_token", ""):
        headers["Authorization"] = "Bearer " + client.hq_token
    request = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    with client.open(request, timeout=3) as response:
        return json.loads(response.read() or b"{}")


class AnnouncementMigrationTests(unittest.TestCase):
    def test_old_notification_schema_migrates_idempotently(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "users.db")
            with sqlite3.connect(path) as connection:
                connection.execute(
                    """CREATE TABLE user_notifications(
                           id INTEGER PRIMARY KEY AUTOINCREMENT,
                           username TEXT NOT NULL,
                           kind TEXT NOT NULL DEFAULT 'system',
                           title TEXT NOT NULL,
                           detail TEXT NOT NULL,
                           created_by TEXT NOT NULL,
                           created_at INTEGER NOT NULL
                       )"""
                )
                connection.execute(
                    """INSERT INTO user_notifications(
                           username,kind,title,detail,created_by,created_at
                       ) VALUES('alice','system','旧通知','仍需保留','admin',1)"""
                )

            old_db = os.environ.get("HQ_TEST_AUTH_DB")
            os.environ["HQ_TEST_AUTH_DB"] = path
            try:
                import server.auth_server as auth_server

                auth = importlib.reload(auth_server)
                auth.DB = path
                auth.init_db()
                auth.init_db()
                with sqlite3.connect(path) as connection:
                    notification_columns = {
                        row[1] for row in connection.execute(
                            "PRAGMA table_info(user_notifications)"
                        )
                    }
                    campaign_columns = {
                        row[1] for row in connection.execute(
                            "PRAGMA table_info(announcement_campaigns)"
                        )
                    }
                    old_row = connection.execute(
                        "SELECT title,detail FROM user_notifications WHERE username='alice'"
                    ).fetchone()
                    unique_index_sql = connection.execute(
                        """SELECT sql FROM sqlite_master
                           WHERE type='index' AND name='idx_user_notifications_campaign_user'"""
                    ).fetchone()[0]
                self.assertTrue(
                    {"campaign_id", "read_at", "popup_snoozed_until"}
                    <= notification_columns
                )
                self.assertTrue(
                    {
                        "title", "detail", "audience_json", "status", "recipient_count",
                        "created_by", "request_id", "created_at", "published_at",
                        "recalled_at", "recalled_by", "wechat_push_requested",
                        "wechat_recipient_count",
                    }
                    <= campaign_columns
                )
                self.assertEqual(old_row, ("旧通知", "仍需保留"))
                self.assertIn("UNIQUE INDEX", unique_index_sql.upper())
            finally:
                if old_db is None:
                    os.environ.pop("HQ_TEST_AUTH_DB", None)
                else:
                    os.environ["HQ_TEST_AUTH_DB"] = old_db


class AuthAnnouncementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = os.environ.get("HQ_TEST_AUTH_DB")
        os.environ["HQ_TEST_AUTH_DB"] = os.path.join(self.tmp.name, "users.db")
        import server.auth_server as auth_server

        self.auth = importlib.reload(auth_server)
        self.auth.DB = os.environ["HQ_TEST_AUTH_DB"]
        self.auth.INTERNAL_TOKEN = INTERNAL_TOKEN
        self.auth.AUTH_COOKIE_SECURE = False
        self.auth.init_db()
        for username, role in [
            ("admin", "admin"),
            ("alice", "member"),
            ("expired", "member"),
            ("experience", "member"),
            ("partner", "member"),
            ("initiator", "member"),
            ("disabled", "member"),
            ("service", "service"),
        ]:
            self.auth.create_user(username, PASSWORD, 0, role)
        with sqlite3.connect(self.auth.DB) as connection:
            connection.executemany(
                """UPDATE users
                   SET membership_tier=?,membership_expires_at=? WHERE username=?""",
                [
                    ("initiator", FIXED_NOW + 100, "admin"),
                    ("experience", FIXED_NOW - 1, "expired"),
                    ("experience", FIXED_NOW + 100, "experience"),
                    ("partner", FIXED_NOW + 100, "partner"),
                    ("initiator", FIXED_NOW + 100, "initiator"),
                    ("partner", FIXED_NOW + 100, "disabled"),
                    ("initiator", FIXED_NOW + 100, "service"),
                ],
            )
            connection.execute(
                "UPDATE users SET account_status='disabled' WHERE username='disabled'"
            )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.auth.H)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        if self.old_db is None:
            os.environ.pop("HQ_TEST_AUTH_DB", None)
        else:
            os.environ["HQ_TEST_AUTH_DB"] = self.old_db
        self.tmp.cleanup()

    def login(self, username):
        client = http_client()
        request_json(
            client, self.base, "/api/auth/login", method="POST",
            payload={"username": username, "password": PASSWORD},
        )
        return client

    def test_audience_rules_and_preview_send_consistency(self):
        preview, err = self.auth.preview_announcement({"mode": "all"}, now=FIXED_NOW)
        self.assertIsNone(err)
        self.assertEqual(preview["count"], 6)
        self.assertEqual(
            preview["breakdown"],
            {"none": 2, "experience": 1, "partner": 1, "initiator": 2},
        )

        audience = {"mode": "tiers", "tiers": ["initiator", "experience", "experience"]}
        preview, err = self.auth.preview_announcement(audience, now=FIXED_NOW)
        self.assertIsNone(err)
        sent, err = self.auth.publish_announcement(
            "会员公告", "仅发给有效体验官与发起人", audience, "request-audience", "admin",
            now=FIXED_NOW,
        )
        self.assertIsNone(err)
        self.assertEqual(sent["count"], preview["count"])
        self.assertEqual(sent["breakdown"], preview["breakdown"])
        self.assertEqual(sent["count"], 3)
        with sqlite3.connect(self.auth.DB) as connection:
            recipients = {
                row[0] for row in connection.execute(
                    "SELECT username FROM user_notifications WHERE campaign_id=?",
                    (sent["campaign"]["id"],),
                )
            }
        self.assertEqual(recipients, {"admin", "experience", "initiator"})

    def test_publish_is_idempotent_and_rejects_changed_replay(self):
        first, err = self.auth.publish_announcement(
            "全员公告", "正文", {"mode": "all"}, "request-idempotent", "admin",
            now=FIXED_NOW,
        )
        self.assertIsNone(err)
        replay, err = self.auth.publish_announcement(
            "全员公告", "正文", {"mode": "all"}, "request-idempotent", "admin",
            now=FIXED_NOW + 1,
        )
        self.assertIsNone(err)
        self.assertTrue(replay["duplicate"])
        self.assertEqual(replay["campaign"]["id"], first["campaign"]["id"])
        changed, err = self.auth.publish_announcement(
            "改过的标题", "正文", {"mode": "all"}, "request-idempotent", "admin",
            now=FIXED_NOW + 2,
        )
        self.assertIsNone(changed)
        self.assertEqual(err, "request_id_conflict")
        with sqlite3.connect(self.auth.DB) as connection:
            campaigns = connection.execute(
                "SELECT COUNT(*) FROM announcement_campaigns"
            ).fetchone()[0]
            notices = connection.execute(
                "SELECT COUNT(*) FROM user_notifications"
            ).fetchone()[0]
        self.assertEqual(campaigns, 1)
        self.assertEqual(notices, first["count"])

    def test_announcement_wechat_push_reuses_subscription_outbox(self):
        old_template = os.environ.get("WX_SUBSCRIBE_ANNOUNCEMENT_TEMPLATE_ID")
        os.environ["WX_SUBSCRIBE_ANNOUNCEMENT_TEMPLATE_ID"] = "announcement-template"
        try:
            with sqlite3.connect(self.auth.DB) as connection:
                connection.execute(
                    """INSERT INTO wechat_subscription_grants(
                           username,event_type,template_id,openid,remaining,last_choice,updated_at)
                       VALUES('alice','announcement','announcement-template','openid-a',1,'accept',?)""",
                    (FIXED_NOW,),
                )
            preview, err = self.auth.preview_announcement({"mode": "all"}, now=FIXED_NOW)
            self.assertIsNone(err)
            self.assertTrue(preview["wechat_push_configured"])
            self.assertEqual(preview["wechat_subscriber_count"], 1)
            sent, err = self.auth.publish_announcement(
                "平台公告", "请在消息中心查看详情", {"mode": "all"},
                "request-wechat", "admin", now=FIXED_NOW, wechat_push=True,
            )
            self.assertIsNone(err)
            self.assertEqual(sent["wechat_recipient_count"], 1)
            with sqlite3.connect(self.auth.DB) as connection:
                outbox = connection.execute(
                    """SELECT username,event_type,status,kind FROM wechat_subscription_outbox
                       WHERE business_id=?""",
                    ("announcement:%d" % sent["campaign"]["id"],),
                ).fetchone()
            self.assertEqual(outbox, ("alice", "announcement", "pending", "announcement"))
            recalled, err = self.auth.recall_announcement(sent["campaign"]["id"], "admin", now=FIXED_NOW + 1)
            self.assertIsNone(err)
            self.assertEqual(recalled["campaign"]["status"], "recalled")
            with sqlite3.connect(self.auth.DB) as connection:
                status = connection.execute(
                    "SELECT status FROM wechat_subscription_outbox WHERE business_id=?",
                    ("announcement:%d" % sent["campaign"]["id"],),
                ).fetchone()[0]
            self.assertEqual(status, "dropped")
        finally:
            if old_template is None:
                os.environ.pop("WX_SUBSCRIBE_ANNOUNCEMENT_TEMPLATE_ID", None)
            else:
                os.environ["WX_SUBSCRIBE_ANNOUNCEMENT_TEMPLATE_ID"] = old_template

    def test_publish_validation_is_explicit(self):
        for title, detail, audience, request_id, expected in [
            ("x" * 81, "正文", {"mode": "all"}, "long-title", "title_too_long"),
            ("标题", "x" * 1001, {"mode": "all"}, "long-detail", "detail_too_long"),
            ("标题", "正文", {"mode": "tiers", "tiers": []}, "no-tier", "missing_tiers"),
            (
                "标题", "正文", {"mode": "tiers", "tiers": ["unknown"]},
                "bad-tier", "invalid_tier",
            ),
            ("标题", "正文", {"mode": "all"}, "", "missing_request_id"),
        ]:
            with self.subTest(expected=expected):
                result, err = self.auth.publish_announcement(
                    title, detail, audience, request_id, "admin", now=FIXED_NOW,
                )
                self.assertIsNone(result)
                self.assertEqual(err, expected)
        result, err = self.auth.publish_announcement(
            "标题", "正文", {"mode": "all"}, "bad-wechat-push", "admin",
            now=FIXED_NOW, wechat_push="false",
        )
        self.assertIsNone(result)
        self.assertEqual(err, "invalid_wechat_push")

    def test_auth_gates_user_state_and_recall_visibility(self):
        admin = self.login("admin")
        alice = self.login("alice")
        partner = self.login("partner")

        with self.assertRaises(urllib.error.HTTPError) as no_internal:
            request_json(
                admin, self.base, "/api/auth/admin/announcements/preview",
                method="POST", payload={"audience": {"mode": "all"}},
            )
        self.assertEqual(no_internal.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as not_admin:
            request_json(
                alice, self.base, "/api/auth/admin/announcements/preview",
                method="POST", payload={"audience": {"mode": "all"}}, internal=True,
            )
        self.assertEqual(not_admin.exception.code, 403)

        sent = request_json(
            admin, self.base, "/api/auth/admin/announcements", method="POST", internal=True,
            payload={
                "title": "当天弹窗", "detail": "公告正文", "audience": {"mode": "all"},
                "request_id": "request-user-state",
            },
        )
        campaign_id = sent["campaign"]["id"]
        self.assertEqual(sent["campaign"]["status"], "published")
        self.assertEqual(sent["count"], 6)
        admin_items = request_json(admin, self.base, "/api/auth/notifications")["items"]
        self.assertIn(campaign_id, {item["campaign_id"] for item in admin_items})
        listing = request_json(
            admin, self.base, "/api/auth/admin/announcements", internal=True,
        )
        self.assertEqual(listing["items"][0]["id"], campaign_id)

        alice_items = request_json(alice, self.base, "/api/auth/notifications")["items"]
        notice = alice_items[0]
        self.assertTrue(
            {
                "id", "campaign_id", "kind", "read_at", "popup_snoozed_until",
                "popup_until",
            }
            <= set(notice)
        )
        self.assertEqual(notice["campaign_id"], campaign_id)
        self.assertEqual(notice["kind"], "announcement")
        self.assertEqual(
            notice["popup_until"], self.auth._shanghai_day_end(sent["campaign"]["published_at"]),
        )

        with self.assertRaises(urllib.error.HTTPError) as other_user:
            request_json(
                partner, self.base, "/api/auth/notifications/%d/read" % notice["id"],
                method="POST", payload={},
            )
        self.assertEqual(other_user.exception.code, 404)
        read = request_json(
            alice, self.base, "/api/auth/notifications/%d/read" % notice["id"],
            method="POST", payload={},
        )["notification"]
        self.assertGreater(read["read_at"], 0)
        before_snooze = int(time.time())
        snoozed = request_json(
            alice, self.base, "/api/auth/notifications/%d/snooze-today" % notice["id"],
            method="POST", payload={},
        )["notification"]
        self.assertEqual(
            snoozed["popup_snoozed_until"], self.auth._shanghai_next_midnight(before_snooze),
        )

        self.auth.create_user_notification("alice", "单独通知", "也要标记已读", "admin")
        read_all = request_json(
            alice, self.base, "/api/auth/notifications/read-all", method="POST", payload={},
        )
        self.assertEqual(read_all["updated_count"], 1)
        self.assertTrue(all(
            item["read_at"] > 0
            for item in request_json(alice, self.base, "/api/auth/notifications")["items"]
        ))

        recalled = request_json(
            admin, self.base, "/api/auth/admin/announcements/%d/recall" % campaign_id,
            method="POST", payload={}, internal=True,
        )
        self.assertEqual(recalled["campaign"]["status"], "recalled")
        visible = request_json(alice, self.base, "/api/auth/notifications")["items"]
        self.assertNotIn(campaign_id, {item["campaign_id"] for item in visible})
        with sqlite3.connect(self.auth.DB) as connection:
            remaining = connection.execute(
                "SELECT COUNT(*) FROM user_notifications WHERE campaign_id=?", (campaign_id,),
            ).fetchone()[0]
            campaign = connection.execute(
                "SELECT status,recipient_count FROM announcement_campaigns WHERE id=?",
                (campaign_id,),
            ).fetchone()
            old_code_visible = connection.execute(
                "SELECT COUNT(*) FROM user_notifications WHERE username='alice' AND campaign_id=?",
                (campaign_id,),
            ).fetchone()[0]
        self.assertEqual(remaining, 0)
        self.assertEqual(campaign, ("recalled", sent["count"]))
        self.assertEqual(old_code_visible, 0)


class AdminAnnouncementProxyAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_auth_db = os.environ.get("HQ_TEST_AUTH_DB")
        self.old_no_proxy = os.environ.get("NO_PROXY")
        os.environ["NO_PROXY"] = "127.0.0.1,localhost"
        os.environ["HQ_TEST_AUTH_DB"] = os.path.join(self.tmp.name, "users.db")
        import server.auth_server as auth_server

        self.auth = importlib.reload(auth_server)
        self.auth.DB = os.environ["HQ_TEST_AUTH_DB"]
        self.auth.INTERNAL_TOKEN = INTERNAL_TOKEN
        self.auth.AUTH_COOKIE_SECURE = False
        self.auth.init_db()
        self.auth.create_user("admin", PASSWORD, 0, "admin")
        self.auth.create_user("member", PASSWORD, 0, "member")
        self.auth_server = ThreadingHTTPServer(("127.0.0.1", 0), self.auth.H)
        self.auth_thread = threading.Thread(target=self.auth_server.serve_forever, daemon=True)
        self.auth_thread.start()
        self.auth_base = "http://127.0.0.1:%d" % self.auth_server.server_address[1]

        import server.admin_api as admin_api

        self.admin = admin_api
        self.old_admin_values = (
            admin_api.AUTH_BASE, admin_api.AUTH_INTERNAL_TOKEN, admin_api.ADMIN_DB,
        )
        admin_api.AUTH_BASE = self.auth_base
        admin_api.AUTH_INTERNAL_TOKEN = INTERNAL_TOKEN
        admin_api.ADMIN_DB = pathlib.Path(self.tmp.name) / "admin.db"
        with sqlite3.connect(admin_api.ADMIN_DB) as connection:
            connection.execute(
                """CREATE TABLE admin_audit(
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       actor TEXT NOT NULL,action TEXT NOT NULL,target TEXT NOT NULL,
                       detail TEXT NOT NULL,created_at INTEGER NOT NULL
                   )"""
            )
        self.admin_server = ThreadingHTTPServer(("127.0.0.1", 0), admin_api.H)
        self.admin_thread = threading.Thread(target=self.admin_server.serve_forever, daemon=True)
        self.admin_thread.start()
        self.admin_base = "http://127.0.0.1:%d" % self.admin_server.server_address[1]

    def tearDown(self):
        self.admin_server.shutdown()
        self.admin_server.server_close()
        self.admin_thread.join(timeout=3)
        self.auth_server.shutdown()
        self.auth_server.server_close()
        self.auth_thread.join(timeout=3)
        (
            self.admin.AUTH_BASE, self.admin.AUTH_INTERNAL_TOKEN, self.admin.ADMIN_DB,
        ) = self.old_admin_values
        if self.old_auth_db is None:
            os.environ.pop("HQ_TEST_AUTH_DB", None)
        else:
            os.environ["HQ_TEST_AUTH_DB"] = self.old_auth_db
        if self.old_no_proxy is None:
            os.environ.pop("NO_PROXY", None)
        else:
            os.environ["NO_PROXY"] = self.old_no_proxy
        self.tmp.cleanup()

    def login(self, username):
        client = http_client()
        login = request_json(
            client, self.auth_base, "/api/auth/miniprogram-login", method="POST",
            payload={"username": username, "password": PASSWORD},
        )
        client.hq_token = login["token"]
        return client

    def test_proxy_endpoints_and_body_free_write_audit(self):
        member = self.login("member")
        self.assertEqual(
            request_json(member, self.auth_base, "/api/auth/me")["user"]["username"],
            "member",
        )
        self.assertEqual(self.admin.verify(member.hq_token)["username"], "member")
        with self.assertRaises(urllib.error.HTTPError) as forbidden:
            request_json(
                member, self.admin_base, "/api/admin/announcements/preview",
                method="POST", payload={"audience": {"mode": "all"}},
            )
        self.assertEqual(forbidden.exception.code, 403)

        admin = self.login("admin")
        preview = request_json(
            admin, self.admin_base, "/api/admin/announcements/preview",
            method="POST", payload={"audience": {"mode": "all"}},
        )
        self.assertEqual(preview["count"], 2)
        title = "不可进入审计的标题"
        detail = "不可进入审计的公告正文"
        sent = request_json(
            admin, self.admin_base, "/api/admin/announcements", method="POST",
            payload={
                "title": title, "detail": detail, "audience": {"mode": "all"},
                "request_id": "request-proxy-audit",
            },
        )
        campaign_id = sent["campaign"]["id"]
        listing = request_json(admin, self.admin_base, "/api/admin/announcements")
        self.assertEqual(listing["items"][0]["id"], campaign_id)
        recalled = request_json(
            admin, self.admin_base, "/api/admin/announcements/%d/recall" % campaign_id,
            method="POST", payload={},
        )
        self.assertEqual(recalled["campaign"]["status"], "recalled")

        with sqlite3.connect(self.admin.ADMIN_DB) as connection:
            rows = connection.execute(
                "SELECT action,detail FROM admin_audit ORDER BY id"
            ).fetchall()
        self.assertEqual([row[0] for row in rows], [
            "announcement_publish", "announcement_recall",
        ])
        audit_text = "\n".join(row[1] for row in rows)
        self.assertNotIn(title, audit_text)
        self.assertNotIn(detail, audit_text)
        publish_detail = json.loads(rows[0][1])
        self.assertEqual(
            set(publish_detail), {
                "request_id", "audience", "recipient_count",
                "wechat_push_requested", "wechat_recipient_count",
            },
        )


if __name__ == "__main__":
    unittest.main()
