import json
import os
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

SERVER = str(Path(__file__).resolve().parents[1] / "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

from content_domains import short_drama_assembly_lipsync as subject
from content_domains import short_drama_assembly


class ShortDramaAssemblyLipsyncTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
        CREATE TABLE short_drama_projects (
          id TEXT PRIMARY KEY, revision INTEGER NOT NULL
        );
        CREATE TABLE short_drama_shots (
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL
        );
        CREATE TABLE short_drama_lipsync_quotes (
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL
        );
        CREATE TABLE short_drama_lipsync_attempts (
          id TEXT PRIMARY KEY, quote_id TEXT NOT NULL, state TEXT NOT NULL,
          cost INTEGER NOT NULL, updated_at INTEGER NOT NULL
        );
        CREATE TABLE short_drama_lipsync_jobs (
          id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL, project_id TEXT NOT NULL,
          state TEXT NOT NULL, created_at INTEGER NOT NULL
        );
        CREATE TABLE short_drama_lipsync_versions (
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL, shot_id TEXT NOT NULL,
          version INTEGER NOT NULL, job_id TEXT NOT NULL, input_hash TEXT NOT NULL,
          provider TEXT NOT NULL, model_version TEXT NOT NULL,
          dependency_hashes_json TEXT NOT NULL, media_spec_json TEXT NOT NULL,
          file TEXT NOT NULL, file_hash TEXT NOT NULL, cost_json TEXT NOT NULL
        );
        CREATE TABLE short_drama_lipsync_current (
          project_id TEXT NOT NULL, shot_id TEXT NOT NULL, version_id TEXT NOT NULL,
          revision INTEGER NOT NULL, locked_at INTEGER, locked_by TEXT
        );
        """)
        self.conn.executescript(subject.SCHEMA)
        self.conn.execute(
            "INSERT INTO short_drama_projects VALUES ('project-1',7)"
        )
        self.conn.execute(
            "INSERT INTO short_drama_shots VALUES ('shot-1','project-1')"
        )
        self.conn.execute(
            "INSERT INTO short_drama_lipsync_quotes VALUES ('quote-1','project-1')"
        )
        self.conn.execute(
            "INSERT INTO short_drama_lipsync_attempts "
            "VALUES ('attempt-1','quote-1','settled',20,100)"
        )
        self.conn.execute(
            "INSERT INTO short_drama_lipsync_jobs "
            "VALUES ('job-1','attempt-1','project-1','succeeded',100)"
        )
        self.conn.execute(
            "INSERT INTO short_drama_lipsync_versions VALUES "
            "('version-1','project-1','shot-1',1,'job-1','input-1','fal',"
            "'model-1','{}',?,'clips/lipsync.mp4','file-hash','{\"points\":20}')",
            (json.dumps({
                "duration_ms": 5000, "width": 1280, "height": 720,
                "ratio": "16:9",
            }),),
        )
        self.conn.execute(
            "INSERT INTO short_drama_lipsync_current VALUES "
            "('project-1','shot-1','version-1',2,101,'owner')"
        )
        self.source_hash = "a" * 64
        self.conn.execute(
            "UPDATE short_drama_lipsync_versions SET file_hash=?",
            (self.source_hash,),
        )
        self.conn.commit()
        self.project = {"id": "project-1", "revision": 7}
        self.inspected = {
            "fingerprint": {"sha256": self.source_hash},
            "probe": {
                "duration_ms": 5000,
                "video": {"width": 1280, "height": 720},
                "audio": {"codec": "aac"},
            },
        }
        self.snapshot = {
            "input_hash": "input-1",
            "dependencies": {
                "timeline": {
                    "version_id": "timeline-1", "timeline_version": 1,
                    "timeline_revision": 2, "contract_version": "v1",
                    "timeline_hash": "timeline-hash", "source_hashes": {},
                    "visible_segments": [{
                        "id": "segment-1", "shot_id": "shot-1",
                        "start_ms": 0, "end_ms": 1000,
                    }],
                },
                "audio": {"master_audio_hash": "audio-hash"},
                "alignment": {
                    "version_id": "alignment-1", "version": 1,
                    "status": "locked", "effective_status": "locked",
                    "input_hash": "alignment-input",
                    "alignment_hash": "alignment-hash",
                    "review_audit_complete": True,
                },
                "visual_sources": [{
                    "shot_id": "shot-1", "source_hash": "visual-hash",
                }],
            },
        }

    def tearDown(self):
        self.conn.close()

    def test_capture_freezes_locked_settled_version_and_survives_revision_change(self):
        with mock.patch.dict(
            os.environ, {"HQ_SHORT_DRAMA_LIPSYNC_ASSEMBLY_ENABLED": "1"}
        ), mock.patch.object(
            subject.short_drama_lipsync_snapshot,
            "build_snapshot",
            return_value=self.snapshot,
        ):
            plan = subject.capture_for_handoff(
                self.conn, self.project,
                source_inspector=lambda _: self.inspected,
            )
        self.assertEqual(plan["required_shot_ids"], ["shot-1"])
        self.assertEqual(
            plan["selected_sources"][0]["version_id"], "version-1"
        )
        changed = {"id": "project-1", "revision": 8}
        with mock.patch.object(
            subject.short_drama_lipsync_snapshot,
            "build_snapshot",
            return_value=self.snapshot,
        ):
            loaded = subject.load_plan(self.conn, changed, require=True)
        self.assertEqual(loaded["plan_hash"], plan["plan_hash"])

    def test_dependency_change_blocks_assembly(self):
        with mock.patch.dict(
            os.environ, {"HQ_SHORT_DRAMA_LIPSYNC_ASSEMBLY_ENABLED": "1"}
        ), mock.patch.object(
            subject.short_drama_lipsync_snapshot,
            "build_snapshot",
            return_value=self.snapshot,
        ):
            subject.capture_for_handoff(
                self.conn, self.project,
                source_inspector=lambda _: self.inspected,
            )
        changed = json.loads(json.dumps(self.snapshot))
        changed["dependencies"]["audio"]["master_audio_hash"] = "changed"
        with mock.patch.object(
            subject.short_drama_lipsync_snapshot,
            "build_snapshot",
            return_value=changed,
        ):
            with self.assertRaises(subject.LipsyncAssemblyBlocked) as caught:
                subject.load_plan(self.conn, self.project, require=True)
        self.assertEqual(caught.exception.code, "lipsync_dependency_changed")

    def test_unsettled_attempt_blocks_handoff(self):
        self.conn.execute(
            "UPDATE short_drama_lipsync_attempts SET state='charged'"
        )
        self.conn.commit()
        with mock.patch.dict(
            os.environ, {"HQ_SHORT_DRAMA_LIPSYNC_ASSEMBLY_ENABLED": "1"}
        ), mock.patch.object(
            subject.short_drama_lipsync_snapshot,
            "build_snapshot",
            return_value=self.snapshot,
        ):
            with self.assertRaises(subject.LipsyncAssemblyBlocked) as caught:
                subject.capture_for_handoff(
                    self.conn, self.project,
                    source_inspector=lambda _: self.inspected,
                )
        self.assertEqual(caught.exception.code, "lipsync_billing_unsettled")

    def test_handoff_fails_closed_when_source_bytes_do_not_match_version(self):
        replaced = {
            **self.inspected,
            "fingerprint": {"sha256": "replaced-file-hash"},
        }
        with mock.patch.dict(
            os.environ, {"HQ_SHORT_DRAMA_LIPSYNC_ASSEMBLY_ENABLED": "1"}
        ), mock.patch.object(
            subject.short_drama_lipsync_snapshot,
            "build_snapshot",
            return_value=self.snapshot,
        ):
            with self.assertRaises(subject.LipsyncAssemblyBlocked) as caught:
                subject.capture_for_handoff(
                    self.conn, self.project,
                    source_inspector=lambda _: replaced,
                )
        self.assertEqual(
            caught.exception.code, "lipsync_source_hash_mismatch"
        )
        self.assertEqual(
            0,
            self.conn.execute(
                "SELECT COUNT(*) FROM short_drama_lipsync_assembly_plans"
            ).fetchone()[0],
        )

    def test_manifest_validator_rejects_damage_and_source_substitution(self):
        plan = {
            "plan_hash": "plan-hash",
            "selected_sources": [{"shot_id": "shot-1", "file_hash": "hash"}],
        }
        manifest = {
            "contract_version": subject.MANIFEST_CONTRACT_VERSION,
            "kind": "final",
            "project_id": "project-1",
            "input_hash": "input-hash",
            "plan_hash": plan["plan_hash"],
            "selected_sources": plan["selected_sources"],
        }
        manifest["manifest_hash"] = subject.canonical_hash(manifest)
        self.assertEqual(
            manifest["manifest_hash"],
            subject.validate_composition_manifest(
                manifest,
                stored_manifest_hash=manifest["manifest_hash"],
                expected_kind="final",
                expected_project_id="project-1",
                expected_input_hash="input-hash",
                plan=plan,
            ),
        )
        for damaged in (
            "{",
            {**manifest, "manifest_hash": "tampered"},
            {
                **manifest,
                "selected_sources": [
                    {"shot_id": "shot-1", "file_hash": "other"}
                ],
            },
            {**manifest, "plan_hash": "other-plan"},
        ):
            with self.subTest(damaged=damaged):
                with self.assertRaises(subject.LipsyncAssemblyBlocked):
                    subject.validate_composition_manifest(
                        damaged,
                        stored_manifest_hash=manifest["manifest_hash"],
                        expected_kind="final",
                        expected_project_id="project-1",
                        expected_input_hash="input-hash",
                        plan=plan,
                    )

    def test_render_handoff_rechecks_lipsync_source_bytes(self):
        sources = [{
            "id": "shot-1",
            "video_version": {"file": "clips/lipsync.mp4"},
            "lipsync_source": {"file_hash": "frozen-hash"},
        }]
        with self.assertRaises(short_drama_assembly.PreviewBlocked) as caught:
            short_drama_assembly._validate_lipsync_render_sources(
                sources,
                lambda _: {
                    "fingerprint": {"sha256": "replaced-hash"},
                    "probe": {},
                },
            )
        self.assertEqual(
            caught.exception.code, "lipsync_source_hash_mismatch"
        )

    def test_apply_replaces_only_required_shot_visual(self):
        plan = {
            "selected_sources": [{
                "shot_id": "shot-1", "version_id": "version-1",
                "version": 1, "file": "clips/lipsync.mp4",
                "input_hash": "input-1",
                "media_spec": {"duration_ms": 5000},
            }]
        }
        sources = [{
            "id": "shot-1", "duration": 5,
            "video_version": {
                "id": "ordinary", "file": "ordinary.mp4",
            },
        }, {
            "id": "shot-2", "duration": 5,
            "video_version": {
                "id": "ordinary-2", "file": "ordinary-2.mp4",
            },
        }]
        subject.apply_to_sources(sources, plan, "16:9")
        self.assertEqual(
            sources[0]["video_version"]["file"], "clips/lipsync.mp4"
        )
        self.assertEqual(sources[0]["lipsync_source"]["version_id"], "version-1")
        self.assertEqual(
            sources[1]["video_version"]["file"], "ordinary-2.mp4"
        )


if __name__ == "__main__":
    unittest.main()
