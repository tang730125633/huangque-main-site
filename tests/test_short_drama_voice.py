import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


SERVER_DIR = str(Path(__file__).resolve().parents[1] / "server")
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from content_domains import short_drama, short_drama_voice


class ShortDramaVoiceSchemaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "content.db")
        self.db = lambda: sqlite3.connect(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_init_creates_all_voice_tables_and_is_idempotent(self):
        short_drama.init_db(self.db)
        short_drama.init_db(self.db)
        with closing(self.db()) as conn:
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertTrue({
            "short_drama_voice_shots",
            "short_drama_voice_lines",
            "short_drama_voice_versions",
            "short_drama_voice_jobs",
            "short_drama_voice_quotes",
            "short_drama_voice_charge_attempts",
        }.issubset(tables))

    def test_voice_line_and_job_constraints_reject_cross_project_links(self):
        short_drama.init_db(self.db)
        with closing(self.db()) as conn:
            conn.execute(
                "INSERT INTO short_drama_projects "
                "(id,username,title,synopsis,ratio,target_duration,shot_count,"
                "visual_style,target_platform,stage,revision,created_at,updated_at) "
                "VALUES ('p1','alice','A','long enough','9:16',30,6,'film','douyin',"
                "'voice_review',1,1,1)"
            )
            conn.execute(
                "INSERT INTO short_drama_projects "
                "(id,username,title,synopsis,ratio,target_duration,shot_count,"
                "visual_style,target_platform,stage,revision,created_at,updated_at) "
                "VALUES ('p2','alice','B','long enough','9:16',30,6,'film','douyin',"
                "'voice_review',1,1,1)"
            )
            conn.execute(
                "INSERT INTO short_drama_shots "
                "(id,project_id,script_version,shot_key,sort_order,duration,"
                "scene_description,camera_description,character_keys_json,"
                "dialogue_line_ids_json,image_prompt,video_prompt) "
                "VALUES ('s1','p1',1,'shot-1',0,5,'scene','camera','[]','[]','image','video')"
            )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO short_drama_voice_shots "
                    "(shot_id,project_id,locked,timeline_revision,created_at,updated_at) "
                    "VALUES ('s1','p2',0,1,1,1)"
                )
