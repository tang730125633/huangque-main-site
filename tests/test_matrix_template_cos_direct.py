from __future__ import annotations

import importlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"


class MatrixTemplateCosDirectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if str(SERVER) not in sys.path:
            sys.path.insert(0, str(SERVER))

    def test_direct_cos_session_is_opt_in(self):
        module = importlib.import_module("content_domains.cos")
        captured = []

        class FakeConfig:
            def __init__(self, **kwargs):
                self.values = kwargs

        class FakeClient:
            def __init__(self, config, retry=3, session=None):
                captured.append({
                    "config": config.values,
                    "retry": retry,
                    "session": session,
                })

        fake_sdk = SimpleNamespace(CosConfig=FakeConfig, CosS3Client=FakeClient)
        direct_session = SimpleNamespace(trust_env=True)
        module._client_singleton = None
        module._direct_client_singleton = None
        try:
            with mock.patch.dict(sys.modules, {"qcloud_cos": fake_sdk}), \
                 mock.patch("requests.Session", return_value=direct_session):
                default_client = module._client()
                direct_client = module._client(direct=True)

            self.assertIsNot(default_client, direct_client)
            self.assertIsNone(captured[0]["session"])
            self.assertIs(direct_session, captured[1]["session"])
            self.assertFalse(direct_session.trust_env)
            self.assertEqual([3, 3], [item["retry"] for item in captured])
            self.assertEqual(
                ["https", "https"],
                [item["config"]["Scheme"] for item in captured],
            )
        finally:
            module._client_singleton = None
            module._direct_client_singleton = None

    def test_public_url_keeps_default_upload_path_unchanged(self):
        core = importlib.import_module("content_domains.core")
        cos = importlib.import_module("content_domains.cos")
        with tempfile.TemporaryDirectory() as temp:
            out_dir = Path(temp)
            video = out_dir / "video" / "matrix.mp4"
            video.parent.mkdir(parents=True)
            video.write_bytes(b"video")
            with mock.patch.object(core, "OUT_DIR", out_dir), \
                 mock.patch.object(cos, "enabled", return_value=True), \
                 mock.patch.object(
                     cos, "upload", side_effect=["default-url", "direct-url"],
                 ) as upload:
                default_url = core.public_url(
                    "video/matrix.mp4", "video/mp4", private=True,
                )
                direct_url = core.public_url(
                    "video/matrix.mp4", "video/mp4",
                    private=True, direct_cos=True,
                )

        self.assertEqual(("default-url", "direct-url"), (default_url, direct_url))
        self.assertEqual(
            mock.call(video, "video/matrix.mp4", "video/mp4", private=True),
            upload.call_args_list[0],
        )
        self.assertEqual(
            mock.call(
                video, "video/matrix.mp4", "video/mp4",
                private=True, direct=True,
            ),
            upload.call_args_list[1],
        )


if __name__ == "__main__":
    unittest.main()
