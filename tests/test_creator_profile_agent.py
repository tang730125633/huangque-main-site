import copy
import io
import json
import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from creator_agent.profile_agent import (
    DeepSeekProfileAgent, MODULES, ProfileAgentError, current_question, initial_state,
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
            "action": "answer", "accepted": True, "value": "企业AI顾问",
            "reply": "我了解了。接下来聊聊影响你最深的一次转折。",
            "next_question": {
                "question": "哪一次真实转折最影响现在的你？",
                "template": "当时___，后来___。", "options": [],
            },
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
        self.assertIn("action", body["messages"][0]["content"])
        self.assertIn("skip", body["messages"][0]["content"])
        self.assertGreaterEqual(timeout, 10)

    def test_deepseek_can_interpret_navigation_as_skip(self):
        opener = Opener({
            "action": "skip", "accepted": False, "value": "",
            "reply": "可以，我们换到下一题。",
            "next_question": {
                "question": "哪一次真实转折最影响现在的你？",
                "template": "当时___，后来___。", "options": [],
            },
        })
        agent = DeepSeekProfileAgent("secret", opener=opener)
        result = agent.capture_answer(initial_state(), "下一个问题")
        self.assertEqual(result["action"], "skip")
        self.assertFalse(result["accepted"])

    def test_capture_rejects_inconsistent_model_action(self):
        opener = Opener({
            "action": "skip", "accepted": True, "value": "虚构答案",
            "reply": "已处理", "next_question": {
                "question": "哪一次真实转折最影响现在的你？",
                "template": "当时___，后来___。", "options": [],
            },
        })
        agent = DeepSeekProfileAgent("secret", opener=opener)
        with self.assertRaises(ProfileAgentError):
            agent.capture_answer(initial_state(), "下一个问题")

    def test_clarification_is_a_deepseek_generated_question_without_advancing(self):
        opener = Opener({
            "action": "clarify", "accepted": False, "value": "",
            "reply": "你可以先说说最近投入最多精力的一件事。",
            "next_question": {
                "question": "最近哪件工作最能代表你现在的状态？",
                "template": "我最近主要在___。", "options": [],
            },
        })
        agent = DeepSeekProfileAgent("secret", opener=opener)
        result = agent.capture_answer(initial_state(), "不知道怎么说")
        self.assertEqual(result["action"], "clarify")
        self.assertEqual(result["next_question"]["key"], "identity")

    def test_deepseek_generates_the_displayed_profile_question(self):
        opener = Opener({
            "reply": "我们先从你现在正在做的事情聊起。",
            "question": {
                "question": "你最近主要把时间投入在哪件事情上？",
                "template": "我目前主要在做___。", "options": [],
            },
        })
        agent = DeepSeekProfileAgent("secret", opener=opener)
        generated = agent.ask_question(initial_state(), "首次进入")
        self.assertEqual(
            generated["question"]["question"],
            "你最近主要把时间投入在哪件事情上？",
        )
        request = json.loads(opener.requests[0][0].data)
        self.assertIn("不要照抄字段目标", request["messages"][0]["content"])

    def test_deepseek_interprets_free_text_and_composes_final_reply(self):
        intent_agent = DeepSeekProfileAgent("secret", opener=Opener({
            "intent": "start_video",
            "payload": {"topic": "企业先梳理流程", "platforms": ["douyin"]},
        }))
        interpreted = intent_agent.interpret_intent(
            {}, {"mode": "idle"}, "做一个抖音视频：企业先梳理流程",
        )
        self.assertEqual(interpreted["intent"], "start_video")
        self.assertEqual(interpreted["payload"]["platforms"], ["douyin"])

        reply_agent = DeepSeekProfileAgent("secret", opener=Opener({
            "reply": "方案已经准备好，你可以继续修改或确认。",
        }))
        reply = reply_agent.compose_reply(
            {}, "生成视频", {"kind": "video_plan"}, "方案已经生成",
        )
        self.assertIn("继续修改或确认", reply)

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

    def test_module_review_rejects_missing_candidate_fields(self):
        opener = Opener({
            "summary": "定位总结",
            "options": [
                {
                    "title": "方案%d" % index,
                    "one_liner": "一句话定位%d" % index,
                    "strengths": "not-a-list" if index == 1 else ["真实经历"],
                    "risks": ["仍需验证"],
                }
                for index in range(1, 4)
            ],
        })
        agent = DeepSeekProfileAgent("secret", opener=opener)
        with self.assertRaises(ProfileAgentError):
            agent.build_module_review(initial_state(), 1)

    def test_topic_plan_validates_titles_recommendations_and_scripts(self):
        valid = {
            "reply": "已完成选题计划",
            "topics": [{"title": "选题%d" % index} for index in range(1, 16)],
            "recommended": ["选题1", "选题2", "选题3"],
            "scripts": [{"platform": "douyin", "content": "这是一篇可以直接发布的完整文案"}],
        }
        agent = DeepSeekProfileAgent("secret", opener=Opener(valid))
        result = agent.topic_plan({}, ["douyin"], "生成选题")
        self.assertEqual(len(result["topics"]), 15)
        self.assertEqual(result["recommended"], ["选题1", "选题2", "选题3"])

        malformed_cases = []
        missing_title = copy.deepcopy(valid)
        missing_title["topics"][0] = {}
        malformed_cases.append(missing_title)
        unknown_recommendation = copy.deepcopy(valid)
        unknown_recommendation["recommended"][0] = "不存在的选题"
        malformed_cases.append(unknown_recommendation)
        wrong_script_platform = copy.deepcopy(valid)
        wrong_script_platform["scripts"] = [
            {"platform": "unknown", "content": "完整文案"},
        ]
        malformed_cases.append(wrong_script_platform)
        for malformed in malformed_cases:
            with self.subTest(malformed=malformed):
                agent = DeepSeekProfileAgent("secret", opener=Opener(malformed))
                with self.assertRaises(ProfileAgentError):
                    agent.topic_plan({}, ["douyin"], "生成选题")


if __name__ == "__main__":
    unittest.main()
