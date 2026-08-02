import json
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

import dl_service
from content_domains import core, cos


class PrivateAssetsTest(unittest.TestCase):
    def test_download_proxy_sends_douyin_referer(self):
        self.assertEqual(
            dl_service.download_headers("v26-webf.douyinvod.com")["Referer"],
            "https://www.douyin.com/",
        )
        self.assertNotIn("Referer", dl_service.download_headers("sns-video-hw.xhscdn.com"))

    def test_download_proxy_health_endpoint(self):
        server = dl_service.ThreadingHTTPServer(("127.0.0.1", 0), dl_service.H)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(
                "http://127.0.0.1:%d/api/gen/dl/health" % server.server_port,
                timeout=2,
            ) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(
                    json.loads(response.read()),
                    {"ok": True, "service": "huangque-dl"},
                )
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

    def test_private_cos_upload_sets_object_acl_and_returns_signed_url(self):
        client = Mock()
        client.get_presigned_url.return_value = "https://signed.example/video"
        with tempfile.NamedTemporaryFile() as source, \
                patch.object(cos, "enabled", return_value=True), \
                patch.object(cos, "_client", return_value=client):
            source.write(b"video")
            source.flush()
            url = cos.upload(source.name, "video/private.mp4", "video/mp4", private=True)

        self.assertEqual(url, "https://signed.example/video")
        self.assertEqual(client.put_object.call_args.kwargs["ACL"], "private")

    def test_sensitive_local_file_requires_matching_asset_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "assets.db")
            with sqlite3.connect(db) as conn:
                conn.execute("CREATE TABLE video_assets(username TEXT,status TEXT,image_file TEXT,audio_file TEXT,reference_video_file TEXT,video_file TEXT)")
                conn.execute("CREATE TABLE avatars(username TEXT,status TEXT,image_file TEXT)")
                conn.execute("CREATE TABLE audio_voices(username TEXT,scope TEXT,preview_file TEXT)")
                conn.execute("INSERT INTO video_assets VALUES(?,?,?,?,?,?)",
                             ("alice", "done", None, None, "video/tryon_person_a.mp4", "video/tryon_a.mp4"))
            with patch.object(core, "AUDIO_DB", db):
                self.assertTrue(core._user_owns_output_file("alice", "video/tryon_a.mp4"))
                self.assertFalse(core._user_owns_output_file("bob", "video/tryon_a.mp4"))
                self.assertTrue(core._sensitive_output_file("video/tryon_a.mp4"))

    def test_download_proxy_token_verification_fails_closed(self):
        response = Mock()
        response.read.return_value = json.dumps({"user": {"username": "alice"}}).encode()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch.object(dl_service.urllib.request, "urlopen", return_value=response):
            self.assertTrue(dl_service.verify_token("valid"))
        with patch.object(dl_service.urllib.request, "urlopen", side_effect=OSError("down")):
            self.assertFalse(dl_service.verify_token("valid"))


if __name__ == "__main__":
    unittest.main()
