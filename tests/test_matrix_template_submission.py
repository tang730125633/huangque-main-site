from contextlib import closing
import json
import pathlib
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from content_domains import matrix_template_submission, submission_idempotency


class FakePoints:
    class AuthPointsError(Exception):
        def __init__(self, status, detail, data=None):
            super().__init__(detail)
            self.status = status
            self.detail = detail
            self.data = data or {}

    def __init__(self):
        self.ledger = {}
        self.deductions = []
        self.refunds = []
        self.crash_after_deduct = False
        self.crash_after_refund = False

    def get_points_transaction(self, key):
        value = self.ledger.get(key)
        return dict(value) if value else None

    def deduct_points(self, username, amount, reason, transaction_key=""):
        if transaction_key not in self.ledger:
            self.deductions.append((username, amount, transaction_key))
            self.ledger[transaction_key] = {
                "username": username, "delta": -int(amount), "after_points": 95,
            }
        if self.crash_after_deduct:
            self.crash_after_deduct = False
            raise SystemExit("hard exit after confirmed deduction")
        return self.ledger[transaction_key]["after_points"]

    def refund_points(self, username, amount, reason, transaction_key=""):
        if transaction_key not in self.ledger:
            self.refunds.append((username, amount, transaction_key))
            self.ledger[transaction_key] = {
                "username": username, "delta": int(amount), "after_points": 100,
            }
        if self.crash_after_refund:
            self.crash_after_refund = False
            raise SystemExit("hard exit after confirmed refund")
        return self.ledger[transaction_key]["after_points"]


class MatrixTemplateSubmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.temp.name) / "jobs.db"
        self.clock = {"now": 2_000_000_000}
        self.points = FakePoints()
        self.body = {
            "top_text": "有效标题", "bottom_text": "关注查看更多",
            "template_id": "native-bold", "bgm": True,
        }
        self.key = "matrix-hard-crash-0001"
        with closing(self.db()) as connection:
            connection.execute("""CREATE TABLE jobs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,username TEXT NOT NULL,cost INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',payload TEXT NOT NULL,
                result TEXT,error TEXT,refunded INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL,owner TEXT
            )""")
            submission_idempotency.ensure_table(connection)
            matrix_template_submission.ensure_table(connection)
            connection.commit()
        state, _ = submission_idempotency.begin(
            self.db, "alice", "/api/gen/matrix-template", self.key, self.body,
        )
        self.assertEqual(state, "new")

    def tearDown(self):
        self.temp.cleanup()

    def db(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def now(self):
        return self.clock["now"]

    def attempt(self):
        return matrix_template_submission.get(
            self.db, "alice", "/api/gen/matrix-template", self.key,
        )

    def job_count(self):
        with closing(self.db()) as connection:
            return connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

    def recover(self, body_marker=True):
        return matrix_template_submission.recover(
            self.db, self.points, "alice", "/api/gen/matrix-template", self.key,
            body=self.body if body_marker else None,
            cost=5 if body_marker else None, owner="content", now=self.now(),
        )

    def begin_paid_child(self, kind, suffix):
        endpoint = "/api/gen/%s" % kind
        key = "digital-human-%s-%s" % (kind, suffix)
        body = {
            "prompt": "locked child", "digital_human_run_id": "dh-run-0001",
            "digital_human_stage": "material" if kind == "image" else "talking",
        }
        state, _ = submission_idempotency.begin(
            self.db, "alice", endpoint, key, body,
        )
        self.assertEqual("new", state)
        return endpoint, key, body

    def recover_paid_child(self, kind, endpoint, key, body=None):
        return matrix_template_submission.recover(
            self.db, self.points, "alice", endpoint, key, body=body,
            cost=7 if body is not None else None, owner="content",
            now=self.now(), kind=kind,
        )

    def test_hard_exit_after_deduction_recovers_one_job_without_second_charge(self):
        self.points.crash_after_deduct = True
        with mock.patch.object(matrix_template_submission.time, "time", side_effect=self.now), \
             self.assertRaises(SystemExit):
            self.recover()
        self.assertEqual(self.attempt()["state"], "charging")
        self.assertEqual(self.job_count(), 0)
        self.assertEqual(len(self.points.deductions), 1)

        self.clock["now"] += matrix_template_submission.LEASE_SECONDS + 1
        self.assertEqual(len(matrix_template_submission.recoverable(
            self.db, now=self.now(),
        )), 1)
        with mock.patch.object(matrix_template_submission.time, "time", side_effect=self.now):
            recovered = self.recover(body_marker=False)
        self.assertEqual(recovered["state"], "linked")
        self.assertEqual(self.job_count(), 1)
        self.assertEqual(len(self.points.deductions), 1)
        replay_state, response = submission_idempotency.replay_existing(
            self.db, "alice", "/api/gen/matrix-template", self.key, [self.body],
        )
        self.assertEqual(replay_state, "replay")
        self.assertEqual(response["job_id"], recovered["job_id"])

    def test_hard_exit_after_prepare_before_deduction_resumes_once(self):
        prepared = matrix_template_submission.prepare(
            self.db, "alice", "/api/gen/matrix-template", self.key,
            self.body, 5, now=self.now(),
        )
        self.assertEqual(prepared["state"], "prepared")
        self.assertEqual(self.points.deductions, [])
        self.assertEqual(self.job_count(), 0)
        self.assertEqual(len(matrix_template_submission.recoverable(
            self.db, now=self.now(),
        )), 1)
        with mock.patch.object(matrix_template_submission.time, "time", side_effect=self.now):
            recovered = self.recover(body_marker=False)
        self.assertEqual(recovered["state"], "linked")
        self.assertEqual(len(self.points.deductions), 1)
        self.assertEqual(self.job_count(), 1)

    def test_execution_payload_is_frozen_separately_from_idempotency_input(self):
        execution = dict(self.body, duration=11, semantic_layout={
            "version": 1, "model": "gpt-4.1-mini",
            "source_sha256": "a" * 64, "top1_end": 3,
            "top_break_after": [3], "bottom_break_after": [],
        })
        prepared = matrix_template_submission.prepare(
            self.db, "alice", "/api/gen/matrix-template", self.key,
            self.body, 5, now=self.now(), execution_body=execution,
        )
        self.assertEqual(self.body, prepared["input"])
        self.assertEqual(execution, prepared["execution"])
        self.assertTrue(prepared["execution_frozen"])

        changed_execution = dict(execution, duration=12)
        replayed = matrix_template_submission.prepare(
            self.db, "alice", "/api/gen/matrix-template", self.key,
            self.body, 5, now=self.now(), execution_body=changed_execution,
        )
        self.assertEqual(execution, replayed["execution"])

        recovered = self.recover(body_marker=False)
        self.assertEqual("linked", recovered["state"])
        with closing(self.db()) as connection:
            payload = json.loads(connection.execute(
                "SELECT payload FROM jobs WHERE id=?", (recovered["job_id"],),
            ).fetchone()[0])
        self.assertEqual(execution, payload)

    def test_legacy_attempt_without_execution_payload_uses_original_input(self):
        matrix_template_submission.prepare(
            self.db, "alice", "/api/gen/matrix-template", self.key,
            self.body, 5, now=self.now(),
        )
        with closing(self.db()) as connection:
            connection.execute(
                "UPDATE matrix_template_submission_attempts "
                "SET execution_json='' WHERE username='alice' AND idem_key=?",
                (self.key,),
            )
            connection.commit()
        attempt = self.attempt()
        self.assertFalse(attempt["execution_frozen"])
        self.assertEqual(self.body, attempt["execution"])

    def test_hard_exit_after_job_commit_replays_linked_job(self):
        real_create = matrix_template_submission.jobs_store.create_job_after_charge

        def create_then_exit(*args, **kwargs):
            real_create(*args, **kwargs)
            raise SystemExit("hard exit after job and accepted claim commit")

        with mock.patch.object(matrix_template_submission.time, "time", side_effect=self.now), \
             mock.patch.object(
                 matrix_template_submission.jobs_store, "create_job_after_charge",
                 side_effect=create_then_exit,
             ), self.assertRaises(SystemExit):
            self.recover()
        linked = self.attempt()
        self.assertEqual(linked["state"], "linked")
        self.assertEqual(self.job_count(), 1)
        self.assertEqual(len(self.points.deductions), 1)

        with mock.patch.object(matrix_template_submission.time, "time", side_effect=self.now):
            replayed = self.recover(body_marker=False)
        self.assertEqual(replayed["job_id"], linked["job_id"])
        self.assertEqual(self.job_count(), 1)
        self.assertEqual(len(self.points.deductions), 1)

    def test_director_copy_hard_exit_after_job_commit_replays_original_job(self):
        endpoint = "/api/gen/copy"
        key = "director-production-hard-crash-0001"
        body = {
            "prompt": "energy drink; buy three get one free",
            "format": "script", "style": "种草", "dur": "30s",
            "platform": "抖音", "ctype": "分镜脚本",
            "source_page": "script",
        }
        state, _ = submission_idempotency.begin(
            self.db, "alice", endpoint, key, body,
        )
        self.assertEqual("new", state)
        real_create = matrix_template_submission.jobs_store.create_job_after_charge

        def create_then_exit(*args, **kwargs):
            real_create(*args, **kwargs)
            raise SystemExit("hard exit after Director copy job commit")

        with mock.patch.object(
            matrix_template_submission.jobs_store, "create_job_after_charge",
            side_effect=create_then_exit,
        ), self.assertRaises(SystemExit):
            matrix_template_submission.recover(
                self.db, self.points, "alice", endpoint, key,
                body=body, cost=5, owner="content", now=self.now(), kind="copy",
            )

        replay_state, replay = submission_idempotency.replay_existing(
            self.db, "alice", endpoint, key, [body],
        )
        linked = matrix_template_submission.recover(
            self.db, self.points, "alice", endpoint, key,
            owner="content", now=self.now(), kind="copy",
        )
        self.assertEqual("replay", replay_state)
        self.assertEqual("linked", linked["state"])
        self.assertEqual("copy", linked["kind"])
        self.assertEqual(linked["job_id"], replay["job_id"])
        self.assertEqual(1, self.job_count())
        self.assertEqual(1, len(self.points.deductions))

    def test_concurrent_recovery_uses_one_lease_one_charge_and_one_job(self):
        matrix_template_submission.prepare(
            self.db, "alice", "/api/gen/matrix-template", self.key,
            self.body, 5, now=self.now(),
        )
        original_deduct = self.points.deduct_points
        entered, release = threading.Event(), threading.Event()

        def slow_deduct(*args, **kwargs):
            entered.set()
            release.wait(3)
            return original_deduct(*args, **kwargs)

        self.points.deduct_points = slow_deduct
        results, errors = [], []

        def run():
            try:
                results.append(self.recover(body_marker=False))
            except Exception as exc:
                errors.append(exc)

        first = threading.Thread(target=run)
        second = threading.Thread(target=run)
        with mock.patch.object(matrix_template_submission.time, "time", side_effect=self.now):
            first.start()
            self.assertTrue(entered.wait(2))
            second.start(); second.join(2)
            release.set(); first.join(3)
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], matrix_template_submission.AttemptInProgress)
        self.assertEqual(len(self.points.deductions), 1)
        self.assertEqual(self.job_count(), 1)

    def test_job_insert_failure_persists_and_confirms_one_refund(self):
        with mock.patch.object(matrix_template_submission.time, "time", side_effect=self.now), \
             mock.patch.object(
                 matrix_template_submission.jobs_store, "create_job_after_charge",
                 side_effect=RuntimeError("disk full"),
             ):
            refunded = self.recover()
        self.assertEqual(refunded["state"], "refunded")
        self.assertEqual(self.job_count(), 0)
        self.assertEqual(len(self.points.deductions), 1)
        self.assertEqual(len(self.points.refunds), 1)
        replay_state, response = submission_idempotency.replay_existing(
            self.db, "alice", "/api/gen/matrix-template", self.key, [self.body],
        )
        self.assertEqual(replay_state, "replay")
        self.assertTrue(response["operation_terminal"])

    def test_hard_exit_after_refund_recovers_without_second_refund(self):
        self.points.crash_after_refund = True
        with mock.patch.object(matrix_template_submission.time, "time", side_effect=self.now), \
             mock.patch.object(
                 matrix_template_submission.jobs_store, "create_job_after_charge",
                 side_effect=RuntimeError("disk full"),
             ), self.assertRaises(SystemExit):
            self.recover()
        self.assertEqual(self.attempt()["state"], "refund_pending")
        self.assertEqual(len(self.points.deductions), 1)
        self.assertEqual(len(self.points.refunds), 1)

        self.clock["now"] += matrix_template_submission.LEASE_SECONDS + 1
        with mock.patch.object(matrix_template_submission.time, "time", side_effect=self.now):
            recovered = self.recover(body_marker=False)
        self.assertEqual(recovered["state"], "refunded")
        self.assertEqual(len(self.points.deductions), 1)
        self.assertEqual(len(self.points.refunds), 1)

    def test_digital_human_image_and_video_recover_hard_exit_after_deduction(self):
        for kind in ("image", "video"):
            with self.subTest(kind=kind):
                endpoint, key, body = self.begin_paid_child(kind, "deduct")
                charges_before = len(self.points.deductions)
                self.points.crash_after_deduct = True
                with mock.patch.object(matrix_template_submission.time, "time", side_effect=self.now), \
                     self.assertRaises(SystemExit):
                    self.recover_paid_child(kind, endpoint, key, body)
                self.clock["now"] += matrix_template_submission.LEASE_SECONDS + 1
                recovered = self.recover_paid_child(kind, endpoint, key)
                self.assertEqual("linked", recovered["state"])
                self.assertEqual(kind, recovered["kind"])
                self.assertEqual(charges_before + 1, len(self.points.deductions))
                self.assertEqual(1, self.job_count())
                with closing(self.db()) as connection:
                    connection.execute("DELETE FROM jobs")
                    connection.commit()

    def test_digital_human_image_and_video_replay_hard_exit_after_job_commit(self):
        real_create = matrix_template_submission.jobs_store.create_job_after_charge
        for kind in ("image", "video"):
            with self.subTest(kind=kind):
                endpoint, key, body = self.begin_paid_child(kind, "commit")
                charges_before = len(self.points.deductions)

                def create_then_exit(*args, **kwargs):
                    real_create(*args, **kwargs)
                    raise SystemExit("hard exit after job commit")

                with mock.patch.object(
                    matrix_template_submission.jobs_store, "create_job_after_charge",
                    side_effect=create_then_exit,
                ), self.assertRaises(SystemExit):
                    self.recover_paid_child(kind, endpoint, key, body)
                linked = self.recover_paid_child(kind, endpoint, key)
                self.assertEqual("linked", linked["state"])
                self.assertEqual(kind, linked["kind"])
                self.assertEqual(charges_before + 1, len(self.points.deductions))
                with closing(self.db()) as connection:
                    self.assertEqual(1, connection.execute(
                        "SELECT COUNT(*) FROM jobs WHERE kind=?", (kind,),
                    ).fetchone()[0])

    def test_digital_human_image_and_video_recover_hard_exit_after_refund(self):
        for kind in ("image", "video"):
            with self.subTest(kind=kind):
                endpoint, key, body = self.begin_paid_child(kind, "refund")
                charges_before = len(self.points.deductions)
                refunds_before = len(self.points.refunds)
                self.points.crash_after_refund = True
                with mock.patch.object(
                    matrix_template_submission.jobs_store, "create_job_after_charge",
                    side_effect=RuntimeError("disk full"),
                ), self.assertRaises(SystemExit):
                    self.recover_paid_child(kind, endpoint, key, body)
                self.clock["now"] += matrix_template_submission.LEASE_SECONDS + 1
                recovered = self.recover_paid_child(kind, endpoint, key)
                self.assertEqual("refunded", recovered["state"])
                self.assertEqual(kind, recovered["kind"])
                self.assertEqual(charges_before + 1, len(self.points.deductions))
                self.assertEqual(refunds_before + 1, len(self.points.refunds))
                with closing(self.db()) as connection:
                    self.assertEqual(0, connection.execute(
                        "SELECT COUNT(*) FROM jobs WHERE kind=?", (kind,),
                    ).fetchone()[0])


class MatrixTemplatePreviewRecordTests(unittest.TestCase):
    """Preview credential storage: owner scoped, expiry enforced, retention pruned."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.temp.name) / "previews.db"
        self.clock = {"now": 2_000_000_000}
        with closing(self.factory()):
            pass

    def tearDown(self):
        self.temp.cleanup()

    def factory(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def now(self):
        return self.clock["now"]

    def test_preview_records_are_owner_scoped_and_expire(self):
        matrix_template_submission.record_preview(
            self.factory, "alice", "preview-1", template_id="ref-05-changsha-white-red",
            template_revision="b" * 64, fingerprint="f" * 64,
            overrides={"title_scale": 0.9},
            effective_overrides={"title_scale": 0.9, "cta_scale": 1.0},
            materials=[{"sha256": "a" * 64, "media_type": "video"}],
            prepared_digest="d" * 64, expires_at=self.now() + 600, now=self.now(),
        )
        record = matrix_template_submission.get_preview(
            self.factory, "alice", "preview-1", now=self.now(),
        )
        self.assertEqual("ref-05-changsha-white-red", record["template_id"])
        self.assertEqual({"title_scale": 0.9}, record["overrides"])
        self.assertEqual([{"sha256": "a" * 64, "media_type": "video"}],
                         record["materials"])
        self.assertEqual("d" * 64, record["prepared_digest"])
        self.assertIsNone(matrix_template_submission.get_preview(
            self.factory, "bob", "preview-1", now=self.now(),
        ))
        self.assertIsNone(matrix_template_submission.get_preview(
            self.factory, "alice", "preview-1", now=self.now() + 601,
        ))

    def test_recording_the_same_preview_twice_keeps_one_row(self):
        for scale in (0.9, 1.05):
            matrix_template_submission.record_preview(
                self.factory, "alice", "preview-2",
                template_id="ref-05-changsha-white-red", template_revision="b" * 64,
                fingerprint="f" * 64, overrides={"title_scale": scale},
                effective_overrides={"title_scale": scale},
                materials=[], prepared_digest="d" * 64,
                expires_at=self.now() + 600, now=self.now(),
            )
        with closing(self.factory()) as connection:
            rows = connection.execute(
                "SELECT COUNT(*) FROM matrix_template_preview_records",
            ).fetchone()[0]
        self.assertEqual(1, rows)
        self.assertEqual(1.05, matrix_template_submission.get_preview(
            self.factory, "alice", "preview-2", now=self.now(),
        )["overrides"]["title_scale"])

    def test_prune_drops_only_rows_past_retention(self):
        matrix_template_submission.record_preview(
            self.factory, "alice", "preview-old",
            template_id="ref-05-changsha-white-red", template_revision="b" * 64,
            fingerprint="f" * 64, overrides={}, effective_overrides={},
            materials=[], prepared_digest="",
            expires_at=self.now() - matrix_template_submission.PREVIEW_RETENTION_SECONDS - 10,
            now=self.now() - matrix_template_submission.PREVIEW_RETENTION_SECONDS - 10,
        )
        matrix_template_submission.record_preview(
            self.factory, "alice", "preview-new",
            template_id="ref-05-changsha-white-red", template_revision="b" * 64,
            fingerprint="f" * 64, overrides={}, effective_overrides={},
            materials=[], prepared_digest="", expires_at=self.now() + 600,
            now=self.now(),
        )
        self.assertEqual(1, matrix_template_submission.prune_previews(
            self.factory, now=self.now(),
        ))
        with closing(self.factory()) as connection:
            remaining = connection.execute(
                "SELECT preview_id FROM matrix_template_preview_records",
            ).fetchall()
        self.assertEqual(["preview-new"], [row["preview_id"] for row in remaining])

    def test_record_preview_requires_owner_and_identifier(self):
        with self.assertRaises(ValueError):
            matrix_template_submission.record_preview(
                self.factory, "", "preview-3", template_id="t",
                template_revision="b" * 64, fingerprint="f" * 64,
            )
        with self.assertRaises(ValueError):
            matrix_template_submission.record_preview(
                self.factory, "alice", "", template_id="t",
                template_revision="b" * 64, fingerprint="f" * 64,
            )


if __name__ == "__main__":
    unittest.main()
