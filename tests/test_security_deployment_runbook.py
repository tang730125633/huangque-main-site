import ipaddress
import re
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERIFY_SCRIPT = ROOT / "deploy/test-server/verify-full-environment.sh"
RUNBOOK = ROOT / "docs/security/test-server-security-runbook.md"
SHELL_HARNESS = ROOT / "tests/test_verify_full_environment.sh"

INTERNAL_PATHS = (
    "/api/auth/points/deduct",
    "/api/auth/points/refund",
    "/api/auth/admin/points/adjust",
    "/api/auth/admin/points/audit",
    "/api/auth/admin/users",
    "/api/auth/admin/recharge/review",
    "/api/auth/admin/recharge/orders",
)
SECURITY_HEADERS = (
    "Strict-Transport-Security",
    "X-Content-Type-Options",
    "Referrer-Policy",
    "Permissions-Policy",
    "Content-Security-Policy",
)


class FullEnvironmentVerificationScriptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = VERIFY_SCRIPT.read_text(encoding="utf-8")

    def test_declares_required_runtime_inputs_and_cleanup_trap(self):
        self.assertRegex(self.script, r'PUBLIC_BASE_URL[}:?=]')
        self.assertIn("AUTH_INTERNAL_URL", self.script)
        self.assertIn("http://127.0.0.1:8095", self.script)
        self.assertRegex(self.script, r'ADMIN_ALLOWED_SOURCE[}:?=]')
        self.assertRegex(self.script, r"mktemp")
        self.assertRegex(self.script, r"trap\s+[^\n]*EXIT")

    def test_has_only_the_three_documented_runtime_inputs(self):
        for forbidden in (
            "ADMIN_ALLOWLIST_FILE",
            "CONTENT_INTERNAL_URL",
            "IMGGEN_INTERNAL_URL",
            "LEADGEN_INTERNAL_URL",
            "DL_INTERNAL_URL",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.script)
        self.assertIn(
            "/etc/nginx/snippets/huangque-admin-allowlist.conf", self.script
        )
        for endpoint in (
            "http://127.0.0.1:8096/api/gen/health",
            "http://127.0.0.1:8101/api/gen/banana/health",
            "http://127.0.0.1:8100/api/gen/leadgen/health",
            "http://127.0.0.1:8097/api/gen/dl/health",
        ):
            with self.subTest(endpoint=endpoint):
                self.assertIn(endpoint, self.script)

    def test_validates_public_origin_and_exact_internal_auth_url(self):
        body = self._function_body("validate_inputs")
        self.assertIn("PUBLIC_BASE_URL", body)
        self.assertIn("https?", body)
        for rejected_component in ("credentials", "path", "query", "fragment", "whitespace"):
            with self.subTest(component=rejected_component):
                self.assertIn(rejected_component, body.lower())
        self.assertIn('AUTH_INTERNAL_URL" != "http://127.0.0.1:8095', body)

    def test_checks_core_health_and_public_login(self):
        for path in (
            "/api/auth/health",
            "/api/gen/health",
            "/api/gen/banana/health",
            "/api/gen/leadgen/health",
            "/api/gen/dl/health",
            "/login.html",
        ):
            with self.subTest(path=path):
                self.assertIn(path, self.script)
        self.assertIn("--fail-with-body", self.script)

    def test_success_helper_requires_2xx_without_following_redirects(self):
        body = self._function_body("fetch_success")
        self.assertIn("--write-out", body)
        self.assertRegex(body, r"2\?\?|\[2[0-9][0-9]\]")
        self.assertNotIn("--location", body)
        self.assertNotIn("--location-trusted", body)
        self.assertIn("--output", body)
        self.assertIn("--dump-header", body)

    def test_every_curl_probe_has_timeouts_and_url_option_terminator(self):
        normalized = self.script.replace("\\\n", " ")
        commands = [
            match.group(0)
            for match in re.finditer(r"\bcurl\b[^\n]*", normalized)
        ]
        self.assertGreaterEqual(len(commands), 3)
        for command in commands:
            with self.subTest(command=command):
                self.assertIn("--connect-timeout", command)
                self.assertIn("--max-time", command)
                self.assertRegex(
                    command,
                    r"\s--\s+(?:\"\$url\"|https://api\.openai\.com/v1/models)",
                )

    def test_checks_every_internal_path_with_and_without_spoofed_token(self):
        for path in INTERNAL_PATHS:
            with self.subTest(path=path):
                self.assertIn(path, self.script)
        self.assertIn("X-HQ-Internal-Token", self.script)
        self.assertRegex(self.script, r'(?:expected_status|expect_status)[\s\S]*404')

    def test_checks_security_headers_and_non_allowlisted_admin_access(self):
        for header in SECURITY_HEADERS:
            with self.subTest(header=header):
                self.assertIn(header, self.script)
        self.assertRegex(self.script, r'(?:expected_status|expect_status)[\s\S]*403')
        self.assertIn("/admin/", self.script)
        self.assertIn("/api/admin/", self.script)

    def test_verifies_the_deployed_allowlist_exactly(self):
        self.assertIn("huangque-admin-allowlist.conf", self.script)
        self.assertIn("ADMIN_ALLOWED_SOURCE", self.script)
        self.assertIn('awk -v expected="$ADMIN_ALLOWED_SOURCE"', self.script)
        self.assertIn('$1 == "allow" && $2 == expected ";"', self.script)
        self.assertIn("deny all", self.script)

    def test_expected_error_status_helper_does_not_use_fail_with_body(self):
        body = self._function_body("expect_status")
        self.assertNotIn("--fail-with-body", body)
        self.assertIn("--write-out", body)

    def test_does_not_print_secret_bearing_values(self):
        normalized = self.script.replace("\\\n", " ")
        self.assertNotRegex(self.script, r"(?m)^\s*set\s+-[^\n]*x")
        self.assertNotRegex(normalized, r"\bcurl\b[^\n]*(?:--verbose|-v\b|--trace)")
        self.assertNotRegex(
            normalized,
            r"(?m)^\s*(?:cat|tee)\b[^\n]*(?:TEMP_DIR|body|header)",
        )
        output_lines = [
            line
            for line in self.script.splitlines()
            if re.search(r"\b(?:echo|printf)\b", line)
        ]
        for line in output_lines:
            with self.subTest(line=line):
                self.assertNotRegex(
                    line,
                    r"(?i)\$(?:\{)?[A-Z0-9_]*(?:PASSWORD|TOKEN|SECRET|KEY|COOKIE|CSRF)[A-Z0-9_]*",
                )
                self.assertNotRegex(line, r"\$(?:\{)?(?:TEMP_DIR|body_file|header_file)")

    def test_linux_fake_curl_harness_is_executable_and_covers_security_cases(self):
        harness = SHELL_HARNESS.read_text(encoding="utf-8")
        self.assertTrue(harness.startswith("#!/usr/bin/env bash\n"))
        for marker in (
            "redirect",
            "2xx",
            "paired",
            "cleanup",
            "super-secret-body",
            "session-secret",
            "csrf-secret",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, harness)
        index = subprocess.run(
            ["git", "ls-files", "--stage", "--", str(SHELL_HARNESS.relative_to(ROOT))],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertTrue(index.startswith("100755 "), "harness must be executable in Git")

    @classmethod
    def _function_body(cls, name):
        match = re.search(
            rf"{re.escape(name)}\(\)\s*\{{(?P<body>.*?)^\}}",
            cls.script,
            flags=re.MULTILINE | re.DOTALL,
        )
        if match is None:
            raise AssertionError(f"{name} helper is missing")
        return match.group("body")


class SecurityDeploymentRunbookTest(unittest.TestCase):
    def test_runbook_uses_the_implemented_admin_points_limit_variable(self):
        text = RUNBOOK.read_text(encoding="utf-8")
        self.assertIn("HQ_ADMIN_POINTS_MAX_DELTA", text)
        self.assertNotIn("HQ_ADMIN_ADJUST_MAX_ABS", text)

    @classmethod
    def setUpClass(cls):
        cls.text = RUNBOOK.read_text(encoding="utf-8")

    def test_covers_predeployment_backup_and_configuration(self):
        for requirement in (
            "数据库",
            "Nginx",
            "systemd",
            "环境文件",
            "HQ_CSRF_SECRET",
            "HQ_ALLOWED_ORIGINS",
            "HQ_ADMIN_POINTS_MAX_DELTA",
            "白名单",
            "ADMIN_ALLOWED_SOURCE",
            "非白名单来源",
        ):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, self.text)

    def test_requires_safe_deployment_order_and_smoke_monitoring(self):
        code = self.text.index("部署代码")
        frontend = self.text.index("部署前端")
        nginx = self.text.index("nginx -t")
        reload_nginx = self.text.index("reload Nginx")
        self.assertLess(code, nginx)
        self.assertLess(frontend, nginx)
        self.assertLess(nginx, reload_nginx)
        for requirement in ("登录", "余额", "生成", "401", "403", "415", "5xx"):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, self.text)

    def test_documents_exact_rollback_order_and_no_schema_rollback(self):
        rollback = self.text.index("## 回滚")
        text = self.text[rollback:]
        restore_nginx = text.index("恢复 Nginx")
        reload_nginx = text.index("reload Nginx")
        previous_sha = text.index("上一代码 SHA")
        restart_services = text.index("重启 auth/admin")
        final_smoke = text.index("登录、余额和生成冒烟")
        self.assertLess(restore_nginx, reload_nginx)
        self.assertLess(reload_nginx, previous_sha)
        self.assertLess(previous_sha, restart_services)
        self.assertLess(restart_services, final_smoke)
        self.assertIn("不执行数据库 schema 回滚", text)

    def test_uses_only_documentation_addresses_and_no_credentials(self):
        addresses = re.findall(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?", self.text)
        documentation_networks = (
            ipaddress.ip_network("192.0.2.0/24"),
            ipaddress.ip_network("198.51.100.0/24"),
            ipaddress.ip_network("203.0.113.0/24"),
        )
        for address in addresses:
            parsed = ipaddress.ip_interface(address).ip
            with self.subTest(address=address):
                self.assertTrue(
                    parsed.is_loopback
                    or any(parsed in network for network in documentation_networks)
                )
        self.assertNotRegex(self.text, r"(?m)^\s*allow\s+\d")
        self.assertNotRegex(self.text, r"(?i)(?:password|token|secret)\s*[:=]\s*\S+")

    def test_documents_strict_inputs_negative_source_and_timeouts(self):
        for requirement in (
            "仅接受",
            "HTTP(S) origin",
            "非白名单来源",
            "不跟随重定向",
            "连接超时",
            "总超时",
        ):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, self.text)


if __name__ == "__main__":
    unittest.main()
