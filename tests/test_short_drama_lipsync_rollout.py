import json
import os
import sqlite3
import unittest
import uuid
from unittest import mock

from server.content_domains import (
    short_drama_lipsync_observability as observability,
    short_drama_lipsync_rollout as rollout,
)


class LipsyncRolloutTests(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(
            os.getcwd(), "lipsync-rollout-%s.db" % uuid.uuid4().hex
        )

        def db_factory():
            conn = sqlite3.connect(self.path)
            conn.row_factory = sqlite3.Row
            return conn

        self.db = db_factory
        rollout.init_db(self.db)
        observability.init_db(self.db)

    def tearDown(self):
        if os.path.exists(self.path):
            os.unlink(self.path)

    def configure(self, **changes):
        payload = {
            "enabled": True,
            "kill_switch": False,
            "percentage": 0,
            "allow_users": [],
            "allow_projects": [],
            "deny_users": [],
            "deny_projects": [],
            "provider_policy": {"allowed": ["fal"], "weights": {"fal": 100}},
            "reason": "test rollout",
        }
        payload.update(changes)
        return rollout.set_config(self.db, "admin", payload)

    def test_default_is_fail_closed(self):
        with mock.patch.object(rollout.feature_flags, "is_enabled", return_value=True):
            decision = rollout.evaluate(
                self.db, "alice", "project-1", operation="create"
            )
        self.assertFalse(decision["eligible"])
        self.assertEqual("rollout_disabled", decision["reason"])

    def test_allowlist_and_kill_switch_priority(self):
        self.configure(allow_projects=["project-1"])
        with mock.patch.object(rollout.feature_flags, "is_enabled", return_value=True):
            allowed = rollout.evaluate(
                self.db, "alice", "project-1", operation="create"
            )
        self.assertTrue(allowed["eligible"])
        self.assertEqual("internal", allowed["cohort"])

        self.configure(kill_switch=True, allow_projects=["project-1"])
        with mock.patch.object(rollout.feature_flags, "is_enabled", return_value=True):
            paused = rollout.evaluate(
                self.db, "alice", "project-1", operation="create"
            )
            readable = rollout.evaluate(
                self.db, "alice", "project-1", operation="read"
            )
        self.assertFalse(paused["eligible"])
        self.assertEqual("kill_switch", paused["reason"])
        self.assertTrue(readable["eligible"])

    def test_percentage_is_stable_and_pinned(self):
        self.configure(percentage=100)
        with mock.patch.dict(os.environ, {"HQ_SHORT_DRAMA_ROLLOUT_SECRET": "secret"}):
            with mock.patch.object(
                rollout.feature_flags, "is_enabled", return_value=True
            ):
                first = rollout.evaluate(
                    self.db, "alice", "project-2", operation="create"
                )
                self.configure(percentage=0)
                second = rollout.evaluate(
                    self.db, "alice", "project-2", operation="create"
                )
        self.assertTrue(first["eligible"])
        self.assertTrue(second["eligible"])
        self.assertEqual("pinned_decision", second["reason"])

    def test_paused_provider_blocks_new_work(self):
        self.configure(allow_projects=["project-1"])
        rollout.set_provider_paused(
            self.db, "admin", "fal", True, "provider incident",
            incident_id="INC-1",
        )
        with mock.patch.object(rollout.feature_flags, "is_enabled", return_value=True):
            with self.assertRaises(rollout.RolloutError) as caught:
                rollout.require(
                    self.db, "alice", "project-1",
                    operation="create", provider="fal",
                )
        self.assertEqual("provider_paused", caught.exception.code)

    def test_event_redacts_credentials(self):
        observability.emit(
            self.db, "lipsync.test", actor="alice",
            detail={
                "Authorization": "Basic unsafe",
                "token": "unsafe",
                "safe": "visible",
            },
        )
        conn = self.db()
        try:
            row = conn.execute(
                "SELECT actor_hash,detail_json FROM short_drama_lipsync_events"
            ).fetchone()
        finally:
            conn.close()
        detail = json.loads(row["detail_json"])
        self.assertNotEqual("alice", row["actor_hash"])
        self.assertEqual("[REDACTED]", detail["Authorization"])
        self.assertEqual("[REDACTED]", detail["token"])
        self.assertEqual("visible", detail["safe"])


if __name__ == "__main__":
    unittest.main()
