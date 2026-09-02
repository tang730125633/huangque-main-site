import io
import json
import unittest

from hq_cli import mcp_server
from hq_cli.catalog import CAPABILITIES


class McpServerTests(unittest.TestCase):
    def test_every_capability_is_a_distinct_typed_tool(self):
        tools = mcp_server.list_tools()
        by_name = {tool["name"]: tool for tool in tools}
        self.assertEqual(len(CAPABILITIES) + len(mcp_server.CONTROL_TOOLS), len(tools))
        self.assertEqual(len(tools), len(by_name))
        for identifier in CAPABILITIES:
            self.assertIn(mcp_server.capability_tool_name(identifier), by_name)
        paid = by_name["hq_collect_content"]["inputSchema"]
        self.assertIn("url", paid["properties"])
        self.assertIn("confirm", paid["properties"])
        self.assertIn("quote_token", paid["properties"])
        upload = by_name["hq_image_upload"]["inputSchema"]
        self.assertEqual({"file", "confirm"}, set(upload["required"]))
        digital_human_audio = by_name[
            "hq_digital_human_oneclick_audio_upload"
        ]["inputSchema"]
        self.assertEqual(
            {"file", "confirm", "run_id"},
            set(digital_human_audio["required"]),
        )
        self.assertEqual(
            "^dh-run-[A-Za-z0-9._:-]{8,128}$",
            digital_human_audio["properties"]["run_id"]["pattern"],
        )
        director_upload = by_name["hq_director_breakdown_upload"]["inputSchema"]
        self.assertEqual({"file"}, set(director_upload["required"]))
        self.assertEqual(
            {"file", "confirm", "quote_token", "expected_cost"},
            set(director_upload["properties"]),
        )
        single = by_name["hq_matrix_template_generate"]["inputSchema"]
        self.assertIn("font_family", single["properties"])
        self.assertIn("quote_token", single["properties"])
        batch = by_name["hq_matrix_template_batch_generate"]["inputSchema"]
        self.assertEqual((2, 5), (
            batch["properties"]["count"]["minimum"],
            batch["properties"]["count"]["maximum"],
        ))
        self.assertIn("count", batch["required"])
        download = by_name["hq_dl"]["inputSchema"]
        self.assertEqual({"url", "output_file"}, set(download["required"]))
        self.assertTrue(by_name["hq_asset_delete"]["annotations"]["destructiveHint"])

    def test_download_maps_output_file_without_sending_it_to_the_server(self):
        calls = []

        def runner(arguments, stdin_text):
            calls.append((arguments, json.loads(stdin_text)))
            return 0, {"schema": "hq.run/v1", "result": {"path": "/tmp/result.mp4"}}

        result = mcp_server.call_tool("hq_dl", {
            "url": "https://video.huangquechuanmei.com/result.mp4",
            "output_file": "/tmp/result.mp4",
        }, runner=runner)
        self.assertNotIn("isError", result)
        self.assertEqual(([
            "run", "dl", "--input", "@-", "--output", "/tmp/result.mp4",
        ], {"url": "https://video.huangquechuanmei.com/result.mp4"}), calls[0])

    def test_paid_call_reuses_cli_quote_and_confirmation_arguments(self):
        calls = []

        def runner(arguments, stdin_text):
            calls.append((arguments, json.loads(stdin_text)))
            return 0, {"schema": "hq.run/v1", "result": {"job_id": 12}}

        result = mcp_server.call_tool("hq_collect_content", {
            "url": "https://www.bilibili.com/video/BV1xx411c7mD",
            "confirm": True,
            "quote_token": "q.test",
        }, runner=runner)
        self.assertNotIn("isError", result)
        self.assertEqual({"url": "https://www.bilibili.com/video/BV1xx411c7mD"}, calls[0][1])
        self.assertEqual([
            "run", "collect-content", "--input", "@-", "--confirm", "--quote-token", "q.test",
        ], calls[0][0])

    def test_paid_director_upload_quotes_then_reuses_cost_and_quote_token(self):
        calls = []

        def runner(arguments, stdin_text):
            calls.append((arguments, stdin_text))
            return 0, {"schema": "hq.run/v1", "result": {"cost": 20}}

        quoted = mcp_server.call_tool("hq_director_breakdown_upload", {
            "file": "/tmp/director-reference.png",
        }, runner=runner)
        confirmed = mcp_server.call_tool("hq_director_breakdown_upload", {
            "file": "/tmp/director-reference.png", "confirm": True,
            "quote_token": "q.director.upload", "expected_cost": 20,
        }, runner=runner)
        self.assertNotIn("isError", quoted)
        self.assertNotIn("isError", confirmed)
        self.assertEqual(([
            "run", "director-breakdown-upload", "--file", "/tmp/director-reference.png",
        ], ""), calls[0])
        self.assertEqual(([
            "run", "director-breakdown-upload", "--file", "/tmp/director-reference.png",
            "--confirm", "--quote-token", "q.director.upload", "--expected-cost", "20",
        ], ""), calls[1])

    def test_digital_human_audio_upload_maps_required_run_id(self):
        calls = []

        def runner(arguments, stdin_text):
            calls.append((arguments, stdin_text))
            return 0, {
                "schema": "hq.run/v1",
                "result": {"audio_upload_id": "dha_" + "a" * 32},
            }

        missing = mcp_server.call_tool(
            "hq_digital_human_oneclick_audio_upload",
            {"file": "/tmp/complete.mp3", "confirm": True},
            runner=runner,
        )
        invalid = mcp_server.call_tool(
            "hq_digital_human_oneclick_audio_upload",
            {"file": "/tmp/complete.mp3", "run_id": "bad", "confirm": True},
            runner=runner,
        )
        confirmed = mcp_server.call_tool(
            "hq_digital_human_oneclick_audio_upload",
            {
                "file": "/tmp/complete.mp3",
                "run_id": "dh-run-audio-0001",
                "confirm": True,
            },
            runner=runner,
        )
        self.assertTrue(missing["isError"])
        self.assertTrue(invalid["isError"])
        self.assertNotIn("isError", confirmed)
        self.assertEqual([([
            "run", "digital-human-oneclick-audio-upload",
            "--file", "/tmp/complete.mp3",
            "--run-id", "dh-run-audio-0001", "--confirm",
        ], "")], calls)

    def test_template_batch_passes_one_confirmation_to_the_fixed_cli_action(self):
        calls = []

        def runner(arguments, stdin_text):
            calls.append((arguments, json.loads(stdin_text)))
            return 0, {"schema": "hq.run/v1", "result": {"job_ids": [1, 2]}}

        payload = {
            "top_text": "顶部标题", "bottom_text": "底部行动文案",
            "template_id": "poster-split", "font_family": "Noto Sans SC",
            "count": 2, "confirm": True, "quote_token": "q.batch",
        }
        result = mcp_server.call_tool("hq_matrix_template_batch_generate", payload, runner=runner)
        self.assertNotIn("isError", result)
        self.assertEqual({
            "top_text": "顶部标题", "bottom_text": "底部行动文案",
            "template_id": "poster-split", "font_family": "Noto Sans SC", "count": 2,
        }, calls[0][1])
        self.assertEqual([
            "run", "matrix-template-batch-generate", "--input", "@-",
            "--confirm", "--quote-token", "q.batch",
        ], calls[0][0])

    def test_write_and_logout_are_blocked_without_confirmation(self):
        calls = []

        def runner(arguments, stdin_text):
            calls.append(arguments)
            return 0, {"ok": True}

        write = mcp_server.call_tool("hq_inspiration_like", {"id": 1001, "favorite": True}, runner=runner)
        logout = mcp_server.call_tool("hq_cli_logout", {}, runner=runner)
        self.assertTrue(write["isError"])
        self.assertTrue(logout["isError"])
        self.assertEqual([], calls)

    def test_stdio_handshake_lists_tools_and_calls_cli(self):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "hq_cli_version", "arguments": {}}},
        ]
        source = io.StringIO("".join(json.dumps(item) + "\n" for item in requests))
        output = io.StringIO()

        def runner(arguments, stdin_text):
            self.assertEqual(["version"], arguments)
            return 0, {"schema": "hq.version/v1", "cli_version": "0.14.1"}

        self.assertEqual(0, mcp_server.serve(source, output, runner=runner))
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([1, 2, 3, 4], [item["id"] for item in responses])
        self.assertEqual("huangque", responses[0]["result"]["serverInfo"]["name"])
        self.assertEqual("2025-06-18", responses[0]["result"]["protocolVersion"])
        self.assertEqual({}, responses[1]["result"])
        self.assertEqual(len(CAPABILITIES) + len(mcp_server.CONTROL_TOOLS), len(responses[2]["result"]["tools"]))
        self.assertEqual("hq.version/v1", responses[3]["result"]["structuredContent"]["schema"])

    def test_current_protocol_uses_stateless_request_metadata(self):
        meta = {
            mcp_server.PROTOCOL_META: mcp_server.PROTOCOL_VERSION,
            mcp_server.CLIENT_CAPABILITIES_META: {},
        }
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {"_meta": meta}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {"_meta": meta}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "_meta": meta, "name": "hq_cli_version", "arguments": {},
            }},
        ]
        source = io.StringIO("".join(json.dumps(item) + "\n" for item in requests))
        output = io.StringIO()
        calls = []

        def runner(arguments, stdin_text):
            calls.append((arguments, stdin_text))
            return 0, {"schema": "hq.version/v1", "cli_version": "0.14.1"}

        self.assertEqual(0, mcp_server.serve(source, output, runner=runner))
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(mcp_server.PROTOCOL_VERSION, responses[0]["result"]["supportedVersions"][0])
        self.assertEqual(
            {"name": "huangque", "version": "0.14.1"},
            responses[1]["result"]["_meta"][mcp_server.SERVER_INFO_META],
        )
        self.assertEqual([(["version"], "")], calls)
        self.assertEqual("hq.version/v1", responses[2]["result"]["structuredContent"]["schema"])
        self.assertIn(mcp_server.SERVER_INFO_META, responses[2]["result"]["_meta"])

    def test_current_protocol_rejects_missing_or_unknown_metadata(self):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {"_meta": {
                mcp_server.PROTOCOL_META: "9999-01-01",
                mcp_server.CLIENT_CAPABILITIES_META: {},
            }}},
            {"jsonrpc": "2.0", "id": 3, "method": "initialize", "params": {
                "protocolVersion": mcp_server.PROTOCOL_VERSION,
            }},
        ]
        output = io.StringIO()
        self.assertEqual(0, mcp_server.serve(
            io.StringIO("".join(json.dumps(item) + "\n" for item in requests)), output,
        ))
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([-32602, -32022, -32022], [item["error"]["code"] for item in responses])
        self.assertEqual("9999-01-01", responses[1]["error"]["data"]["requested"])
        self.assertIn(mcp_server.PROTOCOL_VERSION, responses[1]["error"]["data"]["supported"])

    def test_stdio_connection_locks_legacy_or_modern_era(self):
        meta = {
            mcp_server.PROTOCOL_META: mcp_server.PROTOCOL_VERSION,
            mcp_server.CLIENT_CAPABILITIES_META: {},
        }
        legacy_then_modern = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-11-25",
            }},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {"_meta": meta}},
        ]
        output = io.StringIO()
        self.assertEqual(0, mcp_server.serve(
            io.StringIO("".join(json.dumps(item) + "\n" for item in legacy_then_modern)), output,
        ))
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(-32022, responses[1]["error"]["code"])

        modern_then_legacy = [
            {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {"_meta": meta}},
            {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {
                "protocolVersion": "2025-11-25",
            }},
            {"jsonrpc": "2.0", "id": 3, "method": "ping", "params": {"_meta": meta}},
        ]
        output = io.StringIO()
        self.assertEqual(0, mcp_server.serve(
            io.StringIO("".join(json.dumps(item) + "\n" for item in modern_then_legacy)), output,
        ))
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(-32022, responses[1]["error"]["code"])
        self.assertEqual(-32601, responses[2]["error"]["code"])

    def test_parse_error_returns_json_rpc_error_with_null_id(self):
        output = io.StringIO()
        self.assertEqual(0, mcp_server.serve(io.StringIO("{bad json\n"), output))
        response = json.loads(output.getvalue())
        self.assertIsNone(response["id"])
        self.assertEqual(-32700, response["error"]["code"])


if __name__ == "__main__":
    unittest.main()
