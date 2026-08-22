import base64
import hashlib
import io
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from content_domains import (
    audio, cli_gateway, cli_uploads, core, submission_idempotency, upstream_guard, video,
    video_minimax_h3, video_openai,
)


class _Points:
    class AuthPointsError(Exception):
        pass

    def __init__(self):
        self.deductions = []

    def cost_of(self, kind, payload):
        return 24

    def get_points(self, username):
        return 100

    def deduct_points(self, *_args, **_kwargs):
        return 76

    def refund_points(self, *_args, **_kwargs):
        return None


class _DispatchNothing:
    class RevisionConflict(Exception):
        pass

    def dispatch_http(self, *args, **kwargs):
        return False

    def _http_error(self, handler, error, **kwargs):
        handler._send(400, {"detail": str(error)})


class HQCLIContentTests(unittest.TestCase):
    def setUp(self):
        self.points = _Points()
        self.originals = {
            "internal": core.AUTH_INTERNAL_TOKEN,
            "verify": core.verify,
            "domains": core._domains,
            "short_drama": core._short_drama_domain,
            "digital_ip": core._digital_ip_domain,
            "require_enabled": core.feature_flags.require_enabled,
            "security": core.miniprogram_security.check_payload,
            "shutting_down": core.is_shutting_down,
            "upstream": upstream_guard.exhausted_reason,
            "handlers": core.HANDLERS,
        }
        core.AUTH_INTERNAL_TOKEN = "test-cli-secret"
        core.verify = lambda token: {"username": "alice", "must_change": False}
        core._domains = lambda: (audio, self.points, object())
        core._short_drama_domain = lambda: _DispatchNothing()
        core._digital_ip_domain = lambda: _DispatchNothing()
        core.feature_flags.require_enabled = lambda kind: None
        core.miniprogram_security.check_payload = lambda payload: None
        core.is_shutting_down = lambda: False
        upstream_guard.exhausted_reason = lambda kind, payload: None
        core.HANDLERS = {"image": lambda payload: payload}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), core.H)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        core.AUTH_INTERNAL_TOKEN = self.originals["internal"]
        core.verify = self.originals["verify"]
        core._domains = self.originals["domains"]
        core._short_drama_domain = self.originals["short_drama"]
        core._digital_ip_domain = self.originals["digital_ip"]
        core.feature_flags.require_enabled = self.originals["require_enabled"]
        core.miniprogram_security.check_payload = self.originals["security"]
        core.is_shutting_down = self.originals["shutting_down"]
        upstream_guard.exhausted_reason = self.originals["upstream"]
        core.HANDLERS = self.originals["handlers"]

    def _post(self, path, payload, internal=True, expected=None, idempotency_key=""):
        headers = {"Authorization": "Bearer bridge-token", "Content-Type": "application/json"}
        if internal:
            headers["X-HQ-Internal-Token"] = "test-cli-secret"
        if expected is not None:
            headers["X-HQ-Expected-Cost"] = str(expected)
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        request = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode(), headers=headers, method="POST",
        )
        try:
            with self.opener.open(request, timeout=3) as response:
                return response.getcode(), json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_server_quote_and_expected_cost_gate_precede_any_deduction(self):
        generation = {"prompt": "a yellow bird", "provider": "openai", "ratio": "1:1", "quality": "hd", "count": 1}
        status, quote = self._post("/api/gen/cli/quote", {"kind": "image", "payload": generation})
        self.assertEqual((200, 24, 100), (status, quote["cost"], quote["points"]))
        self.assertEqual(403, self._post("/api/gen/cli/quote", {"kind": "image", "payload": generation}, internal=False)[0])
        status, result = self._post("/api/gen/image", generation, expected=25)
        self.assertEqual(409, status)
        self.assertEqual("quote_cost_changed", result["code"])
        self.assertEqual([], self.points.deductions)

    def test_audio_validation_rejects_bad_knobs_before_generation(self):
        with self.assertRaisesRegex(ValueError, "pitch"):
            audio.validate_audio_payload({"text": "hello", "pitch": 99})
        clean = audio.validate_audio_payload({"text": " hello ", "speed": 1.2, "pitch": 0, "volume": 0})
        self.assertEqual(("hello", 1.2), (clean["text"], clean["speed"]))
        original = audio.resolve_audio_provider_voice
        audio.resolve_audio_provider_voice = lambda username, voice_key: voice_key
        try:
            public = audio.validate_audio_payload(
                {"text": "hello", "voice": "S_d21F8OR62", "voice_scope": "personal", "provider": "openai"},
                "qa",
            )
            personal = audio.validate_audio_payload(
                {"text": "hello", "voice": "vip_slot_1", "voice_scope": "public", "provider": "openai"},
                "qa",
            )
        finally:
            audio.resolve_audio_provider_voice = original
        self.assertEqual((public["voice_scope"], public["provider"]), ("public", "cosyvoice"))
        self.assertEqual((personal["voice_scope"], personal["provider"]), ("personal", "cosyvoice"))

    def test_video_lipsync_uses_owned_assets_and_real_duration_pricing(self):
        with mock.patch.object(video, "get_video_asset", return_value={
                "id": 21, "video_file": "video/source.mp4",
                "ratio": "9:16", "resolution": "768p", "status": "done",
        }), mock.patch.object(video, "get_audio_asset", return_value={
                "id": 34, "file": "audio/speech.mp3",
        }), mock.patch.object(video, "_resolve_out_file", side_effect=lambda value: Path("/") / value), \
                mock.patch.object(video, "_user_owns_output_file", return_value=True), \
                mock.patch.object(video, "_normalize_audio_file_ref", return_value="audio/speech.mp3"), \
                mock.patch.object(video, "_probe_video_duration", return_value=15.1), \
                mock.patch.object(video.pricing, "get_price", side_effect=lambda key: {
                    "video.lipsync.speed": 3, "video.lipsync.precision": 6,
                }[key]):
            clean = video.validate_video_payload({
                "mode": "lipsync", "video_asset_id": 21,
                "audio_asset_id": 34, "lipsync_mode": "speed",
                "dynamic_duration": False,
            }, "alice")
            self.assertEqual(("video/source.mp4", "audio/speech.mp3"), (
                clean["reference_video_file"], clean["audio_file"]))
            self.assertEqual(46, video.video_cost(clean))

    def test_video_lipsync_quote_does_not_require_a_photo_avatar(self):
        clean = {
            "mode": "lipsync", "video_asset_id": 21,
            "audio_asset_id": 34, "reference_video_file": "video/source.mp4",
            "audio_file": "audio/speech.mp3", "_lipsync_duration": 15.0,
            "lipsync_mode": "speed", "dynamic_duration": False,
        }
        with mock.patch.object(
                core, "_domains", return_value=(audio, self.points, video)), \
                mock.patch.object(
                    video, "validate_video_payload", return_value=clean) as validate:
            status, result = self._post("/api/gen/cli/quote", {
                "kind": "video", "payload": {
                    "mode": "lipsync", "video_asset_id": 21,
                    "audio_asset_id": 34, "lipsync_mode": "speed",
                    "dynamic_duration": False,
                },
            })
        self.assertEqual((200, 24), (status, result["cost"]))
        validate.assert_called_once()

    def test_heygen_lipsync_preserves_source_and_uses_idempotency(self):
        captured = {}

        def request(method, path, body=None, headers=None, **_kwargs):
            if method == "POST":
                captured.update(
                    path=path, body=json.loads(body), headers=dict(headers or {}))
                return {"data": {"lipsync_id": "ls_123"}}
            self.assertEqual(("GET", "/lipsyncs/ls_123"), (method, path))
            return {"data": {
                "status": "completed", "duration": 15,
                "video_url": "https://files.heygen.ai/result.mp4",
            }}

        with mock.patch.object(video, "_resolve_out_file", side_effect=lambda value: Path("/") / value), \
                mock.patch.object(video, "_heygen_mcp_enabled", return_value=False), \
                mock.patch.object(video, "_heygen_upload_asset", side_effect=["vid_asset", "aud_asset"]), \
                mock.patch.object(video, "_heygen_request_json", side_effect=request), \
                mock.patch.object(video, "_download_video_file_direct", return_value="video/lipsync.mp4"), \
                mock.patch.object(video, "_extract_first_frame_cover", return_value="video/lipsync.jpg"), \
                mock.patch.object(video, "update_video_asset_phase"):
            result = video.generate_heygen_lipsync(
                "video/source.mp4", "audio/speech.mp3", "speed", False,
                job_id=77,
            )
        self.assertEqual("/lipsyncs", captured["path"])
        self.assertEqual("huangque-lipsync-job-77", captured["headers"]["Idempotency-Key"])
        self.assertEqual({"type": "asset_id", "asset_id": "vid_asset"}, captured["body"]["video"])
        self.assertFalse(captured["body"]["enable_dynamic_duration"])
        self.assertTrue(captured["body"]["keep_the_same_format"])
        self.assertEqual(("ls_123", "video/lipsync.mp4"), (
            result["video_id"], result["video_file"]))

    def test_heygen_lipsync_prefers_configured_mcp_subscription(self):
        calls = []

        def mcp(tool, arguments, **_kwargs):
            calls.append((tool, arguments))
            if tool == "create_lipsync":
                return {"lipsync_id": "ls_mcp"}
            return {
                "status": "completed", "duration": 15,
                "video_url": "https://files.heygen.ai/mcp-result.mp4",
            }

        with mock.patch.object(video, "_resolve_out_file", side_effect=lambda value: Path("/") / value), \
                mock.patch.object(video, "_heygen_mcp_enabled", return_value=True), \
                mock.patch.object(video, "_mux_seedance_upscale_audio", return_value="video/detection.mp4"), \
                mock.patch.object(video, "public_url", side_effect=lambda value, *_args, **_kwargs: "https://media.test/" + value), \
                mock.patch.object(video, "_heygen_mcp_call", side_effect=mcp), \
                mock.patch.object(video, "_heygen_upload_asset", side_effect=AssertionError("MCP must use signed URLs")), \
                mock.patch.object(video, "_download_video_file_direct", return_value="video/lipsync-mcp.mp4"), \
                mock.patch.object(video, "_extract_first_frame_cover", return_value="video/lipsync-mcp.jpg"), \
                mock.patch.object(video, "update_video_asset_phase"):
            result = video.generate_heygen_lipsync(
                "video/source.mp4", "audio/speech.mp3", "speed", False,
                job_id=78,
            )
        self.assertEqual(["create_lipsync", "get_lipsync"], [item[0] for item in calls])
        create = calls[0][1]
        self.assertEqual("https://media.test/video/detection.mp4", create["video"]["url"])
        self.assertEqual("https://media.test/audio/speech.mp3", create["audio"]["url"])
        self.assertFalse(create["enableDynamicDuration"])
        self.assertEqual("video/lipsync-mcp.mp4", result["video_file"])

    def test_sora_submit_expands_cli_reference_before_validation_and_queue(self):
        raw = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl9l1sAAAAASUVORK5CYII="
        )
        captured = {}

        def create_job(*args, **_kwargs):
            captured.update(args[6])
            return 42, 76

        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(cli_uploads, "UPLOAD_ROOT", Path(folder)), \
                mock.patch.object(core, "HANDLERS", {"sora_video": lambda payload: payload}), \
                mock.patch.object(core, "_domains", return_value=(audio, self.points, video)), \
                mock.patch.object(video, "SORA_VIDEO_ENABLED", True), \
                mock.patch.object(video_openai, "available", return_value=True), \
                mock.patch.object(core, "_idempotency_begin", return_value=("new", None)), \
                mock.patch.object(core, "_idempotency_complete"), \
                mock.patch.object(core, "_user_video_submit_limit", return_value=None), \
                mock.patch.object(core, "_user_active_job_count", return_value=0), \
                mock.patch.object(core.jobs_store, "create_paid_job", side_effect=create_job), \
                mock.patch.object(video, "record_video_pending_asset"), \
                mock.patch.object(core, "enqueue_job", return_value=True):
            uploaded = cli_uploads.store_image(
                io.BytesIO(raw), len(raw), "alice", "image/png",
                hashlib.sha256(raw).hexdigest(),
            )
            status, result = self._post(
                "/api/gen/sora_video", {
                    "prompt": "让 @图片1 缓慢旋转", "channel": "sora", "model": "sora-2",
                    "seconds": 4, "ratio": "9:16", "resolution": "720p",
                    "reference_upload_ids": [uploaded["upload_id"]],
                }, expected=24, idempotency_key="sora-ref-test-001",
            )
        self.assertEqual((200, 42), (status, result["job_id"]))
        self.assertNotIn("reference_upload_ids", captured)
        self.assertEqual(1, len(captured["reference_images"]))
        self.assertTrue(captured["reference_images"][0].startswith("data:image/png;base64,"))

    def test_minimax_quote_expands_private_reference_as_typed_data_uri(self):
        from PIL import Image

        image = io.BytesIO()
        Image.new("RGB", (256, 256), (40, 80, 120)).save(image, "PNG")
        raw = image.getvalue()
        captured = {}
        original_validate = video.validate_xiaole_video_payload

        def validate(payload, username=None):
            cleaned = original_validate(payload, username)
            captured.update(cleaned)
            return cleaned

        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(cli_uploads, "UPLOAD_ROOT", Path(folder)), \
                mock.patch.object(core, "_domains", return_value=(audio, self.points, video)), \
                mock.patch.object(core.feature_flags, "is_enabled", return_value=True), \
                mock.patch.object(video_minimax_h3, "available", return_value=True), \
                mock.patch.object(video, "validate_xiaole_video_payload", side_effect=validate):
            uploaded = cli_uploads.store_image(
                io.BytesIO(raw), len(raw), "alice", "image/png",
                hashlib.sha256(raw).hexdigest(),
            )
            status, result = self._post("/api/gen/cli/quote", {
                "kind": "xiaole_video", "payload": {
                    "prompt": "让 @图片1 向镜头挥手", "channel": "minimax",
                    "duration": 4, "ratio": "9:16", "resolution": "2k",
                    "reference_upload_ids": [uploaded["upload_id"]],
                },
            })

        self.assertEqual(200, status, result)
        self.assertEqual((24, 100), (result["cost"], result["points"]))
        self.assertNotIn("reference_upload_ids", captured)
        self.assertEqual(1, len(captured["reference_images"]))
        self.assertTrue(captured["reference_images"][0].startswith("data:image/png;base64,"))

    def test_minimax_quote_accepts_verified_text_only_2k_contract(self):
        with mock.patch.object(core, "_domains", return_value=(audio, self.points, video)), \
                mock.patch.object(core.feature_flags, "is_enabled", return_value=True), \
                mock.patch.object(video_minimax_h3, "available", return_value=True):
            status, result = self._post("/api/gen/cli/quote", {
                "kind": "xiaole_video", "payload": {
                    "prompt": "史诗级太空歌剧院线预告", "channel": "minimax",
                    "duration": 5, "ratio": "16:9", "resolution": "2k",
                },
            })

        self.assertEqual(200, status, result)
        self.assertEqual((24, 100), (result["cost"], result["points"]))

    def test_minimax_legacy_768p_same_key_replays_before_new_task_validation(self):
        legacy = {
            "prompt": "legacy paid request", "channel": "minimax",
            "model": "MiniMax-H3", "duration": 5,
            "ratio": "9:16", "resolution": "768p",
        }
        with mock.patch.object(
            core, "_domains", return_value=(audio, self.points, video),
        ), mock.patch.object(
            core, "HANDLERS", {"xiaole_video": lambda payload: payload},
        ), mock.patch.object(
            core.submission_idempotency, "replay_existing",
            return_value=("replay", {"job_id": 73, "cost": 24}),
        ), mock.patch.object(
            video, "validate_xiaole_video_payload",
            side_effect=AssertionError("replay must precede new-task validation"),
        ):
            status, result = self._post(
                "/api/gen/xiaole_video", legacy,
                expected=24, idempotency_key="minimax-legacy-replay-001",
            )
        self.assertEqual((200, 73), (status, result["job_id"]))

        with mock.patch.object(
            core, "_domains", return_value=(audio, self.points, video),
        ), mock.patch.object(
            core, "HANDLERS", {"xiaole_video": lambda payload: payload},
        ), mock.patch.object(
            core.submission_idempotency, "replay_existing",
            return_value=("missing", None),
        ), mock.patch.object(
            video_minimax_h3, "available", return_value=True,
        ), mock.patch("content_domains.feature_flags.is_enabled", return_value=True):
            status, result = self._post(
                "/api/gen/xiaole_video", legacy,
                expected=24, idempotency_key="minimax-new-768-rejected-001",
            )
        self.assertEqual(400, status, result)
        self.assertIn("仅支持 2K", result["detail"])
        self.assertEqual([], self.points.deductions)

    def test_minimax_historical_upload_hash_conflict_fails_closed(self):
        request = {
            "prompt": "让 @图片1 挥手", "channel": "minimax",
            "duration": 5, "ratio": "9:16",
            "reference_upload_ids": ["img_" + "a" * 32],
        }
        expanded = dict(request)
        expanded.pop("reference_upload_ids")
        expanded["reference_images"] = ["https://example.com/reference.png"]
        with mock.patch.object(
            core, "_domains", return_value=(audio, self.points, video),
        ), mock.patch.object(
            core, "HANDLERS", {"xiaole_video": lambda payload: payload},
        ), mock.patch.object(
            core.submission_idempotency, "replay_existing",
            return_value=("replay", {"job_id": 91, "cost": 24}),
        ), mock.patch.object(
            video, "minimax_idempotency_claim_body",
            side_effect=AssertionError("historical replay must precede strict claim validation"),
        ):
            status, result = self._post(
                "/api/gen/xiaole_video", expanded, expected=24,
                idempotency_key="minimax-legacy-url-replay-001",
            )
        self.assertEqual((200, 91), (status, result["job_id"]))

        with self.assertRaisesRegex(ValueError, "参考图"):
            video.minimax_idempotency_claim_body(expanded)
        old_body = video.minimax_idempotency_replay_bodies(expanded)[0]
        self.assertEqual(expanded["reference_images"], old_body["reference_images"])

        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "jobs.db"

            def database_factory():
                connection = sqlite3.connect(database)
                connection.row_factory = sqlite3.Row
                return connection

            connection = database_factory()
            connection.execute(
                "CREATE TABLE jobs(id INTEGER PRIMARY KEY, kind TEXT, username TEXT, payload TEXT)"
            )
            submission_idempotency.ensure_table(connection)
            connection.execute(
                "INSERT INTO jobs(id,kind,username,payload) VALUES(?,?,?,?)",
                (91, "xiaole_video", "alice", json.dumps(old_body)),
            )
            connection.commit()
            connection.close()
            state, _ = submission_idempotency.begin(
                database_factory, "alice", "/api/gen/xiaole_video",
                "minimax-expired-replay-001", old_body,
            )
            self.assertEqual("new", state)
            submission_idempotency.complete(
                database_factory, "alice", "/api/gen/xiaole_video",
                "minimax-expired-replay-001", {"job_id": 91, "cost": 24},
            )
            state, _ = submission_idempotency.replay_existing(
                database_factory, "alice", "/api/gen/xiaole_video",
                "minimax-expired-replay-001",
                video.minimax_idempotency_replay_bodies(request),
            )
            self.assertEqual("conflict", state)

            with mock.patch.object(
                core, "_domains", return_value=(audio, self.points, video),
            ), mock.patch.object(
                core, "HANDLERS", {"xiaole_video": lambda payload: payload},
            ), mock.patch.object(
                core, "jdb", side_effect=database_factory,
            ), mock.patch.object(
                cli_uploads, "expand_image_payload",
                side_effect=AssertionError("hash conflicts must precede upload lookup"),
            ) as expand:
                status, result = self._post(
                    "/api/gen/xiaole_video", request,
                    expected=24, idempotency_key="minimax-expired-replay-001",
                )
                different_image = dict(
                    request, reference_upload_ids=["img_" + "b" * 32],
                )
                image_status, image_result = self._post(
                    "/api/gen/xiaole_video", different_image,
                    expected=24, idempotency_key="minimax-expired-replay-001",
                )
                mismatched = dict(request, prompt="让 @图片1 跳舞")
                mismatch_status, mismatch_result = self._post(
                    "/api/gen/xiaole_video", mismatched,
                    expected=24, idempotency_key="minimax-expired-replay-001",
                )

        self.assertEqual((409, "idempotency_conflict"), (status, result["code"]))
        self.assertEqual(
            (409, "idempotency_conflict"), (image_status, image_result["code"]),
        )
        self.assertEqual(
            (409, "idempotency_conflict"),
            (mismatch_status, mismatch_result["code"]),
        )
        expand.assert_not_called()
        self.assertEqual([], self.points.deductions)

    def test_minimax_submit_expands_private_reference_before_charge_and_queue(self):
        from PIL import Image

        image = io.BytesIO()
        Image.new("RGB", (256, 256), (40, 80, 120)).save(image, "PNG")
        raw = image.getvalue()
        captured = {}
        idempotency_bodies = []

        def create_job(*args, **_kwargs):
            captured.update(args[6])
            return 42, 76

        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(cli_uploads, "UPLOAD_ROOT", Path(folder)), \
                mock.patch.object(cli_uploads, "expand_image_payload", wraps=cli_uploads.expand_image_payload) as expand, \
                mock.patch.object(core, "HANDLERS", {"xiaole_video": lambda payload: payload}), \
                mock.patch.object(core, "_domains", return_value=(audio, self.points, video)), \
                mock.patch.object(core.feature_flags, "is_enabled", return_value=True), \
                mock.patch.object(video_minimax_h3, "available", return_value=True), \
                mock.patch.object(
                    core, "_idempotency_begin",
                    side_effect=lambda _username, _endpoint, _key, body: (
                        idempotency_bodies.append(dict(body)) or ("new", None)
                    ),
                ), \
                mock.patch.object(core, "_idempotency_complete"), \
                mock.patch.object(core, "_user_video_submit_limit", return_value=None), \
                mock.patch.object(core, "_user_active_job_count", return_value=0), \
                mock.patch.object(core.jobs_store, "create_paid_job", side_effect=create_job), \
                mock.patch.object(video, "record_video_pending_asset"), \
                mock.patch.object(core, "enqueue_job", return_value=True):
            uploaded = cli_uploads.store_image(
                io.BytesIO(raw), len(raw), "alice", "image/png",
                hashlib.sha256(raw).hexdigest(),
            )
            status, result = self._post(
                "/api/gen/xiaole_video", {
                    "prompt": "让 @图片1 向镜头挥手", "channel": "minimax",
                    "duration": 4, "ratio": "9:16", "resolution": "2k",
                    "reference_upload_ids": [uploaded["upload_id"]],
                }, expected=24, idempotency_key="minimax-ref-test-001",
            )

        self.assertEqual((200, 42), (status, result["job_id"]))
        self.assertNotIn("reference_upload_ids", captured)
        self.assertEqual(1, len(captured["reference_images"]))
        self.assertTrue(captured["reference_images"][0].startswith("data:image/png;base64,"))
        self.assertEqual(
            video_minimax_h3.ORIGIN_METASO, captured["_minimax_origin"]
        )
        self.assertNotIn("_minimax_api_base", captured)
        self.assertEqual(1, len(idempotency_bodies))
        self.assertNotIn("_minimax_origin", idempotency_bodies[0])
        self.assertNotIn("_minimax_api_base", idempotency_bodies[0])
        self.assertEqual([uploaded["upload_id"]], idempotency_bodies[0]["reference_upload_ids"])
        self.assertEqual([], idempotency_bodies[0]["reference_images"])
        expand.assert_called_once()

    def test_minimax_submit_rejects_foreign_and_expired_references_before_charge(self):
        from PIL import Image

        image = io.BytesIO()
        Image.new("RGB", (256, 256), (120, 80, 40)).save(image, "PNG")
        raw = image.getvalue()
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(cli_uploads, "UPLOAD_ROOT", Path(folder)), \
                mock.patch.object(core, "HANDLERS", {"xiaole_video": lambda payload: payload}), \
                mock.patch.object(core, "_domains", return_value=(audio, self.points, video)), \
                mock.patch.object(core.feature_flags, "is_enabled", return_value=True), \
                mock.patch.object(video_minimax_h3, "available", return_value=True):
            foreign = cli_uploads.store_image(
                io.BytesIO(raw), len(raw), "bob", "image/png",
                hashlib.sha256(raw).hexdigest(), now=100,
            )
            own = cli_uploads.store_image(
                io.BytesIO(raw), len(raw), "alice", "image/png",
                hashlib.sha256(raw).hexdigest(), now=100,
            )
            base_payload = {
                "prompt": "a person waves to camera", "channel": "minimax",
                "duration": 4, "ratio": "9:16", "resolution": "2k",
            }
            with mock.patch.object(cli_uploads.time, "time", return_value=100):
                foreign_status, _ = self._post(
                    "/api/gen/xiaole_video",
                    dict(base_payload, reference_upload_ids=[foreign["upload_id"]]),
                    expected=24, idempotency_key="foreign-ref-test-001",
                )
            with mock.patch.object(cli_uploads.time, "time", return_value=100 + cli_uploads.TTL + 1):
                expired_status, _ = self._post(
                    "/api/gen/xiaole_video",
                    dict(base_payload, reference_upload_ids=[own["upload_id"]]),
                    expected=24, idempotency_key="expired-ref-test-001",
                )

        self.assertEqual((400, 400), (foreign_status, expired_status))
        self.assertEqual([], self.points.deductions)

    def test_minimax_quote_rejects_foreign_private_reference(self):
        from PIL import Image

        image = io.BytesIO()
        Image.new("RGB", (256, 256), (120, 80, 40)).save(image, "PNG")
        raw = image.getvalue()

        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(cli_uploads, "UPLOAD_ROOT", Path(folder)), \
                mock.patch.object(core, "_domains", return_value=(audio, self.points, video)), \
                mock.patch.object(core.feature_flags, "is_enabled", return_value=True), \
                mock.patch.object(video_minimax_h3, "available", return_value=True):
            uploaded = cli_uploads.store_image(
                io.BytesIO(raw), len(raw), "bob", "image/png",
                hashlib.sha256(raw).hexdigest(),
            )
            status, result = self._post("/api/gen/cli/quote", {
                "kind": "xiaole_video", "payload": {
                    "prompt": "人物向镜头挥手", "channel": "minimax",
                    "duration": 4, "ratio": "9:16", "resolution": "2k",
                    "reference_upload_ids": [uploaded["upload_id"]],
                },
            })

        self.assertEqual(400, status, result)
        self.assertIn("不存在或已失效", result["detail"])

    def test_minimax_quote_rejects_expired_private_reference(self):
        from PIL import Image

        image = io.BytesIO()
        Image.new("RGB", (256, 256), (80, 120, 40)).save(image, "PNG")
        raw = image.getvalue()
        expired_at = 100 + cli_uploads.TTL + 1

        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(cli_uploads, "UPLOAD_ROOT", Path(folder)), \
                mock.patch.object(core, "_domains", return_value=(audio, self.points, video)), \
                mock.patch.object(cli_uploads.time, "time", return_value=expired_at), \
                mock.patch.object(core.feature_flags, "is_enabled", return_value=True), \
                mock.patch.object(video_minimax_h3, "available", return_value=True):
            uploaded = cli_uploads.store_image(
                io.BytesIO(raw), len(raw), "alice", "image/png",
                hashlib.sha256(raw).hexdigest(), now=100,
            )
            status, result = self._post("/api/gen/cli/quote", {
                "kind": "xiaole_video", "payload": {
                    "prompt": "人物向镜头挥手", "channel": "minimax",
                    "duration": 4, "ratio": "9:16", "resolution": "2k",
                    "reference_upload_ids": [uploaded["upload_id"]],
                },
            })

        self.assertEqual(400, status, result)
        self.assertIn("已过期", result["detail"])

    def test_banana_quote_checks_the_banana_flag_not_the_image_flag(self):
        checked = []
        core.feature_flags.require_enabled = checked.append
        status, result = self._post("/api/gen/cli/quote", {
            "kind": "image", "payload": {
                "prompt": "海报", "provider": "banana", "model": "nb2",
                "ratio": "1:1", "quality": "std", "count": 1,
            },
        })
        self.assertEqual((200, 24), (status, result["cost"]))
        self.assertEqual(["banana"], checked)

    def test_collect_search_and_leads_quotes_validate_without_external_calls(self):
        checked = []
        core.feature_flags.require_enabled = checked.append
        cases = (
            ("collect", {"url": "https://v.douyin.com/abc123/", "want": ["video"]}, 24, "collect"),
            ("collect", {"url": "https://weixin.qq.com/sph/Abc123", "want": ["video"]}, 24, "collect"),
            ("collect", {"url": "https://weixin.qq.com:443/sph/Abc123", "want": ["video"]}, 24, "collect"),
            ("collect", {"url": "https://b23.tv/keSUqLz", "want": ["video"]}, 24, "collect"),
            ("collect_search", {"platform": "xhs", "keyword": "轻食创业", "page": 2}, 1, "collect"),
            ("leads", {
                "keyword": "美容院拓客", "platforms": ["douyin"],
                "count": 20, "pages": 1, "channels_targets": [],
            }, 24, "leads"),
        )
        with mock.patch.object(cli_gateway.pricing, "get_price", return_value=1):
            for kind, payload, cost, _flag in cases:
                with self.subTest(kind=kind):
                    status, result = self._post(
                        "/api/gen/cli/quote", {"kind": kind, "payload": payload})
                    self.assertEqual((200, cost, 100), (status, result["cost"], result["points"]))
        self.assertEqual([item[3] for item in cases], checked)

        status, result = self._post("/api/gen/cli/quote", {
            "kind": "collect", "payload": {
                "url": "https://douyin.com.evil.example/video/1", "want": ["video"],
            },
        })
        self.assertEqual(400, status)
        self.assertIn("仅支持抖音、小红书、视频号或 B 站", result["detail"])
        for invalid_channels in (
                "http://weixin.qq.com/sph/Abc123",
                "https://weixin.qq.com/sph/",
                "https://weixin.qq.com/sph//Abc123",
                "https://weixin.qq.com/sph/../Abc123",
                "https://weixin.qq.com/sphx/Abc123",
                "https://weixin.qq.com/not-sph/Abc123",
                "https://evil.weixin.qq.com/sph/Abc123",
                "https://weixin.qq.com:80/sph/Abc123",
                "https://weixin.qq.com:444/sph/Abc123",
                "https://%75:%70@weixin.qq.com/sph/Abc123",
                "https://weixin.qq.com.evil.example/sph/Abc123"):
            status, result = self._post("/api/gen/cli/quote", {
                "kind": "collect", "payload": {"url": invalid_channels, "want": ["video"]},
            })
            self.assertEqual(400, status)
        status, result = self._post("/api/gen/cli/quote", {
            "kind": "collect", "payload": {
                "url": "https://mp.weixin.qq.com/s/Abc123", "want": ["video"],
            },
        })
        self.assertEqual(400, status)
        self.assertIn("仅支持抖音、小红书、视频号或 B 站", result["detail"])
        status, result = self._post("/api/gen/cli/quote", {
            "kind": "leads", "payload": {
                "keyword": "", "platforms": ["channels"], "count": 20,
                "pages": 1, "channels_targets": [],
            },
        })
        self.assertEqual(400, status)
        self.assertIn("channels_targets", result["detail"])
        status, result = self._post("/api/gen/cli/quote", {
            "kind": "collect_search", "payload": {
                "platform": "douyin", "keyword": "越界页码", "page": 51,
            },
        })
        self.assertEqual(400, status)
        self.assertIn("1-50", result["detail"])

    def test_digital_ip_text_quote_requires_ready_owned_avatar_and_voice(self):
        request = {
            "mode": "text", "avatar_id": 7, "text": "欢迎来到黄雀",
            "voice": "owned-voice", "resolution": "1080p", "ratio": "9:16",
        }
        cleaned = dict(request, motion="medium", bgm_data="", bgm_volume=0.18)
        with mock.patch.object(core, "_domains", return_value=(audio, self.points, video)), \
                mock.patch.object(video, "validate_video_payload", return_value=cleaned) as validate, \
                mock.patch.object(video, "get_video_avatar", return_value={
                    "id": 7, "status": "ready", "image_file": "avatars/alice.jpg",
                }) as get_avatar, \
                mock.patch.object(audio, "resolve_audio_provider_voice",
                                  return_value="provider-voice") as resolve_voice:
            status, result = self._post(
                "/api/gen/cli/quote", {"kind": "video", "payload": request})
        self.assertEqual((200, 24), (status, result["cost"]))
        validate.assert_called_once_with(request, "alice")
        get_avatar.assert_called_once_with("alice", 7)
        resolve_voice.assert_called_once_with("alice", "owned-voice")

        with mock.patch.object(core, "_domains", return_value=(audio, self.points, video)), \
                mock.patch.object(video, "validate_video_payload", return_value=cleaned), \
                mock.patch.object(video, "get_video_avatar", return_value={
                    "id": 7, "status": "pending", "image_file": "avatars/alice.jpg",
                }), \
                mock.patch.object(audio, "resolve_audio_provider_voice") as resolve_voice:
            status, result = self._post(
                "/api/gen/cli/quote", {"kind": "video", "payload": request})
        self.assertEqual(400, status)
        self.assertIn("尚未就绪", result["detail"])
        resolve_voice.assert_not_called()

        injected = dict(request, image_data="data:image/png;base64,AAAA")
        with mock.patch.object(core, "_domains", return_value=(audio, self.points, video)), \
                mock.patch.object(video, "validate_video_payload") as validate:
            status, result = self._post(
                "/api/gen/cli/quote", {"kind": "video", "payload": injected})
        self.assertEqual(400, status)
        self.assertIn("私密上传照片", result["detail"])
        validate.assert_not_called()

    def test_digital_ip_audio_quote_accepts_only_owned_asset_reference(self):
        request = {
            "mode": "audio", "avatar_id": 9, "audio_file": "audio/alice.wav",
            "resolution": "1080p", "ratio": "9:16",
        }
        cleaned = dict(
            request, motion="medium", audio_data="", bgm_data="", bgm_volume=0.18)
        with mock.patch.object(core, "_domains", return_value=(audio, self.points, video)), \
                mock.patch.object(video, "validate_video_payload", return_value=cleaned) as validate, \
                mock.patch.object(video, "get_video_avatar", return_value={
                    "id": 9, "status": "ready", "image_file": "avatars/alice.jpg",
                }), \
                mock.patch.object(audio, "resolve_audio_provider_voice") as resolve_voice:
            status, result = self._post(
                "/api/gen/cli/quote", {"kind": "video", "payload": request})
        self.assertEqual((200, 24), (status, result["cost"]))
        validate.assert_called_once_with(request, "alice")
        resolve_voice.assert_not_called()

        injected = dict(request, audio_data="data:audio/wav;base64,AAAA")
        with mock.patch.object(core, "_domains", return_value=(audio, self.points, video)), \
                mock.patch.object(video, "validate_video_payload") as validate:
            status, result = self._post(
                "/api/gen/cli/quote", {"kind": "video", "payload": injected})
        self.assertEqual(400, status)
        self.assertIn("本人资产音频", result["detail"])
        validate.assert_not_called()

        with mock.patch.object(core, "_domains", return_value=(audio, self.points, video)):
            status, result = self._post(
                "/api/gen/cli/quote", {"kind": "video", "payload": []})
        self.assertEqual(400, status)
        self.assertIn("合法 JSON", result["detail"])

    def test_cinematic_quote_requires_ready_provider_avatar_and_expands_private_references(self):
        request = {
            "cine_mode": "open", "avatar_ids": [3, 4], "prompt": "在工作室交流",
            "resolution": "720p", "ratio": "16:9", "duration": 10,
        }
        cleaned = dict(
            request, reference_video_files=[], reference_image_files=[], enhance_prompt=False)
        ready = {
            "status": "ready", "image_file": "avatars/alice.jpg",
            "provider_avatar_id": "look-id",
        }
        with mock.patch.object(core, "_domains", return_value=(audio, self.points, video)), \
                mock.patch.object(video, "validate_cinematic_payload", return_value=cleaned) as validate, \
                mock.patch.object(video, "get_video_avatar", return_value=ready) as get_avatar:
            status, result = self._post(
                "/api/gen/cli/quote", {"kind": "cinematic", "payload": request})
        self.assertEqual((200, 24), (status, result["cost"]))
        validate.assert_called_once_with(request, "alice", [])
        self.assertEqual([mock.call("alice", 3), mock.call("alice", 4)],
                         get_avatar.call_args_list)

        not_provider_ready = dict(ready, provider_avatar_id="")
        with mock.patch.object(core, "_domains", return_value=(audio, self.points, video)), \
                mock.patch.object(video, "validate_cinematic_payload", return_value=cleaned), \
                mock.patch.object(video, "get_video_avatar", return_value=not_provider_ready):
            status, result = self._post(
                "/api/gen/cli/quote", {"kind": "cinematic", "payload": request})
        self.assertEqual(400, status)
        self.assertIn("电影化身尚未就绪", result["detail"])

        with_reference = dict(request, reference_video_upload_ids=["vid_" + "a" * 32])
        expanded = dict(request, reference_videos=["data:video/mp4;base64,AAAA"])
        with mock.patch.object(core, "_domains", return_value=(audio, self.points, video)), \
                mock.patch.object(cli_uploads, "expand_role_media_payload", return_value=expanded) as expand, \
                mock.patch.object(video, "validate_cinematic_payload", return_value=cleaned) as validate, \
                mock.patch.object(video, "get_video_avatar", return_value=ready):
            status, result = self._post(
                "/api/gen/cli/quote", {"kind": "cinematic", "payload": with_reference})
        self.assertEqual((200, 24), (status, result["cost"]))
        expand.assert_called_once_with(with_reference, "alice")
        validate.assert_called_once_with(expanded, "alice", [])

    def test_cinematic_submit_replays_before_upload_expansion_and_cleans_queue_failure(self):
        raw = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl9l1sAAAAASUVORK5CYII="
        )
        claims = {}
        created = []

        def begin(_username, _endpoint, key, body):
            digest = json.dumps(body, sort_keys=True, separators=(",", ":"))
            if key not in claims:
                claims[key] = {"digest": digest, "response": None}
                return "new", None
            if claims[key]["digest"] != digest:
                return "conflict", None
            response = claims[key]["response"]
            return ("replay", response) if response else ("processing", None)

        def complete(_username, _endpoint, key, response):
            claims[key]["response"] = response

        def abort(_username, _endpoint, key):
            if key in claims and claims[key]["response"] is None:
                claims.pop(key)

        def create_job(*args, **_kwargs):
            created.append(args[6])
            return 40 + len(created), 76

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            upload_root, output_root = root / "uploads", root / "output"

            def out_path(relative):
                path = output_root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                return path

            with mock.patch.object(cli_uploads, "UPLOAD_ROOT", upload_root):
                uploaded = cli_uploads.store_image(
                    io.BytesIO(raw), len(raw), "alice", "image/png",
                    hashlib.sha256(raw).hexdigest(),
                )
            request = {
                "cine_mode": "open", "avatar_ids": [3], "prompt": "在工作室交流",
                "resolution": "720p", "ratio": "9:16", "duration": 8,
                "reference_image_upload_ids": [uploaded["upload_id"]],
            }

            with mock.patch.object(cli_uploads, "UPLOAD_ROOT", upload_root), \
                    mock.patch.object(core, "HANDLERS", {"cinematic": lambda payload: payload}), \
                    mock.patch.object(core, "_domains", return_value=(audio, self.points, video)), \
                    mock.patch.object(core, "_idempotency_begin", side_effect=begin), \
                    mock.patch.object(core, "_idempotency_complete", side_effect=complete), \
                    mock.patch.object(core, "_idempotency_abort", side_effect=abort), \
                    mock.patch.object(core, "_user_video_submit_limit", return_value=None), \
                    mock.patch.object(core, "_user_active_job_count", return_value=0), \
                    mock.patch.object(core.jobs_store, "create_paid_job", side_effect=create_job), \
                    mock.patch.object(core, "_reject_pending_job"), \
                    mock.patch.object(video, "get_video_avatar", return_value={
                        "id": 3, "status": "ready", "provider_avatar_id": "look-3",
                    }), \
                    mock.patch.object(video, "_out_path", side_effect=out_path), \
                    mock.patch.object(video, "record_video_pending_asset"), \
                    mock.patch.object(video, "update_video_asset_phase"), \
                    mock.patch.object(core, "enqueue_job", side_effect=(True, False)), \
                    mock.patch.object(cli_uploads, "expand_role_media_payload",
                                      wraps=cli_uploads.expand_role_media_payload) as expand:
                status, first = self._post(
                    "/api/gen/cinematic", request, expected=24,
                    idempotency_key="hqcli-cinematic-retry-001",
                )
                self.assertEqual((200, 41), (status, first["job_id"]))
                kept = {path for path in output_root.rglob("*") if path.is_file()}
                self.assertEqual(1, len(kept))

                # Replay must not touch the short-lived upload again.
                for path in upload_root.iterdir():
                    path.unlink()
                status, replay = self._post(
                    "/api/gen/cinematic", request, expected=24,
                    idempotency_key="hqcli-cinematic-retry-001",
                )
                self.assertEqual((200, first), (status, replay))
                self.assertEqual((1, 1), (len(created), expand.call_count))
                self.assertEqual(kept, {
                    path for path in output_root.rglob("*") if path.is_file()
                })

                # A fresh request that cannot enter the queue must remove its files.
                with mock.patch.object(cli_uploads, "_load_image", return_value=(
                    base64.b64encode(raw).decode("ascii"), {"mime": "image/png"},
                )):
                    status, failed = self._post(
                        "/api/gen/cinematic", dict(request, prompt="队列失败"), expected=24,
                        idempotency_key="hqcli-cinematic-retry-002",
                    )
                self.assertEqual((429, "queue_full"), (status, failed["code"]))
                self.assertEqual(2, len(created))
                self.assertEqual(kept, {
                    path for path in output_root.rglob("*") if path.is_file()
                })

    def test_script_to_video_replays_before_mutable_plan_validation(self):
        from content_domains import pixelle_video, script_to_video

        claims = {}
        created = []

        def begin(_username, _endpoint, key, body):
            digest = json.dumps(body, sort_keys=True, separators=(",", ":"))
            if key not in claims:
                claims[key] = {"digest": digest, "response": None}
                return "new", None
            self.assertEqual(claims[key]["digest"], digest)
            return "replay", claims[key]["response"]

        def complete(_username, _endpoint, key, response):
            claims[key]["response"] = response

        prepared = {
            "pipeline": "pixelle", "text": "AI 培训", "mode": "generate",
            "template": "1080x1920/image_default.html", "n_scenes": 1,
            "scenes": [{"line": "AI 培训"}],
        }
        quote_token, _ = pixelle_video.issue_quote(
            prepared, "alice", 24, core.AUTH_INTERNAL_TOKEN)
        prepared_with_quote = dict(prepared, _quote_token=quote_token)

        def create_job(*args, **_kwargs):
            created.append(args[6])
            return 51, 76

        with mock.patch.object(core, "HANDLERS", {"script_to_video": lambda payload: payload}), \
                mock.patch.object(core, "_domains", return_value=(audio, self.points, video)), \
                mock.patch.object(core, "_idempotency_begin", side_effect=begin), \
                mock.patch.object(core, "_idempotency_complete", side_effect=complete), \
                mock.patch.object(core, "_user_video_submit_limit", return_value=None), \
                mock.patch.object(core, "_user_active_job_count", return_value=0), \
                mock.patch.object(core.jobs_store, "create_paid_job", side_effect=create_job), \
                mock.patch.object(video, "record_video_pending_asset"), \
                mock.patch.object(core, "enqueue_job", return_value=True), \
                mock.patch.object(pixelle_video, "paid_plan_association", return_value=None), \
                mock.patch.object(
                    script_to_video, "prepare_script_to_video_payload",
                    side_effect=[prepared_with_quote, AssertionError("replay reached mutable plan validation")],
                ) as prepare:
            request = {
                "pipeline": "pixelle", "text": "AI 培训",
                "quote_token": quote_token,
            }
            status, first = self._post(
                "/api/gen/script_to_video", request, expected=24,
                idempotency_key="script-video-lost-response-001",
            )
            status_replay, replay = self._post(
                "/api/gen/script_to_video", request, expected=24,
                idempotency_key="script-video-lost-response-001",
            )

        self.assertEqual((200, 51), (status, first["job_id"]))
        self.assertEqual((200, first), (status_replay, replay))
        self.assertEqual(1, prepare.call_count)
        self.assertEqual(1, len(created))

    def test_batch_and_tryon_quotes_use_server_validation_and_total_cost(self):
        batch = {
            "mode": "text", "text": "欢迎到店", "voice": "owned-voice",
            "avatars": [{"avatar_id": 1}, {"avatar_id": 2}],
        }
        items = [
            {"mode": "text", "avatar_id": "1", "voice": "owned-voice"},
            {"mode": "text", "avatar_id": "2", "voice": "owned-voice"},
        ]
        ready = {"status": "ready", "image_file": "avatars/alice.jpg"}
        with mock.patch.object(core, "_domains", return_value=(audio, self.points, video)), \
                mock.patch.object(video, "validate_video_batch_payload", return_value=items), \
                mock.patch.object(video, "get_video_avatar", return_value=ready), \
                mock.patch.object(audio, "resolve_audio_provider_voice", return_value="provider"):
            status, result = self._post(
                "/api/gen/cli/quote", {"kind": "video_batch", "payload": batch})
        self.assertEqual((200, 48), (status, result["cost"]))

        request = {
            "line": "2", "person_image_upload_id": "img_" + "a" * 32,
            "clothes_upload_id": "img_" + "b" * 32,
        }
        expanded = {"line": "2", "person_image_data": "data:image/png;base64,AAAA",
                    "clothes_data": "data:image/png;base64,BBBB"}
        with mock.patch.object(core, "_domains", return_value=(audio, self.points, video)), \
                mock.patch.object(cli_uploads, "expand_role_media_payload", return_value=expanded), \
                mock.patch.object(video, "validate_tryon_payload", return_value=expanded):
            status, result = self._post(
                "/api/gen/cli/quote", {"kind": "tryon", "payload": request})
        self.assertEqual((200, 24), (status, result["cost"]))

    def test_tryon_submit_expands_private_roles_before_security_and_cost_gate(self):
        request = {
            "line": "2", "person_image_upload_id": "img_" + "a" * 32,
            "clothes_upload_id": "img_" + "b" * 32,
        }
        expanded = {"line": "2", "person_image_data": "data:image/png;base64,AAAA",
                    "clothes_data": "data:image/png;base64,BBBB", "seconds": 6}
        checked = []
        with mock.patch.object(core, "_domains", return_value=(audio, self.points, video)), \
                mock.patch.object(core, "HANDLERS", {"tryon": lambda payload: payload}), \
                mock.patch.object(cli_uploads, "expand_role_media_payload", return_value=expanded) as expand, \
                mock.patch.object(video, "validate_tryon_payload", return_value=expanded), \
                mock.patch.object(core.miniprogram_security, "check_payload", side_effect=checked.append):
            status, result = self._post("/api/gen/tryon", request, expected=25)
        self.assertEqual((409, "quote_cost_changed"), (status, result["code"]))
        expand.assert_called_once_with(request, "alice")
        self.assertEqual([expanded], checked)
        self.assertEqual([], self.points.deductions)


if __name__ == "__main__":
    unittest.main()
