import io
import json
import unittest
from unittest.mock import patch

from scripts.domain_access_check import (
    CheckResult,
    Observation,
    classify,
    main,
    run_check,
)


class DomainAccessClassificationTest(unittest.TestCase):
    def test_accepts_site_owned_http_to_https_redirect(self):
        http = Observation(
            301,
            "http://huangquechuanmei.com/",
            "https://huangquechuanmei.com/",
            12,
        )
        https = Observation(
            200,
            "https://huangquechuanmei.com/workbench/inspiration",
            None,
            25,
        )

        result = classify("huangquechuanmei.com", http, https)

        self.assertEqual(
            result,
            CheckResult(True, "OK", "site reachable", http, https),
        )

    def test_detects_dnspod_webblock_redirect(self):
        http = Observation(
            302,
            "http://huangquechuanmei.com/",
            "https://dnspod.qcloud.com/static/webblock.html?d=huangquechuanmei.com",
            10,
        )

        result = classify("huangquechuanmei.com", http, None)

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "DNSPOD_WEBBLOCK")

    def test_rejects_cross_domain_http_redirect(self):
        http = Observation(
            302,
            "http://huangquechuanmei.com/",
            "https://example.com/",
            10,
        )
        result = classify("huangquechuanmei.com", http, None)
        self.assertEqual(result.code, "UNEXPECTED_REDIRECT")

    def test_rejects_non_redirecting_http_status(self):
        http = Observation(200, "http://huangquechuanmei.com/", None, 10)
        result = classify("huangquechuanmei.com", http, None)
        self.assertEqual(result.code, "HTTP_STATUS")

    def test_rejects_cross_domain_https_result(self):
        http = Observation(
            301,
            "http://huangquechuanmei.com/",
            "https://huangquechuanmei.com/",
            10,
        )
        https = Observation(200, "https://example.com/", None, 20)
        result = classify("huangquechuanmei.com", http, https)
        self.assertEqual(result.code, "HTTPS_CROSS_DOMAIN")

    def test_rejects_non_success_https_status(self):
        http = Observation(
            301,
            "http://huangquechuanmei.com/",
            "https://huangquechuanmei.com/",
            10,
        )
        https = Observation(503, "https://huangquechuanmei.com/", None, 20)
        result = classify("huangquechuanmei.com", http, https)
        self.assertEqual(result.code, "HTTPS_STATUS")


class DomainAccessRunTest(unittest.TestCase):
    def test_calls_http_without_following_then_https_with_following(self):
        calls = []

        def fake_fetch(url, timeout, follow_redirects):
            calls.append((url, timeout, follow_redirects))
            if url.startswith("http://"):
                return Observation(
                    301,
                    url,
                    "https://huangquechuanmei.com/",
                    10,
                )
            return Observation(200, url, None, 20)

        result = run_check("huangquechuanmei.com", 4.0, fake_fetch)

        self.assertTrue(result.ok)
        self.assertEqual(
            calls,
            [
                ("http://huangquechuanmei.com/", 4.0, False),
                ("https://huangquechuanmei.com/", 4.0, True),
            ],
        )

    def test_stops_before_https_when_http_is_webblocked(self):
        calls = []

        def fake_fetch(url, timeout, follow_redirects):
            calls.append(url)
            return Observation(
                302,
                url,
                "https://dnspod.qcloud.com/static/webblock.html?d=huangquechuanmei.com",
                10,
            )

        result = run_check("huangquechuanmei.com", 4.0, fake_fetch)

        self.assertEqual(result.code, "DNSPOD_WEBBLOCK")
        self.assertEqual(calls, ["http://huangquechuanmei.com/"])

    def test_converts_transport_exception_to_result(self):
        def failing_fetch(url, timeout, follow_redirects):
            raise TimeoutError("timed out")

        result = run_check("huangquechuanmei.com", 4.0, failing_fetch)

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "NETWORK_ERROR")
        self.assertIn("timed out", result.message)

    def test_main_prints_json_and_returns_nonzero_for_failure(self):
        failure = CheckResult(False, "NETWORK_ERROR", "timed out")
        output = io.StringIO()
        with patch(
            "scripts.domain_access_check.run_check",
            return_value=failure,
        ):
            with patch("sys.stdout", output):
                exit_code = main(["--domain", "huangquechuanmei.com"])

        self.assertEqual(exit_code, 1)
        self.assertEqual(json.loads(output.getvalue())["code"], "NETWORK_ERROR")


if __name__ == "__main__":
    unittest.main()
