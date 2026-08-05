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

    def _request(self, path, payload=None, token="", browser=None, origin=None, method=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        if origin:
            headers["Origin"] = origin
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
            "X-HQ-Image-SHA256": hashlib.sha256(raw).hexdigest(),
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
        with self.assertRaises(self.auth.hq_cli_api.CLIAPIError):
            self.auth.hq_cli_api.action_plan("image-generate", {
                "prompt": "bad", "provider": "seedream", "image_upload_id": upload_id,
                "mask_upload_id": "img_" + "b" * 32,
            })

    def test_channels_use_customer_account_authorization_and_include_minimax(self):
        token = self._token(["profile:read"])
        status, payload = self._request("/api/auth/cli/action", {
            "action": "channels", "input": {}, "confirm": False,
        }, token=token)
        self.assertEqual(200, status)
        self.assertEqual(15, payload["total"])
        self.assertEqual("alice", payload["account"])
        self.assertEqual(
            {"channel": "minimax", "resolution": "768p"},
            {k: self.auth.hq_cli_api.action_plan("video-generate", {
                "prompt": "人物故事", "channel": "minimax",
            })["payload"][k] for k in ("channel", "resolution")},
        )

    def test_server_requires_confirmation_for_external_ai_and_writes(self):
        token = self._token(["prompt:optimize", "ip12:write", "ip12:chat", "canvas:write", "assets:write",
                             "video-compose:write", "digital-presenter:write"])
        cases = [
            ("prompt-optimize", {"prompt": "portrait", "kind": "image"}),
            ("ip12-create", {"title": "my project"}),
            ("ip12-message", {"project_id": "ip_1", "message": "我的客户是餐饮老板", "request_id": "turn-001"}),
            ("canvas-create", {"name": "my board"}),
            ("asset-tags", {"kind": "image", "key": "asset-1", "tags": ["客户案例"]}),
            ("video-compose-create", {"source_asset_id": 7}),
            ("digital-presenter-create", {"board_id": "cb_1", "request_id": "hqcli-dp-001"}),
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
        input_body = {"project_id": "ip_1", "message": "我的客户是餐饮老板", "request_id": "turn-001"}
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
        self.assertEqual({"conversation_id": "ip_1", "message": "我的客户是餐饮老板"}, plan["body"])
        self.assertEqual("turn-001", plan["headers"]["Idempotency-Key"])

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
