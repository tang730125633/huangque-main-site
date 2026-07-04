import importlib
import sys
import unittest
from pathlib import Path


class ContentDomainTests(unittest.TestCase):
    def setUp(self):
        server_dir = str(Path(__file__).resolve().parents[1] / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)

    def test_entrypoint_uses_domain_registry(self):
        content_api = importlib.import_module("content_api")
        self.assertEqual(
            sorted(content_api.HANDLERS),
            ["audio", "collect", "copy", "image", "leads", "video"],
        )
        self.assertIs(content_api.HANDLERS, content_api.registry.HANDLERS)

    def test_domains_export_expected_handlers(self):
        registry = importlib.import_module("content_domains.registry")
        for name in ("image", "copy", "collect", "leads", "audio", "video"):
            self.assertIn(name, registry.HANDLERS)
            self.assertTrue(callable(registry.HANDLERS[name]))

    def test_core_does_not_own_domain_handlers(self):
        core = importlib.import_module("content_domains.core")
        for name in ("gen_image", "gen_copy", "gen_collect", "gen_leads", "gen_audio", "gen_video"):
            self.assertFalse(hasattr(core, name), name)

        core_path = Path(core.__file__)
        self.assertLess(len(core_path.read_text(encoding="utf-8").splitlines()), 1000)


if __name__ == "__main__":
    unittest.main()
