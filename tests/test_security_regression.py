import builtins
import importlib
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock


class SecurityRegressionTests(unittest.TestCase):
    ORIGIN = "https://app.example.test"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_secret = os.environ.get("HQ_CSRF_SECRET")
        self.old_secure = os.environ.get("HQ_AUTH_COOKIE_SECURE")
        self.old_allowed_origins = os.environ.get("HQ_ALLOWED_ORIGINS")
        os.environ["HQ_CSRF_SECRET"] = "security-regression-secret"
        os.environ["HQ_AUTH_COOKIE_SECURE"] = "0"
        os.environ["HQ_ALLOWED_ORIGINS"] = (
            self.ORIGIN + ",https://admin.example.test:8443"
        )

        import server.auth_server as auth_server

        self.auth = importlib.reload(auth_server)
        self.auth.DB = os.path.join(self.tmp.name, "users.db")
        self.auth.init_db()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.auth.H)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self._restore_env("HQ_CSRF_SECRET", self.old_secret)
        self._restore_env("HQ_AUTH_COOKIE_SECURE", self.old_secure)
        self._restore_env("HQ_ALLOWED_ORIGINS", self.old_allowed_origins)
        self.tmp.cleanup()

    @staticmethod
    def _restore_env(name, value):
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value

    def _request(self, method, path, payload=None, headers=None, raw=None):
        body = raw if raw is not None else json.dumps(payload or {}).encode()
        request_headers = {"Content-Type": "application/json"}
        request_headers.update(headers or {})
        request = urllib.request.Request(
            self.base + path,
            data=body if method not in {"GET", "HEAD"} else None,
            headers=request_headers,
            method=method,
        )
        return urllib.request.urlopen(request, timeout=3)

    def _login(self, username):
        self.auth.create_user(username, "secret123", 5)
        with self._request(
            "POST",
            "/api/auth/login",
            {"username": username, "password": "secret123"},
            {"Origin": self.ORIGIN},
        ) as response:
            cookies = response.headers.get_all("Set-Cookie") or []
        cookie_header = "; ".join(cookie.split(";", 1)[0] for cookie in cookies)
        session_cookie = next(
            cookie for cookie in cookies if cookie.startswith(self.auth.AUTH_COOKIE_NAME + "=")
        )
        session_token = session_cookie.split(";", 1)[0].split("=", 1)[1]
        return cookie_header, self.auth.csrf_token_for(session_token)

    def _cookie_headers(self, username):
        cookie, csrf = self._login(username)
        return {
            "Cookie": cookie,
            "Origin": self.ORIGIN,
            "X-CSRF-Token": csrf,
        }

    def _assert_http_error(self, code, method, path, payload=None, headers=None, raw=None):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self._request(method, path, payload, headers, raw)
        self.assertEqual(error.exception.code, code)
        error.exception.close()

    def test_cookie_mutations_require_csrf_for_every_unsafe_method(self):
        cookie, _ = self._login("unsafe_methods")
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            for supplied in (None, "wrong"):
                headers = {"Cookie": cookie, "Origin": self.ORIGIN}
                if supplied is not None:
                    headers["X-CSRF-Token"] = supplied
                with self.subTest(method=method, supplied=supplied):
                    self._assert_http_error(
                        403, method, "/api/auth/profile", {"display_name": "blocked"}, headers
                    )

    def test_cookie_mutations_reject_untrusted_origins_and_referers(self):
        cookie, csrf = self._login("bad_origins")
        base = {"Cookie": cookie, "X-CSRF-Token": csrf}
        for origin in (
            "https://evil.example",
            "https://app.example.test.evil.example",
            "https://app.example.test/",
            "null",
        ):
            with self.subTest(origin=origin):
                self._assert_http_error(
                    403,
                    "POST",
                    "/api/auth/profile",
                    {"display_name": "blocked"},
                    {**base, "Origin": origin},
                )
        self._assert_http_error(
            403,
            "POST",
            "/api/auth/profile",
            {"display_name": "blocked"},
            {**base, "Referer": "https://evil.example/path"},
        )
        self._assert_http_error(
            403,
            "POST",
            "/api/auth/profile",
            {"display_name": "blocked"},
            base,
        )
        self._assert_http_error(
            403,
            "POST",
            "/api/auth/profile",
            {"display_name": "blocked"},
            {
                **base,
                "Origin": "https://evil.example",
                "Referer": self.ORIGIN + "/workbench/",
            },
        )

    def test_allowed_referer_is_used_only_when_origin_is_absent(self):
        headers = self._cookie_headers("referer_user")
        headers.pop("Origin")
        headers["Referer"] = self.ORIGIN + "/account/settings?tab=profile"
        with self._request(
            "POST", "/api/auth/profile", {"display_name": "Referer User"}, headers
        ) as response:
            self.assertEqual(response.status, 200)

    def test_allowed_origin_and_csrf_preserve_cookie_operations(self):
        cases = (
            ("logout", "/api/auth/logout", {}),
            ("profile", "/api/auth/profile", {"display_name": "Updated"}),
            (
                "password",
                "/api/auth/change_password",
                {"old_password": "secret123", "new_password": "newsecret456"},
            ),
            ("recharge", "/api/auth/recharge/order", {"amount": 99}),
        )
        for name, path, payload in cases:
            with self.subTest(name=name):
                with self._request(
                    "POST", path, payload, self._cookie_headers("valid_" + name)
                ) as response:
                    self.assertEqual(response.status, 200)

    def test_browser_mutation_rejects_non_json_media_type(self):
        headers = self._cookie_headers("non_json")
        headers["Content-Type"] = "text/plain"
        self._assert_http_error(
            415, "POST", "/api/auth/profile", headers=headers, raw=b"{}"
        )

    def test_login_requires_allowed_origin_and_json(self):
        self.auth.create_user("login_guard", "secret123", 5)
        payload = {"username": "login_guard", "password": "secret123"}
        self._assert_http_error(
            403, "POST", "/api/auth/login", payload, {"Origin": "https://evil.example"}
        )
        self._assert_http_error(
            415,
            "POST",
            "/api/auth/login",
            headers={"Origin": self.ORIGIN, "Content-Type": "text/plain"},
            raw=json.dumps(payload).encode(),
        )

    def test_login_origin_requirement_cannot_be_bypassed_by_bearer_header(self):
        self.auth.create_user("login_bearer_guard", "secret123", 5)
        self._assert_http_error(
            403,
            "POST",
            "/api/auth/login",
            {"username": "login_bearer_guard", "password": "secret123"},
            {
                "Origin": "https://evil.example",
                "Authorization": "Bearer unrelated-token",
            },
        )

    def test_bearer_miniprogram_flow_does_not_require_browser_csrf(self):
        self.auth.create_user("mini_user", "secret123", 5)
        with self._request(
            "POST",
            "/api/auth/miniprogram-login",
            {"username": "mini_user", "password": "secret123"},
        ) as response:
            token = json.loads(response.read())["token"]
        with self._request(
            "POST",
            "/api/auth/profile",
            {"display_name": "Mini User"},
            {"Authorization": "Bearer " + token},
        ) as response:
            self.assertEqual(response.status, 200)

    def test_wxpay_notify_still_reaches_signature_verification(self):
        fake_wxpay = mock.Mock()
        fake_wxpay.configured.return_value = True
        fake_wxpay.verify_notify.return_value = False
        self.auth.wxpay = fake_wxpay
        self._assert_http_error(
            401,
            "POST",
            "/api/auth/wxpay/notify",
            headers={"Content-Type": "application/octet-stream"},
            raw=b"{}",
        )
        fake_wxpay.verify_notify.assert_called_once()

    def test_get_and_head_do_not_require_csrf(self):
        cookie, _ = self._login("safe_methods")
        with self._request("GET", "/api/auth/me", headers={"Cookie": cookie}) as response:
            self.assertEqual(response.status, 200)
        self._assert_http_error(501, "HEAD", "/api/auth/me", headers={"Cookie": cookie})

    def test_valid_unsupported_mutations_preserve_501(self):
        headers = self._cookie_headers("unsupported_methods")
        for method in ("PUT", "PATCH"):
            with self.subTest(method=method):
                self._assert_http_error(
                    501,
                    method,
                    "/api/auth/profile",
                    {"display_name": "unchanged"},
                    headers,
                )

    def test_security_event_contains_only_safe_fields(self):
        handler = object.__new__(self.auth.H)
        handler.command = "POST"
        handler.path = "/api/auth/profile"
        handler.client_address = ("192.0.2.10", 54321)
        handler.headers = {
            "Content-Type": "application/json",
            "Origin": "https://evil.example",
            "Cookie": "hq_session=session-secret",
            "X-CSRF-Token": "csrf-secret",
            "Authorization": "Basic bearer-secret",
            "X-Request-ID": "request-123",
        }
        handler._send = mock.Mock()
        with mock.patch.object(builtins, "print") as printed:
            self.assertFalse(handler._require_browser_mutation_security(handler.path))
        event = json.loads(printed.call_args.args[0])
        self.assertEqual(
            set(event), {"request_id", "path", "client_ip", "origin", "reason"}
        )
        serialized = json.dumps(event)
        for secret in ("session-secret", "csrf-secret", "bearer-secret"):
            self.assertNotIn(secret, serialized)


if __name__ == "__main__":
    unittest.main()
