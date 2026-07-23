import io
import json
import unittest
import urllib.error
from unittest.mock import Mock, patch

from scripts.domain_access_check import (
    CheckResult,
    Observation,
    classify,
    fetch,
    main,
    run_check,
)


DOMAIN = "huangquechuanmei.com"


class DomainAccessClassificationTest(unittest.TestCase):
    def test_accepts_standard_http_redirect_statuses(self):
        https = Observation(200, f"https://{DOMAIN}/workbench", None, 25)

        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                http = Observation(
                    status,
                    f"http://{DOMAIN}/",
                    f"https://{DOMAIN}/",
                    12,
                )
                self.assertTrue(classify(DOMAIN, http, https).ok)

    def test_detects_dnspod_webblock_redirect(self):
        http = Observation(
            302,
            f"http://{DOMAIN}/",
            f"https://dnspod.qcloud.com/static/webblock.html?d={DOMAIN}",
            10,
        )

        result = classify(DOMAIN, http, None)

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "DNSPOD_WEBBLOCK")

    def test_rejects_non_redirecting_http_status(self):
        http = Observation(200, f"http://{DOMAIN}/", None, 10)

        self.assertEqual(classify(DOMAIN, http, None).code, "HTTP_STATUS")

    def test_rejects_cross_domain_redirects(self):
        http = Observation(
            302,
            f"http://{DOMAIN}/",
            "https://example.com/",
            10,
        )

        self.assertEqual(
            classify(DOMAIN, http, None).code,
            "UNEXPECTED_REDIRECT",
        )

    def test_rejects_bad_https_result(self):
        http = Observation(
            301,
            f"http://{DOMAIN}/",
            f"https://{DOMAIN}/",
            10,
        )

        cases = (
            (Observation(200, "https://example.com/", None, 20), "HTTPS_CROSS_DOMAIN"),
            (Observation(503, f"https://{DOMAIN}/", None, 20), "HTTPS_STATUS"),
        )
        for https, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(classify(DOMAIN, http, https).code, expected)


class DomainAccessFetchTest(unittest.TestCase):
    @patch("scripts.domain_access_check.urllib.request.build_opener")
    def test_closes_response(self, build_opener):
        response = Mock()
        response.getcode.return_value = 200
        response.geturl.return_value = f"https://{DOMAIN}/"
        response.headers.get.return_value = None
        build_opener.return_value.open.return_value = response

        observation = fetch(f"https://{DOMAIN}/", 4.0, True)

        self.assertEqual(observation.status, 200)
        response.close.assert_called_once_with()

    @patch("scripts.domain_access_check.urllib.request.build_opener")
    def test_closes_redirect_error_response(self, build_opener):
        body = io.BytesIO()
        error = urllib.error.HTTPError(
            f"http://{DOMAIN}/",
            307,
            "redirect",
            {"Location": f"https://{DOMAIN}/"},
            body,
        )
        build_opener.return_value.open.side_effect = error

        observation = fetch(f"http://{DOMAIN}/", 4.0, False)

        self.assertEqual(observation.status, 307)
        self.assertTrue(body.closed)


class DomainAccessRunTest(unittest.TestCase):
    def test_checks_http_then_https(self):
        calls = []

        def fake_fetch(url, timeout, follow_redirects):
            calls.append((url, timeout, follow_redirects))
            if url.startswith("http://"):
                return Observation(307, url, f"https://{DOMAIN}/", 10)
            return Observation(200, url, None, 20)

        result = run_check(DOMAIN, 4.0, fake_fetch)

        self.assertTrue(result.ok)
        self.assertEqual(
            calls,
            [
                (f"http://{DOMAIN}/", 4.0, False),
                (f"https://{DOMAIN}/", 4.0, True),
            ],
        )

    def test_converts_transport_exception_to_result(self):
        def failing_fetch(url, timeout, follow_redirects):
            raise TimeoutError("timed out")

        result = run_check(DOMAIN, 4.0, failing_fetch)

        self.assertEqual(result.code, "NETWORK_ERROR")
        self.assertIn("timed out", result.message)

    def test_main_prints_json_and_returns_nonzero_for_failure(self):
        failure = CheckResult(False, "NETWORK_ERROR", "timed out")
        output = io.StringIO()
        with patch(
            "scripts.domain_access_check.run_check",
            return_value=failure,
        ), patch("sys.stdout", output):
            exit_code = main(["--domain", DOMAIN])

        self.assertEqual(exit_code, 1)
        self.assertEqual(json.loads(output.getvalue())["code"], "NETWORK_ERROR")


if __name__ == "__main__":
    unittest.main()
