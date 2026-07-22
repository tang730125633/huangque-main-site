import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


SERVER_DIR = str(Path(__file__).resolve().parents[1] / "server")
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from content_domains import short_drama, short_drama_production


def _project_payload():
    return {
        "title": "Production test",
        "synopsis": "A detective receives a visitor after midnight.",
        "ratio": "9:16",
        "target_duration": 30,
        "shot_count": 6,
    }


def _six_shot_plan():
    return {
        "title": "Production plan",
        "characters": [],
        "script": {"title": "Production plan", "dialogue_lines": []},
        "shots": [{
            "shot_key": "shot-%s" % index,
            "duration": 5,
            "scene_description": "Night interior",
            "camera_description": "Medium shot",
            "character_keys": [],
            "dialogue_line_ids": [],
            "image_prompt": "cinematic night scene",
            "video_prompt": "slow camera movement",
        } for index in range(6)],
    }


class ShortDramaProductionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "content.db")
        self.db = lambda: sqlite3.connect(self.path)
        short_drama.init_db(self.db)

        project = short_drama.create_project(self.db, "alice", _project_payload())
        project = short_drama.apply_plan(
            self.db, "alice", project["id"], project["revision"],
            _six_shot_plan(), planning_cost=0, planning_job_id=1,
        )
        for stage in ("characters_review", "script_review", "storyboard_review"):
            project = short_drama.confirm_stage(
                self.db, "alice", project["id"], project["revision"], stage
            )
        self.project = project

    def tearDown(self):
        self.tmp.cleanup()

    def test_init_creates_versioned_production_tables(self):
        with closing(self.db()) as conn:
            names = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
        self.assertTrue({
            "short_drama_assets",
            "short_drama_asset_versions",
            "short_drama_production_jobs",
        }.issubset(names))

    def test_stage_sequence_keeps_existing_stills_projects_eligible(self):
        self.assertEqual(self.project["stage"], "stills_review")
        self.assertEqual(short_drama.NEXT_STAGE["storyboard_review"], "stills_review")
        self.assertEqual(short_drama.NEXT_STAGE["stills_review"], "voice_review")
        self.assertEqual(short_drama.STAGES[-4:], (
            "voice_review", "video_review", "assembly_review", "completed",
        ))

    def test_ensure_asset_slots_creates_one_still_slot_per_shot(self):
        with closing(self.db()) as conn:
            short_drama_production.ensure_asset_slots(conn, self.project["id"])
            short_drama_production.ensure_asset_slots(conn, self.project["id"])
            slots = conn.execute(
                "SELECT shot_id, type FROM short_drama_assets WHERE project_id=? ORDER BY shot_id",
                (self.project["id"],),
            ).fetchall()

        self.assertEqual(6, len(slots))
        self.assertEqual({"still"}, {slot[1] for slot in slots})


if __name__ == "__main__":
    unittest.main()
