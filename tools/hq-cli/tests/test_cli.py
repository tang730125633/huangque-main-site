import io
import hashlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hq_cli import cli, client


class HqCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"HQ_CLI_CONFIG_DIR": self.temp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def invoke(self, argv, stdin=b""):
        stdout, stderr = io.StringIO(), io.StringIO()
        input_stream = type("Input", (), {"buffer": io.BytesIO(stdin)})()
        with patch("sys.stdout", stdout), patch("sys.stderr", stderr), patch("sys.stdin", input_stream):
            code = cli.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    @staticmethod
    def payload(output):
        return json.loads(output)

    def authorize(self):
        client.save_credentials("t" * 43, 2000000000, cli.LOGIN_SCOPES)

    def test_help_version_and_discovery_are_json(self):
        for argv in ([], ["help"], ["-h"], ["version", "--help"], ["capabilities", "--json"]):
            code, output, error = self.invoke(argv)
            self.assertEqual(0, code, error)
            self.assertTrue(self.payload(output)["schema"].startswith("hq."))
        code, output, _ = self.invoke(["version"])
        self.assertEqual("Huangque main-site CLI", self.payload(output)["product"])
        self.assertEqual("https://huangquechuanmei.com", self.payload(output)["origin"])

    def test_all_requested_authenticated_capabilities_are_available(self):
        _, output, _ = self.invoke(["capabilities"])
        by_id = {item["id"]: item for item in self.payload(output)["capabilities"]}
        expected = {
            "account", "channels", "ip12-projects", "ip12-project", "ip12-create", "ip12-report", "ip12-message",
            "prompt-optimize", "canvas-list", "canvas-get", "canvas-create", "canvas-agent-plan", "canvas-ops", "tasks", "task",
            "assets", "voices", "image-upload", "asset-favorite", "asset-tags",
            "image-generate", "video-generate", "audio-generate",
        }
        self.assertTrue(expected <= set(by_id))
        self.assertTrue(all(by_id[item]["availability"] == "available" for item in expected))
        self.assertTrue(all(by_id[item]["runnable"] for item in expected))
        self.assertEqual("server_quote", by_id["image-generate"]["cost"]["kind"])
        self.assertEqual("hq_device_authorization", by_id["ip12-projects"]["target_auth"])
        self.assertEqual("assets:upload", by_id["image-upload"]["required_scope"])
        self.assertEqual("server_quote", by_id["canvas-agent-plan"]["cost"]["kind"])
        self.assertEqual("canvas:edit", by_id["canvas-ops"]["required_scope"])
        self.assertEqual(12, by_id["canvas-ops"]["input_schema"]["properties"]["ops"]["maxItems"])
        self.assertIn("minimax", by_id["video-generate"]["input_schema"]["properties"]["channel"]["enum"])

    def test_channels_command_uses_current_authorized_account(self):
        self.authorize()
        response = {"total": 15, "account": "alice", "channels": [{"id": "xai"}]}
        with patch("hq_cli.client.request_json", return_value=(200, response)) as request:
            code, output, error = self.invoke(["channels", "--json"])
        self.assertEqual(0, code, error)
        self.assertEqual(15, self.payload(output)["result"]["total"])
        self.assertEqual({"action": "channels", "input": {}, "confirm": False}, request.call_args.kwargs["body"])
        self.assertEqual("t" * 43, request.call_args.kwargs["token"])

    def test_login_uses_device_flow_saves_token_without_printing_it(self):
        responses = [
            (200, {"device_code": "device-secret", "user_code": "ABCD-EFGH",
                   "verification_uri": "https://huangquechuanmei.com/workbench/device?user_code=ABCD-EFGH",
                   "expires_in": 600, "interval": 3, "scopes": cli.LOGIN_SCOPES}),
            (202, {"detail": "pending", "code": "authorization_pending"}),
            (200, {"access_token": "s" * 43, "expires_in": 28800, "scopes": cli.LOGIN_SCOPES}),
            (200, {"user": {"username": "alice", "points": 88}, "scopes": cli.LOGIN_SCOPES,
                   "expires_at": 2000000000}),
        ]
        with patch("hq_cli.client.request_json", side_effect=responses) as request, \
                patch("hq_cli.cli.time.sleep"), patch("hq_cli.cli.webbrowser.open", return_value=True):
            code, output, progress = self.invoke(["login"])
        self.assertEqual(0, code, progress)
        self.assertEqual("alice", self.payload(output)["result"]["user"]["username"])
        self.assertEqual("ABCD-EFGH", self.payload(progress)["user_code"])
        self.assertNotIn("device-secret", output + progress)
        self.assertNotIn("s" * 43, output + progress)
        self.assertEqual("s" * 43, client.load_credentials()["access_token"])
        self.assertEqual(4, request.call_count)

    def test_credentials_are_private_and_logout_revokes_then_deletes(self):
        self.authorize()
        mode = stat.S_IMODE(os.stat(client.credentials_path()).st_mode)
        self.assertEqual(0o600, mode)
        with patch("hq_cli.client.request_json", return_value=(200, {"ok": True})) as request:
            code, output, error = self.invoke(["logout"])
        self.assertEqual(0, code, error)
        self.assertTrue(self.payload(output)["revoked"])
        self.assertFalse(client.credentials_path().exists())
        self.assertEqual("/api/auth/cli/logout", request.call_args.args[0])

    def test_status_requires_authorization_and_never_accepts_password_input(self):
        code, output, error = self.invoke(["status"])
        self.assertEqual(cli.EXIT_AUTH, code)
        self.assertEqual("auth_required", self.payload(error)["error"])
        code, output, error = self.invoke(["login", "--password", "secret"])
        self.assertEqual(cli.EXIT_USAGE, code)
        self.assertNotIn("secret", error)

    def test_authenticated_read_uses_fixed_action_and_saved_token(self):
        self.authorize()
        with patch("hq_cli.client.request_json", return_value=(200, {"items": [{"id": "p1"}]})) as request:
            code, output, error = self.invoke(["run", "ip12-projects"])
        self.assertEqual(0, code, error)
        self.assertEqual("p1", self.payload(output)["result"]["items"][0]["id"])
        self.assertEqual("/api/auth/cli/action", request.call_args.args[0])
        self.assertEqual({"action": "ip12-projects", "input": {}, "confirm": False}, request.call_args.kwargs["body"])
        self.assertEqual("t" * 43, request.call_args.kwargs["token"])
        self.assertEqual(120, request.call_args.kwargs["timeout"])

    def test_external_ai_and_write_actions_require_explicit_confirmation_before_http(self):
        self.authorize()
        inputs = {
            "prompt-optimize": b'{"prompt":"better portrait","kind":"image"}',
            "ip12-create": b'{"title":"My IP"}',
            "ip12-message": '{"project_id":"ip_1","message":"我的核心客户是本地餐饮老板","request_id":"turn-001"}'.encode(),
            "canvas-create": b'{"name":"Launch","prompt":"first idea"}',
            "canvas-ops": b'{"board_id":"cb_1","base_version":1,"op_id":"hqcli-abcdefghijkl","ops":[{"type":"node.patch","id":"n1","fields":{"x":120}}]}',
            "asset-tags": '{"kind":"image","key":"asset-1","tags":["客户案例"]}'.encode(),
            "video-compose-review": ('{"project_id":"compose_%s","expected_revision":2,'
                                       '"decisions":{"candidate_%s":"remove"}}' % ("a" * 32, "b" * 16)).encode(),
            "digital-presenter-create": b'{"board_id":"cb_1","request_id":"hqcli-dp-001"}',
        }
        with patch("hq_cli.client.request_json") as request:
            for capability, raw in inputs.items():
                code, output, error = self.invoke(["run", capability, "--input", "@-"], raw)
                self.assertEqual(cli.EXIT_CONFIRMATION, code)
                self.assertEqual("confirmation_required", self.payload(error)["error"])
        request.assert_not_called()

    def test_video_compose_decisions_reject_invalid_object_values_before_http(self):
        self.authorize()
        raw = ('{"project_id":"compose_%s","expected_revision":2,'
               '"decisions":{"candidate_%s":"maybe"}}' % ("a" * 32, "b" * 16)).encode()
        with patch("hq_cli.client.request_json") as request:
            code, _, error = self.invoke(["run", "video-compose-review", "--input", "@-"], raw)
        self.assertEqual(cli.EXIT_INPUT, code)
        self.assertEqual("input_error", self.payload(error)["error"])
        request.assert_not_called()

    def test_confirmed_ip12_message_calls_fixed_action_with_long_timeout(self):
        self.authorize()
        with patch("hq_cli.client.request_json", return_value=(200, {"assistant": "继续回答", "state": {}})) as request:
            code, output, error = self.invoke(
                ["run", "ip12-message", "--input", "@-", "--confirm"],
                b'{"project_id":"ip_1","message":"my customer is a restaurant owner","request_id":"turn-001"}',
            )
        self.assertEqual(0, code, error)
        self.assertEqual("继续回答", self.payload(output)["result"]["assistant"])
        self.assertEqual({
            "action": "ip12-message",
            "input": {"project_id": "ip_1", "message": "my customer is a restaurant owner", "request_id": "turn-001"},
            "confirm": True,
        }, request.call_args.kwargs["body"])
        self.assertEqual(310, request.call_args.kwargs["timeout"])

    def test_confirmed_canvas_create_calls_server_action(self):
        self.authorize()
        with patch("hq_cli.client.request_json", return_value=(200, {"board": {"id": "cb_1"}, "url": "https://huangquechuanmei.com/workbench/canvas?collab=cb_1"})) as request:
            code, output, error = self.invoke(
                ["run", "canvas-create", "--input", "@-", "--confirm"],
                b'{"name":"Launch","prompt":"first idea"}',
            )
        self.assertEqual(0, code, error)
        self.assertEqual("cb_1", self.payload(output)["result"]["board"]["id"])
        self.assertTrue(request.call_args.kwargs["body"]["confirm"])

    def test_paid_generation_is_quote_then_same_input_confirm(self):
        self.authorize()
        quote = {"quote_token": "q.abc", "kind": "image", "cost": 24, "points": 100,
                 "expires_in": 300, "confirmation_required": True}
        with patch("hq_cli.client.request_json", side_effect=[(200, quote), (200, {"job_id": 42, "cost": 24, "points_left": 76})]) as request:
            code, output, error = self.invoke(
                ["run", "image-generate", "--input", "@-"], b'{"prompt":"gold bird","count":2}',
            )
            self.assertEqual(0, code, error)
            self.assertEqual(24, self.payload(output)["result"]["cost"])
            code, output, error = self.invoke(
                ["run", "image-generate", "--input", "@-", "--confirm", "--quote-token", "q.abc"],
                b'{"prompt":"gold bird","count":2}',
            )
        self.assertEqual(0, code, error)
        self.assertEqual(42, self.payload(output)["result"]["job_id"])
        first, second = request.call_args_list
        self.assertFalse(first.kwargs["body"]["confirm"])
        self.assertTrue(second.kwargs["body"]["confirm"])
        self.assertEqual("q.abc", second.kwargs["body"]["quote_token"])
        self.assertEqual(first.kwargs["body"]["input"], second.kwargs["body"]["input"])

    def test_canvas_agent_plan_uses_paid_flow_without_auto_writing(self):
        self.authorize()
        snapshot = {
            "prompt": "创建图片草稿", "project_id": "collab:cb_1", "snapshot_digest": "deadbeef",
            "scope": "collab", "nodes": [{
                "id": "n1", "type": "text", "title": "卖点", "content": "轻便", "selected": True,
            }], "edges": [], "selected_node_ids": ["n1"], "history": [],
        }
        raw = json.dumps(snapshot, ensure_ascii=False).encode()
        quote = {"quote_token": "q.canvas", "kind": "canvas_agent", "cost": 3, "points": 100,
                 "expires_in": 300, "confirmation_required": True}
        with patch("hq_cli.client.request_json", side_effect=[
                (200, quote), (200, {"job_id": 84, "cost": 3, "points_left": 97})]) as request:
            code, output, error = self.invoke(["run", "canvas-agent-plan", "--input", "@-"], raw)
            self.assertEqual(0, code, error)
            self.assertEqual(3, self.payload(output)["result"]["cost"])
            code, output, error = self.invoke([
                "run", "canvas-agent-plan", "--input", "@-", "--confirm", "--quote-token", "q.canvas",
            ], raw)
        self.assertEqual(0, code, error)
        self.assertEqual(84, self.payload(output)["result"]["job_id"])
        first, second = request.call_args_list
        self.assertEqual("canvas-agent-plan", first.kwargs["body"]["action"])
        self.assertFalse(first.kwargs["body"]["confirm"])
        self.assertTrue(second.kwargs["body"]["confirm"])
        self.assertEqual(first.kwargs["body"]["input"], second.kwargs["body"]["input"])

    def test_paid_confirm_without_server_quote_is_blocked_before_http(self):
        self.authorize()
        with patch("hq_cli.client.request_json") as request:
            code, output, error = self.invoke(
                ["run", "audio-generate", "--input", "@-", "--confirm"], b'{"text":"hello"}',
            )
        self.assertEqual(cli.EXIT_CONFIRMATION, code)
        self.assertEqual("quote_required", self.payload(error)["error"])
        request.assert_not_called()

    def test_image_upload_requires_confirmation_and_uses_file_transport(self):
        self.authorize()
        image_path = os.path.join(self.temp.name, "reference.png")
        with patch.object(client, "upload_image") as upload:
            code, _, error = self.invoke(["run", "image-upload", "--file", image_path])
            self.assertEqual(cli.EXIT_CONFIRMATION, code)
            upload.assert_not_called()
            upload.return_value = (200, {
                "upload_id": "img_" + "a" * 32, "mime": "image/png", "bytes": 12,
                "sha256": "b" * 64, "expires_in": 3600,
            })
            code, output, error = self.invoke([
                "run", "image-upload", "--file", image_path, "--confirm", "--json",
            ])
        self.assertEqual(0, code, error)
        self.assertEqual("img_" + "a" * 32, self.payload(output)["result"]["upload_id"])
        upload.assert_called_once_with(image_path, "t" * 43)

    def test_streaming_image_client_sends_no_local_path_or_filename(self):
        raw = b"\x89PNG\r\n\x1a\n" + b"private-image"
        image_path = Path(self.temp.name) / "secret-name.png"
        image_path.write_bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()

        class Response:
            status = 200

            def read(self, _limit):
                return json.dumps({"upload_id": "img_" + "a" * 32, "sha256": digest}).encode()

        class Connection:
            def __init__(self):
                self.headers, self.sent = {}, bytearray()

            def putrequest(self, method, path, **_kwargs):
                self.method, self.path = method, path

            def putheader(self, key, value):
                self.headers[key] = value

            def endheaders(self):
                pass

            def send(self, chunk):
                self.sent.extend(chunk)

            def getresponse(self):
                return Response()

            def close(self):
                pass

        connection = Connection()
        with patch.object(client.http.client, "HTTPSConnection", return_value=connection):
            status, payload = client.upload_image(str(image_path), "t" * 43)
        self.assertEqual(200, status)
        self.assertEqual("img_" + "a" * 32, payload["upload_id"])
        self.assertEqual(raw, bytes(connection.sent))
        self.assertEqual(client.IMAGE_UPLOAD_PATH, connection.path)
        self.assertEqual("true", connection.headers["X-HQ-Confirm"])
        serialized = json.dumps(connection.headers)
        self.assertNotIn("secret-name.png", serialized)
        self.assertNotIn(str(image_path), serialized)

        link = Path(self.temp.name) / "linked.png"
        link.symlink_to(image_path)
        with self.assertRaises(ValueError):
            client.upload_image(str(link), "t" * 43)

        real_dir = Path(self.temp.name) / "real"
        real_dir.mkdir()
        (real_dir / "inside.png").write_bytes(raw)
        linked_dir = Path(self.temp.name) / "linked-dir"
        linked_dir.symlink_to(real_dir, target_is_directory=True)
        with self.assertRaises(ValueError):
            client.upload_image(str(linked_dir / "inside.png"), "t" * 43)

    def test_navigation_is_main_site_only_and_never_opens_by_default(self):
        with patch("hq_cli.cli.webbrowser.open") as opened:
            code, output, error = self.invoke(["run", "image", "--input", "@-"], b'{"prompt":"A & B"}')
        self.assertEqual(0, code, error)
        self.assertEqual("https://huangquechuanmei.com/workbench/banana?prompt=A+%26+B", self.payload(output)["result"]["url"])
        opened.assert_not_called()
        code, output, error = self.invoke(["run", "image", "--environment", "zelong"])
        self.assertEqual(cli.EXIT_USAGE, code)

    def test_strict_inputs_reject_unknown_nonfinite_bad_boolean_and_arbitrary_base(self):
        cases = [
            (["run", "canvas", "--input", "@-"], b'{"collab":"no"}'),
            (["run", "audio-generate", "--input", "@-"], b'{"text":"x","speed":NaN}'),
            (["run", "video-generate", "--input", "@-"], b'{"prompt":"x","generate_audio":1}'),
            (["run", "asset-tags", "--input", "@-"], b'{"kind":"image","key":"x","tags":"not-array"}'),
            (["run", "image", "--base-url", "https://evil.example"], b""),
        ]
        with patch("hq_cli.client.request_json") as request:
            for argv, raw in cases:
                code, output, error = self.invoke(argv, raw)
                self.assertIn(code, {cli.EXIT_USAGE, cli.EXIT_INPUT})
                self.assertEqual("hq.error/v1", self.payload(error)["schema"])
        request.assert_not_called()

    def test_deep_json_and_invalid_unicode_are_json_errors(self):
        deep = (b'{"x":' * 1200) + b'0' + (b'}' * 1200)
        for raw in (deep, b'{"prompt":"\\ud800"}'):
            code, output, error = self.invoke(["run", "image", "--input", "@-"], raw)
            self.assertEqual(cli.EXIT_INPUT, code)
            self.assertEqual("input_error", self.payload(error)["error"])

    def test_doctor_disables_proxies_and_redirects(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def getcode(self): return 200

        class Opener:
            def open(self, request, timeout): return Response()

        with patch("hq_cli.cli.urllib.request.build_opener", return_value=Opener()) as build:
            code, output, error = self.invoke(["doctor"])
        self.assertEqual(0, code, error)
        self.assertEqual(["auth", "generation"], [item["service"] for item in self.payload(output)["checks"]])
        proxy = next(item for item in build.call_args.args if isinstance(item, cli.urllib.request.ProxyHandler))
        self.assertEqual({}, proxy.proxies)

    def test_client_refuses_non_cli_paths_and_redirects(self):
        with self.assertRaises(ValueError):
            client.request_json("/api/auth/me")
        redirect = client._NoRedirect()
        self.assertIsNone(redirect.redirect_request(None, None, 302, "Found", {}, "https://evil.example"))

    def test_option_abbreviation_is_rejected(self):
        code, output, error = self.invoke(["run", "image", "--environ", "main"])
        self.assertEqual(cli.EXIT_USAGE, code)
        self.assertEqual("usage_error", self.payload(error)["error"])


if __name__ == "__main__":
    unittest.main()
