import io
import json
from http.server import ThreadingHTTPServer
import pathlib
import sys
import threading
import unittest
import urllib.error
import urllib.request


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from creator_agent.model_proxy import AccountModelProxy
from creator_agent.service import AuthClient, CreatorAgentHandler


class FakeLease:
    def __init__(self):
        self.success = None

    def finish(self, success=True):
        self.success = success


class FakeUsage:
    def __init__(self):
        self.calls = []
        self.lease = FakeLease()

    def acquire(self, *args):
        self.calls.append(args)
        return self.lease


class FakeResponse:
    def __init__(self):
        self.headers = {"Content-Type": "text/event-stream"}
        self.body = io.BytesIO(
            b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
            b'data: [DONE]\n\n'
        )

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def getcode(self):
        return 200

    def read(self, size=-1):
        return self.body.read(size)


class FakeOpener:
    def __init__(self):
        self.request = None

    def open(self, request, timeout=0):
        self.request = request
        return FakeResponse()


class FakeProfileAgent:
    configured = True
    base_url = "https://api.deepseek.com"
    model = "deepseek-v4-flash"
    api_key = "provider-secret"

    def __init__(self):
        self.opener = FakeOpener()


class FakeAuth:
    def verify(self, headers):
        if headers.get("Authorization") != "Bearer account-token":
            from creator_agent.service import APIError
            raise APIError(401, "请先登录", "unauthorized")
        return {"username": "tang", "account_id": "HQ-1"}


class FakeAuthOpener:
    def __init__(self):
        self.paths = []

    def open(self, request, timeout=0):
        self.paths.append(request.full_url)
        if request.full_url.endswith("/api/auth/me"):
            raise urllib.error.HTTPError(request.full_url, 401, "unauthorized", {}, io.BytesIO(b"{}"))
        payload = {
            "user": {"username": "tang", "account_id": "HQ-1"},
            "scopes": ["creator-agent:read"],
        }
        response = FakeResponse()
        response.body = io.BytesIO(json.dumps(payload).encode())
        return response


class FakeService:
    def __init__(self):
        self.auth = FakeAuth()
        self.profile_agent = FakeProfileAgent()
        self.usage_guard = FakeUsage()
        self.model_proxy = AccountModelProxy(self.profile_agent, self.usage_guard)

    @staticmethod
    def _client_ip(_headers):
        return "127.0.0.1"


class AccountModelProxyTests(unittest.TestCase):
    def setUp(self):
        self.service = FakeService()
        CreatorAgentHandler.service = self.service
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), CreatorAgentHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def request(self, body, token="account-token"):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(urllib.request.Request(
            "http://127.0.0.1:%d/model/v1/chat/completions" % self.server.server_address[1],
            data=json.dumps(body).encode(),
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
            method="POST",
        ), timeout=3)

    def test_account_token_streams_server_model_without_exposing_provider_key(self):
        with self.request({
            "model": "huangque-agent",
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 99999,
        }) as response:
            raw = response.read()
        upstream = self.service.profile_agent.opener.request
        payload = json.loads(upstream.data)
        self.assertEqual(payload["model"], "deepseek-v4-flash")
        self.assertEqual(payload["max_tokens"], 8192)
        self.assertEqual(upstream.get_header("Authorization"), "Bearer provider-secret")
        self.assertIn(b'data: [DONE]', raw)
        self.assertTrue(self.service.usage_guard.lease.success)

    def test_creator_service_accepts_scoped_cli_login_after_web_auth_rejects_it(self):
        opener = FakeAuthOpener()
        user = AuthClient("http://127.0.0.1:8095", opener=opener).verify({
            "Authorization": "Bearer cli-access-token",
        })
        self.assertEqual(user["username"], "tang")
        self.assertEqual(opener.paths, [
            "http://127.0.0.1:8095/api/auth/me",
            "http://127.0.0.1:8095/api/auth/cli/status",
        ])

    def test_rejects_missing_login_and_image_input(self):
        body = {
            "model": "huangque-agent", "stream": True,
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},
            ]}],
        }
        with self.assertRaises(urllib.error.HTTPError) as unauthorized:
            self.request(body, token="wrong")
        self.assertEqual(unauthorized.exception.code, 401)
        with self.assertRaises(urllib.error.HTTPError) as unsupported:
            self.request(body)
        self.assertEqual(unsupported.exception.code, 400)
        self.assertEqual(json.loads(unsupported.exception.read())["code"], "image_input_unsupported")
        self.assertEqual(self.service.usage_guard.calls, [])


if __name__ == "__main__":
    unittest.main()
