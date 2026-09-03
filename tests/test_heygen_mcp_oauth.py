import importlib
import json
import os
import sys
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

SERVER = str(Path(__file__).resolve().parents[1] / "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

video = importlib.import_module("content_domains.video")


class HeyGenMcpOAuthTests(unittest.TestCase):
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
            self.assertEqual(os.stat(credentials).st_mode & 0o077, 0)
            self.assertEqual(os.stat(str(credentials) + ".lock").st_mode & 0o077, 0)
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0].get_header("User-agent"), "huangque-content/1.0")

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
            with self.assertRaisesRegex(RuntimeError, "MOVIO_PAYMENT_INSUFFICIENT_CREDIT"):
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
        with tempfile.NamedTemporaryFile(suffix=".jpg") as f:
            f.write(b"fake-image")
            f.flush()
            with patch.object(video, "_heygen_mcp_enabled", return_value=True), \
                 patch.object(video, "_heygen_upload_asset_oauth",
                              return_value={"data": {"id": "asset-oauth"}}) as oauth, \
                 patch.object(video, "_heygen_direct_req") as direct:
                asset_id = video._heygen_upload_asset(f.name, direct=True)
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

        with tempfile.NamedTemporaryFile(suffix=".jpg") as f:
            f.write(b"fake-image")
            f.flush()
            with patch.object(video, "_heygen_mcp_access_token", return_value="oauth-token"), \
                 patch.object(video, "_heygen_direct_opener", return_value=Opener()):
                video._heygen_upload_asset_oauth(f.name, "image/jpeg")
        self.assertEqual(requests[0].get_header("Authorization"), "Bearer oauth-token")
        self.assertIsNone(requests[0].get_header("X-api-key"))


if __name__ == "__main__":
    unittest.main()
