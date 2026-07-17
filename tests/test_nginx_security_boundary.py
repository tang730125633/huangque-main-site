import ipaddress
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATHS = (
    ROOT / "deploy/nginx-huangquechuanmei.conf",
)
ADMIN_ALLOWLIST = "/etc/nginx/snippets/huangque-admin-allowlist.conf"
ADMIN_ALLOWLIST_EXAMPLE = ROOT / "deploy/admin-allowlist.conf.example"
DOCUMENTATION_NETWORKS = {
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("2001:db8::/32"),
}
INTERNAL_AUTH_PATHS = (
    "/api/auth/points/deduct",
    "/api/auth/points/refund",
    "/api/auth/admin/points/adjust",
    "/api/auth/admin/points/audit",
    "/api/auth/admin/users",
    "/api/auth/admin/recharge/review",
    "/api/auth/admin/recharge/orders",
)
LOCATION_DECLARATION = re.compile(
    r"^[ \t]*location[ \t]+(?:(=|\^~|~\*?|@)[ \t]+)?([^\n{]+?)[ \t]*\{",
    flags=re.MULTILINE,
)
ALLOWED_UNRELATED_REGEXES = {
    r"^/(.+)\.html$",
    r"\.(webp|png|jpe?g|gif|mp4|css|js|json|svg|ico|woff2?)$",
}
AUTH_ROUTE_PROBES = (
    "/api/auth",
    "/api/auth/",
    "/api/auth/login",
    "/api/auth/example.html",
    "/api/auth/example.js",
    *INTERNAL_AUTH_PATHS,
)


def _strip_comments(config):
    active_lines = []
    for line in config.splitlines(keepends=True):
        quote = None
        escaped = False
        active = []
        for character in line:
            if escaped:
                active.append(character)
                escaped = False
            elif character == "\\":
                active.append(character)
                escaped = True
            elif quote is not None:
                active.append(character)
                if character == quote:
                    quote = None
            elif character in ('"', "'"):
                active.append(character)
                quote = character
            elif character == "#":
                if line.endswith("\n"):
                    active.append("\n")
                break
            else:
                active.append(character)
        active_lines.append("".join(active))
    return "".join(active_lines)


def _block_end(config, opening_brace):
    depth = 0
    quote = None
    escaped = False
    for index in range(opening_brace, len(config)):
        character = config[index]
        if escaped:
            escaped = False
        elif character == "\\":
            escaped = True
        elif quote is not None:
            if character == quote:
                quote = None
        elif character in ('"', "'"):
            quote = character
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return index + 1
    raise AssertionError("unterminated location block")


def _active_locations(config):
    active_config = _strip_comments(config)
    locations = []
    for match in LOCATION_DECLARATION.finditer(active_config):
        modifier = match.group(1) or ""
        pattern = " ".join(match.group(2).split())
        opening_brace = active_config.find("{", match.start(), match.end())
        end = _block_end(active_config, opening_brace)
        locations.append((modifier, pattern, match.start(), active_config[match.start():end]))
    return locations


def _locations_for(locations, modifier, path):
    return [location for location in locations if location[:2] == (modifier, path)]


def _unique_public_proxy(locations, path):
    matching_locations = [location for location in locations if location[1] == path]
    if len(matching_locations) != 1:
        raise AssertionError(
            f"expected one active location for {path}, found {len(matching_locations)}"
        )
    if matching_locations[0][0] != "^~":
        raise AssertionError(f"public proxy for {path} must use ^~")
    return matching_locations[0]


def _validate_exact_404s(config):
    locations = _active_locations(config)
    public_auth = _unique_public_proxy(locations, "/api/auth/")

    for path in INTERNAL_AUTH_PATHS:
        exact_locations = _locations_for(locations, "=", path)
        if len(exact_locations) != 1:
            raise AssertionError(f"expected one exact location for {path}, found {len(exact_locations)}")
        exact_location = exact_locations[0]
        if re.search(
            r"(?:^|\{)[ \t]*return[ \t]+404[ \t]*;",
            exact_location[3],
            re.MULTILINE,
        ) is None:
            raise AssertionError(f"missing return 404 for {path}")
        if exact_location[2] >= public_auth[2]:
            raise AssertionError(f"{path} must precede public auth proxy")


def _validate_no_broad_regex(config):
    for modifier, pattern, _, _ in _active_locations(config):
        if modifier not in ("~", "~*") or pattern in ALLOWED_UNRELATED_REGEXES:
            continue
        if "api" in pattern.lower():
            raise AssertionError(f"regex location may target API routes: {pattern}")
        try:
            compiled = re.compile(pattern, re.IGNORECASE if modifier == "~*" else 0)
        except re.error as error:
            raise AssertionError(f"cannot safely evaluate regex location {pattern}: {error}") from error
        if any(compiled.search(route) for route in AUTH_ROUTE_PROBES):
            raise AssertionError(f"regex location overlaps auth routes: {pattern}")


def _validate_token_headers(config):
    locations = _active_locations(config)
    for path in ("/api/auth/", "/api/admin/"):
        proxy = _unique_public_proxy(locations, path)
        active_headers = re.findall(
            r'^[ \t]*proxy_set_header[ \t]+X-HQ-Internal-Token[ \t]+""[ \t]*;',
            proxy[3],
            flags=re.MULTILINE,
        )
        if len(active_headers) != 1:
            raise AssertionError(f"{path} does not clear internal token")


def _validate_admin_allowlist(config):
    locations = _active_locations(config)
    for path in ("/admin/", "/api/admin/"):
        location = _unique_public_proxy(locations, path)
        includes = re.findall(
            rf"^[ \t]*include[ \t]+{re.escape(ADMIN_ALLOWLIST)}[ \t]*;",
            location[3],
            flags=re.MULTILINE,
        )
        if len(includes) != 1:
            raise AssertionError(
                f"{path} must include the admin allowlist exactly once"
            )


def _validate_allowlist_example(config):
    active_config = _strip_comments(config)
    directives = [
        " ".join(match.group(1).split())
        for match in re.finditer(r"^[ \t]*(allow\s+[^;]+|deny\s+all)[ \t]*;", active_config, re.MULTILINE)
    ]
    if not directives or not any(directive.startswith("allow ") for directive in directives):
        raise AssertionError("allowlist example must document at least one CIDR")
    for directive in directives[:-1]:
        if not directive.startswith("allow "):
            raise AssertionError("only allow CIDRs may precede deny all")
        try:
            network = ipaddress.ip_network(directive.removeprefix("allow "), strict=True)
        except ValueError as error:
            raise AssertionError("allowlist example entries must be CIDRs") from error
        if network not in DOCUMENTATION_NETWORKS:
            raise AssertionError("allowlist example must only use documentation CIDRs")
    if directives[-1] != "deny all":
        raise AssertionError("allowlist example must end with an active deny all")


def _validate_config(config):
    _validate_exact_404s(config)
    _validate_no_broad_regex(config)
    _validate_token_headers(config)
    _validate_admin_allowlist(config)


class NginxSecurityBoundaryTest(unittest.TestCase):
    def test_internal_auth_routes_are_exact_404s_before_public_auth_proxy(self):
        for config_path in CONFIG_PATHS:
            with self.subTest(config=config_path.name):
                config = config_path.read_text(encoding="utf-8")
                _validate_exact_404s(config)

    def test_internal_auth_routes_are_not_hidden_with_a_broad_regex(self):
        for config_path in CONFIG_PATHS:
            with self.subTest(config=config_path.name):
                config = config_path.read_text(encoding="utf-8")
                _validate_no_broad_regex(config)

    def test_public_auth_and_admin_proxies_clear_internal_token(self):
        for config_path in CONFIG_PATHS:
            with self.subTest(config=config_path.name):
                config = config_path.read_text(encoding="utf-8")
                _validate_token_headers(config)

    def test_admin_page_and_api_use_the_same_allowlist(self):
        for config_path in CONFIG_PATHS:
            with self.subTest(config=config_path.name):
                _validate_admin_allowlist(config_path.read_text(encoding="utf-8"))

    def test_allowlist_example_ends_with_deny_all(self):
        self.assertTrue(ADMIN_ALLOWLIST_EXAMPLE.is_file(), "allowlist example is missing")
        _validate_allowlist_example(ADMIN_ALLOWLIST_EXAMPLE.read_text(encoding="utf-8"))


class NginxSecurityBoundaryValidatorMutationTest(unittest.TestCase):
    def setUp(self):
        self.config = CONFIG_PATHS[0].read_text(encoding="utf-8")

    def _insert_before_public_auth(self, directive):
        marker = "    location ^~ /api/auth/ {"
        return self.config.replace(marker, f"    {directive}\n\n{marker}", 1)

    def test_commented_rules_do_not_satisfy_boundary(self):
        mutated = self.config
        for path in INTERNAL_AUTH_PATHS:
            mutated = mutated.replace(
                f"    location = {path}",
                f"    # location = {path}",
                1,
            )

        with self.assertRaises(AssertionError):
            _validate_config(mutated)

    def test_commented_headers_do_not_satisfy_boundary(self):
        mutated = self.config
        mutated = mutated.replace(
            '        proxy_set_header X-HQ-Internal-Token "";',
            '        # proxy_set_header X-HQ-Internal-Token "";',
        )

        with self.assertRaises(AssertionError):
            _validate_config(mutated)

    def test_commented_allowlist_include_does_not_satisfy_boundary(self):
        mutated = self.config.replace(
            f"include {ADMIN_ALLOWLIST};",
            f"# include {ADMIN_ALLOWLIST};",
            1,
        )

        with self.assertRaises(AssertionError):
            _validate_config(mutated)

    def test_duplicate_admin_page_location_is_rejected(self):
        mutated = self._insert_before_public_auth(
            f"location /admin/ {{ include {ADMIN_ALLOWLIST}; }}"
        )

        with self.assertRaises(AssertionError):
            _validate_config(mutated)

    def test_alternate_whitespace_duplicate_public_auth_proxy_is_rejected(self):
        mutated = self._insert_before_public_auth(
            'location   ^~   /api/auth/ { proxy_set_header X-HQ-Internal-Token ""; }'
        )

        with self.assertRaises(AssertionError):
            _validate_config(mutated)

    def test_plain_prefix_duplicates_of_public_proxies_are_rejected(self):
        for path in ("/api/auth/", "/api/admin/"):
            directive = (
                f'location {path} {{ proxy_set_header X-HQ-Internal-Token ""; }}'
            )
            with self.subTest(path=path):
                with self.assertRaises(AssertionError):
                    _validate_config(self._insert_before_public_auth(directive))

    def test_alternate_whitespace_duplicate_is_rejected(self):
        mutated = self._insert_before_public_auth(
            "location   =   /api/auth/points/deduct { return 404; }"
        )

        with self.assertRaises(AssertionError):
            _validate_config(mutated)

    def test_api_regexes_that_can_cover_auth_are_rejected(self):
        for directive in (
            "location ~ ^/api/ { return 404; }",
            "location ~ ^/api/(auth|public)/ { return 404; }",
        ):
            with self.subTest(directive=directive):
                with self.assertRaises(AssertionError):
                    _validate_config(self._insert_before_public_auth(directive))


if __name__ == "__main__":
    unittest.main()
