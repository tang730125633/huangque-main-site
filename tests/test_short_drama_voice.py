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


def voice_plan():
    dialogue = [
        {"id": "line-1", "character_key": "detective", "text": "谁在那里？"},
        {"id": "line-2", "character_key": "narrator", "text": "门外没有回答。"},
    ]
    characters = [
        {
            "character_key": "detective", "name": "林探长",
            "identity_text": "detective", "personality": "calm",
            "source_type": "ai_character", "avatar_id": None,
            "appearance_prompt": "coat", "wardrobe_prompt": "dark coat",
            "voice_key": "longwan",
            "voice_settings": {"speed": 1.2, "pitch": 1, "volume": 4},
            "sort_order": 0,
        },
        {
            "character_key": "narrator", "name": "旁白",
            "identity_text": "narrator", "personality": "steady",
            "source_type": "ai_character", "avatar_id": None,
            "appearance_prompt": "voice only", "wardrobe_prompt": "none",
            "voice_key": "longcheng", "voice_settings": {},
            "sort_order": 1,
        },
    ]
    shots = []
    for index in range(6):
        shots.append({
            "shot_key": "shot-%d" % (index + 1), "sort_order": index,
            "duration": 5, "scene_description": "scene",
            "camera_description": "camera",
            "character_keys": ["detective", "narrator"] if index == 0 else [],
            "dialogue_line_ids": ["line-1", "line-2"] if index == 0 else [],
            "image_prompt": "image", "video_prompt": "video",
        })
    return {
        "characters": characters,
        "script": {
            "title": "Night", "logline": "visitor", "hook": "knock",
            "conflict_text": "silence", "turn_text": "empty",
            "ending": "door opens", "dialogue_lines": dialogue,
        },
        "shots": shots,
    }


class GetHandler:
    def __init__(self, path, token="alice"):
        self.path = path
        self.token = token
        self.response = None

    def _token(self):
        return self.token

    def _send(self, status, payload):
        self.response = (status, payload)


class ShortDramaVoiceSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "content.db")
        self.db = lambda: sqlite3.connect(self.path)
        short_drama.init_db(self.db)
        payload = {
            "title": "Night", "synopsis": "A detective hears a midnight knock.",
            "ratio": "9:16", "target_duration": 30, "shot_count": 6,
        }
        project = short_drama.create_project(self.db, "alice", payload)
        self.project = short_drama.apply_plan(
            self.db, "alice", project["id"], project["revision"],
            voice_plan(), planning_cost=0, planning_job_id=501,
        )
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_projects SET stage='voice_review' WHERE id=?",
                (self.project["id"],),
            )
            conn.commit()

    def tearDown(self):
        self.tmp.cleanup()

    def test_lazy_snapshot_maps_dialogue_narration_defaults_and_silent_shots(self):
        snapshot = short_drama_voice.get_voice_workspace(
            self.db, "alice", self.project["id"]
        )
        self.assertEqual("voice_review", snapshot["stage"])
        self.assertEqual(6, len(snapshot["shots"]))
        first = snapshot["shots"][0]
        self.assertEqual(["dialogue", "narration"], [
            line["line_type"] for line in first["lines"]
        ])
        self.assertEqual(["谁在那里？", "门外没有回答。"], [
            line["source_text"] for line in first["lines"]
        ])
        self.assertEqual("longwan", first["lines"][0]["voice_key"])
        self.assertEqual(1.2, first["lines"][0]["speed"])
        self.assertEqual("pending", first["status"])
        self.assertTrue(all(shot["status"] == "silent" for shot in snapshot["shots"][1:]))

    def test_snapshot_is_idempotent_and_does_not_resync_source_changes(self):
        first = short_drama_voice.get_voice_workspace(
            self.db, "alice", self.project["id"]
        )
        line_id = first["shots"][0]["lines"][0]["id"]
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_voice_lines SET speech_text='custom' WHERE id=?",
                (line_id,),
            )
            script = conn.execute(
                "SELECT id,dialogue_lines_json FROM short_drama_scripts "
                "WHERE project_id=? ORDER BY version DESC LIMIT 1",
                (self.project["id"],),
            ).fetchone()
            lines = json.loads(script[1])
            lines[0]["text"] = "changed upstream"
            conn.execute(
                "UPDATE short_drama_scripts SET dialogue_lines_json=? WHERE id=?",
                (json.dumps(lines, ensure_ascii=False), script[0]),
            )
            conn.commit()
        second = short_drama_voice.get_voice_workspace(
            self.db, "alice", self.project["id"]
        )
        self.assertEqual(line_id, second["shots"][0]["lines"][0]["id"])
        self.assertEqual("谁在那里？", second["shots"][0]["lines"][0]["source_text"])
        self.assertEqual("custom", second["shots"][0]["lines"][0]["speech_text"])

    def test_voice_get_route_requires_auth_and_returns_owned_snapshot(self):
        handler = GetHandler(
            "/api/gen/short-drama/voice?project_id=" + self.project["id"]
        )
        handled = short_drama.dispatch_http(
            handler, "GET", self.db,
            lambda token: {"username": token, "must_change": False} if token else None,
        )
        self.assertTrue(handled)
        self.assertEqual(200, handler.response[0])
        self.assertEqual(self.project["id"], handler.response[1]["project_id"])

        anonymous = GetHandler(handler.path, token="")
        short_drama.dispatch_http(anonymous, "GET", self.db, lambda _token: None)
        self.assertEqual(401, anonymous.response[0])

        other = GetHandler(handler.path, token="mallory")
        short_drama.dispatch_http(
            other, "GET", self.db,
            lambda token: {"username": token, "must_change": False},
        )
        self.assertEqual(404, other.response[0])

        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_projects SET board_id='board-a' WHERE id=?",
                (self.project["id"],),
            )
            conn.commit()
        viewer = GetHandler(handler.path, token="viewer")
        short_drama.dispatch_http(
            viewer, "GET", self.db,
            lambda token: {"username": token, "must_change": False},
            canvas_access_resolver=lambda _handler: {
                "board_id": "board-a", "role": "viewer",
            },
        )
        self.assertEqual(200, viewer.response[0])


class ShortDramaVoiceSchemaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "content.db")
        self.db = lambda: sqlite3.connect(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def _insert_project(self, conn, project_id, username):
        conn.execute(
            "INSERT INTO short_drama_projects "
            "(id,username,title,synopsis,ratio,target_duration,shot_count,"
            "visual_style,target_platform,stage,revision,created_at,updated_at) "
            "VALUES (?,?,?,'long enough','9:16',30,6,'film','douyin',"
            "'voice_review',1,1,1)",
            (project_id, username, project_id),
        )

    def _insert_shot(self, conn, shot_id, project_id):
        conn.execute(
            "INSERT INTO short_drama_shots "
            "(id,project_id,script_version,shot_key,sort_order,duration,"
            "scene_description,camera_description,character_keys_json,"
            "dialogue_line_ids_json,image_prompt,video_prompt) "
            "VALUES (?,?,1,?,0,5,'scene','camera','[]','[]','image','video')",
            (shot_id, project_id, shot_id),
        )

    def _insert_voice_line(self, conn, line_id, project_id, shot_id, sort_order=0,
                           **changes):
        line = {
            "id": line_id,
            "project_id": project_id,
            "shot_id": shot_id,
            "line_type": "dialogue",
            "sort_order": sort_order,
            "source_text": "source",
            "speech_text": "speech",
            "subtitle_text": "subtitle",
            "input_hash": "hash",
            "created_at": 1,
            "updated_at": 1,
        }
        line.update(changes)
        columns = ",".join(line)
        placeholders = ",".join("?" for _ in line)
        conn.execute(
            "INSERT INTO short_drama_voice_lines (%s) VALUES (%s)"
            % (columns, placeholders),
            tuple(line.values()),
        )

    def _insert_quote(self, conn, token, username, project_id, voice_line_id):
        conn.execute(
            "INSERT INTO short_drama_voice_quotes "
            "(token,username,project_id,voice_line_id,request_hash,cost,expires_at,created_at) "
            "VALUES (?,?,?,?, 'hash',0,10,1)",
            (token, username, project_id, voice_line_id),
        )

    def _insert_job(self, conn, job_id, username, project_id, shot_id, voice_line_id):
        conn.execute(
            "INSERT INTO short_drama_voice_jobs "
            "(id,username,project_id,shot_id,voice_line_id,job_id,idempotency_key,"
            "quoted_cost,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,0,'pending',1,1)",
            (job_id, username, project_id, shot_id, voice_line_id, 100, job_id),
        )

    def _insert_charge(self, conn, charge_key, username, project_id, shot_id,
                       voice_line_id, quote_token):
        conn.execute(
            "INSERT INTO short_drama_voice_charge_attempts "
            "(charge_key,refund_key,username,endpoint,idempotency_key,request_hash,"
            "project_id,shot_id,voice_line_id,quote_token,cost,audio_payload_json,state,"
            "created_at,updated_at) "
            "VALUES (?,?,?,'voice',?,'hash',?,?,?,?,0,'{}','accepted',1,1)",
            (charge_key, charge_key + "-refund", username, charge_key, project_id,
             shot_id, voice_line_id, quote_token),
        )

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

    def test_voice_shots_reject_cross_project_links_on_insert_and_update(self):
        short_drama.init_db(self.db)
        with closing(self.db()) as conn:
            self._insert_project(conn, "p1", "alice")
            self._insert_project(conn, "p2", "bob")
            self._insert_shot(conn, "s1", "p1")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO short_drama_voice_shots "
                    "(shot_id,project_id,locked,timeline_revision,created_at,updated_at) "
                    "VALUES ('s1','p2',0,1,1,1)"
                )
            conn.execute(
                "INSERT INTO short_drama_voice_shots "
                "(shot_id,project_id,locked,timeline_revision,created_at,updated_at) "
                "VALUES ('s1','p1',0,1,1,1)"
            )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE short_drama_voice_shots SET project_id='p2' WHERE shot_id='s1'"
                )

    def test_voice_line_source_text_is_immutable(self):
        short_drama.init_db(self.db)
        with closing(self.db()) as conn:
            self._insert_project(conn, "p1", "alice")
            self._insert_shot(conn, "s1", "p1")
            self._insert_voice_line(conn, "line-1", "p1", "s1")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE short_drama_voice_lines SET source_text='changed' "
                    "WHERE id='line-1'"
                )
            self.assertEqual(
                "source",
                conn.execute(
                    "SELECT source_text FROM short_drama_voice_lines WHERE id='line-1'"
                ).fetchone()[0],
            )

    def test_time_columns_reject_fractional_values(self):
        short_drama.init_db(self.db)
        with closing(self.db()) as conn:
            self._insert_project(conn, "p1", "alice")
            self._insert_shot(conn, "s1", "p1")
            with self.assertRaises(sqlite3.IntegrityError):
                self._insert_voice_line(conn, "line-start", "p1", "s1", start_ms=1.5)
            with self.assertRaises(sqlite3.IntegrityError):
                self._insert_voice_line(conn, "line-end", "p1", "s1", end_ms=1.5)
            self._insert_voice_line(conn, "line-1", "p1", "s1")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO short_drama_voice_versions "
                    "(id,voice_line_id,version,job_id,duration_ms,speech_text,voice_key,"
                    "settings_json,input_hash,status,created_at) "
                    "VALUES ('version-1','line-1',1,1,1.5,'speech','voice','{}','hash',"
                    "'done',1)"
                )

    def test_jobs_quotes_and_charge_attempts_reject_cross_project_links_on_insert_and_update(self):
        short_drama.init_db(self.db)
        with closing(self.db()) as conn:
            self._insert_project(conn, "p1", "alice")
            self._insert_project(conn, "p2", "bob")
            self._insert_shot(conn, "s1", "p1")
            self._insert_shot(conn, "s2", "p2")
            self._insert_voice_line(conn, "line-1", "p1", "s1")
            self._insert_voice_line(conn, "line-2", "p2", "s2")
            with self.assertRaises(sqlite3.IntegrityError):
                self._insert_job(conn, "job-cross-project", "alice", "p2", "s1", "line-1")
            with self.assertRaises(sqlite3.IntegrityError):
                self._insert_job(conn, "job-cross-user", "bob", "p1", "s1", "line-1")
            self._insert_job(conn, "job-1", "alice", "p1", "s1", "line-1")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE short_drama_voice_jobs SET project_id='p2' WHERE id='job-1'")

            with self.assertRaises(sqlite3.IntegrityError):
                self._insert_quote(conn, "quote-cross-project", "alice", "p2", "line-1")
            with self.assertRaises(sqlite3.IntegrityError):
                self._insert_quote(conn, "quote-cross-user", "bob", "p1", "line-1")
            self._insert_quote(conn, "quote-1", "alice", "p1", "line-1")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE short_drama_voice_quotes SET project_id='p2' WHERE token='quote-1'")

            with self.assertRaises(sqlite3.IntegrityError):
                self._insert_charge(
                    conn, "charge-cross-quote", "alice", "p2", "s2", "line-2", "quote-1"
                )
            self._insert_charge(conn, "charge-1", "alice", "p1", "s1", "line-1", "quote-1")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE short_drama_voice_charge_attempts SET voice_line_id='line-2' "
                    "WHERE charge_key='charge-1'"
                )

            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE short_drama_voice_lines SET project_id='p2' WHERE id='line-1'")
