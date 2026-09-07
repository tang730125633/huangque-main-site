import json
import pathlib
import tempfile
import unittest
import urllib.parse
from unittest import mock

from server import heygen_oauth


class _Response:
    def __init__(self, body):
        self.body = json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit=-1):
        return self.body


class _Opener:
    def __init__(self, body):
        self.body = body
        self.request = None

    def open(self, request, timeout=0):
        self.request = request
        self.timeout = timeout
        return _Response(self.body)


class HeyGenAdminOAuthTests(unittest.TestCase):
    def setUp(self):
        heygen_oauth._flows.clear()

    def tearDown(self):
        heygen_oauth._flows.clear()

    def test_begin_uses_pkce_state_resource_and_fixed_redirect(self):
        result = heygen_oauth.begin_authorization(
            "C:/safe/heygen.json",
            "http://127.0.0.1:8104/api/admin/heygen-oauth/callback",
            "qilin",
            now=100,
        )
        parsed = urllib.parse.urlsplit(result["authorization_url"])
        query = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(parsed.scheme + "://" + parsed.netloc, "https://app.heygen.com")
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["resource"], ["https://mcp.heygen.com/mcp/v1"])
        self.assertEqual(query["redirect_uri"], [
            "http://127.0.0.1:8104/api/admin/heygen-oauth/callback"
        ])
        self.assertNotIn("code_verifier", query)
        self.assertEqual(len(heygen_oauth._flows), 1)

    def test_begin_rejects_untrusted_plain_http_redirect(self):
        with self.assertRaises(heygen_oauth.HeyGenOAuthError):
            heygen_oauth.begin_authorization(
                "C:/safe/heygen.json", "http://example.com/callback", "admin"
            )

    def test_callback_exchanges_once_and_writes_flat_runtime_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "heygen.json"
            result = heygen_oauth.begin_authorization(
                path, "http://127.0.0.1:8104/api/admin/heygen-oauth/callback", "qilin", now=100
            )
            state = urllib.parse.parse_qs(
                urllib.parse.urlsplit(result["authorization_url"]).query
            )["state"][0]
            opener = _Opener({
                "access_token": "access-value",
                "refresh_token": "refresh-value",
                "expires_in": 7200,
                "scope": "openid profile email",
                "token_type": "Bearer",
            })
            completed = heygen_oauth.complete_authorization(
                state, "authorization-code", opener=opener, now=110
            )
            stored = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(completed["ok"])
        self.assertEqual(stored["client_id"], heygen_oauth.DEFAULT_CLIENT_ID)
        self.assertEqual(stored["access_token"], "access-value")
        self.assertEqual(stored["refresh_token"], "refresh-value")
        self.assertEqual(stored["expires_at"], 7310)
        self.assertEqual(stored["authorized_by"], "qilin")
        form = urllib.parse.parse_qs(opener.request.data.decode("utf-8"))
        self.assertEqual(form["grant_type"], ["authorization_code"])
        self.assertEqual(form["resource"], ["https://mcp.heygen.com/mcp/v1"])
        self.assertIn("code_verifier", form)
        with self.assertRaises(heygen_oauth.HeyGenOAuthError):
            heygen_oauth.complete_authorization(state, "replayed", opener=opener, now=111)

    def test_status_never_returns_tokens_and_accepts_official_nested_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "credentials"
            path.write_text(json.dumps({
                "oauth": {
                    "access_token": "must-not-leak",
                    "refresh_token": "also-secret",
                    "expires_at": "2100-01-01T00:00:00Z",
                    "scope": "openid",
                },
                "user": {"email": "person@example.test"},
            }), encoding="utf-8")
            status = heygen_oauth.credential_status(path, now=100)
        self.assertTrue(status["configured"])
        self.assertEqual(status["status"], "connected")
        self.assertEqual(status["account"]["email"], "person@example.test")
        self.assertNotIn("must-not-leak", json.dumps(status))
        self.assertNotIn("also-secret", json.dumps(status))

    def test_disconnect_only_removes_local_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "credentials"
            path.write_text("{}", encoding="utf-8")
            self.assertTrue(heygen_oauth.disconnect(path))
            self.assertFalse(path.exists())
            self.assertFalse(heygen_oauth.disconnect(path))


if __name__ == "__main__":
    unittest.main()
