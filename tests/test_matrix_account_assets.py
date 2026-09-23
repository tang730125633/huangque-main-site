import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from content_domains import matrix_account_assets as assets, cos


class AccountAssetsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patch = patch.object(assets, "ROOT", self.root)
        self.patch.start()
        self.account = "HQTEST"
        self.aid = "a"*32
        self.data = b"verified source bytes"
        self.item = {"id": self.aid, "ext": ".mp4", "source": "upload",
            "size": len(self.data), "sha256": hashlib.sha256(self.data).hexdigest(),
            "cos_key": f"hq-materials/{self.account}/{self.aid}.mp4"}
        (self.root / self.account).mkdir()
        self.write([self.item])

    def write(self, items):
        (self.root / self.account / "index.json").write_text(json.dumps({"items": items}), encoding="utf-8")

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def test_cos_recovers_missing_local_original_and_checks_hash(self):
        item = assets.select(self.account, {"mode": "picked", "ids": [self.aid]})[0]
        target = self.root / "cache" / "source.mp4"
        with patch.object(cos, "head", return_value={"Content-Length": len(self.data)}), patch.object(
                cos, "download", side_effect=lambda key, path: path.write_bytes(self.data)) as download:
            self.assertEqual(assets.fetch(item, target), self.item["sha256"])
        self.assertEqual(download.call_args.args[0], cos._object_key(self.item["cos_key"]))
        self.assertFalse((self.root / self.account / (self.aid + ".mp4")).exists())

    def test_cross_account_and_injected_key_never_fetch(self):
        with self.assertRaises(ValueError):
            assets.select("HQOTHER", {"mode": "picked", "ids": [self.aid]})
        self.write([dict(self.item, cos_key=f"hq-materials/OTHER/{self.aid}.mp4")])
        self.assertEqual(assets.records(self.account), [])
        with self.assertRaises(ValueError):
            assets.records("../HQOTHER")

    def test_deleted_or_changed_identity_cannot_replay(self):
        item = assets.records(self.account)[0]
        self.write([])
        with self.assertRaisesRegex(ValueError, "MATERIAL_UNAVAILABLE"):
            assets.fetch(item, self.root / "no.mp4")

    def test_checksum_failure_is_not_silent_fallback(self):
        item = assets.records(self.account)[0]
        with patch.object(cos, "head", return_value={"Content-Length": 3}), patch.object(
                cos, "download", side_effect=lambda key, path: path.write_bytes(b"bad")):
            with self.assertRaisesRegex(RuntimeError, "校验失败"):
                assets.fetch(item, self.root / "cache.mp4")

    def test_auto_excludes_prior_finished_videos(self):
        self.write([self.item, dict(self.item, id="b"*32, cos_key="", source="agent")])
        self.assertEqual([i["asset_id"] for i in assets.select(self.account, {"mode":"auto"})], [self.aid])


if __name__ == "__main__":
    unittest.main()
