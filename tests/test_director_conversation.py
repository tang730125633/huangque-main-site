import json
import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
from content_domains import director_agent as agent, director_conversation as chat
from tests.test_director_agent_explicit_confirmation import payload


def text_reply(text="你好，想做什么内容？", protocol="responses"):
    if protocol == "chat_completions":
        return {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": text}}]}
    return {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]}


def tool_reply(name="hq_cli_page_guide", arguments=None, call_id="call_1", protocol="responses"):
    args = json.dumps({"page": "script"} if arguments is None else arguments)
    if protocol == "chat_completions":
        return {"choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "content": None,
            "tool_calls": [{"type": "function", "id": call_id, "function": {"name": name, "arguments": args}}]}}]}
    return {"status": "completed", "output": [{"type": "function_call", "name": name,
            "arguments": args, "call_id": call_id}]}


class DirectorConversationTests(unittest.TestCase):
    def request(self, **updates):
        request = agent.validate_payload(payload(prompt="你好", **updates))
        request.update(_username="account-not-for-model", _job_id=42)
        return request

    def converse(self, replies, protocol="responses", request=None, guide=None):
        self.post = mock.Mock(side_effect=replies)
        self.guide = guide or mock.Mock(return_value={"capability": {"id": "script"}})
        raw = chat.converse(request or self.request(), post=self.post,
            model="deepseek-v4-flash" if protocol == "chat_completions" else "configured-model",
            protocol=protocol, reasoning_effort="low",
            action_schema=agent.DIRECTOR_AGENT_SCHEMA["properties"]["actions"], page_guide=self.guide)
        return json.loads(raw)

    def test_plain_greeting_needs_no_cli_or_json_schema_in_either_protocol(self):
        for protocol in ("responses", "chat_completions"):
            with self.subTest(protocol=protocol):
                value = self.converse([text_reply(protocol=protocol)], protocol)
                self.assertEqual("你好，想做什么内容？", value["content"])
                self.assertEqual([], value["actions"])
                self.assertFalse(value["offer_production"])
                self.guide.assert_not_called()
                self.assertEqual(1, self.post.call_count)
                body = json.loads(self.post.call_args.args[1])
                self.assertEqual("auto", body["tool_choice"])
                self.assertNotIn("json_schema", json.dumps(body))
                self.assertNotIn("account-not-for-model", json.dumps(body))

    def test_deepseek_parameters_and_native_chat_protocol(self):
        self.converse([text_reply(protocol="chat_completions")], "chat_completions")
        path, data, _ = self.post.call_args.args
        self.assertEqual("/v1/chat/completions", path)
        body = json.loads(data)
        self.assertEqual({"type": "disabled"}, body["thinking"])
        self.assertEqual((0.4, 2200), (body["temperature"], body["max_tokens"]))
        self.assertNotIn("instructions", body)
        self.assertNotIn("response_format", body)

    def test_history_is_roles_not_flattened_and_current_system_always_wins(self):
        history = [{"role": "assistant", "content": "orphan"},
                   {"role": "user", "content": "饮料买三送一"},
                   {"role": "assistant", "content": "旧长答案" * 60}]
        for protocol in ("responses", "chat_completions"):
            self.converse([text_reply(protocol=protocol)], protocol, self.request(history=history))
            body = json.loads(self.post.call_args.args[1])
            messages = body.get("input", body.get("messages"))
            if protocol == "chat_completions":
                self.assertEqual("system", messages.pop(0)["role"])
            self.assertEqual(["user", "assistant", "user"], [m["role"] for m in messages])
            self.assertEqual("饮料买三送一", messages[0]["content"])
            self.assertIn("买三送一", chat.SYSTEM_PROMPT)
            self.assertIn("不是模仿范例", chat.SYSTEM_PROMPT)

    def test_requested_draft_is_not_cut_to_eighty_characters(self):
        draft = "可直接使用的分镜草稿。" * 200
        result = self.converse([text_reply(draft)])
        normalized = agent.normalize_model_result(json.dumps(result), self.request())
        self.assertEqual(draft, normalized["content"])
        self.assertNotIn("_pending_production_plan", normalized)

    def test_cli_tool_receipt_returns_to_model_in_both_protocols(self):
        for protocol in ("responses", "chat_completions"):
            self.converse([tool_reply(protocol=protocol), text_reply("已核实素材要求。", protocol)], protocol)
            self.guide.assert_called_once_with("script")
            body = json.loads(self.post.call_args.args[1])
            messages = body.get("input", body.get("messages"))
            receipt = messages[-1]
            self.assertEqual("call_1", receipt.get("call_id", receipt.get("tool_call_id")))
            self.assertIn("capability", receipt.get("output", receipt.get("content")))

    def test_two_page_queries_are_cached_not_reexecuted(self):
        self.converse([tool_reply(), tool_reply(call_id="call_2"), text_reply()])
        self.guide.assert_called_once_with("script")

    def test_query_other_supported_page_does_not_navigate(self):
        result = self.converse([tool_reply(arguments={"page": "digital_human_oneclick"}), text_reply()])
        self.guide.assert_called_once_with("digital_human_oneclick")
        self.assertEqual([], result["actions"])

    def test_unknown_paid_and_shell_tools_rejected_before_execution(self):
        for name in ("run", "confirm", "login", "upload", "shell", "director-script-generate"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.converse([tool_reply(name=name)])
            self.guide.assert_not_called()

    def test_tools_cannot_take_arbitrary_paths_accounts_or_urls(self):
        for args in ({"page": "../../etc"}, {"page": "script", "username": "other"},
                     {"page": "script", "url": "http://internal"}, [], None):
            reply = tool_reply(arguments=args if args is not None else {"page": None})
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.converse([reply])
            self.guide.assert_not_called()

    def test_duplicate_call_id_rejected(self):
        with self.assertRaises(ValueError):
            self.converse([tool_reply(), tool_reply()])
        self.guide.assert_called_once()

    def test_malformed_json_and_oversize_arguments_are_rejected(self):
        for args in ("{bad", "x" * 16001):
            reply = tool_reply()
            reply["output"][0]["arguments"] = args
            with self.assertRaises(ValueError):
                self.converse([reply])
            self.guide.assert_not_called()

    def test_tool_and_round_budgets_are_bounded(self):
        reply = tool_reply()
        reply["output"] *= 3
        with self.assertRaises(ValueError):
            self.converse([reply])
        with self.assertRaises(ValueError):
            self.converse([tool_reply(call_id="call_" + str(i)) for i in range(chat.MAX_ROUNDS)])
        self.assertEqual(chat.MAX_ROUNDS, self.post.call_count)

    def test_deadline_prevents_another_model_call(self):
        with mock.patch.object(chat.time, "monotonic", side_effect=[0, 151]), self.assertRaises(ValueError):
            self.converse([text_reply()])
        self.post.assert_not_called()

    def test_incomplete_empty_refusal_and_tool_error_do_not_become_boilerplate(self):
        for reply in ({"status": "incomplete"}, text_reply(""),
                      {"output": [{"type": "message", "content": [{"type": "refusal"}]}]}):
            with self.subTest(reply=reply), self.assertRaises(ValueError):
                self.converse([reply])
        with self.assertRaises(ValueError):
            self.converse([text_reply("x" * 5001)])
        with self.assertRaises(ValueError):
            agent.normalize_model_result("bad protocol", self.request())
        with self.assertRaises(agent.director_cli.DirectorCLIError):
            self.converse([tool_reply()], guide=mock.Mock(side_effect=agent.director_cli.DirectorCLIError("unavailable")))

    def test_script_proposal_is_inert_and_still_passes_existing_plan_gate(self):
        request = self.request()
        result = self.converse([tool_reply("prepare_script_plan", {"actions": []}), text_reply("已整理方案。")], request=request)
        self.assertTrue(result["offer_production"])
        with mock.patch.object(agent.director_cli, "production_is_available", return_value=True):
            normalized = agent.normalize_model_result(json.dumps(result), request)
        self.assertIn("_pending_production_plan", normalized)
        self.assertNotIn("production_offer", normalized)
        self.assertIn("确认生成", normalized["content"])

    def test_actions_still_reject_generation_and_cross_page_targets(self):
        for action in ({"type": "execute", "target": "generate_video"},
                       {"type": "fill_field", "field": "digital_human_script", "value": "draft", "label": "fill"}):
            result = self.converse([tool_reply("propose_page_actions", {"actions": [action]}), text_reply()])
            with self.assertRaises(ValueError):
                agent.normalize_model_result(json.dumps(result), self.request())

    def test_failed_turn_cannot_return_partially_staged_actions(self):
        with self.assertRaises(ValueError):
            self.converse([tool_reply("prepare_script_plan", {"actions": []}), text_reply("")])

    def test_two_proposals_cannot_overwrite_each_other(self):
        with self.assertRaises(ValueError):
            self.converse([tool_reply("prepare_script_plan", {"actions": []}),
                           tool_reply("propose_page_actions", {"actions": []}, "call_2")])

    def test_scope_safe_provider_pair_is_used_on_every_post(self):
        request = self.request()
        with mock.patch.object(agent, "API_BASE", "https://example.invalid/v1"), \
             mock.patch.object(agent, "API_KEY", "dedicated-test-key"), \
             mock.patch.object(agent, "MODEL", "deepseek-v4-flash"), \
             mock.patch.dict(agent.os.environ, {"DIRECTOR_AGENT_API_PROTOCOL": "auto"}), \
             mock.patch.object(agent.director_cli, "page_guide", return_value={}), \
             mock.patch.object(agent, "_post", side_effect=[tool_reply(protocol="chat_completions"),
                    text_reply(protocol="chat_completions")]) as post:
            agent._responses_chat(request)
        self.assertEqual(2, post.call_count)
        for call in post.call_args_list:
            self.assertEqual("https://example.invalid/v1", call.kwargs["base"])
            self.assertEqual("dedicated-test-key", call.kwargs["key"])
            self.assertNotIn("dedicated-test-key", call.args[1].decode())

    def test_dedicated_endpoint_never_inherits_global_key(self):
        with mock.patch.object(agent, "API_BASE", "https://example.invalid"), \
             mock.patch.object(agent, "API_KEY", None), mock.patch.object(agent, "_post") as post:
            with self.assertRaises(ValueError):
                agent._responses_chat(self.request())
        post.assert_not_called()

    def test_customer_cannot_select_protocol_or_model(self):
        for key in ("model", "protocol", "DIRECTOR_AGENT_API_PROTOCOL"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                agent.validate_payload(payload(**{key: "attacker"}))

    def test_invalid_server_protocol_fails_before_network(self):
        with self.assertRaises(ValueError):
            self.converse([text_reply()], "arbitrary")
        self.post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
