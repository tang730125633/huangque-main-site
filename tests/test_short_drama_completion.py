import json
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock


import sys

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from content_domains import (
    short_drama,
    short_drama_assembly_lipsync,
    short_drama_completion,
    short_drama_video,
    short_drama_voice,
)


def _project_payload():
    return {
        "title": "D-6 完成确认测试",
        "synopsis": "一段用于验证完成快照和原子推进的短剧故事。",
        "ratio": "9:16",
        "target_duration": 30,
        "shot_count": 6,
        "visual_style": "电影写实",
        "point_budget": 100,
    }


def _plan():
    return {
        "characters": [],
        "script": {"title": "D-6 测试剧本", "dialogue_lines": []},
        "shots": [
            {
                "key": "shot-%d" % (index + 1),
                "duration": 5,
                "scene_description": "场景 %d" % (index + 1),
                "camera_description": "固定镜头",
                "character_keys": [],
                "dialogue_line_ids": [],
                "image_prompt": "电影感静帧",
                "video_prompt": "自然运动",
            }
            for index in range(6)
        ],
    }


class ShortDramaCompletionTests(unittest.TestCase):
    def setUp(self):
        descriptor, path = tempfile.mkstemp(
            prefix=".tmp-short-drama-completion-", suffix=".db", dir=ROOT
        )
        os.close(descriptor)
        self.db_path = Path(path)

        def db_factory():
            return sqlite3.connect(self.db_path, timeout=5)

        self.db = db_factory
        short_drama.init_db(self.db)
        project = short_drama.create_project(
            self.db, "alice", _project_payload()
        )
        applied = short_drama.apply_plan(
            self.db, "alice", project["id"], project["revision"],
            _plan(), planning_cost=0, planning_job_id=9601,
        )
        self.project_id = applied["id"]
        self._make_ready()

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(self.db_path) + suffix)
            if candidate.exists():
                candidate.unlink()

    def _make_ready(self):
        now = int(time.time())
        with closing(self.db()) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute(
                "UPDATE short_drama_projects SET stage='assembly_review',"
                "revision=revision+1 WHERE id=?",
                (self.project_id,),
            )
            short_drama_voice.ensure_voice_workspace(
                conn, self.project_id, allowed_stages={"assembly_review"}
            )
            conn.execute(
                "UPDATE short_drama_voice_shots SET locked=1 "
                "WHERE project_id=?",
                (self.project_id,),
            )
            short_drama_video.ensure_video_workspace(
                conn, self.project_id, allowed_stages={"assembly_review"}
            )
            conn.execute(
                "UPDATE short_drama_video_shots SET current_version=1,"
                "locked=1,updated_at=? WHERE project_id=?",
                (now, self.project_id),
            )
            shots = conn.execute(
                "SELECT id FROM short_drama_shots WHERE project_id=? "
                "ORDER BY sort_order,id",
                (self.project_id,),
            ).fetchall()
            for index, shot in enumerate(shots, 1):
                conn.execute(
                    "INSERT INTO short_drama_assets "
                    "(id,project_id,shot_id,type,current_version,locked,"
                    "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        "still-%d" % index, self.project_id, shot["id"],
                        "still", 1, 1, now, now,
                    ),
                )
            conn.execute(
                "INSERT INTO short_drama_compositions "
                "(project_id,assembly_revision,config_json,"
                "current_final_version,preview_locked,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (self.project_id, 4, "{}", 1, 1, now, now),
            )
            conn.execute(
                "INSERT INTO short_drama_composition_versions "
                "(id,project_id,kind,version,job_id,input_hash,config_json,"
                "file,url,cover_file,duration_ms,width,height,fps,video_codec,"
                "audio_codec,status,object_key,cover_key,sha256,size,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "final-version-1", self.project_id, "final", 1,
                    "final-job-1", "input-hash", "{}", "final.mp4",
                    "https://signed.invalid/final", "cover.jpg", 30000,
                    1080, 1920, 30.0, "h264", "aac", "succeeded",
                    "short-drama/alice/final.mp4",
                    "short-drama/alice/cover.jpg", "f" * 64, 123456, now,
                ),
            )
            conn.execute(
                "INSERT INTO short_drama_final_assets "
                "(id,owner_username,created_by,project_id,"
                "composition_version_id,job_id,title,object_key,cover_key,"
                "size,sha256,width,height,fps,duration_ms,video_codec,"
                "audio_codec,archive_status,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "final-asset-1", "alice", "alice", self.project_id,
                    "final-version-1", "final-job-1", "正式成片",
                    "short-drama/alice/final.mp4",
                    "short-drama/alice/cover.jpg", 123456, "f" * 64,
                    1080, 1920, 30.0, 30000, "h264", "aac", "ready", now,
                ),
            )
            conn.commit()

    def _readiness(self, actor="alice"):
        return short_drama_completion.readiness(
            self.db, actor, "alice", self.project_id,
            point_usage=short_drama._project_point_usage,
        )

    @staticmethod
    def _body(report):
        return {
            "project_id": report["project_id"],
            "revision": report["revision"],
            "final_version_id": report["final_version"]["id"],
            "asset_id": report["asset"]["id"],
            "delivery_hash": report["delivery_hash"],
            "acknowledged": True,
        }

    def test_schema_and_project_completion_columns_are_idempotent(self):
        short_drama_completion.init_db(self.db)
        with closing(self.db()) as conn:
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name LIKE 'short_drama_completion%'"
                )
            }
            columns = {
                row[1] for row in conn.execute(
                    "PRAGMA table_info(short_drama_projects)"
                )
            }
        self.assertEqual({
            "short_drama_completions",
            "short_drama_completion_attempts",
            "short_drama_completion_migration_runs",
            "short_drama_completion_migration_items",
        }, tables)
        self.assertTrue({
            "completion_id", "completed_at", "completed_by",
        }.issubset(columns))

    def test_default_rollout_keeps_legacy_stage_completion_available(self):
        with closing(self.db()) as conn:
            revision = conn.execute(
                "SELECT revision FROM short_drama_projects WHERE id=?",
                (self.project_id,),
            ).fetchone()[0]
        with mock.patch.dict(
            os.environ, {"HQ_SHORT_DRAMA_COMPLETION_ENABLED": "1"},
        ):
            with self.assertRaises(
                short_drama_completion.CompletionError
            ) as required:
                short_drama.confirm_stage(
                    self.db, "alice", self.project_id, revision,
                    "assembly_review",
                )
        self.assertEqual("completion_required", required.exception.code)

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HQ_SHORT_DRAMA_COMPLETION_ENABLED", None)
            completed = short_drama.confirm_stage(
                self.db, "alice", self.project_id, revision,
                "assembly_review",
            )
        self.assertEqual("completed", completed["stage"])
        self.assertEqual(revision + 1, completed["revision"])
        self.assertIsNone(completed["completion_id"])

    def _mark_legacy_completed(self, with_archived_attempt):
        now = int(time.time())
        with closing(self.db()) as conn:
            if with_archived_attempt:
                conn.execute(
                    "INSERT INTO short_drama_final_attempts "
                    "(id,actor_username,owner_username,project_id,"
                    "idempotency_key,request_hash,quote_token,cost,charge_key,"
                    "refund_key,state,job_id,asset_id,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        "legacy-final-attempt", "alice", "alice",
                        self.project_id, "legacy-final-key",
                        "legacy-final-hash", "legacy-final-quote", 5,
                        "legacy-final-charge", "legacy-final-refund",
                        "archived", "final-job-1", "final-asset-1", now, now,
                    ),
                )
            conn.execute(
                "UPDATE short_drama_projects SET stage='completed',"
                "revision=revision+1,updated_at=? WHERE id=?",
                (now, self.project_id),
            )
            conn.commit()

    def test_legacy_completed_project_migrates_only_with_complete_evidence(self):
        self._mark_legacy_completed(with_archived_attempt=True)
        dry_run = short_drama_completion.migrate_legacy_completions(
            self.db, apply=False, now=1730000100
        )
        self.assertEqual(
            {
                "dry_run": True,
                "run_id": None,
                "state": "dry_run",
                "replayed": False,
                "scanned": 1,
                "eligible": 1,
                "migrated": 0,
                "manual_review": [],
            },
            dry_run,
        )
        with closing(self.db()) as conn:
            self.assertIsNone(conn.execute(
                "SELECT completion_id FROM short_drama_projects WHERE id=?",
                (self.project_id,),
            ).fetchone()[0])
            self.assertEqual(0, conn.execute(
                "SELECT COUNT(*) FROM short_drama_completion_migration_runs"
            ).fetchone()[0])
        applied = short_drama_completion.migrate_legacy_completions(
            self.db, apply=True, now=1730000100,
            run_id="release-20260803",
        )
        self.assertEqual(1, applied["migrated"])
        self.assertEqual("release-20260803", applied["run_id"])
        with closing(self.db()) as conn:
            conn.row_factory = sqlite3.Row
            project = conn.execute(
                "SELECT stage,revision,completion_id FROM "
                "short_drama_projects WHERE id=?",
                (self.project_id,),
            ).fetchone()
            completion = conn.execute(
                "SELECT * FROM short_drama_completions WHERE project_id=?",
                (self.project_id,),
            ).fetchone()
            audit = conn.execute(
                "SELECT event_type,detail_json FROM short_drama_audit_events "
                "WHERE project_id=? AND event_type='legacy_completion_migrated'",
                (self.project_id,),
            ).fetchone()
        snapshot = json.loads(completion["snapshot_json"])
        self.assertEqual("completed", project["stage"])
        self.assertEqual(project["completion_id"], completion["completion_id"])
        self.assertEqual(project["revision"], completion["completed_revision"])
        self.assertTrue(snapshot["completion"]["legacy_migration"])
        self.assertEqual(
            "assembly_confirm",
            snapshot["legacy_evidence"]["source"],
        )
        self.assertEqual("legacy_completion_migrated", audit["event_type"])
        self.assertTrue(json.loads(audit["detail_json"])["legacy_migration"])
        replay = short_drama_completion.migrate_legacy_completions(
            self.db, apply=True, now=1730000200,
            run_id="release-20260803",
        )
        self.assertTrue(replay["replayed"])
        self.assertEqual(1, replay["migrated"])

        verified = short_drama_completion.verify_legacy_completions(
            self.db, run_id="release-20260803", now=1730000201,
        )
        self.assertTrue(verified["ok"])
        self.assertEqual(1, verified["applied_items"])

        rolled_back = short_drama_completion.rollback_legacy_completions(
            self.db, "release-20260803", now=1730000202,
        )
        self.assertEqual(1, rolled_back["rolled_back"])
        with closing(self.db()) as conn:
            project = conn.execute(
                "SELECT stage,revision,completion_id,completed_at,completed_by "
                "FROM short_drama_projects WHERE id=?", (self.project_id,),
            ).fetchone()
            completion_count = conn.execute(
                "SELECT COUNT(*) FROM short_drama_completions "
                "WHERE project_id=?", (self.project_id,),
            ).fetchone()[0]
        self.assertEqual("completed", project[0])
        self.assertIsNone(project[2])
        self.assertIsNone(project[3])
        self.assertIsNone(project[4])
        self.assertEqual(0, completion_count)
        rollback_replay = short_drama_completion.rollback_legacy_completions(
            self.db, "release-20260803", now=1730000203,
        )
        self.assertTrue(rollback_replay["replayed"])

    def test_legacy_migration_rollback_is_atomic_after_project_changes(self):
        self._mark_legacy_completed(with_archived_attempt=True)
        short_drama_completion.migrate_legacy_completions(
            self.db, apply=True, now=1730000100,
            run_id="release-conflict",
        )
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_projects SET revision=revision+1 "
                "WHERE id=?", (self.project_id,),
            )
            conn.commit()
        with self.assertRaises(short_drama_completion.CompletionError) as blocked:
            short_drama_completion.rollback_legacy_completions(
                self.db, "release-conflict", now=1730000200,
            )
        self.assertEqual("migration_rollback_blocked", blocked.exception.code)
        with closing(self.db()) as conn:
            project = conn.execute(
                "SELECT completion_id FROM short_drama_projects WHERE id=?",
                (self.project_id,),
            ).fetchone()
            completion_count = conn.execute(
                "SELECT COUNT(*) FROM short_drama_completions "
                "WHERE project_id=?", (self.project_id,),
            ).fetchone()[0]
            run_state = conn.execute(
                "SELECT state FROM short_drama_completion_migration_runs "
                "WHERE run_id='release-conflict'",
            ).fetchone()[0]
        self.assertIsNotNone(project[0])
        self.assertEqual(1, completion_count)
        self.assertEqual("applied", run_state)

    def test_legacy_completed_project_without_archived_attempt_needs_review(self):
        self._mark_legacy_completed(with_archived_attempt=False)
        result = short_drama_completion.migrate_legacy_completions(
            self.db, apply=True, now=1730000100
        )
        self.assertEqual(0, result["migrated"])
        self.assertEqual([{
            "project_id": self.project_id,
            "code": "legacy_final_attempt_missing",
        }], result["manual_review"])
        with closing(self.db()) as conn:
            project = conn.execute(
                "SELECT stage,completion_id FROM short_drama_projects "
                "WHERE id=?",
                (self.project_id,),
            ).fetchone()
            count = conn.execute(
                "SELECT COUNT(*) FROM short_drama_completions"
            ).fetchone()[0]
        self.assertEqual(("completed", None), project)
        self.assertEqual(0, count)

        with self.assertRaises(short_drama_completion.CompletionError) as pending:
            short_drama_completion.get_completion(
                self.db, "alice", self.project_id,
            )
        self.assertEqual(
            "legacy_completion_pending_migration", pending.exception.code,
        )
        self.assertEqual(409, pending.exception.status)

    def test_readiness_is_free_deterministic_and_machine_readable(self):
        first = self._readiness()
        second = self._readiness()
        self.assertTrue(first["ready"])
        self.assertEqual(first["delivery_hash"], second["delivery_hash"])
        self.assertEqual("final-version-1", first["final_version"]["id"])
        self.assertEqual("final-asset-1", first["asset"]["id"])
        self.assertNotIn("url", first["asset"])
        self.assertEqual(0, first["billing"]["reserved_points"])
        with closing(self.db()) as conn:
            self.assertEqual(
                0, conn.execute(
                    "SELECT COUNT(*) FROM short_drama_completion_attempts"
                ).fetchone()[0]
            )

    def test_d6_revalidates_manifest_body_and_hash_fail_closed(self):
        plan = {
            "plan_hash": "locked-plan-hash",
            "dependency_hash": "dependency-hash",
            "selected_sources": [{
                "shot_id": "shot-1",
                "version_id": "lipsync-version-1",
                "file_hash": "source-hash",
            }],
        }

        def manifest():
            value = {
                "contract_version":
                    short_drama_assembly_lipsync.MANIFEST_CONTRACT_VERSION,
                "kind": "final",
                "project_id": self.project_id,
                "input_hash": "input-hash",
                "plan_hash": plan["plan_hash"],
                "selected_sources": plan["selected_sources"],
            }
            value["manifest_hash"] = (
                short_drama_assembly_lipsync.canonical_hash(value)
            )
            return value

        def persist(
            value, stored_hash=None, version_plan_hash=plan["plan_hash"]
        ):
            with closing(self.db()) as conn:
                conn.execute(
                    "UPDATE short_drama_composition_versions "
                    "SET plan_hash=?,manifest_hash=?,manifest_json=? "
                    "WHERE id='final-version-1'",
                    (
                        version_plan_hash,
                        stored_hash if stored_hash is not None else (
                            value.get("manifest_hash", "")
                            if isinstance(value, dict) else ""
                        ),
                        json.dumps(value, ensure_ascii=False)
                        if isinstance(value, dict) else value,
                    ),
                )
                conn.commit()

        valid = manifest()
        persist(valid)
        with mock.patch.object(
            short_drama_assembly_lipsync,
            "load_plan",
            return_value=plan,
        ):
            valid_report = self._readiness()
            self.assertTrue(valid_report["ready"])

            cases = []
            cases.append(("{", valid["manifest_hash"],
                          "lipsync_manifest_invalid"))
            cases.append((valid, "tampered-hash",
                          "lipsync_manifest_mismatch"))
            changed_source = {
                **valid,
                "selected_sources": [{
                    "shot_id": "shot-1",
                    "version_id": "other-version",
                    "file_hash": "other-hash",
                }],
            }
            changed_source["manifest_hash"] = (
                short_drama_assembly_lipsync.canonical_hash({
                    key: item for key, item in changed_source.items()
                    if key != "manifest_hash"
                })
            )
            cases.append((
                changed_source, changed_source["manifest_hash"],
                "lipsync_manifest_mismatch",
            ))
            changed_plan = {**valid, "plan_hash": "other-plan"}
            changed_plan["manifest_hash"] = (
                short_drama_assembly_lipsync.canonical_hash({
                    key: item for key, item in changed_plan.items()
                    if key != "manifest_hash"
                })
            )
            cases.append((
                changed_plan, changed_plan["manifest_hash"],
                "lipsync_manifest_mismatch",
            ))

            for value, stored_hash, expected_code in cases:
                with self.subTest(expected_code=expected_code, value=value):
                    persist(value, stored_hash)
                    report = self._readiness()
                    self.assertFalse(report["ready"])
                    self.assertIn(
                        expected_code,
                        {item["code"] for item in report["blockers"]},
                    )
            persist(
                valid,
                valid["manifest_hash"],
                version_plan_hash="different-column-plan-hash",
            )
            mismatch = self._readiness()
            self.assertFalse(mismatch["ready"])
            self.assertIn(
                "lipsync_manifest_mismatch",
                {item["code"] for item in mismatch["blockers"]},
            )
            with mock.patch.dict(
                os.environ, {"HQ_SHORT_DRAMA_COMPLETION_ENABLED": "1"}
            ):
                with self.assertRaises(
                    short_drama_completion.CompletionError
                ) as caught:
                    short_drama_completion.confirm(
                        self.db, "alice", "alice", "",
                        self._body(valid_report), "mismatched-column-confirm",
                        point_usage=short_drama._project_point_usage,
                    )
            self.assertEqual(
                "lipsync_manifest_mismatch", caught.exception.code
            )
            with closing(self.db()) as conn:
                self.assertEqual(
                    0,
                    conn.execute(
                        "SELECT COUNT(*) FROM short_drama_completions"
                    ).fetchone()[0],
                )
                self.assertEqual(
                    0,
                    conn.execute(
                        "SELECT COUNT(*) FROM short_drama_audit_events "
                        "WHERE event_type='completion_committed'"
                    ).fetchone()[0],
                )
                project = conn.execute(
                    "SELECT stage,completion_id FROM short_drama_projects "
                    "WHERE id=?",
                    (self.project_id,),
                ).fetchone()
                self.assertEqual(("assembly_review", None), project)

            persist(
                valid, valid["manifest_hash"], version_plan_hash=""
            )
            empty_column = self._readiness()
            self.assertFalse(empty_column["ready"])
            self.assertIn(
                "lipsync_manifest_mismatch",
                {item["code"] for item in empty_column["blockers"]},
            )

            persist(valid)
            consistent = self._readiness()
            self.assertTrue(consistent["ready"])
            with mock.patch.dict(
                os.environ, {"HQ_SHORT_DRAMA_COMPLETION_ENABLED": "1"}
            ):
                completed = short_drama_completion.confirm(
                    self.db, "alice", "alice", "",
                    self._body(consistent), "consistent-manifest-confirm",
                    point_usage=short_drama._project_point_usage,
                )
            self.assertEqual("completed", completed["stage"])

    def test_all_charge_tables_and_non_terminal_states_block_completion(self):
        tables = (
            "short_drama_charge_attempts",
            "short_drama_voice_charge_attempts",
            "short_drama_video_charge_attempts",
            "short_drama_final_attempts",
            "short_drama_provider_shot_attempts",
        )
        with closing(sqlite3.connect(":memory:")) as conn:
            conn.row_factory = sqlite3.Row
            for table in tables:
                conn.execute(
                    "CREATE TABLE %s (project_id TEXT, state TEXT)" % table
                )
            for table in tables:
                for state in ("accepted", "charged", "linked", "refund_pending"):
                    with self.subTest(table=table, state=state):
                        conn.execute(
                            "INSERT INTO %s (project_id,state) VALUES (?,?)"
                            % table,
                            ("project-under-review", state),
                        )
                        found = short_drama_completion._unsettled_attempts(
                            conn, "project-under-review"
                        )
                        self.assertEqual(
                            [{"table": table, "state": state}], found
                        )
                        conn.execute("DELETE FROM %s" % table)
            for table in tables:
                for state in ("done", "refunded", "failed"):
                    conn.execute(
                        "INSERT INTO %s (project_id,state) VALUES (?,?)" % table,
                        ("project-under-review", state),
                    )
            self.assertEqual(
                [],
                short_drama_completion._unsettled_attempts(
                    conn, "project-under-review"
                ),
            )

    def test_charged_final_attempt_without_job_blocks_completion(self):
        with closing(self.db()) as conn:
            conn.execute(
                "INSERT INTO short_drama_final_attempts "
                "(id,actor_username,owner_username,project_id,"
                "idempotency_key,request_hash,quote_token,cost,charge_key,"
                "refund_key,state,job_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "charged-without-job", "alice", "alice", self.project_id,
                    "charged-without-job-key", "charged-without-job-hash",
                    "charged-without-job-quote", 5,
                    "charged-without-job-charge", "charged-without-job-refund",
                    "charged", None, 1, 1,
                ),
            )
            conn.commit()
        report = self._readiness()
        self.assertFalse(report["ready"])
        self.assertEqual(1, report["billing"]["unsettled_attempts"])
        self.assertIn(
            "billing_unsettled",
            {item["code"] for item in report["blockers"]},
        )

    def test_readiness_returns_permission_lock_media_job_and_billing_blockers(self):
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_voice_shots SET locked=0 "
                "WHERE shot_id=(SELECT id FROM short_drama_shots "
                "WHERE project_id=? ORDER BY sort_order LIMIT 1)",
                (self.project_id,),
            )
            conn.execute(
                "UPDATE short_drama_final_assets SET width=720 "
                "WHERE id='final-asset-1'"
            )
            conn.execute(
                "INSERT INTO short_drama_composition_jobs "
                "(id,username,project_id,job_id,kind,idempotency_key,"
                "request_hash,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    "active-job", "alice", self.project_id, "job-active",
                    "preview", "active-idem", "active-hash", "running",
                    1, 1,
                ),
            )
            conn.execute(
                "INSERT INTO short_drama_final_attempts "
                "(id,actor_username,owner_username,project_id,"
                "idempotency_key,request_hash,quote_token,cost,charge_key,"
                "refund_key,state,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "pending-attempt", "alice", "alice", self.project_id,
                    "pending-idem", "pending-hash", "pending-quote", 5,
                    "pending-charge", "pending-refund", "accepted", 1, 1,
                ),
            )
            conn.commit()
        report = self._readiness(actor="editor")
        codes = {item["code"] for item in report["blockers"]}
        self.assertTrue({
            "forbidden", "required_lock_missing",
            "media_verification_failed", "active_job", "billing_unsettled",
        }.issubset(codes))
        self.assertTrue(all({
            "code", "domain", "entity_id", "message", "recommended_action",
        } == set(item) for item in report["blockers"]))

    @mock.patch.dict(os.environ, {"HQ_SHORT_DRAMA_COMPLETION_ENABLED": "1"})
    def test_confirm_is_atomic_idempotent_and_permanently_read_only(self):
        report = self._readiness()
        body = self._body(report)
        result = short_drama_completion.confirm(
            self.db, "alice", "alice", "", body, "completion-key",
            point_usage=short_drama._project_point_usage,
        )
        replay = short_drama_completion.confirm(
            self.db, "alice", "alice", "", body, "completion-key",
            point_usage=short_drama._project_point_usage,
        )
        self.assertFalse(result["replayed"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(result["completion_id"], replay["completion_id"])
        self.assertEqual("completed", result["stage"])
        self.assertNotIn("video_url", result["snapshot"]["asset"])
        with closing(self.db()) as conn:
            project = conn.execute(
                "SELECT stage,revision,completion_id,completed_by "
                "FROM short_drama_projects WHERE id=?",
                (self.project_id,),
            ).fetchone()
            counts = (
                conn.execute(
                    "SELECT COUNT(*) FROM short_drama_completions"
                ).fetchone()[0],
                conn.execute(
                    "SELECT COUNT(*) FROM short_drama_audit_events "
                    "WHERE event_type='completion_committed'"
                ).fetchone()[0],
                conn.execute(
                    "SELECT COUNT(*) FROM short_drama_completion_attempts"
                ).fetchone()[0],
            )
        self.assertEqual(("completed", result["revision"],
                          result["completion_id"], "alice"), project)
        self.assertEqual((1, 1, 1), counts)
        with self.assertRaises(short_drama_completion.ProjectCompleted):
            short_drama._project_username_for_access(
                self.db, "alice", self.project_id, write=True
            )

    @mock.patch.dict(os.environ, {"HQ_SHORT_DRAMA_COMPLETION_ENABLED": "1"})
    def test_same_key_different_request_conflicts_without_side_effects(self):
        report = self._readiness()
        body = self._body(report)
        short_drama_completion.confirm(
            self.db, "alice", "alice", "", body, "stable-key",
            point_usage=short_drama._project_point_usage,
        )
        changed = dict(body)
        changed["asset_id"] = "other-asset"
        with self.assertRaises(short_drama_completion.CompletionError) as error:
            short_drama_completion.confirm(
                self.db, "alice", "alice", "", changed, "stable-key",
                point_usage=short_drama._project_point_usage,
            )
        self.assertEqual("idempotency_conflict", error.exception.code)
        with closing(self.db()) as conn:
            self.assertEqual(
                1, conn.execute(
                    "SELECT COUNT(*) FROM short_drama_completions"
                ).fetchone()[0]
            )

    @mock.patch.dict(os.environ, {"HQ_SHORT_DRAMA_COMPLETION_ENABLED": "1"})
    def test_failure_injection_rolls_back_snapshot_audit_project_and_attempt(self):
        for stage in (
            "attempt_started", "readiness_passed", "snapshot_written",
            "audit_written", "before_commit",
        ):
            with self.subTest(stage=stage):
                report = self._readiness()
                body = self._body(report)

                def fail(current):
                    if current == stage:
                        raise RuntimeError("injected " + stage)

                with self.assertRaises(RuntimeError):
                    short_drama_completion.confirm(
                        self.db, "alice", "alice", "", body,
                        "failure-" + stage,
                        point_usage=short_drama._project_point_usage,
                        failure_hook=fail,
                    )
                with closing(self.db()) as conn:
                    project = conn.execute(
                        "SELECT stage,completion_id FROM short_drama_projects "
                        "WHERE id=?", (self.project_id,),
                    ).fetchone()
                    self.assertEqual(("assembly_review", None), project)
                    self.assertEqual(
                        0, conn.execute(
                            "SELECT COUNT(*) FROM short_drama_completions"
                        ).fetchone()[0]
                    )
                    self.assertEqual(
                        0, conn.execute(
                            "SELECT COUNT(*) FROM short_drama_audit_events"
                        ).fetchone()[0]
                    )
                    self.assertEqual(
                        0, conn.execute(
                            "SELECT COUNT(*) FROM "
                            "short_drama_completion_attempts"
                        ).fetchone()[0]
                    )

    def test_completion_is_feature_gated(self):
        report = self._readiness()
        with mock.patch.dict(
            os.environ, {"HQ_SHORT_DRAMA_COMPLETION_ENABLED": "0"}
        ):
            with self.assertRaises(
                short_drama_completion.CompletionDisabled
            ):
                short_drama_completion.confirm(
                    self.db, "alice", "alice", "",
                    self._body(report), "disabled-key",
                    point_usage=short_drama._project_point_usage,
                )


if __name__ == "__main__":
    unittest.main()
