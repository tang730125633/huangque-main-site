import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SERVER = str(Path(__file__).resolve().parents[1] / "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

video = importlib.import_module("content_domains.video")


class HeyGenMcpOAuthTests(unittest.TestCase):
    def test_expired_oauth_refreshes_and_stays_private(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600}).encode()

        class Opener:
            def open(self, *_args, **_kwargs):
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
            saved = json.loads(credentials.read_text())
            self.assertEqual(saved["refresh_token"], "new-refresh")
            self.assertEqual(os.stat(credentials).st_mode & 0o077, 0)

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
            info = video._heygen_poll_video(video_id, deadline_s=30)

        self.assertEqual(video_id, "mcp-video-1")
        self.assertEqual(info["video_url"], "https://example/video.mp4")
        self.assertEqual(calls[0], ("create_video_from_cinematic_avatar", {
            "prompt": "模仿参考动作", "avatarId": ["look-1"],
            "aspectRatio": "16:9", "resolution": "720p", "autoDuration": False,
            "duration": 13, "enhancePrompt": False, "title": "follow_reference_motion",
            "references": [{"type": "asset_id", "asset_id": "asset-1"}],
        }))
        self.assertEqual(calls[1], ("get_video", {"videoId": "mcp-video-1"}))


if __name__ == "__main__":
    unittest.main()
