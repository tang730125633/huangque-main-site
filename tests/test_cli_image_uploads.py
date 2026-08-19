import base64
import hashlib
import io
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from types import SimpleNamespace

from server.content_domains import cli_gateway, cli_uploads


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

    def test_minimax_accepts_five_and_banana_accepts_fourteen_reference_uploads(self):
        upload_ids = [self.upload(now=100)["upload_id"] for _ in range(14)]
        minimax = cli_uploads.expand_image_payload({
            "channel": "minimax", "reference_upload_ids": upload_ids[:5],
        }, "alice", now=101)
        banana = cli_uploads.expand_image_payload({
            "provider": "banana", "reference_upload_ids": upload_ids,
        }, "alice", now=101)
        self.assertEqual(5, len(minimax["reference_images"]))
        self.assertTrue(minimax["reference_images"][0].startswith("data:image/png;base64,"))
        self.assertEqual(14, len(banana["images"]))
        self.assertEqual("image/png", banana["images"][0]["mime_type"])

    def test_sora_reference_is_expanded_as_one_data_url(self):
        uploaded = self.upload(now=100)
        body = cli_uploads.expand_image_payload({
            "channel": "sora", "reference_upload_ids": [uploaded["upload_id"]],
        }, "alice", now=101)
        self.assertEqual(1, len(body["reference_images"]))
        self.assertTrue(body["reference_images"][0].startswith("data:image/png;base64,"))

    def test_cli_quote_expands_minimax_reference_upload(self):
        uploaded = self.upload(now=int(time.time()))

        class Handler:
            headers = {"X-HQ-Internal-Token": "secret"}

            def _token(self):
                return "token"

            def _json_body_strict(self):
                return {"kind": "xiaole_video", "payload": {
                    "channel": "minimax", "reference_upload_ids": [uploaded["upload_id"]],
                }}

            def _send(self, status, body):
                self.result = (status, body)

        handler = Handler()
        seen = {}

        def validate(payload):
            seen.update(payload)
            return payload

        cli_gateway.handle_quote(
            handler, "/api/gen/cli/quote", lambda token: {"username": "alice"},
            lambda user: False, lambda: False,
            SimpleNamespace(require_enabled=lambda feature: None, FeatureDisabled=RuntimeError),
            SimpleNamespace(cost_of=lambda kind, payload: 90, get_points=lambda username: 100),
            SimpleNamespace(), SimpleNamespace(validate_xiaole_video_payload=validate), "secret",
        )
        self.assertEqual(200, handler.result[0], handler.result[1])
        self.assertEqual(1, len(seen["reference_images"]))
        self.assertNotIn("reference_upload_ids", seen)

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
