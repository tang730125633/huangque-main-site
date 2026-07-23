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
