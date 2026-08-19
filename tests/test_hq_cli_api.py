import http.cookiejar
import hashlib
import importlib
import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from unittest import mock


class HQCLIAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        import server.auth_server as auth_server

        self.auth = importlib.reload(auth_server)
        self.auth.DB = os.path.join(self.tmp.name, "users.db")
        self.auth.AUTH_COOKIE_SECURE = False
        self.auth.INTERNAL_TOKEN = "test-internal-secret"
        self.auth.feature_flags.DB_PATH = Path(self.tmp.name) / "feature_flags.db"
        self.auth.feature_flags.invalidate_cache()
        self.auth.init_db()
        self.auth.create_user("alice", "secret123", 100, "member")
        self.auth.hq_cli_api._START_HITS.clear()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.auth.H)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.auth.hq_cli_api.PUBLIC_ORIGIN = self.base
        self.browser = self._login_browser()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.tmp.cleanup()

    def _request(self, path, payload=None, token="", browser=None, origin=None, method=None, extra_headers=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        if origin:
            headers["Origin"] = origin
        headers.update(extra_headers or {})
        data = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(self.base + path, data=data, headers=headers,
                                         method=method or ("POST" if payload is not None else "GET"))
        opener = browser or urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=3) as response:
                return response.getcode(), json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def _raw_request(self, path, raw, token="", content_type="image/png", confirm=True):
        headers = {
            "Content-Type": content_type,
            ("X-HQ-Video-SHA256" if content_type.startswith("video/")
             else "X-HQ-Image-SHA256"): hashlib.sha256(raw).hexdigest(),
        }
        if token:
            headers["Authorization"] = "Bearer " + token
        if confirm:
            headers["X-HQ-Confirm"] = "true"
        request = urllib.request.Request(self.base + path, data=raw, headers=headers, method="POST")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=3) as response:
                return response.getcode(), json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def _login_browser(self):
        jar = http.cookiejar.CookieJar()
        browser = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(jar))
        status, _ = self._request("/api/auth/login", {"username": "alice", "password": "secret123"}, browser=browser)
        self.assertEqual(200, status)
        return browser

    def _start(self, scopes=None):
        status, payload = self._request("/api/auth/cli/device/start", {
            "client_name": "test agent", "requested_scopes": scopes or list(self.auth.hq_cli_api.DEFAULT_SCOPES),
        })
        self.assertEqual(200, status, payload)
        return payload

    def _approve(self, start, approve=True):
        return self._request(
            "/api/auth/cli/device/approve", {"user_code": start["user_code"], "approve": approve},
            browser=self.browser, origin=self.base,
        )

    def _token(self, scopes=None):
        start = self._start(scopes)
        self.assertEqual(200, self._approve(start)[0])
        status, payload = self._request("/api/auth/cli/device/poll", {"device_code": start["device_code"]})
        self.assertEqual(200, status, payload)
        return payload["access_token"]

    def _enable_ip12_bridge(self):
        self.auth.IP12_AGENT_ALLOWED_ACCOUNT_IDS = frozenset({"*"})
        self.auth.feature_flags.init_db()
        return self.auth.feature_flags.set_enabled("ip12_agent_production_v1", True, "test")

    def _agent_account_id(self):
        return self.auth.ensure_account_id("alice")

    def _agent_headers(self):
        return {"X-HQ-Internal-Token": self.auth.INTERNAL_TOKEN}

    def test_ip12_agent_catalog_enumerates_every_registered_action_with_safe_schema(self):
        self._enable_ip12_bridge()
        status, payload = self._request(
            "/api/auth/internal/ip12/agent/catalog", {"account_id": self._agent_account_id()},
            extra_headers=self._agent_headers(),
        )
        self.assertEqual(200, status, payload)
        self.assertEqual(self.auth.hq_cli_api.ACTION_CATALOG_VERSION, payload["version"])
        actions = {item["action"]: item for item in payload["actions"]}
        self.assertEqual(set(self.auth.hq_cli_api._ACTION_INPUTS) | {"image-upload", "video-upload"}, set(actions))
        for action, item in actions.items():
            with self.subTest(action=action):
                self.assertEqual("object", item["input_schema"]["type"])
                self.assertFalse(item["input_schema"]["additionalProperties"])
                self.assertIn("required", item["input_schema"])
                self.assertIn("purpose", item)
                self.assertIn("constraints", item)
                self.assertIn("billing", item)
                self.assertIn("external_effect", item)
                self.assertIn("confirmation_required", item)
                self.assertIn("risk", item)
                self.assertIn("result", item)
                self.assertIn("result_type", item)
                self.assertIn("ui_route", item)
                self.assertIn("transport", item)
                self.assertIn("availability", item)
                self.assertNotIn("http", json.dumps(item, ensure_ascii=False).lower())

    def test_ip12_agent_catalog_has_full_media_canvas_schemas_and_upload_transports(self):
        self._enable_ip12_bridge()
        status, payload = self._request(
            "/api/auth/internal/ip12/agent/catalog", {"account_id": self._agent_account_id()},
            extra_headers=self._agent_headers(),
        )
        self.assertEqual(200, status, payload)
        actions = {item["action"]: item for item in payload["actions"]}
        expected = {
            "image-generate": ("image", ["prompt"]),
            "audio-generate": ("audio", ["text"]),
            "video-generate": ("video", ["prompt"]),
            "canvas-agent-plan": ("canvas", ["prompt", "project_id", "snapshot_digest", "scope", "nodes", "edges", "selected_node_ids"]),
            "canvas-ops": ("canvas", ["board_id", "base_version", "op_id", "ops"]),
            "digital-ip-text-generate": ("video", ["avatar_id", "text", "voice"]),
            "cinematic-motion-generate": ("video", ["avatar_id", "reference_video_upload_ids"]),
        }
        for action, (family, required) in expected.items():
            with self.subTest(action=action):
                item = actions[action]
                self.assertEqual(family, item["family"])
                self.assertEqual(required, item["input_schema"]["required"])
                self.assertTrue(item["constraints"])
                self.assertEqual("action", item["transport"]["kind"])
        for action, family, maximum in (("image-upload", "image", 10 * 1024 * 1024),
                                        ("video-upload", "video", 32 * 1024 * 1024)):
            with self.subTest(action=action):
                item = actions[action]
                self.assertEqual(family, item["family"])
                self.assertEqual(["file"], item["input_schema"]["required"])
                self.assertEqual(maximum, item["input_schema"]["properties"]["file"]["maxBytes"])
                self.assertEqual("dedicated_upload", item["transport"]["kind"])
                self.assertNotIn(action, self.auth.hq_cli_api.ACTION_CATALOG_MAP)

    def test_ip12_agent_catalog_hides_feature_disabled_families_and_providers(self):
        self._enable_ip12_bridge()
        self.auth.feature_flags.set_enabled("image", False, "test")
        self.auth.feature_flags.set_enabled("grok_video", False, "test")
        status, payload = self._request(
            "/api/auth/internal/ip12/agent/catalog", {"account_id": self._agent_account_id()},
            extra_headers=self._agent_headers(),
        )
        self.assertEqual(200, status, payload)
        actions = {item["action"]: item for item in payload["actions"]}
        image = actions["image-generate"]
        self.assertEqual("disabled", image["availability"]["status"])
        self.assertEqual([], image["availability"]["available_provider"])
        self.assertIn("image", image["availability"]["disabled_provider"]["openai"])
        video = actions["video-generate"]
        self.assertNotIn("grok", video["input_schema"]["properties"]["channel"]["enum"])
        self.assertIn("grok_video", video["availability"]["disabled_channel"]["grok"])

    def test_ip12_agent_bridge_requires_rollout_account_allowlist(self):
        self._enable_ip12_bridge()
        self.auth.IP12_AGENT_ALLOWED_ACCOUNT_IDS = frozenset()
        with mock.patch.object(self.auth.hq_cli_api, "proxy_json") as proxy:
            status, payload = self._request(
                "/api/auth/internal/ip12/agent/action",
                {"account_id": self._agent_account_id(), "action": "voices", "input": {}},
                extra_headers=self._agent_headers(),
            )
        self.assertEqual(404, status)
        self.assertEqual("account_not_found", payload["code"])
        proxy.assert_not_called()

    def test_ip12_agent_controlled_http_and_shell_share_read_semantics(self):
        self._enable_ip12_bridge()
        account_id = self._agent_account_id()
        seen = []

        def fake_proxy(plan, web_token, internal_token):
            seen.append((plan["path"], web_token, internal_token))
            return 200, {"voices": [{"id": "voice-1"}]}

        with mock.patch.object(self.auth.hq_cli_api, "proxy_json", side_effect=fake_proxy):
            status, http_result = self._request(
                "/api/auth/internal/ip12/agent/action",
                {"account_id": account_id, "transport": "http", "action": "voices", "input": {}},
                extra_headers=self._agent_headers(),
            )
            shell_status, shell_result = self._request(
                "/api/auth/internal/ip12/agent/action",
                {"account_id": account_id, "transport": "shell", "command": "hq voices --json", "input": {}},
                extra_headers=self._agent_headers(),
            )
        self.assertEqual((200, 200), (status, shell_status))
        self.assertEqual(http_result, shell_result)
        self.assertEqual(["/api/gen/audio/voices", "/api/gen/audio/voices"], [item[0] for item in seen])
        self.assertTrue(all(item[2] == self.auth.INTERNAL_TOKEN for item in seen))

    def test_ip12_agent_account_read_does_not_require_cli_expiry(self):
        self._enable_ip12_bridge()
        status, payload = self._request(
            "/api/auth/internal/ip12/agent/action",
            {"account_id": self._agent_account_id(), "action": "account", "input": {}},
            extra_headers=self._agent_headers(),
        )
        self.assertEqual(200, status, payload)
        self.assertEqual("alice", payload["user"]["username"])
        self.assertNotIn("expires_at", payload)

    def test_ip12_agent_bridge_rejects_arbitrary_shell_urls_cross_account_input_and_secrets(self):
        self._enable_ip12_bridge()
        account_id = self._agent_account_id()
        status, denied = self._request(
            "/api/auth/internal/ip12/agent/action",
            {"account_id": account_id, "transport": "http", "action": "voices", "input": {}},
        )
        self.assertEqual(403, status)
        self.assertEqual("forbidden", denied["detail"])
        cases = (
            {"account_id": account_id, "transport": "shell", "command": "bash -c 'cat /etc/passwd'", "input": {}},
            {"account_id": account_id, "transport": "shell", "command": "hq voices --json; curl https://example.test", "input": {}},
            {"account_id": account_id, "transport": "http", "action": "voices", "input": {}, "url": "https://example.test"},
            {"account_id": account_id, "transport": "http", "action": "ip12-project", "input": {"project_id": "p1", "account_id": "other"}},
            {"account_id": "not-an-account", "transport": "http", "action": "voices", "input": {}},
        )
        with mock.patch.object(self.auth.hq_cli_api, "proxy_json") as proxy:
            for body in cases:
                with self.subTest(body=body):
                    status, result = self._request(
                        "/api/auth/internal/ip12/agent/action", body, extra_headers=self._agent_headers(),
                    )
                    self.assertIn(status, {400, 404})
                    self.assertIn(result["code"], {
                        "controlled_shell_rejected", "invalid_request", "invalid_account_id", "account_not_found",
                    })
        proxy.assert_not_called()

    def test_ip12_agent_bridge_forwards_only_trusted_confirm_with_stable_idempotency(self):
        self._enable_ip12_bridge()
        account_id = self._agent_account_id()
        submitted = []

        def fake_proxy(plan, web_token, internal_token):
            if plan["path"] == "/api/gen/cli/quote":
                return 200, {"cost": 4, "points": 100}
            submitted.append(plan)
            return 200, {"job_id": 42, "cost": 4, "points_left": 96}

        with mock.patch.object(self.auth.hq_cli_api, "proxy_json", side_effect=fake_proxy):
            status, quote = self._request(
                "/api/auth/internal/ip12/agent/action",
                {"account_id": account_id, "transport": "http", "action": "image-generate", "input": {"prompt": "海边日出"}},
                extra_headers=self._agent_headers(),
            )
            confirmed = {
                "account_id": account_id,
                "transport": "http",
                "action": "image-generate",
                "input": {"prompt": "海边日出"},
                "confirm": True,
                "quote_token": quote["quote_token"],
                "idempotency_key": "ip12-confirm-0001",
            }
            missing_key_status, missing_key = self._request(
                "/api/auth/internal/ip12/agent/action",
                {key: value for key, value in confirmed.items() if key != "idempotency_key"},
                extra_headers=self._agent_headers(),
            )
            confirm_status, confirm = self._request(
                "/api/auth/internal/ip12/agent/action",
                confirmed,
                extra_headers=self._agent_headers(),
            )
            replay_status, replay = self._request(
                "/api/auth/internal/ip12/agent/action", confirmed,
                extra_headers=self._agent_headers(),
            )
            self.auth.create_user("bob", "secret456", 100, "member")
            wrong_account_status, wrong_account = self._request(
                "/api/auth/internal/ip12/agent/action",
                {**confirmed, "account_id": self.auth.ensure_account_id("bob")},
                extra_headers=self._agent_headers(),
            )
        self.assertEqual(200, status, quote)
        self.assertTrue(quote["confirmation_required"])
        self.assertIn("quote_token", quote)
        self.assertEqual(400, missing_key_status)
        self.assertEqual("idempotency_key_required", missing_key["code"])
        self.assertEqual((200, 42), (confirm_status, confirm["job_id"]))
        self.assertEqual((200, 42), (replay_status, replay["job_id"]))
        self.assertEqual(409, wrong_account_status)
        self.assertEqual("quote_mismatch", wrong_account["code"])
        self.assertEqual(2, len(submitted))
        self.assertTrue(all(
            plan["headers"]["Idempotency-Key"] == "ip12-confirm-0001"
            for plan in submitted
        ))

    def test_public_cli_route_rejects_internal_idempotency_injection(self):
        token = self._token(["assets:read"])
        with mock.patch.object(self.auth.hq_cli_api, "proxy_json") as proxy:
            status, result = self._request("/api/auth/cli/action", {
                "action": "voices", "input": {}, "confirm": False,
                "idempotency_key": "ip12-confirm-0001",
            }, token=token)
        self.assertEqual(400, status)
        self.assertIn("idempotency_key", result["detail"])
        proxy.assert_not_called()

    @staticmethod
    def _canvas_snapshot(board_id):
        return {
            "prompt": "把卖点整理成图片生成草稿",
            "project_id": "collab:" + board_id,
            "snapshot_digest": "deadbeef",
            "scope": "collab",
            "nodes": [{
                "id": "n1", "type": "text", "title": "卖点", "content": "轻便耐用", "selected": True,
            }],
            "edges": [], "selected_node_ids": ["n1"], "history": [],
        }

    def test_device_codes_and_access_token_are_only_stored_as_hashes(self):
        start = self._start()
        with sqlite3.connect(self.auth.DB) as connection:
            row = connection.execute(
                "SELECT device_code_hash,user_code_hash,token_hash FROM cli_device_grants"
            ).fetchone()
        self.assertNotEqual(start["device_code"], row[0])
        self.assertNotEqual(start["user_code"], row[1])
        self.assertIsNone(row[2])
        self._approve(start)
        _, polled = self._request("/api/auth/cli/device/poll", {"device_code": start["device_code"]})
        with sqlite3.connect(self.auth.DB) as connection:
            stored = connection.execute("SELECT token_hash FROM cli_device_grants").fetchone()[0]
        self.assertNotEqual(polled["access_token"], stored)
        self.assertNotIn(polled["access_token"], Path(self.auth.DB).read_bytes().decode("latin1"))

    def test_approval_requires_same_origin_and_browser_cookie(self):
        start = self._start()
        status, info = self._request(
            "/api/auth/cli/device/info", {"user_code": start["user_code"]},
            browser=self.browser, origin=self.base,
        )
        self.assertEqual(200, status)
        self.assertEqual("test agent", info["client_name"])
        self.assertEqual(start["scopes"], info["scopes"])
        status, _ = self._request(
            "/api/auth/cli/device/approve", {"user_code": start["user_code"], "approve": True},
            browser=self.browser,
        )
        self.assertEqual(403, status)
        status, _ = self._request(
            "/api/auth/cli/device/approve", {"user_code": start["user_code"], "approve": True},
            origin=self.base,
        )
        self.assertEqual(401, status)
        status, payload = self._approve(start)
        self.assertEqual(200, status)
        self.assertEqual("approved", payload["status"])

    def test_cli_token_status_logout_and_web_token_isolation(self):
        token = self._token()
        status, payload = self._request("/api/auth/cli/status", token=token)
        self.assertEqual(200, status)
        self.assertEqual("alice", payload["user"]["username"])
        self.assertIn("generation:submit", payload["scopes"])
        status, _ = self._request("/api/auth/me", token=token)
        self.assertEqual(401, status)
        self.assertEqual(200, self._request("/api/auth/cli/logout", {}, token=token)[0])
        self.assertEqual(401, self._request("/api/auth/cli/status", token=token)[0])

    def test_denied_and_expired_device_grants_never_issue_tokens(self):
        denied = self._start()
        self.assertEqual("denied", self._approve(denied, False)[1]["status"])
        status, payload = self._request("/api/auth/cli/device/poll", {"device_code": denied["device_code"]})
        self.assertEqual(403, status)
        self.assertEqual("access_denied", payload["code"])
        expired = self._start()
        with sqlite3.connect(self.auth.DB) as connection:
            connection.execute("UPDATE cli_device_grants SET expires_at=0 WHERE device_code_hash=?",
                               (self.auth.hq_cli_api._hash(expired["device_code"]),))
            connection.commit()
        status, payload = self._request("/api/auth/cli/device/poll", {"device_code": expired["device_code"]})
        self.assertEqual(410, status)
        self.assertEqual("expired_token", payload["code"])

    def test_concurrent_poll_issues_exactly_one_access_token(self):
        start = self._start()
        self._approve(start)

        def poll():
            return self._request("/api/auth/cli/device/poll", {"device_code": start["device_code"]})

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: poll(), range(2)))
        self.assertEqual(sorted(status for status, _ in results), [200, 409])
        self.assertEqual(sum("access_token" in body for _, body in results), 1)

    def test_scope_enforcement_happens_before_business_proxy(self):
        token = self._token(["ip12:read"])
        with mock.patch.object(self.auth.hq_cli_api, "proxy_json") as proxy:
            status, payload = self._request("/api/auth/cli/action", {
                "action": "ip12-create", "input": {"title": "blocked"}, "confirm": True,
            }, token=token)
        self.assertEqual(403, status)
        self.assertEqual("insufficient_scope", payload["code"])
        proxy.assert_not_called()

    def test_fixed_read_proxy_uses_short_lived_web_token_and_deletes_it(self):
        token = self._token(["ip12:read"])
        captured = {}

        def fake_proxy(plan, web_token, internal_token):
            captured.update(plan=plan, web_token=web_token, internal_token=internal_token)
            return 200, {"items": [{"id": "p1"}]}

        with mock.patch.object(self.auth.hq_cli_api, "proxy_json", side_effect=fake_proxy):
            status, payload = self._request("/api/auth/cli/action", {
                "action": "ip12-projects", "input": {}, "confirm": False,
            }, token=token)
        self.assertEqual(200, status)
        self.assertEqual("p1", payload["items"][0]["id"])
        self.assertEqual(self.auth.hq_cli_api.HERMES_BASE, captured["plan"]["base"])
        self.assertEqual("/api/conversations", captured["plan"]["path"])
        self.assertNotEqual(token, captured["web_token"])
        with sqlite3.connect(self.auth.DB) as connection:
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM tokens WHERE token=?", (captured["web_token"],)
            ).fetchone()[0])

    def test_image_upload_requires_own_scope_confirmation_and_streams_raw_bytes(self):
        raw = b"\x89PNG\r\n\x1a\n" + b"private-image"
        denied = self._token(["assets:read"])
        with mock.patch.object(self.auth.hq_cli_api, "proxy_image_upload") as proxy:
            status, payload = self._raw_request("/api/auth/cli/image-upload", raw, token=denied)
        self.assertEqual(403, status)
        self.assertEqual("insufficient_scope", payload["code"])
        proxy.assert_not_called()

        token = self._token(["assets:upload"])
        status, payload = self._raw_request(
            "/api/auth/cli/image-upload", raw, token=token, confirm=False,
        )
        self.assertEqual(409, status)
        self.assertEqual("confirmation_required", payload["code"])

        busy_slots = mock.Mock()
        busy_slots.acquire.return_value = False
        with mock.patch.object(self.auth.hq_cli_api, "IMAGE_UPLOAD_SLOTS", busy_slots):
            status, payload = self._raw_request("/api/auth/cli/image-upload", raw, token=token)
        self.assertEqual(429, status)
        self.assertEqual("upload_busy", payload["code"])
        busy_slots.release.assert_not_called()

        captured = {}

        def fake_upload(stream, length, web_token, internal_token, content_type, digest):
            captured.update(
                raw=stream.read(length), web_token=web_token, internal_token=internal_token,
                content_type=content_type, digest=digest,
            )
            return 200, {"upload_id": "img_" + "a" * 32, "sha256": digest}

        with mock.patch.object(self.auth.hq_cli_api, "proxy_image_upload", side_effect=fake_upload):
            status, payload = self._raw_request("/api/auth/cli/image-upload", raw, token=token)
        self.assertEqual(200, status, payload)
        self.assertEqual(raw, captured["raw"])
        self.assertEqual("image/png", captured["content_type"])
        self.assertEqual(hashlib.sha256(raw).hexdigest(), captured["digest"])
        self.assertEqual(self.auth.INTERNAL_TOKEN, captured["internal_token"])
        with sqlite3.connect(self.auth.DB) as connection:
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM tokens WHERE token=?", (captured["web_token"],)
            ).fetchone()[0])

    def test_video_upload_requires_confirmation_and_streams_raw_bytes(self):
        raw = b"\x00\x00\x00\x18ftypisom" + b"private-video"
        token = self._token(["assets:upload"])
        status, payload = self._raw_request(
            "/api/auth/cli/video-upload", raw, token=token,
            content_type="video/mp4", confirm=False,
        )
        self.assertEqual((409, "confirmation_required"), (status, payload["code"]))

        captured = {}

        def fake_upload(stream, length, web_token, internal_token, content_type, digest):
            captured.update(raw=stream.read(length), content_type=content_type, digest=digest)
            return 200, {"upload_id": "vid_" + "a" * 32, "sha256": digest, "duration": 5.5}

        with mock.patch.object(self.auth.hq_cli_api, "proxy_video_upload", side_effect=fake_upload):
            status, payload = self._raw_request(
                "/api/auth/cli/video-upload", raw, token=token, content_type="video/mp4",
            )
        self.assertEqual(200, status, payload)
        self.assertEqual(raw, captured["raw"])
        self.assertEqual("video/mp4", captured["content_type"])
        self.assertEqual(hashlib.sha256(raw).hexdigest(), captured["digest"])

    def test_canvas_create_builds_one_safe_text_node(self):
        token = self._token(["canvas:write"])
        status, payload = self._request("/api/auth/cli/action", {
            "action": "canvas-create", "input": {"name": "Launch", "prompt": "first idea"}, "confirm": True,
        }, token=token)
        self.assertEqual(200, status, payload)
        board = payload["board"]
        self.assertEqual("Launch", board["name"])
        self.assertEqual("text", board["data"]["nodes"][0]["type"])
        self.assertEqual("first idea", board["data"]["nodes"][0]["outputs"]["prompt"])
        self.assertIn("collab=" + board["id"], payload["url"])

    def test_canvas_agent_plan_is_scoped_quoted_and_never_auto_applies(self):
        board, err = self.auth.create_canvas_board("alice", {
            "name": "Agent board",
            "data": {"nodes": [{"id": "n1", "type": "text", "params": {"text": "轻便耐用"}}], "edges": []},
        })
        self.assertIsNone(err)
        input_body = self._canvas_snapshot(board["id"])
        input_body.update(
            page_context={
                "page": "canvas", "path": "/workbench/canvas", "title": "黄雀画布",
                "can_edit": True, "selected_count": 1,
            },
            ip12_context={
                "project_id": "ip12_project_1", "title": "美业 IP", "status": "confirmed",
                "foundation_status": "confirmed", "facts": [{"label": "定位", "value": "主理人"}],
            },
        )
        denied = self._token(["generation:submit"])
        with mock.patch.object(self.auth.hq_cli_api, "proxy_json") as proxy:
            status, payload = self._request("/api/auth/cli/action", {
                "action": "canvas-agent-plan", "input": input_body, "confirm": False,
            }, token=denied)
        self.assertEqual(403, status)
        self.assertEqual("insufficient_scope", payload["code"])
        proxy.assert_not_called()

        quote_only = self._token(["canvas:agent"])
        with mock.patch.object(self.auth.hq_cli_api, "proxy_json", return_value=(
                200, {"kind": "canvas_agent", "cost": 3, "points": 100})):
            status, quote = self._request("/api/auth/cli/action", {
                "action": "canvas-agent-plan", "input": input_body, "confirm": False,
            }, token=quote_only)
            self.assertEqual(200, status, quote)
            status, payload = self._request("/api/auth/cli/action", {
                "action": "canvas-agent-plan", "input": input_body, "confirm": True,
                "quote_token": quote["quote_token"],
            }, token=quote_only)
        self.assertEqual(403, status)
        self.assertEqual("insufficient_scope", payload["code"])

        token = self._token(["canvas:agent", "generation:submit"])
        submitted = []

        def fake_proxy(plan, web_token, internal_token):
            if plan["path"] == "/api/gen/canvas-agent/quote":
                self.assertEqual({}, plan["body"])
                return 200, {"kind": "canvas_agent", "cost": 3, "points": 100}
            submitted.append(plan)
            return 200, {"job_id": 84, "cost": 3, "points_left": 97}

        request = {"action": "canvas-agent-plan", "input": input_body, "confirm": False}
        with mock.patch.object(self.auth.hq_cli_api, "proxy_json", side_effect=fake_proxy):
            status, quote = self._request("/api/auth/cli/action", request, token=token)
            self.assertEqual(200, status, quote)
            confirm = dict(request, confirm=True, quote_token=quote["quote_token"])
            status, result = self._request("/api/auth/cli/action", confirm, token=token)
            self.assertEqual(200, status, result)
            changed = dict(confirm, input={**input_body, "prompt": "不同任务"})
            mismatch_status, mismatch = self._request("/api/auth/cli/action", changed, token=token)
        self.assertEqual(409, mismatch_status)
        self.assertEqual("quote_mismatch", mismatch["code"])
        self.assertEqual(84, result["job_id"])
        self.assertEqual("/api/gen/canvas_agent", submitted[0]["path"])
        self.assertEqual(3, submitted[0]["body"]["quoted_cost"])
        self.assertEqual("美业 IP", submitted[0]["body"]["ip12_context"]["title"])
        self.assertEqual("canvas", submitted[0]["body"]["page_context"]["page"])
        self.assertEqual(board["id"], submitted[0]["headers"]["X-Canvas-Board-Id"])
        self.assertEqual("3", submitted[0]["headers"]["X-HQ-Expected-Cost"])
        self.assertTrue(submitted[0]["headers"]["Idempotency-Key"].startswith("hqcli-"))
        current, _ = self.auth.get_canvas_board("alice", board["id"])
        self.assertEqual(1, current["version"])

    def test_canvas_ops_are_confirmed_strict_and_idempotent(self):
        board, err = self.auth.create_canvas_board("alice", {
            "name": "CLI board",
            "data": {"nodes": [{"id": "n1", "type": "text", "params": {"text": "卖点"}}], "edges": []},
        })
        self.assertIsNone(err)
        action_input = {
            "board_id": board["id"], "base_version": 1, "op_id": "hqcli-abcdefghijkl",
            "ops": [
                {"type": "node.patch", "id": "n1", "fields": {"params": {"title": "核心卖点"}}},
                {"type": "node.create", "node": {
                    "id": "n2", "type": "gen", "x": 360, "y": 80,
                    "params": {"title": "图片草稿", "text": "轻便耐用的产品海报"},
                }},
                {"type": "edge.create", "edge": {
                    "from": {"node": "n1", "port": "prompt"},
                    "to": {"node": "n2", "port": "prompt"},
                }},
            ],
        }
        denied = self._token(["canvas:read"])
        status, payload = self._request("/api/auth/cli/action", {
            "action": "canvas-ops", "input": action_input, "confirm": True,
        }, token=denied)
        self.assertEqual(403, status)
        self.assertEqual("insufficient_scope", payload["code"])

        token = self._token(["canvas:edit"])
        status, payload = self._request("/api/auth/cli/action", {
            "action": "canvas-ops", "input": action_input, "confirm": False,
        }, token=token)
        self.assertEqual(409, status)
        self.assertEqual("confirmation_required", payload["code"])
        confirmed = {"action": "canvas-ops", "input": action_input, "confirm": True}
        status, result = self._request("/api/auth/cli/action", confirmed, token=token)
        self.assertEqual(200, status, result)
        self.assertEqual(2, result["version"])
        self.assertEqual(2, len(result["board"]["data"]["nodes"]))
        self.assertEqual(200, self._request("/api/auth/cli/action", confirmed, token=token)[0])

        changed = {**action_input, "ops": [
            {"type": "node.patch", "id": "n1", "fields": {"params": {"title": "其他内容"}}},
        ]}
        status, payload = self._request("/api/auth/cli/action", {
            "action": "canvas-ops", "input": changed, "confirm": True,
        }, token=token)
        self.assertEqual(409, status)
        self.assertEqual("idempotency_conflict", payload["code"])
        dangerous = {**action_input, "op_id": "hqcli-mnopqrstuvwx", "ops": [{"type": "node.delete", "id": "n1"}]}
        self.assertEqual(400, self._request("/api/auth/cli/action", {
            "action": "canvas-ops", "input": dangerous, "confirm": True,
        }, token=token)[0])
        stale = {**action_input, "op_id": "hqcli-zyxwvutsrqpo", "ops": [
            {"type": "node.patch", "id": "n1", "fields": {"x": 120}},
        ]}
        status, payload = self._request("/api/auth/cli/action", {
            "action": "canvas-ops", "input": stale, "confirm": True,
        }, token=token)
        self.assertEqual(409, status)
        self.assertEqual("canvas_version_conflict", payload["code"])
        current, _ = self.auth.get_canvas_board("alice", board["id"])
        self.assertEqual(2, current["version"])

    def test_paid_quote_binds_user_payload_cost_expiry_and_idempotency(self):
        token = self._token(["generation:quote", "generation:submit"])
        submitted = []

        def fake_proxy(plan, web_token, internal_token):
            if plan["path"] == "/api/gen/cli/quote":
                return 200, {"kind": "image", "cost": 24, "points": 100}
            submitted.append(plan)
            return 200, {"job_id": 42, "cost": 24, "points_left": 76}

        request = {"action": "image-generate", "input": {"prompt": "gold bird", "count": 2}, "confirm": False}
        with mock.patch.object(self.auth.hq_cli_api, "proxy_json", side_effect=fake_proxy):
            status, quote = self._request("/api/auth/cli/action", request, token=token)
            self.assertEqual(200, status, quote)
            confirm = dict(request, confirm=True, quote_token=quote["quote_token"])
            self.assertEqual(200, self._request("/api/auth/cli/action", confirm, token=token)[0])
            self.assertEqual(200, self._request("/api/auth/cli/action", confirm, token=token)[0])
            changed = dict(confirm, input={"prompt": "different", "count": 2})
            status, payload = self._request("/api/auth/cli/action", changed, token=token)
        self.assertEqual(409, status)
        self.assertEqual("quote_mismatch", payload["code"])
        self.assertEqual(submitted[0]["headers"]["Idempotency-Key"], submitted[1]["headers"]["Idempotency-Key"])
        self.assertEqual("24", submitted[0]["headers"]["X-HQ-Expected-Cost"])
        self.assertTrue(all(plan["internal"] for plan in submitted))

    def test_collect_and_leads_actions_are_strict_quoted_and_submit_to_leadgen(self):
        douyin = "https://v.douyin.com/abc123/"
        xhs = "https://www.xiaohongshu.com/explore/note-1"
        channels = "https://weixin.qq.com/sph/Abc123"
        channels_443 = "https://weixin.qq.com:443/sph/Abc123"
        bilibili = "https://b23.tv/keSUqLz"
        expected = {
            "collect-content": ("collect", "/api/gen/collect", {"url": xhs, "want": ["comments"]}),
            "collect-video": ("collect", "/api/gen/collect", {"url": douyin, "want": ["video"]}),
            "collect-transcript": ("collect", "/api/gen/collect", {"url": channels, "want": ["transcript"]}),
            "collect-search": ("collect_search", "/api/gen/collect_search",
                               {"platform": "xhs", "keyword": "轻食创业", "page": 2}),
            "leads-generate": ("leads", "/api/gen/leads", {
                "keyword": "美容院拓客", "platforms": ["douyin", "channels"],
                "count": 20, "pages": 1, "channels_targets": ["sph123"],
            }),
        }
        inputs = {
            "collect-content": {"url": xhs},
            "collect-video": {"url": douyin},
            "collect-transcript": {"url": channels},
            "collect-search": {"platform": "xhs", "keyword": "轻食创业", "page": 2},
            "leads-generate": {
                "keyword": "美容院拓客", "platforms": ["douyin", "channels"],
                "channels_targets": ["sph123"],
            },
        }
        for action, fields in expected.items():
            plan = self.auth.hq_cli_api.action_plan(action, inputs[action])
            self.assertEqual(
                ("generation:quote", fields[0], fields[1], self.auth.hq_cli_api.LEADGEN_BASE, fields[2]),
                (plan["scope"], plan["generation_kind"], plan["endpoint"],
                 plan["submit_base"], plan["payload"]),
            )
        self.assertEqual(
            channels_443,
            self.auth.hq_cli_api.action_plan("collect-video", {"url": channels_443})["payload"]["url"],
        )
        self.assertEqual(
            bilibili,
            self.auth.hq_cli_api.action_plan("collect-video", {"url": bilibili})["payload"]["url"],
        )
        with self.assertRaises(self.auth.hq_cli_api.CLIAPIError):
            self.auth.hq_cli_api.action_plan(
                "collect-video", {"url": "https://douyin.com.evil.example/video/1"})
        with self.assertRaises(self.auth.hq_cli_api.CLIAPIError):
            self.auth.hq_cli_api.action_plan(
                "collect-video", {"url": "https://mp.weixin.qq.com/s/Abc123"})
        for invalid_channels in (
                "http://weixin.qq.com/sph/Abc123",
                "https://weixin.qq.com/sph/",
                "https://weixin.qq.com/sph//Abc123",
                "https://weixin.qq.com/sph/../Abc123",
                "https://weixin.qq.com/sphx/Abc123",
                "https://weixin.qq.com/not-sph/Abc123",
                "https://evil.weixin.qq.com/sph/Abc123",
                "https://weixin.qq.com:80/sph/Abc123",
                "https://weixin.qq.com:444/sph/Abc123",
                "https://%75:%70@weixin.qq.com/sph/Abc123",
                "https://weixin.qq.com.evil.example/sph/Abc123"):
            with self.assertRaises(self.auth.hq_cli_api.CLIAPIError):
                self.auth.hq_cli_api.action_plan("collect-video", {"url": invalid_channels})
        with self.assertRaises(self.auth.hq_cli_api.CLIAPIError):
            self.auth.hq_cli_api.action_plan(
                "collect-search", {"platform": "douyin", "keyword": "越界页码", "page": 51})
        with self.assertRaises(self.auth.hq_cli_api.CLIAPIError):
            self.auth.hq_cli_api.action_plan(
                "leads-generate", {"platforms": ["channels"], "channels_targets": []})
        history = self.auth.hq_cli_api.action_plan("tasks", {"kind": "collect_search"})
        self.assertIn("kind=collect_search", history["path"])

        token = self._token(["generation:quote", "generation:submit"])
        submitted = []

        def fake_proxy(plan, _web_token, _internal_token):
            if plan["path"] == "/api/gen/cli/quote":
                return 200, {"kind": "collect", "cost": 3, "points": 100}
            submitted.append(plan)
            return 200, {"job_id": 88, "cost": 3, "points_left": 97}

        request = {"action": "collect-video", "input": {"url": douyin}, "confirm": False}
        with mock.patch.object(self.auth.hq_cli_api, "proxy_json", side_effect=fake_proxy):
            status, quote = self._request("/api/auth/cli/action", request, token=token)
            self.assertEqual(200, status, quote)
            status, result = self._request(
                "/api/auth/cli/action",
                dict(request, confirm=True, quote_token=quote["quote_token"]), token=token,
            )
        self.assertEqual((200, 88), (status, result["job_id"]))
        self.assertEqual(
            (self.auth.hq_cli_api.LEADGEN_BASE, "/api/gen/collect", {"url": douyin, "want": ["video"]}),
            (submitted[0]["base"], submitted[0]["path"], submitted[0]["body"]),
        )
        self.assertEqual("3", submitted[0]["headers"]["X-HQ-Expected-Cost"])

    def test_image_generation_accepts_only_valid_upload_id_combinations(self):
        upload_id = "img_" + "a" * 32
        plan = self.auth.hq_cli_api.action_plan("image-generate", {
            "prompt": "keep the person", "provider": "openai", "image_upload_id": upload_id,
        })
        self.assertEqual(upload_id, plan["payload"]["image_upload_id"])
        multi = self.auth.hq_cli_api.action_plan("image-generate", {
            "prompt": "use @图片1", "provider": "openai", "reference_upload_ids": [upload_id],
        })
        self.assertEqual([upload_id], multi["payload"]["reference_upload_ids"])
        video = self.auth.hq_cli_api.action_plan("video-generate", {
            "prompt": "use @图片1", "channel": "grok", "reference_upload_ids": [upload_id],
        })
        self.assertEqual([upload_id], video["payload"]["reference_upload_ids"])
        self.assertEqual("banana", plan["payload"]["source_page"])
        self.assertEqual("video", video["payload"]["source_page"])
        audio = self.auth.hq_cli_api.action_plan("audio-generate", {"text": "你好"})
        self.assertEqual("audio", audio["payload"]["source_page"])
        banana = self.auth.hq_cli_api.action_plan("image-generate", {
            "prompt": "海报", "provider": "banana", "model": "pro", "ratio": "21:9",
        })
        self.assertEqual(("banana", "pro"), (
            banana["payload"]["provider"], banana["payload"]["model"]))
        sora = self.auth.hq_cli_api.action_plan("video-generate", {
            "prompt": "海边日出", "channel": "sora", "model": "sora-2",
            "seconds": 8, "ratio": "16:9", "reference_upload_ids": [upload_id],
        })
        self.assertEqual(("sora_video", "/api/gen/sora_video", 8), (
            sora["generation_kind"], sora["endpoint"], sora["payload"]["seconds"]))
        with self.assertRaises(self.auth.hq_cli_api.CLIAPIError):
            self.auth.hq_cli_api.action_plan("image-generate", {
                "prompt": "bad", "provider": "seedream", "image_upload_id": upload_id,
                "mask_upload_id": "img_" + "b" * 32,
            })

    def test_digital_ip_and_cinematic_generation_plans_are_narrow_and_fixed(self):
        text = self.auth.hq_cli_api.action_plan("digital-ip-text-generate", {
            "avatar_id": 7, "text": "欢迎来到黄雀", "voice": "S_d21F8OR62",
            "ratio": "16:9", "motion": "high", "subtitle": True,
            "subtitle_style": "bar", "subtitle_position": "lower",
        })
        self.assertEqual(("generation:quote", "video", "/api/gen/video"), (
            text["scope"], text["generation_kind"], text["endpoint"]))
        self.assertEqual(("text", 7, "1080p", True), (
            text["payload"]["mode"], text["payload"]["avatar_id"],
            text["payload"]["resolution"], text["payload"]["subtitle"]))

        asset_audio = self.auth.hq_cli_api.action_plan("digital-ip-audio-generate", {
            "avatar_id": 8, "audio_file": "audio/voice-owned.mp3",
        })
        self.assertEqual(("audio", "audio/voice-owned.mp3"), (
            asset_audio["payload"]["mode"], asset_audio["payload"]["audio_file"]))
        self.assertNotIn("audio_data", asset_audio["payload"])

        cinematic = self.auth.hq_cli_api.action_plan("cinematic-open-generate", {
            "avatar_ids": [7, 8], "prompt": "两人在工作室自然交谈",
            "ratio": "1:1", "duration": 12, "enhance_prompt": True,
        })
        self.assertEqual(("cinematic", "/api/gen/cinematic"), (
            cinematic["generation_kind"], cinematic["endpoint"]))
        self.assertEqual(("open", [7, 8], "720p", 12), (
            cinematic["payload"]["cine_mode"], cinematic["payload"]["avatar_ids"],
            cinematic["payload"]["resolution"], cinematic["payload"]["duration"]))
        self.assertNotIn("reference_videos", cinematic["payload"])

        for avatar_ids, allowed in (([7], 8), ([7, 8], 7), ([7, 8, 9], 6)):
            accepted = self.auth.hq_cli_api.action_plan("cinematic-open-generate", {
                "avatar_ids": avatar_ids, "prompt": "共享参考图额度",
                "reference_image_upload_ids": ["img_" + str(index) * 32 for index in range(allowed)],
            })
            self.assertEqual(allowed, len(accepted["payload"]["reference_image_upload_ids"]))
            with self.assertRaisesRegex(self.auth.hq_cli_api.CLIAPIError, "共用 9 张额度"):
                self.auth.hq_cli_api.action_plan("cinematic-open-generate", {
                    "avatar_ids": avatar_ids, "prompt": "超出共享参考图额度",
                    "reference_image_upload_ids": [
                        "img_" + str(index) * 32 for index in range(allowed + 1)
                    ],
                })

        with self.assertRaises(self.auth.hq_cli_api.CLIAPIError):
            self.auth.hq_cli_api.action_plan("digital-ip-text-generate", {
                "avatar_id": 7, "text": "越权输入", "voice": "S_d21F8OR62",
                "image_data": "data:image/png;base64,AAAA",
            })
        with self.assertRaises(self.auth.hq_cli_api.CLIAPIError):
            self.auth.hq_cli_api.action_plan("cinematic-open-generate", {
                "avatar_ids": [7, 7], "prompt": "重复形象",
            })

    def test_new_video_actions_reuse_signed_quote_confirm_contract(self):
        cases = (
            ("digital-ip-text-generate", {
                "avatar_id": 11, "text": "你好", "voice": "S_d21F8OR62",
            }, "video", "/api/gen/video", 30),
            ("digital-ip-audio-generate", {
                "avatar_id": 11, "audio_file": "audio/owned.wav",
            }, "video", "/api/gen/video", 30),
            ("cinematic-open-generate", {
                "avatar_ids": [11], "prompt": "在工作室自然交流", "duration": 8,
            }, "cinematic", "/api/gen/cinematic", 16),
        )
        for action, input_body, kind, endpoint, cost in cases:
            with self.subTest(action=action):
                token = self._token(["generation:quote", "generation:submit"])
                submitted = []

                def fake_proxy(plan, web_token, internal_token):
                    if plan["path"] == "/api/gen/cli/quote":
                        self.assertEqual(kind, plan["body"]["kind"])
                        return 200, {"kind": kind, "cost": cost, "points": 100}
                    submitted.append(plan)
                    return 200, {"job_id": 91, "cost": cost, "points_left": 100 - cost}

                request = {"action": action, "input": input_body, "confirm": False}
                with mock.patch.object(
                        self.auth.hq_cli_api, "proxy_json", side_effect=fake_proxy):
                    status, quote = self._request(
                        "/api/auth/cli/action", request, token=token)
                    self.assertEqual(200, status, quote)
                    status, result = self._request(
                        "/api/auth/cli/action", dict(
                            request, confirm=True, quote_token=quote["quote_token"]),
                        token=token,
                    )
                self.assertEqual((200, 91), (status, result["job_id"]))
                self.assertEqual(endpoint, submitted[0]["path"])
                self.assertEqual(
                    str(cost), submitted[0]["headers"]["X-HQ-Expected-Cost"])
                self.assertTrue(
                    submitted[0]["headers"]["Idempotency-Key"].startswith("hqcli-"))

    def test_batch_motion_and_tryon_plans_bind_private_upload_ids(self):
        image_id = "img_" + "a" * 32
        video_id = "vid_" + "b" * 32
        batch = self.auth.hq_cli_api.action_plan("digital-ip-batch-generate", {
            "avatars": [{"avatar_id": 1, "label": "主理人"}, {"avatar_id": 2}],
            "text": "欢迎到店", "voice": "owned-voice",
        })
        self.assertEqual(("video_batch", "/api/gen/video/batch"), (
            batch["generation_kind"], batch["endpoint"]))
        self.assertEqual([1, 2], [item["avatar_id"] for item in batch["payload"]["avatars"]])

        motion = self.auth.hq_cli_api.action_plan("cinematic-motion-generate", {
            "avatar_id": 3, "reference_video_upload_ids": [video_id], "ratio": "9:16",
        })
        self.assertEqual("motion", motion["payload"]["cine_mode"])
        self.assertEqual([video_id], motion["payload"]["reference_video_upload_ids"])

        fast = self.auth.hq_cli_api.action_plan("tryon-fast-generate", {
            "person_image_upload_id": image_id, "clothes_upload_id": image_id,
        })
        classic = self.auth.hq_cli_api.action_plan("tryon-classic-generate", {
            "person_video_upload_id": video_id, "background_upload_id": image_id,
        })
        self.assertEqual(("2", "tryon"), (fast["payload"]["line"], fast["generation_kind"]))
        self.assertEqual(("1", video_id), (
            classic["payload"]["line"], classic["payload"]["person_video_upload_id"]))

        with self.assertRaises(self.auth.hq_cli_api.CLIAPIError):
            self.auth.hq_cli_api.action_plan("tryon-classic-generate", {
                "person_video_upload_id": video_id,
            })

    def test_customer_read_actions_use_fixed_owner_scoped_routes(self):
        cases = {
            "pricing": ("profile:read", "/api/gen/pricing"),
            "text-video-capability": ("assets:read", "/api/gen/text-video/capability"),
            "text-video-templates": ("assets:read", "/api/gen/text-video/templates"),
            "text-video-styles": ("assets:read", "/api/gen/text-video/styles"),
            "text-video-voices": ("assets:read", "/api/gen/text-video/voices"),
            "digital-ip-projects": ("ip12:read", "/api/gen/digital-ip/projects"),
        }
        for action, expected in cases.items():
            plan = self.auth.hq_cli_api.action_plan(action, {})
            self.assertEqual(expected, (plan["scope"], plan["path"]))
            self.assertEqual(self.auth.hq_cli_api.CONTENT_BASE, plan["base"])
        project = self.auth.hq_cli_api.action_plan(
            "digital-ip-project", {"project_id": "project_1"})
        report = self.auth.hq_cli_api.action_plan(
            "digital-ip-report", {"project_id": "project_1"})
        self.assertEqual("/api/gen/digital-ip/projects/project_1", project["path"])
        self.assertEqual("/api/gen/digital-ip/projects/project_1/report", report["path"])

    def test_safe_customer_actions_use_fixed_routes_and_strict_inputs(self):
        cases = {
            "inspiration-catalog": ("inspiration:read", self.auth.hq_cli_api.ADMIN_BASE,
                                    "/api/admin/public/inspirations"),
            "inspiration-likes": ("inspiration:read", self.auth.hq_cli_api.CONTENT_BASE,
                                  "/api/gen/inspiration/likes"),
            "video-avatars": ("assets:read", self.auth.hq_cli_api.CONTENT_BASE,
                              "/api/gen/video/avatars?limit=120"),
            "audio-slots": ("assets:read", self.auth.hq_cli_api.CONTENT_BASE,
                            "/api/gen/audio/slots?include_points=0"),
        }
        for action, expected in cases.items():
            plan = self.auth.hq_cli_api.action_plan(action, {})
            self.assertEqual(expected, (plan["scope"], plan["base"], plan["path"]))
        lead_id = "a" * 16
        crm = self.auth.hq_cli_api.action_plan("leads-crm", {"lead_ids": [lead_id, lead_id]})
        self.assertEqual("/api/gen/leads/crm?ids=" + lead_id, crm["path"])
        update = self.auth.hq_cli_api.action_plan("leads-crm-upsert", {
            "lead_id": lead_id, "intent": "咨询", "follow_status": "跟进中", "follow_note": "明天回访",
        })
        self.assertEqual(("leads:write", "POST", lead_id),
                         (update["scope"], update["method"], update["body"]["lead_id"]))
        like = self.auth.hq_cli_api.action_plan("inspiration-like", {"id": 7, "favorite": True})
        self.assertEqual({"id": 7, "favorite": True}, like["body"])
        projects = self.auth.hq_cli_api.action_plan("short-drama-projects", {"page": 2, "page_size": 50})
        self.assertEqual("/api/gen/short-drama/projects?page=2&page_size=50", projects["path"])
        for action, suffix in (("short-drama-project", "project?id="),
                               ("short-drama-conversation", "conversation?project_id="),
                               ("short-drama-preflight", "preflight?project_id=")):
            plan = self.auth.hq_cli_api.action_plan(action, {"project_id": "project_1"})
            self.assertEqual("short-drama:read", plan["scope"])
            self.assertEqual("/api/gen/short-drama/" + suffix + "project_1", plan["path"])
        with self.assertRaises(self.auth.hq_cli_api.CLIAPIError):
            self.auth.hq_cli_api.action_plan("leads-crm", {"lead_ids": ["not-a-lead"]})

    def test_channels_use_customer_account_authorization_and_include_minimax(self):
        token = self._token(["profile:read"])
        status, payload = self._request("/api/auth/cli/action", {
            "action": "channels", "input": {}, "confirm": False,
        }, token=token)
        self.assertEqual(200, status)
        self.assertEqual(16, payload["total"])
        self.assertEqual("alice", payload["account"])
        channels = {item["id"]: item for item in payload["channels"]}
        self.assertEqual({"channel": "sora"}, channels["openai"]["selectors"][1]["input"])
        self.assertEqual({"provider": "banana"}, channels["gemini"]["selectors"][0]["input"])
        self.assertTrue({
            "digital-ip-text-generate", "digital-ip-audio-generate",
            "cinematic-open-generate",
        }.issubset(channels["heygen"]["capabilities"]))
        self.assertEqual("mixed", channels["tikhub"]["access"])
        self.assertTrue({
            "collect-content", "collect-video", "collect-transcript", "collect-search", "leads-generate",
        }.issubset(channels["tikhub"]["capabilities"]))
        self.assertEqual(
            {"channel": "minimax", "resolution": "768p"},
            {k: self.auth.hq_cli_api.action_plan("video-generate", {
                "prompt": "人物故事", "channel": "minimax",
            })["payload"][k] for k in ("channel", "resolution")},
        )

    def test_server_requires_confirmation_for_external_ai_and_writes(self):
        token = self._token(["prompt:optimize", "ip12:write", "ip12:chat", "canvas:write", "assets:write",
                             "video-compose:write", "digital-presenter:write", "inspiration:write", "leads:write"])
        cases = [
            ("prompt-optimize", {"prompt": "portrait", "kind": "image"}),
            ("ip12-create", {"title": "my project"}),
            ("ip12-message", {"project_id": "ip_1", "message": "我的客户是餐饮老板", "request_id": "turn-001"}),
            ("canvas-create", {"name": "my board"}),
            ("asset-tags", {"kind": "image", "key": "asset-1", "tags": ["客户案例"]}),
            ("video-compose-create", {"source_asset_id": 7}),
            ("digital-presenter-create", {"board_id": "cb_1", "request_id": "hqcli-dp-001"}),
            ("inspiration-like", {"id": 7, "favorite": True}),
            ("leads-crm-upsert", {"lead_id": "a" * 16, "follow_status": "跟进中"}),
        ]
        with mock.patch.object(self.auth.hq_cli_api, "proxy_json") as proxy:
            for action, input_body in cases:
                status, payload = self._request("/api/auth/cli/action", {
                    "action": action, "input": input_body, "confirm": False,
                }, token=token)
                self.assertEqual(409, status)
                self.assertEqual("confirmation_required", payload["code"])
        proxy.assert_not_called()

    def test_new_project_actions_use_fixed_routes_headers_and_strict_inputs(self):
        compose = self.auth.hq_cli_api.action_plan("video-compose-review", {
            "project_id": "compose_" + "a" * 32, "expected_revision": 3,
            "decisions": {"candidate_" + "b" * 16: "remove"},
        })
        self.assertEqual(("video-compose:write", "POST"), (compose["scope"], compose["method"]))
        self.assertTrue(compose["path"].endswith("/edit-decisions"))
        presenter = self.auth.hq_cli_api.action_plan("digital-presenter-create", {
            "board_id": "cb_1", "request_id": "hqcli-dp-001", "title": "口播一号",
        })
        self.assertEqual("cb_1", presenter["headers"]["X-Canvas-Board-Id"])
        self.assertEqual("hqcli-dp-001", presenter["headers"]["Idempotency-Key"])
        with self.assertRaises(self.auth.hq_cli_api.CLIAPIError):
            self.auth.hq_cli_api.action_plan("video-compose-review", {
                "project_id": "compose_" + "a" * 32, "expected_revision": 3,
                "decisions": {"candidate_" + "b" * 16: "maybe"},
            })

    def test_ip12_message_has_separate_scope_and_fixed_non_streaming_proxy(self):
        message = "我的客户是餐饮老板\n我想分两段说明"
        input_body = {"project_id": "ip_1", "message": message, "request_id": "turn-001"}
        with mock.patch.object(self.auth.hq_cli_api, "proxy_json") as proxy:
            token = self._token(["ip12:write"])
            status, payload = self._request("/api/auth/cli/action", {
                "action": "ip12-message", "input": input_body, "confirm": True,
            }, token=token)
            self.assertEqual(403, status)
            self.assertEqual("insufficient_scope", payload["code"])
            proxy.assert_not_called()

            token = self._token(["ip12:chat"])
            proxy.return_value = (200, {"ok": True, "assistant": "继续回答"})
            status, payload = self._request("/api/auth/cli/action", {
                "action": "ip12-message", "input": input_body, "confirm": True,
            }, token=token)
        self.assertEqual(200, status)
        self.assertEqual("继续回答", payload["assistant"])
        plan = proxy.call_args.args[0]
        self.assertEqual((self.auth.hq_cli_api.HERMES_BASE, "/api/chat-complete", "POST", 290),
                         (plan["base"], plan["path"], plan["method"], plan["timeout"]))
        self.assertEqual({"conversation_id": "ip_1", "message": message}, plan["body"])
        self.assertEqual("turn-001", plan["headers"]["Idempotency-Key"])

        with self.assertRaises(self.auth.hq_cli_api.CLIAPIError):
            self.auth.hq_cli_api.action_plan("ip12-message", dict(input_body, message="正常文字\x00非法控制符"))

        status, replay = self._request("/api/auth/cli/action", {
            "action": "ip12-message", "input": input_body, "confirm": True,
        }, token=token)
        self.assertEqual(200, status)
        self.assertTrue(replay["replayed"])
        self.assertEqual(1, proxy.call_count)
        changed = dict(input_body, message="另一条回答")
        status, conflict = self._request("/api/auth/cli/action", {
            "action": "ip12-message", "input": changed, "confirm": True,
        }, token=token)
        self.assertEqual(409, status)
        self.assertEqual("idempotency_conflict", conflict["code"])

    def test_ip12_message_blocks_same_project_inflight_and_limits_rate(self):
        action = "ip12-message"
        claim = self.auth.hq_cli_api.begin_action_request(
            self.auth.db, "alice", action, "turn-1", "ip_1", "hash-1", now=100,
        )
        self.assertEqual(("new", None), claim)
        self.assertEqual(("in_progress", None), self.auth.hq_cli_api.begin_action_request(
            self.auth.db, "alice", action, "turn-1", "ip_1", "hash-1", now=101,
        ))
        self.assertEqual(("busy", None), self.auth.hq_cli_api.begin_action_request(
            self.auth.db, "alice", action, "turn-2", "ip_1", "hash-2", now=102,
        ))
        self.auth.hq_cli_api.finish_action_request(self.auth.db, "alice", action, "turn-1", 200, now=103)
        for number in range(2, 7):
            self.assertEqual(("new", None), self.auth.hq_cli_api.begin_action_request(
                self.auth.db, "alice", action, "turn-%s" % number, "ip_%s" % number,
                "hash-%s" % number, now=104 + number,
            ))
            self.auth.hq_cli_api.finish_action_request(
                self.auth.db, "alice", action, "turn-%s" % number, 200, now=105 + number,
            )
        self.assertEqual(("rate_limited", None), self.auth.hq_cli_api.begin_action_request(
            self.auth.db, "alice", action, "turn-7", "ip_7", "hash-7", now=112,
        ))

    def test_ip12_message_uncertain_result_blocks_fresh_project_request(self):
        token = self._token(["ip12:chat"])
        first = {"project_id": "ip_1", "message": "第一轮回答", "request_id": "turn-001"}
        second = {"project_id": "ip_1", "message": "第二轮回答", "request_id": "turn-002"}
        with mock.patch.object(self.auth.hq_cli_api, "proxy_json", side_effect=TimeoutError("lost response")) as proxy:
            status, payload = self._request("/api/auth/cli/action", {
                "action": "ip12-message", "input": first, "confirm": True,
            }, token=token)
            self.assertEqual(500, status)
            self.assertEqual("cli_internal_error", payload["code"])
            status, payload = self._request("/api/auth/cli/action", {
                "action": "ip12-message", "input": second, "confirm": True,
            }, token=token)
        self.assertEqual(409, status)
        self.assertEqual("result_unknown", payload["code"])
        self.assertEqual(1, proxy.call_count)

    def test_asset_offset_reaches_every_backend(self):
        for kind in ("image", "audio", "video"):
            plan = self.auth.hq_cli_api.action_plan("assets", {"kind": kind, "limit": 10, "offset": 20})
            self.assertIn("limit=10", plan["path"])
            self.assertIn("offset=20", plan["path"])


if __name__ == "__main__":
    unittest.main()
