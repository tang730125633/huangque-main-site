import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from server.content_domains import (
    short_drama,
    short_drama_assembly,
    short_drama_sound_design,
)


def plan():
    shots = []
    for index in range(6):
        shots.append({
            "key": "shot-%d" % (index + 1),
            "duration": 5,
            "scene_description": (
                "孩子在雨中的街道奔跑" if index == 0
                else "安静的室内镜头"
            ),
            "camera_description": "固定镜头",
            "character_keys": [],
            "dialogue_line_ids": [],
            "image_prompt": "电影写实画面",
            "video_prompt": "自然动作，不要声音",
        })
    return {
        "characters": [],
        "script": {"title": "音效测试", "dialogue_lines": []},
        "shots": shots,
    }


class ShortDramaSoundDesignTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "content.db"

        def db_factory():
            connection = sqlite3.connect(self.path)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            return connection

        self.db = db_factory
        with closing(self.db()) as conn:
            conn.execute(
                "CREATE TABLE jobs("
                "id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT,username TEXT,"
                "cost INTEGER,status TEXT DEFAULT 'pending',payload TEXT,"
                "result TEXT,error TEXT,refunded INTEGER DEFAULT 0,"
                "deleted INTEGER DEFAULT 0,owner TEXT,created_at INTEGER,"
                "updated_at INTEGER)"
            )
            conn.commit()
        short_drama.init_db(self.db)
        project = short_drama.create_project(self.db, "alice", {
            "title": "AI 音效测试",
            "synopsis": "一个孩子在雨中的街道奔跑并进入安静房间。",
            "ratio": "9:16",
            "target_duration": 30,
            "shot_count": 6,
            "point_budget": 1000,
        })
        self.project = short_drama.apply_plan(
            self.db, "alice", project["id"], project["revision"],
            plan(), planning_cost=0, planning_job_id=99001,
        )
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_projects SET stage='assembly_review',"
                "revision=revision+1 WHERE id=?", (self.project["id"],),
            )
            conn.commit()
        self.workspace = short_drama_sound_design.analyze(
            self.db, "alice", {
                "project_id": self.project["id"],
                "revision": self.project["revision"] + 1,
            },
        )

    def tearDown(self):
        self.temp.cleanup()

    def _confirm_first(self):
        suggestion = self.workspace["suggestions"][0]
        self.workspace = short_drama_sound_design.update_suggestions(
            self.db, "alice", {
                "project_id": self.workspace["project_id"],
                "revision": self.workspace["revision"],
                "items": [{
                    "id": suggestion["id"],
                    "prompt": suggestion["prompt"],
                    "status": "confirmed",
                    "volume": 0.4,
                    "loop": bool(suggestion["loop"]),
                }],
            },
        )
        return next(
            item for item in self.workspace["suggestions"]
            if item["id"] == suggestion["id"]
        )

    def test_analysis_is_free_replayable_and_returns_structured_suggestions(self):
        self.assertGreaterEqual(len(self.workspace["suggestions"]), 6)
        first = self.workspace["suggestions"][0]
        self.assertIn(first["kind"], short_drama_sound_design.ALLOWED_KINDS)
        self.assertGreater(first["duration_ms"], 0)
        replay = short_drama_sound_design.analyze(
            self.db, "alice", {
                "project_id": self.workspace["project_id"],
                "revision": self.workspace["revision"],
            },
        )
        self.assertEqual(self.workspace["set"]["id"], replay["set"]["id"])

    def test_quote_is_blocked_before_charge_when_provider_is_unconfigured(self):
        suggestion = self._confirm_first()
        with mock.patch.dict(os.environ, {
            "SOUND_EFFECT_PROVIDER": "elevenlabs",
            "ELEVENLABS_API_KEY": "",
        }, clear=False):
            with self.assertRaises(short_drama_sound_design.SoundDesignError) as ctx:
                short_drama_sound_design.prepare_quote(
                    self.db, "alice", "alice", {
                        "project_id": self.workspace["project_id"],
                        "revision": self.workspace["revision"],
                        "assembly_revision": self.workspace["assembly_revision"],
                        "suggestion_ids": [suggestion["id"]],
                    },
                )
        self.assertEqual("sound_effect_provider_unavailable", ctx.exception.code)

    def test_quote_is_blocked_by_project_budget_before_charge(self):
        suggestion = self._confirm_first()
        with mock.patch.dict(os.environ, {
            "SOUND_EFFECT_PROVIDER": "mock",
            "SOUND_EFFECT_ALLOW_MOCK": "1",
        }, clear=False):
            with self.assertRaises(
                short_drama_sound_design.SoundDesignError
            ) as ctx:
                short_drama_sound_design.prepare_quote(
                    self.db, "alice", "alice", {
                        "project_id": self.workspace["project_id"],
                        "revision": self.workspace["revision"],
                        "assembly_revision": self.workspace[
                            "assembly_revision"
                        ],
                        "suggestion_ids": [suggestion["id"]],
                    },
                    point_usage=lambda conn, project_id: {
                        "spent_points": 999,
                        "reserved_points": 0,
                    },
                )
        self.assertEqual("point_budget_exceeded", ctx.exception.code)

    def test_project_revision_change_requires_reanalysis_before_quote(self):
        suggestion = self._confirm_first()
        old_set_id = self.workspace["set"]["id"]
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_projects SET revision=revision+1 "
                "WHERE id=?",
                (self.workspace["project_id"],),
            )
            revision = conn.execute(
                "SELECT revision FROM short_drama_projects WHERE id=?",
                (self.workspace["project_id"],),
            ).fetchone()[0]
            conn.commit()

        with self.assertRaises(short_drama_sound_design.SoundDesignError) as ctx:
            short_drama_sound_design.update_suggestions(
                self.db, "alice", {
                    "project_id": self.workspace["project_id"],
                    "revision": revision,
                    "items": [{
                        "id": suggestion["id"],
                        "prompt": suggestion["prompt"],
                        "status": "confirmed",
                        "volume": float(suggestion["volume"]),
                        "loop": bool(suggestion["loop"]),
                    }],
                },
            )
        self.assertEqual("sound_design_stale", ctx.exception.code)

        with mock.patch.dict(os.environ, {
            "SOUND_EFFECT_PROVIDER": "mock",
            "SOUND_EFFECT_ALLOW_MOCK": "1",
        }, clear=False):
            with self.assertRaises(
                short_drama_sound_design.SoundDesignError
            ) as ctx:
                short_drama_sound_design.prepare_quote(
                    self.db, "alice", "alice", {
                        "project_id": self.workspace["project_id"],
                        "revision": revision,
                        "assembly_revision": self.workspace[
                            "assembly_revision"
                        ],
                        "suggestion_ids": [suggestion["id"]],
                    },
                )
            self.assertEqual("sound_design_stale", ctx.exception.code)

            self.workspace = short_drama_sound_design.analyze(
                self.db, "alice", {
                    "project_id": self.workspace["project_id"],
                    "revision": revision,
                },
            )
            current = self._confirm_first()
            quote = short_drama_sound_design.prepare_quote(
                self.db, "alice", "alice", {
                    "project_id": self.workspace["project_id"],
                    "revision": revision,
                    "assembly_revision": self.workspace["assembly_revision"],
                    "suggestion_ids": [current["id"]],
                },
            )

        self.assertNotEqual(old_set_id, self.workspace["set"]["id"])
        self.assertGreater(quote["total_cost"], 0)

    def test_source_hash_change_invalidates_quote_before_charge(self):
        suggestion = self._confirm_first()
        with mock.patch.dict(os.environ, {
            "SOUND_EFFECT_PROVIDER": "mock",
            "SOUND_EFFECT_ALLOW_MOCK": "1",
        }, clear=False):
            quote = short_drama_sound_design.prepare_quote(
                self.db, "alice", "alice", {
                    "project_id": self.workspace["project_id"],
                    "revision": self.workspace["revision"],
                    "assembly_revision": self.workspace["assembly_revision"],
                    "suggestion_ids": [suggestion["id"]],
                },
            )
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_shots SET video_prompt=? WHERE id=?",
                ("镜头来源已变化但项目版本尚未同步", suggestion["shot_id"]),
            )
            conn.commit()

        with self.assertRaises(short_drama_sound_design.SoundDesignError) as ctx:
            short_drama_sound_design.submit(
                self.db, "alice", "alice", {
                    "project_id": self.workspace["project_id"],
                    "revision": self.workspace["revision"],
                    "assembly_revision": self.workspace["assembly_revision"],
                    "quote_token": quote["quote_token"],
                },
                "stale-source-hash-key",
                deduct_points=lambda *args: self.fail(
                    "stale source hash must fail before charge"
                ),
                refund_points=lambda *args, **kwargs: None,
                enqueue=lambda *args: True,
            )
        self.assertEqual("sound_design_stale", ctx.exception.code)

    def test_superseded_confirmed_suggestion_cannot_be_quoted(self):
        suggestion = self._confirm_first()
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_shots SET scene_description=? WHERE id=?",
                ("全新的夜间室内场景", suggestion["shot_id"]),
            )
            conn.commit()
        self.workspace = short_drama_sound_design.analyze(
            self.db, "alice", {
                "project_id": self.workspace["project_id"],
                "revision": self.workspace["revision"],
            },
        )
        with mock.patch.dict(os.environ, {
            "SOUND_EFFECT_PROVIDER": "mock",
            "SOUND_EFFECT_ALLOW_MOCK": "1",
        }, clear=False):
            with self.assertRaises(
                short_drama_sound_design.SoundDesignError
            ) as ctx:
                short_drama_sound_design.prepare_quote(
                    self.db, "alice", "alice", {
                        "project_id": self.workspace["project_id"],
                        "revision": self.workspace["revision"],
                        "assembly_revision": self.workspace[
                            "assembly_revision"
                        ],
                        "suggestion_ids": [suggestion["id"]],
                    },
                )
        self.assertEqual("suggestion_not_confirmed", ctx.exception.code)

    def test_quote_is_invalidated_when_suggestion_set_is_replaced(self):
        suggestion = self._confirm_first()
        with mock.patch.dict(os.environ, {
            "SOUND_EFFECT_PROVIDER": "mock",
            "SOUND_EFFECT_ALLOW_MOCK": "1",
        }, clear=False):
            quote = short_drama_sound_design.prepare_quote(
                self.db, "alice", "alice", {
                    "project_id": self.workspace["project_id"],
                    "revision": self.workspace["revision"],
                    "assembly_revision": self.workspace["assembly_revision"],
                    "suggestion_ids": [suggestion["id"]],
                },
            )
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_shots SET scene_description=? WHERE id=?",
                ("报价后替换的全新场景", suggestion["shot_id"]),
            )
            conn.commit()
        short_drama_sound_design.analyze(
            self.db, "alice", {
                "project_id": self.workspace["project_id"],
                "revision": self.workspace["revision"],
            },
        )
        with self.assertRaises(short_drama_sound_design.SoundDesignError) as ctx:
            short_drama_sound_design.submit(
                self.db, "alice", "alice", {
                    "project_id": self.workspace["project_id"],
                    "revision": self.workspace["revision"],
                    "assembly_revision": self.workspace["assembly_revision"],
                    "quote_token": quote["quote_token"],
                },
                "superseded-quote-key",
                deduct_points=lambda *args: self.fail(
                    "superseded quote must not charge"
                ),
                refund_points=lambda *args, **kwargs: None,
                enqueue=lambda *args: True,
            )
        self.assertEqual("suggestion_not_confirmed", ctx.exception.code)

    def test_paid_submission_replays_and_generated_asset_can_be_applied(self):
        suggestion = self._confirm_first()
        with mock.patch.dict(os.environ, {
            "SOUND_EFFECT_PROVIDER": "mock",
            "SOUND_EFFECT_ALLOW_MOCK": "1",
        }, clear=False):
            quote = short_drama_sound_design.prepare_quote(
                self.db, "alice", "alice", {
                    "project_id": self.workspace["project_id"],
                    "revision": self.workspace["revision"],
                    "assembly_revision": self.workspace["assembly_revision"],
                    "suggestion_ids": [suggestion["id"]],
                },
            )
        body = {
            "project_id": self.workspace["project_id"],
            "revision": self.workspace["revision"],
            "assembly_revision": self.workspace["assembly_revision"],
            "quote_token": quote["quote_token"],
        }
        queued = []
        submit = short_drama_sound_design.submit(
            self.db, "alice", "alice", body, "stable-sfx-key",
            deduct_points=lambda user, cost, reason, key: 900,
            refund_points=lambda *args, **kwargs: None,
            enqueue=lambda job_id, kind, mode: queued.append(job_id) or True,
        )
        replay = short_drama_sound_design.submit(
            self.db, "alice", "alice", body, "stable-sfx-key",
            deduct_points=lambda *args: self.fail("replay must not charge"),
            refund_points=lambda *args, **kwargs: None,
            enqueue=lambda *args: True,
        )
        self.assertFalse(submit["replayed"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(submit["job_ids"], replay["job_ids"])
        self.assertEqual(submit["job_ids"], queued)

        job_id = submit["job_ids"][0]
        asset = {"id": 77, "username": "alice", "file": "audio/generated.mp3"}
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE jobs SET status='done',result=? WHERE id=?",
                ('{"quality":{"decision":"passed","duration_ms":5000}}',
                 job_id),
            )
            conn.commit()
        recovered = short_drama_sound_design.jobs(
            self.db, "alice", self.workspace["project_id"],
            audio_asset_by_job=lambda username, found_job_id: (
                asset if username == "alice" and found_job_id == job_id
                else None
            ),
        )
        self.assertEqual("done", recovered["items"][0]["status"])
        self.assertEqual(77, recovered["items"][0]["asset_id"])
        saved = short_drama_sound_design.apply_generated(
            self.db, "alice", {
                "project_id": self.workspace["project_id"],
                "revision": self.workspace["revision"],
                "assembly_revision": self.workspace["assembly_revision"],
                "job_ids": [job_id],
                "approve_manual_review": False,
            },
            assembly_module=short_drama_assembly,
            audio_asset_lookup=lambda username, asset_id: (
                asset if username == "alice" and asset_id == 77 else None
            ),
        )
        self.assertEqual(self.workspace["assembly_revision"] + 1,
                         saved["assembly_revision"])
        self.assertEqual(77, saved["config"]["sound_cues"][0]["asset_id"])
        self.assertEqual("ai-sfx-%s" % job_id,
                         saved["config"]["sound_cues"][0]["id"])

    def test_done_job_rebuilds_missing_audio_asset_after_write_failure(self):
        suggestion = self._confirm_first()
        with mock.patch.dict(os.environ, {
            "SOUND_EFFECT_PROVIDER": "mock",
            "SOUND_EFFECT_ALLOW_MOCK": "1",
        }, clear=False):
            quote = short_drama_sound_design.prepare_quote(
                self.db, "alice", "alice", {
                    "project_id": self.workspace["project_id"],
                    "revision": self.workspace["revision"],
                    "assembly_revision": self.workspace["assembly_revision"],
                    "suggestion_ids": [suggestion["id"]],
                },
            )
        submitted = short_drama_sound_design.submit(
            self.db, "alice", "alice", {
                "project_id": self.workspace["project_id"],
                "revision": self.workspace["revision"],
                "assembly_revision": self.workspace["assembly_revision"],
                "quote_token": quote["quote_token"],
            },
            "asset-recovery-key",
            deduct_points=lambda user, cost, reason, key: 900,
            refund_points=lambda *args, **kwargs: None,
            enqueue=lambda *args: True,
        )
        job_id = submitted["job_ids"][0]
        result = {
            "type": "audio",
            "file": "audio/recovered.mp3",
            "quality": {"decision": "passed", "duration_ms": 5000},
        }
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE jobs SET status='done',result=? WHERE id=?",
                (short_drama_sound_design._canonical(result), job_id),
            )
            conn.commit()

        assets = {}
        recorder_calls = []

        def lookup(username, found_job_id):
            return assets.get((username, found_job_id))

        def record(found_job_id, username, found_result):
            recorder_calls.append((found_job_id, username))
            if len(recorder_calls) == 1:
                raise sqlite3.OperationalError("simulated audio db failure")
            self.assertEqual(result, found_result)
            assets[(username, found_job_id)] = {
                "id": 88,
                "username": username,
                "job_id": found_job_id,
                "file": found_result["file"],
            }

        first = short_drama_sound_design.jobs(
            self.db, "alice", self.workspace["project_id"],
            audio_asset_by_job=lookup,
            record_audio_asset=record,
        )
        recovered = short_drama_sound_design.jobs(
            self.db, "alice", self.workspace["project_id"],
            audio_asset_by_job=lookup,
            record_audio_asset=record,
        )
        replayed = short_drama_sound_design.jobs(
            self.db, "alice", self.workspace["project_id"],
            audio_asset_by_job=lookup,
            record_audio_asset=record,
        )

        self.assertEqual("pending", first["items"][0]["status"])
        self.assertEqual("done", recovered["items"][0]["status"])
        self.assertEqual(88, recovered["items"][0]["asset_id"])
        self.assertEqual(recovered, replayed)
        self.assertEqual(2, len(recorder_calls))


if __name__ == "__main__":
    unittest.main()
