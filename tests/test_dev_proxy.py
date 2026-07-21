import unittest

from scripts import dev_proxy


class ProxyPrimitiveTests(unittest.TestCase):
    def test_normalize_upstream_accepts_fixed_http_origins(self):
        parsed = dev_proxy.normalize_upstream("http://8.138.143.64")
        self.assertEqual(
            (parsed.scheme, parsed.netloc, parsed.path),
            ("http", "8.138.143.64", ""),
        )

    def test_normalize_upstream_rejects_credentials_paths_and_bad_schemes(self):
        invalid_values = (
            "file:///tmp/site",
            "http://user:pass@example.com",
            "https://example.com/prefix",
            "example.com",
        )
        for value in invalid_values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                dev_proxy.normalize_upstream(value)

    def test_rewrite_set_cookie_for_loopback_http(self):
        value = (
            "hq_session=secret; Domain=example.com; Path=/; Secure; "
            "HttpOnly; SameSite=Strict; Max-Age=3600"
        )

        rewritten = dev_proxy.rewrite_set_cookie(value, local_http=True)

        self.assertNotIn("Domain=", rewritten)
        self.assertNotIn("Secure", rewritten)
        self.assertIn("Path=/", rewritten)
        self.assertIn("HttpOnly", rewritten)
        self.assertIn("SameSite=Strict", rewritten)
        self.assertIn("Max-Age=3600", rewritten)

    def test_rewrite_set_cookie_keeps_secure_outside_loopback_http(self):
        value = "hq_session=secret; Path=/; Secure; HttpOnly"

        rewritten = dev_proxy.rewrite_set_cookie(value, local_http=False)

        self.assertIn("Secure", rewritten)
        self.assertIn("HttpOnly", rewritten)

    def test_safe_request_path_removes_query_and_fragment(self):
        self.assertEqual(
            dev_proxy.safe_request_path("/api/auth/login?token=secret#fragment"),
            "/api/auth/login",
        )

    def test_hop_by_hop_header_names_are_recognized_case_insensitively(self):
        self.assertTrue(dev_proxy.is_hop_by_hop("Connection"))
        self.assertTrue(dev_proxy.is_hop_by_hop("transfer-encoding"))
        self.assertFalse(dev_proxy.is_hop_by_hop("Content-Type"))


if __name__ == "__main__":
    unittest.main()
