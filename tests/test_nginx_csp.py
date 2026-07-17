import re
import unittest
from pathlib import Path

from tests.test_nginx_security_boundary import _active_locations, _block_end, _strip_comments


ROOT = Path(__file__).parents[1]
HTTP_CONFIG = ROOT / "deploy/test-server/nginx.conf"
HTTPS_CONFIG = ROOT / "deploy/nginx-huangquechuanmei.conf"
REQUIRED_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


def _active_header_values(config, name):
    return re.findall(
        rf'^[ \t]*add_header[ \t]+{re.escape(name)}[ \t]+"([^"]+)"[ \t]+always[ \t]*;',
        _strip_comments(config),
        flags=re.MULTILINE,
    )


def _first_server_without_locations(config):
    active_config = _strip_comments(config)
    server = re.search(r"^[ \t]*server[ \t]*\{", active_config, flags=re.MULTILINE)
    if server is None:
        raise AssertionError("missing active server block")
    opening_brace = active_config.find("{", server.start(), server.end())
    server_block = active_config[server.start():_block_end(active_config, opening_brace)]
    locations = _active_locations(server_block)
    for _, _, start, block in reversed(locations):
        server_block = server_block[:start] + server_block[start + len(block):]
    return server_block


def _assert_required_headers(test_case, config, require_hsts=False):
    server_block = _first_server_without_locations(config)
    for name, value in REQUIRED_HEADERS.items():
        test_case.assertEqual(_active_header_values(server_block, name), [value], name)

    server_policies = _active_header_values(server_block, "Content-Security-Policy")
    test_case.assertEqual(len(server_policies), 1, "server must set CSP exactly once")
    test_case.assertIn("frame-ancestors 'self'", server_policies[0])

    if require_hsts:
        test_case.assertEqual(
            _active_header_values(server_block, "Strict-Transport-Security"),
            ["max-age=31536000; includeSubDomains"],
        )

    for _, path, _, block in _active_locations(config):
        if re.search(r"^[ \t]*add_header\s+", block, flags=re.MULTILINE):
            for name, value in REQUIRED_HEADERS.items():
                test_case.assertEqual(
                    _active_header_values(block, name),
                    [value],
                    f"{path} replaces inherited headers and must retain {name}",
                )
            policies = _active_header_values(block, "Content-Security-Policy")
            test_case.assertEqual(len(policies), 1, f"{path} must retain CSP")
            test_case.assertIn("frame-ancestors 'self'", policies[0])
            if require_hsts:
                test_case.assertEqual(
                    _active_header_values(block, "Strict-Transport-Security"),
                    ["max-age=31536000; includeSubDomains"],
                    f"{path} replaces inherited headers and must retain HSTS",
                )


class NginxCspTest(unittest.TestCase):
    def test_csp_is_active_and_consistent(self):
        config = HTTPS_CONFIG.read_text(encoding="utf-8")
        policies = _active_header_values(config, "Content-Security-Policy")

        self.assertGreaterEqual(len(policies), 2)
        self.assertEqual(len(set(policies)), 1)
        for directive in (
            "base-uri 'self'",
            "object-src 'none'",
            "script-src 'self' 'unsafe-inline' https://unpkg.com",
            "style-src 'self' 'unsafe-inline' https://unpkg.com",
            "frame-ancestors 'self'",
        ):
            self.assertIn(directive, policies[0])

    def test_http_and_https_templates_set_required_security_headers(self):
        for config_path in (HTTP_CONFIG, HTTPS_CONFIG):
            with self.subTest(config=config_path.name):
                _assert_required_headers(
                    self,
                    config_path.read_text(encoding="utf-8"),
                    require_hsts=config_path == HTTPS_CONFIG,
                )

    def test_hsts_is_only_enabled_by_the_https_template(self):
        http_config = HTTP_CONFIG.read_text(encoding="utf-8")
        https_config = HTTPS_CONFIG.read_text(encoding="utf-8")

        self.assertEqual(_active_header_values(http_config, "Strict-Transport-Security"), [])
        hsts_values = _active_header_values(https_config, "Strict-Transport-Security")
        self.assertGreaterEqual(len(hsts_values), 1)
        self.assertEqual(set(hsts_values), {"max-age=31536000; includeSubDomains"})


class NginxHeaderValidatorMutationTest(unittest.TestCase):
    def setUp(self):
        self.config = HTTPS_CONFIG.read_text(encoding="utf-8")

    def test_commented_server_header_does_not_satisfy_validator(self):
        mutated = self.config.replace(
            '    add_header X-Content-Type-Options "nosniff" always;',
            '    # add_header X-Content-Type-Options "nosniff" always;',
            1,
        )

        with self.assertRaises(AssertionError):
            _assert_required_headers(self, mutated, require_hsts=True)

    def test_duplicate_location_header_is_rejected(self):
        marker = '        add_header Referrer-Policy "strict-origin-when-cross-origin" always;'
        mutated = self.config.replace(marker, f"{marker}\n{marker}", 1)

        with self.assertRaises(AssertionError):
            _assert_required_headers(self, mutated, require_hsts=True)


if __name__ == "__main__":
    unittest.main()
