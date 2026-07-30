import re
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
CONFIG_PROXIES = {
    "deploy/nginx-huangquechuanmei.conf": ("/api/auth/", "/api/admin/", "/api/gen/"),
    "server/nginx-huangquechuanmei.conf": ("/api/auth/", "/api/gen/"),
}
INTERNAL_AUTH_PATHS = (
    "/api/auth/points",
    "/api/auth/points/deduct",
    "/api/auth/points/refund",
    "/api/auth/membership/voice-slot-entitlement",
    "/api/auth/admin/points/adjust",
    "/api/auth/admin/points/audit",
    "/api/auth/admin/users",
    "/api/auth/admin/password/reset",
    "/api/auth/admin/recharge/review",
    "/api/auth/admin/recharge/orders",
)


def location_block(config, path):
    matches = list(
        re.finditer(
            rf"(?m)^[ \t]*location[ \t]+\^~[ \t]+{re.escape(path)}[ \t]*\{{",
            config,
        )
    )
    if len(matches) != 1:
        raise AssertionError(f"expected one public proxy for {path}")
    start = matches[0].start()
    depth = 0
    for index in range(config.find("{", start), len(config)):
        if config[index] == "{":
            depth += 1
        elif config[index] == "}":
            depth -= 1
            if depth == 0:
                return start, config[start : index + 1]
    raise AssertionError(f"unterminated location for {path}")


class NginxInternalBoundaryTests(unittest.TestCase):
    def test_internal_auth_routes_are_exact_404s_in_both_configs(self):
        pattern = (
            r"(?m)^[ \t]*location[ \t]+=[ \t]+{path}[ \t]*"
            r"\{{[ \t]*return[ \t]+404[ \t]*;[ \t]*\}}[ \t]*$"
        )
        for relative_path in CONFIG_PROXIES:
            config = (ROOT / relative_path).read_text(encoding="utf-8")
            public_auth_start, _ = location_block(config, "/api/auth/")
            for path in INTERNAL_AUTH_PATHS:
                with self.subTest(config=relative_path, path=path):
                    matches = list(
                        re.finditer(pattern.format(path=re.escape(path)), config)
                    )
                    self.assertEqual(len(matches), 1)
                    self.assertLess(matches[0].start(), public_auth_start)

    def test_public_proxies_drop_client_internal_token(self):
        header = re.compile(
            r'(?m)^[ \t]*proxy_set_header[ \t]+'
            r'X-HQ-Internal-Token[ \t]+""[ \t]*;[ \t]*$'
        )
        for relative_path, proxy_paths in CONFIG_PROXIES.items():
            config = (ROOT / relative_path).read_text(encoding="utf-8")
            for path in proxy_paths:
                with self.subTest(config=relative_path, path=path):
                    _, block = location_block(config, path)
                    self.assertEqual(len(header.findall(block)), 1)


if __name__ == "__main__":
    unittest.main()
