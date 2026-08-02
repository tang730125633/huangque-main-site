import base64
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
import urllib.error
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
DEPLOY_SECRETS = SERVER.parent / "deploy" / "huangque-secrets.env.example"
CONTENT_SERVICE = SERVER.parent / "deploy" / "systemd" / "huangque-content.service"


class BreakdownBatchTests(unittest.TestCase):
    def test_submission_validation_rejects_junk_before_queueing(self):
        with self.assertRaisesRegex(ValueError, "完整 http"):
            breakdown.validate_breakdown_payload({"url": "随便一段文字"})

    def test_submission_validation_normalizes_supported_share_text(self):
        with mock.patch(
            "tikhub.parse_link",
            return_value={
                "platform": "douyin",
                "id": "1234567890123456789",
                "note_type": "video",
            },
        ):
            body = breakdown.validate_breakdown_payload({
                "url": "复制 https://v.douyin.com/AbCdEf/ 打开抖音",
                "mode": "reverse_prompt",
            })
        self.assertEqual("https://v.douyin.com/AbCdEf/", body["url"])
        self.assertEqual("reverse_prompt", body["mode"])
        self.assertEqual("1234567890123456789", body["_resolved_link"]["id"])

    def test_submission_validation_requires_specific_work_id(self):
        with mock.patch("tikhub.parse_link") as parse_link:
            with self.assertRaisesRegex(ValueError, "作品 ID"):
                breakdown.validate_breakdown_payload({
                    "url": "https://www.douyin.com/",
                })
            with self.assertRaisesRegex(ValueError, "笔记 ID"):
                breakdown.validate_breakdown_payload({
                    "url": "https://www.xiaohongshu.com/explore/",
                })
            parse_link.assert_not_called()

        with mock.patch(
            "tikhub.parse_link",
            return_value={"platform": "douyin", "id": None},
        ):
            with self.assertRaisesRegex(ValueError, "无法解析"):
                breakdown.validate_breakdown_payload({
                    "url": "https://v.douyin.com/invalid/",
                })

    def test_submission_validation_records_long_link_resolution_without_api(self):
        with mock.patch("tikhub.parse_link") as parse_link:
            body = breakdown.validate_breakdown_payload({
                "urls": [
                    "https://www.douyin.com/video/1234567890123456789",
                    "https://www.xiaohongshu.com/explore/64abcdef1234567890abcdef",
                ],
            })
        parse_link.assert_not_called()
        self.assertEqual(
            ["douyin", "xhs"],
            [item["platform"] for item in body["_resolved_links"]],
        )

    def test_submission_validation_rejects_unknown_host_and_oversized_batch(self):
        with self.assertRaisesRegex(ValueError, "仅支持"):
            breakdown.validate_breakdown_payload({"url": "https://example.com/video/1"})
        with self.assertRaisesRegex(ValueError, "最多提交 5"):
            breakdown.validate_breakdown_payload({
                "urls": ["https://v.douyin.com/%d/" % index for index in range(6)]
            })

    def test_download_rotates_cdn_urls_and_uses_200mb_cap(self):
        fake = mock.Mock()
        fake.download_to_file.side_effect = [TimeoutError("slow"), 123]
        detail = {
            "play_url": "https://cdn.example/one.mp4",
            "play_urls": [
                "https://cdn.example/one.mp4",
                "https://cdn.example/two.mp4",
            ],
        }
        result = breakdown._download_breakdown_video(
            fake, {"platform": "douyin", "id": "123", "note_type": "video"},
            detail, "/tmp/video.mp4",
        )
        self.assertEqual("https://cdn.example/two.mp4", result["play_url"])
        self.assertEqual(2, fake.download_to_file.call_count)
        for call in fake.download_to_file.call_args_list:
            self.assertEqual(
                breakdown.BREAKDOWN_MAX_DOWNLOAD_BYTES,
                call.kwargs["max_bytes"],
            )
        self.assertEqual(200 * 1024 * 1024, breakdown.BREAKDOWN_MAX_DOWNLOAD_BYTES)

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

    def test_worker_reuses_precharge_resolution(self):
        url = "https://www.douyin.com/video/1234567890123456789"
        resolved = {
            "url": url,
            "platform": "douyin",
            "id": "1234567890123456789",
            "note_type": "video",
        }
        with mock.patch("tikhub.parse_link") as parse_link, \
             mock.patch.object(
                 breakdown,
                 "_do_breakdown",
                 return_value={"type": "breakdown"},
             ) as run:
            result = breakdown.gen_breakdown({
                "url": url,
                "_resolved_link": resolved,
            })
        self.assertEqual("breakdown", result["type"])
        parse_link.assert_not_called()
        self.assertEqual("1234567890123456789", run.call_args.args[1]["id"])

    def test_worker_rejects_arbitrary_server_path(self):
        handle = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        handle.close()
        try:
            with self.assertRaisesRegex(ValueError, "服务器本地路径"):
                breakdown.gen_breakdown({
                    "local_path": handle.name, "media_type": "image",
                    "_job_id": 8, "_username": "fang",
                })
            self.assertTrue(os.path.exists(handle.name), "拒绝路径时绝不能删除目标文件")
        finally:
            os.unlink(handle.name)


class BreakdownLinkSubmissionHttpTests(unittest.TestCase):
    def test_invalid_links_do_not_create_job_or_deduct_points(self):
        class FakePointsError(Exception):
            def __init__(self, status, detail):
                self.status, self.detail = status, detail

        class FakePoints:
            AuthPointsError = FakePointsError

            def __init__(self):
                self.deductions = []

            @staticmethod
            def cost_of(kind, body):
                return 20

            def deduct_points(self, username, cost, reason="", transaction_key=""):
                self.deductions.append((username, cost))
                return 980

            @staticmethod
            def refund_points(*args, **kwargs):
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
            "handlers": core.HANDLERS,
        }
        fake = FakePoints()
        server = None
        with tempfile.TemporaryDirectory() as directory:
            core.JOB_DB = str(pathlib.Path(directory) / "jobs.db")
            core.verify = lambda token: {"username": "fang", "must_change": False}
            core._domains = lambda: (None, fake, mock.Mock())
            core.feature_flags.require_enabled = lambda kind: None
            core._job_queue = queue.Queue(maxsize=4)
            core._queued_job_ids = set()
            core.HANDLERS = {"breakdown": lambda payload: payload}
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
                endpoint = (
                    "http://127.0.0.1:%d/api/gen/breakdown"
                    % server.server_address[1]
                )

                def submit(body):
                    request = urllib.request.Request(
                        endpoint,
                        data=json.dumps(body).encode(),
                        method="POST",
                        headers={
                            "Authorization": "Bearer test",
                            "Content-Type": "application/json",
                        },
                    )
                    return urllib.request.urlopen(request, timeout=5)

                with mock.patch(
                    "tikhub.parse_link",
                    return_value={"platform": "douyin", "id": None},
                ) as parse_link:
                    for invalid in (
                        "https://www.douyin.com/",
                        "https://www.xiaohongshu.com/explore/",
                        "https://v.douyin.com/invalid/",
                    ):
                        with self.assertRaises(urllib.error.HTTPError) as rejected:
                            submit({"url": invalid})
                        self.assertEqual(400, rejected.exception.code)

                    self.assertEqual(1, parse_link.call_count)
                    self.assertEqual([], fake.deductions)
                    with closing(core.jdb()) as database:
                        self.assertEqual(
                            0,
                            database.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
                        )

                    with submit({
                        "urls": [
                            "https://www.douyin.com/video/1234567890123456789",
                            "https://www.xiaohongshu.com/explore/64abcdef1234567890abcdef",
                        ],
                    }) as response:
                        accepted = json.loads(response.read())

                self.assertEqual(20, accepted["cost"])
                self.assertEqual([("fang", 20)], fake.deductions)
                self.assertEqual(1, core._job_queue.qsize())
                with closing(core.jdb()) as database:
                    row = database.execute(
                        "SELECT payload FROM jobs WHERE id=?",
                        (accepted["job_id"],),
                    ).fetchone()
                payload = json.loads(row["payload"])
                self.assertEqual(
                    ["douyin", "xhs"],
                    [item["platform"] for item in payload["_resolved_links"]],
                )
            finally:
                if server:
                    server.shutdown()
                    server.server_close()
                core.JOB_DB = originals["JOB_DB"]
                core.verify = originals["verify"]
                core._domains = originals["_domains"]
                core.feature_flags.require_enabled = originals["require_enabled"]
                core._job_queue = originals["queue"]
                core._queued_job_ids = originals["ids"]
                core.HANDLERS = originals["handlers"]


class CopyZhipuProviderTests(unittest.TestCase):
    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def read():
            return b'{"choices":[{"message":{"content":"zhipu ok"}}]}'

    def test_copy_uses_reverse_zhipu_key_and_glm_4v_plus(self):
        with mock.patch.object(text, "DIRECTOR_ZHIPU_API_KEY", "secret-test-key"), \
             mock.patch.object(text._NOPROXY, "open", return_value=self._Response()) as opened:
            self.assertEqual("zhipu ok", text._director_chat("system", "user", 0.5))

        request = opened.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual("glm-4v-plus", body["model"])
        self.assertEqual("Bearer secret-test-key", request.headers["Authorization"])
        self.assertEqual(text.ZHIPU_API_BASE + "/chat/completions", request.full_url)

    def test_copy_fails_closed_without_reverse_zhipu_key(self):
        with mock.patch.object(text, "DIRECTOR_ZHIPU_API_KEY", ""):
            with self.assertRaisesRegex(RuntimeError, "REVERSE_ZHIPU_KEY"):
                text._director_chat("system", "user", 0.5)

    def test_reference_script_uses_same_zhipu_multimodal_model(self):
        with mock.patch.object(text, "DIRECTOR_ZHIPU_API_KEY", "secret-test-key"), \
             mock.patch.object(text._NOPROXY, "open", return_value=self._Response()) as opened:
            text._director_chat_multimodal(
                "system", "user", ["data:image/png;base64,YQ=="], 0.5
            )

        body = json.loads(opened.call_args.args[0].data)
        self.assertEqual("glm-4v-plus", body["model"])
        content = body["messages"][1]["content"]
        self.assertEqual("image_url", content[1]["type"])

    def test_generic_copy_and_short_drama_keep_legacy_openai_channel(self):
        with mock.patch.object(text, "ZHIPU_API_KEY", ""), \
             mock.patch.object(text, "_post", return_value={
                 "choices": [{"message": {"content": "legacy ok"}}],
             }) as post:
            self.assertEqual("legacy ok", text._chat("system", "user", 0.5))

        body = json.loads(post.call_args.args[1])
        self.assertEqual(text.FALLBACK_COPY_MODEL, body["model"])
        self.assertEqual("/v1/chat/completions", post.call_args.args[0])

    def test_script_business_path_uses_director_channel_only(self):
        director_result = json.dumps({
            "scenes": [{"dur": "3s", "scene": "产品置于窗边", "line": "自然介绍"}],
        }, ensure_ascii=False)
        with mock.patch.object(
            text, "_director_chat", return_value=director_result
        ) as director, mock.patch.object(text, "_chat") as legacy:
            result = text.gen_copy({
                "prompt": "介绍产品",
                "format": "script",
                "style": "口播",
                "dur": "30s",
                "platform": "抖音",
            })

        self.assertEqual("script", result["mode"])
        director.assert_called_once()
        legacy.assert_not_called()


class BreakdownZhipuProviderTests(unittest.TestCase):
    def test_video_frames_are_evenly_capped_for_glm_image_limit(self):
        frames = ["frame-%d.jpg" % index for index in range(8)]
        with mock.patch.object(breakdown, "ZHIPU_API_KEY", "secret-test-key"), \
             mock.patch.object(
                 breakdown, "_bounded_ai_frame",
                 return_value=(b"bounded-jpeg", "image/jpeg"),
             ) as bounded, \
             mock.patch.object(
                 breakdown.egress, "post_json_idempotent",
                 return_value={"choices": [{"message": {"content": "vision ok"}}]},
             ) as posted:
            breakdown._chat_multimodal("system", "user", frames, 0.2)

        self.assertEqual(breakdown._AI_MAX_FRAMES, bounded.call_count)
        selected = [call.args[0] for call in bounded.call_args_list]
        self.assertEqual(["frame-0.jpg", "frame-2.jpg", "frame-5.jpg", "frame-7.jpg"], selected)
        body = json.loads(posted.call_args.args[3])
        images = [
            item for item in body["messages"][1]["content"]
            if item.get("type") == "image_url"
        ]
        self.assertEqual(breakdown._AI_MAX_FRAMES, len(images))

    def test_breakdown_uses_reverse_zhipu_key_and_glm_4v_plus(self):
        with tempfile.TemporaryDirectory() as directory:
            image = pathlib.Path(directory) / "frame.jpg"
            image.write_bytes(base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC"
                "AAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
            ))
            with mock.patch.object(breakdown, "ZHIPU_API_KEY", "secret-test-key"), \
                 mock.patch.object(
                     breakdown,
                     "_bounded_ai_frame",
                     return_value=(b"bounded-jpeg", "image/jpeg"),
                 ), \
                 mock.patch.object(
                     breakdown.egress,
                     "post_json_idempotent",
                     return_value={"choices": [{"message": {"content": "vision ok"}}]},
                 ) as posted:
                self.assertEqual(
                    "vision ok",
                    breakdown._chat_multimodal("system", "user", [str(image)], 0.2),
                )

        body = json.loads(posted.call_args.args[3])
        self.assertEqual("glm-4v-plus", body["model"])
        image_url = body["messages"][1]["content"][1]["image_url"]["url"]
        self.assertTrue(image_url.startswith("data:image/jpeg;base64,"))
        self.assertLessEqual(
            len(base64.b64decode(image_url.split(",", 1)[1])),
            breakdown._AI_FRAME_MAX_BYTES,
        )
        self.assertEqual(
            "Bearer secret-test-key", posted.call_args.args[4]["Authorization"]
        )
        self.assertEqual(
            (breakdown.ZHIPU_API_BASE, breakdown.ZHIPU_API_BASE),
            posted.call_args.args[:2],
        )

    def test_breakdown_fails_closed_without_reverse_zhipu_key(self):
        with mock.patch.object(breakdown, "ZHIPU_API_KEY", ""):
            with self.assertRaisesRegex(RuntimeError, "REVERSE_ZHIPU_KEY"):
                breakdown._chat_multimodal("system", "user", [], 0.2)


class DirectorDeploymentContractTests(unittest.TestCase):
    def test_reverse_zhipu_variables_are_documented_for_content_service(self):
        template = DEPLOY_SECRETS.read_text(encoding="utf-8")
        service = CONTENT_SERVICE.read_text(encoding="utf-8")
        self.assertIn("REVERSE_ZHIPU_KEY=replace-with-zhipu-api-key", template)
        self.assertIn("REVERSE_ZHIPU_MODEL=glm-4v-plus", template)
        self.assertIn("/home/ubuntu/content-api/content.env", template)
        self.assertIn(
            "EnvironmentFile=/home/ubuntu/content-api/content.env", service
        )


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
            "OUT_DIR": core.OUT_DIR,
            "verify": core.verify,
            "_domains": core._domains,
            "require_enabled": core.feature_flags.require_enabled,
            "queue": core._job_queue,
            "ids": core._queued_job_ids,
            "handlers": core.HANDLERS,
        }
        fake = FakePoints()
        server = None
        uploaded_path = ""
        with tempfile.TemporaryDirectory() as directory:
            core.JOB_DB = str(pathlib.Path(directory) / "jobs.db")
            core.OUT_DIR = pathlib.Path(directory) / "out"
            core.verify = lambda token: {"username": "fang", "must_change": False}
            core._domains = lambda: (None, fake, mock.Mock())
            core.feature_flags.require_enabled = lambda kind: None
            core._job_queue = queue.Queue(maxsize=4)
            core._queued_job_ids = set()
            core.HANDLERS = {"breakdown": lambda payload: payload}
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
                    upload = database.execute(
                        "SELECT token,username,suffix,job_id FROM breakdown_uploads WHERE job_id=?",
                        (accepted["job_id"],),
                    ).fetchone()
                payload = json.loads(row["payload"])
                self.assertNotIn("local_path", payload)
                self.assertEqual(payload["upload_token"], upload["token"])
                self.assertEqual(("fang", accepted["job_id"]), (upload["username"], upload["job_id"]))
                uploaded_path = str(
                    core.OUT_DIR / "_breakdown_uploads" / (upload["token"] + upload["suffix"]))
                self.assertEqual(("breakdown", "fang", 20), tuple(row[:3]))
                self.assertEqual("image", payload["media_type"])
                self.assertTrue(os.path.isfile(uploaded_path))
                self.assertEqual(1, core._job_queue.qsize())

                protected = pathlib.Path(directory) / "must-not-be-read.txt"
                protected.write_text("secret", encoding="utf-8")
                unsafe = urllib.request.Request(
                    "http://127.0.0.1:%d/api/gen/breakdown" % server.server_address[1],
                    data=json.dumps({
                        "local_path": str(protected), "media_type": "image",
                    }).encode(),
                    method="POST",
                    headers={"Authorization": "Bearer test", "Content-Type": "application/json"},
                )
                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    urllib.request.urlopen(unsafe, timeout=5)
                self.assertEqual(400, rejected.exception.code)
                self.assertTrue(protected.is_file())
                self.assertEqual([("fang", 20)], fake.deductions)

                with self.assertRaisesRegex(ValueError, "不属于当前任务"):
                    breakdown.gen_breakdown(dict(
                        payload, _username="other-user", _job_id=accepted["job_id"]))
                self.assertTrue(os.path.isfile(uploaded_path))
                with mock.patch.object(
                        breakdown, "_reverse_from_frames",
                        return_value={"type": "breakdown_reverse", "prompt": "demo"}):
                    result = breakdown.gen_breakdown(dict(
                        payload, _username="fang", _job_id=accepted["job_id"]))
                self.assertEqual("breakdown_reverse", result["type"])
                self.assertFalse(os.path.exists(uploaded_path))
                with closing(core.jdb()) as database:
                    self.assertEqual(
                        0, database.execute(
                            "SELECT COUNT(*) FROM breakdown_uploads WHERE job_id=?",
                            (accepted["job_id"],),
                        ).fetchone()[0])
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
                core.OUT_DIR = originals["OUT_DIR"]
                core.verify = originals["verify"]
                core._domains = originals["_domains"]
                core.feature_flags.require_enabled = originals["require_enabled"]
                core._job_queue = originals["queue"]
                core._queued_job_ids = originals["ids"]
                core.HANDLERS = originals["handlers"]


if __name__ == "__main__":
    unittest.main()
