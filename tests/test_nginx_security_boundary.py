import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATHS = (
    ROOT / "deploy/nginx-huangquechuanmei.conf",
)
INTERNAL_AUTH_PATHS = (
    "/api/auth/points/deduct",
    "/api/auth/points/refund",
    "/api/auth/admin/points/adjust",
    "/api/auth/admin/points/audit",
    "/api/auth/admin/users",
    "/api/auth/admin/recharge/review",
    "/api/auth/admin/recharge/orders",
)


def _location_block(config, modifier, path):
    pattern = rf"location\s+{re.escape(modifier)}\s+{re.escape(path)}\s*\{{([^{{}}]*)\}}"
    match = re.search(pattern, config)
    if match is None:
        raise AssertionError(f"missing location {modifier} {path}")
    return match.group(0)


class NginxSecurityBoundaryTest(unittest.TestCase):
    def test_internal_auth_routes_are_exact_404s_before_public_auth_proxy(self):
        for config_path in CONFIG_PATHS:
            with self.subTest(config=config_path.name):
                config = config_path.read_text(encoding="utf-8")
                public_auth_index = config.index("location ^~ /api/auth/")

                for path in INTERNAL_AUTH_PATHS:
                    directive = f"location = {path}"
                    self.assertEqual(config.count(directive), 1, directive)
                    block = _location_block(config, "=", path)
                    self.assertRegex(block, r"\breturn\s+404\s*;")
                    self.assertLess(config.index(directive), public_auth_index)

    def test_internal_auth_routes_are_not_hidden_with_a_broad_regex(self):
        for config_path in CONFIG_PATHS:
            with self.subTest(config=config_path.name):
                config = config_path.read_text(encoding="utf-8")
                broad_auth_regexes = re.findall(
                    r"^\s*location\s+~\*?\s+[^\n{]*?/api/auth[^\n{]*\{",
                    config,
                    flags=re.MULTILINE,
                )
                self.assertEqual(broad_auth_regexes, [])

    def test_public_auth_and_admin_proxies_clear_internal_token(self):
        for config_path in CONFIG_PATHS:
            with self.subTest(config=config_path.name):
                config = config_path.read_text(encoding="utf-8")
                for path in ("/api/auth/", "/api/admin/"):
                    block = _location_block(config, "^~", path)
                    self.assertIn('proxy_set_header X-HQ-Internal-Token "";', block)


if __name__ == "__main__":
    unittest.main()
