import http.client
import http.server
import io
import json
import pathlib
import tempfile
import threading
import time
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


class FakeUpstreamHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _reply(self):
        if self.path == "/api/slow":
            time.sleep(0.2)
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8") if length else ""
        payload = json.dumps(
            {
                "method": self.command,
                "path": self.path,
                "body": body,
                "host": self.headers.get("Host"),
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header(
            "Set-Cookie",
            "hq_session=secret; Domain=example.com; Path=/; Secure; "
            "HttpOnly; SameSite=Strict",
        )
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                pass

    do_GET = _reply
    do_POST = _reply
    do_PUT = _reply
    do_PATCH = _reply
    do_DELETE = _reply
    do_OPTIONS = _reply

    def log_message(self, _format, *_args):
        pass


class ProxyIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.site_root = pathlib.Path(self.tmp.name) / "site"
        workbench = self.site_root / "workbench"
        workbench.mkdir(parents=True)
        (workbench / "inspiration.html").write_text(
            "LOCAL-INSPIRATION", encoding="utf-8"
        )
        (pathlib.Path(self.tmp.name) / "secret.txt").write_text(
            "MUST-NOT-LEAK", encoding="utf-8"
        )

        self.upstream = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), FakeUpstreamHandler
        )
        self.upstream_thread = threading.Thread(
            target=self.upstream.serve_forever, daemon=True
        )
        self.upstream_thread.start()
        upstream_url = "http://127.0.0.1:%d" % self.upstream.server_port

        self.proxy_log = io.StringIO()
        self.proxy = dev_proxy.create_server(
            self.site_root, upstream_url, port=0, log_stream=self.proxy_log
        )
        self.proxy_thread = threading.Thread(
            target=self.proxy.serve_forever, daemon=True
        )
        self.proxy_thread.start()

    def tearDown(self):
        self.proxy.shutdown()
        self.proxy.server_close()
        if self.upstream:
            self.upstream.shutdown()
            self.upstream.server_close()
        self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection(
            "127.0.0.1", self.proxy.server_port, timeout=3
        )
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        response_body = response.read()
        result = response.status, response.getheaders(), response_body
        conn.close()
        return result

    def stop_upstream(self):
        self.upstream.shutdown()
        self.upstream.server_close()
        self.upstream = None

    def test_serves_static_files_from_configured_site_root(self):
        status, _, body = self.request("GET", "/workbench/inspiration.html")

        self.assertEqual(status, 200)
        self.assertEqual(body, b"LOCAL-INSPIRATION")

    def test_forwards_api_method_query_body_host_and_rewrites_cookie(self):
        status, headers, body = self.request(
            "POST",
            "/api/auth/login?source=local",
            b'{"username":"qilin"}',
            {"Content-Type": "application/json"},
        )

        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["method"], "POST")
        self.assertEqual(payload["path"], "/api/auth/login?source=local")
        self.assertEqual(payload["body"], '{"username":"qilin"}')
        self.assertEqual(
            payload["host"], "127.0.0.1:%d" % self.upstream.server_port
        )
        cookie = next(value for name, value in headers if name == "Set-Cookie")
        self.assertNotIn("Domain=", cookie)
        self.assertNotIn("Secure", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertFalse(any(name.lower() == "connection" for name, _ in headers))

    def test_non_api_writes_are_rejected(self):
        status, _, body = self.request("POST", "/workbench/inspiration.html")

        self.assertEqual(status, 405)
        self.assertEqual(json.loads(body)["detail"], "method not allowed")

    def test_encoded_directory_traversal_cannot_escape_site_root(self):
        status, _, body = self.request("GET", "/%2e%2e/secret.txt")

        self.assertEqual(status, 404)
        self.assertNotIn(b"MUST-NOT-LEAK", body)

    def test_upstream_failure_returns_sanitized_502_json(self):
        self.stop_upstream()

        status, _, body = self.request(
            "GET", "/api/auth/health?secret=must-not-be-logged"
        )

        self.assertEqual(status, 502)
        self.assertEqual(json.loads(body)["detail"], "test backend unavailable")
        self.assertNotIn("must-not-be-logged", self.proxy_log.getvalue())

    def test_upstream_timeout_returns_504_json(self):
        self.proxy.upstream_timeout = 0.05

        status, _, body = self.request("GET", "/api/slow")

        self.assertEqual(status, 504)
        self.assertEqual(json.loads(body)["detail"], "test backend timeout")

    def test_create_server_rejects_missing_site_root(self):
        with self.assertRaises(ValueError):
            dev_proxy.create_server(
                pathlib.Path(self.tmp.name) / "missing",
                "http://127.0.0.1:1",
                port=0,
            )


class LauncherContractTests(unittest.TestCase):
    def test_launcher_uses_proxy_when_test_upstream_is_configured(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        launcher = (root / "scripts" / "dev_local.sh").read_text(encoding="utf-8")

        self.assertIn('if [ -n "${HQ_DEV_UPSTREAM:-}" ]', launcher)
        self.assertIn('dev_proxy.py" --upstream "$HQ_DEV_UPSTREAM"', launcher)
        self.assertIn("真实测试服务器账号、点数和第三方额度", launcher)


if __name__ == "__main__":
    unittest.main()
