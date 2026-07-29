import io
import json
import unittest
from unittest.mock import patch

from hq_cli import cli


class HqCliTests(unittest.TestCase):
    def invoke(self, argv, stdin=b""):
        stdout, stderr = io.StringIO(), io.StringIO()
        input_stream = type("Input", (), {"buffer": io.BytesIO(stdin)})()
        with patch("sys.stdout", stdout), patch("sys.stderr", stderr), patch("sys.stdin", input_stream):
            code = cli.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def payload(self, output):
        return json.loads(output)

    def test_no_argument_and_help_are_successful_json(self):
        for argv in ([], ["help"], ["-h"], ["version", "--help"], ["--help", "capabilities"], ["capabilities", "--json"]):
            code, output, error = self.invoke(argv)
            self.assertEqual(0, code, error)
            self.assertTrue(self.payload(output)["schema"].startswith("hq."))
            self.assertIn("cli_version", self.payload(output))

    def test_capabilities_are_complete_and_discoverable(self):
        code, output, error = self.invoke(["capabilities", "--json"])
        self.assertEqual(0, code, error)
        payload = self.payload(output)
        self.assertEqual("hq.capabilities/v1", payload["schema"])
        image = next(item for item in payload["capabilities"] if item["id"] == "image")
        self.assertEqual("/workbench/banana", image["deep_link"]["path"])
        self.assertFalse(image["requires_auth"])
        self.assertEqual("account_for_actions", image["target_auth"])
        self.assertEqual("navigation", image["side_effect"])
        self.assertTrue(all(key in image for key in ("input_schema", "output_schema", "requires_auth", "side_effect", "confirmation_required", "cost", "availability", "runnable")))
        ip12 = next(item for item in payload["capabilities"] if item["id"] == "ip12")
        self.assertEqual("planned_auth", ip12["availability"])
        self.assertFalse(ip12["runnable"])

    def test_run_defaults_to_empty_input_and_uses_extensionless_path(self):
        code, output, error = self.invoke(["run", "script", "--json"])
        self.assertEqual(0, code, error)
        payload = self.payload(output)
        self.assertEqual("hq.run/v1", payload["schema"])
        self.assertEqual("https://huangquechuanmei.com/workbench/script", payload["url"])

    def test_run_uses_explicit_environment_and_encoded_safe_prefill(self):
        with patch("hq_cli.cli.webbrowser.open") as opened:
            code, output, error = self.invoke(
                ["run", "image", "--environment", "zelong", "--input", "@-", "--json"],
                b'{"prompt":"A & B","engine":"gpt"}',
            )
        self.assertEqual(0, code, error)
        self.assertEqual("https://zelong.huangquechuanmei.com/workbench/banana?prompt=A+%26+B&engine=gpt", self.payload(output)["url"])
        opened.assert_not_called()

    def test_strict_input_rejects_unknown_nonfinite_and_bad_enum_without_http(self):
        with patch("hq_cli.cli.urllib.request.build_opener") as build_opener:
            code, output, error = self.invoke(["run", "canvas", "--input", "@-"], b'{"collab":"no"}')
            self.assertEqual(cli.EXIT_INPUT, code)
            self.assertEqual("hq.error/v1", self.payload(error)["schema"])
            code, output, error = self.invoke(["run", "audio", "--input", "@-"], b'{"speed":NaN}')
            self.assertEqual(cli.EXIT_INPUT, code)
            self.assertEqual("input_error", self.payload(error)["error"])
            code, output, error = self.invoke(["run", "audio", "--input", "@-"], b'{"pitch":1.5}')
            self.assertEqual(cli.EXIT_INPUT, code)
            self.assertEqual("input_error", self.payload(error)["error"])
            code, output, error = self.invoke(["run", "assets", "--input", "@-"], b'{"cat":"task"}')
            self.assertEqual(cli.EXIT_INPUT, code)
        build_opener.assert_not_called()

    def test_input_recursion_and_invalid_unicode_are_json_input_errors(self):
        deep = (b'{"x":' * 1200) + b'0' + (b'}' * 1200)
        for raw in (deep, b'{"prompt":"\\ud800"}'):
            code, output, error = self.invoke(["run", "image", "--input", "@-"], raw)
            self.assertEqual(cli.EXIT_INPUT, code)
            self.assertEqual("", output)
            self.assertEqual("input_error", self.payload(error)["error"])

    def test_planned_ip12_and_generation_are_discoverable_but_not_runnable(self):
        for identifier in ("ip12", "ip12-report", "image-generate", "video-generate", "audio-generate"):
            code, output, error = self.invoke(["run", identifier, "--json"])
            self.assertEqual(cli.EXIT_UNAVAILABLE, code)
            self.assertEqual("", output)
            self.assertEqual("unavailable_capability", self.payload(error)["error"])

    def test_unknown_capability_and_base_url_are_json_errors(self):
        code, output, error = self.invoke(["describe", "missing"])
        self.assertEqual(cli.EXIT_UNKNOWN_CAPABILITY, code)
        self.assertEqual("hq.error/v1", self.payload(error)["schema"])
        code, output, error = self.invoke(["run", "image", "--base-url", "https://example.test"])
        self.assertEqual(cli.EXIT_USAGE, code)
        self.assertEqual("usage_error", self.payload(error)["error"])

    def test_open_browser_is_explicit(self):
        with patch("hq_cli.cli.webbrowser.open") as opened:
            code, output, error = self.invoke(["run", "image", "--open-browser"])
        self.assertEqual(0, code, error)
        self.assertTrue(self.payload(output)["opened_browser"])
        opened.assert_called_once()

    def test_browser_false_and_exception_are_reported(self):
        with patch("hq_cli.cli.webbrowser.open", return_value=False):
            code, output, error = self.invoke(["run", "image", "--open-browser"])
        self.assertEqual(0, code, error)
        self.assertFalse(self.payload(output)["opened_browser"])
        with patch("hq_cli.cli.webbrowser.open", side_effect=OSError("blocked")):
            code, output, error = self.invoke(["run", "image", "--open-browser"])
        self.assertEqual(cli.EXIT_BROWSER, code)
        self.assertEqual("browser_error", self.payload(error)["error"])

    def test_doctor_checks_only_fixed_auth_and_generation_health_urls(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def getcode(self):
                return 200

        class Opener:
            def open(self, request, timeout):
                return Response()

        with patch("hq_cli.cli.urllib.request.build_opener", return_value=Opener()) as build_opener:
            code, output, error = self.invoke(["doctor", "--environment", "zelong", "--json"])
        self.assertEqual(0, code, error)
        payload = self.payload(output)
        self.assertEqual("hq.doctor/v1", payload["schema"])
        self.assertEqual(["auth", "generation"], [item["service"] for item in payload["checks"]])
        self.assertEqual(
            ["https://zelong.huangquechuanmei.com/api/auth/health", "https://zelong.huangquechuanmei.com/api/gen/health"],
            [item["url"] for item in payload["checks"]],
        )
        handlers = build_opener.call_args.args
        proxy = next(handler for handler in handlers if isinstance(handler, cli.urllib.request.ProxyHandler))
        self.assertEqual({}, proxy.proxies)
        redirect = next(handler for handler in handlers if isinstance(handler, cli._NoRedirect))
        self.assertIsNone(redirect.redirect_request(None, None, 302, "Found", {}, "https://elsewhere.test"))

    def test_option_abbreviation_is_rejected(self):
        code, output, error = self.invoke(["run", "image", "--environ", "main"])
        self.assertEqual(cli.EXIT_USAGE, code)
        self.assertEqual("usage_error", self.payload(error)["error"])


if __name__ == "__main__":
    unittest.main()
