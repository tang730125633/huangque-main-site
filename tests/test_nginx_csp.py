import re
import unittest
from pathlib import Path


class NginxCspTest(unittest.TestCase):
    def test_csp_is_active_and_consistent(self):
        config = (Path(__file__).parents[1] / "deploy/nginx-huangquechuanmei.conf").read_text()
        policies = re.findall(r'add_header Content-Security-Policy "([^"]+)" always;', config)

        self.assertEqual(len(policies), 2)
        self.assertEqual(policies[0], policies[1])
        for directive in (
            "base-uri 'self'",
            "object-src 'none'",
            "script-src 'self' 'unsafe-inline' https://unpkg.com",
            "style-src 'self' 'unsafe-inline' https://unpkg.com",
        ):
            self.assertIn(directive, policies[0])


if __name__ == "__main__":
    unittest.main()
