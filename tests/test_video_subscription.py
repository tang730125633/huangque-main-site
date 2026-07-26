import importlib
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch


class VideoSubscriptionTests(unittest.TestCase):
    def test_done_and_video_outbox_share_one_transaction(self):
        from server.content_domains import jobs_store

        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "jobs.db")

            def jdb():
                conn = sqlite3.connect(path)
                conn.row_factory = sqlite3.Row
                return conn

            with closing(jdb()) as conn:
                conn.execute("""CREATE TABLE jobs(
                    id INTEGER PRIMARY KEY, username TEXT, status TEXT,
                    result TEXT, error TEXT, updated_at INTEGER)""")
                conn.execute("INSERT INTO jobs VALUES(1,'alice','running',NULL,NULL,1)")
                conn.commit()
            jobs_store.ensure_video_notification_outbox(jdb)
            self.assertTrue(jobs_store.set_done_with_video_outbox(
                jdb, 1, "alice", "video", {"url": "/video.mp4"},
            ))
            with closing(jdb()) as conn:
                self.assertEqual(conn.execute("SELECT status FROM jobs WHERE id=1").fetchone()[0], "done")
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM video_notification_outbox WHERE job_id=1").fetchone()[0], 1)
            self.assertFalse(jobs_store.set_done_with_video_outbox(jdb, 1, "alice", "video", {"url": "/again.mp4"}))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_template = os.environ.get("WX_SUBSCRIBE_WORK_COMPLETE_TEMPLATE_ID")
        os.environ["WX_SUBSCRIBE_WORK_COMPLETE_TEMPLATE_ID"] = "template-test"
        import server.auth_server as auth
        self.auth = importlib.reload(auth)
        self.auth.DB = os.path.join(self.tmp.name, "users.db")
        self.auth.init_db()
        self.auth.create_user("alice", "secret123", 0)

    def tearDown(self):
        if self.old_template is None:
            os.environ.pop("WX_SUBSCRIBE_WORK_COMPLETE_TEMPLATE_ID", None)
        else:
            os.environ["WX_SUBSCRIBE_WORK_COMPLETE_TEMPLATE_ID"] = self.old_template
        self.tmp.cleanup()

    def test_internal_event_is_idempotent_and_no_grant_drops_without_affecting_job(self):
        first = self.auth.enqueue_video_subscription("alice", 7, "video")
        second = self.auth.enqueue_video_subscription("alice", 7, "video")
        self.assertEqual(first["status"], "accepted")
        self.assertEqual(second["status"], "duplicate")
        self.assertIsNone(self.auth._claim_video_subscription())
        with closing(self.auth.db()) as conn:
            row = conn.execute("SELECT status FROM wechat_subscription_outbox WHERE business_id='job:7'").fetchone()
        self.assertEqual(row[0], "dropped")

    def test_transient_send_failure_is_retryable(self):
        with patch.object(self.auth.wechat_vpay, "code_to_session", return_value={"openid": "openid-a"}):
            result, err = self.auth.record_subscription_choices(
                "alice", {"work_complete": "accept"}, "wx-login-code",
            )
        self.assertIsNone(err)
        work = next(item for item in result["events"] if item["event_type"] == "work_complete")
        self.assertEqual(work["remaining"], 1)
        self.auth.enqueue_video_subscription("alice", 8, "video")
        row = self.auth._claim_video_subscription()
        self.assertIsNotNone(row)
        self.auth._finish_video_subscription(row, "failed", "network", restore_grant=True)
        with closing(self.auth.db()) as conn:
            conn.execute("UPDATE wechat_subscription_outbox SET next_retry_at=0 WHERE id=?", (row["id"],))
            conn.commit()
        retry = self.auth._claim_video_subscription()
        self.assertIsNotNone(retry)
        self.auth._finish_video_subscription(retry, "sent")
        with closing(self.auth.db()) as conn:
            status = conn.execute("SELECT status FROM wechat_subscription_outbox WHERE id=?", (row["id"],)).fetchone()[0]
        self.assertEqual(status, "sent")

    def test_same_wechat_can_subscribe_for_two_test_accounts(self):
        self.auth.create_user("bob", "secret123", 0)
        with patch.object(self.auth.wechat_vpay, "code_to_session", return_value={"openid": "same-openid"}):
            for username in ("alice", "bob"):
                result, err = self.auth.record_subscription_choices(
                    username, {"work_complete": "accept"}, "wx-login-code",
                )
                self.assertIsNone(err)
                self.assertEqual(result["events"][0]["remaining"], 1)
        with closing(self.auth.db()) as conn:
            rows = conn.execute(
                "SELECT username,openid FROM wechat_subscription_grants ORDER BY username",
            ).fetchall()
        self.assertEqual([(row[0], row[1]) for row in rows], [
            ("alice", "same-openid"), ("bob", "same-openid"),
        ])

    def test_expired_sender_lease_restores_grant_before_retry(self):
        with patch.object(self.auth.wechat_vpay, "code_to_session", return_value={"openid": "openid-a"}):
            result, err = self.auth.record_subscription_choices(
                "alice", {"work_complete": "accept"}, "wx-login-code",
            )
        self.assertIsNone(err)
        self.assertIsInstance(result["events"], list)
        self.auth.enqueue_video_subscription("alice", 9, "video")
        first = self.auth._claim_video_subscription()
        self.assertIsNotNone(first)
        with closing(self.auth.db()) as conn:
            conn.execute(
                "UPDATE wechat_subscription_outbox SET lease_until=0 WHERE id=?",
                (first["id"],),
            )
            conn.commit()
        retry = self.auth._claim_video_subscription()
        self.assertEqual(retry["id"], first["id"])
        self.auth._finish_video_subscription(retry, "sent")
        with closing(self.auth.db()) as conn:
            remaining = conn.execute(
                "SELECT remaining FROM wechat_subscription_grants WHERE username='alice'",
            ).fetchone()[0]
        self.assertEqual(remaining, 0)


if __name__ == "__main__":
    unittest.main()
