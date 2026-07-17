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
ADD_HEADER_DIRECTIVE = re.compile(
    r'^[ \t]*add_header\s+'
    r'(?P<name>[^\s;]+)\s+'
    r'(?P<value>"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[^\s;]+)'
    r'(?P<tail>(?:\s+[^\s;]+)*)\s*;',
    flags=re.MULTILINE,
)
SERVER_DECLARATION = re.compile(r"^[ \t]*server\s*\{", flags=re.MULTILINE)
HSTS_VALUE = "max-age=31536000; includeSubDomains"


def _active_header_directives(config, name):
    directives = []
    for match in ADD_HEADER_DIRECTIVE.finditer(_strip_comments(config)):
        if match.group("name").casefold() != name.casefold():
            continue
        value = match.group("value")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        tail = match.group("tail").split()
        directives.append((value, tail == ["always"]))
    return directives


def _active_header_values(config, name):
    return [value for value, _ in _active_header_directives(config, name)]


def _active_server_blocks(config):
    active_config = _strip_comments(config)
    servers = []
    for server in SERVER_DECLARATION.finditer(active_config):
        opening_brace = active_config.find("{", server.start(), server.end())
        servers.append(active_config[server.start():_block_end(active_config, opening_brace)])
    if not servers:
        raise AssertionError("missing active server block")
    return servers


def _server_without_locations(server_block):
    locations = _active_locations(server_block)
    for _, _, start, block in reversed(locations):
        server_block = server_block[:start] + server_block[start + len(block):]
    return server_block


def _first_server_without_locations(config):
    return _server_without_locations(_active_server_blocks(config)[0])


def _server_listens_with_ssl(server_block):
    direct_server = _server_without_locations(server_block)
    listen_arguments = re.findall(
        r"^[ \t]*listen\s+([^;]+);",
        direct_server,
        flags=re.MULTILINE,
    )
    return any("ssl" in arguments.split() for arguments in listen_arguments)


def _assert_hsts_server_contexts(test_case, config, require_ssl_server):
    ssl_servers = 0
    for index, server_block in enumerate(_active_server_blocks(config), start=1):
        direct_server = _server_without_locations(server_block)
        direct_hsts = _active_header_directives(direct_server, "Strict-Transport-Security")
        all_hsts = _active_header_directives(server_block, "Strict-Transport-Security")
        is_ssl = _server_listens_with_ssl(server_block)
        if is_ssl:
            ssl_servers += 1
            test_case.assertEqual(
                direct_hsts,
                [(HSTS_VALUE, True)],
                f"SSL server {index} must set one correct HSTS header",
            )
            for _, path, _, block in _active_locations(server_block):
                if re.search(r"^[ \t]*add_header\s+", block, flags=re.MULTILINE):
                    test_case.assertEqual(
                        _active_header_directives(block, "Strict-Transport-Security"),
                        [(HSTS_VALUE, True)],
                        f"{path} replaces inherited headers and must retain HSTS",
                    )
        else:
            test_case.assertEqual(
                all_hsts,
                [],
                f"non-SSL server {index} must not set HSTS",
            )
    if require_ssl_server:
        test_case.assertGreaterEqual(ssl_servers, 1, "HTTPS template must contain an SSL server")


def _assert_required_headers(test_case, config, require_hsts=False):
    server_block = _first_server_without_locations(config)
    for name, value in REQUIRED_HEADERS.items():
        test_case.assertEqual(
            _active_header_directives(server_block, name),
            [(value, True)],
            name,
        )

    server_policies = _active_header_directives(server_block, "Content-Security-Policy")
    test_case.assertEqual(len(server_policies), 1, "server must set CSP exactly once")
    test_case.assertTrue(server_policies[0][1], "server CSP must use always")
    test_case.assertIn("frame-ancestors 'self'", server_policies[0][0])

    if require_hsts:
        test_case.assertEqual(
            _active_header_directives(server_block, "Strict-Transport-Security"),
            [(HSTS_VALUE, True)],
        )

    for _, path, _, block in _active_locations(config):
        if re.search(r"^[ \t]*add_header\s+", block, flags=re.MULTILINE):
            for name, value in REQUIRED_HEADERS.items():
                test_case.assertEqual(
                    _active_header_directives(block, name),
                    [(value, True)],
                    f"{path} replaces inherited headers and must retain {name}",
                )
            policies = _active_header_directives(block, "Content-Security-Policy")
            test_case.assertEqual(len(policies), 1, f"{path} must retain CSP")
            test_case.assertTrue(policies[0][1], f"{path} CSP must use always")
            test_case.assertIn("frame-ancestors 'self'", policies[0][0])
    _assert_hsts_server_contexts(test_case, config, require_ssl_server=require_hsts)


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
        self.assertEqual(set(hsts_values), {HSTS_VALUE})
        _assert_hsts_server_contexts(self, http_config, require_ssl_server=False)
        _assert_hsts_server_contexts(self, https_config, require_ssl_server=True)


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

    def test_unquoted_duplicate_nosniff_is_rejected(self):
        marker = '        add_header X-Content-Type-Options "nosniff" always;'
        mutated = self.config.replace(
            marker,
            f"{marker}\n        add_header X-Content-Type-Options nosniff always;",
            1,
        )

        with self.assertRaises(AssertionError):
            _assert_required_headers(self, mutated, require_hsts=True)

    def test_conflicting_quoted_referrer_policy_is_rejected(self):
        marker = '        add_header Referrer-Policy "strict-origin-when-cross-origin" always;'
        mutated = self.config.replace(
            marker,
            f'{marker}\n        add_header Referrer-Policy "no-referrer" always;',
            1,
        )

        with self.assertRaises(AssertionError):
            _assert_required_headers(self, mutated, require_hsts=True)

    def test_duplicate_without_always_is_rejected(self):
        marker = '        add_header X-Content-Type-Options "nosniff" always;'
        mutated = self.config.replace(
            marker,
            f'{marker}\n        add_header X-Content-Type-Options "nosniff";',
            1,
        )

        with self.assertRaises(AssertionError):
            _assert_required_headers(self, mutated, require_hsts=True)

    def test_multiline_duplicate_and_conflicting_headers_are_rejected(self):
        marker = '        add_header Referrer-Policy "strict-origin-when-cross-origin" always;'
        for directive in (
            'add_header\n            Referrer-Policy\n            "strict-origin-when-cross-origin"\n            always;',
            'add_header\n            Referrer-Policy\n            "no-referrer"\n            always;',
        ):
            with self.subTest(directive=directive):
                mutated = self.config.replace(marker, f"{marker}\n        {directive}", 1)
                with self.assertRaises(AssertionError):
                    _assert_required_headers(self, mutated, require_hsts=True)

    def test_multiline_duplicate_without_always_is_rejected(self):
        marker = '        add_header X-Content-Type-Options "nosniff" always;'
        directive = 'add_header\n            X-Content-Type-Options\n            "nosniff";'
        mutated = self.config.replace(marker, f"{marker}\n        {directive}", 1)

        with self.assertRaises(AssertionError):
            _assert_required_headers(self, mutated, require_hsts=True)

    def test_lowercase_duplicate_target_header_is_rejected(self):
        marker = '        add_header X-Content-Type-Options "nosniff" always;'
        mutated = self.config.replace(
            marker,
            f'{marker}\n        add_header x-content-type-options "nosniff" always;',
            1,
        )

        with self.assertRaises(AssertionError):
            _assert_required_headers(self, mutated, require_hsts=True)

    def test_hsts_in_non_ssl_redirect_server_is_rejected(self):
        marker = "    listen 80;"
        mutated = self.config.replace(
            marker,
            '    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;\n'
            f"{marker}",
            1,
        )

        with self.assertRaises(AssertionError):
            _assert_required_headers(self, mutated, require_hsts=True)


if __name__ == "__main__":
    unittest.main()
