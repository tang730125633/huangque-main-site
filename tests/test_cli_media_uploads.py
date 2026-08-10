import base64
import hashlib
import io
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server.content_domains import cli_uploads


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl9l1sAAAAASUVORK5CYII="
)
MP4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 32


class CLIMediaUploadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root_patch = mock.patch.object(cli_uploads, "UPLOAD_ROOT", Path(self.temp.name))
        self.root_patch.start()

    def tearDown(self):
        self.root_patch.stop()
        self.temp.cleanup()

    def image(self):
        return cli_uploads.store_image(
            io.BytesIO(PNG), len(PNG), "alice", "image/png",
            hashlib.sha256(PNG).hexdigest(), now=100,
        )["upload_id"]

    def video(self, duration=5.5):
        with mock.patch.object(cli_uploads, "_probe_video_duration", return_value=duration):
            return cli_uploads.store_video(
                io.BytesIO(MP4), len(MP4), "alice", "video/mp4",
                hashlib.sha256(MP4).hexdigest(), now=100,
            )["upload_id"]

    def test_owner_bound_video_expands_for_cinematic_motion(self):
        upload_id = self.video()
        body = cli_uploads.expand_role_media_payload(
            {"reference_video_upload_ids": [upload_id]}, "alice", now=101,
        )
        self.assertTrue(body["reference_videos"][0].startswith("data:video/mp4;base64,"))
        self.assertNotIn("reference_video_upload_ids", body)
        with self.assertRaisesRegex(ValueError, "不存在或已失效"):
            cli_uploads.expand_role_media_payload(
                {"reference_video_upload_ids": [upload_id]}, "bob", now=101,
            )

    def test_tryon_roles_expand_and_classic_video_is_six_seconds_max(self):
        person, clothes = self.image(), self.image()
        fast = cli_uploads.expand_role_media_payload({
            "person_image_upload_id": person, "clothes_upload_id": clothes,
        }, "alice", now=101)
        self.assertTrue(fast["person_image_data"].startswith("data:image/png;base64,"))
        self.assertTrue(fast["clothes_data"].startswith("data:image/png;base64,"))

        with self.assertRaisesRegex(ValueError, "不能超过 6 秒"):
            cli_uploads.expand_role_media_payload({
                "person_video_upload_id": self.video(6.1), "clothes_upload_id": clothes,
            }, "alice", now=101)

    def test_cinematic_reference_images_share_nine_slots_with_avatars(self):
        references = ["img_" + str(index) * 32 for index in range(7)]
        for avatar_ids in ([1, 2, 3], {"invalid": True}):
            with self.subTest(avatar_ids=avatar_ids), \
                    self.assertRaisesRegex(ValueError, "共用 9 张"):
                cli_uploads.expand_role_media_payload({
                    "avatar_ids": avatar_ids,
                    "reference_image_upload_ids": references,
                }, "alice", now=101)

    def test_video_digest_mime_and_duration_fail_closed(self):
        with mock.patch.object(cli_uploads, "_probe_video_duration", return_value=5):
            with self.assertRaisesRegex(ValueError, "声明格式"):
                cli_uploads.store_video(
                    io.BytesIO(MP4), len(MP4), "alice", "video/webm",
                    hashlib.sha256(MP4).hexdigest(), now=100,
                )
        with mock.patch.object(cli_uploads, "_probe_video_duration", side_effect=ValueError("bad")):
            with self.assertRaisesRegex(ValueError, "bad"):
                cli_uploads.store_video(
                    io.BytesIO(MP4), len(MP4), "alice", "video/mp4",
                    hashlib.sha256(MP4).hexdigest(), now=100,
                )

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                         "ffmpeg/ffprobe are unavailable")
    def test_real_audio_only_mp4_and_webm_are_rejected(self):
        video_path = Path(self.temp.name) / "real-video.mp4"
        subprocess.run([
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
            "color=c=black:size=16x16:duration=0.2", "-an", "-c:v", "mpeg4",
            "-y", str(video_path),
        ], check=True, timeout=20)
        raw_video = video_path.read_bytes()
        stored = cli_uploads.store_video(
            io.BytesIO(raw_video), len(raw_video), "alice", "video/mp4",
            hashlib.sha256(raw_video).hexdigest(), now=100,
        )
        self.assertGreater(stored["duration"], 0)

        for suffix, codec, content_type in (
                (".mp4", "aac", "video/mp4"),
                (".m4a", "aac", "video/mp4"),
                (".webm", "libopus", "video/webm")):
            with self.subTest(suffix=suffix):
                path = Path(self.temp.name) / ("audio-only" + suffix)
                subprocess.run([
                    "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=0.2", "-vn", "-c:a", codec,
                    "-y", str(path),
                ], check=True, timeout=20)
                raw = path.read_bytes()
                self.assertEqual(content_type, cli_uploads.detect_video_mime(raw[:32]))
                with self.assertRaisesRegex(ValueError, "无法读取视频时长"):
                    cli_uploads.store_video(
                        io.BytesIO(raw), len(raw), "alice", content_type,
                        hashlib.sha256(raw).hexdigest(), now=100,
                    )


if __name__ == "__main__":
    unittest.main()
