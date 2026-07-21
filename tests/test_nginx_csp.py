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


if __name__ == "__main__":
    unittest.main()
