import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATHS = (
    ROOT / "deploy/test-server/nginx.conf",
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


def _validate_config(config):
    _validate_exact_404s(config)
    _validate_no_broad_regex(config)
    _validate_token_headers(config)


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
