import base64
import hashlib
import io
import json
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

from content_domains import audio, cli_uploads, core, upstream_guard, video, video_openai


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


if __name__ == "__main__":
    unittest.main()
