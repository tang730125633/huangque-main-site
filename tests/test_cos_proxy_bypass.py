from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"


class CosProxyBypassTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if str(SERVER) not in sys.path:
            sys.path.insert(0, str(SERVER))

    @staticmethod
    def fake_sdk(captured):
        class FakeConfig:
            def __init__(self, **kwargs):
                captured["config"] = kwargs

        class FakeClient:
            def __init__(self, config, retry=3, session=None):
                captured["client_config"] = config
                captured["retry"] = retry
                captured["session"] = session

        return SimpleNamespace(CosConfig=FakeConfig, CosS3Client=FakeClient)

    def assert_direct_session(self, module, getter_name, singleton_name):
        captured = {}
        fake_session = SimpleNamespace(trust_env=True)
        setattr(module, singleton_name, None)
        with mock.patch.dict(
            sys.modules, {"qcloud_cos": self.fake_sdk(captured)}
        ), mock.patch("requests.Session", return_value=fake_session):
            client = getattr(module, getter_name)()
        self.assertIs(client, getattr(module, singleton_name))
        self.assertIs(fake_session, captured["session"])
        self.assertFalse(fake_session.trust_env)
        self.assertEqual(3, captured["retry"])
        self.assertEqual("https", captured["config"]["Scheme"])

    def test_content_cos_ignores_process_proxy_environment(self):
        module = importlib.import_module("content_domains.cos")
        self.assert_direct_session(module, "_client", "_client_singleton")

    def test_imggen_cos_ignores_process_proxy_environment(self):
        module = importlib.import_module("imggen_api")
        self.assert_direct_session(module, "_cos_get_client", "_cos_client")


if __name__ == "__main__":
    unittest.main()
