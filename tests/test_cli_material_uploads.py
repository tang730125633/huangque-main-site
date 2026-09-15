"""顾客素材上传（数字人一键成片）：200MB / 10 个 / 图片+视频+音频。

背景：2026-09 把这条口从「10MB、只能 PNG/JPG/WebP、最多 20 个、合计 96MB」
放宽到「单文件 200MB、整批 200MB、最多 10 个、图片+视频+音频」。

⚠️ 这些用例同时钉住一条边界：**通用图片口（store_image）不受影响** ——
它还同时被视频助手和 CLI 上传使用，跟着一起放开会波及那两条链路。
"""
import base64
import hashlib
import io
import pathlib
import tempfile
import time
import unittest
from unittest import mock

from server.content_domains import cli_uploads


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl9l1sAAAAASUVORK5CYII="
)
MP4 = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 48
MP3 = b"ID3\x04\x00" + b"\x00" * 59
HEIC = b"\x00\x00\x00\x20ftypheic" + b"\x00" * 48
ZIP = b"PK\x03\x04" + b"\x00" * 60
PDF = b"%PDF-1.7\n" + b"\x00" * 56


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class MaterialMimeDetectionTests(unittest.TestCase):
    """识别一律以文件头为准；认不出来的一律拒。"""

    def test_recognizes_every_whitelisted_family(self):
        cases = [
            (PNG, "image/png"), (b"\xff\xd8\xff\xe0" + b"\x00" * 12, "image/jpeg"),
            (b"GIF89a" + b"\x00" * 10, "image/gif"), (b"BM" + b"\x00" * 14, "image/bmp"),
            (b"II*\x00" + b"\x00" * 12, "image/tiff"),
            (b"RIFF\x00\x00\x00\x00WEBP", "image/webp"),
            (HEIC, "image/heic"),
            (b"\x00\x00\x00\x20ftypavif" + b"\x00" * 8, "image/avif"),
            (MP4, "video/mp4"),
            (b"\x00\x00\x00\x20ftypqt  " + b"\x00" * 8, "video/quicktime"),
            (b"\x1aE\xdf\xa3" + b"\x00" * 12, "video/webm"),
            (b"RIFF\x00\x00\x00\x00AVI ", "video/x-msvideo"),
            (MP3, "audio/mpeg"),
            (b"RIFF\x00\x00\x00\x00WAVE", "audio/wav"),
            (b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 8, "audio/mp4"),
            (b"\xff\xf1\x50\x00" + b"\x00" * 12, "audio/aac"),
            (b"OggS" + b"\x00" * 12, "audio/ogg"),
            (b"fLaC" + b"\x00" * 12, "audio/flac"),
        ]
        for raw, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(cli_uploads.detect_material_mime(raw[:16]), expected)

    def test_every_detected_mime_is_in_the_whitelist(self):
        detected = {
            cli_uploads.detect_material_mime(head)
            for head in (PNG, MP4, MP3, HEIC)
        }
        self.assertTrue(detected <= set(cli_uploads.MATERIAL_MIME_EXTENSIONS))

    def test_rejects_unrecognised_bytes(self):
        for raw in (ZIP, PDF, b"", b"\x00" * 16):
            with self.subTest(raw=raw[:6]):
                self.assertEqual(cli_uploads.detect_material_mime(raw[:16]), "")

    def test_jpeg_is_not_mistaken_for_mp3(self):
        # JPEG 的 FF D8 FF 与 MP3 帧同步 FF Ex 只差一位 —— 曾经是踩坑点
        self.assertEqual(
            cli_uploads.detect_material_mime(b"\xff\xd8\xff\xe0" + b"\x00" * 12),
            "image/jpeg",
        )
        self.assertEqual(
            cli_uploads.detect_material_mime(b"\xff\xfb\x90\x00" + b"\x00" * 12),
            "audio/mpeg",
        )


class MaterialUploadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root_patch = mock.patch.object(
            cli_uploads, "UPLOAD_ROOT", pathlib.Path(self.temp.name))
        self.root_patch.start()

    def tearDown(self):
        self.root_patch.stop()
        self.temp.cleanup()

    def upload(self, raw=PNG, mime="image/png", username="alice", now=100, length=None):
        return cli_uploads.store_material(
            io.BytesIO(raw), len(raw) if length is None else length,
            username, mime, sha(raw), now=now)

    # ---- 类型 ----

    def test_accepts_image_video_and_audio(self):
        for raw, mime in ((PNG, "image/png"), (MP4, "video/mp4"), (MP3, "audio/mpeg"),
                          (HEIC, "image/heic")):
            with self.subTest(mime=mime):
                result = self.upload(raw, mime)
                self.assertTrue(result["upload_id"].startswith("img_"))
                self.assertEqual(result["mime"], mime)

    def test_rejects_zip_and_pdf(self):
        for raw, mime in ((ZIP, "application/zip"), (PDF, "application/pdf")):
            with self.subTest(mime=mime):
                with self.assertRaisesRegex(ValueError, "仅支持"):
                    self.upload(raw, mime)

    def test_rejects_when_declared_type_disagrees_with_content(self):
        # 声明成图片、实际是 zip —— 必须按内容判，不能被声明值骗过去
        with self.assertRaisesRegex(ValueError, "内容与声明格式不一致|仅支持"):
            self.upload(ZIP, "image/png")

    # ---- 大小 ----

    def test_single_file_ceiling_is_200mb(self):
        # 只校验门槛，不真的写 200MB：把长度声明成超限即刻拒绝
        over = cli_uploads.MATERIAL_MAX_BYTES + 1
        with self.assertRaisesRegex(ValueError, "200MB"):
            cli_uploads.store_material(
                io.BytesIO(b""), over, "alice", "image/png", sha(b""), now=100)

    def test_batch_total_ceiling(self):
        # 单文件都没超，但加起来超了整批上限。
        # 用缩小的数字验证同一段逻辑 —— 真去写 200MB 只会让测试变慢。
        big = PNG + b"\x00" * 40          # 实测 108 字节
        with mock.patch.object(cli_uploads, "MATERIAL_MAX_BYTES", 4096), \
             mock.patch.object(cli_uploads, "MATERIAL_MAX_USER_BYTES", 200):
            self.upload(big)
            self.assertEqual(len(big), 108)
            with self.assertRaisesRegex(ValueError, "上限"):
                self.upload(big)

    def test_batch_file_count_ceiling_is_10(self):
        with mock.patch.object(cli_uploads, "MATERIAL_MAX_BYTES", 1024):
            for index in range(cli_uploads.MATERIAL_MAX_USER_FILES):
                self.upload(PNG, now=100)
            with self.assertRaisesRegex(ValueError, "上限"):
                self.upload(PNG, now=100)

    # ---- 通用图片口不受影响 ----

    def test_generic_image_upload_still_refuses_video_and_oversize(self):
        with self.assertRaisesRegex(ValueError, "PNG / JPG / WebP"):
            cli_uploads.store_image(
                io.BytesIO(MP4), len(MP4), "alice", "video/mp4", sha(MP4), now=100)
        with self.assertRaisesRegex(ValueError, "10MB"):
            cli_uploads.store_image(
                io.BytesIO(b""), cli_uploads.MAX_BYTES + 1, "alice",
                "image/png", sha(b""), now=100)

    # ---- 读取 / 清理 ----

    def test_read_back_and_discard(self):
        result = self.upload(MP4, "video/mp4")
        raw, meta = cli_uploads.read_material_bytes(result["upload_id"], "alice", now=100)
        self.assertEqual(raw, MP4)
        self.assertEqual(meta["mime"], "video/mp4")
        self.assertTrue(cli_uploads.discard_material(result["upload_id"], "alice", now=100))

    def test_expiry_cleanup_removes_the_data_file_too(self):
        """素材的扩展名（.mp4/.heic…）不在图片表里，清理必须覆盖它们，
        否则过期后只删掉 .json，数据文件留在盘上变成垃圾。"""
        result = self.upload(MP4, "video/mp4", now=100)
        data_files = [
            path for path in pathlib.Path(self.temp.name).iterdir()
            if path.suffix != ".json" and not path.name.startswith(".")
        ]
        self.assertEqual(len(data_files), 1, "素材数据文件应当已落盘")
        cli_uploads._cleanup(100 + cli_uploads.TTL + 1)
        leftovers = [p for p in pathlib.Path(self.temp.name).iterdir() if not p.name.startswith(".")]
        self.assertEqual(leftovers, [], "过期后数据文件和元数据都该被删掉")


class MaterialLimitsConstantsTests(unittest.TestCase):
    """把「这一口改了什么、没改什么」钉死，防止以后被误改回去。"""

    def test_material_limits(self):
        self.assertEqual(cli_uploads.MATERIAL_MAX_BYTES, 200 * 1024 * 1024)
        self.assertEqual(cli_uploads.MATERIAL_MAX_USER_BYTES, 200 * 1024 * 1024)
        self.assertEqual(cli_uploads.MATERIAL_MAX_USER_FILES, 10)
        self.assertEqual(len(cli_uploads.MATERIAL_MIME_EXTENSIONS), 18)

    def test_generic_image_limits_unchanged(self):
        self.assertEqual(cli_uploads.MAX_BYTES, 10 * 1024 * 1024)
        self.assertEqual(cli_uploads.MAX_USER_FILES, 20)
        self.assertEqual(cli_uploads.MIME_EXTENSIONS,
                         {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"})


if __name__ == "__main__":
    unittest.main()
