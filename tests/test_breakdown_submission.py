import importlib
import json
import os
import pathlib
import queue
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.request
from contextlib import closing
from http.server import ThreadingHTTPServer
from unittest import mock


SERVER = pathlib.Path(__file__).resolve().parents[1] / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

breakdown = importlib.import_module("content_domains.breakdown")
core = importlib.import_module("content_domains.core")
text = importlib.import_module("content_domains.text")


class BreakdownBatchTests(unittest.TestCase):
    def test_batch_runs_every_url_and_reports_partial_failures(self):
        def parse_link(url):
            if url.endswith("/bad"):
                raise ValueError("bad link")
            return {"platform": "douyin", "id": url.rsplit("/", 1)[-1]}

        with mock.patch.dict(sys.modules, {"tikhub": mock.Mock(parse_link=parse_link)}), \
             mock.patch.object(breakdown, "_do_breakdown",
                               side_effect=lambda payload, info, url: {
                                   "type": "breakdown", "source_url": url,
                               }), \
             mock.patch.object(breakdown, "_heartbeat") as heartbeat:
            result = breakdown.gen_breakdown({
                "_job_id": 7,
                "urls": ["https://example.test/one", "https://example.test/bad",
                         "https://example.test/two"],
            })

        self.assertEqual("breakdown_batch", result["type"])
        self.assertEqual(3, result["total"])
        self.assertEqual(
            ["https://example.test/one", "https://example.test/two"],
            [item["source_url"] for item in result["results"]],
        )
        self.assertEqual("https://example.test/bad", result["errors"][0]["url"])
        self.assertEqual(
            [mock.call(7, "batch_1_3"), mock.call(7, "batch_2_3"),
             mock.call(7, "batch_3_3")],
            heartbeat.call_args_list,
        )

    def test_local_reverse_deletes_temporary_upload(self):
        handle = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        try:
            handle.write(b"valid-enough-for-mocked-analysis")
            handle.close()
            with mock.patch.object(
                    breakdown, "_reverse_from_frames",
                    return_value={"type": "breakdown_reverse", "prompt": "demo"}):
                result = breakdown.gen_breakdown({
                    "local_path": handle.name, "media_type": "image", "_job_id": 8,
                })
            self.assertEqual("breakdown_reverse", result["type"])
            self.assertFalse(os.path.exists(handle.name))
        finally:
            try:
                os.unlink(handle.name)
            except FileNotFoundError:
                pass


class CopyProviderFallbackTests(unittest.TestCase):
    def test_copy_uses_existing_openai_channel_without_zhipu_key(self):
        with mock.patch.object(text, "ZHIPU_API_KEY", ""), \
             mock.patch.object(text, "_post", return_value={
                 "choices": [{"message": {"content": "fallback ok"}}],
             }) as post:
            self.assertEqual("fallback ok", text._chat("system", "user", 0.5))
        self.assertEqual("/v1/chat/completions", post.call_args.args[0])


class BreakdownLocalUploadHttpTests(unittest.TestCase):
    def test_local_upload_route_creates_paid_breakdown_job(self):
        class FakePointsError(Exception):
            def __init__(self, status, detail):
                self.status, self.detail = status, detail

        class FakePoints:
            AuthPointsError = FakePointsError

            def __init__(self):
                self.deductions = []

            def cost_of(self, kind, body):
                self.assert_kind = kind
                return 20

            def deduct_points(self, username, cost, reason="", transaction_key=""):
                self.deductions.append((username, cost))
                return 980

            def refund_points(self, *args, **kwargs):
                return True

            @staticmethod
            def public_error_body(error, cost):
                return {"detail": error.detail, "need": cost}

        originals = {
            "JOB_DB": core.JOB_DB,
            "verify": core.verify,
            "_domains": core._domains,
            "require_enabled": core.feature_flags.require_enabled,
            "queue": core._job_queue,
            "ids": core._queued_job_ids,
        }
        fake = FakePoints()
        server = None
        uploaded_path = ""
        with tempfile.TemporaryDirectory() as directory:
            core.JOB_DB = str(pathlib.Path(directory) / "jobs.db")
            core.verify = lambda token: {"username": "fang", "must_change": False}
            core._domains = lambda: (None, fake, mock.Mock())
            core.feature_flags.require_enabled = lambda kind: None
            core._job_queue = queue.Queue(maxsize=4)
            core._queued_job_ids = set()
            try:
                with closing(sqlite3.connect(core.JOB_DB)) as database:
                    database.execute(
                        """CREATE TABLE jobs(
                            id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT,username TEXT,cost INTEGER,
                            status TEXT DEFAULT 'pending',payload TEXT,result TEXT,error TEXT,
                            created_at INTEGER,updated_at INTEGER,deleted INTEGER DEFAULT 0,
                            refunded INTEGER DEFAULT 0,owner TEXT
                        )"""
                    )
                    database.commit()
                server = ThreadingHTTPServer(("127.0.0.1", 0), core.H)
                threading.Thread(target=server.serve_forever, daemon=True).start()
                url = (
                    "http://127.0.0.1:%d/api/gen/breakdown/local-upload?media_type=image"
                    % server.server_address[1]
                )
                request = urllib.request.Request(
                    url, data=b"\x89PNG\r\n\x1a\nmock-image", method="POST",
                    headers={"Authorization": "Bearer test", "Content-Type": "image/png"},
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    accepted = json.loads(response.read())
                self.assertEqual(20, accepted["cost"])
                self.assertEqual(980, accepted["points_left"])
                self.assertEqual([("fang", 20)], fake.deductions)
                with closing(core.jdb()) as database:
                    row = database.execute(
                        "SELECT kind,username,cost,payload FROM jobs WHERE id=?",
                        (accepted["job_id"],),
                    ).fetchone()
                payload = json.loads(row["payload"])
                uploaded_path = payload["local_path"]
                self.assertEqual(("breakdown", "fang", 20), tuple(row[:3]))
                self.assertEqual("image", payload["media_type"])
                self.assertTrue(os.path.isfile(uploaded_path))
                self.assertEqual(1, core._job_queue.qsize())
            finally:
                if server:
                    server.shutdown()
                    server.server_close()
                if uploaded_path:
                    try:
                        os.unlink(uploaded_path)
                    except FileNotFoundError:
                        pass
                core.JOB_DB = originals["JOB_DB"]
                core.verify = originals["verify"]
                core._domains = originals["_domains"]
                core.feature_flags.require_enabled = originals["require_enabled"]
                core._job_queue = originals["queue"]
                core._queued_job_ids = originals["ids"]


if __name__ == "__main__":
    unittest.main()
