from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from content_domains import matrix_jobs_api as api
from content_domains import matrix_template_submission as ledger, submission_idempotency as idem


class Points:
    def __init__(self):
        self.transactions = {}

    def cost_of(self, *args):
        return 5

    def get_points_transaction(self, key):
        return self.transactions.get(key)

    def deduct_points(self, user, cost, reason, transaction_key):
        self.transactions.setdefault(transaction_key, {"username": user, "delta": -cost, "after_points": 95})
        return 95


class JobsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "jobs.db"
        with closing(self.db()) as c:
            c.execute("""CREATE TABLE jobs(id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,username TEXT NOT NULL,cost INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',payload TEXT NOT NULL,
                result TEXT,error TEXT,refunded INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL,owner TEXT)""")
            idem.ensure_table(c)
            ledger.ensure_table(c)
            c.commit()
        self.points = Points()
        self.enqueue = Mock()
        self.body = {"account": {"username": "alice"}, "template_id": "health-team-hook",
            "texts": {"top_text": "团队八个人", "bottom_text": "评论区扣888"},
            "materials": {"mode": "picked", "ids": ["vid_" + "a"*32]},
            "bgm": False, "dedupe_key": "same-operation-01"}

    def tearDown(self):
        self.temp.cleanup()

    def db(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    def submit(self, body=None):
        return api.submit(self.db, self.points, {"username": "alice", "id": 1},
            body or self.body, owner="test", enqueue=self.enqueue)

    def test_replay_same_job_one_charge_and_no_media_work(self):
        with patch.object(api, "prepare_request", side_effect=AssertionError("must be async")):
            first = self.submit()
            second = self.submit()
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertTrue(second["dedupe"])
        self.assertEqual(len(self.points.transactions), 1)
        self.assertEqual(api.status(self.db, "alice", first["job_id"])["status"], "accepted")

    def test_changed_input_conflicts_without_second_charge(self):
        self.submit()
        with self.assertRaises(api.APIError) as error:
            self.submit(dict(self.body, bgm=True))
        self.assertEqual(error.exception.code, "IDEMPOTENCY_CONFLICT")
        self.assertEqual(len(self.points.transactions), 1)

    def test_owner_cannot_be_forged_and_jobs_are_private(self):
        with self.assertRaises(api.APIError):
            self.submit(dict(self.body, account={"username": "bob"}))
        first = self.submit()
        with self.assertRaises(api.APIError) as error:
            api.status(self.db, "bob", first["job_id"])
        self.assertEqual(error.exception.status, 404)

    def test_new_key_after_failure_creates_new_job(self):
        first = self.submit()
        with closing(self.db()) as c:
            c.execute("UPDATE jobs SET status='error',error='test failure' WHERE id=?", (first["job_id"],))
            c.commit()
        self.assertEqual(api.status(self.db, "alice", first["job_id"])["status"], "failed")
        second = self.submit(dict(self.body, dedupe_key="new-operation-02"))
        self.assertNotEqual(first["job_id"], second["job_id"])
        self.assertEqual(len(self.points.transactions), 2)

    def test_no_key_active_dedupe(self):
        body = dict(self.body)
        body.pop("dedupe_key")
        self.assertEqual(self.submit(body)["job_id"], self.submit(body)["job_id"])

    def test_no_key_after_terminal_can_generate_again(self):
        body = dict(self.body)
        body.pop("dedupe_key")
        first = self.submit(body)
        with closing(self.db()) as c:
            c.execute("UPDATE jobs SET status='error' WHERE id=?", (first["job_id"],))
            c.commit()
        second = self.submit(body)
        self.assertNotEqual(first["job_id"], second["job_id"])

    def test_real_http_entry_returns_202_and_owner_scoped_query(self):
        from content_domains import core, matrix_template_video as matrix
        from http.server import ThreadingHTTPServer
        import urllib.request
        import urllib.error
        server = ThreadingHTTPServer(("127.0.0.1", 0), core.H)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.object(api, "ENABLED", True), patch.object(core, "AUTH_INTERNAL_TOKEN", "internal-test"), \
                    patch.object(core, "verify", return_value={"username":"alice"}), \
                    patch.object(core, "jdb", self.db), patch.object(core, "enqueue_job", self.enqueue), \
                    patch.object(core, "_domains", return_value=(Mock(), self.points, Mock())), \
                    patch.object(core, "_user_active_job_count", return_value=0), \
                    patch.object(core, "is_shutting_down", return_value=False), \
                    patch.object(matrix, "require_available"), \
                    patch.object(matrix, "public_templates", return_value=[{"id":"health-team-hook"}]):
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                base = f"http://127.0.0.1:{server.server_port}"
                headers = {"Authorization":"Bearer user-test", "X-HQ-Internal-Token":"internal-test", "Content-Type":"application/json"}
                request = urllib.request.Request(base+api.ENDPOINT, data=json.dumps(self.body).encode(), headers=headers)
                with opener.open(request, timeout=5) as reply:
                    self.assertEqual(reply.status, 202)
                    accepted = json.load(reply)
                with opener.open(urllib.request.Request(base+accepted["query_url"], headers=headers), timeout=5) as reply:
                    self.assertEqual(json.load(reply)["status"], "accepted")
                with self.assertRaises(urllib.error.HTTPError) as exc:
                    opener.open(urllib.request.Request(base+accepted["query_url"], headers={"Authorization":"Bearer user-test"}), timeout=5)
                self.assertEqual(exc.exception.code, 403)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_short_image_and_excess_materials_accepted(self):
        body = dict(self.body, materials={"mode": "picked", "ids": ["img_"+"a"*32]*20})
        response = self.submit(body)
        self.assertEqual(response["status"], "accepted")

    def test_font_contract_and_voice_identity_frozen(self):
        body = dict(self.body, text_revision="f"*64,
            text_overrides={"top1": {"font_size_px": 80}},
            voiceover={"text": "这是一段口播", "voice": "personal-one", "speed": 1.2})
        response = self.submit(body)
        with closing(self.db()) as c:
            raw = c.execute("SELECT payload FROM jobs WHERE id=?", (response["job_id"],)).fetchone()
        frozen = json.loads(raw["payload"])["_matrix_unified"]
        prepared, changes = api.prepare_request(frozen, "alice")
        self.assertEqual(prepared["voiceover"]["speed"], 1.2)
        self.assertEqual(prepared["text_overrides"], body["text_overrides"])
        self.assertEqual(prepared["material_adaptation"], "auto-v1")
        self.assertEqual(changes, [])

    def test_bad_or_unimplemented_inputs_fail_explicitly(self):
        for change in ({"account": {"account_id": 2}},
                {"materials": {"mode": "refs", "items": [{"url": "http://127.0.0.1"}]}},
                {"bgm": "unknown-id"}, {"text_overrides": {"top1": {"font_size_px": float("nan")}}}):
            with self.subTest(change=change), self.assertRaises(api.APIError):
                self.submit(dict(self.body, **change))
        self.assertFalse(self.points.transactions)

    def test_internal_and_user_auth_both_required(self):
        core, handler = Mock(), Mock()
        core.cli_gateway._internal_auth.return_value = False
        self.assertTrue(api.handle(handler, api.ENDPOINT, "POST", core))
        self.assertEqual(handler._send.call_args.args[0], 403)
        core.verify.assert_not_called()

    def test_copy_truncation_is_disclosed(self):
        request = api.normalize(dict(self.body, texts={"top_text": "长文案"*50, "bottom_text": "看详情"}), {"username":"alice"})
        body, changes = api.prepare_request(request, "alice")
        self.assertLessEqual(len(body["top_text"]), 60)
        self.assertEqual(changes[0]["original"], "长文案"*50)

    def test_worker_preparation_is_durable_and_not_repeated(self):
        from content_domains import core, matrix_template_video as matrix, matrix_account_assets as assets
        accepted = self.submit()
        jid = accepted["job_id"]
        with closing(self.db()) as c:
            c.execute("UPDATE jobs SET status='running' WHERE id=?", (jid,))
            c.commit()
        rendered = {"video_file": "video/test.mp4", "duration": 8, "material_manifest": [{"sha256":"a"*64}]}
        raw = {"_job_id": jid, "_username":"alice"}
        with patch.object(core, "jdb", self.db), \
                patch.object(assets, "resolve", return_value=([{"sha256":"a"*64,"media_type":"video"}], [{"sha256":"a"*64,"asset_id":"original"}])) as resolve, \
                patch.object(matrix, "validate_payload", side_effect=lambda b,*a,**k: dict(b, duration=8)), \
                patch.object(matrix, "_generate", side_effect=lambda p: dict(rendered)), \
                patch.object(api, "deliver", side_effect=lambda r,d: r):
            self.assertEqual(api.execute(raw)["material_manifest"][0]["asset_id"], "original")
            api.execute(raw)
        self.assertEqual(resolve.call_count, 1)
        with closing(self.db()) as c:
            stored = json.loads(c.execute("SELECT payload FROM jobs WHERE id=?", (jid,)).fetchone()["payload"])
        self.assertTrue(stored["_matrix_unified_prepared"])
        self.assertEqual(stored["material_adaptation"], "auto-v1")


if __name__ == "__main__":
    unittest.main()
