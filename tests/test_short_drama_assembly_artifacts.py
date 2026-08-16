import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path


import sys

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from content_domains import short_drama, short_drama_assembly_artifacts as artifacts


class ShortDramaAssemblyArtifactTests(unittest.TestCase):
    @staticmethod
    def bundle_artifacts(hash_character="a"):
        base = "short_drama_assembly/p/d2-a"
        return [
            {
                "kind": "shot_voice", "shot_id": "shot-1",
                "file": f"{base}/shot-1.wav",
                "file_hash": hash_character * 64,
                "duration_ms": 1000, "sample_rate": 48000, "channels": 2,
            },
            {
                "kind": "dialogue", "shot_id": "",
                "file": f"{base}/dialogue.wav",
                "file_hash": hash_character * 64,
                "duration_ms": 30000, "sample_rate": 48000, "channels": 2,
            },
            {
                "kind": "master_audio", "shot_id": "",
                "file": f"{base}/master.wav",
                "file_hash": hash_character * 64,
                "duration_ms": 30000, "sample_rate": 48000, "channels": 2,
            },
            {
                "kind": "subtitles_ass", "shot_id": "",
                "file": f"{base}/subtitles.ass",
                "file_hash": hash_character * 64,
            },
            {
                "kind": "manifest", "shot_id": "",
                "file": f"{base}/manifest.json",
                "file_hash": hash_character * 64,
            },
        ]

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(
            prefix=".tmp-short-drama-d2-artifacts-", dir=ROOT
        )
        self.db_path = Path(self.tempdir.name) / "content.db"

        def db_factory():
            return sqlite3.connect(self.db_path, timeout=5)

        self.db = db_factory
        short_drama.init_db(self.db)
        project = short_drama.create_project(self.db, "alice", {
            "title": "D2 产物测试",
            "synopsis": "用于测试短剧音频字幕中间产物缓存和并发资格。",
            "ratio": "9:16",
            "target_duration": 30,
            "shot_count": 6,
            "visual_style": "电影写实",
            "point_budget": 100,
        })
        self.project_id = project["id"]

    def tearDown(self):
        self.tempdir.cleanup()

    def test_schema_and_identity_constraints(self):
        with closing(self.db()) as conn:
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name LIKE 'short_drama_assembly_%'"
                )
            }
            self.assertIn("short_drama_assembly_builds", tables)
            self.assertIn("short_drama_assembly_artifacts", tables)

        claim = artifacts.claim_build(
            self.db, self.project_id, "d1-a", "d2-a", now=100
        )
        self.assertEqual("claimed", claim["status"])
        with closing(self.db()) as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE short_drama_assembly_builds SET input_hash='other' "
                    "WHERE project_id=? AND input_hash='d2-a'",
                    (self.project_id,),
                )

    def test_claim_is_exclusive_recoverable_and_ready_is_reused(self):
        first = artifacts.claim_build(
            self.db, self.project_id, "d1-a", "d2-a", now=100
        )
        self.assertEqual("claimed", first["status"])
        self.assertEqual(
            "in_progress",
            artifacts.claim_build(
                self.db, self.project_id, "d1-a", "d2-a", now=101
            )["status"],
        )
        recovered = artifacts.claim_build(
            self.db, self.project_id, "d1-a", "d2-a",
            now=1000, stale_after_seconds=600,
        )
        self.assertEqual("claimed", recovered["status"])
        self.assertNotEqual(first["claim_token"], recovered["claim_token"])
        self.assertFalse(artifacts.claim_is_current(
            self.db, self.project_id, "d2-a", first["claim_token"]
        ))
        self.assertTrue(artifacts.claim_is_current(
            self.db, self.project_id, "d2-a", recovered["claim_token"]
        ))
        with self.assertRaises(ValueError):
            artifacts.record_ready(
                self.db, self.project_id, "d1-a", "d2-a", [],
                {}, first["claim_token"], now=1001,
            )
        artifacts.mark_failed(
            self.db, self.project_id, "d2-a", "audio_mix_failed",
            first["claim_token"], now=1001,
        )
        self.assertEqual(
            "building",
            artifacts.build_snapshot(
                self.db, self.project_id, "d1-a", "d2-a"
            )["status"],
        )
        artifacts.record_ready(
            self.db,
            self.project_id,
            "d1-a",
            "d2-a",
            self.bundle_artifacts(),
            {"engine_version": artifacts.ENGINE_VERSION},
            recovered["claim_token"],
            now=1001,
        )
        self.assertEqual(
            "validation_required",
            artifacts.claim_build(
                self.db, self.project_id, "d1-a", "d2-a", now=1002
            )["status"],
        )
        self.assertEqual(
            "ready",
            artifacts.claim_build(
                self.db, self.project_id, "d1-a", "d2-a", now=1002,
                ready_validator=lambda _project_id, _input_hash: True,
            )["status"],
        )

    def test_concurrent_claim_has_one_builder(self):
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def worker():
            try:
                barrier.wait()
                results.append(artifacts.claim_build(
                    self.db, self.project_id, "d1-a", "d2-race",
                    now=int(time.time()),
                )["status"])
            except Exception as error:
                errors.append(error)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual([], errors)
        self.assertEqual(["claimed", "in_progress"], sorted(results))

    def test_ready_snapshot_hides_file_paths_and_rejects_bad_artifacts(self):
        claim = artifacts.claim_build(
            self.db, self.project_id, "d1-a", "d2-a", now=100
        )
        for bad in (
            {"kind": "master_audio", "file": "../master.wav",
             "file_hash": "a" * 64},
            {"kind": "unknown", "file": "master.wav",
             "file_hash": "a" * 64},
            {"kind": "master_audio", "file": "master.wav",
             "file_hash": "bad"},
        ):
            with self.assertRaises(ValueError):
                artifacts.record_ready(
                    self.db, self.project_id, "d1-a", "d2-a",
                    [bad], {}, claim["claim_token"], now=101,
                )
        artifacts.record_ready(
            self.db, self.project_id, "d1-a", "d2-a",
            self.bundle_artifacts("b"),
            {"subtitle_events": 2},
            claim["claim_token"],
            now=102,
        )
        snapshot = artifacts.build_snapshot(
            self.db, self.project_id, "d1-a", "d2-a"
        )
        self.assertEqual("ready", snapshot["status"])
        self.assertEqual(5, len(snapshot["artifacts"]))
        self.assertNotIn("file", snapshot["artifacts"][0])
        self.assertEqual("b" * 64, snapshot["artifacts"][0]["file_hash"])

    def test_invalid_ready_cache_is_marked_stale_and_reclaimed(self):
        claim = artifacts.claim_build(
            self.db, self.project_id, "d1-a", "d2-a", now=100
        )
        artifacts.record_ready(
            self.db, self.project_id, "d1-a", "d2-a",
            self.bundle_artifacts(), {}, claim["claim_token"], now=101,
        )
        recovered = artifacts.claim_build(
            self.db, self.project_id, "d1-a", "d2-a", now=102,
            ready_validator=lambda _project_id, _input_hash: False,
        )
        self.assertEqual("claimed", recovered["status"])
        with closing(self.db()) as conn:
            statuses = {
                row[0] for row in conn.execute(
                    "SELECT status FROM short_drama_assembly_artifacts "
                    "WHERE project_id=? AND input_hash=?",
                    (self.project_id, "d2-a"),
                )
            }
        self.assertEqual({"stale"}, statuses)

    def test_failed_build_can_retry_without_changing_identity(self):
        claim = artifacts.claim_build(
            self.db, self.project_id, "d1-a", "d2-a", now=100
        )
        artifacts.mark_failed(
            self.db, self.project_id, "d2-a", "audio_mix_failed",
            claim["claim_token"], now=101,
        )
        snapshot = artifacts.build_snapshot(
            self.db, self.project_id, "d1-a", "d2-a"
        )
        self.assertEqual("failed", snapshot["status"])
        self.assertEqual("audio_mix_failed", snapshot["error_code"])
        self.assertEqual(
            "claimed",
            artifacts.claim_build(
                self.db, self.project_id, "d1-a", "d2-a", now=102
            )["status"],
        )

    def test_reusable_audio_preserves_hashes_and_stales_source_atomically(self):
        claim = artifacts.claim_build(
            self.db, self.project_id, "d1-source", "d2-source", now=100
        )
        values = self.bundle_artifacts("f")
        artifacts.record_ready(
            self.db,
            self.project_id,
            "d1-source",
            "d2-source",
            values,
            {
                "master_audio": {
                    "master_audio_hash": "m" * 64,
                }
            },
            claim["claim_token"],
            now=101,
        )
        reusable = artifacts.reusable_audio_files(
            self.db, self.project_id, "m" * 64
        )
        self.assertEqual("d2-source", reusable["source_input_hash"])
        master = reusable["files"][("master_audio", "")]
        self.assertEqual("f" * 64, master["file_hash"])
        self.assertEqual(30000, master["duration_ms"])
        self.assertEqual(48000, master["sample_rate"])
        self.assertEqual(2, master["channels"])
        self.assertTrue(artifacts.mark_reusable_audio_stale(
            self.db,
            self.project_id,
            "d2-source",
            "audio_cache_hash_mismatch",
        ))
        with closing(self.db()) as conn:
            build = conn.execute(
                "SELECT status,error_code FROM short_drama_assembly_builds "
                "WHERE project_id=? AND input_hash=?",
                (self.project_id, "d2-source"),
            ).fetchone()
            statuses = {
                row[0] for row in conn.execute(
                    "SELECT status FROM short_drama_assembly_artifacts "
                    "WHERE project_id=? AND input_hash=?",
                    (self.project_id, "d2-source"),
                )
            }
        self.assertEqual(("stale", "audio_cache_hash_mismatch"), build)
        self.assertEqual({"stale"}, statuses)

    def test_input_hash_is_canonical_and_includes_bgm_fingerprint(self):
        left = artifacts.compute_input_hash(
            "d1", {"subtitle": {"position": "bottom"}},
            [{"id": "v1", "sha256": "a" * 64}],
            {"id": 2, "sha256": "b" * 64},
        )
        right = artifacts.compute_input_hash(
            "d1", {"subtitle": {"position": "bottom"}},
            [{"sha256": "a" * 64, "id": "v1"}],
            {"sha256": "b" * 64, "id": 2},
        )
        changed = artifacts.compute_input_hash(
            "d1", {"subtitle": {"position": "bottom"}},
            [{"id": "v1", "sha256": "a" * 64}],
            {"id": 2, "sha256": "c" * 64},
        )
        self.assertEqual(left, right)
        self.assertNotEqual(left, changed)


if __name__ == "__main__":
    unittest.main()
