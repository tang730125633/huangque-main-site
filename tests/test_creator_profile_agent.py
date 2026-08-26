import io
import json
import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from creator_agent.profile_agent import (
    DeepSeekProfileAgent, MODULES, current_question, initial_state,
)


class Response:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return io.BytesIO(json.dumps(self.value).encode("utf-8"))

    def __exit__(self, *_args):
        return False


class Opener:
    def __init__(self, payload):
        self.payload = payload
        self.requests = []

    def open(self, request, timeout=0):
        self.requests.append((request, timeout))
        return Response({
            "choices": [{"message": {"content": json.dumps(self.payload, ensure_ascii=False)}}],
        })


class CreatorProfileAgentTests(unittest.TestCase):
    def test_question_map_is_independent_and_reuses_compact_intake(self):
        state = initial_state()
        first = current_question(state)
        self.assertEqual((first["module"], first["key"]), (1, "identity"))
        self.assertEqual([len(MODULES[index]["questions"]) for index in range(1, 5)], [5, 2, 2, 2])
        self.assertNotIn("ip12", json.dumps(MODULES, ensure_ascii=False).lower())

    def test_deepseek_v4_flash_chat_contract_and_json_capture(self):
        opener = Opener({
            "accepted": True, "value": "企业AI顾问",
            "ack": "已记录", "clarification": "",
        })
        agent = DeepSeekProfileAgent(
            "secret", base_url="https://api.deepseek.com",
            model="deepseek-v4-flash", opener=opener,
        )
        result = agent.capture_answer(initial_state(), "我是企业AI顾问")
        self.assertTrue(result["accepted"])
        self.assertEqual(result["value"], "企业AI顾问")
        request, timeout = opener.requests[0]
        body = json.loads(request.data)
        self.assertEqual(request.full_url, "https://api.deepseek.com/chat/completions")
        self.assertEqual(body["model"], "deepseek-v4-flash")
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertGreaterEqual(timeout, 10)

    def test_only_exact_deepseek_v4_flash_configuration_is_ready(self):
        self.assertTrue(DeepSeekProfileAgent("key", model="deepseek-v4-flash").configured)
        self.assertFalse(DeepSeekProfileAgent("", model="deepseek-v4-flash").configured)
        self.assertFalse(DeepSeekProfileAgent("key", model="deepseek-chat").configured)
        self.assertFalse(DeepSeekProfileAgent(
            "key", base_url="https://relay.example.com", model="deepseek-v4-flash",
        ).configured)

    def test_health_verifies_official_model_catalog(self):
        class ModelsOpener:
            def open(self, request, timeout=0):
                self.url = request.full_url
                self.timeout = timeout
                return Response({
                    "data": [{"id": "deepseek-v4-flash"}, {"id": "deepseek-v4-pro"}],
                })

        opener = ModelsOpener()
        agent = DeepSeekProfileAgent("key", opener=opener)
        self.assertTrue(agent.health(force=True))
        self.assertEqual(opener.url, "https://api.deepseek.com/models")
        self.assertEqual(opener.timeout, 5)


if __name__ == "__main__":
    unittest.main()
