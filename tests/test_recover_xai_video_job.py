import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "recover_xai_video_job.py"
spec = importlib.util.spec_from_file_location("recover_xai_video_job", SCRIPT)
recover = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recover)


class RecoverXaiVideoJobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.job_db = str(root / "jobs.db")
        self.asset_db = str(root / "assets.db")
        with sqlite3.connect(self.job_db) as db:
            db.execute(
                """CREATE TABLE jobs(
                    id INTEGER PRIMARY KEY, kind TEXT, status TEXT, refunded INTEGER,
                    cost INTEGER, error TEXT, updated_at INTEGER)"""
            )
            db.executemany(
                "INSERT INTO jobs VALUES(?,?,?,?,?,?,?)",
                [
                    (7, "xiaole_video", "error", 1, 300, "HTTP 503", 0),
                    (8, "xiaole_video", "error", 0, 300, "HTTP 503", 0),
                    (9, "xiaole_video", "error", 1, 300, "HTTP 503", 0),
                ],
            )
        with sqlite3.connect(self.asset_db) as db:
            db.execute(
                """CREATE TABLE video_assets(
                    job_id INTEGER PRIMARY KEY, provider_video_id TEXT, model TEXT,
                    status TEXT, phase TEXT, error TEXT, updated_at INTEGER)"""
            )
            db.executemany(
                "INSERT INTO video_assets VALUES(?,?,?,?,?,?,?)",
                [
                    (7, "rid-7", "grok-imagine-video", "failed", "failed", "HTTP 503", 0),
                    (8, "rid-8", "grok-imagine-video", "failed", "failed", "HTTP 503", 0),
                    (9, "   ", "grok-imagine-video", "failed", "failed", "HTTP 503", 0),
                ],
            )

    def tearDown(self):
        self.tmp.cleanup()

    def job_row(self, job_id):
        with sqlite3.connect(self.job_db) as db:
            return db.execute(
                "SELECT status,refunded,cost,error,updated_at FROM jobs WHERE id=?",
                (job_id,),
            ).fetchone()

    def asset_row(self, job_id):
        with sqlite3.connect(self.asset_db) as db:
            return db.execute(
                "SELECT status,phase,provider_video_id,error,updated_at "
                "FROM video_assets WHERE job_id=?",
                (job_id,),
            ).fetchone()

    def test_dry_run_does_not_change_databases(self):
        before_job = self.job_row(7)
        before_asset = self.asset_row(7)

        result = recover.recover_job(
            7, apply=False, job_db=self.job_db, asset_db=self.asset_db
        )

        self.assertEqual(
            result,
            {
                "job_id": 7,
                "request_id": "rid-7",
                "model": "grok-imagine-video",
                "apply": False,
            },
        )
        self.assertEqual(self.job_row(7), before_job)
        self.assertEqual(self.asset_row(7), before_asset)

    def test_dry_run_rejects_missing_job_db_without_creating_it(self):
        missing = Path(self.job_db).with_name("missing jobs.db")

        try:
            with self.assertRaisesRegex(FileNotFoundError, "任务数据库不存在"):
                recover.recover_job(
                    7, apply=False, job_db=str(missing), asset_db=self.asset_db
                )
        finally:
            self.assertFalse(missing.exists())

    def test_dry_run_rejects_missing_asset_db_without_creating_it(self):
        missing = Path(self.asset_db).with_name("missing assets.db")

        try:
            with self.assertRaisesRegex(FileNotFoundError, "视频资产数据库不存在"):
                recover.recover_job(
                    7, apply=False, job_db=self.job_db, asset_db=str(missing)
                )
        finally:
            self.assertFalse(missing.exists())

    def test_apply_requeues_without_changing_cost_or_refunded(self):
        result = recover.recover_job(
            7, apply=True, job_db=self.job_db, asset_db=self.asset_db
        )

        self.assertTrue(result["apply"])
        self.assertEqual(self.job_row(7)[:3], ("pending", 1, 300))
        self.assertIsNone(self.job_row(7)[3])
        self.assertEqual(self.asset_row(7)[:3], ("running", "xai_pending", "rid-7"))
        self.assertIsNone(self.asset_row(7)[3])

    def test_rejects_job_without_refund(self):
        before_job = self.job_row(8)
        before_asset = self.asset_row(8)

        with self.assertRaisesRegex(ValueError, "不满足补偿恢复条件"):
            recover.recover_job(8, apply=True, job_db=self.job_db, asset_db=self.asset_db)

        self.assertEqual(self.job_row(8), before_job)
        self.assertEqual(self.asset_row(8), before_asset)

    def test_rejects_blank_request_id(self):
        before_job = self.job_row(9)
        before_asset = self.asset_row(9)

        with self.assertRaisesRegex(ValueError, "不满足补偿恢复条件"):
            recover.recover_job(9, apply=True, job_db=self.job_db, asset_db=self.asset_db)

        self.assertEqual(self.job_row(9), before_job)
        self.assertEqual(self.asset_row(9), before_asset)

    def test_second_apply_is_rejected_without_extra_changes(self):
        recover.recover_job(7, apply=True, job_db=self.job_db, asset_db=self.asset_db)
        after_first_job = self.job_row(7)
        after_first_asset = self.asset_row(7)

        with self.assertRaisesRegex(ValueError, "不满足补偿恢复条件"):
            recover.recover_job(7, apply=True, job_db=self.job_db, asset_db=self.asset_db)

        self.assertEqual(self.job_row(7), after_first_job)
        self.assertEqual(self.asset_row(7), after_first_asset)

    def test_asset_cas_failure_rolls_back_job_update(self):
        with sqlite3.connect(self.asset_db) as db:
            db.execute(
                """CREATE TRIGGER ignore_recovery_update
                   BEFORE UPDATE ON video_assets
                   WHEN NEW.status='running' AND NEW.phase='xai_pending'
                   BEGIN
                     SELECT RAISE(IGNORE);
                   END"""
            )
        before_job = self.job_row(7)
        before_asset = self.asset_row(7)

        with self.assertRaisesRegex(RuntimeError, "资产状态已变化"):
            recover.recover_job(7, apply=True, job_db=self.job_db, asset_db=self.asset_db)

        self.assertEqual(self.job_row(7), before_job)
        self.assertEqual(self.asset_row(7), before_asset)


if __name__ == "__main__":
    unittest.main()
