import importlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock


class AuthCsrfTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_secret = os.environ.get("HQ_CSRF_SECRET")
        self.old_secure = os.environ.get("HQ_AUTH_COOKIE_SECURE")
        self.old_allowed_origins = os.environ.get("HQ_ALLOWED_ORIGINS")
        os.environ["HQ_CSRF_SECRET"] = "fixed-test-csrf-secret"
        os.environ["HQ_AUTH_COOKIE_SECURE"] = "0"
        os.environ["HQ_ALLOWED_ORIGINS"] = "https://app.example.test"

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
        if self.old_secret is None:
            os.environ.pop("HQ_CSRF_SECRET", None)
        else:
            os.environ["HQ_CSRF_SECRET"] = self.old_secret
        if self.old_secure is None:
            os.environ.pop("HQ_AUTH_COOKIE_SECURE", None)
        else:
            os.environ["HQ_AUTH_COOKIE_SECURE"] = self.old_secure
        if self.old_allowed_origins is None:
            os.environ.pop("HQ_ALLOWED_ORIGINS", None)
        else:
            os.environ["HQ_ALLOWED_ORIGINS"] = self.old_allowed_origins
        self.tmp.cleanup()

    def _post(self, path, payload, headers=None):
        request_headers = {
            "Content-Type": "application/json",
            "Origin": "https://app.example.test",
        }
        request_headers.update(headers or {})
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode(),
            headers=request_headers,
            method="POST",
        )
        return urllib.request.urlopen(request, timeout=3)

    def _login(self, username="csrf_user"):
        self.auth.create_user(username, "secret123", 5)
        with self._post(
            "/api/auth/login",
            {"username": username, "password": "secret123"},
        ) as response:
            return response.headers.get_all("Set-Cookie") or []

    @staticmethod
    def _cookie(cookies, name):
        return next(cookie for cookie in cookies if cookie.startswith(name + "="))

    def test_csrf_token_is_stable_and_bound_to_session(self):
        first = self.auth.csrf_token_for("session-one")
        self.assertEqual(first, self.auth.csrf_token_for("session-one"))
        self.assertNotEqual(first, self.auth.csrf_token_for("session-two"))

    def test_empty_session_and_empty_secret_are_rejected(self):
        with self.assertRaises(ValueError):
            self.auth.csrf_token_for("")
        self.auth.CSRF_SECRET = ""
        with self.assertRaises(RuntimeError):
            self.auth.csrf_token_for("session-one")

    def test_server_startup_rejects_empty_csrf_secret(self):
        env = os.environ.copy()
        env["HQ_CSRF_SECRET"] = ""
        result = subprocess.run(
            [sys.executable, "server/auth_server.py"],
            cwd=os.path.dirname(os.path.dirname(__file__)),
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("HQ_CSRF_SECRET", result.stderr + result.stdout)

    def test_csrf_validation_uses_constant_time_comparison(self):
        handler = object.__new__(self.auth.H)
        expected = self.auth.csrf_token_for("session-one")
        handler.headers = {"X-CSRF-Token": expected}
        with mock.patch.object(
            self.auth.secrets,
            "compare_digest",
            wraps=self.auth.secrets.compare_digest,
        ) as compare:
            self.assertTrue(handler._csrf_valid("session-one"))
        compare.assert_called_once_with(expected, expected)

    def test_login_sets_separate_session_and_csrf_cookies(self):
        cookies = self._login()
        self.assertEqual(len(cookies), 2)
        session_cookie = self._cookie(cookies, self.auth.AUTH_COOKIE_NAME)
        csrf_cookie = self._cookie(cookies, "hq_csrf")
        self.assertIn("HttpOnly", session_cookie)
        self.assertIn("SameSite=Lax", session_cookie)
        self.assertIn("Path=/", csrf_cookie)
        self.assertIn("SameSite=Lax", csrf_cookie)
        self.assertNotIn("HttpOnly", csrf_cookie)
        self.assertNotIn(",", session_cookie + csrf_cookie)

    def test_register_sets_secure_session_and_csrf_cookies(self):
        self.auth.AUTH_COOKIE_SECURE = True
        with self._post(
            "/api/auth/register",
            {"username": "new_csrf_user", "password": "secret123"},
        ) as response:
            cookies = response.headers.get_all("Set-Cookie") or []
        self.assertEqual(len(cookies), 2)
        self.assertIn("Secure", self._cookie(cookies, self.auth.AUTH_COOKIE_NAME))
        csrf_cookie = self._cookie(cookies, "hq_csrf")
        self.assertIn("Secure", csrf_cookie)
        self.assertNotIn("HttpOnly", csrf_cookie)

    def test_logout_rejects_wrong_token_and_clears_both_cookies(self):
        cookies = self._login("logout_user")
        session_cookie = self._cookie(cookies, self.auth.AUTH_COOKIE_NAME)
        session_token = session_cookie.split(";", 1)[0].split("=", 1)[1]
        cookie_header = "; ".join(cookie.split(";", 1)[0] for cookie in cookies)

        with self.assertRaises(urllib.error.HTTPError) as error:
            self._post(
                "/api/auth/logout",
                {},
                {"Cookie": cookie_header, "X-CSRF-Token": "wrong"},
            )
        self.assertEqual(error.exception.code, 403)

        with self._post(
            "/api/auth/logout",
            {},
            {
                "Cookie": cookie_header,
                "X-CSRF-Token": self.auth.csrf_token_for(session_token),
            },
        ) as response:
            cleared = response.headers.get_all("Set-Cookie") or []
        self.assertEqual(len(cleared), 2)
        self.assertIn("Max-Age=0", self._cookie(cleared, self.auth.AUTH_COOKIE_NAME))
        self.assertIn("Max-Age=0", self._cookie(cleared, "hq_csrf"))

    def test_bearer_logout_ignores_unrelated_ambient_session_cookie(self):
        self.auth.create_user("bearer_user", "secret123", 5)
        with self._post(
            "/api/auth/miniprogram-login",
            {"username": "bearer_user", "password": "secret123"},
        ) as response:
            token = json.loads(response.read())["token"]

        with self._post(
            "/api/auth/logout",
            {},
            {
                "Authorization": "Bearer " + token,
                "Cookie": self.auth.AUTH_COOKIE_NAME + "=unrelated-stale-session",
            },
        ) as response:
            self.assertEqual(response.status, 200)

    def test_logout_rejects_non_ascii_csrf_token_with_403(self):
        cookies = self._login("non_ascii_csrf_user")
        cookie_header = "; ".join(cookie.split(";", 1)[0] for cookie in cookies)

        with self.assertRaises(urllib.error.HTTPError) as error:
            self._post(
                "/api/auth/logout",
                {},
                {"Cookie": cookie_header, "X-CSRF-Token": "é"},
            )
        self.assertEqual(error.exception.code, 403)


if __name__ == "__main__":
    unittest.main()
