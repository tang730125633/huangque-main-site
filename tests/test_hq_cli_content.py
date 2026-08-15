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

from content_domains import audio, cli_gateway, cli_uploads, core, upstream_guard, video, video_openai


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

    def test_collect_search_and_leads_quotes_validate_without_external_calls(self):
        checked = []
        core.feature_flags.require_enabled = checked.append
        cases = (
            ("collect", {"url": "https://v.douyin.com/abc123/", "want": ["video"]}, 24, "collect"),
            ("collect", {"url": "https://weixin.qq.com/sph/Abc123", "want": ["video"]}, 24, "collect"),
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
        self.assertIn("仅支持抖音、小红书或视频号", result["detail"])
        for invalid_channels in (
                "http://weixin.qq.com/sph/Abc123",
                "https://weixin.qq.com/sph/",
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
        self.assertIn("仅支持抖音、小红书或视频号", result["detail"])
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
