import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

import dl_service
from content_domains import core, cos


class PrivateAssetsTest(unittest.TestCase):
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
