import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "backfill_launch_experience_members.py"
SPEC = importlib.util.spec_from_file_location("membership_launch_backfill", SCRIPT)
MIGRATION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MIGRATION)


class MembershipLaunchBackfillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "users.db"
        conn = sqlite3.connect(self.db)
        conn.executescript(
            """
            CREATE TABLE users(
                id INTEGER PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                points INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                membership_tier TEXT NOT NULL DEFAULT '',
                membership_started_at INTEGER,
                membership_expires_at INTEGER
            );
            CREATE TABLE recharge_orders(
                order_id TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                status TEXT NOT NULL,
                note TEXT
            );
            CREATE TABLE membership_audit(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                before_tier TEXT NOT NULL,
                after_tier TEXT NOT NULL,
                before_expires_at INTEGER,
                after_expires_at INTEGER,
                operator TEXT NOT NULL,
                reason TEXT,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE membership_upgrade_records(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                from_level TEXT NOT NULL,
                to_level TEXT NOT NULL,
                source TEXT NOT NULL,
                source_order_id TEXT UNIQUE,
                operator TEXT,
                status TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            """
        )
        conn.executemany(
            """INSERT INTO users(
                   id,username,points,created_at,membership_tier,
                   membership_started_at,membership_expires_at
               ) VALUES(?,?,?,?,?,?,?)""",
            [
                (1, "before_plain", 11, "2026-07-20 12:00:00", "", None, None),
                (2, "before_noted", 22, "2026-07-20 12:00:00", "", None, None),
                (3, "since_cutoff", 33, "2026-07-20 16:00:00", "", None, None),
                (4, "pending_note", 44, "2026-07-20 12:00:00", "", None, None),
                (5, "existing_partner", 55, "2026-07-22 00:00:00", "partner", 1, 9999999999),
            ],
        )
        conn.executemany(
            "INSERT INTO recharge_orders(order_id,username,status,note) VALUES(?,?,?,?)",
            [
                ("approved-note", "before_noted", "approved", "线下充值"),
                ("pending-note", "pending_note", "pending", "尚未到账"),
                ("blank-note", "before_plain", "approved", "   "),
            ],
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_dry_run_selects_only_eligible_non_members_without_writing(self):
        result = MIGRATION.run(self.db, now="2026-07-25T12:00:00+08:00")
        self.assertEqual(result["matched"], 2)
        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["skipped_existing_members"], 1)
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM users WHERE membership_tier='experience'"
                ).fetchone()[0],
                0,
            )
        finally:
            conn.close()

    def test_apply_is_backed_up_idempotent_and_does_not_change_points_or_rewards(self):
        now = "2026-07-25T12:00:00+08:00"
        result = MIGRATION.run(
            self.db, now=now, apply=True, confirm=MIGRATION.CONFIRM_TEXT,
        )
        self.assertEqual(result["updated"], 2)
        self.assertTrue(Path(result["backup"]).is_file())
        conn = sqlite3.connect(self.db)
        try:
            rows = conn.execute(
                "SELECT username,points,membership_tier FROM users ORDER BY id"
            ).fetchall()
            self.assertEqual([row[1] for row in rows], [11, 22, 33, 44, 55])
            self.assertEqual(rows[1][2], "experience")
            self.assertEqual(rows[2][2], "experience")
            self.assertEqual(rows[0][2], "")
            self.assertEqual(rows[3][2], "")
            self.assertEqual(rows[4][2], "partner")
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM membership_audit").fetchone()[0], 2
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM membership_upgrade_records "
                    "WHERE source='launch_backfill'"
                ).fetchone()[0],
                2,
            )
        finally:
            conn.close()

        # 第二次预览不再把已迁移用户列为候选，也不会延长会员期限。
        rerun = MIGRATION.run(self.db, now="2026-07-26T12:00:00+08:00")
        self.assertEqual(rerun["matched"], 0)
        self.assertEqual(rerun["skipped_existing_members"], 3)

    def test_apply_requires_explicit_confirmation(self):
        with self.assertRaisesRegex(RuntimeError, "confirm"):
            MIGRATION.run(self.db, apply=True, confirm="")


if __name__ == "__main__":
    unittest.main()
