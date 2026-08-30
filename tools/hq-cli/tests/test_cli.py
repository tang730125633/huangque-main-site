import io
import hashlib
import json
import os
import re
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
        self.assertEqual("0.13.1", self.payload(output)["cli_version"])
        self.assertEqual("Huangque main-site CLI", self.payload(output)["product"])
        self.assertEqual("https://huangquechuanmei.com", self.payload(output)["origin"])

    def test_powershell_utf8_bom_input_is_accepted(self):
        self.authorize()
        with patch("hq_cli.client.request_json", return_value=(200, {"items": []})):
            code, output, error = self.invoke(
                ["run", "tasks", "--input", "@-"], b"\xef\xbb\xbf{}",
            )
        self.assertEqual(0, code, error)
        self.assertEqual([], self.payload(output)["result"]["items"])

    @unittest.skipUnless(os.name == "nt", "Windows credential behavior")
    def test_windows_credentials_use_appdata_and_dpapi(self):
        self.env.stop()
        try:
            appdata = Path(self.temp.name) / "Roaming profile"
            with patch.dict(os.environ, {"APPDATA": str(appdata)}, clear=False):
                os.environ.pop("HQ_CLI_CONFIG_DIR", None)
                client.save_credentials("s" * 43, 2000000000, ["profile:read"])
                path = appdata / "Huangque" / "hq-cli" / "credentials.json"
                raw = path.read_text(encoding="utf-8")
                self.assertNotIn("s" * 43, raw)
                self.assertEqual("windows-dpapi-current-user", json.loads(raw)["protection"])
                self.assertEqual("s" * 43, client.load_credentials()["access_token"])
        finally:
            self.env.start()

    def test_all_requested_authenticated_capabilities_are_available(self):
        _, output, _ = self.invoke(["capabilities"])
        by_id = {item["id"]: item for item in self.payload(output)["capabilities"]}
        expected = {
            "account", "channels", "ip12-projects", "ip12-project", "ip12-create", "ip12-report", "ip12-message", "ip12-delete",
            "prompt-optimize", "canvas-list", "canvas-get", "canvas-create", "canvas-agent-plan", "canvas-ops", "tasks", "task",
            "assets", "voices", "image-upload", "video-upload", "asset-favorite", "asset-tags", "asset-delete",
            "image-generate", "video-generate", "video-lipsync", "audio-generate",
            "digital-ip-text-generate", "digital-ip-audio-generate", "digital-ip-batch-generate",
            "cinematic-open-generate", "cinematic-motion-generate",
            "tryon-fast-generate", "tryon-classic-generate",
            "digital-ip-projects", "digital-ip-project", "digital-ip-report",
            "text-video-capability", "text-video-templates", "text-video-styles", "text-video-voices",
            "text-video-avatar-import", "text-video-plan", "text-video-generate", "pricing",
            "matrix-template-capability", "matrix-template-templates", "matrix-template-generate",
            "matrix-template-batch-generate",
            "inspiration-catalog", "inspiration-likes", "inspiration-like",
            "collect-content", "collect-video", "collect-transcript", "collect-search", "leads-generate",
            "leads-crm", "leads-crm-upsert", "video-avatars", "audio-slots",
            "short-drama-projects", "short-drama-project", "short-drama-conversation", "short-drama-preflight",
        }
        self.assertTrue(expected <= set(by_id))
        self.assertTrue(all(by_id[item]["availability"] == "available" for item in expected))
        self.assertTrue(all(by_id[item]["runnable"] for item in expected))

    def test_every_capability_teaches_an_agent_how_to_use_and_recover_it(self):
        _, output, _ = self.invoke(["capabilities"])
        items = self.payload(output)["capabilities"]
        self.assertGreater(len(items), 80)
        for capability in items:
            agent = capability["agent"]
            self.assertIn(agent["operation"], {
                "navigate", "list", "get", "create", "update", "delete", "execute",
            })
            self.assertTrue(agent["resource"])
            self.assertEqual(capability["description"], agent["when_to_use"])
            self.assertTrue(agent["workflow"])
            self.assertTrue(agent["success_evidence"])
            self.assertTrue(agent["recovery"])
            self.assertTrue(
                set(capability["input_schema"].get("required") or [])
                <= set(agent["required_inputs"]),
            )

    def test_ip12_resource_has_complete_crud_guidance(self):
        _, output, _ = self.invoke(["capabilities"])
        by_id = {item["id"]: item for item in self.payload(output)["capabilities"]}
        operations = by_id["ip12-project"]["agent"]["resource_operations"]
        self.assertEqual({
            "list": ["ip12-projects"], "get": ["ip12-project", "ip12-report"],
            "create": ["ip12-create"], "update": ["ip12-message"],
            "delete": ["ip12-delete"],
        }, operations)
        self.assertEqual([], by_id["ip12-delete"]["agent"]["missing_crud"])
        self.assertTrue(by_id["ip12-delete"]["confirmation_required"])

        asset = by_id["asset-delete"]["agent"]
        self.assertEqual("delete", asset["operation"])
        self.assertEqual("asset", asset["resource"])
        self.assertIn("asset-delete", asset["resource_operations"]["delete"])
        self.assertTrue(by_id["asset-delete"]["confirmation_required"])

        digital_ip = by_id["digital-ip-project"]["agent"]["resource_operations"]
        self.assertEqual({
            "list": ["digital-ip-projects"],
            "get": ["digital-ip-project", "digital-ip-report"],
            "create": ["digital-ip-create"],
            "update": ["digital-ip-update"],
            "delete": ["digital-ip-delete"],
        }, digital_ip)
        self.assertEqual([], by_id["digital-ip-project"]["agent"]["missing_crud"])

        leads = by_id["leads-delete"]["agent"]
        self.assertEqual("lead", leads["resource"])
        self.assertIn("leads-crm", leads["resource_operations"]["list"])
        self.assertIn("leads-delete", leads["resource_operations"]["delete"])
        self.assertTrue(any("先调用 leads-crm" in item for item in leads["workflow"]))

        tryon_inputs = by_id["tryon-classic-generate"]["agent"]["required_inputs"]
        clothes = tryon_inputs["clothes_upload_id"]
        self.assertIn("image-upload", clothes)
        self.assertNotIn("audio-upload", clothes)
        self.assertIn("image-upload", tryon_inputs["background_upload_id"])
        self.assertIn("video-upload", tryon_inputs["person_video_upload_id"])
        self.assertEqual("server_quote", by_id["image-generate"]["cost"]["kind"])
        self.assertEqual("hq_device_authorization", by_id["ip12-projects"]["target_auth"])
        self.assertEqual("assets:upload", by_id["image-upload"]["required_scope"])
        self.assertEqual(20, by_id["image-upload"]["file_input"]["accountActiveMaxFiles"])
        self.assertEqual(96 * 1024 * 1024, by_id["image-upload"]["file_input"]["accountActiveMaxBytes"])
        self.assertEqual(32 * 1024 * 1024, by_id["video-upload"]["file_input"]["maxBytes"])
        self.assertEqual(
            ["video/mp4", "video/quicktime", "video/webm"],
            by_id["video-upload"]["file_input"]["mimeTypes"],
        )
        self.assertEqual("assets:upload", by_id["audio-upload"]["required_scope"])
        self.assertEqual(10 * 1024 * 1024, by_id["audio-upload"]["file_input"]["maxBytes"])
        self.assertEqual(
            ["audio/mpeg", "audio/wav", "audio/mp4", "audio/aac", "audio/ogg"],
            by_id["audio-upload"]["file_input"]["mimeTypes"],
        )
        self.assertIn("audio-upload", by_id["voice-clone-create"]["agent"]["required_inputs"]["audio_upload_id"])
        self.assertIn("audio-slots", by_id["voice-clone-create"]["agent"]["required_inputs"]["slot_id"])
        self.assertTrue(any(
            "30-60 seconds" in item
            for item in by_id["voice-clone-create"]["constraints"]
        ))
        self.assertTrue(any(
            "silence" in item
            for item in by_id["voice-clone-create"]["constraints"]
        ))
        self.assertTrue(any(
            "voice-clone-status" in item
            for item in by_id["voice-clone-create"]["agent"]["workflow"]
        ))
        self.assertTrue(any(
            "有效语音太短" in item
            for item in by_id["voice-clone-create"]["agent"]["recovery"]
        ))
        self.assertEqual("server_quote", by_id["canvas-agent-plan"]["cost"]["kind"])
        self.assertEqual("canvas:edit", by_id["canvas-ops"]["required_scope"])
        self.assertEqual(12, by_id["canvas-ops"]["input_schema"]["properties"]["ops"]["maxItems"])
        self.assertIn("minimax", by_id["video-generate"]["input_schema"]["properties"]["channel"]["enum"])
        self.assertIn("2k", by_id["video-generate"]["input_schema"]["properties"]["resolution"]["enum"])
        minimax_rule = next(
            rule for rule in by_id["video-generate"]["input_schema"]["allOf"]
            if rule.get("if", {}).get("properties", {}).get("channel", {}).get("const") == "minimax"
        )
        self.assertEqual(["2k"], minimax_rule["then"]["properties"]["resolution"]["enum"])
        self.assertIn("banana", by_id["image-generate"]["input_schema"]["properties"]["provider"]["enum"])
        self.assertEqual(["nb2", "pro"], by_id["image-generate"]["input_schema"]["properties"]["model"]["enum"])
        self.assertIn("21:9", by_id["image-generate"]["input_schema"]["properties"]["ratio"]["enum"])
        self.assertIn("sora", by_id["video-generate"]["input_schema"]["properties"]["channel"]["enum"])
        self.assertIn("1024p", by_id["video-generate"]["input_schema"]["properties"]["resolution"]["enum"])
        self.assertIn("sora-2-pro", by_id["video-generate"]["input_schema"]["properties"]["model"]["enum"])
        self.assertEqual([4, 8, 12], by_id["video-generate"]["input_schema"]["properties"]["seconds"]["enum"])
        self.assertEqual("server_quote", by_id["digital-ip-text-generate"]["cost"]["kind"])
        self.assertEqual("server_quote", by_id["text-video-generate"]["cost"]["kind"])
        self.assertEqual("server_quote", by_id["matrix-template-generate"]["cost"]["kind"])
        self.assertEqual("server_quote", by_id["matrix-template-batch-generate"]["cost"]["kind"])
        self.assertEqual(80, by_id["matrix-template-generate"]["input_schema"]
                         ["properties"]["font_family"]["maxLength"])
        self.assertEqual(
            ["top_text", "bottom_text", "template_id"],
            by_id["matrix-template-generate"]["input_schema"]["required"],
        )
        self.assertEqual(
            ["top_text", "bottom_text", "template_id", "count"],
            by_id["matrix-template-batch-generate"]["input_schema"]["required"],
        )
        self.assertEqual((2, 5), (
            by_id["matrix-template-batch-generate"]["input_schema"]
                 ["properties"]["count"]["minimum"],
            by_id["matrix-template-batch-generate"]["input_schema"]
                 ["properties"]["count"]["maximum"],
        ))
        self.assertTrue(any(
            "全部 job_ids" in item
            for item in by_id["matrix-template-batch-generate"]["agent"]["workflow"]
        ))
        self.assertTrue(any(
            "原 quote_token" in item
            for item in by_id["matrix-template-batch-generate"]["agent"]["recovery"]
        ))
        self.assertEqual(
            ["text", "template", "style", "voice"],
            by_id["text-video-generate"]["input_schema"]["required"],
        )
        self.assertEqual(
            ["generate", "fixed"],
            by_id["text-video-generate"]["input_schema"]["properties"]["mode"]["enum"],
        )
        self.assertEqual("assets:upload", by_id["text-video-avatar-import"]["required_scope"])
        self.assertEqual("generation:quote", by_id["text-video-plan"]["required_scope"])
        self.assertTrue(by_id["text-video-avatar-import"]["confirmation_required"])
        self.assertTrue(by_id["text-video-plan"]["confirmation_required"])
        self.assertEqual(
            ["video_asset_id", "audio_asset_id"],
            by_id["video-lipsync"]["input_schema"]["required"],
        )
        self.assertEqual(
            ["speed", "precision"],
            by_id["video-lipsync"]["input_schema"]["properties"]["quality"]["enum"],
        )
        self.assertEqual([], by_id["digital-ip-audio-generate"]["input_schema"]["required"])
        self.assertEqual(4, len(by_id["digital-ip-audio-generate"]["input_schema"]["oneOf"]))
        self.assertEqual(
            [{"required": ["avatar_id"]}, {"required": ["image_upload_id"]}],
            by_id["digital-ip-text-generate"]["input_schema"]["oneOf"],
        )
        self.assertEqual(
            500,
            by_id["digital-ip-audio-generate"]["input_schema"]["properties"]["audio_file"]["maxLength"],
        )
        cinematic = by_id["cinematic-open-generate"]["input_schema"]
        self.assertEqual(3, cinematic["properties"]["avatar_ids"]["maxItems"])
        self.assertEqual(8, cinematic["properties"]["reference_image_upload_ids"]["maxItems"])
        self.assertEqual(3, cinematic["properties"]["reference_video_upload_ids"]["maxItems"])
        self.assertTrue(any(
            "1 avatar allows 8 references, 2 allow 7, and 3 allow 6" in item
            for item in by_id["cinematic-open-generate"]["constraints"]
        ))
        self.assertEqual(5, by_id["digital-ip-batch-generate"]["input_schema"]["properties"]["avatars"]["maxItems"])
        self.assertEqual(1, by_id["cinematic-motion-generate"]["input_schema"]["properties"]["reference_video_upload_ids"]["maxItems"])
        self.assertEqual("inspiration:write", by_id["inspiration-like"]["required_scope"])
        self.assertEqual("leads:write", by_id["leads-crm-upsert"]["required_scope"])
        self.assertTrue(by_id["inspiration-like"]["confirmation_required"])
        self.assertTrue(by_id["leads-crm-upsert"]["confirmation_required"])
        for identifier in ("collect-content", "collect-video", "collect-transcript", "collect-search", "leads-generate"):
            self.assertEqual("paid", by_id[identifier]["side_effect"])
            self.assertEqual("generation:quote", by_id[identifier]["required_scope"])
            self.assertEqual("server_quote", by_id[identifier]["cost"]["kind"])

        collect_url = by_id["collect-video"]["input_schema"]["properties"]["url"]
        self.assertTrue(re.match(collect_url["pattern"], "https://v.douyin.com/abc123/"))
        self.assertTrue(re.match(collect_url["pattern"], "https://douyin.com:80/video"))
        self.assertTrue(re.match(collect_url["pattern"], "https://xiaohongshu.com:443/explore/123"))
        self.assertFalse(re.match(collect_url["pattern"], "https://douyin.com:8080/video"))
        self.assertTrue(re.match(collect_url["pattern"], "https://www.xiaohongshu.com/explore/123"))
        self.assertFalse(re.match(collect_url["pattern"], "https://example.com/video"))
        search = by_id["collect-search"]["input_schema"]
        self.assertEqual(["douyin", "xhs"], search["properties"]["platform"]["enum"])
        self.assertEqual(50, search["properties"]["page"]["maximum"])
        self.assertEqual(["platform", "keyword"], search["required"])
        leads = by_id["leads-generate"]["input_schema"]
        self.assertEqual(["platforms"], leads["required"])
        self.assertEqual(30, leads["properties"]["count"]["maximum"])
        self.assertEqual(3, leads["properties"]["pages"]["maximum"])
        self.assertEqual(120, leads["properties"]["channels_targets"]["items"]["maxLength"])
        self.assertEqual(["douyin", "xhs", "channels"], leads["properties"]["platforms"]["items"]["enum"])

    def test_p0_navigation_reads_and_website_modes_are_discoverable(self):
        _, output, _ = self.invoke(["capabilities"])
        by_id = {item["id"]: item for item in self.payload(output)["capabilities"]}
        navigation = {
            "text-video": "/workbench/text-video", "short-drama": "/workbench/short-drama",
            "matrix-template": "/workbench/matrix-template.html",
            "pricing-page": "/workbench/pricing", "invite": "/workbench/invite",
            "recharge": "/workbench/recharge", "bots": "/workbench/bots",
        }
        for identifier, path in navigation.items():
            self.assertEqual("navigation", by_id[identifier]["kind"])
            self.assertEqual("navigation", by_id[identifier]["side_effect"])
            self.assertEqual(path, by_id[identifier]["deep_link"]["path"])
        self.assertNotIn("device", by_id)

        reads = {
            "digital-ip-projects": "ip12:read", "digital-ip-project": "ip12:read",
            "digital-ip-report": "ip12:read", "text-video-capability": "assets:read",
            "text-video-templates": "assets:read", "text-video-styles": "assets:read",
            "text-video-voices": "assets:read", "pricing": "profile:read",
            "matrix-template-capability": "assets:read",
            "matrix-template-templates": "assets:read",
            "inspiration-catalog": "inspiration:read", "inspiration-likes": "inspiration:read",
            "leads-crm": "leads:read", "video-avatars": "assets:read", "audio-slots": "assets:read",
            "short-drama-projects": "short-drama:read", "short-drama-project": "short-drama:read",
            "short-drama-conversation": "short-drama:read", "short-drama-preflight": "short-drama:read",
        }
        for identifier, scope in reads.items():
            self.assertEqual("read", by_id[identifier]["side_effect"])
            self.assertEqual(identifier, by_id[identifier]["api_action"])
            self.assertEqual(scope, by_id[identifier]["required_scope"])

        expected_modes = {
            "image-generate": {"banana", "openai", "seedream", "xiaole"},
            "video-generate": {"grok", "sora", "minimax", "omni", "seedance"},
            "audio-generate": {"tts"}, "video-compose-projects": {"one_click"},
            "digital-ip-text-generate": {"digital_ip"},
            "digital-ip-audio-generate": {"digital_ip"},
            "digital-ip-batch-generate": {"digital_ip"},
            "cinematic-open-generate": {"cinematic"},
            "cinematic-motion-generate": {"cinematic"},
            "tryon-fast-generate": {"tryon"},
            "tryon-classic-generate": {"tryon"},
            "video-upload": {"cinematic", "tryon"},
            "audio-upload": {"tts", "digital_ip"},
            "digital-presenter-capability": {"digitalPresenter"},
            "text-video-capability": {"text_video"}, "text-video-generate": {"text_video"},
            "text-video-avatar-import": {"text_video"}, "text-video-plan": {"text_video"},
            "matrix-template-capability": {"matrix_template.single"},
            "matrix-template-templates": {"matrix_template.single"},
            "matrix-template-generate": {"matrix_template.single"},
            "matrix-template-batch-generate": {"matrix_template.batch"},
            "digital-ip-projects": {"digital_ip"},
            "pricing": {"pricing.catalog"},
            "inspiration-catalog": {"inspiration.browse"}, "inspiration-like": {"inspiration.like"},
            "collect": {"collect.content.comments", "collect.content.video", "collect.content.transcript", "collect.keyword.search"},
            "collect-content": {"collect.content.comments"}, "collect-video": {"collect.content.video"},
            "collect-transcript": {"collect.content.transcript"}, "collect-search": {"collect.keyword.search"},
            "leads": {"leads.keyword.search"}, "leads-generate": {"leads.keyword.search"},
            "leads-crm": {"leads.crm.update"}, "video-avatars": {"cinematic", "digital_ip", "live_action"},
            "audio-slots": {"tts"}, "short-drama-projects": {"live_action"},
        }
        for identifier, modes in expected_modes.items():
            self.assertEqual(modes, set(by_id[identifier]["website_modes"]))

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
        if os.name != "nt":
            mode = stat.S_IMODE(os.stat(client.credentials_path()).st_mode)
            self.assertEqual(0o600, mode)
        else:
            self.assertNotIn("t" * 43, client.credentials_path().read_text(encoding="utf-8"))
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

    def test_new_read_capabilities_send_their_fixed_action_body(self):
        self.authorize()
        cases = {
            "digital-ip-projects": {},
            "digital-ip-project": {"project_id": "project_1"},
            "digital-ip-report": {"project_id": "project_1"},
            "text-video-capability": {}, "text-video-templates": {},
            "text-video-styles": {}, "text-video-voices": {}, "pricing": {},
            "matrix-template-capability": {}, "matrix-template-templates": {},
            "inspiration-catalog": {}, "inspiration-likes": {},
            "leads-crm": {"lead_ids": ["a" * 16]}, "video-avatars": {"limit": 20}, "audio-slots": {},
            "short-drama-projects": {"page": 1, "page_size": 20},
            "short-drama-project": {"project_id": "11111111-1111-4111-8111-111111111111"},
            "short-drama-conversation": {"project_id": "22222222-2222-4222-8222-222222222222"},
            "short-drama-preflight": {"project_id": "33333333-3333-4333-8333-333333333333"},
        }
        for identifier, payload in cases.items():
            argv = ["run", identifier]
            raw = b""
            if payload:
                argv += ["--input", "@-"]
                raw = json.dumps(payload).encode()
            with self.subTest(identifier=identifier), \
                    patch("hq_cli.client.request_json", return_value=(200, {"ok": True})) as request:
                code, output, error = self.invoke(argv, raw)
                self.assertEqual(0, code, error)
                self.assertTrue(self.payload(output)["result"]["ok"])
                self.assertEqual("/api/auth/cli/action", request.call_args.args[0])
                self.assertEqual({"action": identifier, "input": payload, "confirm": False},
                                 request.call_args.kwargs["body"])

    def test_banana_and_sora_keep_the_generic_paid_action_body(self):
        self.authorize()
        upload_id = "img_" + "a" * 32
        cases = {
            "image-generate": {
                "prompt": "海报", "provider": "banana", "model": "pro", "ratio": "21:9",
                "count": 4, "reference_upload_ids": [upload_id],
            },
            "video-generate": {
                "prompt": "海边日出", "channel": "sora", "model": "sora-2-pro",
                "seconds": 8, "ratio": "16:9", "resolution": "1024p",
                "reference_upload_ids": [upload_id],
            },
        }
        quote = {"quote_token": "q.new", "cost": 1, "confirmation_required": True}
        for identifier, payload in cases.items():
            with self.subTest(identifier=identifier), \
                    patch("hq_cli.client.request_json", return_value=(200, quote)) as request:
                code, _, error = self.invoke(
                    ["run", identifier, "--input", "@-"], json.dumps(payload, ensure_ascii=False).encode(),
                )
                self.assertEqual(0, code, error)
                self.assertEqual({"action": identifier, "input": payload, "confirm": False},
                                 request.call_args.kwargs["body"])

    def test_paid_actions_use_the_existing_quote_confirm_flow(self):
        self.authorize()
        cases = {
            "digital-ip-text-generate": {
                "avatar_id": 17, "text": "欢迎来到黄雀", "voice": "S_public",
                "ratio": "9:16", "motion": "medium",
            },
            "digital-ip-audio-generate": {
                "avatar_id": 17, "audio_file": "audio/mine.mp3", "ratio": "9:16",
            },
            "digital-ip-text-generate": {
                "image_upload_id": "img_" + "d" * 32, "text": "临时人物照片口播",
                "voice": "S_public", "ratio": "9:16",
            },
            "digital-ip-batch-generate": {
                "avatars": [{"avatar_id": 17, "label": "主讲人"}, {"avatar_id": 18}],
                "text": "欢迎来到黄雀", "voice": "S_public", "ratio": "9:16",
            },
            "cinematic-open-generate": {
                "avatar_ids": [17, 18], "prompt": "人物在明亮工作室自然挥手",
                "duration": 10, "ratio": "9:16",
                "reference_image_upload_ids": ["img_" + "a" * 32],
                "reference_video_upload_ids": ["vid_" + "b" * 32],
            },
            "cinematic-motion-generate": {
                "avatar_id": 17, "reference_video_upload_ids": ["vid_" + "b" * 32],
                "ratio": "16:9",
            },
            "tryon-fast-generate": {
                "person_image_upload_id": "img_" + "a" * 32,
                "clothes_upload_id": "img_" + "b" * 32, "seconds": 8,
            },
            "tryon-classic-generate": {
                "person_video_upload_id": "vid_" + "c" * 32,
                "background_upload_id": "img_" + "d" * 32, "seconds": 6,
            },
            "collect-content": {"url": "https://v.douyin.com/abc123/"},
            "collect-video": {"url": "https://www.xiaohongshu.com/explore/123"},
            "collect-transcript": {"url": "https://xhslink.com/a1b2c3"},
            "collect-search": {"platform": "douyin", "keyword": "AI 创业"},
            "leads-generate": {
                "keyword": "AI 获客", "platforms": ["douyin", "xhs"],
            },
        }
        for identifier, payload in cases.items():
            quote = {"quote_token": "q.%s" % identifier, "cost": 10,
                     "confirmation_required": True}
            result = {"job_id": 100, "cost": 10, "points_left": 90}
            with self.subTest(identifier=identifier), patch(
                    "hq_cli.client.request_json", side_effect=[(200, quote), (200, result)]) as request:
                raw = json.dumps(payload, ensure_ascii=False).encode()
                code, _, error = self.invoke(["run", identifier, "--input", "@-"], raw)
                self.assertEqual(0, code, error)
                code, output, error = self.invoke([
                    "run", identifier, "--input", "@-", "--confirm", "--quote-token",
                    quote["quote_token"],
                ], raw)
                self.assertEqual(0, code, error)
                self.assertEqual(100, self.payload(output)["result"]["job_id"])
                first, second = request.call_args_list
                self.assertEqual({"action": identifier, "input": payload, "confirm": False},
                                 first.kwargs["body"])
                self.assertEqual(payload, second.kwargs["body"]["input"])
                self.assertTrue(second.kwargs["body"]["confirm"])
                self.assertEqual(quote["quote_token"], second.kwargs["body"]["quote_token"])

    def test_video_action_cardinality_is_rejected_before_http(self):
        invalid = {
            "digital-ip-batch-generate": {
                "avatars": [{"avatar_id": 17}], "text": "x", "voice": "S_public",
            },
            "cinematic-motion-generate": {
                "avatar_id": 17,
                "reference_video_upload_ids": ["vid_" + "a" * 32, "vid_" + "b" * 32],
            },
            "cinematic-open-generate": {
                "avatar_id": 17, "avatar_ids": [18], "prompt": "x",
            },
            "digital-ip-audio-generate": {
                "avatar_id": 17, "audio_file": "a" * 501,
            },
            "tryon-classic-generate": {
                "person_video_upload_id": "vid_" + "c" * 32,
            },
        }
        with patch("hq_cli.client.request_json") as request:
            for identifier, payload in invalid.items():
                with self.subTest(identifier=identifier):
                    code, _, error = self.invoke(
                        ["run", identifier, "--input", "@-"], json.dumps(payload).encode(),
                    )
                    self.assertEqual(cli.EXIT_INPUT, code, error)
        request.assert_not_called()

    def test_cinematic_open_retains_single_avatar_id_compatibility(self):
        self.authorize()
        payload = {"avatar_id": 17, "prompt": "人物在工作室自然挥手"}
        with patch("hq_cli.client.request_json", return_value=(200, {"quote_token": "q.compat"})) as request:
            code, _, error = self.invoke(
                ["run", "cinematic-open-generate", "--input", "@-"],
                json.dumps(payload, ensure_ascii=False).encode(),
            )
        self.assertEqual(0, code, error)
        self.assertEqual(payload, request.call_args.kwargs["body"]["input"])

    def test_external_ai_and_write_actions_require_explicit_confirmation_before_http(self):
        self.authorize()
        inputs = {
            "prompt-optimize": b'{"prompt":"better portrait","kind":"image"}',
            "ip12-create": b'{"title":"My IP"}',
            "ip12-message": '{"project_id":"ip_1","message":"我的核心客户是本地餐饮老板","request_id":"turn-001"}'.encode(),
            "canvas-create": b'{"name":"Launch","prompt":"first idea"}',
            "canvas-ops": b'{"board_id":"cb_1","base_version":1,"op_id":"hqcli-abcdefghijkl","ops":[{"type":"node.patch","id":"n1","fields":{"x":120}}]}',
            "asset-tags": '{"kind":"image","key":"asset-1","tags":["客户案例"]}'.encode(),
            "inspiration-like": b'{"id":1001,"favorite":true}',
            "leads-crm-upsert": '{"lead_id":"0123456789abcdef","follow_status":"跟进中"}'.encode(),
            "video-compose-review": ('{"project_id":"compose_%s","expected_revision":2,'
                                       '"decisions":{"candidate_%s":"remove"}}' % ("a" * 32, "b" * 16)).encode(),
            "digital-presenter-create": b'{"board_id":"cb_1","request_id":"hqcli-dp-001"}',
            "text-video-avatar-import": ('{"image_upload_id":"img_%s"}' % ("a" * 32)).encode(),
            "text-video-plan": b'{"text":"AI training","template":"1080x1920/image_default.html","style":"realistic_commercial","voice":"public:zh-CN-YunjianNeural"}',
        }
        with patch("hq_cli.client.request_json") as request:
            for capability, raw in inputs.items():
                code, output, error = self.invoke(["run", capability, "--input", "@-"], raw)
                self.assertEqual(cli.EXIT_CONFIRMATION, code)
                self.assertEqual("confirmation_required", self.payload(error)["error"])
        request.assert_not_called()

    def test_confirmed_safe_writes_send_only_the_fixed_action_and_input(self):
        self.authorize()
        cases = {
            "inspiration-like": {"id": 1001, "favorite": True},
            "leads-crm-upsert": {"lead_id": "0123456789abcdef", "follow_status": "跟进中"},
        }
        for identifier, payload in cases.items():
            with self.subTest(identifier=identifier), \
                    patch("hq_cli.client.request_json", return_value=(200, {"ok": True})) as request:
                code, output, error = self.invoke(
                    ["run", identifier, "--input", "@-", "--confirm"],
                    json.dumps(payload, ensure_ascii=False).encode(),
                )
                self.assertEqual(0, code, error)
                self.assertTrue(self.payload(output)["result"]["ok"])
                self.assertEqual(
                    {"action": identifier, "input": payload, "confirm": True},
                    request.call_args.kwargs["body"],
                )

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

    def test_text_video_generation_exposes_quote_breakdown_and_confirms_same_input(self):
        self.authorize()
        value = {
            "text": "AI 培训如何提升团队效率",
            "template": "1080x1920/image_default.html",
            "mode": "fixed", "style": "realistic_commercial",
            "voice": "public:zh-CN-YunjianNeural", "speech_rate": 1.0,
        }
        raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
        quote = {
            "quote_token": "q.text-video", "kind": "script_to_video", "cost": 70,
            "scene_count": 3, "cost_breakdown": {"scene_count": 3, "total": 70},
            "expires_in": 300, "confirmation_required": True,
        }
        with patch("hq_cli.client.request_json", side_effect=[
                (200, quote), (200, {"job_id": 91, "cost": 70, "points_left": 30})]) as request:
            code, output, error = self.invoke(
                ["run", "text-video-generate", "--input", "@-"], raw)
            self.assertEqual(0, code, error)
            self.assertEqual(3, self.payload(output)["result"]["scene_count"])
            code, output, error = self.invoke([
                "run", "text-video-generate", "--input", "@-", "--confirm",
                "--quote-token", "q.text-video",
            ], raw)
        self.assertEqual(0, code, error)
        self.assertEqual(91, self.payload(output)["result"]["job_id"])
        first, second = request.call_args_list
        self.assertEqual(first.kwargs["body"]["input"], second.kwargs["body"]["input"])
        self.assertEqual("q.text-video", second.kwargs["body"]["quote_token"])

    def test_matrix_template_quotes_confirms_and_reuses_exact_input(self):
        self.authorize()
        value = {
            "top_text": "真正拉开差距的不是工具",
            "bottom_text": "评论区留下关键词领取方案",
            "template_id": "native-bold", "font_family": "AaHouDiHei",
        }
        raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
        quote = {
            "quote_token": "q.matrix", "kind": "matrix_template_video",
            "cost": 5, "points": 100, "expires_in": 300,
            "confirmation_required": True,
        }
        with patch("hq_cli.client.request_json", side_effect=[
                (200, quote), (200, {"job_id": 92, "cost": 5, "points_left": 95})]) as request:
            code, output, error = self.invoke(
                ["run", "matrix-template-generate", "--input", "@-"], raw)
            self.assertEqual(0, code, error)
            self.assertEqual(5, self.payload(output)["result"]["cost"])
            code, output, error = self.invoke([
                "run", "matrix-template-generate", "--input", "@-", "--confirm",
                "--quote-token", "q.matrix",
            ], raw)
        self.assertEqual(0, code, error)
        self.assertEqual(92, self.payload(output)["result"]["job_id"])
        first, second = request.call_args_list
        self.assertEqual(first.kwargs["body"]["input"], second.kwargs["body"]["input"])
        self.assertEqual("q.matrix", second.kwargs["body"]["quote_token"])

    def test_matrix_template_batch_quotes_and_confirms_exact_count(self):
        self.authorize()
        value = {
            "top_text": "批量模板成片标题",
            "bottom_text": "评论区领取完整方案",
            "template_id": "native-bold", "font_family": "AaHouDiHei", "count": 3,
        }
        raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
        quote = {
            "quote_token": "q.matrix.batch", "kind": "matrix_template_video_batch",
            "cost": 15, "cost_per_job": 5, "count": 3, "points": 100,
            "expires_in": 300, "confirmation_required": True,
        }
        accepted = {
            "job_ids": [101, 102, 103], "submitted_count": 3,
            "failed_count": 0, "cost": 15, "points_left": 85,
        }
        with patch("hq_cli.client.request_json", side_effect=[
                (200, quote), (200, accepted)]) as request:
            code, output, error = self.invoke(
                ["run", "matrix-template-batch-generate", "--input", "@-"], raw)
            self.assertEqual(0, code, error)
            self.assertEqual((15, 3), (
                self.payload(output)["result"]["cost"],
                self.payload(output)["result"]["count"],
            ))
            code, output, error = self.invoke([
                "run", "matrix-template-batch-generate", "--input", "@-", "--confirm",
                "--quote-token", "q.matrix.batch",
            ], raw)
        self.assertEqual(0, code, error)
        self.assertEqual([101, 102, 103], self.payload(output)["result"]["job_ids"])
        first, second = request.call_args_list
        self.assertEqual(first.kwargs["body"]["input"], second.kwargs["body"]["input"])
        self.assertEqual("q.matrix.batch", second.kwargs["body"]["quote_token"])

    def test_matrix_template_batch_pending_error_preserves_accepted_jobs(self):
        self.authorize()
        value = {
            "top_text": "批量结果待确认",
            "bottom_text": "保留已经受理的任务",
            "template_id": "full-overlay-bold", "count": 3,
        }
        raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
        payload = {
            "detail": "批量提交结果待确认，请复用原 quote_token 重试",
            "code": "batch_result_pending",
            "jobs": [{"index": 1, "job_id": 501, "cost": 5}],
            "failures": [{"index": 2, "code": "result_unknown"}],
            "next_index": 2,
        }
        with patch("hq_cli.client.request_json", return_value=(502, payload)):
            code, _, error = self.invoke([
                "run", "matrix-template-batch-generate", "--input", "@-", "--confirm",
                "--quote-token", "q.matrix.batch",
            ], raw)
        result = self.payload(error)
        self.assertEqual(cli.EXIT_API, code)
        self.assertEqual("batch_result_pending", result["error"])
        self.assertEqual(501, result["details"]["jobs"][0]["job_id"])
        self.assertEqual(2, result["details"]["next_index"])

    def test_matrix_template_rejects_unknown_fields_and_invalid_template_id(self):
        self.authorize()
        base = {
            "top_text": "有效标题", "bottom_text": "有效行动文案",
            "template_id": "native-bold",
        }
        for payload in (
            dict(base, duration=8),
            dict(base, bgm=False),
            dict(base, template_id="../bad"),
            dict(base, font_family="x" * 81),
        ):
            with self.subTest(payload=payload), patch("hq_cli.client.request_json") as request:
                code, _, error = self.invoke(
                    ["run", "matrix-template-generate", "--input", "@-"],
                    json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                )
                self.assertEqual(cli.EXIT_INPUT, code)
                self.assertEqual("input_error", self.payload(error)["error"])
                request.assert_not_called()
        for count in (1, 6, True):
            with self.subTest(count=count), patch("hq_cli.client.request_json") as request:
                payload = dict(base, count=count)
                code, _, error = self.invoke(
                    ["run", "matrix-template-batch-generate", "--input", "@-"],
                    json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                )
                self.assertEqual(cli.EXIT_INPUT, code)
                self.assertEqual("input_error", self.payload(error)["error"])
                request.assert_not_called()

    def test_text_video_full_talking_flow_uses_import_plan_and_scene_selection(self):
        self.authorize()
        avatar_id = "local_avatar_" + "c" * 32
        plan_id = "talking_plan_" + "d" * 32
        source_hash = "e" * 64
        base = {
            "text": "完整文案", "template": "1080x1920/image_default.html",
            "mode": "fixed", "style": "realistic_commercial",
            "voice": "public:zh-CN-YunjianNeural", "speech_rate": 1.0,
        }
        talking = {
            "enabled": True, "plan_id": plan_id, "source_hash": source_hash,
            "ratio": 0.3, "default_avatar_asset_id": avatar_id,
            "scenes": [
                {"scene_id": "scene_01", "enabled": True},
                {"scene_id": "scene_02", "enabled": False},
            ],
        }
        quote = {"quote_token": "q.talking", "cost": 70, "scene_count": 2,
                 "cost_breakdown": {"scene_count": 2, "total": 70}}
        with patch("hq_cli.client.request_json", side_effect=[
                (200, {"asset_id": avatar_id}),
                (200, {"plan_id": plan_id, "source_hash": source_hash, "scenes": talking["scenes"]}),
                (200, quote), (200, {"job_id": 93, "cost": 70})]) as request:
            code, _, error = self.invoke([
                "run", "text-video-avatar-import", "--input", "@-", "--confirm",
            ], ('{"image_upload_id":"img_%s"}' % ("a" * 32)).encode())
            self.assertEqual(0, code, error)
            code, _, error = self.invoke([
                "run", "text-video-plan", "--input", "@-", "--confirm",
            ], json.dumps(dict(base, ratio=0.3), ensure_ascii=False).encode())
            self.assertEqual(0, code, error)
            payload = dict(base, talking_material=talking)
            raw = json.dumps(payload, ensure_ascii=False).encode()
            code, _, error = self.invoke([
                "run", "text-video-generate", "--input", "@-"], raw)
            self.assertEqual(0, code, error)
            code, output, error = self.invoke([
                "run", "text-video-generate", "--input", "@-", "--confirm",
                "--quote-token", "q.talking"], raw)
        self.assertEqual(0, code, error)
        self.assertEqual(93, self.payload(output)["result"]["job_id"])
        calls = request.call_args_list
        self.assertEqual("text-video-avatar-import", calls[0].kwargs["body"]["action"])
        self.assertEqual("text-video-plan", calls[1].kwargs["body"]["action"])
        self.assertEqual(talking, calls[2].kwargs["body"]["input"]["talking_material"])
        self.assertEqual(calls[2].kwargs["body"]["input"], calls[3].kwargs["body"]["input"])

    def test_text_video_talking_input_is_rejected_locally_when_incomplete(self):
        self.authorize()
        payload = {
            "text": "完整文案", "template": "1080x1920/image_default.html",
            "style": "realistic_commercial", "voice": "public:zh-CN-YunjianNeural",
            "talking_material": {"enabled": True},
        }
        with patch("hq_cli.client.request_json") as request:
            code, _, error = self.invoke([
                "run", "text-video-generate", "--input", "@-"],
                json.dumps(payload, ensure_ascii=False).encode())
        self.assertEqual(cli.EXIT_INPUT, code)
        self.assertEqual("input_error", self.payload(error)["error"])
        request.assert_not_called()

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

    def test_video_upload_requires_confirmation_and_uses_file_transport(self):
        self.authorize()
        video_path = os.path.join(self.temp.name, "reference.mp4")
        with patch.object(client, "upload_video") as upload:
            code, _, error = self.invoke(["run", "video-upload", "--file", video_path])
            self.assertEqual(cli.EXIT_CONFIRMATION, code)
            upload.assert_not_called()
            upload.return_value = (200, {
                "upload_id": "vid_" + "a" * 32, "mime": "video/mp4", "bytes": 24,
                "sha256": "b" * 64, "expires_in": 3600,
            })
            code, output, error = self.invoke([
                "run", "video-upload", "--file", video_path, "--confirm", "--json",
            ])
        self.assertEqual(0, code, error)
        self.assertEqual("vid_" + "a" * 32, self.payload(output)["result"]["upload_id"])
        upload.assert_called_once_with(video_path, "t" * 43)

    def test_audio_upload_requires_confirmation_and_uses_file_transport(self):
        self.authorize()
        audio_path = os.path.join(self.temp.name, "sample.mp3")
        with patch.object(client, "upload_audio") as upload:
            code, _, error = self.invoke(["run", "audio-upload", "--file", audio_path])
            self.assertEqual(cli.EXIT_CONFIRMATION, code)
            upload.assert_not_called()
            upload.return_value = (200, {
                "upload_id": "aud_" + "a" * 32, "mime": "audio/mpeg", "bytes": 24,
                "sha256": "b" * 64, "duration": 60.0, "expires_in": 3600,
            })
            code, output, error = self.invoke([
                "run", "audio-upload", "--file", audio_path, "--confirm", "--json",
            ])
        self.assertEqual(0, code, error)
        self.assertEqual("aud_" + "a" * 32, self.payload(output)["result"]["upload_id"])
        upload.assert_called_once_with(audio_path, "t" * 43)

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
        try:
            link.symlink_to(image_path)
        except OSError:
            pass
        else:
            with self.assertRaises(ValueError):
                client.upload_image(str(link), "t" * 43)

        real_dir = Path(self.temp.name) / "real"
        real_dir.mkdir()
        (real_dir / "inside.png").write_bytes(raw)
        linked_dir = Path(self.temp.name) / "linked-dir"
        try:
            linked_dir.symlink_to(real_dir, target_is_directory=True)
        except OSError:
            pass
        else:
            with self.assertRaises(ValueError):
                client.upload_image(str(linked_dir / "inside.png"), "t" * 43)

    def test_streaming_video_client_enforces_magic_size_and_private_transport(self):
        raw = b"\x00\x00\x00\x18ftypisom" + b"private-video"
        video_path = Path(self.temp.name) / "secret-name.mp4"
        video_path.write_bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()

        class Response:
            status = 200

            def read(self, _limit):
                return json.dumps({"upload_id": "vid_" + "a" * 32, "sha256": digest}).encode()

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
            status, payload = client.upload_video(str(video_path), "t" * 43)
        self.assertEqual((200, "vid_" + "a" * 32), (status, payload["upload_id"]))
        self.assertEqual(raw, bytes(connection.sent))
        self.assertEqual(client.VIDEO_UPLOAD_PATH, connection.path)
        self.assertEqual(digest, connection.headers["X-HQ-Video-SHA256"])
        self.assertNotIn("secret-name.mp4", json.dumps(connection.headers))
        self.assertEqual("video/quicktime", client._video_mime(b"\x00\x00\x00\x18ftypqt  "))
        self.assertEqual("video/webm", client._video_mime(b"\x1aE\xdf\xa3"))

        link = Path(self.temp.name) / "linked.mp4"
        try:
            link.symlink_to(video_path)
        except OSError:
            pass
        else:
            with self.assertRaises(ValueError):
                client.upload_video(str(link), "t" * 43)
        with self.assertRaises(ValueError):
            client.upload_video("relative.mp4", "t" * 43)
        oversized = Path(self.temp.name) / "oversized.mp4"
        with oversized.open("wb") as handle:
            handle.write(b"\x00\x00\x00\x18ftypisom")
            handle.truncate(client.MAX_VIDEO_UPLOAD_BYTES + 1)
        with self.assertRaises(ValueError):
            client.upload_video(str(oversized), "t" * 43)

    def test_streaming_audio_client_enforces_magic_size_and_private_transport(self):
        raw = b"ID3" + b"private-audio"
        audio_path = Path(self.temp.name) / "secret-name.mp3"
        audio_path.write_bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()

        class Response:
            status = 200

            def read(self, _limit):
                return json.dumps({"upload_id": "aud_" + "a" * 32, "sha256": digest}).encode()

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
            status, payload = client.upload_audio(str(audio_path), "t" * 43)
        self.assertEqual((200, "aud_" + "a" * 32), (status, payload["upload_id"]))
        self.assertEqual(raw, bytes(connection.sent))
        self.assertEqual(client.AUDIO_UPLOAD_PATH, connection.path)
        self.assertEqual(digest, connection.headers["X-HQ-Audio-SHA256"])
        self.assertEqual("audio/mpeg", connection.headers["Content-Type"])
        self.assertNotIn("secret-name.mp3", json.dumps(connection.headers))
        self.assertEqual("audio/wav", client._audio_mime(b"RIFF1234WAVE"))
        self.assertEqual("audio/ogg", client._audio_mime(b"OggS"))
        self.assertEqual("audio/mp4", client._audio_mime(b"\x00\x00\x00\x18ftypM4A "))
        self.assertEqual("audio/aac", client._audio_mime(b"\xff\xf1"))

        link = Path(self.temp.name) / "linked.mp3"
        try:
            link.symlink_to(audio_path)
        except OSError:
            pass
        else:
            with self.assertRaises(ValueError):
                client.upload_audio(str(link), "t" * 43)
        with self.assertRaises(ValueError):
            client.upload_audio("relative.mp3", "t" * 43)
        oversized = Path(self.temp.name) / "oversized.mp3"
        with oversized.open("wb") as handle:
            handle.write(b"ID3")
            handle.truncate(client.MAX_AUDIO_UPLOAD_BYTES + 1)
        with self.assertRaises(ValueError):
            client.upload_audio(str(oversized), "t" * 43)

    def test_navigation_is_main_site_only_and_never_opens_by_default(self):
        with patch("hq_cli.cli.webbrowser.open") as opened:
            code, output, error = self.invoke(["run", "image", "--input", "@-"], b'{"prompt":"A & B"}')
            self.assertEqual(0, code, error)
            self.assertEqual("https://huangquechuanmei.com/workbench/banana?prompt=A+%26+B", self.payload(output)["result"]["url"])
            for identifier, path in {
                "text-video": "/workbench/text-video", "matrix-template": "/workbench/matrix-template.html",
                "short-drama": "/workbench/short-drama",
                "pricing-page": "/workbench/pricing", "invite": "/workbench/invite",
                "recharge": "/workbench/recharge", "bots": "/workbench/bots",
            }.items():
                code, output, error = self.invoke(["run", identifier])
                self.assertEqual(0, code, error)
                self.assertEqual("https://huangquechuanmei.com" + path, self.payload(output)["result"]["url"])
            opened.assert_not_called()
        code, output, error = self.invoke(["run", "image", "--environment", "zelong"])
        self.assertEqual(cli.EXIT_USAGE, code)

    def test_strict_inputs_reject_unknown_nonfinite_bad_boolean_and_arbitrary_base(self):
        cases = [
            (["run", "canvas", "--input", "@-"], b'{"collab":"no"}'),
            (["run", "audio-generate", "--input", "@-"], b'{"text":"x","speed":NaN}'),
            (["run", "video-generate", "--input", "@-"], b'{"prompt":"x","generate_audio":1}'),
            (["run", "video-generate", "--input", "@-"], b'{"prompt":"x","channel":"minimax","resolution":"768p"}'),
            (["run", "video-generate", "--input", "@-"], b'{"prompt":"x","channel":"sora","seconds":5}'),
            (["run", "asset-tags", "--input", "@-"], b'{"kind":"image","key":"x","tags":"not-array"}'),
            (["run", "asset-delete", "--input", "@-"], b'{"kind":"avatar","keys":["a_1"]}'),
            (["run", "asset-delete", "--input", "@-"], b'{"kind":"video","keys":[]}'),
            (["run", "asset-delete", "--input", "@-"], b'{"kind":"video","keys":"v_1"}'),
            (["run", "asset-delete", "--input", "@-"], b'{"kind":"video","keys":["v_1","v_1"]}'),
            (["run", "asset-delete", "--input", "@-"], b'{"kind":"video"}'),
            (["run", "asset-delete", "--input", "@-"], b'{"kind":"video","id":9,"keys":["v_1"]}'),
            (["run", "leads-generate", "--input", "@-"], b'{"keyword":"x","platforms":["twitter"]}'),
            (["run", "leads-generate", "--input", "@-"], b'{"platforms":["douyin"],"channels_targets":["target"]}'),
            (["run", "leads-generate", "--input", "@-"], b'{"keyword":"x","platforms":["channels"]}'),
            (["run", "leads-generate", "--input", "@-"], b'{"keyword":"x","platforms":["douyin","channels"]}'),
            (["run", "leads-generate", "--input", "@-"], b'{"platforms":["douyin","channels"],"channels_targets":["target"]}'),
            (["run", "image", "--base-url", "https://evil.example"], b""),
        ]
        with patch("hq_cli.client.request_json") as request:
            for argv, raw in cases:
                code, output, error = self.invoke(argv, raw)
                self.assertIn(code, {cli.EXIT_USAGE, cli.EXIT_INPUT})
                self.assertEqual("hq.error/v1", self.payload(error)["schema"])
        request.assert_not_called()

    def test_video_channel_contract_is_enforced_before_network(self):
        _, output, error = self.invoke(["capabilities"])
        self.assertFalse(error)
        video = {
            item["id"]: item for item in self.payload(output)["capabilities"]
        }["video-generate"]
        schema = video["input_schema"]
        self.assertTrue(schema["allOf"])
        rules = schema["x-hq-channel-rules"]
        self.assertEqual([4, 15], rules["micro"]["duration"])
        self.assertEqual([3, 10], rules["omni"]["duration"])
        self.assertTrue(any(
            "channel=omni accepts resolution=720p" in item
            and "JPEG/PNG/WebP" in item
            for item in video["constraints"]
        ))
        self.assertEqual(["2k"], rules["minimax"]["resolutions"])
        self.assertIn("21:9", rules["minimax"]["ratios"])
        self.assertNotIn("3:2", rules["minimax"]["ratios"])

        valid = (
            {"channel": "grok", "ratio": "3:2", "duration": 1, "resolution": "480p"},
            {"channel": "micro", "ratio": "21:9", "duration": 4, "resolution": "1080p"},
            {"channel": "omni", "ratio": "16:9", "duration": 3, "resolution": "720p"},
            {"channel": "minimax", "ratio": "adaptive", "duration": 4, "resolution": "2k"},
            {"channel": "sora", "model": "sora-2-pro", "seconds": 12,
             "ratio": "16:9", "resolution": "1080p"},
        )
        invalid = (
            {"channel": "grok", "model": "grok-imagine-video-1.5"},
            {"channel": "grok", "resolution": "480p",
             "reference_upload_ids": ["img_" + "a" * 32]},
            {"channel": "micro", "duration": 3},
            {"channel": "micro", "ratio": "3:2"},
            {"channel": "omni", "duration": 11},
            {"channel": "omni", "resolution": "1080p"},
            {"channel": "minimax", "duration": 3},
            {"channel": "minimax", "ratio": "3:2"},
            {"channel": "sora", "model": "sora-2", "resolution": "1080p"},
            {"channel": "grok", "resolution": "2k"},
        )

        with patch("hq_cli.client.request_json") as request:
            for fields in valid:
                with self.subTest(valid=fields):
                    raw = json.dumps({"prompt": "valid channel contract", **fields}).encode()
                    code, output, error = self.invoke(
                        ["run", "video-generate", "--input", "@-"], raw,
                    )
                    self.assertEqual(cli.EXIT_AUTH, code, error)
                    self.assertEqual("auth_required", self.payload(error)["error"])
            for fields in invalid:
                with self.subTest(invalid=fields):
                    raw = json.dumps({"prompt": "invalid channel contract", **fields}).encode()
                    code, output, error = self.invoke(
                        ["run", "video-generate", "--input", "@-"], raw,
                    )
                    self.assertEqual(cli.EXIT_INPUT, code, error)
                    self.assertEqual("input_error", self.payload(error)["error"])
            reference_limits = {"grok": 7, "micro": 9, "omni": 6, "minimax": 5, "sora": 1}
            for channel, limit in reference_limits.items():
                valid_references = ["img_" + format(index, "032x") for index in range(limit)]
                with self.subTest(channel=channel, references="max"):
                    raw = json.dumps({
                        "prompt": "reference limit", "channel": channel,
                        "reference_upload_ids": valid_references,
                    }).encode()
                    code, output, error = self.invoke(
                        ["run", "video-generate", "--input", "@-"], raw,
                    )
                    self.assertEqual(cli.EXIT_AUTH, code, error)
                with self.subTest(channel=channel, references="overflow"):
                    raw = json.dumps({
                        "prompt": "reference overflow", "channel": channel,
                        "reference_upload_ids": valid_references + ["img_" + format(limit, "032x")],
                    }).encode()
                    code, output, error = self.invoke(
                        ["run", "video-generate", "--input", "@-"], raw,
                    )
                    self.assertEqual(cli.EXIT_INPUT, code, error)
                    self.assertEqual("input_error", self.payload(error)["error"])
            raw = json.dumps({
                "prompt": "empty references", "reference_upload_ids": [],
            }).encode()
            code, output, error = self.invoke(
                ["run", "video-generate", "--input", "@-"], raw,
            )
            self.assertEqual(cli.EXIT_INPUT, code, error)
            self.assertEqual("input_error", self.payload(error)["error"])
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
