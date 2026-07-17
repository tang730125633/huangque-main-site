import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERIFY_SCRIPT = ROOT / "deploy/test-server/verify-full-environment.sh"
RUNBOOK = ROOT / "docs/security/test-server-security-runbook.md"

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
        match = re.search(
            r"(?:expected_status|expect_status)\(\)\s*\{(?P<body>.*?)^\}",
            self.script,
            flags=re.MULTILINE | re.DOTALL,
        )
        self.assertIsNotNone(match, "expected-status helper is missing")
        self.assertNotIn("--fail-with-body", match.group("body"))
        self.assertIn("--write-out", match.group("body"))

    def test_does_not_print_secret_bearing_values(self):
        forbidden_expansions = (
            "${COOKIE",
            "$COOKIE",
            "${CSRF",
            "$CSRF",
            "${HQ_INTERNAL_TOKEN",
            "$HQ_INTERNAL_TOKEN",
            "${OPENAI_API_KEY",
            "$OPENAI_API_KEY",
            "${GEMINI_API_KEY",
            "$GEMINI_API_KEY",
        )
        output_lines = [
            line
            for line in self.script.splitlines()
            if re.search(r"\b(?:echo|printf)\b", line)
        ]
        for line in output_lines:
            with self.subTest(line=line):
                self.assertFalse(any(value in line for value in forbidden_expansions))


class SecurityDeploymentRunbookTest(unittest.TestCase):
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
            "HQ_ADMIN_ADJUST_MAX_ABS",
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
        self.assertNotRegex(
            self.text,
            r"(?<![\d.])(?:8\.138\.143\.64|129\.204\.166\.13)(?![\d.])",
        )
        self.assertNotRegex(self.text, r"(?i)(?:password|token|secret)\s*[:=]\s*\S+")


if __name__ == "__main__":
    unittest.main()
