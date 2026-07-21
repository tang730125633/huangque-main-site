import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


SERVER_DIR = str(Path(__file__).resolve().parents[1] / "server")
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from content_domains import short_drama


class ShortDramaProjectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "content.db")
        self.db = lambda: sqlite3.connect(self.path)
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

    def test_validation_rejects_unsupported_duration_ratio_and_shot_count(self):
        base = {"title": "短剧", "synopsis": "足够长的故事梗概", "ratio": "9:16",
                "target_duration": 30, "shot_count": 6, "visual_style": "写实"}
        for patch in ({"ratio": "1:1"}, {"target_duration": 20}, {"shot_count": 11}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                short_drama.create_project(self.db, "alice", dict(base, **patch))

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
