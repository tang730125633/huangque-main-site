import base64
import importlib
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
from io import BytesIO
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SERVER = str(ROOT / "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

video = importlib.import_module("content_domains.video")
core = importlib.import_module("content_domains.core")


class HeyGenMcpOAuthTests(unittest.TestCase):
    def test_subscription_avatar_preflight_rejects_missing_oauth_without_network(self):
        with patch.object(video, "_HEYGEN_BILLING_MODE", "subscription"), \
             patch.object(video, "_HEYGEN_MCP_CREDENTIALS", ""), \
             patch.object(video, "_heygen_mcp_call") as call:
            with self.assertRaisesRegex(video.HeyGenMCPAuthError, "未提交且未扣点"):
                video.require_avatar_submission_ready()
        call.assert_not_called()

    def test_subscription_avatar_preflight_accepts_refreshable_local_oauth(self):
        credentials = json.dumps({
            "client_id": "client",
            "refresh_token": "refresh",
            "expires_at": 0,
        })
        with patch.object(video, "_HEYGEN_BILLING_MODE", "subscription"), \
             patch.object(video, "_HEYGEN_MCP_CREDENTIALS", "/secure/heygen-mcp.json"), \
             patch.object(Path, "is_file", return_value=True), \
             patch.object(Path, "read_text", return_value=credentials), \
             patch.object(video, "_heygen_mcp_call") as call:
            self.assertTrue(video.require_avatar_submission_ready())
        call.assert_not_called()

    def test_talking_preflight_rejects_missing_voice_service_before_charge(self):
        with patch.object(video, "require_avatar_submission_ready", return_value=True), \
             patch.object(video.cosyvoice, "enabled", return_value=False):
            with self.assertRaisesRegex(ValueError, "未提交且未扣点"):
                video.require_video_submission_ready({"mode": "text", "voice": "S_xaUB8OR62"})

    def test_subscription_avatar_preflight_treats_malformed_expiry_as_auth_required(self):
        credentials = json.dumps({"access_token": "token", "expires_at": "broken"})
        with patch.object(video, "_HEYGEN_BILLING_MODE", "subscription"), \
             patch.object(video, "_HEYGEN_MCP_CREDENTIALS", "/secure/heygen-mcp.json"), \
             patch.object(Path, "is_file", return_value=True), \
             patch.object(Path, "read_text", return_value=credentials):
            with self.assertRaises(video.HeyGenMCPAuthError):
                video.require_avatar_submission_ready()

    def test_billing_mode_is_explicit_and_subscription_fails_closed_without_oauth(self):
        with patch.object(video, "_HEYGEN_BILLING_MODE", "subscription"), \
             patch.object(video, "_HEYGEN_MCP_CREDENTIALS", ""), \
             patch.object(video, "generate_heygen_video_direct") as direct:
            self.assertTrue(video._heygen_subscription_mode())
            with self.assertRaisesRegex(video.HeyGenMCPAuthError, "未配置 MCP OAuth"):
                video.generate_heygen_video(
                    "i.jpg", "a.mp3", "1080p", "9:16", "medium")
        direct.assert_not_called()

    def test_mcp_file_input_is_limited_to_tools_that_accept_inline_base64(self):
        with patch.object(Path, "is_file", return_value=True), \
             patch.object(Path, "read_bytes", return_value=b"\xff\xd8\xff\xe0jpeg"):
            result = video._heygen_mcp_file_input(Path("portrait.jpg"), video.VALID_IMAGE_MIMES)
        self.assertEqual(result, {
            "type": "base64",
            "data": "/9j/4GpwZWc=",
            "media_type": "image/jpeg",
        })

    def test_subscription_preflight_reads_both_web_plan_credit_pools(self):
        profile = {"data": {"billing_type": "subscription", "subscription": {
            "plan": "creator",
            "credits": {
                "premium_credits": {"remaining": 600},
                "add_on_credits": {"remaining": 25},
            },
        }}}
        with patch.object(video, "_heygen_mcp_call", return_value=profile) as call:
            status = video._heygen_require_subscription_credits()
        self.assertEqual(status["remaining"], 625)
        call.assert_called_once_with("get_current_user", {}, timeout=30)

    def test_subscription_preflight_fails_closed_when_plan_is_empty(self):
        profile = {"subscription": {"credits": {
            "premium_credits": {"remaining": 0},
            "add_on_credits": {"remaining": 0},
        }}}
        with patch.object(video, "_heygen_mcp_call", return_value=profile):
            with self.assertRaisesRegex(ValueError, "套餐额度不足"):
                video._heygen_require_subscription_credits()

    def test_subscription_video_never_uploads_to_api_wallet_or_falls_back(self):
        with patch.object(video, "_HEYGEN_MCP_CREDENTIALS", "/secure/heygen-mcp.json"), \
             patch.object(video, "generate_heygen_video_subscription", return_value={"video_id": "v"}) as plan, \
             patch.object(video, "generate_heygen_video_direct") as direct, \
             patch.object(video, "_resolve_out_file") as relay:
            result = video.generate_heygen_video("i.jpg", "a.mp3", "1080p", "9:16", "medium")
        self.assertEqual(result["video_id"], "v")
        plan.assert_called_once()
        direct.assert_not_called()
        relay.assert_not_called()

    def test_photo_avatar_uploads_asset_then_creates_via_subscription_mcp(self):
        canonical = Path("canonical.jpg")
        with patch.object(video, "_HEYGEN_MCP_CREDENTIALS", "/secure/heygen-mcp.json"), \
             patch.object(video, "_save_data_file", return_value="avatar_src.jpg"), \
             patch.object(video, "_resolve_out_file", return_value=canonical), \
             patch.object(video, "_ensure_heygen_image_jpg", return_value=canonical), \
             patch.object(video, "require_avatar_submission_ready", return_value=True), \
             patch.object(video, "_heygen_retry_net", side_effect=lambda fn, _what: fn()), \
             patch.object(video, "_heygen_retry_429", side_effect=lambda fn, _what: fn()), \
             patch.object(video, "_heygen_create_photo_avatar", return_value=("look", "group")) as create, \
             patch.object(video, "_heygen_mcp_upload_asset", return_value="asset-portrait") as upload, \
             patch.object(video, "_heygen_upload_asset") as direct_upload, \
             patch.object(video, "_heygen_wait_photo_avatar"), \
             patch.object(video, "record_video_avatar", return_value={"id": 7, "name": "人物"}) as record, \
             patch.object(video, "public_url", return_value="/api/gen/file/canonical.jpg"), \
             patch.object(video, "_unlink_owned_output", return_value=True):
            result = video.gen_avatar({"_username": "owner", "image_data": "unused"})
        upload.assert_called_once_with(canonical)
        direct_upload.assert_not_called()
        create.assert_called_once_with("asset-portrait", direct=True)
        self.assertEqual(record.call_args.kwargs["provider_image_asset_id"], "asset-portrait")
        self.assertEqual(result["provider_avatar_id"], "look")

    def test_avatar_schema_migrates_provider_image_asset_id(self):
        uri = "file:avatar-asset-schema?mode=memory&cache=shared"
        keeper = sqlite3.connect(uri, uri=True)

        def connect():
            connection = sqlite3.connect(uri, uri=True)
            connection.row_factory = sqlite3.Row
            return connection

        try:
            with patch.object(core, "adb", side_effect=connect), \
                 patch.object(core, "_domains", return_value=[Mock()]):
                core.init_audio_db()
                columns = {row[1] for row in keeper.execute("PRAGMA table_info(avatars)")}
        finally:
            keeper.close()
        self.assertIn("provider_image_asset_id", columns)

    def test_mcp_asset_upload_uses_same_oauth_workspace_and_completes(self):
        raw = b"jpeg-bytes"
        checksum = hashlib.sha256(raw).hexdigest()

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        opener = Mock()
        opener.open.return_value = Response()

        def mcp_call(tool, arguments, timeout=90):
            if tool == "create_asset_upload":
                self.assertEqual(arguments, {
                    "filename": "canonical.jpg",
                    "contentType": "image/jpeg",
                    "sizeBytes": len(raw),
                    "checksumSha256": checksum,
                })
                return {
                    "data": {
                        "asset_id": "oauth-asset",
                        "upload_url": "https://uploads.example.test/presigned",
                        "required_headers": {
                            "Content-Type": "image/jpeg",
                            "x-amz-checksum-sha256": "provider-signed-checksum",
                        },
                    },
                }
            self.assertEqual(tool, "complete_asset_upload")
            self.assertEqual(arguments, {
                "assetId": "oauth-asset",
                "checksumSha256": checksum,
            })
            return {"data": {"asset_id": "oauth-asset"}}

        with patch.object(Path, "is_file", return_value=True), \
             patch.object(Path, "read_bytes", return_value=raw), \
             patch.object(video, "_detect_image_mime", return_value="image/jpeg"), \
             patch.object(video, "_heygen_mcp_call", side_effect=mcp_call), \
             patch.object(video, "_heygen_direct_opener", return_value=opener):
            asset_id = video._heygen_mcp_upload_asset(Path("canonical.jpg"))

        self.assertEqual(asset_id, "oauth-asset")
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://uploads.example.test/presigned")
        self.assertEqual(request.method, "PUT")
        self.assertEqual(request.data, raw)
        self.assertEqual(
            request.get_header("X-amz-checksum-sha256"),
            "provider-signed-checksum",
        )

    def test_mcp_asset_upload_does_not_invent_unsigned_amz_headers(self):
        raw = b"jpeg-bytes"

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        opener = Mock()
        opener.open.return_value = Response()

        def mcp_call(tool, _arguments, timeout=90):
            if tool == "create_asset_upload":
                return {
                    "data": {
                        "asset_id": "oauth-asset",
                        "upload_url": (
                            "https://uploads.example.test/presigned?"
                            "X-Amz-SignedHeaders=host&X-Amz-Signature=redacted"
                        ),
                    },
                }
            return {"data": {"asset_id": "oauth-asset"}}

        with patch.object(Path, "is_file", return_value=True), \
             patch.object(Path, "read_bytes", return_value=raw), \
             patch.object(video, "_detect_image_mime", return_value="image/jpeg"), \
             patch.object(video, "_heygen_mcp_call", side_effect=mcp_call), \
             patch.object(video, "_heygen_direct_opener", return_value=opener):
            video._heygen_mcp_upload_asset(Path("canonical.jpg"))

        request = opener.open.call_args.args[0]
        self.assertIsNone(request.get_header("X-amz-checksum-sha256"))

    def test_mcp_asset_upload_reports_sanitized_s3_error_details(self):
        raw = b"jpeg-bytes"
        upload_url = (
            "https://uploads.example.test/private?"
            "X-Amz-Credential=secret&X-Amz-Signature=top-secret"
        )
        error = urllib.error.HTTPError(
            upload_url,
            403,
            "Forbidden",
            {},
            BytesIO(
                b"<Error><Code>SignatureDoesNotMatch</Code>"
                b"<Message>sensitive provider detail</Message>"
                b"<RequestId>request-safe-123</RequestId></Error>"
            ),
        )
        opener = Mock()
        opener.open.side_effect = error

        def mcp_call(tool, _arguments, timeout=90):
            if tool == "create_asset_upload":
                return {"data": {"asset_id": "oauth-asset", "upload_url": upload_url}}
            raise AssertionError("upload failure must stop before completion")

        with patch.object(Path, "is_file", return_value=True), \
             patch.object(Path, "read_bytes", return_value=raw), \
             patch.object(video, "_detect_image_mime", return_value="image/jpeg"), \
             patch.object(video, "_heygen_mcp_call", side_effect=mcp_call), \
             patch.object(video, "_heygen_direct_opener", return_value=opener), \
             patch.object(video, "_heygen_retry_net", side_effect=lambda fn, _what: fn()):
            with self.assertRaisesRegex(RuntimeError, "SignatureDoesNotMatch.*request-safe-123") as raised:
                video._heygen_mcp_upload_asset(Path("canonical.jpg"))

        detail = str(raised.exception)
        self.assertNotIn("top-secret", detail)
        self.assertNotIn("sensitive provider detail", detail)

    def test_subscription_video_reuses_oauth_image_asset_and_strict_mcp_poll(self):
        image = Path("i.jpg")
        audio = Path("a.mp3")
        with patch.object(video, "_resolve_out_file", side_effect=[image, audio]), \
             patch.object(video, "_ensure_heygen_audio_mp3", return_value=audio), \
             patch.object(video, "_owned_output_relative", return_value="audio/a.mp3"), \
             patch.object(video, "public_url", return_value="https://cos.example/private-audio"), \
             patch.object(video, "_heygen_require_subscription_credits", return_value={"remaining": 600}), \
             patch.object(video, "_heygen_retry_429", side_effect=lambda fn, _what: fn()), \
             patch.object(video, "_heygen_create_video", return_value="video-plan-1") as create, \
             patch.object(video, "_heygen_poll_video", return_value={"video_url": "https://example/video.mp4"}) as poll, \
             patch.object(video, "_download_video_file_direct", return_value="video/out.mp4"), \
             patch.object(video, "_extract_first_frame_cover", return_value=None), \
             patch.object(video, "heygen_slot", side_effect=lambda _label: nullcontext()), \
             patch.object(video, "update_video_asset_phase"), \
             patch.object(video, "_heygen_mcp_upload_asset") as mcp_upload, \
             patch.object(video, "_heygen_upload_asset") as upload:
            result = video.generate_heygen_video_subscription(
                "i.jpg", "a.mp3", "1080p", "9:16", "medium",
                image_asset_id="oauth-image-1")
        upload.assert_not_called()
        mcp_upload.assert_not_called()
        create.assert_called_once_with(
            {"type": "asset_id", "asset_id": "oauth-image-1"},
            None, "1080p", "9:16", "medium",
            audio_url="https://cos.example/private-audio",
        )
        poll.assert_called_once_with(
            "video-plan-1", deadline_s=video.VIDEO_GEN_DEADLINE, mcp=True,
            allow_api_fallback=False,
        )
        self.assertEqual(result["billing_mode"], "subscription")

    def test_existing_subscription_avatar_reuses_persisted_image_asset(self):
        avatar = {
            "id": 2, "image_file": "avatar.jpg",
            "provider_avatar_id": "look-2",
            "provider_image_asset_id": "oauth-image-2",
        }
        with patch.object(video, "_HEYGEN_BILLING_MODE", "subscription"), \
             patch.object(video, "_heygen_mcp_enabled", return_value=True), \
             patch.object(video, "get_video_avatar", return_value=avatar), \
             patch.object(video, "gen_audio", return_value={"file": "audio/a.mp3"}), \
             patch.object(video, "generate_heygen_video", return_value={"video_id": "v"}) as generate, \
             patch.object(video, "update_video_asset_phase"):
            result = video.gen_video({
                "_username": "owner", "avatar_id": 2, "mode": "text",
                "text": "hello", "voice": "S_xaUB8OR62",
            })
        self.assertEqual(result["avatar_id"], 2)
        self.assertEqual(generate.call_args.kwargs["image_asset_id"], "oauth-image-2")

    def test_legacy_subscription_avatar_without_image_asset_fails_before_audio(self):
        avatar = {
            "id": 2, "image_file": "avatar.jpg",
            "provider_avatar_id": "look-2",
            "provider_image_asset_id": None,
        }
        with patch.object(video, "_HEYGEN_BILLING_MODE", "subscription"), \
             patch.object(video, "_heygen_mcp_enabled", return_value=True), \
             patch.object(video, "get_video_avatar", return_value=avatar), \
             patch.object(video, "gen_audio") as audio, \
             patch.object(video, "generate_heygen_video") as generate:
            with self.assertRaisesRegex(ValueError, "缺少.*图片素材编号"):
                video.gen_video({
                    "_username": "owner", "avatar_id": 2, "mode": "text",
                    "text": "hello", "voice": "S_xaUB8OR62",
                })
        audio.assert_not_called()
        generate.assert_not_called()

    def test_legacy_subscription_avatar_is_rejected_before_job_charge(self):
        avatar = {
            "id": 2, "image_file": "avatar.jpg",
            "provider_avatar_id": "look-2",
            "provider_image_asset_id": None,
        }
        with patch.object(video, "_HEYGEN_BILLING_MODE", "subscription"), \
             patch.object(video, "get_video_avatar", return_value=avatar):
            with self.assertRaisesRegex(ValueError, "未提交且未扣点"):
                video.validate_video_payload({
                    "avatar_id": 2, "mode": "text", "text": "hello",
                    "voice": "S_xaUB8OR62",
                }, username="owner")

    def test_strict_mcp_poll_does_not_use_api_key_after_oauth_failure(self):
        with patch.object(video, "_heygen_mcp_call", side_effect=video.HeyGenMCPAuthError("expired")), \
             patch.object(video, "_heygen_request_json") as api_get:
            with self.assertRaises(video.HeyGenMCPAuthError):
                video._heygen_poll_video(
                    "video-plan-1", deadline_s=30, mcp=True,
                    allow_api_fallback=False,
                )
        api_get.assert_not_called()

    def test_plain_video_persists_provider_correlation_before_polling(self):
        with patch.object(video, "_resolve_out_file", side_effect=[Path("i.jpg"), Path("a.mp3")]), \
             patch.object(video, "_ensure_heygen_audio_mp3", return_value=Path("a.mp3")), \
             patch.object(video, "_upload_heygen_image_asset", return_value="image-1"), \
             patch.object(video, "_heygen_upload_asset", return_value="audio-1"), \
             patch.object(video, "_heygen_retry_net", side_effect=lambda fn, _what: fn()), \
             patch.object(video, "_heygen_retry_429", side_effect=lambda fn, _what: fn()), \
             patch.object(video, "_heygen_create_video", return_value="video-1"), \
             patch.object(video, "_heygen_poll_video", return_value={"video_url": "https://example/video.mp4"}), \
             patch.object(video, "_download_video_file_direct", return_value="video/out.mp4"), \
             patch.object(video, "_extract_first_frame_cover", return_value=None), \
             patch.object(video, "heygen_slot", side_effect=lambda _label: nullcontext()), \
             patch.object(video, "update_video_asset_phase") as phase:
            result = video.generate_heygen_video_direct(
                "i.jpg", "a.mp3", "1080p", "9:16", "medium", job_id=6695)

        self.assertEqual(result["video_id"], "video-1")
        self.assertEqual([call.args[1] for call in phase.call_args_list], [
            "uploading_audio_asset", "submitting_video", "polling_video", "downloading_video",
        ])
        self.assertEqual(phase.call_args_list[2].kwargs["provider_video_id"], "video-1")

    def test_expired_oauth_refreshes_and_stays_private(self):
        requests = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600}).encode()

        class Opener:
            def open(self, request, **_kwargs):
                requests.append(request)
                return Response()

        with tempfile.TemporaryDirectory() as directory:
            credentials = Path(directory) / "heygen-mcp.json"
            credentials.write_text(json.dumps({
                "client_id": "client", "access_token": "old-access",
                "refresh_token": "old-refresh", "expires_at": 1,
            }))
            credentials.chmod(0o600)
            with patch.object(video, "_HEYGEN_MCP_CREDENTIALS", str(credentials)), \
                 patch.object(video, "_heygen_direct_opener", return_value=Opener()), \
                 patch.object(video.time, "time", return_value=1000):
                self.assertEqual(video._heygen_mcp_access_token(), "new-access")
                self.assertEqual(video._heygen_mcp_access_token(), "new-access")
            saved = json.loads(credentials.read_text())
            self.assertEqual(saved["refresh_token"], "new-refresh")
            if os.name != "nt":
                self.assertEqual(os.stat(credentials).st_mode & 0o077, 0)
                self.assertEqual(os.stat(str(credentials) + ".lock").st_mode & 0o077, 0)
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0].get_header("User-agent"), "huangque-content/1.0")

    def test_official_cli_nested_oauth_schema_can_refresh_for_mcp(self):
        requests = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({
                    "access_token": "nested-new", "refresh_token": "nested-refresh-new",
                    "expires_in": 3600,
                }).encode()

        class Opener:
            def open(self, request, **_kwargs):
                requests.append(request)
                return Response()

        with tempfile.TemporaryDirectory() as directory:
            credentials = Path(directory) / "credentials"
            credentials.write_text(json.dumps({
                "oauth": {
                    "access_token": "nested-old", "refresh_token": "nested-refresh-old",
                    "expires_at": "2000-01-01T00:00:00Z",
                },
                "user": {"email": "person@example.test"},
            }))
            credentials.chmod(0o600)
            with patch.object(video, "_HEYGEN_MCP_CREDENTIALS", str(credentials)), \
                 patch.object(video, "_heygen_direct_opener", return_value=Opener()), \
                 patch.object(video.time, "time", return_value=2_000_000_000):
                self.assertTrue(video.require_avatar_submission_ready())
                self.assertEqual(video._heygen_mcp_access_token(), "nested-new")
            saved = json.loads(credentials.read_text())
        self.assertEqual(saved["oauth"]["access_token"], "nested-new")
        self.assertEqual(saved["oauth"]["client_id"], video._HEYGEN_OAUTH_CLIENT_ID)
        self.assertEqual(saved["user"]["email"], "person@example.test")
        form = urllib.parse.parse_qs(requests[0].data.decode())
        self.assertEqual(form["client_id"], [video._HEYGEN_OAUTH_CLIENT_ID])

    def test_one_time_refresh_token_is_not_reused(self):
        requests = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({"access_token": "last-access", "expires_in": 3600}).encode()

        class Opener:
            def open(self, request, **_kwargs):
                requests.append(request)
                return Response()

        with tempfile.TemporaryDirectory() as directory:
            credentials = Path(directory) / "heygen-mcp.json"
            credentials.write_text(json.dumps({
                "client_id": "client", "access_token": "old-access",
                "refresh_token": "one-time-refresh", "expires_at": 1,
            }))
            credentials.chmod(0o600)
            with patch.object(video, "_HEYGEN_MCP_CREDENTIALS", str(credentials)), \
                 patch.object(video, "_heygen_direct_opener", return_value=Opener()), \
                 patch.object(video.time, "time", return_value=1000):
                self.assertEqual(video._heygen_mcp_access_token(), "last-access")
            saved = json.loads(credentials.read_text())
            self.assertEqual(saved["refresh_token"], "")
            with patch.object(video, "_HEYGEN_MCP_CREDENTIALS", str(credentials)), \
                 patch.object(video.time, "time", return_value=5000):
                with self.assertRaisesRegex(video.HeyGenMCPAuthError, "不可刷新"):
                    video._heygen_mcp_access_token()
            self.assertEqual(len(requests), 1)

    def test_mcp_transport_sets_cloudflare_safe_user_agent(self):
        requests = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'event: message\ndata: {"jsonrpc":"2.0","id":"x","result":{"content":[{"type":"text","text":"{\\"ok\\":true}"}],"isError":false}}\n\n'

        class Opener:
            def open(self, request, **_kwargs):
                requests.append(request)
                return Response()

        with patch.object(video, "_heygen_mcp_access_token", return_value="token"), \
             patch.object(video, "_heygen_direct_opener", return_value=Opener()):
            self.assertEqual(video._heygen_mcp_call("get_current_user", {}), {"ok": True})
        self.assertEqual(requests[0].get_header("User-agent"), "huangque-content/1.0")

    def test_mcp_ready_text_becomes_completed_video(self):
        video_id = "21c9e83eb35bcfa223f8f72bd55aa34a"
        video_url = "https://files2.heygen.ai/video.mp4?Signature=signed"
        ready_text = (
            f"Video {video_id} is ready. Watch it in the inline player or download it "
            f"from {video_url}. Call show_video with video_id={video_id} to display it "
            "in the inline player."
        )

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                message = {
                    "jsonrpc": "2.0",
                    "id": "x",
                    "result": {
                        "content": [{"type": "text", "text": ready_text}],
                        "isError": False,
                    },
                }
                return ("data: " + json.dumps(message) + "\n\n").encode()

        class Opener:
            def open(self, _request, **_kwargs):
                return Response()

        with patch.object(video, "_heygen_mcp_access_token", return_value="token"), \
             patch.object(video, "_heygen_direct_opener", return_value=Opener()):
            result = video._heygen_mcp_call("get_video", {"videoId": video_id})

        self.assertEqual(result, {
            "id": video_id,
            "status": "completed",
            "video_url": video_url,
        })
        self.assertIsNone(video._heygen_mcp_ready_text(
            ready_text.replace("files2.heygen.ai", "attacker.example"), video_id,
        ))
        self.assertIsNone(video._heygen_mcp_ready_text(
            ready_text.replace("files2.heygen.ai", "files2.heygen.ai:444"), video_id,
        ))
        self.assertIsNone(video._heygen_mcp_ready_text(
            ready_text.replace("https://files2", "https://attacker@files2"), video_id,
        ))
        self.assertIsNone(video._heygen_mcp_ready_text(
            ready_text.replace(video_id, "different-id", 1), video_id,
        ))
        self.assertIsNone(video._heygen_mcp_ready_text(
            ready_text.replace(f"video_id={video_id}", "video_id=different-id"), video_id,
        ))

    def test_cinematic_create_and_poll_use_exact_mcp_contract(self):
        calls = []

        def call(tool, arguments, timeout=90):
            calls.append((tool, arguments))
            if tool == "create_video_from_cinematic_avatar":
                return {"video_id": "mcp-video-1"}
            return {"id": "mcp-video-1", "status": "completed", "video_url": "https://example/video.mp4"}

        with patch.object(video, "_HEYGEN_MCP_CREDENTIALS", "/secure/heygen-mcp.json"), \
             patch.object(video, "_heygen_mcp_call", side_effect=call):
            video_id = video._heygen_create_cinematic_video(
                ["look-1"], ["asset-1"], "16:9", "720p", 13,
                prompt="模仿参考动作", enhance_prompt=False,
            )
            info = video._heygen_poll_video(video_id, deadline_s=30, mcp=True)

        self.assertEqual(video_id, "mcp-video-1")
        self.assertEqual(info["video_url"], "https://example/video.mp4")
        self.assertEqual(calls[0], ("create_video_from_cinematic_avatar", {
            "prompt": "模仿参考动作", "avatarId": ["look-1"],
            "aspectRatio": "16:9", "resolution": "720p", "autoDuration": False,
            "duration": 13, "enhancePrompt": False, "title": "follow_reference_motion",
            "references": [{"type": "asset_id", "asset_id": "asset-1"}],
        }))
        self.assertEqual(calls[1], ("get_video", {"videoId": "mcp-video-1"}))

    def test_plain_video_create_uses_plan_credits_via_exact_mcp_contract(self):
        with patch.object(video, "_HEYGEN_MCP_CREDENTIALS", "/secure/heygen-mcp.json"), \
             patch.object(video, "_heygen_mcp_call", return_value={"video_id": "plain-mcp-1"}) as call:
            video_id = video._heygen_create_video(
                "image-asset", "audio-asset", "1080p", "9:16", "medium", direct=True)
        self.assertEqual(video_id, "plain-mcp-1")
        arguments = call.call_args.args[1]
        self.assertEqual(call.call_args.args[0], "create_video_from_image")
        self.assertEqual(arguments, {
            "title": arguments["title"],
            "image": {"type": "asset_id", "asset_id": "image-asset"},
            "audioAssetId": "audio-asset", "resolution": "1080p", "aspectRatio": "9:16",
            "fit": "cover", "expressiveness": "medium", "outputFormat": "mp4",
        })

    def test_photo_avatar_create_and_status_use_exact_mcp_contract(self):
        calls = []

        def call(tool, arguments, timeout=90):
            calls.append((tool, arguments))
            if tool == "create_photo_avatar":
                return {"avatar_item": {"id": "look-1"}, "avatar_group": {"id": "group-1"}}
            return {"id": "look-1", "status": "completed"}

        with patch.object(video, "_HEYGEN_MCP_CREDENTIALS", "/secure/heygen-mcp.json"), \
             patch.object(video, "_heygen_mcp_call", side_effect=call):
            look_id, group_id = video._heygen_create_photo_avatar("image-asset", direct=True)
            status, message = video._heygen_look_status(look_id, group_id, direct=True)

        self.assertEqual((look_id, group_id, status, message),
                         ("look-1", "group-1", "completed", ""))
        self.assertEqual(calls[0][0], "create_photo_avatar")
        self.assertEqual(calls[0][1]["file"], {"type": "asset_id", "asset_id": "image-asset"})
        self.assertEqual(calls[1], ("get_avatar_look", {"lookId": "look-1"}))

    def test_plain_video_oauth_failure_does_not_repeat_on_relay(self):
        with patch.object(video, "_HEYGEN_DIRECT", True), \
             patch.object(video, "HEYGEN_API_KEY", "key"), \
             patch.object(video, "generate_heygen_video_direct",
                          side_effect=video.HeyGenMCPAuthError("不可刷新")), \
             patch.object(video, "_resolve_out_file") as relay:
            with self.assertRaises(video.HeyGenMCPAuthError):
                video.generate_heygen_video("i.jpg", "a.mp3", "1080p", "9:16", "medium")
        relay.assert_not_called()

    def test_plain_video_poll_never_depends_on_mcp_oauth(self):
        failed = {"data": {"id": "plain-video", "status": "failed",
                           "failure_code": "MOVIO_PAYMENT_INSUFFICIENT_CREDIT"}}
        with patch.object(video, "_HEYGEN_MCP_CREDENTIALS", "/secure/heygen-mcp.json"), \
             patch.object(video, "_heygen_mcp_call") as mcp_call, \
             patch.object(video, "_heygen_request_json", return_value=failed):
            with self.assertRaises(RuntimeError):
                video._heygen_poll_video("plain-video", deadline_s=30)
        mcp_call.assert_not_called()

    def test_cinematic_poll_falls_back_to_free_api_get_after_oauth_failure(self):
        completed = {"data": {"id": "mcp-video", "status": "completed",
                              "video_url": "https://example/video.mp4"}}
        with patch.object(video, "_heygen_mcp_call",
                          side_effect=video.HeyGenMCPAuthError("invalid_grant")), \
             patch.object(video, "_heygen_request_json", return_value=completed) as api_get:
            info = video._heygen_poll_video("mcp-video", direct=True, deadline_s=30, mcp=True)
        self.assertEqual(info["video_url"], "https://example/video.mp4")
        api_get.assert_called_once_with("GET", "/videos/mcp-video", timeout=90, direct=True)


    def test_upload_asset_uses_oauth_when_mcp_enabled(self):
        with tempfile.NamedTemporaryFile(suffix=".jpg", dir=ROOT, delete=False) as f:
            f.write(b"fake-image")
            f.flush()
            image_path = f.name
        try:
            with patch.object(video, "_heygen_mcp_enabled", return_value=True), \
                 patch.object(video, "_heygen_upload_asset_oauth",
                              return_value={"data": {"id": "asset-oauth"}}) as oauth, \
                 patch.object(video, "_heygen_direct_req") as direct:
                asset_id = video._heygen_upload_asset(image_path, direct=True)
        finally:
            Path(image_path).unlink(missing_ok=True)
        self.assertEqual(asset_id, "asset-oauth")
        oauth.assert_called_once()
        direct.assert_not_called()

    def test_upload_asset_oauth_uses_bearer_token(self):
        requests = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({"data": {"id": "asset-1"}}).encode()

        class Opener:
            def open(self, request, **_kwargs):
                requests.append(request)
                return Response()

        with tempfile.NamedTemporaryFile(suffix=".jpg", dir=ROOT, delete=False) as f:
            f.write(b"fake-image")
            f.flush()
            image_path = f.name
        try:
            with patch.object(video, "_heygen_mcp_access_token", return_value="oauth-token"), \
                 patch.object(video, "_heygen_direct_opener", return_value=Opener()):
                video._heygen_upload_asset_oauth(image_path, "image/jpeg")
        finally:
            Path(image_path).unlink(missing_ok=True)
        self.assertEqual(requests[0].get_header("Authorization"), "Bearer oauth-token")
        self.assertIsNone(requests[0].get_header("X-api-key"))


if __name__ == "__main__":
    unittest.main()
