import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_membership_launch_readiness.py"
SPEC = importlib.util.spec_from_file_location("membership_readiness", SCRIPT)
READINESS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(READINESS)


class MembershipLaunchReadinessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "users.db"
        conn = sqlite3.connect(self.db)
        try:
            conn.executescript(
                """
                CREATE TABLE users(
                    id INTEGER PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL,
                    membership_tier TEXT NOT NULL DEFAULT '',
                    membership_started_at INTEGER,
                    membership_expires_at INTEGER
                );
                CREATE TABLE membership_audit(id INTEGER PRIMARY KEY);
                CREATE TABLE membership_upgrade_records(id INTEGER PRIMARY KEY);
                CREATE TABLE membership_voice_slot_entitlements(
                    username TEXT PRIMARY KEY
                );
                CREATE TABLE user_invites(
                    id INTEGER PRIMARY KEY,
                    invitee_user_id INTEGER UNIQUE
                );
                CREATE TABLE invite_reward_point_records(id INTEGER PRIMARY KEY);
                """
            )
            conn.execute(
                "INSERT INTO users VALUES(1,'member','partner',100,300)"
            )
            conn.execute(
                "INSERT INTO membership_voice_slot_entitlements VALUES('member')"
            )
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_clean_snapshot_is_ready_and_read_only(self):
        before = self.db.read_bytes()
        result = READINESS.check(self.db, now=200, enforcement="0")
        self.assertTrue(result["ready"])
        self.assertEqual(result["stats"]["active_partner"], 1)
        self.assertEqual(result["stats"]["active_members_without_voice_slot"], 0)
        self.assertEqual(self.db.read_bytes(), before)

    def test_unknown_tier_and_missing_slot_are_blockers(self):
        conn = sqlite3.connect(self.db)
        try:
            conn.execute(
                "INSERT INTO users VALUES(2,'unknown','vip',100,300)"
            )
            conn.execute(
                "INSERT INTO users VALUES(3,'slotless','experience',100,300)"
            )
            conn.commit()
        finally:
            conn.close()
        result = READINESS.check(self.db, now=200)
        self.assertFalse(result["ready"])
        self.assertEqual(result["stats"]["invalid_membership_tiers"], 1)
        self.assertEqual(result["stats"]["active_members_without_voice_slot"], 1)


if __name__ == "__main__":
    unittest.main()
