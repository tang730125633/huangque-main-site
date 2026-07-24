import csv
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
            CREATE TABLE membership_voice_slot_entitlements(
                username TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                source_order_id TEXT,
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

    def write_manifest(self, approved):
        path = Path(self.tmp.name) / "approved.csv"
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(
                stream, fieldnames=("username", "created_at", "reason", "approved")
            )
            writer.writeheader()
            for username in approved:
                writer.writerow({
                    "username": username,
                    "created_at": "2026-07-20 12:00:00",
                    "reason": "manual_confirmed",
                    "approved": "yes",
                })
        return path

    def test_discovery_outputs_auditable_candidates_without_writing(self):
        output = Path(self.tmp.name) / "candidates.csv"
        result = MIGRATION.run(
            self.db, now="2026-07-25T12:00:00+08:00", discovery_out=output,
        )
        self.assertEqual(result["matched"], 2)
        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["skipped_existing_members"], 1)
        text = output.read_text(encoding="utf-8-sig")
        self.assertIn("username,created_at,reason,approved", text)
        self.assertIn("before_noted", text)
        self.assertIn("approved_recharge_with_note", text)
        self.assertIn("since_cutoff", text)
        self.assertIn("registered_since_2026-07-21", text)
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

    def test_apply_uses_only_explicit_manifest_and_grants_slot_entitlement(self):
        manifest = self.write_manifest(["before_noted"])
        digest = MIGRATION.manifest_sha256(manifest)
        result = MIGRATION.run(
            self.db,
            now="2026-07-25T12:00:00+08:00",
            apply=True,
            confirm=MIGRATION.CONFIRM_TEXT,
            manifest=manifest,
            expected_manifest_sha256=digest,
        )
        self.assertEqual(result["updated"], 1)
        self.assertTrue(Path(result["backup"]).is_file())
        conn = sqlite3.connect(self.db)
        try:
            rows = conn.execute(
                "SELECT username,points,membership_tier FROM users ORDER BY id"
            ).fetchall()
            self.assertEqual([row[1] for row in rows], [11, 22, 33, 44, 55])
            self.assertEqual(rows[1][2], "experience")
            self.assertEqual(rows[2][2], "", "名单外候选用户不得升级")
            self.assertEqual(rows[0][2], "")
            self.assertEqual(rows[3][2], "")
            self.assertEqual(rows[4][2], "partner")
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM membership_audit").fetchone()[0], 1
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM membership_upgrade_records "
                    "WHERE source='launch_backfill'"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT username,source FROM membership_voice_slot_entitlements"
                ).fetchall(),
                [("before_noted", "launch_backfill")],
            )
        finally:
            conn.close()

        rerun = MIGRATION.run(
            self.db,
            now="2026-07-26T12:00:00+08:00",
            manifest=manifest,
        )
        self.assertEqual(rerun["matched"], 0)
        self.assertEqual(rerun["skipped_existing_members"], 1)

    def test_apply_requires_manifest_hash_and_explicit_confirmation(self):
        manifest = self.write_manifest(["before_noted"])
        digest = MIGRATION.manifest_sha256(manifest)
        with self.assertRaisesRegex(RuntimeError, "manifest"):
            MIGRATION.run(
                self.db, apply=True, confirm=MIGRATION.CONFIRM_TEXT,
            )
        with self.assertRaisesRegex(RuntimeError, "confirm"):
            MIGRATION.run(
                self.db, apply=True, confirm="",
                manifest=manifest, expected_manifest_sha256=digest,
            )
        with self.assertRaisesRegex(RuntimeError, "SHA256"):
            MIGRATION.run(
                self.db, apply=True, confirm=MIGRATION.CONFIRM_TEXT,
                manifest=manifest, expected_manifest_sha256="0" * 64,
            )


if __name__ == "__main__":
    unittest.main()
