import base64
import hashlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server.content_domains import cli_uploads


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl9l1sAAAAASUVORK5CYII="
)
JPEG = b"\xff\xd8\xff\xe0" + b"jpeg-test"


class CLIImageUploadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root_patch = mock.patch.object(cli_uploads, "UPLOAD_ROOT", Path(self.temp.name))
        self.root_patch.start()

    def tearDown(self):
        self.root_patch.stop()
        self.temp.cleanup()

    def upload(self, raw=PNG, mime="image/png", username="alice", now=100):
        return cli_uploads.store_image(
            io.BytesIO(raw), len(raw), username, mime, hashlib.sha256(raw).hexdigest(), now=now,
        )

    def test_private_upload_expands_for_owner_only(self):
        uploaded = self.upload()
        body = cli_uploads.expand_image_payload(
            {"provider": "openai", "image_upload_id": uploaded["upload_id"]}, "alice", now=101,
        )
        self.assertEqual(PNG, base64.b64decode(body["image"]))
        self.assertNotIn("image_upload_id", body)
        with self.assertRaisesRegex(ValueError, "不存在或已失效"):
            cli_uploads.expand_image_payload(
                {"provider": "openai", "image_upload_id": uploaded["upload_id"]}, "bob", now=101,
            )

    def test_multi_reference_and_png_mask_contract(self):
        first, second = self.upload(now=100), self.upload(now=100)
        body = cli_uploads.expand_image_payload({
            "provider": "xiaole", "reference_upload_ids": [first["upload_id"], second["upload_id"]],
        }, "alice", now=101)
        self.assertEqual(2, len(body["reference_images"]))
        jpg = self.upload(JPEG, "image/jpeg", now=100)
        with self.assertRaisesRegex(ValueError, "蒙版必须是 PNG"):
            cli_uploads.expand_image_payload({
                "provider": "openai", "image_upload_id": first["upload_id"],
                "mask_upload_id": jpg["upload_id"],
            }, "alice", now=101)

    def test_digest_mime_expiry_and_combinations_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "发生变化"):
            cli_uploads.store_image(io.BytesIO(PNG), len(PNG), "alice", "image/png", "0" * 64, now=100)
        with self.assertRaisesRegex(ValueError, "声明格式"):
            cli_uploads.store_image(
                io.BytesIO(PNG), len(PNG), "alice", "image/jpeg", hashlib.sha256(PNG).hexdigest(), now=100,
            )
        uploaded = self.upload(now=100)
        with self.assertRaisesRegex(ValueError, "已过期"):
            cli_uploads.expand_image_payload(
                {"provider": "openai", "image_upload_id": uploaded["upload_id"]},
                "alice", now=100 + cli_uploads.TTL + 1,
            )
        with self.assertRaisesRegex(ValueError, "单参考图和多参考图"):
            cli_uploads.expand_image_payload({
                "provider": "xiaole", "image_upload_id": "img_" + "a" * 32,
                "reference_upload_ids": ["img_" + "b" * 32],
            }, "alice", now=101)

    def test_account_quota_disk_reserve_and_stale_temp_cleanup(self):
        stale = Path(self.temp.name) / ".img_stale.tmp"
        stale.write_bytes(b"stale")
        os.utime(stale, (0, 0))
        with mock.patch.object(cli_uploads, "MAX_USER_FILES", 1):
            self.upload(now=1000)
            with self.assertRaisesRegex(ValueError, "临时图片已达上限"):
                self.upload(now=1001)
        self.assertFalse(stale.exists())
        with mock.patch.object(
            cli_uploads.shutil, "disk_usage",
            return_value=mock.Mock(free=cli_uploads.MIN_FREE_BYTES),
        ):
            with self.assertRaises(OSError):
                self.upload(username="bob", now=1002)


if __name__ == "__main__":
    unittest.main()
