import base64
import importlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SERVER = str(Path(__file__).resolve().parents[1] / "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

video = importlib.import_module("content_domains.video")


JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 16
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


class ImageMimeNormalizationTests(unittest.TestCase):
    def test_save_data_file_uses_detected_jpeg_extension(self):
        data_url = "data:image/png;base64," + base64.b64encode(JPEG_BYTES).decode()
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            video, "_out_path", lambda rel: Path(tmp) / rel
        ):
            rel = video._save_data_file(data_url, "avatar_src", [".jpg", ".png", ".webp"])

            self.assertTrue(rel.endswith(".jpg"))
            self.assertEqual((Path(tmp) / rel).read_bytes(), JPEG_BYTES)

    def test_ensure_heygen_image_corrects_mismatched_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "avatar.png"
            source.write_bytes(JPEG_BYTES)

            normalized = video._ensure_heygen_image_jpg(source)

            self.assertEqual(normalized.suffix, ".jpg")
            self.assertEqual(normalized.read_bytes(), JPEG_BYTES)

    def test_ensure_heygen_image_keeps_matching_png(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "avatar.png"
            source.write_bytes(PNG_BYTES)

            self.assertEqual(video._ensure_heygen_image_jpg(source), source)

    def test_heygen_upload_uses_content_mime_for_old_mismatched_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "old-avatar.png"
            source.write_bytes(JPEG_BYTES)
            with patch.object(
                video,
                "_heygen_direct_req",
                return_value={"data": {"asset_id": "asset-1"}},
            ) as request:
                self.assertEqual(video._heygen_upload_asset(source, direct=True), "asset-1")

            self.assertEqual(request.call_args.args[3], "image/jpeg")
            self.assertEqual(request.call_args.args[2], JPEG_BYTES)


if __name__ == "__main__":
    unittest.main()
