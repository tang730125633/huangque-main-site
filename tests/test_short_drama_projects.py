import json
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path


SERVER_DIR = str(Path(__file__).resolve().parents[1] / "server")
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from content_domains import core, short_drama


def valid_project(**changes):
    project = {
        "title": "雨夜来客", "synopsis": "陌生女孩敲开侦探的门",
        "ratio": "9:16", "target_duration": 30, "shot_count": 6,
        "visual_style": "电影写实", "point_budget": 1400,
    }
    project.update(changes)
    return project


def valid_raw_plan():
    return {
        "title": "第一稿",
        "characters": [],
        "script": {"title": "第一稿", "dialogue_lines": []},
        "shots": [{
            "shot_key": "shot-%s" % index,
            "duration": 5,
            "scene_description": "场景",
            "camera_description": "镜头",
            "character_keys": [],
            "dialogue_line_ids": [],
            "image_prompt": "画面",
            "video_prompt": "视频",
        } for index in range(6)],
    }


def valid_editable_plan():
    characters = [{
        "character_key": "lin-mo",
        "name": "林默",
        "identity_text": "侦探",
        "personality": "冷静",
        "source_type": "ai_character",
        "avatar_id": None,
        "appearance_prompt": "黑色风衣",
        "wardrobe_prompt": "深色西装",
        "voice_key": "calm",
        "voice_settings": {"speed": 1},
    }, {
        "character_key": "su-qing",
        "name": "苏晴",
        "identity_text": "记者",
        "personality": "果断",
        "source_type": "ai_character",
        "avatar_id": None,
        "appearance_prompt": "米色大衣",
        "wardrobe_prompt": "浅色长裙",
        "voice_key": None,
        "voice_settings": {},
    }]
    dialogue_lines = [{
        "id": "line-1", "character_key": "lin-mo", "text": "我们得赶在天亮前。",
    }, {
        "id": "line-2", "character_key": "lin-mo", "text": "线索就在门后。",
    }]
    return {
        "title": "第一稿",
        "characters": characters,
        "script": {
            "title": "第一稿", "logline": "两人追查雨夜秘密",
            "hook": "门外有脚步声", "conflict_text": "线索即将被毁",
            "turn_text": "同伴隐瞒了真相", "ending": "门终于打开",
            "dialogue_lines": dialogue_lines,
        },
        "shots": [{
            "shot_key": "shot-%s" % index,
            "duration": 5,
            "scene_description": "雨夜门厅",
            "camera_description": "稳定推轨",
            "character_keys": [characters[index % 2]["character_key"]],
            "dialogue_line_ids": [dialogue_lines[index % 2]["id"]],
            "image_prompt": "潮湿门厅画面",
            "video_prompt": "人物走向木门",
        } for index in range(6)],
    }


class ShortDramaProjectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "content.db")
        self.db = lambda: sqlite3.connect(self.path)
        self.next_job_id = 600
        short_drama.init_db(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def _plan(self, shot_count, duration=5):
        return {
            "characters": [],
            "script": {"title": "第一稿", "dialogue_lines": []},
            "shots": [{
                "shot_key": "shot-%s" % index,
                "duration": duration,
                "scene_description": "场景",
                "camera_description": "镜头",
                "character_keys": [],
                "dialogue_line_ids": [],
                "image_prompt": "画面",
                "video_prompt": "视频",
            } for index in range(shot_count)],
        }

    def _assert_plan_rejected_without_side_effects(self, project, plan, job_id):
        before = short_drama.get_project(self.db, "alice", project["id"])
        with self.assertRaises(ValueError):
            short_drama.apply_plan(self.db, "alice", project["id"], before["revision"],
                                   plan, 140, job_id)
        after = short_drama.get_project(self.db, "alice", project["id"])
        self.assertEqual(after["revision"], before["revision"])
        self.assertEqual(after["stage"], before["stage"])
        self.assertEqual(after["spent_points"], before["spent_points"])
        conn = self.db()
        try:
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM short_drama_applied_jobs WHERE job_id=?", (job_id,)
            ).fetchone())
        finally:
            conn.close()

    def applied_project(self):
        self.next_job_id += 1
        project = short_drama.create_project(self.db, "alice", valid_project())
        return short_drama.apply_plan(
            self.db, "alice", project["id"], project["revision"],
            valid_editable_plan(), planning_cost=3, planning_job_id=self.next_job_id,
        )

    def applied_project_with_two_characters_and_dialogue(self):
        return self.applied_project()

    def _content_snapshot(self, project_id):
        project = short_drama.get_project(self.db, "alice", project_id)
        return {
            key: project[key]
            for key in (
                "revision", "stage", "characters", "script_versions", "shots", "spent_points",
            )
        }

    def _assert_content_rejected_without_side_effects(self, project, call, error=ValueError):
        before = self._content_snapshot(project["id"])
        with self.assertRaises(error):
            call(before)
        self.assertEqual(before, self._content_snapshot(project["id"]))

    def test_content_sections_save_only_in_their_review_stage(self):
        project = self.applied_project()
        edited = [dict(project["characters"][0], name="林默（新）")]
        project = short_drama.update_project(
            self.db, "alice", project["id"], project["revision"],
            {"characters": edited},
        )
        self.assertEqual(project["revision"], 3)
        self.assertEqual(project["stage"], "characters_review")
        self.assertEqual(project["characters"][0]["name"], "林默（新）")
        self.assertEqual(project, short_drama.get_project(self.db, "alice", project["id"]))

        with self.assertRaisesRegex(ValueError, "当前阶段"):
            short_drama.update_project(
                self.db, "alice", project["id"], project["revision"],
                {"script": dict(project["script_versions"][-1])},
            )

        project = short_drama.confirm_stage(
            self.db, "alice", project["id"], project["revision"], "characters_review"
        )
        script = dict(project["script_versions"][-1], ending="新的结尾")
        project = short_drama.update_project(
            self.db, "alice", project["id"], project["revision"], {"script": script}
        )
        self.assertEqual(len(project["script_versions"]), 2)
        self.assertEqual(project["script_versions"][-1]["ending"], "新的结尾")
        self.assertTrue(all(
            shot["script_version"] == project["script_versions"][-1]["version"]
            for shot in project["shots"]
        ))

        project = short_drama.confirm_stage(
            self.db, "alice", project["id"], project["revision"], "script_review"
        )
        shots = [dict(shot, scene_description="修改后的场景") for shot in project["shots"]]
        project = short_drama.update_project(
            self.db, "alice", project["id"], project["revision"], {"shots": shots}
        )
        self.assertEqual(len(project["shots"]), 6)
        self.assertTrue(all(x["scene_description"] == "修改后的场景" for x in project["shots"]))
        self.assertEqual(project, short_drama.get_project(self.db, "alice", project["id"]))

    def test_character_and_script_edits_prune_only_invalid_unconfirmed_references(self):
        project = self.applied_project_with_two_characters_and_dialogue()
        kept_key = project["characters"][0]["character_key"]
        expected_character_refs = {
            shot["shot_key"]: [key for key in shot["character_keys"] if key == kept_key]
            for shot in project["shots"]
        }
        project = short_drama.update_project(
            self.db, "alice", project["id"], project["revision"],
            {"characters": [project["characters"][0]]},
        )
        self.assertEqual(expected_character_refs, {
            shot["shot_key"]: shot["character_keys"] for shot in project["shots"]
        })

        project = short_drama.confirm_stage(
            self.db, "alice", project["id"], project["revision"], "characters_review"
        )
        historical_script = dict(project["script_versions"][-1])
        script = dict(project["script_versions"][-1])
        script["dialogue_lines"] = script["dialogue_lines"][:1]
        valid_dialogue = {line["id"] for line in script["dialogue_lines"]}
        expected_dialogue_refs = {
            shot["shot_key"]: [
                line_id for line_id in shot["dialogue_line_ids"] if line_id in valid_dialogue
            ]
            for shot in project["shots"]
        }
        project = short_drama.update_script(
            self.db, "alice", project["id"], project["revision"], script
        )
        self.assertEqual(expected_dialogue_refs, {
            shot["shot_key"]: shot["dialogue_line_ids"] for shot in project["shots"]
        })
        self.assertEqual(historical_script, project["script_versions"][0])

    def test_character_edits_require_complete_character_contract_atomically(self):
        def without(character, field):
            edited = dict(character)
            edited.pop(field, None)
            return edited

        cases = []
        for field in (
            "character_key", "name", "identity_text", "personality",
            "appearance_prompt", "wardrobe_prompt", "source_type",
        ):
            cases.extend((
                ("missing " + field, lambda character, field=field: without(character, field)),
                ("empty " + field, lambda character, field=field: dict(character, **{field: ""})),
            ))
        cases.extend((
            ("missing voice settings", lambda character: without(character, "voice_settings")),
            ("voice settings scalar", lambda character: dict(character, voice_settings="fast")),
            ("voice key container", lambda character: dict(character, voice_key=[])),
            ("avatar id scalar", lambda character: dict(character, avatar_id=123)),
            ("cinematic avatar id", lambda character: dict(
                character, source_type="cinematic_avatar", avatar_id=None
            )),
        ))
        for name, mutate in cases:
            with self.subTest(name=name):
                project = self.applied_project()
                characters = list(project["characters"])
                characters[0] = mutate(characters[0])
                self._assert_content_rejected_without_side_effects(
                    project, lambda before: short_drama.update_characters(
                        self.db, "alice", project["id"], before["revision"], characters
                    )
                )

    def test_character_edits_accept_complete_ai_and_cinematic_avatar_contracts(self):
        project = self.applied_project()
        characters = [dict(character) for character in project["characters"]]
        project = short_drama.update_characters(
            self.db, "alice", project["id"], project["revision"], characters
        )
        self.assertEqual("ai_character", project["characters"][0]["source_type"])
        self.assertIsNone(project["characters"][0]["avatar_id"])

        characters = [dict(character) for character in project["characters"]]
        characters[0].update({
            "source_type": "cinematic_avatar", "avatar_id": "cinematic-avatar-1",
        })
        project = short_drama.update_characters(
            self.db, "alice", project["id"], project["revision"], characters
        )
        self.assertEqual("cinematic_avatar", project["characters"][0]["source_type"])
        self.assertEqual("cinematic-avatar-1", project["characters"][0]["avatar_id"])

    def test_apply_plan_keeps_normalized_content_and_duplicate_job_id_atomic(self):
        plan = valid_editable_plan()
        project = short_drama.create_project(self.db, "alice", valid_project())
        project = short_drama.apply_plan(
            self.db, "alice", project["id"], project["revision"], plan,
            planning_cost=3, planning_job_id=777,
        )
        self.assertEqual(["lin-mo", "su-qing"], [
            character["character_key"] for character in project["characters"]
        ])
        self.assertEqual({"speed": 1}, project["characters"][0]["voice_settings"])
        self.assertEqual(plan["script"]["hook"], project["script_versions"][0]["hook"])
        self.assertEqual(["shot-%s" % index for index in range(6)], [
            shot["shot_key"] for shot in project["shots"]
        ])
        before = self._content_snapshot(project["id"])
        with self.assertRaises(short_drama.AppliedJobConflict):
            short_drama.apply_plan(
                self.db, "alice", project["id"], project["revision"], plan,
                planning_cost=3, planning_job_id=777,
            )
        self.assertEqual(before, self._content_snapshot(project["id"]))

    def test_content_update_access_dispatch_and_stage_rejections_are_atomic(self):
        cases = (
            ("cross owner", lambda p: short_drama.update_characters(
                self.db, "bob", p["id"], p["revision"], p["characters"]
            ), LookupError),
            ("stale revision", lambda p: short_drama.update_characters(
                self.db, "alice", p["id"], p["revision"] - 1, p["characters"]
            ), short_drama.RevisionConflict),
            ("wrong stage", lambda p: short_drama.update_script(
                self.db, "alice", p["id"], p["revision"], p["script_versions"][-1]
            ), ValueError),
            ("two sections", lambda p: short_drama.update_project(
                self.db, "alice", p["id"], p["revision"],
                {"characters": p["characters"], "script": p["script_versions"][-1]},
            ), ValueError),
            ("content plus title", lambda p: short_drama.update_project(
                self.db, "alice", p["id"], p["revision"],
                {"characters": p["characters"], "title": "混合更新"},
            ), ValueError),
            ("non-integer revision", lambda p: short_drama.update_characters(
                self.db, "alice", p["id"], str(p["revision"]), p["characters"]
            ), ValueError),
        )
        for name, call, error in cases:
            with self.subTest(name=name):
                project = self.applied_project()
                self._assert_content_rejected_without_side_effects(
                    project, lambda _before: call(project), error
                )

    def test_malformed_content_and_duplicate_keys_are_rejected_atomically(self):
        cases = []
        project = self.applied_project()
        cases.extend((
            ("characters section", project, lambda p: short_drama.update_characters(
                self.db, "alice", p["id"], p["revision"], {"not": "a list"}
            )),
            ("character field", project, lambda p: short_drama.update_characters(
                self.db, "alice", p["id"], p["revision"],
                [dict(p["characters"][0], name=["not text"])]
            )),
            ("character voice settings", project, lambda p: short_drama.update_characters(
                self.db, "alice", p["id"], p["revision"],
                [dict(p["characters"][0], voice_settings=[])]
            )),
            ("duplicate character key", project, lambda p: short_drama.update_characters(
                self.db, "alice", p["id"], p["revision"],
                [p["characters"][0], dict(p["characters"][0], name="重复角色")]
            )),
        ))
        for name, original, call in cases:
            with self.subTest(name=name):
                self._assert_content_rejected_without_side_effects(
                    original, lambda _before: call(short_drama.get_project(
                        self.db, "alice", original["id"]
                    ))
                )

        for name, mutate in (
            ("script section", lambda _script: []),
            ("script title", lambda script: dict(script, title={"not": "text"})),
            ("script dialogue container", lambda script: dict(script, dialogue_lines={})),
            ("duplicate dialogue id", lambda script: dict(
                script, dialogue_lines=[script["dialogue_lines"][0], script["dialogue_lines"][0]]
            )),
        ):
            with self.subTest(name=name):
                project = self.applied_project()
                project = short_drama.confirm_stage(
                    self.db, "alice", project["id"], project["revision"], "characters_review"
                )
                self._assert_content_rejected_without_side_effects(
                    project, lambda before: short_drama.update_script(
                        self.db, "alice", project["id"], before["revision"],
                        mutate(project["script_versions"][-1]),
                    )
                )

    def test_invalid_storyboard_updates_are_rejected_atomically(self):
        def storyboard_project():
            project = self.applied_project()
            project = short_drama.confirm_stage(
                self.db, "alice", project["id"], project["revision"], "characters_review"
            )
            return short_drama.confirm_stage(
                self.db, "alice", project["id"], project["revision"], "script_review"
            )

        def replace(shots, index, **changes):
            edited = list(shots)
            edited[index] = dict(edited[index], **changes)
            return edited

        cases = (
            ("shots section", lambda _shots: {"not": "a list"}),
            ("shot field", lambda shots: replace(shots, 0, scene_description=[])),
            ("duplicate shot key", lambda shots: replace(
                shots, 1, shot_key=shots[0]["shot_key"]
            )),
            ("shot count", lambda shots: shots[:-1]),
            ("duration type", lambda shots: replace(shots, 0, duration="5")),
            ("duration value", lambda shots: replace(shots, 0, duration=7)),
            ("duration total", lambda shots: replace(shots, 0, duration=10)),
            ("character reference", lambda shots: replace(
                shots, 0, character_keys=["missing-character"]
            )),
            ("dialogue reference", lambda shots: replace(
                shots, 0, dialogue_line_ids=["missing-dialogue"]
            )),
        )
        for name, mutate in cases:
            with self.subTest(name=name):
                project = storyboard_project()
                self._assert_content_rejected_without_side_effects(
                    project, lambda before: short_drama.update_shots(
                        self.db, "alice", project["id"], before["revision"],
                        mutate(project["shots"]),
                    )
                )

    def test_create_get_and_list_are_owner_scoped(self):
        created = short_drama.create_project(self.db, "alice", {
            "title": "雨夜来客", "synopsis": "陌生女孩敲开侦探的门",
            "ratio": "9:16", "target_duration": 30, "shot_count": 6,
            "visual_style": "电影写实", "point_budget": 1400,
        })
        self.assertEqual(created["revision"], 1)
        self.assertEqual(short_drama.get_project(self.db, "alice", created["id"])["title"], "雨夜来客")
        self.assertEqual(len(short_drama.list_projects(self.db, "alice")), 1)
        with self.assertRaises(LookupError):
            short_drama.get_project(self.db, "bob", created["id"])

    def test_update_rejects_stale_revision(self):
        project = short_drama.create_project(self.db, "alice", {
            "title": "旧标题", "synopsis": "足够长的故事梗概", "ratio": "16:9",
            "target_duration": 45, "shot_count": 8, "visual_style": "电影写实",
        })
        updated = short_drama.update_project(self.db, "alice", project["id"], 1, {"title": "新标题"})
        self.assertEqual(updated["revision"], 2)
        with self.assertRaises(short_drama.RevisionConflict):
            short_drama.update_project(self.db, "alice", project["id"], 1, {"title": "冲突标题"})

    def test_mutations_are_owner_scoped(self):
        project = short_drama.create_project(self.db, "alice", valid_project())
        with self.assertRaises(LookupError):
            short_drama.update_project(self.db, "bob", project["id"], project["revision"], {"title": "夺取"})
        with self.assertRaises(LookupError):
            short_drama.apply_plan(
                self.db, "bob", project["id"], project["revision"],
                valid_raw_plan(), planning_cost=3, planning_job_id=100,
            )
        with self.assertRaises(LookupError):
            short_drama.confirm_stage(
                self.db, "bob", project["id"], project["revision"], "characters_review"
            )
        applied = short_drama.apply_plan(
            self.db, "alice", project["id"], project["revision"],
            valid_raw_plan(), planning_cost=3, planning_job_id=101,
        )
        with self.assertRaises(LookupError):
            short_drama.apply_plan(
                self.db, "bob", project["id"], applied["revision"],
                valid_raw_plan(), planning_cost=3, planning_job_id=101,
            )

    def test_apply_and_confirm_reject_stale_revisions(self):
        project = short_drama.create_project(self.db, "alice", valid_project())
        project = short_drama.update_project(
            self.db, "alice", project["id"], project["revision"], {"title": "新版"}
        )
        with self.assertRaises(short_drama.RevisionConflict):
            short_drama.apply_plan(
                self.db, "alice", project["id"], 1,
                valid_raw_plan(), planning_cost=3, planning_job_id=100,
            )
        project = short_drama.apply_plan(
            self.db, "alice", project["id"], project["revision"],
            valid_raw_plan(), planning_cost=3, planning_job_id=101,
        )
        with self.assertRaises(short_drama.RevisionConflict):
            short_drama.confirm_stage(
                self.db, "alice", project["id"], project["revision"] - 1, "characters_review"
            )

    def test_stage_transitions_cannot_skip_forward(self):
        project = short_drama.create_project(self.db, "alice", valid_project())
        project = short_drama.apply_plan(
            self.db, "alice", project["id"], project["revision"],
            valid_raw_plan(), planning_cost=3, planning_job_id=101,
        )
        assert project["stage"] == "characters_review"
        with self.assertRaisesRegex(ValueError, "不能跳过"):
            short_drama.confirm_stage(self.db, "alice", project["id"], project["revision"], "script_review")
        confirmed = short_drama.confirm_stage(
            self.db, "alice", project["id"], project["revision"], "characters_review"
        )
        self.assertEqual(confirmed["stage"], "script_review")

    def test_validation_rejects_unsupported_duration_ratio_and_shot_count(self):
        base = {"title": "短剧", "synopsis": "足够长的故事梗概", "ratio": "9:16",
                "target_duration": 30, "shot_count": 6, "visual_style": "写实"}
        for patch in ({"ratio": "1:1"}, {"target_duration": 20}, {"shot_count": 11}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                short_drama.create_project(self.db, "alice", dict(base, **patch))

    def test_validation_rejects_coercive_or_container_project_settings(self):
        cases = (
            {"ratio": []}, {"ratio": False},
            {"target_duration": []}, {"target_duration": True},
            {"target_duration": 30.0}, {"target_duration": "30"},
            {"shot_count": {}}, {"shot_count": True},
            {"shot_count": 6.0}, {"shot_count": "6"},
        )
        for patch in cases:
            with self.subTest(create=patch), self.assertRaises(ValueError):
                short_drama.create_project(self.db, "alice", valid_project(**patch))

        project = short_drama.create_project(self.db, "alice", valid_project())
        for patch in cases:
            with self.subTest(update=patch), self.assertRaises(ValueError):
                short_drama.update_project(
                    self.db, "alice", project["id"], project["revision"], patch
                )

    def test_concurrent_apply_plan_returns_one_success_and_one_domain_conflict(self):
        project = short_drama.create_project(self.db, "alice", valid_project())
        with closing(self.db()) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
        begin_barrier = threading.Barrier(2)
        read_barrier = threading.Barrier(2)

        class RacingConnection(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                normalized = " ".join(sql.split())
                cursor = super().execute(sql, parameters)
                if normalized == "BEGIN IMMEDIATE":
                    self.serialized = True
                elif normalized == "BEGIN":
                    begin_barrier.wait(timeout=3)
                elif (normalized.startswith(
                        "SELECT project_id, username FROM short_drama_applied_jobs")
                      and not getattr(self, "serialized", False)):
                    read_barrier.wait(timeout=3)
                return cursor

        def racing_db():
            return sqlite3.connect(self.path, timeout=5, factory=RacingConnection)

        outcomes = []

        def apply():
            try:
                outcomes.append(short_drama.apply_plan(
                    racing_db, "alice", project["id"], project["revision"],
                    valid_raw_plan(), planning_cost=3, planning_job_id=501,
                ))
            except Exception as error:
                outcomes.append(error)

        threads = [threading.Thread(target=apply) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=8)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        successes = [result for result in outcomes if isinstance(result, dict)]
        conflicts = [result for result in outcomes if isinstance(
            result, (short_drama.AppliedJobConflict, short_drama.RevisionConflict)
        )]
        raw_sqlite_errors = [result for result in outcomes if isinstance(result, sqlite3.Error)]
        self.assertEqual(1, len(successes), outcomes)
        self.assertEqual(1, len(conflicts), outcomes)
        self.assertEqual([], raw_sqlite_errors)

    def test_apply_plan_rejects_shot_counts_outside_project_limits(self):
        payload = {"title": "短剧", "synopsis": "足够长的故事梗概", "ratio": "9:16",
                   "target_duration": 30, "shot_count": 6, "visual_style": "写实"}
        for index, count in enumerate((5, 11), start=1):
            with self.subTest(count=count):
                project = short_drama.create_project(self.db, "alice", payload)
                self._assert_plan_rejected_without_side_effects(
                    project, self._plan(count), 900 + index
                )

    def test_apply_plan_rejects_duration_total_different_from_project_target(self):
        project = short_drama.create_project(self.db, "alice", {
            "title": "短剧", "synopsis": "足够长的故事梗概", "ratio": "9:16",
            "target_duration": 45, "shot_count": 6, "visual_style": "写实",
        })
        self._assert_plan_rejected_without_side_effects(project, self._plan(6), 999)


class ShortDramaRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.originals = {
            "JOB_DB": core.JOB_DB,
            "verify": core.verify,
            "_domains": core._domains,
            "feature_init_db": core.feature_flags.init_db,
            "init_audio_db": core.init_audio_db,
        }
        core.JOB_DB = str(Path(self.tmp.name) / "content.db")
        core.verify = lambda token: ({"username": token, "must_change": False} if token else None)
        core._domains = lambda: (None, None, None)
        core.feature_flags.init_db = lambda: None
        core.init_audio_db = lambda: None
        core.init_db()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), core.H)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        core.JOB_DB = self.originals["JOB_DB"]
        core.verify = self.originals["verify"]
        core._domains = self.originals["_domains"]
        core.feature_flags.init_db = self.originals["feature_init_db"]
        core.init_audio_db = self.originals["init_audio_db"]
        self.tmp.cleanup()

    def request(self, method, path, username="alice", body=None, raw_body=None):
        data = raw_body if raw_body is not None else (
            None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        )
        headers = {"Content-Type": "application/json"}
        if username:
            headers["Authorization"] = "Bearer " + username
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def insert_job(self, username="alice", *, kind="copy", status="done", mode="short_drama", cost=3,
                   result_json=None, plan=None):
        result = result_json if result_json is not None else json.dumps(
            {"mode": mode, "plan": plan or valid_raw_plan()}, ensure_ascii=False
        )
        with closing(core.jdb()) as db:
            cursor = db.execute(
                "INSERT INTO jobs(kind,username,cost,status,payload,result,created_at,updated_at,owner) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (kind, username, cost, status, "{}", result, 1, 1, "content"),
            )
            db.commit()
            return cursor.lastrowid

    def applied_project(self):
        status, project = self.request(
            "POST", "/api/gen/short-drama/projects", body=valid_project()
        )
        self.assertEqual(200, status)
        job_id = self.insert_job(plan=valid_editable_plan())
        status, project = self.request("POST", "/api/gen/short-drama/apply-plan", body={
            "project_id": project["id"], "revision": project["revision"], "job_id": job_id,
        })
        self.assertEqual(200, status)
        return project

    def confirm(self, project, stage):
        status, confirmed = self.request("POST", "/api/gen/short-drama/confirm", body={
            "project_id": project["id"], "revision": project["revision"], "stage": stage,
        })
        self.assertEqual(200, status)
        return confirmed

    def project_path(self, project):
        return "/api/gen/short-drama/project?" + urllib.parse.urlencode({"id": project["id"]})

    def test_review_content_puts_persist_without_jobs_or_points(self):
        project = self.applied_project()
        with closing(core.jdb()) as db:
            jobs_before = tuple(db.execute(
                "SELECT COUNT(*), COALESCE(SUM(cost), 0) FROM jobs"
            ).fetchone())
        spent_before = project["spent_points"]
        path = self.project_path(project)

        characters = [dict(character, name=character["name"] + "（编辑）")
                      for character in project["characters"]]
        status, project = self.request("PUT", path, body={
            "revision": project["revision"], "characters": characters,
        })
        self.assertEqual(200, status)
        self.assertEqual(3, project["revision"])

        project = self.confirm(project, "characters_review")
        script = dict(project["script_versions"][-1], ending="HTTP 新结尾")
        status, project = self.request("PUT", path, body={
            "revision": project["revision"], "script": script,
        })
        self.assertEqual(200, status)
        self.assertEqual(5, project["revision"])

        project = self.confirm(project, "script_review")
        shots = [dict(shot, scene_description="HTTP 编辑场景") for shot in project["shots"]]
        status, project = self.request("PUT", path, body={
            "revision": project["revision"], "shots": shots,
        })
        self.assertEqual(200, status)
        self.assertEqual(7, project["revision"])

        status, fetched = self.request("GET", path)
        self.assertEqual(200, status)
        self.assertEqual(project, fetched)
        self.assertEqual(spent_before, fetched["spent_points"])
        with closing(core.jdb()) as db:
            jobs_after = tuple(db.execute(
                "SELECT COUNT(*), COALESCE(SUM(cost), 0) FROM jobs"
            ).fetchone())
        self.assertEqual(jobs_before, jobs_after)

    def test_review_content_put_rejections_follow_http_contract(self):
        status, _ = self.request(
            "PUT", "/api/gen/short-drama/project?id=missing",
            username=None, raw_body=b"{malformed",
        )
        self.assertEqual(401, status)

        project = self.applied_project()
        path = self.project_path(project)
        status, _ = self.request("PUT", path, body={
            "revision": project["revision"], "script": project["script_versions"][-1],
        })
        self.assertEqual(400, status)

        status, conflict = self.request("PUT", path, body={
            "revision": project["revision"] - 1, "characters": project["characters"],
        })
        self.assertEqual(409, status)
        self.assertEqual("revision_conflict", conflict["code"])

        status, _ = self.request("PUT", path, body={
            "revision": project["revision"],
            "characters": project["characters"], "script": project["script_versions"][-1],
        })
        self.assertEqual(400, status)

    def test_core_declares_all_six_short_drama_routes(self):
        source = Path(core.__file__).read_text(encoding="utf-8")
        for route in (
            "/api/gen/short-drama/projects",
            "/api/gen/short-drama/project",
            "/api/gen/short-drama/apply-plan",
            "/api/gen/short-drama/confirm",
        ):
            with self.subTest(route=route):
                self.assertIn(route, source)
        self.assertNotIn('HANDLERS["short-drama"]', source)

    def test_project_crud_routes_are_authenticated_and_owner_scoped(self):
        status, _ = self.request("GET", "/api/gen/short-drama/projects", username=None)
        self.assertEqual(401, status)
        status, created = self.request("POST", "/api/gen/short-drama/projects", body=valid_project())
        self.assertEqual(200, status)
        project_id = created["id"]
        status, listed = self.request("GET", "/api/gen/short-drama/projects")
        self.assertEqual([project_id], [item["id"] for item in listed["items"]])
        status, fetched = self.request(
            "GET", "/api/gen/short-drama/project?" + urllib.parse.urlencode({"id": project_id})
        )
        self.assertEqual(200, status)
        self.assertEqual(project_id, fetched["id"])
        status, _ = self.request(
            "GET", "/api/gen/short-drama/project?" + urllib.parse.urlencode({"id": project_id}), username="bob"
        )
        self.assertEqual(404, status)
        status, updated = self.request(
            "PUT", "/api/gen/short-drama/project?" + urllib.parse.urlencode({"id": project_id}),
            body={"revision": created["revision"], "title": "新版标题"},
        )
        self.assertEqual(200, status)
        self.assertEqual("新版标题", updated["title"])
        status, conflict = self.request(
            "PUT", "/api/gen/short-drama/project?" + urllib.parse.urlencode({"id": project_id}),
            body={"revision": created["revision"], "title": "过期标题"},
        )
        self.assertEqual(409, status)
        self.assertEqual("revision_conflict", conflict["code"])

    def test_project_routes_reject_malformed_settings_with_http_400(self):
        for patch in ({"ratio": []}, {"target_duration": []}, {"target_duration": "30"},
                      {"shot_count": {}}, {"shot_count": 6.0}):
            with self.subTest(create=patch):
                status, _ = self.request(
                    "POST", "/api/gen/short-drama/projects", body=valid_project(**patch)
                )
                self.assertEqual(400, status)

        _, project = self.request("POST", "/api/gen/short-drama/projects", body=valid_project())
        path = "/api/gen/short-drama/project?" + urllib.parse.urlencode({"id": project["id"]})
        for patch in ({"ratio": []}, {"target_duration": []}, {"target_duration": 30.0},
                      {"shot_count": {}}, {"shot_count": "6"}):
            with self.subTest(update=patch):
                status, _ = self.request(
                    "PUT", path, body={"revision": project["revision"], **patch}
                )
                self.assertEqual(400, status)

    def test_short_drama_routes_authenticate_before_parsing_malformed_json(self):
        cases = (
            ("POST", "/api/gen/short-drama/projects"),
            ("PUT", "/api/gen/short-drama/project?id=missing"),
            ("POST", "/api/gen/short-drama/apply-plan"),
            ("POST", "/api/gen/short-drama/confirm"),
        )
        for method, path in cases:
            with self.subTest(method=method, path=path):
                status, _ = self.request(
                    method, path, username=None, raw_body=b"{malformed"
                )
                self.assertEqual(401, status)

    def test_apply_plan_uses_only_owned_completed_copy_job_data(self):
        _, project = self.request("POST", "/api/gen/short-drama/projects", body=valid_project())
        job_id = self.insert_job(cost=3)
        status, rejected = self.request("POST", "/api/gen/short-drama/apply-plan", body={
            "project_id": project["id"], "revision": project["revision"], "job_id": job_id,
            "plan": {"shots": []}, "cost": 999,
        })
        self.assertEqual(400, status)
        self.assertIn("字段", rejected["detail"])
        status, applied = self.request("POST", "/api/gen/short-drama/apply-plan", body={
            "project_id": project["id"], "revision": project["revision"], "job_id": job_id,
        })
        self.assertEqual(200, status)
        self.assertEqual("characters_review", applied["stage"])
        self.assertEqual(3, applied["spent_points"])
        status, duplicate = self.request("POST", "/api/gen/short-drama/apply-plan", body={
            "project_id": project["id"], "revision": applied["revision"], "job_id": job_id,
        })
        self.assertEqual(409, status)
        self.assertEqual("job_already_applied", duplicate["code"])

        other_job_id = self.insert_job(username="bob")
        status, _ = self.request("POST", "/api/gen/short-drama/apply-plan", body={
            "project_id": project["id"], "revision": applied["revision"], "job_id": other_job_id,
        })
        self.assertEqual(404, status)

    def test_apply_plan_rejects_untrusted_job_kind_status_mode_and_result(self):
        _, project = self.request("POST", "/api/gen/short-drama/projects", body=valid_project())
        cases = (
            {"kind": "image"},
            {"status": "running"},
            {"mode": "copy"},
            {"result_json": "{malformed"},
        )
        for options in cases:
            with self.subTest(options=options):
                job_id = self.insert_job(**options)
                status, _ = self.request("POST", "/api/gen/short-drama/apply-plan", body={
                    "project_id": project["id"], "revision": project["revision"], "job_id": job_id,
                })
                self.assertEqual(400, status)

    def test_confirm_route_enforces_owner_and_stage_order(self):
        _, project = self.request("POST", "/api/gen/short-drama/projects", body=valid_project())
        job_id = self.insert_job()
        _, project = self.request("POST", "/api/gen/short-drama/apply-plan", body={
            "project_id": project["id"], "revision": project["revision"], "job_id": job_id,
        })
        status, _ = self.request("POST", "/api/gen/short-drama/confirm", username="bob", body={
            "project_id": project["id"], "revision": project["revision"], "stage": "characters_review",
        })
        self.assertEqual(404, status)
        status, skipped = self.request("POST", "/api/gen/short-drama/confirm", body={
            "project_id": project["id"], "revision": project["revision"], "stage": "script_review",
        })
        self.assertEqual(400, status)
        self.assertIn("不能跳过", skipped["detail"])
        status, confirmed = self.request("POST", "/api/gen/short-drama/confirm", body={
            "project_id": project["id"], "revision": project["revision"], "stage": "characters_review",
        })
        self.assertEqual(200, status)
        self.assertEqual("script_review", confirmed["stage"])
