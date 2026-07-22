import json
import queue
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock


SERVER_DIR = str(Path(__file__).resolve().parents[1] / "server")
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from content_domains import core, short_drama, short_drama_production, upstream_guard, video


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

    def _still_request(self, **changes):
        body = {
            "project_id": self.project["id"],
            "revision": self.project["revision"],
            "shot_id": self._shot_id(),
            "prompt": "rainy midnight doorway, consistent detective character",
            "mode": "single",
            "count": 2,
        }
        body.update(changes)
        return body

    def _link_job(self, *, shot_order=0, username="alice", link_username="alice",
                  job_kind="image", job_status="done", link_status="pending", cost=60,
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
                "VALUES (?, ?, ?, ?, ?, ?)",
                (username, job_kind, cost, job_status,
                 json.dumps(payload), json.dumps(result)),
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

    def _completed_still_versions(self, shot_order=0, *, statuses=("done", "done"),
                                  ratios=("9:16", "9:16")):
        with closing(self.db()) as conn:
            short_drama_production.ensure_asset_slots(conn, self.project["id"])
            asset_id = conn.execute(
                "SELECT id FROM short_drama_assets WHERE project_id=? AND shot_id=?",
                (self.project["id"], self._shot_id(shot_order)),
            ).fetchone()[0]
            versions = []
            for version, (status, ratio) in enumerate(zip(statuses, ratios), 1):
                item = {
                    "id": "version-%s-%s" % (shot_order, version),
                    "version": version,
                    "url": "https://example.test/%s-%s.png" % (shot_order, version),
                    "status": status,
                    "ratio": ratio,
                }
                conn.execute(
                    "INSERT INTO short_drama_asset_versions "
                    "(id, asset_id, version, job_id, url, prompt, ratio, cost, status, created_at) "
                    "VALUES (?, ?, ?, ?, ?, 'prompt', ?, 0, ?, 1)",
                    (item["id"], asset_id, version, 10000 + shot_order * 10 + version,
                     item["url"], ratio, status),
                )
                versions.append(item)
            conn.execute(
                "UPDATE short_drama_assets SET current_version=1 WHERE id=?", (asset_id,)
            )
            conn.commit()
        return self.project, asset_id, versions

    def _lock_every_current_still(self):
        with closing(self.db()) as conn:
            short_drama_production.ensure_asset_slots(conn, self.project["id"])
            assets = conn.execute(
                "SELECT id, shot_id FROM short_drama_assets WHERE project_id=? ORDER BY shot_id",
                (self.project["id"],),
            ).fetchall()
            for index, (asset_id, _shot_id) in enumerate(assets):
                conn.execute(
                    "INSERT INTO short_drama_asset_versions "
                    "(id, asset_id, version, job_id, url, prompt, ratio, cost, status, created_at) "
                    "VALUES (?, ?, 1, ?, ?, 'prompt', '9:16', 0, 'done', 1)",
                    ("locked-version-%s" % index, asset_id, 11000 + index,
                     "https://example.test/locked-%s.png" % index),
                )
                conn.execute(
                    "UPDATE short_drama_assets SET current_version=1, locked=1 WHERE id=?",
                    (asset_id,),
                )
            conn.commit()

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

    def test_still_idempotency_descriptor_normalizes_and_binds_server_contract(self):
        request, descriptor = short_drama_production.normalize_still_request(
            self._still_request(
                project_id="  %s  " % self.project["id"],
                shot_id="  %s  " % self._shot_id(),
                prompt="  rainy midnight doorway  ",
            )
        )

        self.assertEqual(self.project["id"], request["project_id"])
        self.assertEqual(self._shot_id(), request["shot_id"])
        self.assertEqual("rainy midnight doorway", request["prompt"])
        self.assertEqual({
            "kind": "short-drama-still",
            "project_id": self.project["id"],
            "revision": self.project["revision"],
            "shot_id": self._shot_id(),
            "prompt": "rainy midnight doorway",
            "mode": "single",
            "count": 2,
            "provider": "seedream",
            "variant": "std",
            "quality": "hd",
        }, descriptor)

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

    def test_reconciliation_accounts_completed_still_cost_in_spent_points_once(self):
        self._link_job(cost=60, quoted_cost=60)

        short_drama_production.get_production(self.db, "alice", self.project["id"])
        short_drama_production.get_production(self.db, "alice", self.project["id"])

        with closing(self.db()) as conn:
            spent_points = conn.execute(
                "SELECT spent_points FROM short_drama_projects WHERE id=?",
                (self.project["id"],),
            ).fetchone()[0]
        self.assertEqual(60, spent_points)

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

    def test_reconciliation_does_not_import_non_image_job_results(self):
        for shot_order, job_kind in enumerate(("copy", "audio", "video")):
            self._link_job(shot_order=shot_order, job_kind=job_kind)

        state = short_drama_production.get_production(
            self.db, "alice", self.project["id"]
        )

        self.assertTrue(all(
            item["still"]["versions"] == [] for item in state["shots"][:3]
        ))

    def test_production_state_excludes_non_image_active_jobs_and_reservations(self):
        for shot_order, job_kind in enumerate(("copy", "audio", "video")):
            self._link_job(
                shot_order=shot_order, job_kind=job_kind,
                job_status="running", quoted_cost=40 + shot_order,
            )

        state = short_drama_production.get_production(
            self.db, "alice", self.project["id"]
        )

        self.assertEqual(0, state["reserved_points"])
        self.assertTrue(all(item["still"]["job"] is None for item in state["shots"][:3]))

    def test_production_state_accepts_a_db_factory_with_row_objects(self):
        def row_db():
            conn = sqlite3.connect(self.path)
            conn.row_factory = sqlite3.Row
            return conn

        state = short_drama_production.get_production(
            row_db, "alice", self.project["id"]
        )

        self.assertEqual(6, len(state["shots"]))

    def test_production_state_rolls_back_reconciliation_when_snapshot_build_fails(self):
        job_id = self._link_job()

        with mock.patch.object(
            short_drama_production, "build_production_snapshot",
            side_effect=RuntimeError("snapshot failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "snapshot failed"):
                short_drama_production.get_production(
                    self.db, "alice", self.project["id"]
                )

        with closing(self.db()) as conn:
            self.assertEqual(0, conn.execute(
                "SELECT COUNT(*) FROM short_drama_assets WHERE project_id=?",
                (self.project["id"],),
            ).fetchone()[0])
            self.assertEqual(0, conn.execute(
                "SELECT COUNT(*) FROM short_drama_asset_versions"
            ).fetchone()[0])
            self.assertEqual(
                "pending",
                conn.execute(
                    "SELECT status FROM short_drama_production_jobs WHERE job_id=?",
                    (job_id,),
                ).fetchone()[0],
            )

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

    def test_reconciliation_rejects_duplicate_candidate_urls_without_partial_archive(self):
        duplicate_url = "https://example.test/duplicate.png"
        job_id = self._link_job(result={
            "urls": [duplicate_url, duplicate_url], "ratio": "9:16",
        })

        with self.assertRaises(ValueError):
            short_drama_production.get_production(
                self.db, "alice", self.project["id"]
            )

        with closing(self.db()) as conn:
            self.assertEqual(0, conn.execute(
                "SELECT COUNT(*) FROM short_drama_asset_versions"
            ).fetchone()[0])
            self.assertEqual(
                "pending",
                conn.execute(
                    "SELECT status FROM short_drama_production_jobs WHERE job_id=?",
                    (job_id,),
                ).fetchone()[0],
            )

    def test_reconciliation_requires_exactly_two_archived_versions_for_asset_job(self):
        job_id = self._link_job()
        with closing(self.db()) as conn:
            short_drama_production.ensure_asset_slots(conn, self.project["id"])
            asset_id = conn.execute(
                "SELECT id FROM short_drama_assets WHERE project_id=? AND shot_id=?",
                (self.project["id"], self._shot_id()),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO short_drama_asset_versions "
                "(id, asset_id, version, job_id, url, prompt, ratio, cost, status, created_at) "
                "VALUES ('unexpected-third', ?, 1, ?, 'https://example.test/stale.png', "
                "'stale', '9:16', 1, 'done', 1)",
                (asset_id, job_id),
            )
            conn.commit()

        with self.assertRaises(ValueError):
            short_drama_production.get_production(
                self.db, "alice", self.project["id"]
            )

        with closing(self.db()) as conn:
            self.assertEqual(1, conn.execute(
                "SELECT COUNT(*) FROM short_drama_asset_versions "
                "WHERE asset_id=? AND job_id=?",
                (asset_id, job_id),
            ).fetchone()[0])
            self.assertEqual(
                "pending",
                conn.execute(
                    "SELECT status FROM short_drama_production_jobs WHERE job_id=?",
                    (job_id,),
                ).fetchone()[0],
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

    def test_selecting_a_version_preserves_history_and_can_lock(self):
        project, asset_id, versions = self._completed_still_versions()

        updated = short_drama_production.select_asset(self.db, "alice", {
            "project_id": project["id"], "revision": project["revision"],
            "asset_id": asset_id, "version": versions[1]["version"], "lock": True,
        })

        selected = updated["shots"][0]["still"]
        self.assertEqual(versions[1]["version"], selected["current_version"])
        self.assertTrue(selected["locked"])
        self.assertEqual(2, len(selected["versions"]))
        self.assertEqual(project["revision"] + 1, updated["revision"])
        self.assertEqual(0, updated["spent_points"])

    def test_select_asset_has_an_exact_typed_contract(self):
        project, asset_id, versions = self._completed_still_versions()
        valid = {
            "project_id": project["id"], "revision": project["revision"],
            "asset_id": asset_id, "version": versions[0]["version"], "lock": True,
        }
        invalid = [
            dict(valid, extra=True),
            {key: value for key, value in valid.items() if key != "asset_id"},
            dict(valid, project_id=1), dict(valid, asset_id=[]),
            dict(valid, revision=True), dict(valid, revision=0),
            dict(valid, version=True), dict(valid, version=0), dict(valid, lock=1),
        ]

        for body in invalid:
            with self.subTest(body=body), self.assertRaises(ValueError):
                short_drama_production.select_asset(self.db, "alice", body)

    def test_select_asset_rejects_non_owned_failed_and_stale_versions(self):
        project, asset_id, versions = self._completed_still_versions(statuses=("done", "failed"))
        request = {
            "project_id": project["id"], "revision": project["revision"],
            "asset_id": asset_id, "version": versions[1]["version"], "lock": True,
        }
        with self.assertRaises(LookupError):
            short_drama_production.select_asset(self.db, "alice", request)
        with self.assertRaises(LookupError):
            short_drama_production.select_asset(
                self.db, "mallory", dict(request, version=versions[0]["version"])
            )
        with self.assertRaises(short_drama.RevisionConflict):
            short_drama_production.select_asset(
                self.db, "alice", dict(request, version=versions[0]["version"],
                                        revision=project["revision"] - 1)
            )

    def test_select_asset_rejects_wrong_ratio_when_unlocking_without_mutation(self):
        project, asset_id, versions = self._completed_still_versions(
            ratios=("9:16", "16:9")
        )
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_assets SET locked=1 WHERE id=?", (asset_id,)
            )
            conn.commit()

        with self.assertRaises(LookupError):
            short_drama_production.select_asset(self.db, "alice", {
                "project_id": project["id"], "revision": project["revision"],
                "asset_id": asset_id, "version": versions[1]["version"], "lock": False,
            })

        with closing(self.db()) as conn:
            asset_state = conn.execute(
                "SELECT current_version, locked FROM short_drama_assets WHERE id=?",
                (asset_id,),
            ).fetchone()
            revision = conn.execute(
                "SELECT revision FROM short_drama_projects WHERE id=?", (project["id"],)
            ).fetchone()[0]
        self.assertEqual((1, 1), asset_state)
        self.assertEqual(project["revision"], revision)

    def test_select_asset_rejects_wrong_ratio_when_locking_without_mutation(self):
        project, asset_id, versions = self._completed_still_versions(
            ratios=("9:16", "16:9")
        )

        with self.assertRaises(LookupError):
            short_drama_production.select_asset(self.db, "alice", {
                "project_id": project["id"], "revision": project["revision"],
                "asset_id": asset_id, "version": versions[1]["version"], "lock": True,
            })

        with closing(self.db()) as conn:
            asset_state = conn.execute(
                "SELECT current_version, locked FROM short_drama_assets WHERE id=?",
                (asset_id,),
            ).fetchone()
            revision = conn.execute(
                "SELECT revision FROM short_drama_projects WHERE id=?", (project["id"],)
            ).fetchone()[0]
        self.assertEqual((1, 0), asset_state)
        self.assertEqual(project["revision"], revision)

    def test_regeneration_after_locking_keeps_selection_and_appends_history(self):
        project, asset_id, versions = self._completed_still_versions()
        selected = short_drama_production.select_asset(self.db, "alice", {
            "project_id": project["id"], "revision": project["revision"],
            "asset_id": asset_id, "version": versions[1]["version"], "lock": True,
        })
        self._link_job()

        regenerated = short_drama_production.get_production(
            self.db, "alice", project["id"]
        )

        still = regenerated["shots"][0]["still"]
        self.assertEqual(versions[1]["version"], still["current_version"])
        self.assertTrue(still["locked"])
        self.assertEqual([1, 2, 3, 4], [item["version"] for item in still["versions"]])
        with closing(self.db()) as conn:
            spent_points = conn.execute(
                "SELECT spent_points FROM short_drama_projects WHERE id=?",
                (project["id"],),
            ).fetchone()[0]
        self.assertEqual(selected["spent_points"] + 60, spent_points)

    def test_confirm_requires_every_current_shot_to_have_a_locked_still(self):
        self._completed_still_versions()

        with self.assertRaises(ValueError):
            short_drama_production.confirm_stage(self.db, "alice", {
                "project_id": self.project["id"], "revision": self.project["revision"],
                "stage": "stills_review",
            })

    def test_confirm_rejects_empty_shots_failed_current_and_wrong_ratio(self):
        empty = short_drama.create_project(self.db, "alice", _project_payload())
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_projects SET stage='stills_review' WHERE id=?",
                (empty["id"],),
            )
            conn.commit()
        with self.assertRaises(ValueError):
            short_drama_production.confirm_stage(self.db, "alice", {
                "project_id": empty["id"], "revision": empty["revision"],
                "stage": "stills_review",
            })

        self._lock_every_current_still()
        with closing(self.db()) as conn:
            first_asset = conn.execute(
                "SELECT id FROM short_drama_assets WHERE project_id=? ORDER BY shot_id LIMIT 1",
                (self.project["id"],),
            ).fetchone()[0]
            conn.execute(
                "UPDATE short_drama_asset_versions SET status='failed' "
                "WHERE asset_id=? AND version=1", (first_asset,),
            )
            conn.commit()
        body = {
            "project_id": self.project["id"], "revision": self.project["revision"],
            "stage": "stills_review",
        }
        with self.assertRaises(ValueError):
            short_drama_production.confirm_stage(self.db, "alice", body)
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_asset_versions SET status='done', ratio='16:9' "
                "WHERE asset_id=? AND version=1", (first_asset,),
            )
            conn.commit()
        with self.assertRaises(ValueError):
            short_drama_production.confirm_stage(self.db, "alice", body)

    def test_confirm_stage_has_exact_contract_owner_revision_and_single_success(self):
        self._lock_every_current_still()
        body = {
            "project_id": self.project["id"], "revision": self.project["revision"],
            "stage": "stills_review",
        }
        invalid = [
            dict(body, extra=True), dict(body, project_id=1),
            dict(body, revision=True), dict(body, revision=0),
            dict(body, stage="voice_review"),
        ]
        for request in invalid:
            with self.subTest(body=request), self.assertRaises(ValueError):
                short_drama_production.confirm_stage(self.db, "alice", request)
        with self.assertRaises(LookupError):
            short_drama_production.confirm_stage(self.db, "mallory", body)

        confirmed = short_drama_production.confirm_stage(self.db, "alice", body)
        self.assertEqual("voice_review", confirmed["stage"])
        self.assertEqual(body["revision"] + 1, confirmed["revision"])
        with self.assertRaises(short_drama.RevisionConflict):
            short_drama_production.confirm_stage(self.db, "alice", body)

    def test_concurrent_stage_confirmation_succeeds_once(self):
        self._lock_every_current_still()
        body = {
            "project_id": self.project["id"], "revision": self.project["revision"],
            "stage": "stills_review",
        }
        barrier = threading.Barrier(2)
        results = []

        def confirm():
            barrier.wait()
            try:
                results.append(short_drama_production.confirm_stage(
                    self.db, "alice", body
                )["stage"])
            except Exception as error:
                results.append(type(error))

        threads = [threading.Thread(target=confirm) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        self.assertEqual(1, results.count("voice_review"))
        self.assertEqual(1, results.count(short_drama.RevisionConflict))

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

    def test_still_submission_is_bound_to_owned_project_shot_and_ratio(self):
        prepared = short_drama_production.prepare_still_submission(
            self.db, "alice", self._still_request()
        )

        self.assertEqual(self.project["id"], prepared["project"]["id"])
        self.assertEqual(self._shot_id(), prepared["shot"]["id"])
        self.assertEqual({
            "provider": "seedream",
            "variant": "std",
            "quality": "hd",
            "prompt": "rainy midnight doorway, consistent detective character",
            "ratio": self.project["ratio"],
            "count": 2,
        }, prepared["image_payload"])

    def test_still_submission_accepts_only_the_immutable_request_contract(self):
        invalid_requests = [
            self._still_request(count=1),
            self._still_request(count=3),
            self._still_request(mode="preview"),
            self._still_request(mode=[]),
            self._still_request(provider="openai"),
            self._still_request(ratio="16:9"),
            self._still_request(cost=0),
        ]

        for body in invalid_requests:
            with self.subTest(body=body), self.assertRaises(ValueError):
                short_drama_production.prepare_still_submission(
                    self.db, "alice", body
                )

    def test_still_submission_requires_owner_exact_revision_stage_and_owned_shot(self):
        with self.assertRaises(LookupError):
            short_drama_production.prepare_still_submission(
                self.db, "mallory", self._still_request()
            )
        with self.assertRaises(short_drama.RevisionConflict):
            short_drama_production.prepare_still_submission(
                self.db, "alice", self._still_request(
                    revision=self.project["revision"] - 1
                )
            )

        other = short_drama.create_project(self.db, "alice", _project_payload())
        other = short_drama.apply_plan(
            self.db, "alice", other["id"], other["revision"],
            _six_shot_plan(), planning_cost=0, planning_job_id=2,
        )
        with closing(self.db()) as conn:
            foreign_shot = conn.execute(
                "SELECT id FROM short_drama_shots WHERE project_id=? LIMIT 1",
                (other["id"],),
            ).fetchone()[0]
        with self.assertRaises(ValueError):
            short_drama_production.prepare_still_submission(
                self.db, "alice", self._still_request(shot_id=foreign_shot)
            )

        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_projects SET stage='voice_review' WHERE id=?",
                (self.project["id"],),
            )
            conn.commit()
        with self.assertRaises(ValueError):
            short_drama_production.prepare_still_submission(
                self.db, "alice", self._still_request()
            )

    def test_batch_still_submission_rejects_a_locked_slot(self):
        with closing(self.db()) as conn:
            short_drama_production.ensure_asset_slots(conn, self.project["id"])
            conn.execute(
                "UPDATE short_drama_assets SET locked=1 "
                "WHERE project_id=? AND shot_id=? AND type='still'",
                (self.project["id"], self._shot_id()),
            )
            conn.commit()

        with self.assertRaises(ValueError):
            short_drama_production.prepare_still_submission(
                self.db, "alice", self._still_request(mode="batch")
            )

    def test_still_quote_uses_server_payload_and_counts_spent_reserved_and_new_cost(self):
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_projects SET point_budget=100, spent_points=20 WHERE id=?",
                (self.project["id"],),
            )
            conn.commit()
        self._link_job(
            job_status="running", link_status="running", cost=30, quoted_cost=30
        )
        quoted_payloads = []

        def cost_of(kind, payload):
            quoted_payloads.append((kind, dict(payload)))
            return 51

        with self.assertRaises(short_drama.PointBudgetExceeded):
            short_drama_production.prepare_still_quote(
                self.db, "alice", self._still_request(), cost_of
            )

        self.assertEqual("image", quoted_payloads[0][0])
        self.assertEqual("seedream", quoted_payloads[0][1]["provider"])
        self.assertEqual("9:16", quoted_payloads[0][1]["ratio"])
        self.assertEqual(2, quoted_payloads[0][1]["count"])

    def test_still_quote_returns_realtime_server_cost(self):
        quote = short_drama_production.prepare_still_quote(
            self.db, "alice", self._still_request(), lambda kind, payload: 24
        )

        self.assertEqual({"cost": 24, "count": 2, "kind": "still"}, quote)

    def test_record_submitted_job_binds_pending_owned_image_job(self):
        with closing(self.db()) as conn:
            cursor = conn.execute(
                "INSERT INTO jobs(username, kind, cost, status, payload, result) "
                "VALUES ('alice', 'image', 24, 'pending', '{}', NULL)"
            )
            job_id = cursor.lastrowid
            conn.commit()

        short_drama_production.record_submitted_job(
            self.db, username="alice", project_id=self.project["id"],
            shot_id=self._shot_id(), job_id=job_id,
            idempotency_key="still-submit-001", quoted_cost=24,
        )

        with closing(self.db()) as conn:
            row = conn.execute(
                "SELECT username, project_id, shot_id, kind, job_id, "
                "idempotency_key, quoted_cost, status "
                "FROM short_drama_production_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
        self.assertEqual((
            "alice", self.project["id"], self._shot_id(), "still", job_id,
            "still-submit-001", 24, "pending",
        ), row)


class ShortDramaStillRouteTests(unittest.TestCase):
    class FakePoints:
        class AuthPointsError(Exception):
            status = 402
            detail = "insufficient points"

        def __init__(self):
            self.cost = 24
            self.cost_calls = []
            self.deduct_calls = []
            self.refund_calls = []

        def cost_of(self, kind, body):
            self.cost_calls.append((kind, dict(body)))
            return self.cost

        def deduct_points(self, username, cost, reason):
            self.deduct_calls.append((username, cost, reason))
            return 100 - cost

        def refund_points(self, username, cost, reason, transaction_key=None):
            self.refund_calls.append((username, cost, reason, transaction_key))
            return 100

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.originals = {
            "JOB_DB": core.JOB_DB,
            "AUDIO_DB": core.AUDIO_DB,
            "verify": core.verify,
            "_domains": core._domains,
            "HANDLERS": core.HANDLERS,
            "feature_init_db": core.feature_flags.init_db,
            "feature_require_enabled": core.feature_flags.require_enabled,
            "init_audio_db": core.init_audio_db,
            "security": core.miniprogram_security.check_payload,
            "upstream": upstream_guard.exhausted_reason,
            "image_queue": core._image_job_queue,
            "queued_ids": core._queued_job_ids,
        }
        core.JOB_DB = str(Path(self.tmp.name) / "content.db")
        core.AUDIO_DB = str(Path(self.tmp.name) / "audio.db")
        core.verify = lambda token: (
            {"username": token, "must_change": token == "locked"} if token else None
        )
        self.points = self.FakePoints()
        core._domains = lambda: (None, self.points, video)
        core.HANDLERS = dict(core.HANDLERS, image=lambda payload: payload)
        core.feature_flags.init_db = lambda: None
        core.feature_flags.require_enabled = lambda kind: None
        core.init_audio_db = lambda: None
        self.security_calls = []
        core.miniprogram_security.check_payload = lambda payload: self.security_calls.append(
            dict(payload) if isinstance(payload, dict) else payload
        )
        self.upstream_calls = []
        upstream_guard.exhausted_reason = lambda kind, payload: self.upstream_calls.append(
            (kind, dict(payload))
        ) or None
        core._image_job_queue = queue.Queue(maxsize=8)
        core._queued_job_ids = set()
        core._shutting_down.clear()
        core.init_db()

        project = short_drama.create_project(core.jdb, "alice", _project_payload())
        project = short_drama.apply_plan(
            core.jdb, "alice", project["id"], project["revision"],
            _six_shot_plan(), planning_cost=0, planning_job_id=91001,
        )
        for stage in ("characters_review", "script_review", "storyboard_review"):
            project = short_drama.confirm_stage(
                core.jdb, "alice", project["id"], project["revision"], stage
            )
        self.project = project
        self.shot_id = project["shots"][0]["id"]

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), core.H)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def tearDown(self):
        core._shutting_down.clear()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        core.JOB_DB = self.originals["JOB_DB"]
        core.AUDIO_DB = self.originals["AUDIO_DB"]
        core.verify = self.originals["verify"]
        core._domains = self.originals["_domains"]
        core.HANDLERS = self.originals["HANDLERS"]
        core.feature_flags.init_db = self.originals["feature_init_db"]
        core.feature_flags.require_enabled = self.originals["feature_require_enabled"]
        core.init_audio_db = self.originals["init_audio_db"]
        core.miniprogram_security.check_payload = self.originals["security"]
        upstream_guard.exhausted_reason = self.originals["upstream"]
        core._image_job_queue = self.originals["image_queue"]
        core._queued_job_ids = self.originals["queued_ids"]
        self.tmp.cleanup()

    def _body(self, **changes):
        body = {
            "project_id": self.project["id"],
            "revision": self.project["revision"],
            "shot_id": self.shot_id,
            "prompt": "rainy midnight doorway, consistent detective character",
            "mode": "single",
            "count": 2,
        }
        body.update(changes)
        return body

    def request(self, path, *, body=None, username="alice", idempotency_key=None,
                raw_body=None, method="POST"):
        data = raw_body if raw_body is not None else json.dumps(
            body if body is not None else {}, ensure_ascii=False
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if username:
            headers["Authorization"] = "Bearer " + username
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        request = urllib.request.Request(
            self.base + path, data=data, method=method, headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def _jobs(self):
        with closing(core.jdb()) as conn:
            return conn.execute(
                "SELECT id, username, kind, cost, status, payload, refunded "
                "FROM jobs ORDER BY id"
            ).fetchall()

    def _idempotency_count(self):
        with closing(core.jdb()) as conn:
            return conn.execute("SELECT COUNT(*) FROM submission_idempotency").fetchone()[0]

    def _lock_every_current_still(self, *, two_versions_for_first=False):
        with closing(core.jdb()) as conn:
            short_drama_production.ensure_asset_slots(conn, self.project["id"])
            assets = conn.execute(
                "SELECT id, shot_id FROM short_drama_assets WHERE project_id=? ORDER BY shot_id",
                (self.project["id"],),
            ).fetchall()
            for index, (asset_id, shot_id) in enumerate(assets):
                version_count = 2 if shot_id == self.shot_id and two_versions_for_first else 1
                for version in range(1, version_count + 1):
                    conn.execute(
                        "INSERT INTO short_drama_asset_versions "
                        "(id, asset_id, version, job_id, url, prompt, ratio, cost, status, created_at) "
                        "VALUES (?, ?, ?, ?, ?, 'prompt', '9:16', 0, 'done', 1)",
                        ("route-version-%s-%s" % (index, version), asset_id, version,
                         12000 + index * 10 + version,
                         "https://example.test/route-%s-%s.png" % (index, version)),
                    )
                conn.execute(
                    "UPDATE short_drama_assets SET current_version=1, locked=1 WHERE id=?",
                    (asset_id,),
                )
            conn.commit()
        return next(asset_id for asset_id, shot_id in assets if shot_id == self.shot_id)

    def test_asset_quote_uses_realtime_server_built_image_payload(self):
        status, quote = self.request(
            "/api/gen/short-drama/asset-quote", body=self._body()
        )

        self.assertEqual(200, status)
        self.assertEqual({"cost": 24, "count": 2, "kind": "still"}, quote)
        self.assertEqual("image", self.points.cost_calls[0][0])
        payload = self.points.cost_calls[0][1]
        self.assertEqual({
            "provider": "seedream", "variant": "std", "quality": "hd",
            "prompt": self._body()["prompt"], "ratio": "9:16", "count": 2,
        }, payload)
        self.assertEqual([], self.points.deduct_calls)

    def test_select_and_confirm_production_routes_do_not_charge_points(self):
        first_asset = self._lock_every_current_still(two_versions_for_first=True)

        select_status, selected = self.request(
            "/api/gen/short-drama/select-asset", body={
                "project_id": self.project["id"], "revision": self.project["revision"],
                "asset_id": first_asset, "version": 2, "lock": True,
            },
        )
        confirm_status, confirmed = self.request(
            "/api/gen/short-drama/confirm-production-stage", body={
                "project_id": self.project["id"], "revision": selected["revision"],
                "stage": "stills_review",
            },
        )

        self.assertEqual(200, select_status)
        self.assertEqual(2, selected["shots"][0]["still"]["current_version"])
        self.assertEqual(200, confirm_status)
        self.assertEqual("voice_review", confirmed["stage"])
        self.assertEqual([], self.points.deduct_calls)
        self.assertEqual([], self._jobs())

    def test_production_mutation_routes_apply_auth_and_hide_missing_assets(self):
        select_path = "/api/gen/short-drama/select-asset"
        anonymous_status, _ = self.request(
            select_path, raw_body=b"{malformed", username=None
        )
        locked_status, _ = self.request(
            select_path, raw_body=b"{malformed", username="locked"
        )
        missing_status, missing = self.request(select_path, body={
            "project_id": self.project["id"], "revision": self.project["revision"],
            "asset_id": "another-users-secret-asset", "version": 1, "lock": True,
        })

        self.assertEqual(401, anonymous_status)
        self.assertEqual(403, locked_status)
        self.assertEqual(404, missing_status)
        self.assertNotIn("another-users-secret-asset", missing.get("detail", ""))
        self.assertEqual([], self.points.deduct_calls)

    def test_legacy_stills_confirmation_cannot_bypass_production_gate(self):
        status, _response = self.request(
            "/api/gen/short-drama/confirm", body={
                "project_id": self.project["id"], "revision": self.project["revision"],
                "stage": "stills_review",
            },
        )

        self.assertEqual(400, status)
        with closing(core.jdb()) as conn:
            stage = conn.execute(
                "SELECT stage FROM short_drama_projects WHERE id=?", (self.project["id"],)
            ).fetchone()[0]
        self.assertEqual("stills_review", stage)

    def test_generate_stills_requires_idempotency_and_replays_without_double_charge_or_queue(self):
        path = "/api/gen/short-drama/generate-stills"
        missing_status, _missing = self.request(path, body=self._body())
        self.assertEqual(400, missing_status)
        self.assertEqual([], self.points.deduct_calls)
        self.assertEqual([], self._jobs())

        status, accepted = self.request(
            path, body=self._body(), idempotency_key="still-submit-001"
        )
        replay_status, replayed = self.request(
            path, body=self._body(), idempotency_key="still-submit-001"
        )
        conflict_status, conflict = self.request(
            path, body=self._body(prompt="changed prompt"),
            idempotency_key="still-submit-001",
        )

        self.assertEqual(200, status)
        self.assertEqual(accepted, replayed)
        self.assertEqual(200, replay_status)
        self.assertEqual(409, conflict_status)
        self.assertEqual("idempotency_conflict", conflict["code"])
        self.assertEqual(self.project["id"], accepted["project_id"])
        self.assertEqual(self.shot_id, accepted["shot_id"])
        self.assertEqual(24, accepted["cost"])
        self.assertEqual(1, len(self.points.deduct_calls))
        self.assertEqual(1, len(self._jobs()))
        self.assertEqual(1, core._image_job_queue.qsize())
        with closing(core.jdb()) as conn:
            association = conn.execute(
                "SELECT project_id, shot_id, job_id, idempotency_key, quoted_cost "
                "FROM short_drama_production_jobs"
            ).fetchone()
        self.assertEqual((
            self.project["id"], self.shot_id, accepted["job_id"],
            "still-submit-001", 24,
        ), tuple(association))

    def test_idempotent_replay_does_not_reconsume_a_fully_reserved_budget(self):
        with closing(core.jdb()) as conn:
            conn.execute(
                "UPDATE short_drama_projects SET point_budget=24 WHERE id=?",
                (self.project["id"],),
            )
            conn.commit()
        path = "/api/gen/short-drama/generate-stills"
        status, accepted = self.request(
            path, body=self._body(), idempotency_key="still-budget-replay-001"
        )
        replay_status, replayed = self.request(
            path, body=self._body(), idempotency_key="still-budget-replay-001"
        )

        self.assertEqual(200, status)
        self.assertEqual(200, replay_status)
        self.assertEqual(accepted, replayed)
        self.assertEqual(1, len(self.points.deduct_calls))
        self.assertEqual(1, core._image_job_queue.qsize())

    def test_completed_replay_precedes_changed_project_shutdown_and_upstream(self):
        path = "/api/gen/short-drama/generate-stills"
        body = self._body()
        status, accepted = self.request(
            path, body=body, idempotency_key="still-early-replay-001"
        )
        self.assertEqual(200, status)
        with closing(core.jdb()) as conn:
            conn.execute(
                "UPDATE short_drama_projects "
                "SET revision=revision+1, stage='completed', point_budget=1 "
                "WHERE id=?",
                (self.project["id"],),
            )
            conn.commit()
        before = (
            len(self.points.cost_calls), len(self.points.deduct_calls),
            len(self._jobs()), core._image_job_queue.qsize(),
        )
        core._shutting_down.set()
        upstream_guard.exhausted_reason = lambda *_args: "upstream exhausted"
        try:
            replay_status, replayed = self.request(
                path, body=body, idempotency_key="still-early-replay-001"
            )
        finally:
            core._shutting_down.clear()

        self.assertEqual(200, replay_status)
        self.assertEqual(accepted, replayed)
        self.assertEqual(before, (
            len(self.points.cost_calls), len(self.points.deduct_calls),
            len(self._jobs()), core._image_job_queue.qsize(),
        ))

    def test_equivalent_whitespace_replays_but_changed_context_conflicts(self):
        path = "/api/gen/short-drama/generate-stills"
        spaced = self._body(
            project_id="  %s " % self.project["id"],
            shot_id=" %s  " % self.shot_id,
            prompt="  rainy midnight doorway  ",
        )
        trimmed = self._body(prompt="rainy midnight doorway")

        status, accepted = self.request(
            path, body=spaced, idempotency_key="still-normalized-001"
        )
        replay_status, replayed = self.request(
            path, body=trimmed, idempotency_key="still-normalized-001"
        )
        conflict_status, conflict = self.request(
            path, body=self._body(mode="retry", prompt="rainy midnight doorway"),
            idempotency_key="still-normalized-001",
        )

        self.assertEqual(200, status)
        self.assertEqual(200, replay_status)
        self.assertEqual(accepted, replayed)
        self.assertEqual(409, conflict_status)
        self.assertEqual("idempotency_conflict", conflict["code"])
        self.assertEqual(1, len(self.points.deduct_calls))

    def test_locked_batch_and_budget_fail_before_deduction(self):
        with closing(core.jdb()) as conn:
            short_drama_production.ensure_asset_slots(conn, self.project["id"])
            conn.execute(
                "UPDATE short_drama_assets SET locked=1 WHERE project_id=? AND shot_id=?",
                (self.project["id"], self.shot_id),
            )
            conn.commit()
        locked_status, _locked = self.request(
            "/api/gen/short-drama/generate-stills",
            body=self._body(mode="batch"), idempotency_key="still-locked-001",
        )
        self.assertEqual(400, locked_status)
        self.assertEqual([], self.points.deduct_calls)
        self.assertEqual([], self._jobs())

        with closing(core.jdb()) as conn:
            conn.execute(
                "UPDATE short_drama_assets SET locked=0 WHERE project_id=? AND shot_id=?",
                (self.project["id"], self.shot_id),
            )
            conn.execute(
                "UPDATE short_drama_projects SET point_budget=23 WHERE id=?",
                (self.project["id"],),
            )
            conn.commit()
        budget_status, budget = self.request(
            "/api/gen/short-drama/generate-stills", body=self._body(),
            idempotency_key="still-budget-001",
        )
        self.assertEqual(400, budget_status)
        self.assertEqual("point_budget_exceeded", budget["code"])
        self.assertEqual([], self.points.deduct_calls)
        self.assertEqual([], self._jobs())
        self.assertEqual(0, self._idempotency_count())

    def test_association_failure_refunds_and_aborts_idempotency(self):
        path = "/api/gen/short-drama/generate-stills"
        with mock.patch.object(
            short_drama_production, "record_submitted_job",
            side_effect=RuntimeError("association failed"),
        ):
            status, failed = self.request(
                path, body=self._body(), idempotency_key="still-assoc-001"
            )

        self.assertEqual(500, status)
        self.assertEqual(1, len(self.points.deduct_calls))
        self.assertEqual(1, len(self.points.refund_calls))
        self.assertEqual("error", self._jobs()[0]["status"])
        self.assertEqual(1, self._jobs()[0]["refunded"])
        self.assertEqual(0, self._idempotency_count())

        retry_status, retried = self.request(
            path, body=self._body(), idempotency_key="still-assoc-001"
        )
        self.assertEqual(200, retry_status)
        self.assertEqual(self.project["id"], retried["project_id"])
        self.assertEqual(2, len(self.points.deduct_calls))

    def test_queue_full_refunds_and_aborts_idempotency(self):
        core._image_job_queue = queue.Queue(maxsize=1)
        core._image_job_queue.put_nowait(999999)
        status, failed = self.request(
            "/api/gen/short-drama/generate-stills", body=self._body(),
            idempotency_key="still-queue-001",
        )

        self.assertEqual(429, status)
        self.assertEqual("queue_full", failed["code"])
        self.assertEqual(1, len(self.points.deduct_calls))
        self.assertEqual(1, len(self.points.refund_calls))
        self.assertEqual("error", self._jobs()[0]["status"])
        self.assertEqual(1, self._jobs()[0]["refunded"])
        self.assertEqual(0, self._idempotency_count())

        core._image_job_queue.get_nowait()
        retry_status, retried = self.request(
            "/api/gen/short-drama/generate-stills", body=self._body(),
            idempotency_key="still-queue-001",
        )
        self.assertEqual(200, retry_status)
        self.assertEqual(self.project["id"], retried["project_id"])
        self.assertEqual(2, len(self.points.deduct_calls))
        self.assertEqual(1, len(self.points.refund_calls))
        self.assertEqual(1, core._image_job_queue.qsize())

    def test_auth_content_shutdown_and_upstream_guards_precede_paid_work(self):
        path = "/api/gen/short-drama/generate-stills"
        anonymous_status, _ = self.request(
            path, raw_body=b"{malformed", username=None,
            idempotency_key="still-guard-001",
        )
        locked_status, _ = self.request(
            path, raw_body=b"{malformed", username="locked",
            idempotency_key="still-guard-002",
        )
        self.assertEqual(401, anonymous_status)
        self.assertEqual(403, locked_status)
        self.assertEqual([], self.security_calls)

        core.miniprogram_security.check_payload = mock.Mock(
            side_effect=core.miniprogram_security.ContentRejected("rejected")
        )
        rejected_status, rejected = self.request(
            path, body=self._body(), idempotency_key="still-guard-003"
        )
        self.assertEqual(400, rejected_status)
        self.assertEqual("content_rejected", rejected["code"])
        self.assertEqual([], self.points.cost_calls)

        core.miniprogram_security.check_payload = lambda payload: self.security_calls.append(
            dict(payload)
        )
        core._shutting_down.set()
        shutdown_status, shutdown = self.request(
            path, body=self._body(), idempotency_key="still-guard-004"
        )
        core._shutting_down.clear()
        self.assertEqual(503, shutdown_status)
        self.assertEqual("shutting_down", shutdown["code"])
        self.assertEqual([], self.upstream_calls)
        self.assertEqual([], self.points.cost_calls)

        upstream_guard.exhausted_reason = lambda kind, payload: "upstream exhausted"
        upstream_status, upstream = self.request(
            path, body=self._body(), idempotency_key="still-guard-005"
        )
        self.assertEqual(503, upstream_status)
        self.assertEqual("upstream_exhausted", upstream["code"])
        self.assertEqual([], self.points.cost_calls)
        self.assertEqual([], self.points.deduct_calls)
        self.assertEqual([], self._jobs())

    def test_stage_confirmation_shares_the_paid_submission_lock(self):
        self._lock_every_current_still()
        lock_states = []
        original = short_drama_production.confirm_stage

        def confirm(*args, **kwargs):
            lock_states.append(core._submission_lock.locked())
            return original(*args, **kwargs)

        with mock.patch.object(short_drama_production, "confirm_stage", side_effect=confirm):
            status, _confirmed = self.request(
                "/api/gen/short-drama/confirm-production-stage", body={
                    "project_id": self.project["id"],
                    "revision": self.project["revision"],
                    "stage": "stills_review",
                },
            )

        self.assertEqual(200, status)
        self.assertEqual([True], lock_states)

    def test_budget_revision_update_shares_the_paid_submission_lock(self):
        lock_states = []
        original = short_drama.update_project

        def update(*args, **kwargs):
            lock_states.append(core._submission_lock.locked())
            return original(*args, **kwargs)

        with mock.patch.object(short_drama, "update_project", side_effect=update):
            status, _updated = self.request(
                "/api/gen/short-drama/project?id=" + self.project["id"],
                body={"revision": self.project["revision"], "point_budget": 200},
                method="PUT",
            )

        self.assertEqual(200, status)
        self.assertEqual([True], lock_states)

    def test_title_revision_update_shares_the_paid_submission_lock(self):
        lock_states = []
        original = short_drama.update_project

        def update(*args, **kwargs):
            lock_states.append(core._submission_lock.locked())
            return original(*args, **kwargs)

        with mock.patch.object(short_drama, "update_project", side_effect=update):
            status, _updated = self.request(
                "/api/gen/short-drama/project?id=" + self.project["id"],
                body={"revision": self.project["revision"], "title": "Renamed project"},
                method="PUT",
            )

        self.assertEqual(200, status)
        self.assertEqual([True], lock_states)


if __name__ == "__main__":
    unittest.main()
