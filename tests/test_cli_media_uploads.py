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


class RepeatingStream:
    """Produces a large payload without allocating it in test memory."""

    def __init__(self, prefix, length):
        self.prefix = prefix
        self.length = length
        self.position = 0

    def read(self, size=-1):
        if self.position >= self.length:
            return b""
        size = self.length - self.position if size < 0 else min(size, self.length - self.position)
        start, self.position = self.position, self.position + size
        prefix = self.prefix[start:start + size]
        return prefix + b"\0" * (size - len(prefix))


def repeated_digest(prefix, length):
    digest = hashlib.sha256()
    digest.update(prefix)
    remaining = length - len(prefix)
    zeroes = b"\0" * (64 * 1024)
    while remaining:
        chunk = zeroes[:min(len(zeroes), remaining)]
        digest.update(chunk)
        remaining -= len(chunk)
    return digest.hexdigest()


class CLIMediaUploadTests(unittest.TestCase):
    def test_inference_size_guard_runs_before_reading_large_media(self):
        import json
        with tempfile.TemporaryDirectory() as root, mock.patch.object(cli_uploads, "UPLOAD_ROOT", Path(root)):
            for prefix, suffix, ceiling, loader in [
                ("img_", ".png", cli_uploads.MAX_BYTES, cli_uploads._load_image),
                ("vid_", ".mp4", cli_uploads.VIDEO_MAX_BYTES, cli_uploads._load_video_bytes),
            ]:
                uid = prefix + "a" * 32
                media = Path(root) / (uid + suffix)
                with media.open("wb") as handle:
                    handle.truncate(ceiling + 1)
                (Path(root) / (uid + ".json")).write_text(json.dumps({
                    "version": 1, "extension": suffix, "bytes": ceiling + 1,
                    "expires_at": 200, "owner_hash": cli_uploads._owner_hash("alice"),
                }))
                original_open = Path.open
                def guarded_open(path, *args, **kwargs):
                    if path == media:
                        raise AssertionError("large model reference read before guard")
                    return original_open(path, *args, **kwargs)
                with mock.patch.object(Path, "open", guarded_open), self.assertRaisesRegex(ValueError, "模型参考输入"):
                    loader(uid, "alice", 100)

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

    def test_load_preview_returns_verified_bytes_and_mime_for_owner(self):
        image_id, video_id = self.image(), self.video()
        image, image_mime = cli_uploads.load_preview("image", image_id, "alice", now=101)
        video, video_mime = cli_uploads.load_preview("video", video_id, "alice", now=101)
        self.assertEqual(PNG, image)
        self.assertEqual("image/png", image_mime)
        self.assertEqual(MP4, video)
        self.assertEqual("video/mp4", video_mime)
        with self.assertRaisesRegex(ValueError, "不存在或已失效"):
            cli_uploads.load_preview("image", image_id, "bob", now=101)

    def test_open_preview_streams_verified_bytes_without_full_read(self):
        image_id, video_id = self.image(), self.video()
        handle, size, mime = cli_uploads.open_preview("image", image_id, "alice", now=101)
        with handle:
            self.assertEqual(size, len(PNG))
            self.assertEqual(mime, "image/png")
            self.assertEqual(handle.read(), PNG)
        handle, size, mime = cli_uploads.open_preview("video", video_id, "alice", now=101)
        with handle:
            self.assertEqual(size, len(MP4))
            self.assertEqual(mime, "video/mp4")
            handle.seek(4)
            self.assertEqual(handle.read(4), b"ftyp")
        with self.assertRaisesRegex(ValueError, "不存在或已失效"):
            cli_uploads.open_preview("video", video_id, "bob", now=101)
        with self.assertRaisesRegex(ValueError, "素材类型不支持预览"):
            cli_uploads.open_preview("audio", video_id, "alice", now=101)

    def test_verify_upload_checks_owner_expiry_and_size_without_full_read(self):
        image_id, video_id = self.image(), self.video()
        self.assertTrue(cli_uploads.verify_upload("image", image_id, "alice", now=101))
        self.assertTrue(cli_uploads.verify_upload("video", video_id, "alice", now=101))
        # 归属校验
        self.assertFalse(cli_uploads.verify_upload("image", image_id, "bob", now=101))
        self.assertFalse(cli_uploads.verify_upload("video", video_id, "bob", now=101))
        # 过期
        self.assertFalse(cli_uploads.verify_upload("image", image_id, "alice", now=99999))
        # 格式/存在性
        self.assertFalse(cli_uploads.verify_upload("image", "img_" + "f" * 32, "alice", now=101))
        self.assertFalse(cli_uploads.verify_upload("image", "vid_" + "f" * 32, "alice", now=101))
        self.assertFalse(cli_uploads.verify_upload("audio", image_id, "alice", now=101))
        self.assertFalse(cli_uploads.verify_upload("image", "not-an-id", "alice", now=101))

    def test_verify_upload_rejects_truncated_or_same_size_replaced_payload(self):
        image_id = self.image()
        image_path, _ = cli_uploads._paths(image_id, ".png")
        original = image_path.read_bytes()
        image_path.write_bytes(original[:-1])
        self.assertFalse(cli_uploads.verify_upload("image", image_id, "alice", now=101))

        video_id = self.video()
        video_path, _ = cli_uploads._video_paths(video_id, ".mp4")
        replaced = bytearray(video_path.read_bytes())
        replaced[-1] ^= 1
        video_path.write_bytes(replaced)
        self.assertFalse(cli_uploads.verify_upload("video", video_id, "alice", now=101))

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

    def test_duplicate_image_and_video_uploads_reuse_active_ids(self):
        with mock.patch.object(cli_uploads, "MAX_USER_FILES", 2), \
                mock.patch.object(cli_uploads, "_probe_video_duration", return_value=5.5):
            image = cli_uploads.store_image(
                io.BytesIO(PNG), len(PNG), "alice", "image/png",
                hashlib.sha256(PNG).hexdigest(), now=100,
            )
            video = cli_uploads.store_video(
                io.BytesIO(MP4), len(MP4), "alice", "video/mp4",
                hashlib.sha256(MP4).hexdigest(), now=100,
            )
            repeated_image = cli_uploads.store_image(
                io.BytesIO(PNG), len(PNG), "alice", "image/png",
                hashlib.sha256(PNG).hexdigest(), now=101,
            )
            repeated_video = cli_uploads.store_video(
                io.BytesIO(MP4), len(MP4), "alice", "video/mp4",
                hashlib.sha256(MP4).hexdigest(), now=101,
            )

        self.assertEqual(image["upload_id"], repeated_image["upload_id"])
        self.assertEqual(video["upload_id"], repeated_video["upload_id"])
        self.assertEqual(2, len(list(Path(self.temp.name).glob("*.json"))))
        self.assertEqual(101 + cli_uploads.TTL, repeated_video["expires_at"])

    def test_generic_video_over_legacy_cap_streams_and_shared_quota_still_applies(self):
        length = 32 * 1024 * 1024 + 1
        digest = repeated_digest(MP4, length)
        with mock.patch.object(cli_uploads, "_probe_video_duration", return_value=5), \
                mock.patch.object(cli_uploads, "MAX_USER_BYTES", length):
            stored = cli_uploads.store_video(
                RepeatingStream(MP4, length), length, "alice", "video/mp4", digest, now=100,
            )
            self.assertEqual(length, stored["bytes"])
            self.assertTrue(cli_uploads.verify_upload("video", stored["upload_id"], "alice", now=101))
            with self.assertRaisesRegex(ValueError, "临时图片或视频已达上限"):
                cli_uploads.store_image(
                    io.BytesIO(PNG), len(PNG), "alice", "image/png",
                    hashlib.sha256(PNG).hexdigest(), now=101,
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
