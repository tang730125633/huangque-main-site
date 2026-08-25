import base64
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

import dl_service
from content_domains import core, cos


class PrivateAssetsTest(unittest.TestCase):
    PNG_1X1 = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )

    def test_local_provider_reference_url_is_signed_and_expires(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(core, "OUT_DIR", Path(tmp)), \
                patch.object(
                    core, "LOCAL_FILE_PUBLIC_BASE_URL", "https://media.example"
                ), \
                patch.object(
                    core, "LOCAL_FILE_SIGNING_SECRET", "s" * 32
                ), \
                patch.object(core, "LOCAL_FILE_URL_TTL", 3600):
            reference = Path(tmp) / "short_drama_role_uploads" / "role one.png"
            reference.parent.mkdir(parents=True)
            reference.write_bytes(self.PNG_1X1)

            url = core.local_provider_reference_url(
                "short_drama_role_uploads/role one.png", now=1000
            )
            parsed = urllib.parse.urlparse(url)
            query = urllib.parse.parse_qs(parsed.query)

            self.assertEqual("https", parsed.scheme)
            self.assertEqual("media.example", parsed.netloc)
            self.assertEqual(
                "/api/gen/file/short_drama_role_uploads/role%20one.png",
                parsed.path,
            )
            self.assertTrue(core._valid_local_provider_file_signature(
                "short_drama_role_uploads/role one.png", query, now=1000
            ))
            self.assertFalse(core._valid_local_provider_file_signature(
                "short_drama_role_uploads/role one.png", query, now=4601
            ))
            query["hq_sig"] = ["0" * 64]
            self.assertFalse(core._valid_local_provider_file_signature(
                "short_drama_role_uploads/role one.png", query, now=1000
            ))

    def test_local_provider_reference_url_requires_public_https_and_image(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(core, "OUT_DIR", Path(tmp)), \
                patch.object(core, "LOCAL_FILE_SIGNING_SECRET", "s" * 32):
            source = Path(tmp) / "reference.txt"
            source.write_text("not an image", encoding="utf-8")
            with patch.object(
                    core, "LOCAL_FILE_PUBLIC_BASE_URL", "http://media.example"):
                with self.assertRaises(RuntimeError):
                    core.local_provider_reference_url("reference.txt")
            for invalid_origin in (
                "https://user:password@media.example",
                "https://media.example/prefix",
                "https://localhost",
                "https://127.0.0.1",
            ):
                with self.subTest(origin=invalid_origin), patch.object(
                        core, "LOCAL_FILE_PUBLIC_BASE_URL", invalid_origin):
                    with self.assertRaises(RuntimeError):
                        core.local_provider_reference_url("reference.txt")
            with patch.object(
                    core, "LOCAL_FILE_PUBLIC_BASE_URL", "https://media.example"):
                with self.assertRaises(ValueError):
                    core.local_provider_reference_url("reference.txt")

    def test_local_provider_reference_url_rejects_fake_or_mismatched_images(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(core, "OUT_DIR", Path(tmp)), \
                patch.object(core, "LOCAL_FILE_PUBLIC_BASE_URL", "https://media.example"), \
                patch.object(core, "LOCAL_FILE_SIGNING_SECRET", "s" * 32):
            fake = Path(tmp) / "short_drama_role_uploads" / "fake.png"
            fake.parent.mkdir(parents=True)
            fake.write_bytes(b"png")
            with self.assertRaisesRegex(ValueError, "valid JPG, PNG, or WebP"):
                core.local_provider_reference_url(fake.relative_to(tmp).as_posix())

            mismatched = fake.with_name("mismatched.jpg")
            mismatched.write_bytes(self.PNG_1X1)
            with self.assertRaisesRegex(ValueError, "valid JPG, PNG, or WebP"):
                core.local_provider_reference_url(mismatched.relative_to(tmp).as_posix())

    def test_local_provider_configuration_uses_dedicated_secret_and_ttl_precedence(self):
        self.assertEqual(
            "dedicated",
            core._local_file_signing_secret({
                "HQ_LOCAL_FILE_SIGNING_SECRET": "dedicated",
                "HQ_INTERNAL_TOKEN": "must-not-be-reused",
            }),
        )
        self.assertEqual(
            "",
            core._local_file_signing_secret({"HQ_INTERNAL_TOKEN": "must-not-be-reused"}),
        )
        self.assertEqual(
            7200,
            core._local_file_url_ttl({
                "HQ_LOCAL_FILE_URL_TTL_SECONDS": "7200",
                "HQ_LOCAL_FILE_URL_TTL": "120",
            }),
        )
        self.assertEqual(120, core._local_file_url_ttl({"HQ_LOCAL_FILE_URL_TTL": "120"}))

    def test_production_contract_documents_local_reference_signing(self):
        root = Path(__file__).resolve().parents[1]
        example = (root / "deploy" / "huangque-secrets.env.example").read_text(
            encoding="utf-8"
        )
        runbook = (root / "deploy" / "生产环境清单与还原手册.md").read_text(
            encoding="utf-8"
        )
        for name in (
            "HQ_CONTENT_PUBLIC_BASE_URL",
            "HQ_LOCAL_FILE_SIGNING_SECRET",
            "HQ_LOCAL_FILE_URL_TTL_SECONDS",
        ):
            self.assertIn(name, example)
            self.assertIn(name, runbook)
        for requirement in (
            "openssl rand",
            "chmod 600",
            "huangque-content",
            "篡改",
            "过期",
            "回滚",
        ):
            self.assertIn(requirement, runbook)

    def test_production_contract_documents_grok_provider_selection(self):
        root = Path(__file__).resolve().parents[1]
        example = (root / "deploy" / "huangque-secrets.env.example").read_text(
            encoding="utf-8"
        )
        runbook = (root / "deploy" / "生产环境清单与还原手册.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER=grok", example)
        self.assertIn("HQ_SHORT_DRAMA_GROK_MODEL=", example)
        for name in (
            "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER",
            "HQ_SHORT_DRAMA_GROK_MODEL",
        ):
            self.assertIn(name, runbook)
        for requirement in (
            "grok_xai",
            "minimax_h3",
            "XAI_API_KEY",
            "/home/ubuntu/content-api/content.env",
            "huangque-content",
            "逐镜",
            "预览",
            "冒烟",
            "回滚",
        ):
            self.assertIn(requirement, runbook)

    def test_signed_provider_file_route_is_public_only_for_valid_current_signature(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(core, "OUT_DIR", Path(tmp)), \
                patch.object(core, "LOCAL_FILE_PUBLIC_BASE_URL", "https://media.example"), \
                patch.object(core, "LOCAL_FILE_SIGNING_SECRET", "s" * 32), \
                patch.object(core, "LOCAL_FILE_URL_TTL", 3600):
            reference = Path(tmp) / "short_drama_role_uploads" / "role.png"
            reference.parent.mkdir(parents=True)
            reference.write_bytes(self.PNG_1X1)
            signed = urllib.parse.urlparse(core.local_provider_reference_url(
                "short_drama_role_uploads/role.png", now=1000
            ))
            server = core.ThreadingHTTPServer(("127.0.0.1", 0), core.H)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            base = "http://127.0.0.1:%d%s" % (server.server_port, signed.path)
            try:
                with patch.object(core.time, "time", return_value=1000):
                    with opener.open(base + "?" + signed.query, timeout=2) as response:
                        self.assertEqual(200, response.status)
                        self.assertEqual(self.PNG_1X1, response.read())

                query = urllib.parse.parse_qs(signed.query)
                query["hq_sig"] = ["0" * 64]
                with patch.object(core.time, "time", return_value=1000), \
                        self.assertRaises(urllib.error.HTTPError) as tampered:
                    opener.open(base + "?" + urllib.parse.urlencode(query, doseq=True), timeout=2)
                self.assertEqual(404, tampered.exception.code)

                with patch.object(core.time, "time", return_value=4601), \
                        self.assertRaises(urllib.error.HTTPError) as expired:
                    opener.open(base + "?" + signed.query, timeout=2)
                self.assertEqual(404, expired.exception.code)

                traversal = "http://127.0.0.1:%d/api/gen/file/%%2e%%2e/role.png?%s" % (
                    server.server_port, signed.query,
                )
                with patch.object(core.time, "time", return_value=1000), \
                        self.assertRaises(urllib.error.HTTPError) as escaped:
                    opener.open(traversal, timeout=2)
                self.assertEqual(404, escaped.exception.code)
            finally:
                server.shutdown()
                thread.join(timeout=2)
                server.server_close()

    def test_output_file_byte_ranges_support_media_streaming(self):
        self.assertIsNone(core._parse_byte_range(None, 100))
        self.assertEqual(core._parse_byte_range("bytes=0-9", 100), (0, 9))
        self.assertEqual(core._parse_byte_range("bytes=90-", 100), (90, 99))
        self.assertEqual(core._parse_byte_range("bytes=-10", 100), (90, 99))
        with self.assertRaises(ValueError):
            core._parse_byte_range("bytes=100-", 100)
        with self.assertRaises(ValueError):
            core._parse_byte_range("bytes=0-1,4-5", 100)

    def test_download_proxy_sends_douyin_referer(self):
        self.assertIn(".hdslb.com", dl_service.ALLOW)
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
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(cos, "enabled", return_value=True), \
                patch.object(cos, "_client", return_value=client):
            source = Path(tmp) / "source.mp4"
            source.write_bytes(b"video")
            url = cos.upload(str(source), "video/private.mp4", "video/mp4", private=True)

        self.assertEqual(url, "https://signed.example/video")
        self.assertEqual(client.put_object.call_args.kwargs["ACL"], "private")

    def test_cos_upload_preserves_prefixed_custom_metadata(self):
        client = Mock()
        client.get_presigned_url.return_value = "https://signed.example/video"
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(cos, "enabled", return_value=True), \
                patch.object(cos, "_client", return_value=client):
            source = Path(tmp) / "source.mp4"
            source.write_bytes(b"video")
            cos.upload(
                source,
                "video/private.mp4",
                "video/mp4",
                private=True,
                metadata={"X-COS-META-SHA256": "digest"},
            )

        self.assertEqual(
            {"x-cos-meta-sha256": "digest"},
            client.put_object.call_args.kwargs["Metadata"],
        )

    def test_cos_upload_rejects_unprefixed_custom_metadata(self):
        client = Mock()
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(cos, "enabled", return_value=True), \
                patch.object(cos, "_client", return_value=client):
            source = Path(tmp) / "source.mp4"
            source.write_bytes(b"video")
            with self.assertRaisesRegex(ValueError, "x-cos-meta-"):
                cos.upload(
                    source,
                    "video/private.mp4",
                    metadata={"sha256": "digest"},
                )

        client.put_object.assert_not_called()

    def test_sensitive_local_file_requires_matching_asset_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "assets.db")
            with closing(sqlite3.connect(db)) as conn:
                conn.execute("CREATE TABLE video_assets(username TEXT,status TEXT,image_file TEXT,audio_file TEXT,reference_video_file TEXT,video_file TEXT)")
                conn.execute("CREATE TABLE avatars(username TEXT,status TEXT,image_file TEXT)")
                conn.execute("CREATE TABLE audio_voices(username TEXT,scope TEXT,preview_file TEXT)")
                conn.execute("INSERT INTO video_assets VALUES(?,?,?,?,?,?)",
                             ("alice", "done", None, None, "video/tryon_person_a.mp4", "video/tryon_a.mp4"))
                conn.commit()
            with patch.object(core, "AUDIO_DB", db):
                self.assertTrue(core._user_owns_output_file("alice", "video/tryon_a.mp4"))
                self.assertFalse(core._user_owns_output_file("bob", "video/tryon_a.mp4"))
                self.assertTrue(core._sensitive_output_file("video/tryon_a.mp4"))

    def test_playback_bundle_requires_project_or_board_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio_db = str(Path(tmp) / "assets.db")
            job_db = str(Path(tmp) / "jobs.db")
            with closing(sqlite3.connect(audio_db)) as conn:
                conn.execute("CREATE TABLE video_assets(username TEXT,status TEXT,image_file TEXT,audio_file TEXT,reference_video_file TEXT,video_file TEXT)")
                conn.execute("CREATE TABLE avatars(username TEXT,status TEXT,image_file TEXT)")
                conn.execute("CREATE TABLE audio_voices(username TEXT,scope TEXT,preview_file TEXT)")
                conn.commit()
            with closing(sqlite3.connect(job_db)) as conn:
                conn.execute(
                    "CREATE TABLE short_drama_projects("
                    "id TEXT PRIMARY KEY,username TEXT,board_id TEXT,deleted INTEGER)"
                )
                conn.execute(
                    "CREATE TABLE short_drama_composition_versions("
                    "project_id TEXT,file TEXT,cover_file TEXT)"
                )
                conn.execute(
                    "CREATE TABLE short_drama_playback_versions("
                    "project_id TEXT,media_file TEXT,subtitle_file TEXT)"
                )
                conn.execute(
                    "CREATE TABLE short_drama_lipsync_versions("
                    "project_id TEXT,file TEXT)"
                )
                conn.execute(
                    "CREATE TABLE short_drama_provider_shot_versions("
                    "project_id TEXT,file TEXT)"
                )
                conn.execute(
                    "INSERT INTO short_drama_projects VALUES(?,?,?,0)",
                    ("personal", "alice", None),
                )
                conn.execute(
                    "INSERT INTO short_drama_projects VALUES(?,?,?,0)",
                    ("shared", "alice", "board-1"),
                )
                conn.execute(
                    "INSERT INTO short_drama_playback_versions VALUES(?,?,?)",
                    (
                        "personal",
                        "short_drama_playback/personal/bundle/playback.mp4",
                        "short_drama_playback/personal/bundle/subtitles.vtt",
                    ),
                )
                conn.execute(
                    "INSERT INTO short_drama_lipsync_versions VALUES(?,?)",
                    ("shared", "lipsync/shared/shot-1/job-1.mp4"),
                )
                conn.execute(
                    "INSERT INTO short_drama_provider_shot_versions VALUES(?,?)",
                    ("personal", "video/short-drama-shot-1.mp4"),
                )
                conn.execute(
                    "INSERT INTO short_drama_playback_versions VALUES(?,?,?)",
                    (
                        "shared",
                        "short_drama_playback/shared/bundle/playback.mp4",
                        "short_drama_playback/shared/bundle/subtitles.vtt",
                    ),
                )
                conn.commit()
            with patch.object(core, "AUDIO_DB", audio_db), \
                    patch.object(core, "JOB_DB", job_db):
                self.assertTrue(core._user_owns_output_file(
                    "alice",
                    "short_drama_playback/personal/bundle/playback.mp4",
                ))
                self.assertTrue(core._user_owns_output_file(
                    "alice", "video/short-drama-shot-1.mp4"
                ))
                self.assertFalse(core._user_owns_output_file(
                    "bob", "video/short-drama-shot-1.mp4"
                ))
                self.assertFalse(core._user_owns_output_file(
                    "bob",
                    "short_drama_playback/personal/bundle/subtitles.vtt",
                ))
                self.assertTrue(core._user_owns_output_file(
                    "bob",
                    "short_drama_playback/shared/bundle/subtitles.vtt",
                    {"board_id": "board-1", "role": "viewer"},
                ))
                self.assertTrue(core._sensitive_output_file(
                    "lipsync/shared/shot-1/job-1.mp4"
                ))
                self.assertFalse(core._user_owns_output_file(
                    "bob", "lipsync/shared/shot-1/job-1.mp4"
                ))
                self.assertTrue(core._user_owns_output_file(
                    "bob", "lipsync/shared/shot-1/job-1.mp4",
                    {"board_id": "board-1", "role": "viewer"},
                ))

    def test_download_proxy_token_verification_fails_closed(self):
        response = Mock()
        response.read.return_value = json.dumps({"user": {"username": "alice"}}).encode()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch.object(dl_service.urllib.request, "urlopen", return_value=response):
            self.assertTrue(dl_service.verify_token("valid"))
        with patch.object(dl_service.urllib.request, "urlopen", side_effect=OSError("down")):
            self.assertFalse(dl_service.verify_token("valid"))

    def test_download_proxy_accepts_only_matching_internal_token(self):
        original = dl_service.INTERNAL_TOKEN
        try:
            dl_service.INTERNAL_TOKEN = "internal-test-token"
            self.assertTrue(dl_service.verify_internal({
                "X-HQ-Internal-Token": "internal-test-token",
            }))
            self.assertFalse(dl_service.verify_internal({
                "X-HQ-Internal-Token": "wrong",
            }))
        finally:
            dl_service.INTERNAL_TOKEN = original


if __name__ == "__main__":
    unittest.main()
