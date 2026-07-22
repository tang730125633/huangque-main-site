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


class _GetHandler:
    def __init__(self, path, token="alice"):
        self.path = path
        self.token = token
        self.response = None

    def _token(self):
        return self.token

    def _send(self, status, payload):
        self.response = (status, payload)


class ShortDramaProductionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "content.db")
        self.db = lambda: sqlite3.connect(self.path)
        short_drama.init_db(self.db)
        with closing(self.db()) as conn:
            conn.execute(
                "CREATE TABLE jobs ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT, kind TEXT, cost INTEGER, "
                "status TEXT, payload TEXT, result TEXT)"
            )
            conn.commit()

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

    def _shot_id(self, sort_order=0):
        with closing(self.db()) as conn:
            return conn.execute(
                "SELECT id FROM short_drama_shots WHERE project_id=? AND sort_order=?",
                (self.project["id"], sort_order),
            ).fetchone()[0]

    def _link_job(self, *, shot_order=0, username="alice", link_username="alice",
                  job_status="done", link_status="pending", cost=60,
                  quoted_cost=60, payload=None, result=None):
        payload = payload if payload is not None else {
            "prompt": "cinematic night scene", "ratio": "9:16",
        }
        result = result if result is not None else {
            "urls": ["https://example.test/one.png", "https://example.test/two.png"],
            "ratio": "9:16",
        }
        with closing(self.db()) as conn:
            cursor = conn.execute(
                "INSERT INTO jobs(username, kind, cost, status, payload, result) "
                "VALUES (?, 'image', ?, ?, ?, ?)",
                (username, cost, job_status, json.dumps(payload), json.dumps(result)),
            )
            job_id = cursor.lastrowid
            conn.execute(
                "INSERT INTO short_drama_production_jobs "
                "(id, username, project_id, shot_id, kind, job_id, idempotency_key, "
                "quoted_cost, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'still', ?, ?, ?, ?, 1, 1)",
                ("link-%s" % job_id, link_username, self.project["id"],
                 self._shot_id(shot_order), job_id, "request-%s" % job_id,
                 quoted_cost, link_status),
            )
            conn.commit()
        return job_id

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
            conn.commit()

        with closing(self.db()) as conn:
            slots = conn.execute(
                "SELECT shot_id, type FROM short_drama_assets WHERE project_id=? ORDER BY shot_id",
                (self.project["id"],),
            ).fetchall()

        self.assertEqual(6, len(slots))
        self.assertEqual({"still"}, {slot[1] for slot in slots})

    def test_production_state_bootstraps_slots_for_existing_stills_project(self):
        state = short_drama_production.get_production(
            self.db, "alice", self.project["id"]
        )

        self.assertEqual({
            "project_id": self.project["id"],
            "revision": self.project["revision"],
            "stage": "stills_review",
            "ratio": "9:16",
            "point_budget": 0,
            "spent_points": 0,
            "reserved_points": 0,
        }, {key: state[key] for key in (
            "project_id", "revision", "stage", "ratio", "point_budget",
            "spent_points", "reserved_points",
        )})
        self.assertEqual(
            list(range(6)), [item["sort_order"] for item in state["shots"]]
        )
        self.assertEqual(
            ["shot-%s" % index for index in range(6)],
            [item["shot_key"] for item in state["shots"]],
        )
        self.assertTrue(all(item["still"]["versions"] == [] for item in state["shots"]))
        self.assertTrue(all(item["still"]["job"] is None for item in state["shots"]))

    def test_production_state_does_not_disclose_another_users_project(self):
        with self.assertRaises(LookupError):
            short_drama_production.get_production(
                self.db, "mallory", self.project["id"]
            )

    def test_production_state_reconciles_completed_image_job_only_once(self):
        job_id = self._link_job()

        first = short_drama_production.get_production(
            self.db, "alice", self.project["id"]
        )
        second = short_drama_production.get_production(
            self.db, "alice", self.project["id"]
        )

        first_still = first["shots"][0]["still"]
        second_still = second["shots"][0]["still"]
        self.assertEqual(1, first_still["current_version"])
        self.assertEqual([1, 2], [item["version"] for item in first_still["versions"]])
        self.assertEqual(first_still["versions"], second_still["versions"])
        self.assertEqual(
            ["https://example.test/one.png", "https://example.test/two.png"],
            [item["url"] for item in first_still["versions"]],
        )
        self.assertTrue(all(item["job_id"] == job_id for item in first_still["versions"]))
        self.assertIsNone(first_still["job"])
        with closing(self.db()) as conn:
            self.assertEqual(
                "done",
                conn.execute(
                    "SELECT status FROM short_drama_production_jobs WHERE job_id=?",
                    (job_id,),
                ).fetchone()[0],
            )

    def test_production_state_reports_active_job_and_reserved_points(self):
        job_id = self._link_job(
            job_status="running", link_status="pending", cost=41, quoted_cost=40
        )

        state = short_drama_production.get_production(
            self.db, "alice", self.project["id"]
        )

        self.assertEqual(40, state["reserved_points"])
        self.assertEqual({
            "id": "link-%s" % job_id,
            "job_id": job_id,
            "kind": "still",
            "status": "running",
            "quoted_cost": 40,
        }, state["shots"][0]["still"]["job"])

    def test_reconciliation_preserves_locked_current_version(self):
        short_drama_production.ensure_asset_slots(
            connection := self.db(), self.project["id"]
        )
        try:
            asset_id = connection.execute(
                "SELECT id FROM short_drama_assets WHERE project_id=? AND shot_id=?",
                (self.project["id"], self._shot_id()),
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO short_drama_asset_versions "
                "(id, asset_id, version, job_id, url, prompt, ratio, cost, status, created_at) "
                "VALUES ('selected', ?, 1, 999, 'https://example.test/selected.png', "
                "'selected', '9:16', 1, 'done', 1)",
                (asset_id,),
            )
            connection.execute(
                "UPDATE short_drama_assets SET current_version=1, locked=1 WHERE id=?",
                (asset_id,),
            )
            connection.commit()
        finally:
            connection.close()
        self._link_job()

        state = short_drama_production.get_production(
            self.db, "alice", self.project["id"]
        )
        still = state["shots"][0]["still"]

        self.assertTrue(still["locked"])
        self.assertEqual(1, still["current_version"])
        self.assertEqual([1, 2, 3], [item["version"] for item in still["versions"]])

    def test_reconciliation_rejects_untrusted_result_and_rolls_back_all_changes(self):
        job_id = self._link_job(result=[])

        with self.assertRaises(ValueError):
            short_drama_production.get_production(
                self.db, "alice", self.project["id"]
            )

        with closing(self.db()) as conn:
            self.assertEqual(
                0,
                conn.execute(
                    "SELECT COUNT(*) FROM short_drama_assets WHERE project_id=?",
                    (self.project["id"],),
                ).fetchone()[0],
            )
            self.assertEqual(
                "pending",
                conn.execute(
                    "SELECT status FROM short_drama_production_jobs WHERE job_id=?",
                    (job_id,),
                ).fetchone()[0],
            )

    def test_reconciliation_does_not_import_another_users_job_result(self):
        self._link_job(username="mallory", link_username="alice")

        state = short_drama_production.get_production(
            self.db, "alice", self.project["id"]
        )

        self.assertEqual([], state["shots"][0]["still"]["versions"])

    def test_production_state_accepts_a_db_factory_with_row_objects(self):
        def row_db():
            conn = sqlite3.connect(self.path)
            conn.row_factory = sqlite3.Row
            return conn

        state = short_drama_production.get_production(
            row_db, "alice", self.project["id"]
        )

        self.assertEqual(6, len(state["shots"]))

    def test_production_state_rejects_a_project_before_production(self):
        draft = short_drama.create_project(self.db, "alice", _project_payload())

        with self.assertRaises(ValueError):
            short_drama_production.get_production(self.db, "alice", draft["id"])

    def test_reconciliation_rejects_a_ratio_mismatch(self):
        self._link_job(payload={"prompt": "night", "ratio": "16:9"})

        with self.assertRaises(ValueError):
            short_drama_production.get_production(
                self.db, "alice", self.project["id"]
            )

    def test_reconciliation_requires_exactly_two_candidate_urls(self):
        self._link_job(result={
            "urls": ["https://example.test/only.png"], "ratio": "9:16",
        })

        with self.assertRaises(ValueError):
            short_drama_production.get_production(
                self.db, "alice", self.project["id"]
            )

    def test_reconciliation_archives_an_unknown_job_status_as_failed(self):
        job_id = self._link_job(job_status="cancelled", link_status="running")

        state = short_drama_production.get_production(
            self.db, "alice", self.project["id"]
        )

        self.assertEqual(0, state["reserved_points"])
        self.assertIsNone(state["shots"][0]["still"]["job"])
        with closing(self.db()) as conn:
            self.assertEqual(
                "failed",
                conn.execute(
                    "SELECT status FROM short_drama_production_jobs WHERE job_id=?",
                    (job_id,),
                ).fetchone()[0],
            )

    def test_production_get_route_returns_the_owned_project_snapshot(self):
        handler = _GetHandler(
            "/api/gen/short-drama/production?project_id=" + self.project["id"]
        )

        handled = short_drama.dispatch_http(
            handler, "GET", self.db,
            lambda token: {"username": token, "must_change": False} if token else None,
        )

        self.assertTrue(handled)
        self.assertEqual(200, handler.response[0])
        self.assertEqual(self.project["id"], handler.response[1]["project_id"])

    def test_production_get_route_applies_standard_authentication_checks(self):
        path = "/api/gen/short-drama/production?project_id=" + self.project["id"]
        anonymous = _GetHandler(path, token="")
        locked = _GetHandler(path, token="locked")
        verify = lambda token: (
            {"username": token, "must_change": token == "locked"} if token else None
        )

        self.assertTrue(short_drama.dispatch_http(anonymous, "GET", self.db, verify))
        self.assertEqual(401, anonymous.response[0])
        self.assertTrue(short_drama.dispatch_http(locked, "GET", self.db, verify))
        self.assertEqual(403, locked.response[0])

    def test_assets_and_jobs_reject_cross_project_shots_on_insert_and_update(self):
        other = short_drama.create_project(self.db, "alice", _project_payload())
        other = short_drama.apply_plan(
            self.db, "alice", other["id"], other["revision"],
            _six_shot_plan(), planning_cost=0, planning_job_id=2,
        )
        with closing(self.db()) as conn:
            own_shot_id = conn.execute(
                "SELECT id FROM short_drama_shots WHERE project_id=? LIMIT 1", (self.project["id"],)
            ).fetchone()[0]
            other_shot_id = conn.execute(
                "SELECT id FROM short_drama_shots WHERE project_id=? LIMIT 1", (other["id"],)
            ).fetchone()[0]

            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO short_drama_assets "
                    "(id, project_id, shot_id, type, created_at, updated_at) VALUES (?, ?, ?, 'still', 1, 1)",
                    ("cross-asset", self.project["id"], other_shot_id),
                )
            conn.execute(
                "INSERT INTO short_drama_assets "
                "(id, project_id, shot_id, type, created_at, updated_at) VALUES (?, ?, ?, 'still', 1, 1)",
                ("owned-asset", self.project["id"], own_shot_id),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE short_drama_assets SET shot_id=? WHERE id=?",
                    (other_shot_id, "owned-asset"),
                )

            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO short_drama_production_jobs "
                    "(id, username, project_id, shot_id, kind, job_id, idempotency_key, quoted_cost, status, created_at, updated_at) "
                    "VALUES (?, 'alice', ?, ?, 'still', 10, 'cross-job', 0, 'pending', 1, 1)",
                    ("cross-job", self.project["id"], other_shot_id),
                )
            conn.execute(
                "INSERT INTO short_drama_production_jobs "
                "(id, username, project_id, shot_id, kind, job_id, idempotency_key, quoted_cost, status, created_at, updated_at) "
                "VALUES (?, 'alice', ?, ?, 'still', 11, 'owned-job', 0, 'pending', 1, 1)",
                ("owned-job", self.project["id"], own_shot_id),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE short_drama_production_jobs SET project_id=? WHERE id=?",
                    (other["id"], "owned-job"),
                )


if __name__ == "__main__":
    unittest.main()
