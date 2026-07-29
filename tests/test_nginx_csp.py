import re
import unittest
from pathlib import Path


class NginxCspTest(unittest.TestCase):
    CONFIGS = (
        "deploy/nginx-huangquechuanmei.conf",
        "server/nginx-huangquechuanmei.conf",
    )

    def _config(self, relative_path):
        return (Path(__file__).parents[1] / relative_path).read_text(encoding="utf-8")

    def test_csp_is_active_and_consistent(self):
        for relative_path in self.CONFIGS:
            with self.subTest(config=relative_path):
                config = self._config(relative_path)
                policies = re.findall(
                    r'add_header Content-Security-Policy "([^"]+)" always;',
                    config,
                )

                self.assertEqual(len(policies), 4)
                self.assertTrue(all(policy == policies[0] for policy in policies))
                for directive in (
                    "base-uri 'self'",
                    "object-src 'none'",
                    "frame-ancestors 'none'",
                    "script-src 'self' 'unsafe-inline' https://unpkg.com",
                    "style-src 'self' 'unsafe-inline' https://unpkg.com",
                ):
                    self.assertIn(directive, policies[0])

    def test_security_headers_cover_server_and_header_overrides(self):
        expected = (
            'add_header Strict-Transport-Security "max-age=31536000" always;',
            'add_header X-Frame-Options "DENY" always;',
            'add_header X-Content-Type-Options "nosniff" always;',
            'add_header Referrer-Policy "strict-origin-when-cross-origin" always;',
        )
        for relative_path in self.CONFIGS:
            config = self._config(relative_path)
            with self.subTest(config=relative_path):
                self.assertEqual(config.count("server_tokens off;"), 2)
                for header in expected:
                    self.assertEqual(config.count(header), 4, header)

    def test_workbench_ip12_uses_the_existing_native_product(self):
        config = self._config("deploy/nginx-huangquechuanmei.conf")
        self.assertNotIn("location ^~ /workbench/ip12/", config)
        self.assertNotIn("X-Forwarded-Prefix /workbench/ip12", config)
        self.assertIn(
            "location = /workbench/ip12/ { try_files /workbench/ip12.html =404; }",
            config,
        )
        self.assertIn("try_files $uri $uri.html $uri/index.html =404;", config)

        page = self._config("site/workbench/ip12.html")
        self.assertIn('<base href="/workbench/">', page)
        self.assertIn("完成模块 1–4 后生成阶段报告", page)
        self.assertIn("download>下载 PDF</a>", page)

    def test_direct_3101_gateway_uses_the_same_flask_service(self):
        config = self._config("deploy/nginx-hermes-ip12-direct.conf")
        self.assertIn("listen 3101;", config)
        self.assertIn("proxy_pass http://127.0.0.1:3102;", config)
        self.assertIn("client_max_body_size 200m;", config)

    def test_hermes_runbook_updates_the_actively_loaded_main_site_config(self):
        runbook = self._config("deploy/生产环境清单与还原手册.md")
        active = "/etc/nginx/sites-enabled/huangquechuanmei"
        self.assertIn(f"backup_file {active} nginx-huangquechuanmei-enabled.conf", runbook)
        self.assertIn(
            f'sudo install -m 0644 "$HERMES_RELEASE_DIR/deploy/nginx-huangquechuanmei.conf" {active}',
            runbook,
        )
        self.assertIn(
            f'restore_file "$backup/nginx-huangquechuanmei-enabled.conf" '
            f'"$backup/nginx-huangquechuanmei-enabled.conf.state" {active}',
            runbook,
        )


if __name__ == "__main__":
    unittest.main()
