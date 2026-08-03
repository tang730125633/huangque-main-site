import json
import os
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

from content_domains import core, image, jobs_store, short_drama, short_drama_asset_graph, short_drama_completion, short_drama_production, short_drama_voice, submission_idempotency, upstream_guard, video


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
                "status TEXT DEFAULT 'pending', payload TEXT, result TEXT)"
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

    def _ready_asset_snapshot(self):
        short_drama_asset_graph.sync_foundation(
            self.db, "alice", "alice", self.project["id"],
        )
        workspace = short_drama_asset_graph.workspace(
            self.db, "alice", self.project["id"],
        )
        revision = workspace["graph_revision"]
        for entity in workspace["entities"]:
            if entity["asset_type"] != "scene":
                continue
            locked = short_drama_asset_graph.lock_version(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"],
                    "graph_revision": revision,
                    "version_id": entity["versions"][0]["id"],
                },
            )
            revision = locked["graph_revision"]
        snapshot = short_drama_asset_graph.build_snapshot(
            self.db, "alice", "alice", {
                "project_id": self.project["id"],
                "graph_revision": revision,
                "shot_id": self._shot_id(),
            },
        )
        self.assertEqual("ready", snapshot["status"])
        return workspace, snapshot

    def _real_auth(self):
        import auth_server
        old_db = auth_server.DB
        auth_server.DB = str(Path(self.tmp.name) / "auth-users.db")
        self.addCleanup(setattr, auth_server, "DB", old_db)
        auth_server.init_db()
        with closing(sqlite3.connect(auth_server.DB)) as conn:
            conn.execute(
                "INSERT INTO users(username,pw_hash,pw_salt,display_name,points,role,must_change) "
                "VALUES('alice','h','s','Alice',100,'member',0)"
            )
            conn.commit()
        return auth_server

    def _accepted_attempt(self, key, **body_changes):
        body = self._still_request(**body_changes)
        quote = short_drama_production.prepare_still_quote(
            self.db, "alice", body, lambda _kind, _payload: 24,
        )
        prepared = short_drama_production.prepare_still_submission(
            self.db, "alice", dict(body, quote_token=quote["quote_token"]),
            require_quote=True, idempotency_key=key,
        )
        return short_drama_production.accept_charge_attempt(
            self.db, username="alice", endpoint="/api/gen/short-drama/generate-stills",
            idempotency_key=key, prepared=prepared,
        )

    def _ensure_job_insert_columns(self):
        with closing(self.db()) as conn:
            for name, definition in (
                ("created_at", "INTEGER"), ("updated_at", "INTEGER"), ("owner", "TEXT"),
            ):
                try: conn.execute("ALTER TABLE jobs ADD COLUMN %s %s" % (name, definition))
                except sqlite3.OperationalError: pass
            conn.commit()

    def _linked_real_ledger_job(self, key, *, status="pending"):
        """Create one actually charged attempt-backed image job for failure-path tests."""
        auth = self._real_auth()
        self._ensure_job_insert_columns()
        with closing(self.db()) as conn:
            for name, definition in (
                ("refunded", "INTEGER NOT NULL DEFAULT 0"),
                ("error", "TEXT NOT NULL DEFAULT ''"),
                ("deleted", "INTEGER NOT NULL DEFAULT 0"),
            ):
                try: conn.execute("ALTER TABLE jobs ADD COLUMN %s %s" % (name, definition))
                except sqlite3.OperationalError: pass
            conn.commit()
        attempt = self._accepted_attempt(key)
        charged, error = auth.deduct_points(
            "alice", 24, "short-drama still", attempt["charge_key"],
        )
        self.assertIsNone(error)
        short_drama_production.mark_attempt_charged(
            self.db, "alice", key, charged["points"],
        )
        job_id = jobs_store.create_job_after_charge(
            self.db, "image", "alice", 24, attempt["image_payload"], "content",
            before_commit=lambda connection, jid: short_drama_production.record_attempt_job(
                self.db, "alice", key, jid, connection=connection),
        )
        if status != "pending":
            with closing(self.db()) as conn:
                conn.execute("UPDATE jobs SET status=? WHERE id=?", (status, job_id))
                conn.commit()
        return auth, job_id, short_drama_production.get_charge_attempt(self.db, "alice", key)

    def _assert_attempt_refund_ledger(self, auth, attempt, *, swept=True):
        if swept:
            self.assertEqual(1, short_drama_production.retry_attempt_refunds(self.db, auth))
        with closing(sqlite3.connect(auth.DB)) as conn:
            still_rows = conn.execute(
                "SELECT transaction_key,delta FROM points_audit WHERE transaction_key=?",
                (attempt["refund_key"],),
            ).fetchall()
            generic_rows = conn.execute(
                "SELECT transaction_key FROM points_audit WHERE transaction_key LIKE 'job-refund:%'",
            ).fetchall()
        self.assertEqual([(attempt["refund_key"], 24)], still_rows)
        self.assertEqual([], generic_rows)
        self.assertEqual(100, auth.get_points_row("alice")["points"])
        self.assertEqual("refunded", short_drama_production.get_charge_attempt(
            self.db, "alice", attempt["idempotency_key"],
        )["state"])

    def test_real_auth_worker_failure_is_owned_by_attempt_sweeper(self):
        auth, job_id, attempt = self._linked_real_ledger_job("worker-attempt-refund-001")
        old = (core.JOB_DB, core.HANDLERS, core._domains, core._start_job_heartbeat)
        core.JOB_DB = self.path
        core.HANDLERS = dict(core.HANDLERS, image=lambda _payload: (_ for _ in ()).throw(RuntimeError("provider failed")))
        core._domains = lambda: (None, auth, video)
        core._start_job_heartbeat = lambda _job_id: (lambda: None)
        self.addCleanup(setattr, core, "JOB_DB", old[0])
        self.addCleanup(setattr, core, "HANDLERS", old[1])
        self.addCleanup(setattr, core, "_domains", old[2])
        self.addCleanup(setattr, core, "_start_job_heartbeat", old[3])

        core.run_job(job_id)

        pending = short_drama_production.get_charge_attempt(self.db, "alice", attempt["idempotency_key"])
        self.assertEqual("refund_pending", pending["state"])
        unavailable = mock.Mock()
        unavailable.refund_points.side_effect = RuntimeError("Auth unavailable")
        self.assertEqual(0, short_drama_production.retry_attempt_refunds(self.db, unavailable))
        self.assertEqual("refund_pending", short_drama_production.get_charge_attempt(
            self.db, "alice", attempt["idempotency_key"],
        )["state"])
        self._assert_attempt_refund_ledger(auth, pending)

    def test_late_failure_cannot_overwrite_first_durable_attempt_terminal(self):
        _auth, job_id, _attempt = self._linked_real_ledger_job("first-terminal-wins-001")
        first = {
            "detail": "queue full", "code": "queue_full",
            "operation_terminal": True, "_http_status": 429,
        }
        short_drama_production.mark_linked_attempt_failed(
            self.db, "alice", "first-terminal-wins-001", first,
        )

        result = short_drama_production.fail_linked_job(
            self.db, job_id, "late worker failure", from_states=("running",),
        )

        self.assertFalse(result["claimed"])
        self.assertEqual(first, short_drama_production.recover_attempt_response(
            self.db, "alice", "first-terminal-wins-001",
        ))

    def test_real_auth_reaper_uses_attempt_refund_owner(self):
        key = "reaper-attempt-refund-001"
        auth, job_id, attempt = self._linked_real_ledger_job(key, status="running")
        with closing(self.db()) as conn:
            conn.execute("UPDATE jobs SET updated_at=0 WHERE id=?", (job_id,))
            conn.commit()
        old = (core.JOB_DB, core._domains)
        core.JOB_DB = self.path
        core._domains = lambda: (None, auth, video)
        try:
            with mock.patch.object(core.time, "sleep", side_effect=StopIteration):
                with self.assertRaises(StopIteration):
                    core.reaper()
        finally:
            core.JOB_DB, core._domains = old
        pending = short_drama_production.get_charge_attempt(self.db, "alice", key)
        self.assertEqual("refund_pending", pending["state"])
        self._assert_attempt_refund_ledger(auth, pending)

    def test_real_auth_startup_reclaim_uses_attempt_refund_owner(self):
        key = "startup-attempt-refund-001"
        auth, _job_id, attempt = self._linked_real_ledger_job(key, status="running")
        old = (core.JOB_DB, core._domains)
        core.JOB_DB = self.path
        core._domains = lambda: (None, auth, video)
        try:
            core.reclaim_orphaned_running()
        finally:
            core.JOB_DB, core._domains = old
        pending = short_drama_production.get_charge_attempt(self.db, "alice", key)
        self.assertEqual("refund_pending", pending["state"])
        self._assert_attempt_refund_ledger(auth, pending)

    def test_real_auth_malformed_result_and_lost_refund_response_restart_once(self):
        auth, job_id, attempt = self._linked_real_ledger_job("malformed-attempt-refund-001", status="done")
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE jobs SET result=? WHERE id=?",
                (json.dumps({"urls": ["only-one"], "ratio": "9:16"}), job_id),
            )
            conn.commit()

        short_drama_production.get_production(self.db, "alice", self.project["id"])
        pending = short_drama_production.get_charge_attempt(self.db, "alice", attempt["idempotency_key"])
        self.assertEqual("refund_pending", pending["state"])
        first, error = auth.refund_points(
            "alice", 24, "short-drama still compensation", pending["refund_key"],
        )
        self.assertIsNone(error)
        self.assertEqual(100, first["points"])
        # Simulate Auth commit followed by a lost response and a process restart.
        self.assertEqual(1, short_drama_production.retry_attempt_refunds(self.db, auth))
        self._assert_attempt_refund_ledger(auth, pending, swept=False)

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
        with closing(self.db()) as conn:
            project_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(short_drama_projects)")
            }
        self.assertIn("board_id", project_columns)

    def test_init_migrates_legacy_asset_versions_with_local_file_column(self):
        legacy_path = str(Path(self.tmp.name) / "legacy.db")
        legacy_db = lambda: sqlite3.connect(legacy_path)
        with closing(legacy_db()) as conn:
            conn.executescript("""
                CREATE TABLE short_drama_projects (id TEXT PRIMARY KEY);
                CREATE TABLE short_drama_shots (
                  id TEXT PRIMARY KEY,
                  project_id TEXT NOT NULL
                );
                CREATE TABLE short_drama_asset_versions (
                  id TEXT PRIMARY KEY,
                  asset_id TEXT NOT NULL,
                  version INTEGER NOT NULL,
                  job_id INTEGER NOT NULL,
                  url TEXT NOT NULL,
                  prompt TEXT NOT NULL,
                  ratio TEXT NOT NULL,
                  cost INTEGER NOT NULL DEFAULT 0,
                  status TEXT NOT NULL,
                  created_at INTEGER NOT NULL
                );
            """)
            conn.commit()

        short_drama_production.init_db(legacy_db)

        with closing(legacy_db()) as conn:
            columns = {
                row[1] for row in conn.execute(
                    "PRAGMA table_info(short_drama_asset_versions)"
                )
            }
            conn.execute(
                "INSERT INTO short_drama_asset_versions "
                "(id,asset_id,version,job_id,url,prompt,ratio,cost,status,created_at) "
                "VALUES('legacy','asset',1,1,'https://cos.test/legacy.png',"
                "'prompt','9:16',0,'done',1)"
            )
            local_file = conn.execute(
                "SELECT file FROM short_drama_asset_versions WHERE id='legacy'"
            ).fetchone()[0]
        self.assertIn("file", columns)
        self.assertEqual("", local_file)

    def test_charge_attempt_is_durable_and_uses_fixed_length_keys(self):
        body = self._still_request()
        quote = short_drama_production.prepare_still_quote(
            self.db, "alice", body, lambda _kind, _payload: 24,
        )
        prepared = short_drama_production.prepare_still_submission(
            self.db, "alice", dict(body, quote_token=quote["quote_token"]),
            require_quote=True, idempotency_key="x" * 128,
        )

        attempt = short_drama_production.accept_charge_attempt(
            self.db, username="alice", endpoint="/api/gen/short-drama/generate-stills",
            idempotency_key="x" * 128, prepared=prepared,
        )

        self.assertRegex(attempt["charge_key"], r"^still-charge:[0-9a-f]{64}$")
        self.assertRegex(attempt["refund_key"], r"^still-refund:[0-9a-f]{64}$")
        self.assertLessEqual(len(attempt["charge_key"]), 160)
        self.assertEqual("accepted", attempt["state"])
        with closing(self.db()) as conn:
            consumed = conn.execute(
                "SELECT consumed_idempotency_key FROM short_drama_still_quotes WHERE token=?",
                (quote["quote_token"],),
            ).fetchone()[0]
        self.assertEqual("x" * 128, consumed)

    def test_accepted_attempt_recovers_after_quote_project_and_acl_drift(self):
        body = self._still_request()
        quote = short_drama_production.prepare_still_quote(
            self.db, "alice", body, lambda _kind, _payload: 24,
        )
        prepared = short_drama_production.prepare_still_submission(
            self.db, "alice", dict(body, quote_token=quote["quote_token"]),
            require_quote=True, idempotency_key="durable-accepted-001",
        )
        accepted = short_drama_production.accept_charge_attempt(
            self.db, username="alice", endpoint="/api/gen/short-drama/generate-stills",
            idempotency_key="durable-accepted-001", prepared=prepared,
        )
        with closing(self.db()) as conn:
            conn.execute("UPDATE short_drama_still_quotes SET expires_at=0 WHERE token=?",
                         (quote["quote_token"],))
            conn.execute(
                "UPDATE short_drama_projects SET revision=revision+1,stage='completed',board_id='board-a' WHERE id=?",
                (self.project["id"],),
            )
            conn.commit()

        recovered = short_drama_production.get_charge_attempt(
            self.db, "alice", "durable-accepted-001",
        )

        self.assertEqual(accepted["charge_key"], recovered["charge_key"])
        self.assertEqual("accepted", recovered["state"])
        self.assertEqual(24, recovered["cost"])
        self.assertEqual("banana", recovered["image_payload"]["provider"])
        self.assertEqual("nb2", recovered["image_payload"]["model"])

    def test_different_key_cannot_accept_same_operation_while_ambiguous(self):
        body = self._still_request()
        quote1 = short_drama_production.prepare_still_quote(
            self.db, "alice", body, lambda _kind, _payload: 24,
        )
        prepared1 = short_drama_production.prepare_still_submission(
            self.db, "alice", dict(body, quote_token=quote1["quote_token"]),
            require_quote=True, idempotency_key="ambiguous-first-001",
        )
        short_drama_production.accept_charge_attempt(
            self.db, username="alice", endpoint="/api/gen/short-drama/generate-stills",
            idempotency_key="ambiguous-first-001", prepared=prepared1,
        )
        quote2 = short_drama_production.prepare_still_quote(
            self.db, "alice", body, lambda _kind, _payload: 24,
        )
        prepared2 = short_drama_production.prepare_still_submission(
            self.db, "alice", dict(body, quote_token=quote2["quote_token"]),
            require_quote=True, idempotency_key="ambiguous-second-001",
        )

        with self.assertRaises(short_drama_production.ChargeAttemptInProgress):
            short_drama_production.accept_charge_attempt(
                self.db, username="alice", endpoint="/api/gen/short-drama/generate-stills",
                idempotency_key="ambiguous-second-001", prepared=prepared2,
            )

    def test_refund_intent_and_terminal_http_failure_are_durable(self):
        body = self._still_request()
        quote = short_drama_production.prepare_still_quote(
            self.db, "alice", body, lambda _kind, _payload: 24,
        )
        prepared = short_drama_production.prepare_still_submission(
            self.db, "alice", dict(body, quote_token=quote["quote_token"]),
            require_quote=True, idempotency_key="refund-intent-001",
        )
        short_drama_production.accept_charge_attempt(
            self.db, username="alice", endpoint="/api/gen/short-drama/generate-stills",
            idempotency_key="refund-intent-001", prepared=prepared,
        )
        terminal = {"detail": "queue full", "code": "queue_full", "_http_status": 429}

        pending = short_drama_production.mark_attempt_refund_pending(
            self.db, "alice", "refund-intent-001", terminal,
        )
        replay = short_drama_production.recover_attempt_response(
            self.db, "alice", "refund-intent-001",
        )

        self.assertEqual("refund_pending", pending["state"])
        self.assertEqual(terminal, replay)
        short_drama_production.mark_attempt_refunded(
            self.db, "alice", "refund-intent-001",
        )
        self.assertEqual(
            "refunded",
            short_drama_production.get_charge_attempt(
                self.db, "alice", "refund-intent-001",
            )["state"],
        )

    def test_real_auth_ledger_reconciles_association_and_refund_crash_boundaries(self):
        auth = self._real_auth()
        self._ensure_job_insert_columns()
        attempt = self._accepted_attempt("real-ledger-refund-001")
        charged, error = auth.deduct_points(
            "alice", 24, "short-drama still", attempt["charge_key"],
        )
        self.assertIsNone(error)
        short_drama_production.mark_attempt_charged(
            self.db, "alice", "real-ledger-refund-001", charged["points"],
        )
        with self.assertRaisesRegex(RuntimeError, "association failed"):
            jobs_store.create_job_after_charge(
                self.db, "image", "alice", 24, {"prompt": "p"}, "content",
                before_commit=lambda _connection, _job_id: (_ for _ in ()).throw(
                    RuntimeError("association failed")),
            )
        with closing(self.db()) as conn:
            self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])

        terminal = {"detail": "association failed", "_http_status": 500}
        pending = short_drama_production.mark_attempt_refund_pending(
            self.db, "alice", "real-ledger-refund-001", terminal,
        )
        self.assertEqual("refund_pending", pending["state"],
                         "refund intent is durable before Auth is called")
        first, first_error = auth.refund_points(
            "alice", 24, "short-drama still compensation", pending["refund_key"],
        )
        self.assertIsNone(first_error)
        # Simulate a lost HTTP response by deliberately not marking completion, then replay Auth.
        replay, replay_error = auth.refund_points(
            "alice", 24, "short-drama still compensation", pending["refund_key"],
        )
        self.assertIsNone(replay_error)
        self.assertEqual(first["points"], replay["points"])
        short_drama_production.mark_attempt_refunded(
            self.db, "alice", "real-ledger-refund-001",
        )
        with closing(sqlite3.connect(auth.DB)) as conn:
            rows = conn.execute(
                "SELECT reason,delta FROM points_audit WHERE transaction_key=?",
                (pending["refund_key"],),
            ).fetchall()
        self.assertEqual([("short-drama still compensation", 24)], rows)
        self.assertEqual(100, auth.get_points_row("alice")["points"])

    def test_real_auth_402_keeps_ledger_unchanged_and_fresh_key_can_charge(self):
        auth = self._real_auth()
        with closing(sqlite3.connect(auth.DB)) as conn:
            conn.execute("UPDATE users SET points=10 WHERE username='alice'")
            conn.commit()
        attempt = self._accepted_attempt("real-ledger-402-001")
        result, error = auth.deduct_points(
            "alice", 24, "short-drama still", attempt["charge_key"],
        )
        self.assertIsNone(result)
        self.assertEqual("insufficient", error)
        terminal = {
            "detail": error, "code": "charge_rejected", "operation_terminal": True,
            "_http_status": 402,
        }
        short_drama_production.mark_attempt_failed(
            self.db, "alice", "real-ledger-402-001", terminal,
        )
        self.assertEqual(terminal, short_drama_production.recover_attempt_response(
            self.db, "alice", "real-ledger-402-001",
        ))
        with closing(sqlite3.connect(auth.DB)) as conn:
            self.assertEqual(0, conn.execute(
                "SELECT COUNT(*) FROM points_audit WHERE transaction_key=?",
                (attempt["charge_key"],),
            ).fetchone()[0])
        self.assertEqual(10, auth.get_points_row("alice")["points"])

        with closing(sqlite3.connect(auth.DB)) as conn:
            conn.execute("UPDATE users SET points=100 WHERE username='alice'")
            conn.commit()
        fresh = self._accepted_attempt("real-ledger-402-002")
        charged, fresh_error = auth.deduct_points(
            "alice", 24, "short-drama still", fresh["charge_key"],
        )
        self.assertIsNone(fresh_error)
        self.assertEqual(76, charged["points"])

    def test_attempt_refund_sweeper_is_the_only_owner_for_linked_job_refund(self):
        auth = self._real_auth()
        self._ensure_job_insert_columns()
        with closing(self.db()) as conn:
            try: conn.execute("ALTER TABLE jobs ADD COLUMN refunded INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError: pass
            try: conn.execute("ALTER TABLE jobs ADD COLUMN error TEXT NOT NULL DEFAULT ''")
            except sqlite3.OperationalError: pass
            conn.commit()
        attempt = self._accepted_attempt("single-refund-owner-001")
        charged, error = auth.deduct_points(
            "alice", 24, "short-drama still", attempt["charge_key"],
        )
        self.assertIsNone(error)
        short_drama_production.mark_attempt_charged(
            self.db, "alice", "single-refund-owner-001", charged["points"],
        )
        job_id = jobs_store.create_job_after_charge(
            self.db, "image", "alice", 24, attempt["image_payload"], "content",
            before_commit=lambda connection, jid: short_drama_production.record_attempt_job(
                self.db, "alice", "single-refund-owner-001", jid, connection=connection),
        )
        pending = short_drama_production.mark_linked_attempt_failed(
            self.db, "alice", "single-refund-owner-001",
            {"detail": "queue full", "code": "queue_full", "_http_status": 429},
        )
        pending_usage = short_drama_production.get_production(
            self.db, "alice", self.project["id"],
        )
        self.assertEqual(24, pending_usage["spent_points"])
        self.assertEqual(0, pending_usage["reserved_points"])
        first, first_error = auth.refund_points(
            "alice", 24, "short-drama still compensation", pending["refund_key"],
        )
        self.assertIsNone(first_error)
        # Lost Auth response: the attempt remains refund_pending while the generic scanner races.
        generic_calls = []
        jobs_store.retry_failed_refunds(
            self.db, lambda jid, username, cost: generic_calls.append((jid, username, cost)) or True,
        )
        self.assertEqual([], generic_calls)
        recovered = short_drama_production.retry_attempt_refunds(self.db, auth)
        self.assertEqual(1, recovered)
        with closing(sqlite3.connect(auth.DB)) as conn:
            refunds = conn.execute(
                "SELECT transaction_key,delta FROM points_audit WHERE transaction_key=?",
                (pending["refund_key"],),
            ).fetchall()
        self.assertEqual([(pending["refund_key"], 24)], refunds)
        self.assertEqual(100, auth.get_points_row("alice")["points"])
        refunded_usage = short_drama_production.get_production(
            self.db, "alice", self.project["id"],
        )
        self.assertEqual(0, refunded_usage["spent_points"])
        self.assertEqual(0, refunded_usage["reserved_points"])
        with closing(self.db()) as conn:
            self.assertEqual(1, conn.execute(
                "SELECT refunded FROM jobs WHERE id=?", (job_id,),
            ).fetchone()[0])

    def test_unlinked_attempt_states_reserve_project_budget_without_double_counting_jobs(self):
        accepted = self._accepted_attempt("reserve-accepted-001")
        state = short_drama_production.get_production(
            self.db, "alice", self.project["id"],
        )
        self.assertEqual(0, state["spent_points"])
        self.assertEqual(24, state["reserved_points"])
        short_drama_production.mark_attempt_charged(
            self.db, "alice", "reserve-accepted-001", 76,
        )
        state = short_drama_production.get_production(
            self.db, "alice", self.project["id"],
        )
        self.assertEqual(24, state["spent_points"])
        self.assertEqual(0, state["reserved_points"])
        self._ensure_job_insert_columns()
        with closing(self.db()) as conn:
            try: conn.execute("ALTER TABLE jobs ADD COLUMN refunded INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError: pass
            conn.commit()
        jobs_store.create_job_after_charge(
            self.db, "image", "alice", 24, accepted["image_payload"], "content",
            before_commit=lambda connection, jid: short_drama_production.record_attempt_job(
                self.db, "alice", "reserve-accepted-001", jid, connection=connection),
        )
        state = short_drama_production.get_production(
            self.db, "alice", self.project["id"],
        )
        self.assertEqual(24, state["spent_points"], "linked job is counted exactly once")
        self.assertEqual(0, state["reserved_points"])

    def test_real_auth_ledger_accepted_recovery_survives_drift_and_http_completion_crash(self):
        auth = self._real_auth()
        self._ensure_job_insert_columns()
        attempt = self._accepted_attempt("real-ledger-linked-001", prompt="durable recovery")
        first, first_error = auth.deduct_points(
            "alice", 24, "short-drama still", attempt["charge_key"],
        )
        self.assertIsNone(first_error)
        with closing(self.db()) as conn:
            conn.execute("UPDATE short_drama_still_quotes SET expires_at=0 WHERE token=?",
                         (attempt["quote_token"],))
            conn.execute(
                "UPDATE short_drama_projects SET revision=revision+1,stage='completed',board_id='removed-board' WHERE id=?",
                (self.project["id"],),
            )
            for name, definition in (
                ("error", "TEXT"), ("created_at", "INTEGER"), ("updated_at", "INTEGER"),
                ("owner", "TEXT"), ("refunded", "INTEGER DEFAULT 0"),
            ):
                try: conn.execute("ALTER TABLE jobs ADD COLUMN %s %s" % (name, definition))
                except sqlite3.OperationalError: pass
            conn.commit()
        replay, replay_error = auth.deduct_points(
            "alice", 24, "short-drama still", attempt["charge_key"],
        )
        self.assertIsNone(replay_error)
        self.assertEqual(first["points"], replay["points"])
        short_drama_production.mark_attempt_charged(
            self.db, "alice", "real-ledger-linked-001", replay["points"],
        )
        job_id = jobs_store.create_job_after_charge(
            self.db, "image", "alice", 24, attempt["image_payload"], "content",
            before_commit=lambda connection, jid: short_drama_production.record_attempt_job(
                self.db, "alice", "real-ledger-linked-001", jid, connection=connection),
        )
        recovered = short_drama_production.recover_attempt_response(
            self.db, "alice", "real-ledger-linked-001",
        )
        self.assertEqual(job_id, recovered["job_id"],
                         "linked response survives crash before HTTP idempotency completion")
        with closing(sqlite3.connect(auth.DB)) as conn:
            self.assertEqual(1, conn.execute(
                "SELECT COUNT(*) FROM points_audit WHERE transaction_key=?",
                (attempt["charge_key"],),
            ).fetchone()[0])

    def test_board_collaborator_roles_control_production_read_and_write(self):
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_projects SET board_id='board-a' WHERE id=?",
                (self.project["id"],),
            )
            conn.commit()
        editor = {"board_id": "board-a", "role": "editor"}
        viewer = {"board_id": "board-a", "role": "viewer"}

        state = short_drama_production.get_production(
            self.db, "editor", self.project["id"], access=editor
        )
        self.assertEqual(self.project["id"], state["project_id"])
        viewer_state = short_drama_production.get_production(
            self.db, "viewer", self.project["id"], access=viewer
        )
        self.assertEqual(self.project["id"], viewer_state["project_id"])
        with self.assertRaises(PermissionError):
            short_drama_production.prepare_still_submission(
                self.db, "viewer", self._still_request(), access=viewer
            )
        prepared = short_drama_production.prepare_still_submission(
            self.db, "editor", self._still_request(), access=editor
        )
        self.assertEqual(self._shot_id(), prepared["shot"]["id"])
        with self.assertRaises(LookupError):
            short_drama_production.get_production(
                self.db, "alice", self.project["id"], access=None
            )

        for denied in (
            {"board_id": "board-a", "role": None},
            {"board_id": "board-b", "role": "editor"},
            {"board_id": "board-a", "role": "viewer"},
        ):
            with self.subTest(access=denied):
                with self.assertRaises((LookupError, PermissionError)):
                    short_drama_production.prepare_still_submission(
                        self.db, "removed", self._still_request(), access=denied
                    )

    def test_stage_sequence_keeps_existing_stills_projects_eligible(self):
        self.assertEqual(self.project["stage"], "stills_review")
        self.assertEqual(short_drama.NEXT_STAGE["storyboard_review"], "stills_review")
        self.assertEqual(short_drama.NEXT_STAGE["stills_review"], "voice_review")
        self.assertEqual(short_drama.STAGES[-4:], (
            "voice_review", "video_review", "assembly_review", "completed",
        ))

    def test_seedream_provider_receives_owned_continuity_image_and_reference_context(self):
        captured = {}
        local_out = Path(self.tmp.name) / "content_out"
        local_out.mkdir()
        (local_out / "owned.png").write_bytes(b"\x89PNG\r\n\x1a\nowned")
        payload = {
            "provider": "seedream", "variant": "std", "quality": "hd",
            "prompt": "rainy doorway", "ratio": "9:16", "count": 2,
            "short_drama_references": [
                {"type": "character", "id": "character-owned", "name": "Detective Lin",
                 "source_type": "avatar", "source_id": "avatar-owned"},
                 {"type": "continuity", "id": "version-owned", "name": "previous locked still",
                  "url": "https://cos.test/owned.png", "file": "owned.png",
                  "ratio": "9:16"},
            ],
        }
        def fake_generate(prompt, ratio, quality, count, img, variant):
            captured.update(prompt=prompt, ratio=ratio, quality=quality, count=count,
                            img=img, variant=variant)
            image._seedream_one("seedream-model", prompt, "1152x2048", img)
            return {"urls": ["a", "b"], "ratio": ratio}
        def fake_post(path, data, content_type, **kwargs):
            captured["outbound"] = json.loads(data)
            return {"data": [{"url": "https://provider.test/result.png"}]}
        def fake_fetch(url):
            self.assertEqual("https://provider.test/result.png", url)
            return b"\x89PNG\r\n\x1a\nresult"
        with mock.patch.object(image, "OUT_DIR", local_out), \
             mock.patch.object(image, "_seedream_fetch", side_effect=fake_fetch), \
             mock.patch.object(image, "_post", side_effect=fake_post), \
             mock.patch.object(image, "_gen_image_seedream", side_effect=fake_generate):
            image.gen_image(payload)
        self.assertIn("Detective Lin", captured["prompt"])
        self.assertIn("previous locked still", captured["prompt"])
        self.assertEqual("9:16", captured["ratio"])
        self.assertEqual(2, captured["count"])
        self.assertEqual("iVBORw0KGgpvd25lZA==", captured["img"])
        self.assertEqual(captured["prompt"], captured["outbound"]["prompt"])
        self.assertEqual("data:image/png;base64,iVBORw0KGgpvd25lZA==",
                         captured["outbound"]["image"])

    def test_invalid_or_traversal_local_continuity_uses_prompt_only_fallback(self):
        local_out = Path(self.tmp.name) / "content_out"
        local_out.mkdir()
        outside = Path(self.tmp.name) / "secret.png"
        outside.write_bytes(b"\x89PNG\r\n\x1a\nsecret")
        (local_out / "large.png").write_bytes(b"\x89PNG\r\n\x1a\nlarge")
        captured = []
        unsafe_references = [
            {"url": "/api/gen/file/../secret.png"},
            {"file": "../secret.png", "url": "https://external.test/secret.png"},
            {"file": str(outside), "url": "/api/gen/file/../secret.png"},
            {"file": "large.png", "url": "https://external.test/large.png"},
        ]
        try:
            (local_out / "outside-link.png").symlink_to(outside)
        except NotImplementedError:
            if os.name != "nt":
                raise
        except OSError as error:
            if os.name != "nt" or getattr(error, "winerror", None) not in {
                1,     # ERROR_INVALID_FUNCTION
                5,     # ERROR_ACCESS_DENIED
                1314,  # ERROR_PRIVILEGE_NOT_HELD
            }:
                raise
        else:
            unsafe_references.append({
                "file": "outside-link.png", "url": "https://external.test/link.png",
            })
        with mock.patch.object(image, "OUT_DIR", local_out), \
             mock.patch.object(image, "IMAGE_REF_MAX_BYTES", 8), \
             mock.patch.object(
                 image, "_seedream_fetch",
                 side_effect=AssertionError("invalid local URL must not be fetched"),
             ), \
             mock.patch.object(
                 image, "_gen_image_seedream",
                 side_effect=lambda prompt, ratio, quality, count, img, variant:
                 captured.append((prompt, img)) or {"urls": ["a", "b"], "ratio": ratio},
             ):
            for reference in unsafe_references:
                image.gen_image({
                    "provider": "seedream", "variant": "std", "quality": "hd",
                    "prompt": "rainy doorway", "ratio": "9:16", "count": 2,
                    "short_drama_references": [dict(
                        reference, type="continuity", id="owned",
                        name="previous still", ratio="9:16",
                    )],
                })
        self.assertEqual(len(unsafe_references), len(captured))
        for prompt, local_image in captured:
            self.assertIn("previous still", prompt)
            self.assertIsNone(local_image)

    def test_cos_disabled_and_upload_failure_keep_trusted_local_file_urls(self):
        local_out = Path(self.tmp.name) / "content_out"
        local_out.mkdir()
        (local_out / "owned.png").write_bytes(b"png")
        with mock.patch.object(core, "OUT_DIR", local_out), \
             mock.patch("content_domains.cos.enabled", return_value=False):
            self.assertEqual("/api/gen/file/owned.png", core.public_url("owned.png", "image/png"))
        with mock.patch.object(core, "OUT_DIR", local_out), \
             mock.patch("content_domains.cos.enabled", return_value=True), \
             mock.patch("content_domains.cos.upload", side_effect=RuntimeError("upload failed")):
            self.assertEqual("/api/gen/file/owned.png", core.public_url("owned.png", "image/png"))

    def test_phase_two_rejects_unready_or_unsupported_confirmation(self):
        for stage, message in (
            ("voice_review", "仍有镜头尚未锁定"),
            ("video_review", "仍有镜头视频尚未锁定"),
        ):
            with self.subTest(stage=stage), closing(self.db()) as conn:
                conn.execute(
                    "UPDATE short_drama_projects SET stage=?, revision=50 WHERE id=?",
                    (stage, self.project["id"]),
                )
                conn.commit()
            with self.assertRaisesRegex(ValueError, message):
                short_drama.confirm_stage(
                    self.db, "alice", self.project["id"], 50, stage
                )
            with closing(self.db()) as conn:
                self.assertEqual(stage, conn.execute(
                    "SELECT stage FROM short_drama_projects WHERE id=?",
                    (self.project["id"],),
                ).fetchone()[0])
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_projects SET stage='assembly_review', "
                "revision=50 WHERE id=?",
                (self.project["id"],),
            )
            conn.commit()
        with mock.patch.dict(
            os.environ, {"HQ_SHORT_DRAMA_COMPLETION_ENABLED": "1"}
        ), self.assertRaises(short_drama_completion.CompletionError) as raised:
            short_drama.confirm_stage(
                self.db, "alice", self.project["id"], 50, "assembly_review"
            )
        self.assertEqual("completion_required", raised.exception.code)
        with closing(self.db()) as conn:
            self.assertEqual("assembly_review", conn.execute(
                "SELECT stage FROM short_drama_projects WHERE id=?",
                (self.project["id"],),
            ).fetchone()[0])

    def test_idempotency_claim_is_atomic_for_concurrent_identical_retries(self):
        def row_db():
            conn = sqlite3.connect(self.path, timeout=5)
            conn.row_factory = sqlite3.Row
            return conn
        barrier = threading.Barrier(8)
        results = []
        errors = []

        def claim():
            try:
                barrier.wait()
                results.append(submission_idempotency.begin(
                    row_db, "alice", "/same", "concurrent-key", {"a": 1}
                )[0])
            except Exception as error:
                errors.append(error)

        workers = [threading.Thread(target=claim) for _ in range(8)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()

        self.assertEqual([], errors)
        self.assertEqual(1, results.count("new"))
        self.assertEqual(7, results.count("processing"))

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
            "provider": "banana",
            "model": "nb2",
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

        first = short_drama_production.get_production(
            self.db, "alice", self.project["id"],
        )
        second = short_drama_production.get_production(
            self.db, "alice", self.project["id"],
        )

        with closing(self.db()) as conn:
            legacy_spent_points = conn.execute(
                "SELECT spent_points FROM short_drama_projects WHERE id=?",
                (self.project["id"],),
            ).fetchone()[0]
        self.assertEqual((60, 60), (first["spent_points"], second["spent_points"]))
        self.assertEqual(0, legacy_spent_points, "the unified ledger is not cached twice")

    def test_production_state_reports_active_job_as_spent_without_reserving_again(self):
        job_id = self._link_job(
            job_status="running", link_status="pending", cost=41, quoted_cost=40
        )

        state = short_drama_production.get_production(
            self.db, "alice", self.project["id"]
        )

        self.assertEqual(41, state["spent_points"])
        self.assertEqual(0, state["reserved_points"])
        self.assertEqual({
            "id": "link-%s" % job_id,
            "job_id": job_id,
            "kind": "still",
            "status": "running",
            "quoted_cost": 40,
            "error": "",
            "refunded": False,
            "refund_pending": False,
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

    def test_reconciliation_isolates_untrusted_result_and_marks_failed(self):
        job_id = self._link_job(result=[])

        state = short_drama_production.get_production(
            self.db, "alice", self.project["id"]
        )

        with closing(self.db()) as conn:
            self.assertEqual(
                6,
                conn.execute(
                    "SELECT COUNT(*) FROM short_drama_assets WHERE project_id=?",
                    (self.project["id"],),
                ).fetchone()[0],
            )
            self.assertEqual(
                "failed",
                conn.execute(
                    "SELECT status FROM short_drama_production_jobs WHERE job_id=?",
                    (job_id,),
                ).fetchone()[0],
            )
        self.assertEqual("failed", state["shots"][0]["still"]["job"]["status"])

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

        state = short_drama_production.get_production(
            self.db, "alice", self.project["id"]
        )
        self.assertEqual("failed", state["shots"][0]["still"]["job"]["status"])
        self.assertIn("比例", state["shots"][0]["still"]["job"]["error"])

    def test_reconciliation_requires_exactly_two_candidate_urls(self):
        self._link_job(result={
            "urls": ["https://example.test/only.png"], "ratio": "9:16",
        })

        state = short_drama_production.get_production(
            self.db, "alice", self.project["id"]
        )
        self.assertEqual("failed", state["shots"][0]["still"]["job"]["status"])

    def test_reconciliation_rejects_duplicate_candidate_urls_without_partial_archive(self):
        duplicate_url = "https://example.test/duplicate.png"
        job_id = self._link_job(result={
            "urls": [duplicate_url, duplicate_url], "ratio": "9:16",
        })

        state = short_drama_production.get_production(
            self.db, "alice", self.project["id"]
        )
        self.assertEqual("failed", state["shots"][0]["still"]["job"]["status"])

        with closing(self.db()) as conn:
            self.assertEqual(0, conn.execute(
                "SELECT COUNT(*) FROM short_drama_asset_versions"
            ).fetchone()[0])
            self.assertEqual(
                "failed",
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

        state = short_drama_production.get_production(
            self.db, "alice", self.project["id"]
        )
        self.assertEqual("failed", state["shots"][0]["still"]["job"]["status"])

        with closing(self.db()) as conn:
            self.assertEqual(1, conn.execute(
                "SELECT COUNT(*) FROM short_drama_asset_versions "
                "WHERE asset_id=? AND job_id=?",
                (asset_id, job_id),
            ).fetchone()[0])
            self.assertEqual(
                "failed",
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
        self.assertEqual("failed", state["shots"][0]["still"]["job"]["status"])
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
        self.assertEqual(
            selected["spent_points"] + 60,
            regenerated["spent_points"],
        )

    def test_confirm_requires_every_current_shot_to_have_a_locked_still(self):
        self._completed_still_versions()

        snapshot = short_drama_production.get_production(
            self.db, "alice", self.project["id"],
        )
        self.assertTrue(snapshot["handoff_blocked"])
        self.assertEqual(
            "missing_locked_still", snapshot["handoff_blockers"][0]["code"],
        )

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

    def test_confirm_stills_creates_voice_snapshot_in_the_same_transaction(self):
        self._lock_every_current_still()
        confirmed = short_drama_production.confirm_stage(
            self.db, "alice", {
                "project_id": self.project["id"],
                "revision": self.project["revision"],
                "stage": "stills_review",
            },
        )
        self.assertEqual("voice_review", confirmed["stage"])
        with closing(self.db()) as conn:
            shot_count = conn.execute(
                "SELECT COUNT(*) FROM short_drama_voice_shots WHERE project_id=?",
                (self.project["id"],),
            ).fetchone()[0]
        self.assertEqual(6, shot_count)

    def test_confirm_reconciles_late_success_before_creating_voice_snapshot(self):
        self._lock_every_current_still()
        job_id = self._link_job(cost=60, quoted_cost=60)
        observed = {}
        original = short_drama_production.short_drama_voice.ensure_voice_workspace

        def inspect_reconciled_ledger(conn, project_id, allowed_stages=None):
            observed["spent_points"] = short_drama._project_point_usage(
                conn, project_id,
            )["spent_points"]
            observed["legacy_spent_points"] = conn.execute(
                "SELECT spent_points FROM short_drama_projects WHERE id=?",
                (project_id,),
            ).fetchone()[0]
            observed["archive_count"] = conn.execute(
                "SELECT COUNT(*) FROM short_drama_asset_versions WHERE job_id=?",
                (job_id,),
            ).fetchone()[0]
            return original(conn, project_id, allowed_stages=allowed_stages)

        with mock.patch(
            "content_domains.short_drama_voice.ensure_voice_workspace",
            side_effect=inspect_reconciled_ledger,
        ):
            confirmed = short_drama_production.confirm_stage(
                self.db, "alice", {
                    "project_id": self.project["id"],
                    "revision": self.project["revision"],
                    "stage": "stills_review",
                },
            )

        self.assertEqual("voice_review", confirmed["stage"])
        self.assertEqual({
            "spent_points": 60, "legacy_spent_points": 0, "archive_count": 2,
        }, observed)
        with closing(self.db()) as conn:
            self.assertEqual(
                (0, 2, "done"),
                (
                    conn.execute(
                        "SELECT spent_points FROM short_drama_projects WHERE id=?",
                        (self.project["id"],),
                    ).fetchone()[0],
                    conn.execute(
                        "SELECT COUNT(*) FROM short_drama_asset_versions WHERE job_id=?",
                        (job_id,),
                    ).fetchone()[0],
                    conn.execute(
                        "SELECT status FROM short_drama_production_jobs WHERE job_id=?",
                        (job_id,),
                    ).fetchone()[0],
                ),
            )

    def test_confirm_rejects_pending_and_running_production_jobs(self):
        self._lock_every_current_still()
        pending_job = self._link_job(
            shot_order=0, job_status="pending", link_status="pending",
        )
        running_job = self._link_job(
            shot_order=1, job_status="running", link_status="pending",
        )
        body = {
            "project_id": self.project["id"],
            "revision": self.project["revision"],
            "stage": "stills_review",
        }

        snapshot = short_drama_production.get_production(
            self.db, "alice", self.project["id"],
        )
        self.assertTrue(snapshot["handoff_blocked"])
        self.assertEqual("active_job", snapshot["handoff_blockers"][0]["code"])

        with self.assertRaisesRegex(
            ValueError, snapshot["handoff_blockers"][0]["message"],
        ):
            short_drama_production.confirm_stage(self.db, "alice", body)

        with closing(self.db()) as conn:
            self.assertEqual(
                ("stills_review", 0),
                (
                    conn.execute(
                        "SELECT stage FROM short_drama_projects WHERE id=?",
                        (self.project["id"],),
                    ).fetchone()[0],
                    conn.execute(
                        "SELECT COUNT(*) FROM short_drama_voice_shots WHERE project_id=?",
                        (self.project["id"],),
                    ).fetchone()[0],
                ),
            )
            conn.execute(
                "UPDATE jobs SET status='failed' WHERE id IN (?,?)",
                (pending_job, running_job),
            )
            conn.commit()

        confirmed = short_drama_production.confirm_stage(self.db, "alice", body)
        self.assertEqual("voice_review", confirmed["stage"])

    def test_snapshot_reports_old_running_job_hidden_by_new_done_job(self):
        self._lock_every_current_still()
        self._link_job(
            shot_order=0, job_status="running", link_status="running",
        )
        self._link_job(shot_order=0, job_status="done", link_status="pending")

        snapshot = short_drama_production.get_production(
            self.db, "alice", self.project["id"],
        )

        self.assertTrue(snapshot["handoff_blocked"])
        self.assertIn(
            "active_job",
            [item["code"] for item in snapshot["handoff_blockers"]],
        )

    def _assert_unresolved_charge_attempt_blocks_handoff(self, state):
        self._lock_every_current_still()
        body = {
            "project_id": self.project["id"],
            "revision": self.project["revision"],
            "stage": "stills_review",
        }
        key = "handoff-attempt-%s" % state
        attempt = self._accepted_attempt(key)
        if state in {"charged", "refund_pending"}:
            attempt = short_drama_production.mark_attempt_charged(
                self.db, "alice", key, 76,
            )
        if state == "refund_pending":
            attempt = short_drama_production.mark_attempt_refund_pending(
                self.db, "alice", key,
                {"detail": "refund pending", "code": "refund_pending"},
            )
        self.assertEqual(state, attempt["state"])

        snapshot = short_drama_production.get_production(
            self.db, "alice", self.project["id"],
        )
        expected_code = (
            "refund_pending" if state == "refund_pending"
            else "charge_attempt_pending"
        )
        self.assertIn(
            expected_code,
            [item["code"] for item in snapshot["handoff_blockers"]],
        )

        with self.assertRaisesRegex(ValueError, "扣点|退款|账本|处理中"):
            short_drama_production.confirm_stage(self.db, "alice", body)

        with closing(self.db()) as conn:
            self.assertEqual(
                ("stills_review", 0),
                (
                    conn.execute(
                        "SELECT stage FROM short_drama_projects WHERE id=?",
                        (self.project["id"],),
                    ).fetchone()[0],
                    conn.execute(
                        "SELECT COUNT(*) FROM short_drama_voice_shots WHERE project_id=?",
                        (self.project["id"],),
                    ).fetchone()[0],
                ),
            )

        if state == "accepted":
            short_drama_production.mark_attempt_failed(
                self.db, "alice", key,
                {"detail": "not charged", "operation_terminal": True},
            )
        else:
            if state == "charged":
                short_drama_production.mark_attempt_refund_pending(
                    self.db, "alice", key,
                    {"detail": "refund pending", "code": "refund_pending"},
                )
            short_drama_production.mark_attempt_refunded(self.db, "alice", key)

        confirmed = short_drama_production.confirm_stage(self.db, "alice", body)
        self.assertEqual("voice_review", confirmed["stage"])

    def test_confirm_rejects_accepted_charge_attempt(self):
        self._assert_unresolved_charge_attempt_blocks_handoff("accepted")

    def test_confirm_rejects_charged_charge_attempt(self):
        self._assert_unresolved_charge_attempt_blocks_handoff("charged")

    def test_confirm_rejects_refund_pending_charge_attempt(self):
        self._assert_unresolved_charge_attempt_blocks_handoff("refund_pending")

    def test_confirm_persists_bad_result_refund_intent_before_rejecting_handoff(self):
        self._lock_every_current_still()
        auth, job_id, attempt = self._linked_real_ledger_job(
            "handoff-malformed-result", status="done",
        )
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE jobs SET result=? WHERE id=?",
                (json.dumps({"urls": ["only-one"], "ratio": "9:16"}), job_id),
            )
            conn.commit()
        body = {
            "project_id": self.project["id"],
            "revision": self.project["revision"],
            "stage": "stills_review",
        }

        with self.assertRaisesRegex(ValueError, "扣点|退款|账本|处理中"):
            short_drama_production.confirm_stage(self.db, "alice", body)

        pending = short_drama_production.get_charge_attempt(
            self.db, "alice", attempt["idempotency_key"],
        )
        self.assertEqual("refund_pending", pending["state"])
        self.assertEqual(76, auth.get_points_row("alice")["points"])
        with closing(self.db()) as conn:
            self.assertEqual(
                ("stills_review", 0, "failed", 2),
                (
                    conn.execute(
                        "SELECT stage FROM short_drama_projects WHERE id=?",
                        (self.project["id"],),
                    ).fetchone()[0],
                    conn.execute(
                        "SELECT COUNT(*) FROM short_drama_voice_shots WHERE project_id=?",
                        (self.project["id"],),
                    ).fetchone()[0],
                    conn.execute(
                        "SELECT status FROM short_drama_production_jobs WHERE job_id=?",
                        (job_id,),
                    ).fetchone()[0],
                    conn.execute(
                        "SELECT refunded FROM short_drama_production_jobs WHERE job_id=?",
                        (job_id,),
                    ).fetchone()[0],
                ),
            )

        self.assertEqual(1, short_drama_production.retry_attempt_refunds(self.db, auth))
        confirmed = short_drama_production.confirm_stage(self.db, "alice", body)
        self.assertEqual("voice_review", confirmed["stage"])
        self.assertEqual(100, auth.get_points_row("alice")["points"])

    def test_charge_acceptance_rechecks_stage_after_a_prepared_request(self):
        request = self._still_request()
        quote = short_drama_production.prepare_still_quote(
            self.db, "alice", request, lambda _kind, _payload: 24,
        )
        prepared = short_drama_production.prepare_still_submission(
            self.db, "alice", dict(request, quote_token=quote["quote_token"]),
            require_quote=True, idempotency_key="prepared-before-handoff",
        )
        self._lock_every_current_still()
        short_drama_production.confirm_stage(self.db, "alice", {
            "project_id": self.project["id"],
            "revision": self.project["revision"],
            "stage": "stills_review",
        })

        with self.assertRaisesRegex(ValueError, "阶段|关键帧"):
            short_drama_production.accept_charge_attempt(
                self.db, username="alice",
                endpoint="/api/gen/short-drama/generate-stills",
                idempotency_key="prepared-before-handoff", prepared=prepared,
            )

        with closing(self.db()) as conn:
            self.assertIsNone(conn.execute(
                "SELECT consumed_idempotency_key FROM short_drama_still_quotes WHERE token=?",
                (quote["quote_token"],),
            ).fetchone()[0])
            self.assertEqual(0, conn.execute(
                "SELECT COUNT(*) FROM short_drama_charge_attempts "
                "WHERE idempotency_key='prepared-before-handoff'"
            ).fetchone()[0])

    def test_snapshot_failure_rolls_back_stage_confirmation(self):
        self._lock_every_current_still()
        job_id = self._link_job(cost=60, quoted_cost=60)
        observed = {}

        def fail_after_reconciliation(conn, project_id, allowed_stages=None):
            observed["spent_points"] = short_drama._project_point_usage(
                conn, project_id,
            )["spent_points"]
            observed["legacy_spent_points"] = conn.execute(
                "SELECT spent_points FROM short_drama_projects WHERE id=?",
                (project_id,),
            ).fetchone()[0]
            observed["archive_count"] = conn.execute(
                "SELECT COUNT(*) FROM short_drama_asset_versions WHERE job_id=?",
                (job_id,),
            ).fetchone()[0]
            raise RuntimeError("snapshot failed")

        with mock.patch(
            "content_domains.short_drama_voice.ensure_voice_workspace",
            side_effect=fail_after_reconciliation,
        ):
            with self.assertRaisesRegex(RuntimeError, "snapshot failed"):
                short_drama_production.confirm_stage(
                    self.db, "alice", {
                        "project_id": self.project["id"],
                        "revision": self.project["revision"],
                        "stage": "stills_review",
                    },
                )
        self.assertEqual({
            "spent_points": 60, "legacy_spent_points": 0, "archive_count": 2,
        }, observed)
        with closing(self.db()) as conn:
            self.assertEqual(
                ("stills_review", 0, 0, "pending"),
                (
                    conn.execute(
                        "SELECT stage FROM short_drama_projects WHERE id=?",
                        (self.project["id"],),
                    ).fetchone()[0],
                    conn.execute(
                        "SELECT spent_points FROM short_drama_projects WHERE id=?",
                        (self.project["id"],),
                    ).fetchone()[0],
                    conn.execute(
                        "SELECT COUNT(*) FROM short_drama_asset_versions WHERE job_id=?",
                        (job_id,),
                    ).fetchone()[0],
                    conn.execute(
                        "SELECT status FROM short_drama_production_jobs WHERE job_id=?",
                        (job_id,),
                    ).fetchone()[0],
                ),
            )

        confirmed = short_drama_production.confirm_stage(self.db, "alice", {
            "project_id": self.project["id"],
            "revision": self.project["revision"],
            "stage": "stills_review",
        })
        self.assertEqual("voice_review", confirmed["stage"])
        self.assertEqual(60, confirmed["spent_points"])
        with closing(self.db()) as conn:
            self.assertEqual(0, conn.execute(
                "SELECT spent_points FROM short_drama_projects WHERE id=?",
                (self.project["id"],),
            ).fetchone()[0])
            self.assertEqual(2, conn.execute(
                "SELECT COUNT(*) FROM short_drama_asset_versions WHERE job_id=?",
                (job_id,),
            ).fetchone()[0])

    def test_concurrent_stage_confirmation_succeeds_once(self):
        self._lock_every_current_still()
        job_id = self._link_job(cost=60, quoted_cost=60)
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
        with closing(self.db()) as conn:
            self.assertEqual(60, short_drama._project_point_usage(
                conn, self.project["id"],
            )["spent_points"])
            self.assertEqual(0, conn.execute(
                "SELECT spent_points FROM short_drama_projects WHERE id=?",
                (self.project["id"],),
            ).fetchone()[0])
            self.assertEqual(2, conn.execute(
                "SELECT COUNT(*) FROM short_drama_asset_versions WHERE job_id=?",
                (job_id,),
            ).fetchone()[0])

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
        payload = prepared["image_payload"]
        self.assertEqual("banana", payload["provider"])
        self.assertEqual("nb2", payload["model"])
        self.assertEqual("hd", payload["quality"])
        self.assertEqual(self.project["ratio"], payload["ratio"])
        self.assertEqual(2, payload["count"])
        self.assertEqual([], payload["short_drama_references"])
        self.assertEqual(
            "rainy midnight doorway, consistent detective character",
            payload["short_drama_raw_prompt"],
        )
        self.assertIn("User direction: rainy midnight doorway", payload["prompt"])

    def test_still_submission_deduplicates_storyboard_prompt_from_legacy_client(self):
        with closing(self.db()) as conn:
            image_prompt = conn.execute(
                "SELECT image_prompt FROM short_drama_shots WHERE id=?",
                (self._shot_id(),),
            ).fetchone()[0]

        prepared = short_drama_production.prepare_still_submission(
            self.db, "alice", self._still_request(prompt=image_prompt)
        )

        self.assertEqual("", prepared["user_direction"])
        self.assertEqual("", prepared["image_payload"]["short_drama_raw_prompt"])
        self.assertEqual(
            1,
            prepared["compiled_prompt"].count(image_prompt),
            "the confirmed storyboard prompt must reach the provider exactly once",
        )
        self.assertNotIn("User direction:", prepared["compiled_prompt"])

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
                "UPDATE short_drama_projects SET point_budget=100 WHERE id=?",
                (self.project["id"],),
            )
            conn.execute(
                "INSERT INTO jobs(username,kind,cost,status,payload,result) "
                "VALUES('alice','copy',20,'done',?,'{}')",
                (json.dumps({
                    "format": "short_drama",
                    "project_id": self.project["id"],
                }),),
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
        self.assertEqual("banana", quoted_payloads[0][1]["provider"])
        self.assertEqual("nb2", quoted_payloads[0][1]["model"])
        self.assertEqual("9:16", quoted_payloads[0][1]["ratio"])
        self.assertEqual(2, quoted_payloads[0][1]["count"])

    def test_still_quote_returns_realtime_server_cost(self):
        quote = short_drama_production.prepare_still_quote(
            self.db, "alice", self._still_request(), lambda kind, payload: 24
        )

        self.assertEqual(24, quote["cost"])
        self.assertEqual(2, quote["count"])
        self.assertEqual("still", quote["kind"])
        self.assertRegex(quote["quote_token"], r"^[0-9a-f]{32}$")
        self.assertGreater(quote["expires_at"], int(__import__("time").time()))

    def test_still_submission_requires_a_matching_unexpired_user_quote(self):
        body = self._still_request()
        with self.assertRaisesRegex(ValueError, "quote"):
            short_drama_production.prepare_still_submission(
                self.db, "alice", body, require_quote=True,
                idempotency_key="quote-bind-001",
            )

        quote = short_drama_production.prepare_still_quote(
            self.db, "alice", body, lambda _kind, _payload: 24,
        )
        submitted = dict(body, quote_token=quote["quote_token"])
        prepared = short_drama_production.prepare_still_submission(
            self.db, "alice", submitted, require_quote=True,
            idempotency_key="quote-bind-001",
        )
        self.assertEqual(24, prepared["quoted_cost"])
        self.assertEqual(quote["quote_token"], prepared["quote_token"])

        with self.assertRaises(LookupError):
            short_drama_production.prepare_still_submission(
                self.db, "mallory", submitted, require_quote=True,
                idempotency_key="quote-bind-002",
            )
        with self.assertRaisesRegex(ValueError, "quote"):
            short_drama_production.prepare_still_submission(
                self.db, "alice", dict(submitted, prompt="altered"),
                require_quote=True, idempotency_key="quote-bind-003",
            )

    def test_storyboard_save_preserves_shot_id_and_invalidates_ready_snapshot(self):
        _workspace, snapshot = self._ready_asset_snapshot()
        original_shot_id = self._shot_id()
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_projects SET stage='storyboard_review' WHERE id=?",
                (self.project["id"],),
            )
            conn.execute(
                "UPDATE short_drama_scripts SET logline='侦探追查真相',hook='午夜来客',"
                "conflict_text='证词冲突',turn_text='线索反转',ending='真相揭晓' "
                "WHERE project_id=?", (self.project["id"],),
            )
            conn.commit()
        detail = short_drama.get_project(self.db, "alice", self.project["id"])
        shots = [dict(item) for item in detail["shots"]]
        shots[0]["image_prompt"] += "，加入新的构图要求"
        updated = short_drama.update_shots(
            self.db, "alice", self.project["id"], detail["revision"], shots,
        )
        self.assertEqual(original_shot_id, updated["shots"][0]["id"])
        with closing(self.db()) as conn:
            with self.assertRaises(short_drama_asset_graph.AssetGraphError) as raised:
                short_drama_asset_graph.generation_package(
                    conn, self.project["id"], original_shot_id,
                )
        self.assertEqual("asset_snapshot_stale", raised.exception.code)
        self.assertNotEqual(
            snapshot["graph_revision"],
            short_drama_asset_graph.workspace(
                self.db, "alice", self.project["id"],
            )["graph_revision"],
        )

    def test_old_quote_is_rejected_after_new_asset_snapshot_without_charge_or_job(self):
        workspace, snapshot_a = self._ready_asset_snapshot()
        body = self._still_request()
        quote = short_drama_production.prepare_still_quote(
            self.db, "alice", body, lambda _kind, _payload: 24,
        )
        self.assertEqual(snapshot_a["id"], quote["snapshot_id"])
        self.assertEqual(
            snapshot_a["package"]["package_hash"], quote["package_hash"],
        )
        submitted = dict(body, quote_token=quote["quote_token"])
        prepared_a = short_drama_production.prepare_still_submission(
            self.db, "alice", submitted, require_quote=True,
            idempotency_key="stale-asset-quote-race",
        )

        scene = next(
            item for item in workspace["entities"] if item["asset_type"] == "scene"
        )
        created = short_drama_asset_graph.create_version(
            self.db, "alice", "alice", {
                "project_id": self.project["id"],
                "graph_revision": snapshot_a["graph_revision"],
                "entity_id": scene["id"],
                "prompt": "NEW_ASSET_MARKER，全新的雨夜场景",
            },
        )
        locked = short_drama_asset_graph.lock_version(
            self.db, "alice", "alice", {
                "project_id": self.project["id"],
                "graph_revision": created["graph_revision"],
                "version_id": created["id"],
            },
        )
        snapshot_b = short_drama_asset_graph.build_snapshot(
            self.db, "alice", "alice", {
                "project_id": self.project["id"],
                "graph_revision": locked["graph_revision"],
                "shot_id": self._shot_id(),
            },
        )
        self.assertEqual("ready", snapshot_b["status"])
        self.assertNotEqual(snapshot_a["id"], snapshot_b["id"])

        with self.assertRaises(short_drama_asset_graph.AssetGraphError) as raised:
            short_drama_production.prepare_still_submission(
                self.db, "alice", submitted,
                require_quote=True, idempotency_key="stale-asset-quote",
            )
        self.assertEqual("asset_quote_stale", raised.exception.code)
        with self.assertRaises(short_drama_asset_graph.AssetGraphError) as race:
            short_drama_production.accept_charge_attempt(
                self.db, username="alice",
                endpoint="/api/gen/short-drama/generate-stills",
                idempotency_key="stale-asset-quote-race", prepared=prepared_a,
            )
        self.assertEqual("asset_quote_stale", race.exception.code)
        with closing(self.db()) as conn:
            quote_row = conn.execute(
                "SELECT consumed_idempotency_key FROM short_drama_still_quotes "
                "WHERE token=?", (quote["quote_token"],),
            ).fetchone()
            attempts = conn.execute(
                "SELECT COUNT(*) FROM short_drama_charge_attempts"
            ).fetchone()[0]
            jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        self.assertIsNone(quote_row[0])
        self.assertEqual(0, attempts)
        self.assertEqual(0, jobs)

        quote_b = short_drama_production.prepare_still_quote(
            self.db, "alice", body, lambda _kind, _payload: 24,
        )
        prepared_b = short_drama_production.prepare_still_submission(
            self.db, "alice", dict(body, quote_token=quote_b["quote_token"]),
            require_quote=True, idempotency_key="current-asset-quote",
        )
        attempt = short_drama_production.accept_charge_attempt(
            self.db, username="alice",
            endpoint="/api/gen/short-drama/generate-stills",
            idempotency_key="current-asset-quote", prepared=prepared_b,
        )
        self.assertEqual(snapshot_b["id"], attempt["snapshot_id"])
        self.assertEqual(snapshot_b["package"]["package_hash"], attempt["package_hash"])
        self.assertEqual(snapshot_b["graph_revision"], attempt["graph_revision"])

    def test_server_derives_owned_character_and_locked_continuity_references(self):
        with closing(self.db()) as conn:
            conn.execute(
                "INSERT INTO short_drama_characters "
                "(id, project_id, character_key, name, source_type, avatar_id, "
                "reference_file, reference_locked, reference_version, sort_order) "
                "VALUES ('character-owned', ?, 'detective', '侦探', 'cinematic_avatar', "
                "'avatar-owned', 'owned.png', 1, 1, 0)",
                (self.project["id"],),
            )
            target_id = self._shot_id(1)
            conn.execute(
                "UPDATE short_drama_shots SET character_keys_json='[\"detective\"]' WHERE id=?",
                (target_id,),
            )
            short_drama_production.ensure_asset_slots(conn, self.project["id"])
            previous_asset = conn.execute(
                "SELECT id FROM short_drama_assets WHERE project_id=? AND shot_id=?",
                (self.project["id"], self._shot_id(0)),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO short_drama_asset_versions "
                "(id, asset_id, version, job_id, url, prompt, ratio, cost, status, created_at) "
                "VALUES ('continuity-owned', ?, 1, 9001, 'https://example.test/locked.png', "
                "'locked', '9:16', 0, 'done', 1)",
                (previous_asset,),
            )
            conn.execute(
                "UPDATE short_drama_assets SET current_version=1, locked=1 WHERE id=?",
                (previous_asset,),
            )
            conn.commit()

        prepared = short_drama_production.prepare_still_submission(
            self.db, "alice", self._still_request(shot_id=target_id)
        )
        references = prepared["shot"]["references"]
        self.assertEqual(["character", "continuity"], [item["type"] for item in references])
        self.assertEqual("character-owned", references[0]["id"])
        self.assertEqual("avatar-owned", references[0]["source_id"])
        self.assertEqual("continuity-owned", references[1]["id"])
        self.assertEqual("https://example.test/locked.png", references[1]["url"])
        self.assertEqual(references, prepared["image_payload"]["short_drama_references"])

        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_asset_versions SET ratio='16:9' WHERE id='continuity-owned'"
            )
            conn.commit()
        with self.assertRaisesRegex(ValueError, "比例"):
            short_drama_production.prepare_still_submission(
                self.db, "alice", self._still_request(shot_id=target_id)
            )

    def test_first_reconciled_snapshot_has_fresh_financial_totals(self):
        self._link_job(cost=60, quoted_cost=60)

        state = short_drama_production.get_production(
            self.db, "alice", self.project["id"]
        )

        self.assertEqual(60, state["spent_points"])
        self.assertEqual(0, state["reserved_points"])

    def test_reconciliation_archives_local_file_keys_without_exposing_them(self):
        local_out = Path(self.tmp.name) / "content_out"
        local_out.mkdir(exist_ok=True)
        for name in ("candidate-one.png", "candidate-two.png"):
            (local_out / name).write_bytes(b"\x89PNG\r\n\x1a\n" + name.encode())
        job_id = self._link_job(result={
            "urls": [
                "https://cos.test/candidate-one.png",
                "https://cos.test/candidate-two.png",
            ],
            "files": ["candidate-one.png", "candidate-two.png"],
            "ratio": "9:16",
        })

        with mock.patch.object(image, "OUT_DIR", local_out):
            state = short_drama_production.get_production(
                self.db, "alice", self.project["id"]
            )

        with closing(self.db()) as conn:
            files = [
                row[0] for row in conn.execute(
                    "SELECT file FROM short_drama_asset_versions "
                    "WHERE job_id=? ORDER BY version",
                    (job_id,),
                )
            ]
        self.assertEqual(["candidate-one.png", "candidate-two.png"], files)
        self.assertTrue(state["shots"][0]["still"]["versions"])
        for version in state["shots"][0]["still"]["versions"]:
            self.assertNotIn("file", version)

    def test_reconciliation_backfills_local_files_for_existing_cos_archive(self):
        local_out = Path(self.tmp.name) / "content_out"
        local_out.mkdir(exist_ok=True)
        for name in ("legacy-one.png", "legacy-two.png"):
            (local_out / name).write_bytes(b"\x89PNG\r\n\x1a\n" + name.encode())
        urls = [
            "https://cos.test/legacy-one.png",
            "https://cos.test/legacy-two.png",
        ]
        job_id = self._link_job(result={
            "urls": urls,
            "files": ["legacy-one.png", "legacy-two.png"],
            "ratio": "9:16",
        })
        with closing(self.db()) as conn:
            short_drama_production.ensure_asset_slots(conn, self.project["id"])
            asset_id = conn.execute(
                "SELECT id FROM short_drama_assets WHERE project_id=? AND shot_id=?",
                (self.project["id"], self._shot_id()),
            ).fetchone()[0]
            for version, url in enumerate(urls, 1):
                conn.execute(
                    "INSERT INTO short_drama_asset_versions "
                    "(id,asset_id,version,job_id,url,prompt,ratio,cost,status,created_at) "
                    "VALUES(?,?,?,?,?,'legacy','9:16',24,'done',1)",
                    ("legacy-version-%s" % version, asset_id, version, job_id, url),
                )
            conn.commit()

        with mock.patch.object(image, "OUT_DIR", local_out):
            short_drama_production.get_production(
                self.db, "alice", self.project["id"]
            )
        with closing(self.db()) as conn:
            files = [
                row[0] for row in conn.execute(
                    "SELECT file FROM short_drama_asset_versions "
                    "WHERE job_id=? ORDER BY version",
                    (job_id,),
                )
            ]
            conn.execute(
                "UPDATE short_drama_assets SET locked=1 WHERE id=?", (asset_id,)
            )
            conn.commit()
        self.assertEqual(["legacy-one.png", "legacy-two.png"], files)

        public_state = short_drama_production.get_production(
            self.db, "alice", self.project["id"]
        )
        public_continuity = public_state["shots"][1]["references"][0]
        self.assertNotIn("file", public_continuity)
        prepared = short_drama_production.prepare_still_submission(
            self.db, "alice", self._still_request(shot_id=self._shot_id(1))
        )
        internal_continuity = prepared["image_payload"]["short_drama_references"][0]
        self.assertEqual("legacy-one.png", internal_continuity["file"])

    def test_malformed_done_job_isolated_and_visible_while_other_job_reconciles(self):
        with closing(self.db()) as conn:
            conn.execute("ALTER TABLE jobs ADD COLUMN error TEXT")
            conn.execute("ALTER TABLE jobs ADD COLUMN refunded INTEGER NOT NULL DEFAULT 0")
            conn.commit()
        bad_id = self._link_job(
            shot_order=0, result={"urls": ["only-one"], "ratio": "9:16"}
        )
        good_id = self._link_job(shot_order=1)

        first = short_drama_production.get_production(
            self.db, "alice", self.project["id"]
        )
        second = short_drama_production.get_production(
            self.db, "alice", self.project["id"]
        )

        bad = first["shots"][0]["still"]["job"]
        self.assertEqual(bad_id, bad["job_id"])
        self.assertEqual("failed", bad["status"])
        self.assertFalse(bad["refunded"])
        self.assertTrue(bad["refund_pending"])
        self.assertIn("2", bad["error"])
        self.assertEqual(good_id, first["shots"][1]["still"]["versions"][0]["job_id"])
        self.assertEqual(first, second)

    def test_snapshot_keeps_latest_terminal_failure_visible(self):
        failed_id = self._link_job(
            job_status="failed", link_status="pending", quoted_cost=17,
        )
        with closing(self.db()) as conn:
            conn.execute("ALTER TABLE jobs ADD COLUMN error TEXT")
            conn.execute("ALTER TABLE jobs ADD COLUMN refunded INTEGER NOT NULL DEFAULT 0")
            conn.execute(
                "UPDATE jobs SET error='upstream rejected', refunded=1 WHERE id=?",
                (failed_id,),
            )
            conn.commit()

        state = short_drama_production.get_production(
            self.db, "alice", self.project["id"]
        )
        job = state["shots"][0]["still"]["job"]
        self.assertEqual("failed", job["status"])
        self.assertEqual("upstream rejected", job["error"])
        self.assertTrue(job["refunded"])
        self.assertEqual(0, state["reserved_points"])

    def test_latest_same_timestamp_job_uses_monotonic_job_id(self):
        old_id = self._link_job(job_status="failed", link_status="failed")
        new_id = self._link_job(job_status="running", link_status="running")
        self.assertGreater(new_id, old_id)
        with closing(self.db()) as conn:
            conn.execute("UPDATE short_drama_production_jobs SET id='zzz-old' WHERE job_id=?", (old_id,))
            conn.execute("UPDATE short_drama_production_jobs SET id='aaa-new' WHERE job_id=?", (new_id,))
            conn.commit()
        state = short_drama_production.get_production(
            self.db, "alice", self.project["id"]
        )
        self.assertEqual(new_id, state["shots"][0]["still"]["job"]["job_id"])

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
    class FakeAudio:
        def __init__(self):
            self.list_calls = []
            self.assets = {
                "alice": [{
                    "id": 101, "username": "alice",
                    "file": "audio/owner-bgm.mp3",
                    "url": "/api/gen/file/audio/owner-bgm.mp3",
                    "text": "owner bgm",
                }],
                "editor": [{
                    "id": 202, "username": "editor",
                    "file": "audio/editor-bgm.mp3",
                    "url": "/api/gen/file/audio/editor-bgm.mp3",
                    "text": "editor bgm",
                }],
            }

        @staticmethod
        def resolve_audio_provider_voice(_username, voice_key):
            if not voice_key:
                raise ValueError("missing voice")
            return voice_key

        def list_audio_assets(self, username, limit=120, offset=0):
            self.list_calls.append((username, limit))
            return [dict(item) for item in self.assets.get(username, [])][offset:offset + limit]

        def get_audio_asset(self, username, asset_id):
            return next((
                dict(item) for item in self.assets.get(username, [])
                if item["id"] == int(asset_id)
            ), None)

    class FakePoints:
        class AuthPointsError(Exception):
            status = 402
            detail = "insufficient points"

        def __init__(self):
            self.cost = 24
            self.cost_calls = []
            self.deduct_calls = []
            self.refund_calls = []
            self.charge_keys = {}
            self.refund_keys = {}
            self.lose_first_charge_response = False
            self.lose_first_refund_response = False
            self.fail_refund_before_commit = False
            self.reject_next_charge = False

        def cost_of(self, kind, body):
            self.cost_calls.append((kind, dict(body)))
            return self.cost

        def deduct_points(self, username, cost, reason, transaction_key=""):
            self.deduct_calls.append((username, cost, reason, transaction_key))
            if self.reject_next_charge:
                self.reject_next_charge = False
                error = self.AuthPointsError()
                error.status = 402
                error.detail = "insufficient points"
                raise error
            if transaction_key in self.charge_keys:
                if self.charge_keys[transaction_key] != (username, cost):
                    raise AssertionError("charge key conflict")
                return 100 - cost
            if transaction_key:
                self.charge_keys[transaction_key] = (username, cost)
            if self.lose_first_charge_response:
                self.lose_first_charge_response = False
                error = self.AuthPointsError()
                error.status = 502
                error.detail = "response lost after commit"
                raise error
            return 100 - cost

        def refund_points(self, username, cost, reason, transaction_key=None):
            self.refund_calls.append((username, cost, reason, transaction_key))
            if self.fail_refund_before_commit:
                self.fail_refund_before_commit = False
                raise RuntimeError("refund unavailable before commit")
            if transaction_key in self.refund_keys:
                if self.refund_keys[transaction_key] != (username, cost):
                    raise AssertionError("refund key conflict")
                return 100
            if transaction_key:
                self.refund_keys[transaction_key] = (username, cost)
            if self.lose_first_refund_response:
                self.lose_first_refund_response = False
                raise RuntimeError("refund response lost after commit")
            return 100

        def get_points(self, username):
            return 100 - sum(cost for (_user, cost) in self.charge_keys.values())

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
            "fast_queue": core._fast_job_queue,
            "queued_ids": core._queued_job_ids,
            "canvas_access": core._short_drama_canvas_access,
        }
        core.JOB_DB = str(Path(self.tmp.name) / "content.db")
        core.AUDIO_DB = str(Path(self.tmp.name) / "audio.db")
        core.verify = lambda token: (
            {"username": token, "must_change": token == "locked"} if token else None
        )
        self.points = self.FakePoints()
        self.audio = self.FakeAudio()
        core._domains = lambda: (self.audio, self.points, video)
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
        core._fast_job_queue = queue.Queue(maxsize=8)
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
        self.quote_cache = {}

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
        core._fast_job_queue = self.originals["fast_queue"]
        core._queued_job_ids = self.originals["queued_ids"]
        core._short_drama_canvas_access = self.originals["canvas_access"]
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

    def _quoted_body(self, **changes):
        body = self._body(**changes)
        status, quote = self.request(
            "/api/gen/short-drama/asset-quote", body=body, with_quote=False,
        )
        self.assertEqual(200, status)
        return dict(body, quote_token=quote["quote_token"])

    def test_precharge_expired_quote_is_explicit_operation_terminal(self):
        body = self._quoted_body()
        with closing(core.jdb()) as conn:
            conn.execute("UPDATE short_drama_still_quotes SET expires_at=0 WHERE token=?",
                         (body["quote_token"],))
            conn.commit()
        status, response = self.request(
            "/api/gen/short-drama/generate-stills", body=body, with_quote=False,
            idempotency_key="expired-terminal-001",
        )
        self.assertEqual(400, status)
        self.assertIs(response.get("operation_terminal"), True)
        self.assertEqual([], self.points.deduct_calls)

    def test_precharge_revision_stage_acl_and_content_rejections_are_terminal(self):
        cases = []
        revision_body = self._quoted_body()
        cases.append(("revision", revision_body, lambda conn: conn.execute(
            "UPDATE short_drama_projects SET revision=revision+1 WHERE id=?", (self.project["id"],))))
        for name, body, mutate in cases:
            with self.subTest(case=name), closing(core.jdb()) as conn:
                mutate(conn); conn.commit()
            status, response = self.request(
                "/api/gen/short-drama/generate-stills", body=body, with_quote=False,
                idempotency_key="%s-terminal-001" % name,
            )
            self.assertEqual(409, status)
            self.assertIs(response.get("operation_terminal"), True)

        with closing(core.jdb()) as conn:
            conn.execute("UPDATE short_drama_projects SET revision=?,stage='stills_review' WHERE id=?",
                         (self.project["revision"], self.project["id"])); conn.commit()
        stage_body = self._quoted_body()
        with closing(core.jdb()) as conn:
            conn.execute("UPDATE short_drama_projects SET stage='completed' WHERE id=?", (self.project["id"],)); conn.commit()
        status, response = self.request(
            "/api/gen/short-drama/generate-stills", body=stage_body, with_quote=False,
            idempotency_key="stage-terminal-001",
        )
        self.assertEqual(400, status); self.assertIs(response.get("operation_terminal"), True)

        with closing(core.jdb()) as conn:
            conn.execute("UPDATE short_drama_projects SET stage='stills_review' WHERE id=?", (self.project["id"],)); conn.commit()
        acl_body = self._quoted_body()
        with closing(core.jdb()) as conn:
            conn.execute("UPDATE short_drama_projects SET board_id='board-terminal' WHERE id=?", (self.project["id"],)); conn.commit()
        core._short_drama_canvas_access = lambda _handler: {
            "board_id": "board-terminal", "role": "viewer",
        }
        status, response = self.request(
            "/api/gen/short-drama/generate-stills", body=acl_body, with_quote=False,
            idempotency_key="acl-terminal-001", board_id="board-terminal",
        )
        self.assertEqual(403, status); self.assertIs(response.get("operation_terminal"), True)
        with closing(core.jdb()) as conn:
            conn.execute("UPDATE short_drama_projects SET board_id=NULL WHERE id=?", (self.project["id"],)); conn.commit()
        core._short_drama_canvas_access = self.originals["canvas_access"]

        content_body = self._quoted_body()
        core.miniprogram_security.check_payload = mock.Mock(
            side_effect=core.miniprogram_security.ContentRejected("rejected")
        )
        status, response = self.request(
            "/api/gen/short-drama/generate-stills", body=content_body, with_quote=False,
            idempotency_key="content-terminal-001",
        )
        self.assertEqual(400, status); self.assertIs(response.get("operation_terminal"), True)
        self.assertEqual([], self.points.deduct_calls)

    def test_precharge_security_5xx_remains_ambiguous(self):
        body = self._quoted_body()
        core.miniprogram_security.check_payload = mock.Mock(
            side_effect=core.miniprogram_security.SecurityUnavailable("security unavailable")
        )
        status, response = self.request(
            "/api/gen/short-drama/generate-stills", body=body, with_quote=False,
            idempotency_key="security-ambiguous-001",
        )
        self.assertEqual(503, status)
        self.assertIsNot(response.get("operation_terminal"), True)
        self.assertEqual([], self.points.deduct_calls)

    def request(self, path, *, body=None, username="alice", idempotency_key=None,
                raw_body=None, method="POST", with_quote=True, board_id=None):
        if (with_quote and path == "/api/gen/short-drama/generate-stills"
                and isinstance(body, dict) and "quote_token" not in body):
            normalized, descriptor = short_drama_production.normalize_still_request(body)
            cache_key = (username, json.dumps(descriptor, sort_keys=True, ensure_ascii=False))
            quote = self.quote_cache.get(cache_key)
            if quote is None:
                quote_status, quote = self.request(
                    "/api/gen/short-drama/asset-quote", body=normalized,
                    username=username, with_quote=False,
                )
                if quote_status != 200:
                    return quote_status, quote
                self.quote_cache[cache_key] = quote
            body = dict(normalized, quote_token=quote["quote_token"])
        data = raw_body if raw_body is not None else json.dumps(
            body if body is not None else {}, ensure_ascii=False
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if username:
            headers["Authorization"] = "Bearer " + username
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        if board_id is not None:
            headers["X-Canvas-Board-Id"] = board_id
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

    def _voice_submission_body(self):
        with closing(core.jdb()) as conn:
            conn.execute(
                "INSERT INTO short_drama_characters "
                "(id,project_id,character_key,name,identity_text,personality,"
                "source_type,appearance_prompt,wardrobe_prompt,voice_key,"
                "voice_settings_json,sort_order) "
                "VALUES ('voice-route-character',?,'hero','主角','','',"
                "'ai_character','','','longwan','{}',0)",
                (self.project["id"],),
            )
            conn.execute(
                "UPDATE short_drama_scripts SET dialogue_lines_json=? "
                "WHERE project_id=?",
                (
                    json.dumps([{
                        "id": "voice-route-line",
                        "character_key": "hero", "text": "你好，欢迎来到黄雀。",
                    }], ensure_ascii=False),
                    self.project["id"],
                ),
            )
            conn.execute(
                "UPDATE short_drama_shots "
                "SET character_keys_json='[\"hero\"]',"
                "dialogue_line_ids_json='[\"voice-route-line\"]' "
                "WHERE id=?",
                (self.shot_id,),
            )
            conn.execute(
                "UPDATE short_drama_projects SET stage='voice_review' WHERE id=?",
                (self.project["id"],),
            )
            conn.commit()
        snapshot = core._short_drama_domain().short_drama_voice.get_voice_workspace(
            core.jdb, "alice", self.project["id"]
        )
        line = next(
            line for shot in snapshot["shots"] for line in shot["lines"]
        )
        item = {
            "line_id": line["id"], "voice_key": line["voice_key"],
            "speed": line["speed"], "pitch": line["pitch"], "volume": line["volume"],
        }
        quote_status, quote = self.request(
            "/api/gen/short-drama/voice-quote",
            body={
                "project_id": self.project["id"],
                "revision": snapshot["revision"], "items": [item],
            },
            with_quote=False,
        )
        self.assertEqual(200, quote_status)
        self.assertEqual(self.points.cost, quote["total_cost"])
        self.assertEqual(100, quote["points_left"])
        self.assertTrue(quote["can_submit"])
        self.assertEqual([], self.points.deduct_calls)
        submitted = {
            "project_id": self.project["id"],
            "revision": snapshot["revision"],
            **item,
            "quote_token": quote["items"][0]["quote_token"],
        }
        return submitted

    def test_voice_quote_submit_and_idempotent_replay_use_one_charge_and_job(self):
        submitted = self._voice_submission_body()
        first_status, first = self.request(
            "/api/gen/short-drama/generate-voice", body=submitted,
            with_quote=False, idempotency_key="voice-route-submit-001",
        )
        replay_status, replay = self.request(
            "/api/gen/short-drama/generate-voice", body=submitted,
            with_quote=False, idempotency_key="voice-route-submit-001",
        )
        self.assertEqual(200, first_status)
        self.assertEqual(200, replay_status)
        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(first["job_id"], replay["job_id"])
        self.assertEqual(1, len(self.points.deduct_calls))
        jobs = self._jobs()
        self.assertEqual(1, len(jobs))
        self.assertEqual("audio", jobs[0]["kind"])
        with closing(core.jdb()) as conn:
            self.assertEqual(1, conn.execute(
                "SELECT COUNT(*) FROM short_drama_voice_jobs"
            ).fetchone()[0])
            self.assertEqual("linked", conn.execute(
                "SELECT state FROM short_drama_voice_charge_attempts"
            ).fetchone()[0])

    def test_voice_accepted_attempt_recovers_before_later_acl_stage_and_voice_drift(self):
        submitted = self._voice_submission_body()
        self.points.lose_first_charge_response = True
        first_status, first = self.request(
            "/api/gen/short-drama/generate-voice", body=submitted,
            with_quote=False, idempotency_key="voice-route-drift-001",
        )
        self.assertEqual(502, first_status)
        self.assertEqual(
            "accepted",
            short_drama_voice.get_voice_attempt(
                core.jdb, "alice", "voice-route-drift-001",
            )["state"],
        )
        with closing(core.jdb()) as conn:
            conn.execute(
                "UPDATE short_drama_voice_quotes SET expires_at=0 "
                "WHERE token=?",
                (submitted["quote_token"],),
            )
            conn.execute(
                "UPDATE short_drama_projects "
                "SET revision=revision+1,stage='completed',board_id='board-later' "
                "WHERE id=?",
                (self.project["id"],),
            )
            conn.commit()
        core._short_drama_canvas_access = lambda _handler: {
            "board_id": "board-later", "role": "viewer",
        }
        self.audio.resolve_audio_provider_voice = mock.Mock(
            side_effect=ValueError("voice removed"),
        )

        retry_status, recovered = self.request(
            "/api/gen/short-drama/generate-voice", body=submitted,
            with_quote=False, idempotency_key="voice-route-drift-001",
            board_id="board-later",
        )

        self.assertEqual(200, retry_status)
        self.assertGreater(recovered["job_id"], 0)
        self.assertEqual(1, len(self.points.charge_keys))
        self.assertEqual(1, len(self._jobs()))
        self.audio.resolve_audio_provider_voice.assert_not_called()
        self.assertEqual(
            "linked",
            short_drama_voice.get_voice_attempt(
                core.jdb, "alice", "voice-route-drift-001",
            )["state"],
        )

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
        self.assertEqual(24, quote["cost"])
        self.assertEqual(2, quote["count"])
        self.assertEqual("still", quote["kind"])
        self.assertRegex(quote["quote_token"], r"^[0-9a-f]{32}$")
        self.assertEqual(self.shot_id, quote["shot_id"])
        self.assertEqual(self._body()["prompt"], quote["user_direction"])
        self.assertIn("User direction: " + self._body()["prompt"], quote["compiled_prompt"])
        self.assertRegex(quote["source_prompt_hash"], r"^[0-9a-f]{64}$")
        self.assertEqual("image", self.points.cost_calls[0][0])
        payload = self.points.cost_calls[0][1]
        self.assertEqual("banana", payload["provider"])
        self.assertEqual("nb2", payload["model"])
        self.assertEqual("hd", payload["quality"])
        self.assertEqual("9:16", payload["ratio"])
        self.assertEqual(2, payload["count"])
        self.assertEqual([], payload["short_drama_references"])
        self.assertEqual(self._body()["prompt"], payload["short_drama_raw_prompt"])
        self.assertIn("User direction: " + self._body()["prompt"], payload["prompt"])
        self.assertEqual([], self.points.deduct_calls)

    def test_routes_recheck_editor_viewer_demotion_and_removal_permissions(self):
        with closing(core.jdb()) as conn:
            conn.execute(
                "UPDATE short_drama_projects SET board_id='board-a' WHERE id=?",
                (self.project["id"],),
            )
            conn.commit()
        roles = {"editor": "editor", "viewer": "viewer"}

        def access(handler):
            username = handler._token()
            board_id = handler.headers.get("X-Canvas-Board-Id")
            role = roles.get(username)
            return {"board_id": board_id, "role": role} if role else None

        core._short_drama_canvas_access = access
        get_path = "/api/gen/short-drama/production?project_id=" + self.project["id"]
        viewer_read, _ = self.request(
            get_path, username="viewer", method="GET", raw_body=b"", board_id="board-a"
        )
        viewer_quote, _ = self.request(
            "/api/gen/short-drama/asset-quote", body=self._body(),
            username="viewer", board_id="board-a", with_quote=False,
        )
        editor_quote, _ = self.request(
            "/api/gen/short-drama/asset-quote", body=self._body(),
            username="editor", board_id="board-a", with_quote=False,
        )
        self.assertEqual(200, viewer_read)
        self.assertEqual(403, viewer_quote)
        self.assertEqual(200, editor_quote)

        roles["editor"] = "viewer"
        demoted, _ = self.request(
            "/api/gen/short-drama/asset-quote", body=self._body(),
            username="editor", board_id="board-a", with_quote=False,
        )
        roles.pop("editor")
        removed, _ = self.request(
            get_path, username="editor", method="GET", raw_body=b"", board_id="board-a"
        )
        wrong_board, _ = self.request(
            get_path, username="viewer", method="GET", raw_body=b"", board_id="board-b"
        )
        self.assertEqual(403, demoted)
        self.assertEqual(404, removed)
        self.assertEqual(404, wrong_board)

    def test_shared_editor_reads_owner_audio_assets_and_can_save_them(self):
        with closing(core.jdb()) as conn:
            conn.execute(
                "UPDATE short_drama_projects "
                "SET board_id='board-a',stage='assembly_review' WHERE id=?",
                (self.project["id"],),
            )
            revision = conn.execute(
                "SELECT revision FROM short_drama_projects WHERE id=?",
                (self.project["id"],),
            ).fetchone()[0]
            conn.commit()
        roles = {
            "alice": "owner", "editor": "editor", "viewer": "viewer",
        }
        core._short_drama_canvas_access = lambda handler: ({
            "board_id": handler.headers.get("X-Canvas-Board-Id"),
            "role": roles.get(handler._token()),
        } if roles.get(handler._token()) else None)
        path = (
            "/api/gen/short-drama/assembly/audio-assets?project_id="
            + self.project["id"] + "&limit=120"
        )

        editor_status, editor_assets = self.request(
            path, username="editor", method="GET", raw_body=b"",
            board_id="board-a",
        )
        viewer_status, viewer_assets = self.request(
            path, username="viewer", method="GET", raw_body=b"",
            board_id="board-a",
        )
        personal_status, personal_assets = self.request(
            "/api/gen/audio/assets?limit=120", username="editor",
            method="GET", raw_body=b"", board_id="board-a",
        )
        wrong_board, _ = self.request(
            path, username="editor", method="GET", raw_body=b"",
            board_id="board-b",
        )
        unauthenticated, _ = self.request(
            path, username="", method="GET", raw_body=b"",
            board_id="board-a",
        )
        roles.pop("editor")
        removed, _ = self.request(
            path, username="editor", method="GET", raw_body=b"",
            board_id="board-a",
        )
        roles["editor"] = "editor"

        self.assertEqual(200, editor_status)
        self.assertEqual([101], [item["id"] for item in editor_assets["items"]])
        self.assertEqual(200, viewer_status)
        self.assertEqual([101], [item["id"] for item in viewer_assets["items"]])
        self.assertEqual(200, personal_status)
        self.assertEqual([202], [item["id"] for item in personal_assets["items"]])
        self.assertEqual(404, wrong_board)
        self.assertEqual(401, unauthenticated)
        self.assertEqual(404, removed)
        self.assertEqual(
            [("alice", 120), ("alice", 120), ("editor", 120)],
            self.audio.list_calls,
        )

        config = {
            "subtitle": {
                "enabled": True, "preset": "white_outline",
                "position": "bottom",
            },
            "bgm": {
                "asset_id": 101, "volume": 0.18,
                "fade_in_ms": 500, "fade_out_ms": 800,
            },
            "sound_cues": [],
        }
        saved_status, saved = self.request(
            "/api/gen/short-drama/assembly/config",
            username="editor", method="PUT", board_id="board-a",
            body={
                "project_id": self.project["id"], "revision": revision,
                "assembly_revision": 1, "config": config,
            },
            with_quote=False,
        )
        self.assertEqual(200, saved_status)
        self.assertEqual(101, saved["config"]["bgm"]["asset_id"])

        config["bgm"]["asset_id"] = 202
        rejected_status, rejected = self.request(
            "/api/gen/short-drama/assembly/config",
            username="editor", method="PUT", board_id="board-a",
            body={
                "project_id": self.project["id"], "revision": revision,
                "assembly_revision": saved["assembly_revision"],
                "config": config,
            },
            with_quote=False,
        )
        self.assertEqual(400, rejected_status)
        self.assertEqual("sound_asset_missing", rejected["code"])

    def test_first_stage_routes_enforce_current_board_role_and_creation_scope(self):
        roles = {"owner": "owner", "editor": "editor", "viewer": "viewer"}
        core._short_drama_canvas_access = lambda handler: ({
            "board_id": handler.headers.get("X-Canvas-Board-Id"),
            "role": roles.get(handler._token()),
        } if roles.get(handler._token()) else None)
        payload = dict(_project_payload(), board_id="board-a")
        created_status, created = self.request(
            "/api/gen/short-drama/projects", body=payload, username="editor",
            board_id="board-a", with_quote=False,
        )
        self.assertEqual(200, created_status)
        project_path = "/api/gen/short-drama/project?id=" + created["id"]
        owner_read, _ = self.request(project_path, username="owner", method="GET",
                                     raw_body=b"", board_id="board-a")
        viewer_read, _ = self.request(project_path, username="viewer", method="GET",
                                      raw_body=b"", board_id="board-a")
        viewer_write, _ = self.request(project_path, username="viewer", method="PUT",
            board_id="board-a", body={"revision": created["revision"], "title": "blocked"},
            with_quote=False)
        spoofed, _ = self.request(
            "/api/gen/short-drama/projects", body=dict(payload, board_id="board-b"),
            username="editor", board_id="board-a", with_quote=False,
        )
        self.assertEqual(200, owner_read)
        self.assertEqual(200, viewer_read)
        self.assertEqual(403, viewer_write)
        self.assertEqual(403, spoofed)
        roles.pop("editor")
        removed, _ = self.request(project_path, username="editor", method="GET",
                                  raw_body=b"", board_id="board-a")
        self.assertEqual(404, removed)

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
        no_quote_status, _no_quote = self.request(
            path, body=self._body(), idempotency_key="still-no-quote-001",
            with_quote=False,
        )
        self.assertEqual(400, no_quote_status)
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

    def test_generic_insert_failure_replays_500_without_creating_again(self):
        path = "/api/gen/image"
        body = {
            "provider": "seedream", "prompt": "rainy doorway",
            "ratio": "9:16", "count": 1,
        }
        failure = jobs_store.PaidJobInsertError("refunded", "generic-insert-001")
        with mock.patch.object(
            jobs_store, "create_paid_job", side_effect=failure
        ) as create_paid_job:
            first_status, first = self.request(
                path, body=body, idempotency_key="generic-insert-failure-001"
            )
            replay_status, replay = self.request(
                path, body=body, idempotency_key="generic-insert-failure-001"
            )

        self.assertEqual((500, 500), (first_status, replay_status))
        self.assertEqual(first, replay)
        self.assertIs(first.get("operation_terminal"), True)
        self.assertNotIn("_http_status", replay)
        self.assertEqual(1, create_paid_job.call_count)

    def test_generic_insert_failure_with_pending_refund_is_not_terminal(self):
        path = "/api/gen/image"
        body = {
            "provider": "seedream", "prompt": "rainy doorway",
            "ratio": "9:16", "count": 1,
        }
        failure = jobs_store.PaidJobInsertError("queued", "generic-insert-queued-001")
        with mock.patch.object(
            jobs_store, "create_paid_job", side_effect=failure
        ):
            status, response = self.request(
                path, body=body, idempotency_key="generic-insert-queued-001"
            )

        self.assertEqual(500, status)
        self.assertNotIn("operation_terminal", response)

    def test_generic_queue_refund_replays_429_without_free_retry(self):
        path = "/api/gen/image"
        body = {
            "provider": "seedream", "prompt": "rainy doorway",
            "ratio": "9:16", "count": 1,
        }
        core._image_job_queue = queue.Queue(maxsize=1)
        core._image_job_queue.put_nowait(999999)

        first_status, first = self.request(
            path, body=body, idempotency_key="generic-queue-failure-001"
        )
        core._image_job_queue.get_nowait()
        replay_status, replay = self.request(
            path, body=body, idempotency_key="generic-queue-failure-001"
        )

        self.assertEqual((429, 429), (first_status, replay_status))
        self.assertEqual(first, replay)
        self.assertEqual(1, len(self.points.deduct_calls))
        self.assertEqual(1, len(self.points.refund_calls))
        self.assertEqual(1, len(self._jobs()))
        self.assertEqual(0, core._image_job_queue.qsize())

    def test_generic_asset_failure_replays_500_without_free_retry(self):
        path = "/api/gen/video"
        body = {"prompt": "rainy doorway"}
        core.HANDLERS["video"] = lambda payload: payload
        with mock.patch.object(
            video, "validate_video_payload",
            side_effect=lambda payload, _username: dict(payload),
        ), mock.patch.object(
            video, "record_video_pending_asset",
            side_effect=RuntimeError("asset write failed"),
        ) as record_asset:
            first_status, first = self.request(
                path, body=body, idempotency_key="generic-asset-failure-001"
            )
            replay_status, replay = self.request(
                path, body=body, idempotency_key="generic-asset-failure-001"
            )

        self.assertEqual((500, 500), (first_status, replay_status))
        self.assertEqual(first, replay)
        self.assertEqual(1, len(self.points.deduct_calls))
        self.assertEqual(1, len(self.points.refund_calls))
        self.assertEqual(1, len(self._jobs()))
        self.assertEqual(1, record_asset.call_count)

    def test_submission_charges_exact_quote_and_consumes_it_atomically(self):
        body = self._body()
        quote_status, quote = self.request(
            "/api/gen/short-drama/asset-quote", body=body, with_quote=False,
        )
        self.assertEqual(200, quote_status)
        self.points.cost = 99
        submitted = dict(body, quote_token=quote["quote_token"])

        status, accepted = self.request(
            "/api/gen/short-drama/generate-stills", body=submitted,
            idempotency_key="quote-exact-001", with_quote=False,
        )
        replay_status, replayed = self.request(
            "/api/gen/short-drama/generate-stills", body=submitted,
            idempotency_key="quote-exact-001", with_quote=False,
        )
        reused_status, _reused = self.request(
            "/api/gen/short-drama/generate-stills", body=submitted,
            idempotency_key="quote-exact-002", with_quote=False,
        )

        self.assertEqual(200, status)
        self.assertEqual(24, accepted["cost"])
        self.assertEqual(accepted, replayed)
        self.assertEqual(200, replay_status)
        self.assertEqual(400, reused_status)
        self.assertEqual([24], [item[1] for item in self.points.deduct_calls])
        self.assertEqual(1, len(self._jobs()))

    def test_expired_quote_is_rejected_before_deduction(self):
        body = self._body()
        _, quote = self.request(
            "/api/gen/short-drama/asset-quote", body=body, with_quote=False,
        )
        with closing(core.jdb()) as conn:
            conn.execute(
                "UPDATE short_drama_still_quotes SET expires_at=0 WHERE token=?",
                (quote["quote_token"],),
            )
            conn.commit()

        status, _response = self.request(
            "/api/gen/short-drama/generate-stills",
            body=dict(body, quote_token=quote["quote_token"]),
            idempotency_key="quote-expired-001", with_quote=False,
        )
        self.assertEqual(400, status)
        self.assertEqual([], self.points.deduct_calls)
        self.assertEqual([], self._jobs())

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

    def test_shutdown_does_not_resume_an_accepted_still_charge_attempt(self):
        path = "/api/gen/short-drama/generate-stills"
        body = self._body()
        self.points.lose_first_charge_response = True
        first_status, _ = self.request(
            path, body=body, idempotency_key="still-shutdown-accepted-001",
        )
        self.assertEqual(502, first_status)
        self.assertEqual(
            "accepted",
            short_drama_production.get_charge_attempt(
                core.jdb, "alice", "still-shutdown-accepted-001",
            )["state"],
        )
        before_deducts = len(self.points.deduct_calls)

        core._shutting_down.set()
        try:
            retry_status, retry = self.request(
                path, body=body, idempotency_key="still-shutdown-accepted-001",
            )
        finally:
            core._shutting_down.clear()

        self.assertEqual(503, retry_status)
        self.assertEqual("shutting_down", retry["code"])
        self.assertNotIn("未扣点", retry["detail"])
        self.assertEqual(before_deducts, len(self.points.deduct_calls))
        self.assertEqual([], self._jobs())
        self.assertEqual(0, core._image_job_queue.qsize())
        self.assertEqual(
            "accepted",
            short_drama_production.get_charge_attempt(
                core.jdb, "alice", "still-shutdown-accepted-001",
            )["state"],
        )
        recovered_status, recovered = self.request(
            path, body=body, idempotency_key="still-shutdown-accepted-001",
        )
        self.assertEqual(200, recovered_status)
        self.assertGreater(recovered["job_id"], 0)
        self.assertEqual(1, len(self.points.charge_keys))
        self.assertEqual(1, len(self._jobs()))
        self.assertEqual(1, core._image_job_queue.qsize())

    def test_shutdown_does_not_create_a_job_for_a_charged_still_attempt(self):
        path = "/api/gen/short-drama/generate-stills"
        body = self._body()
        self.points.lose_first_charge_response = True
        first_status, _ = self.request(
            path, body=body, idempotency_key="still-shutdown-charged-001",
        )
        self.assertEqual(502, first_status)
        short_drama_production.mark_attempt_charged(
            core.jdb, "alice", "still-shutdown-charged-001",
            self.points.get_points("alice"),
        )

        core._shutting_down.set()
        try:
            retry_status, retry = self.request(
                path, body=body, idempotency_key="still-shutdown-charged-001",
            )
        finally:
            core._shutting_down.clear()

        self.assertEqual(503, retry_status)
        self.assertEqual("shutting_down", retry["code"])
        self.assertNotIn("未扣点", retry["detail"])
        self.assertEqual(1, len(self.points.deduct_calls))
        self.assertEqual([], self._jobs())
        self.assertEqual(0, core._image_job_queue.qsize())
        self.assertEqual(
            "charged",
            short_drama_production.get_charge_attempt(
                core.jdb, "alice", "still-shutdown-charged-001",
            )["state"],
        )
        recovered_status, recovered = self.request(
            path, body=body, idempotency_key="still-shutdown-charged-001",
        )
        self.assertEqual(200, recovered_status)
        self.assertGreater(recovered["job_id"], 0)
        self.assertEqual(1, len(self.points.deduct_calls))
        self.assertEqual(1, len(self._jobs()))
        self.assertEqual(1, core._image_job_queue.qsize())

    def test_shutdown_replays_a_linked_still_without_reenqueuing_it(self):
        path = "/api/gen/short-drama/generate-stills"
        body = self._body()
        status, accepted = self.request(
            path, body=body, idempotency_key="still-shutdown-linked-001",
        )
        self.assertEqual(200, status)
        core._image_job_queue = queue.Queue(maxsize=8)
        core._queued_job_ids = set()
        with closing(core.jdb()) as conn:
            conn.execute(
                "UPDATE submission_idempotency SET response_json=NULL "
                "WHERE username='alice' AND endpoint=? AND idem_key=?",
                (path, "still-shutdown-linked-001"),
            )
            conn.commit()

        core._shutting_down.set()
        try:
            self.assertEqual(0, core._recover_pending_jobs())
            self.assertEqual(0, core._image_job_queue.qsize())
            replay_status, replay = self.request(
                path, body=body, idempotency_key="still-shutdown-linked-001",
            )
        finally:
            core._shutting_down.clear()

        self.assertEqual(200, replay_status)
        self.assertEqual(accepted, replay)
        self.assertEqual(0, core._image_job_queue.qsize())
        core._recover_pending_jobs()
        self.assertEqual(1, core._image_job_queue.qsize())

    def test_shutdown_allows_an_existing_refund_to_reconcile(self):
        path = "/api/gen/short-drama/generate-stills"
        body = self._body()
        self.points.fail_refund_before_commit = True
        with mock.patch.object(
            short_drama_production, "record_attempt_job",
            side_effect=RuntimeError("association failed"),
        ):
            first_status, first = self.request(
                path, body=body, idempotency_key="still-shutdown-refund-001",
            )
        self.assertEqual(503, first_status)
        self.assertEqual("refund_pending", first["code"])

        core._shutting_down.set()
        try:
            replay_status, replay = self.request(
                path, body=body, idempotency_key="still-shutdown-refund-001",
            )
        finally:
            core._shutting_down.clear()

        self.assertEqual(500, replay_status)
        self.assertIn("detail", replay)
        self.assertEqual(
            "refunded",
            short_drama_production.get_charge_attempt(
                core.jdb, "alice", "still-shutdown-refund-001",
            )["state"],
        )
        self.assertEqual(1, len(self.points.refund_keys))
        self.assertEqual(0, core._image_job_queue.qsize())

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

    def test_association_failure_refund_consumes_quote_and_attempt(self):
        path = "/api/gen/short-drama/generate-stills"
        with mock.patch.object(
            short_drama_production, "record_attempt_job",
            side_effect=RuntimeError("association failed"),
        ):
            status, failed = self.request(
                path, body=self._body(), idempotency_key="still-assoc-001"
            )

        self.assertEqual(500, status)
        self.assertEqual(1, len(self.points.deduct_calls))
        self.assertEqual(1, len(self.points.refund_calls))
        self.assertEqual([], self._jobs())
        self.assertEqual(
            "refunded",
            short_drama_production.get_charge_attempt(
                core.jdb, "alice", "still-assoc-001",
            )["state"],
        )
        self.assertEqual(1, self._idempotency_count())

        retry_status, retried = self.request(
            path, body=self._body(), idempotency_key="still-assoc-001"
        )
        self.assertEqual(500, retry_status)
        self.assertIn("detail", retried)
        self.assertEqual(1, len(self.points.deduct_calls))
        self.quote_cache.clear()
        fresh_status, fresh = self.request(
            path, body=self._body(), idempotency_key="still-assoc-002"
        )
        self.assertEqual(200, fresh_status)
        self.assertEqual(self.project["id"], fresh["project_id"])
        self.assertEqual(2, len(self.points.deduct_calls))

    def test_queue_full_refund_consumes_quote_and_requires_fresh_attempt(self):
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
        self.assertEqual(1, self._idempotency_count())

        core._image_job_queue.get_nowait()
        retry_status, retried = self.request(
            "/api/gen/short-drama/generate-stills", body=self._body(),
            idempotency_key="still-queue-001",
        )
        self.assertEqual(429, retry_status)
        self.assertEqual("queue_full", retried["code"])
        self.quote_cache.clear()
        fresh_status, fresh = self.request(
            "/api/gen/short-drama/generate-stills", body=self._body(),
            idempotency_key="still-queue-002",
        )
        self.assertEqual(200, fresh_status)
        self.assertEqual(self.project["id"], fresh["project_id"])
        self.assertEqual(2, len(self.points.deduct_calls))
        self.assertEqual(1, len(self.points.refund_calls))
        self.assertEqual(1, core._image_job_queue.qsize())

    def test_failed_queue_attempt_replays_durable_http_status_after_completion_crash(self):
        path = "/api/gen/short-drama/generate-stills"
        core._image_job_queue = queue.Queue(maxsize=1)
        core._image_job_queue.put_nowait(999999)
        status, failed = self.request(
            path, body=self._body(), idempotency_key="still-queue-crash-001",
        )
        self.assertEqual(429, status)
        self.assertEqual("queue_full", failed["code"])
        with closing(core.jdb()) as conn:
            conn.execute(
                "UPDATE submission_idempotency SET response_json=NULL "
                "WHERE username='alice' AND endpoint=? AND idem_key=?",
                (path, "still-queue-crash-001"),
            )
            conn.commit()

        replay_status, replay = self.request(
            path, body=self._body(), idempotency_key="still-queue-crash-001",
        )

        self.assertEqual(429, replay_status)
        self.assertEqual("queue_full", replay["code"])
        self.assertEqual(1, len(self.points.charge_keys))
        self.assertEqual(1, len(self.points.refund_keys))

    def test_lost_auth_response_retries_same_charge_once_and_creates_one_job(self):
        self.points.lose_first_charge_response = True
        path = "/api/gen/short-drama/generate-stills"
        first_status, _ = self.request(
            path, body=self._body(), idempotency_key="still-lost-auth-001"
        )
        retry_status, accepted = self.request(
            path, body=self._body(), idempotency_key="still-lost-auth-001"
        )
        self.assertEqual(502, first_status)
        self.assertEqual(200, retry_status)
        self.assertGreater(accepted["job_id"], 0)
        self.assertEqual(1, len(self.points.charge_keys))
        self.assertEqual(1, len(self._jobs()))
        self.assertEqual(1, core._image_job_queue.qsize())

    def test_definitive_auth_rejection_is_durable_and_allows_fresh_attempt(self):
        path = "/api/gen/short-drama/generate-stills"
        self.points.reject_next_charge = True
        first_status, first = self.request(
            path, body=self._body(), idempotency_key="still-insufficient-001",
        )
        replay_status, replay = self.request(
            path, body=self._body(), idempotency_key="still-insufficient-001",
        )
        self.assertEqual((402, 402), (first_status, replay_status))
        self.assertEqual(first, replay)
        self.assertEqual("failed", short_drama_production.get_charge_attempt(
            core.jdb, "alice", "still-insufficient-001",
        )["state"])
        self.assertEqual(1, len(self.points.deduct_calls))
        self.quote_cache.clear()
        fresh_status, fresh = self.request(
            path, body=self._body(), idempotency_key="still-insufficient-002",
        )
        self.assertEqual(200, fresh_status)
        self.assertGreater(fresh["job_id"], 0)

    def test_ambiguous_attempt_reservation_prevents_project_budget_overrun(self):
        with closing(core.jdb()) as conn:
            conn.execute(
                "UPDATE short_drama_projects SET point_budget=30 WHERE id=?",
                (self.project["id"],),
            )
            conn.commit()
        self.points.lose_first_charge_response = True
        first_status, _ = self.request(
            "/api/gen/short-drama/generate-stills", body=self._body(),
            idempotency_key="budget-reserve-first-001",
        )
        self.quote_cache.clear()
        second_status, second = self.request(
            "/api/gen/short-drama/generate-stills", body=self._body(),
            idempotency_key="budget-reserve-second-001",
        )
        self.assertEqual(502, first_status)
        self.assertEqual(400, second_status)
        self.assertEqual("point_budget_exceeded", second["code"])
        snapshot = short_drama_production.get_production(
            core.jdb, "alice", self.project["id"],
        )
        self.assertEqual(24, snapshot["reserved_points"])
        self.assertEqual(1, len(self.points.deduct_calls))

    def test_refund_pending_response_is_retryable_until_sweeper_finishes(self):
        path = "/api/gen/short-drama/generate-stills"
        self.points.fail_refund_before_commit = True
        with mock.patch.object(
            short_drama_production, "record_attempt_job",
            side_effect=RuntimeError("association failed"),
        ):
            status, pending = self.request(
                path, body=self._body(), idempotency_key="still-pending-refund-001",
            )
        self.assertEqual(503, status)
        self.assertEqual("refund_pending", pending["code"])
        self.assertTrue(pending["retryable"])
        self.assertEqual(1, short_drama_production.retry_attempt_refunds(core.jdb, self.points))
        replay_status, replay = self.request(
            path, body=self._body(), idempotency_key="still-pending-refund-001",
        )
        self.assertEqual(500, replay_status)
        self.assertNotEqual("refund_pending", replay.get("code"))

    def test_accepted_lost_auth_attempt_ignores_later_quote_project_and_acl_drift(self):
        self.points.lose_first_charge_response = True
        path = "/api/gen/short-drama/generate-stills"
        body = self._body()
        first_status, _ = self.request(
            path, body=body, idempotency_key="still-drift-auth-001"
        )
        self.assertEqual(502, first_status)
        with closing(core.jdb()) as conn:
            conn.execute("UPDATE short_drama_still_quotes SET expires_at=0")
            conn.execute(
                "UPDATE short_drama_projects SET revision=revision+1,stage='completed',board_id='board-later' WHERE id=?",
                (self.project["id"],),
            )
            conn.commit()
        core._short_drama_canvas_access = lambda _handler: None

        retry_status, accepted = self.request(
            path, body=body, idempotency_key="still-drift-auth-001"
        )

        self.assertEqual(200, retry_status)
        self.assertGreater(accepted["job_id"], 0)
        self.assertEqual(1, len(self.points.charge_keys))
        self.assertEqual(1, len(self._jobs()))

    def test_association_failure_records_refund_intent_before_refund_and_replays_one_refund(self):
        path = "/api/gen/short-drama/generate-stills"
        self.points.lose_first_refund_response = True
        with mock.patch.object(
            short_drama_production, "record_attempt_job",
            side_effect=RuntimeError("association failed"),
        ):
            first_status, _ = self.request(
                path, body=self._body(), idempotency_key="still-refund-lost-001"
            )
        attempt = short_drama_production.get_charge_attempt(
            core.jdb, "alice", "still-refund-lost-001",
        )
        self.assertEqual(503, first_status)
        self.assertEqual("refund_pending", attempt["state"])
        self.assertIsNotNone(attempt["terminal_response"])

        retry_status, retry = self.request(
            path, body=self._body(), idempotency_key="still-refund-lost-001"
        )

        self.assertEqual(500, retry_status)
        self.assertIn("detail", retry)
        self.assertEqual(1, len(self.points.refund_keys))
        self.assertEqual(2, len(self.points.refund_calls))
        self.assertEqual(
            "refunded",
            short_drama_production.get_charge_attempt(
                core.jdb, "alice", "still-refund-lost-001",
            )["state"],
        )
        self.assertEqual([], [j for j in self._jobs() if j["status"] == "pending"])

    def test_refund_unavailable_before_commit_is_retried_with_same_key(self):
        path = "/api/gen/short-drama/generate-stills"
        self.points.fail_refund_before_commit = True
        with mock.patch.object(
            short_drama_production, "record_attempt_job",
            side_effect=RuntimeError("association failed"),
        ):
            first_status, _ = self.request(
                path, body=self._body(), idempotency_key="still-refund-before-001"
            )
        first_attempt = short_drama_production.get_charge_attempt(
            core.jdb, "alice", "still-refund-before-001",
        )
        self.assertEqual(503, first_status)
        self.assertEqual("refund_pending", first_attempt["state"])

        retry_status, _ = self.request(
            path, body=self._body(), idempotency_key="still-refund-before-001"
        )

        self.assertEqual(500, retry_status)
        keys = [call[3] for call in self.points.refund_calls]
        self.assertEqual(2, len(keys))
        self.assertEqual(1, len(set(keys)))

    def test_max_length_http_idempotency_key_uses_bounded_auth_charge_key(self):
        status, _ = self.request(
            "/api/gen/short-drama/generate-stills", body=self._body(),
            idempotency_key="z" * 128,
        )
        self.assertEqual(200, status)
        transaction_key = self.points.deduct_calls[0][3]
        self.assertRegex(transaction_key, r"^still-charge:[0-9a-f]{64}$")
        self.assertLessEqual(len(transaction_key), 160)

    def test_retry_recovers_committed_job_when_http_completion_was_lost(self):
        path = "/api/gen/short-drama/generate-stills"
        status, accepted = self.request(
            path, body=self._body(), idempotency_key="still-lost-http-001"
        )
        self.assertEqual(200, status)
        with closing(core.jdb()) as conn:
            conn.execute(
                "UPDATE submission_idempotency SET response_json=NULL "
                "WHERE username='alice' AND endpoint=? AND idem_key=?",
                (path, "still-lost-http-001"),
            )
            conn.commit()
        retry_status, recovered = self.request(
            path, body=self._body(), idempotency_key="still-lost-http-001"
        )
        self.assertEqual(200, retry_status)
        self.assertEqual(accepted["job_id"], recovered["job_id"])
        self.assertEqual(1, len(self.points.charge_keys))
        self.assertEqual(1, len(self._jobs()))
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
        self.assertEqual(1, len(self.points.cost_calls))

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
        self.assertEqual(1, len(self.points.cost_calls))

        upstream_guard.exhausted_reason = lambda kind, payload: "upstream exhausted"
        upstream_status, upstream = self.request(
            path, body=self._body(), idempotency_key="still-guard-005"
        )
        self.assertEqual(503, upstream_status)
        self.assertEqual("upstream_exhausted", upstream["code"])
        self.assertEqual(1, len(self.points.cost_calls))
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
