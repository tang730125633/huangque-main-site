import copy
from contextlib import closing
import json
from http.server import ThreadingHTTPServer
import pathlib
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.error
import urllib.request


ROOT = pathlib.Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
sys.path.insert(0, str(SERVER))

from creator_agent.planner import CreatorPlanner, GuidedPlanner, remember_preference
from creator_agent.profile_agent import (
    DeepSeekProfileAgent, MODULES, current_question, initial_state,
    next_question_goal,
)
from creator_agent.profile_pdf import ProfilePDFError
from creator_agent.model_usage import ModelUsageGuard
from creator_agent.service import APIError, CreatorAgentHandler, CreatorAgentService, build_service
from creator_agent.store import (
    CreatorAgentStore, StateConflict, STALE_CLAIM_SECONDS,
)
import hq_cli_api


USER = {
    "username": "creator-qa", "account_id": "HQ-ACCOUNT-1",
    "name": "Creator QA", "points": 1000,
}
PROJECT_ID = "a1b2c3d4e5f6"

PROFILE_ANSWERS = {
    "basic_context": "空黎，90后，现居广州。",
    "career_identity": "我是企业AI顾问，主要帮助企业梳理并落地AI工作流程。",
    "career_history": "我从业两年，持续从事AI产品和自动化项目开发。",
    "income_context": "主要收入来自AI项目咨询和定制开发，目前处于10-30万阶段。",
    "low_point": "我曾经长期找不到合适工作，后来通过持续学习并完成独立AI项目走出低谷。",
    "achievement": "我独立完成了第一个AI项目，并成功交付给真实客户使用。",
    "praised_traits": "别人经常夸我耐心细致，因为我会陪客户把问题解决到底。",
    "criticized_traits": "别人常说我容易钻研过头，导致沟通时解释得过于细致。",
    "proven_ability": "我最擅长AI项目落地，曾经独立完成从需求分析到交付的完整项目。",
    "content_track": "我想长期专注AI实战与效率提升赛道，持续分享可落地的方法。",
    "target_audience": "我想服务不了解AI或刚入门、尚未掌握实际使用方法的人群。",
    "audience_pain": "他们面临产出低、成本高和不会选择工具的问题，我能提供落地方案。",
    "differentiation": "我的差异是能独立完成真实AI项目，证据是已有从需求到交付的客户案例。",
    "existing_accounts": "目前还没有持续运营的内容账号。",
    "personality_words": "耐心、理性、幽默。",
    "communication_style": "我希望表达幽默轻松，但专业结论必须清晰准确。",
    "disliked_style": "我不喜欢啰嗦冗长的表达，因为浪费时间且重点不突出。",
    "content_habits": "我常分享学习心得和实战复盘，不发布未经验证的娱乐八卦。",
    "memorable_statement": "先把真实问题解决，再谈AI能创造多少价值。",
    "self_intro": "我是AI入门导师，专门帮助AI小白解决不会落地使用的问题。",
    "trust_reason": "客户愿意信任我，因为我耐心细致，并持续陪伴他们完成真实项目。",
    "ip_goal": "引流获客",
    "time_commitment": "我每天可以稳定投入1到3小时进行内容创作。",
    "products_services": "我提供AI咨询、工具定制开发和项目代做，按照项目范围报价交付。",
    "short_term_goal": "未来三个月积累10个可验证的客户案例。",
    "long_term_goal": "未来一年建立自己的AI社群，并实现稳定的自由工作。",
    "comeback_story": "我失业后长期找不到方向，随后系统学习AI并独立完成项目，最终获得首个客户认可。",
    "pitfall_story": "我曾因需求确认不充分导致项目返工，后来建立验收清单，避免再次损失时间。",
    "success_story": "我从没有项目经验开始，每天实践并复盘，最终独立完成并交付第一个AI项目。",
    "dramatic_story": "我曾在客户准备放弃项目时开始连夜定位问题，第二天完成修复并让项目顺利上线。",
    "team_project": "我负责过一个跨角色AI项目，协调需求和开发交付，最终按期完成并总结流程。",
}


class FakeAuth:
    base_url = "http://127.0.0.1:8095"

    def verify(self, headers):
        return dict(USER)


class MutableClock:
    def __init__(self, value=2_000_000_000):
        self.value = int(value)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += int(seconds)


class FakeProfileAgent:
    configured = True

    def __init__(self):
        self.calls = []

    def health(self):
        return True

    @staticmethod
    def _question(goal):
        if not goal:
            return None
        return {
            "module": goal["module"], "module_name": goal["module_name"],
            "key": goal["key"], "question": "DeepSeek提问：" + goal["question"],
            "template": goal.get("template") or "", "options": goal.get("options") or [],
        }

    def ask_question(self, state, transition=""):
        goal = current_question({**state, "active_question": None})
        question = self._question(goal)
        self.calls.append(("ask_question", copy.deepcopy(state), transition))
        return {"reply": question["question"], "question": question}

    def capture_answer(self, state, message):
        self.calls.append(("capture", copy.deepcopy(state), message))
        next_question = self._question(next_question_goal(state))
        if message in {"跳过", "下一个问题", "继续下一个问题"}:
            return {
                "action": "skip", "accepted": False, "value": "",
                "reply": (
                    "DeepSeek已理解你想跳过。" +
                    ((" " + next_question["question"]) if next_question else "")
                ),
                "next_question": next_question,
            }
        return {
            "action": "answer", "accepted": True, "value": message,
            "reply": (
                "DeepSeek已理解并记录。" +
                ((" " + next_question["question"]) if next_question else "")
            ),
            "next_question": next_question,
        }

    def build_module_review(self, state, module):
        self.calls.append(("review", module))
        return {
            "module": module, "module_name": "模块%d" % module,
            "summary": "模块%d总结" % module,
            "options": [
                {"title": "方案%d" % index, "one_liner": "定位%d" % index,
                 "strengths": ["真实"], "risks": ["待验证"]}
                for index in range(1, 4)
            ],
        }

    def revise_module_review(self, state, module, instruction):
        result = self.build_module_review(state, module)
        result["reply"] = "DeepSeek已按你的要求更新本模块，请重新选择。"
        result["summary"] = "已修改：" + instruction
        return result

    def topic_plan(self, profile, platforms, request):
        self.calls.append(("topic_plan", copy.deepcopy(profile), list(platforms), request))
        return {
            "reply": "已生成选题与文案。",
            "topics": [{"title": "选题%d" % index} for index in range(1, 16)],
            "recommended": ["选题1", "选题2", "选题3"],
            "scripts": [{"platform": platforms[0] if platforms else "douyin", "content": "完整文案"}],
        }

    def reply(self, profile, message):
        self.calls.append(("reply", copy.deepcopy(profile), message))
        return "已结合你的独立画像回答。"

    def complete_profile(self, profile):
        self.calls.append(("complete_profile", copy.deepcopy(profile)))
        return "DeepSeek已完成并保存你的个人画像，可以继续生成选题或制作视频。"

    def interpret_intent(self, profile, flow, message):
        self.calls.append(("interpret_intent", copy.deepcopy(profile), copy.deepcopy(flow), message))
        compact = "".join(str(message or "").split())
        if any(word in compact for word in ("告诉我你能做什么", "先解释", "为什么选择")):
            return {"intent": "chat", "payload": {}}
        if flow.get("mode") == "template_collect" and compact:
            return {
                "intent": "start_video",
                "payload": {"topic": compact, "platforms": flow.get("platforms") or []},
            }
        if flow.get("mode") == "template_review" and compact:
            return {"intent": "regenerate_video", "payload": {}}
        if "偏好" in compact and any(word in compact for word in ("查看", "显示")):
            return {"intent": "view_preferences", "payload": {}}
        if "偏好" in compact and any(word in compact for word in ("清空", "删除", "重置")):
            return {"intent": "clear_preferences", "payload": {}}
        if "画像" in compact and any(word in compact for word in ("修改", "调整", "更新")):
            return {"intent": "modify_profile", "payload": {}}
        if "文案" in compact and any(word in compact for word in ("修改", "改成", "重写", "删掉")):
            return {"intent": "revise_copy", "payload": {}}
        if "选题" in compact or "写文案" in compact or "口播文案" in compact:
            return {"intent": "topic_plan", "payload": {}}
        if any(word in compact for word in ("标题改", "底部改", "重做视频", "重新生成")):
            return {"intent": "regenerate_video", "payload": {}}
        if "视频" in compact and any(word in compact for word in ("制作", "生成", "做")):
            platforms = [
                key for key, label in {
                    "douyin": "抖音", "xiaohongshu": "小红书",
                    "wechat_channels": "视频号",
                }.items() if label in compact
            ]
            return {
                "intent": "start_video",
                "payload": {
                    "topic": compact.split("：", 1)[1] if "：" in compact else "",
                    "platforms": platforms,
                },
            }
        if compact in {"确认方案", "采用当前方案"}:
            return {"intent": "confirm_plan", "payload": {}}
        return {"intent": "chat", "payload": {}}

    def compose_reply(self, profile, message, event, draft_reply):
        self.calls.append((
            "compose_reply", copy.deepcopy(profile), message,
            copy.deepcopy(event), draft_reply,
        ))
        return draft_reply


class FakeBridge:
    def __init__(self, clock=None):
        self.internal_token = "test-shared-token"
        self.clock = clock or time.time
        self.calls = []
        self.confirm_count = 0
        self.quote_count = 0
        self.quote_expirations = {}
        self.accepted_submissions = {}
        self.reconcile_count = 0
        self.confirm_advance_seconds = 0
        self.task_results = {}
        self.templates = [
            {"id": "native-bold", "name": "原生大字", "tags": ["醒目"]},
            {"id": "minimal-headline", "name": "极简标题", "tags": ["极简"]},
            {"id": "editorial-clean", "name": "稳健叙事", "tags": ["稳健"]},
        ]

    def health(self):
        return {
            "ok": True, "ready": True,
            "actions": [
                "matrix-template-capability", "matrix-template-templates",
                "matrix-template-generate", "matrix-template-reconcile",
            ],
        }

    def catalog(self, account_id):
        self.calls.append(("catalog", account_id))
        return {"actions": [{
            "action": "matrix-template-generate",
            "availability": {"status": "available"},
        }]}

    def action(self, account_id, action, tool_input, **options):
        self.calls.append(("action", action, copy.deepcopy(tool_input), dict(options)))
        if action == "matrix-template-templates":
            return {"templates": copy.deepcopy(self.templates), "cost": 5}
        if action == "task":
            if (
                isinstance(tool_input.get("job_id"), bool)
                or not isinstance(tool_input.get("job_id"), int)
            ):
                raise APIError(400, "job_id 必须是整数", "invalid_request")
            return copy.deepcopy(self.task_results.get(str(tool_input["job_id"]), {
                "status": "running",
            }))
        if action == "matrix-template-generate" and options.get("confirm"):
            token = str(options.get("quote_token") or "")
            if int(self.quote_expirations.get(token) or 0) <= int(self.clock()):
                raise APIError(409, "报价已过期，请重新报价", "quote_expired")
            self.confirm_count += 1
            if self.confirm_advance_seconds:
                self.clock.advance(self.confirm_advance_seconds)
            result = {"status": "running", "job_id": str(7000 + self.confirm_count), "points": 990}
            self.accepted_submissions[str(options.get("idempotency_key") or "")] = copy.deepcopy(result)
            return result
        if action == "matrix-template-generate":
            self.quote_count += 1
            token = "private-quote-%d" % self.quote_count
            expires_at = int(self.clock()) + 300
            self.quote_expirations[token] = expires_at
            return {
                "quote_token": token,
                "cost": 5, "points": 1000, "expires_in": 300,
                "expires_at": expires_at,
            }
        raise AssertionError(action)

    def reconcile(self, account_id, tool_input, idempotency_key):
        self.calls.append(("reconcile", copy.deepcopy(tool_input), idempotency_key))
        self.reconcile_count += 1
        result = self.accepted_submissions.get(str(idempotency_key or ""))
        if result is None:
            raise APIError(404, "未找到已受理的原提交", "idempotency_not_found")
        return {**copy.deepcopy(result), "reconciled": True}


class CreatorAgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = CreatorAgentStore(pathlib.Path(self.temp.name) / "creator.db")
        self.clock = MutableClock()
        self.bridge = FakeBridge(self.clock)
        self.profile_agent = FakeProfileAgent()
        state = initial_state()
        state.update({
            "current_module": 4, "question_index": 1, "phase": "ready",
            "completed_modules": [1, 2, 3, 4], "profile_ready": True,
            "selected_profiles": {str(index): {"title": "模块%d方案" % index} for index in range(1, 5)},
            "answers": {
                str(module): {
                    question["key"]: PROFILE_ANSWERS[question["key"]]
                    for question in MODULES[module]["questions"]
                }
                for module in range(1, 5)
            },
        })
        self.store.ensure_workspace(USER["username"], PROJECT_ID, "我的个人画像")
        self.store.update_workspace(
            USER["username"], PROJECT_ID, profile_state=state,
            profile={"modules": state["selected_profiles"], "answers": state["answers"]},
            flow={"mode": "idle"},
        )
        self.store.add_message(
            USER["username"], PROJECT_ID, "assistant", "独立画像已准备好。",
        )
        self.store.set_active_project(USER["username"], PROJECT_ID)
        self.service = CreatorAgentService(
            self.store, CreatorPlanner(provider=None, fallback=GuidedPlanner()),
            FakeAuth(), self.bridge, self.profile_agent, clock=self.clock,
        )
        self.headers = {}

    def tearDown(self):
        self.temp.cleanup()

    def bootstrap(self):
        return self.service.bootstrap(USER, self.headers)

    def message(self, text, intent="", payload=None, suffix="0001"):
        body = {"message": text, "request_id": "creator-turn-" + suffix}
        if intent:
            body["intent"] = intent
        if payload is not None:
            body["payload"] = payload
        return self.service.message(USER, self.headers, body)

    def test_bootstrap_creates_independent_profile_and_first_question(self):
        store = CreatorAgentStore(pathlib.Path(self.temp.name) / "fresh-creator.db")
        service = CreatorAgentService(
            store, CreatorPlanner(provider=None, fallback=GuidedPlanner()),
            FakeAuth(), self.bridge, self.profile_agent,
        )
        result = service.bootstrap(USER, self.headers)
        self.assertRegex(result["project"]["id"], r"^[0-9a-f]{12}$")
        self.assertFalse(result["project"]["progress"]["profile_complete"])
        self.assertIn("昵称", result["messages"][0]["content"])
        self.assertEqual(result["messages"][0]["public"]["kind"], "profile_question")
        self.assertTrue(any(call[0] == "ask_question" for call in self.profile_agent.calls))

    def test_health_requires_bridge_model_and_writable_database(self):
        health = self.service.health()
        self.assertTrue(health["ok"])
        self.assertTrue(health["ready"])
        self.assertTrue(all(health["checks"].values()))
        self.bridge.internal_token = ""
        health = self.service.health()
        self.assertTrue(health["ok"])
        self.assertFalse(health["ready"])
        self.assertFalse(health["checks"]["bridge_token"])

    def test_build_service_reads_shared_hq_internal_token(self):
        path = pathlib.Path(self.temp.name) / "build-service.db"
        service = build_service({
            "CREATOR_AGENT_DB": str(path),
            "CREATOR_AGENT_AUTH_URL": "http://127.0.0.1:8095",
            "CREATOR_AGENT_BASE_URL": "https://api.deepseek.com",
            "CREATOR_AGENT_API_KEY": "test-deepseek-key",
            "CREATOR_AGENT_MODEL": "deepseek-v4-flash",
            "HQ_INTERNAL_TOKEN": "production-shared-token",
            "CREATOR_AGENT_MODEL_PRICE_VERSION": "test-price-v2",
            "CREATOR_AGENT_MODEL_INPUT_PRICE_MICROUSD_PER_MILLION": "500000",
            "CREATOR_AGENT_MODEL_OUTPUT_PRICE_MICROUSD_PER_MILLION": "1400000",
            "CREATOR_AGENT_MODEL_INPUT_TOKEN_OVERHEAD": "9000",
        })
        self.assertEqual(service.bridge.internal_token, "production-shared-token")
        self.assertEqual(service.usage_guard.price_version, "test-price-v2")
        self.assertEqual(service.usage_guard.input_price_micro_usd_per_million, 500_000)
        self.assertEqual(service.usage_guard.output_price_micro_usd_per_million, 1_400_000)
        self.assertEqual(service.usage_guard.input_token_overhead, 9_000)
        self.assertTrue(service.profile_agent.configured)
        self.assertEqual(service.profile_agent.model, "deepseek-v4-flash")

    def test_build_service_does_not_inherit_shared_deepseek_key(self):
        service = build_service({
            "CREATOR_AGENT_DB": str(pathlib.Path(self.temp.name) / "isolated-key.db"),
            "CREATOR_AGENT_AUTH_URL": "http://127.0.0.1:8095",
            "CREATOR_AGENT_BASE_URL": "https://api.deepseek.com",
            "CREATOR_AGENT_MODEL": "deepseek-v4-flash",
            "DEEPSEEK_API_KEY": "shared-key-must-not-be-used",
            "HQ_INTERNAL_TOKEN": "production-shared-token",
        })
        self.assertEqual(service.profile_agent.api_key, "")
        self.assertIsNone(service.planner.provider)

    def test_each_project_has_one_continuous_conversation(self):
        first = self.bootstrap()
        second = self.bootstrap()
        self.assertEqual(first["project"]["id"], second["project"]["id"])
        self.assertNotIn("conversations", first)
        self.assertEqual(len(self.store.messages(USER["username"], PROJECT_ID)), 1)

    def test_pre_release_v1_message_table_is_archived_without_data_loss(self):
        path = pathlib.Path(self.temp.name) / "legacy.db"
        connection = sqlite3.connect(path)
        connection.execute(
            "CREATE TABLE creator_messages(id INTEGER PRIMARY KEY, conversation_id TEXT, content TEXT)"
        )
        connection.execute(
            "INSERT INTO creator_messages(conversation_id,content) VALUES('legacy','keep me')"
        )
        connection.commit()
        connection.close()
        migrated = CreatorAgentStore(path)
        with closing(migrated.db()) as current:
            columns = {row[1] for row in current.execute("PRAGMA table_info(creator_messages)")}
            archived = current.execute(
                "SELECT content FROM creator_messages_v1_archive"
            ).fetchone()[0]
        self.assertIn("project_id", columns)
        self.assertEqual(archived, "keep me")

    def test_incomplete_profile_uses_own_question_engine_without_paid_action(self):
        self.store.update_workspace(
            USER["username"], PROJECT_ID, profile_state=initial_state(),
            profile={}, flow={"mode": "profile_interview"},
        )
        result = self.message("我是企业AI顾问", suffix="1001")
        self.assertIn("当前职业", result["reply"])
        self.assertEqual(len([call for call in self.profile_agent.calls if call[0] == "capture"]), 1)
        paid = [call for call in self.bridge.calls if call[0] == "action" and call[1] == "matrix-template-generate"]
        self.assertEqual(paid, [])

    def test_profile_navigation_is_understood_and_answered_by_deepseek(self):
        self.store.update_workspace(
            USER["username"], PROJECT_ID, profile_state=initial_state(),
            profile={}, flow={"mode": "profile_interview"},
        )
        body = {
            "message": "跳过", "request_id": "profile-skip-0001",
            "project_id": PROJECT_ID,
        }
        first = self.service.message(USER, self.headers, body)
        state = first["workspace"]["profile_state"]
        self.assertEqual((state["question_index"], state["revision"]), (1, 2))
        self.assertEqual(first["message_public"]["field"], "career_identity")
        self.assertIn("DeepSeek", first["reply"])
        self.assertEqual(
            len([call for call in self.profile_agent.calls if call[0] == "capture"]),
            1,
        )
        self.assertIn("1:basic_context", state["skipped_questions"])
        self.assertEqual(self.service.message(USER, self.headers, body), first)
        self.assertEqual(
            len([call for call in self.profile_agent.calls if call[0] == "capture"]),
            1,
        )

        second = self.service.message(USER, self.headers, {
            "message": "下一个问题", "request_id": "profile-skip-0002",
            "project_id": PROJECT_ID,
        })
        state = second["workspace"]["profile_state"]
        self.assertEqual((state["question_index"], state["revision"]), (2, 3))
        self.assertEqual(second["message_public"]["field"], "career_history")
        self.assertIn("DeepSeek提问", second["reply"])
        self.assertEqual(
            len([call for call in self.profile_agent.calls if call[0] == "capture"]),
            2,
        )

    def test_skipping_last_question_uses_deepseek_then_enters_review(self):
        state = initial_state()
        state["question_index"] = len(MODULES[1]["questions"]) - 1
        state["answers"] = {
            "1": {
                key: PROFILE_ANSWERS[key]
                for key in (
                    "career_identity", "achievement", "proven_ability",
                    "target_audience", "audience_pain", "differentiation",
                )
            },
        }
        self.store.update_workspace(
            USER["username"], PROJECT_ID, profile_state=state,
            profile={}, flow={"mode": "profile_interview"},
        )
        result = self.service.message(USER, self.headers, {
            "message": "继续下一个问题",
            "request_id": "profile-skip-last-0001",
            "project_id": PROJECT_ID,
        })
        current = result["workspace"]["profile_state"]
        self.assertEqual(current["phase"], "review")
        self.assertEqual(current["revision"], 2)
        self.assertEqual(result["message_public"]["kind"], "profile_review")
        self.assertIn("1:existing_accounts", current["skipped_questions"])
        self.assertEqual(
            len([call for call in self.profile_agent.calls if call[0] == "capture"]),
            1,
        )
        self.assertEqual(
            len([call for call in self.profile_agent.calls if call[0] == "review"]),
            1,
        )

    def test_meta_answer_is_reasked_without_being_saved(self):
        state = initial_state()
        self.store.update_workspace(
            USER["username"], PROJECT_ID, profile_state=state,
            profile={}, flow={"mode": "profile_interview"},
        )
        result = self.message(
            "参考", "profile_answer", {"profile_revision": state["revision"]},
            "profile-quality-meta-0001",
        )
        current = result["workspace"]["profile_state"]
        self.assertEqual(current["question_index"], 0)
        self.assertEqual(current["revision"], 2)
        self.assertEqual(result["message_public"]["field"], "basic_context")
        self.assertEqual(current.get("answers") or {}, {})
        self.assertTrue(any(call[0] == "ask_question" for call in self.profile_agent.calls))

    def test_skipping_a_legacy_field_removes_its_stale_alias_value(self):
        state = initial_state()
        state["answers"] = {"1": {"identity": "旧昵称信息"}}
        self.store.update_workspace(
            USER["username"], PROJECT_ID, profile_state=state,
            profile={}, flow={"mode": "profile_interview"},
        )
        result = self.message(
            "跳过", "profile_answer", {"profile_revision": state["revision"]},
            "profile-skip-legacy-alias-0001",
        )
        answers = result["workspace"]["profile_state"]["answers"]["1"]
        self.assertNotIn("identity", answers)
        self.assertNotIn("basic_context", answers)

    def test_story_module_cannot_finish_with_navigation_placeholders(self):
        state = initial_state()
        state.update({
            "current_module": 4,
            "question_index": len(MODULES[4]["questions"]) - 1,
            "answers": {"4": {
                "comeback_story": "参考",
                "success_story": "用户选择回顾或修改模块4：故事资产。",
                "dramatic_story": "用户选择回顾逆袭故事。",
            }},
            "skipped_questions": ["4:pitfall_story"],
        })
        self.store.update_workspace(
            USER["username"], PROJECT_ID, profile_state=state,
            profile={}, flow={"mode": "profile_interview"},
        )
        result = self.message(
            "跳过", "profile_answer", {"profile_revision": state["revision"]},
            "profile-quality-story-0001",
        )
        current = result["workspace"]["profile_state"]
        self.assertEqual(current["phase"], "collecting")
        self.assertEqual(current["question_index"], 0)
        self.assertEqual(result["message_public"]["field"], "comeback_story")
        self.assertNotIn("4", current.get("module_reviews") or {})

    def test_legacy_review_choice_cannot_bypass_quality_gate(self):
        state = initial_state()
        state.update({
            "current_module": 4,
            "phase": "review",
            "answers": {"4": {"comeback_story": "参考"}},
            "module_reviews": {
                "4": self.profile_agent.build_module_review(state, 4),
            },
        })
        state["active_question"] = self.profile_agent._question(current_question(state))
        self.store.update_workspace(
            USER["username"], PROJECT_ID, profile_state=state,
            profile={}, flow={"mode": "profile_interview"},
        )
        result = self.message(
            "选择第一个方案", "profile_choice",
            {"choice_index": 0, "profile_revision": state["revision"]},
            "profile-review-quality-gate-0001",
        )
        current = result["workspace"]["profile_state"]
        self.assertEqual(current["phase"], "collecting")
        self.assertEqual(result["message_public"]["field"], "comeback_story")
        self.assertFalse(current["profile_ready"])

    def test_ready_legacy_profile_is_flagged_and_can_enter_guided_repair(self):
        state = initial_state()
        state.update({
            "current_module": 4,
            "phase": "ready",
            "completed_modules": [1, 2, 3, 4],
            "profile_ready": True,
            "selected_profiles": {
                str(index): {"title": "旧模块%d方案" % index}
                for index in range(1, 5)
            },
            "module_reviews": {
                str(index): {"module": index, "options": []}
                for index in range(1, 5)
            },
            "answers": {
                "1": {
                    "identity": "空黎，AI。",
                    "career_identity": "我目前是AI",
                    "achievement": PROFILE_ANSWERS["achievement"],
                    "proven_ability": PROFILE_ANSWERS["proven_ability"],
                    "target_audience": PROFILE_ANSWERS["target_audience"],
                    "audience_pain": PROFILE_ANSWERS["audience_pain"],
                    "differentiation": "有成功案例",
                },
                "2": {
                    "communication_style": PROFILE_ANSWERS["communication_style"],
                },
                "3": {
                    "memorable_statement": "有，是一句话的雏形",
                    "self_intro": PROFILE_ANSWERS["self_intro"],
                    "trust_reason": PROFILE_ANSWERS["trust_reason"],
                    "ip_goal": PROFILE_ANSWERS["ip_goal"],
                    "products_services": PROFILE_ANSWERS["products_services"],
                    "short_term_goal": PROFILE_ANSWERS["short_term_goal"],
                    "long_term_goal": PROFILE_ANSWERS["long_term_goal"],
                },
                "4": {
                    "comeback_story": "参考",
                    "success_story": "用户选择回顾或修改模块4：故事资产。",
                },
            },
        })
        self.store.update_workspace(
            USER["username"], PROJECT_ID, profile_state=state,
            profile={"answers": state["answers"], "modules": state["selected_profiles"]},
            flow={"mode": "idle"},
        )
        snapshot = self.bootstrap()
        self.assertEqual(snapshot["project"]["profile_quality"]["status"], "needs_review")
        self.assertGreater(snapshot["project"]["profile_quality"]["issue_count"], 0)
        self.assertEqual(snapshot["project"]["foundation_pdf_status"], "blocked")
        self.assertEqual(snapshot["project"]["foundation_pdf_url"], "")
        self.assertEqual(snapshot["project"]["foundation_pdf_retry_url"], "")
        intents = {item["intent"] for item in snapshot["quick_actions"]}
        self.assertIn("repair_profile", intents)
        self.assertNotIn("start_video", intents)

        blocked = self.message(
            "制作视频", "start_video",
            {"topic": "测试", "platforms": ["douyin"]},
            "profile-quality-block-video-0001",
        )
        self.assertEqual(blocked["message_public"]["kind"], "profile_quality_required")
        self.assertEqual(self.store.batches(USER["username"], PROJECT_ID), [])
        with self.assertRaises(APIError) as pdf_blocked:
            self.service.background_pdf(USER, PROJECT_ID)
        self.assertEqual(pdf_blocked.exception.code, "profile_quality_required")

        result = self.message(
            "完善画像", "repair_profile", {}, "profile-guided-repair-0001",
        )
        current = result["workspace"]["profile_state"]
        self.assertFalse(current["profile_ready"])
        self.assertEqual((current["current_module"], current["question_index"]), (1, 1))
        self.assertEqual(result["message_public"]["field"], "career_identity")
        self.assertEqual(current["completed_modules"], [])
        self.assertEqual(current["selected_profiles"], {})

    def test_repair_transition_skips_valid_answers_and_opens_next_review(self):
        state = initial_state()
        state.update({
            "current_module": 1,
            "phase": "review",
            "module_reviews": {
                "1": self.profile_agent.build_module_review(state, 1),
            },
            "answers": {
                "1": {
                    question["key"]: PROFILE_ANSWERS[question["key"]]
                    for question in MODULES[1]["questions"]
                },
                "2": {
                    question["key"]: PROFILE_ANSWERS[question["key"]]
                    for question in MODULES[2]["questions"]
                },
            },
        })
        state["active_question"] = self.profile_agent._question(current_question(state))
        self.store.update_workspace(
            USER["username"], PROJECT_ID, profile_state=state,
            profile={}, flow={"mode": "profile_interview"},
        )
        before_questions = len([
            call for call in self.profile_agent.calls if call[0] == "ask_question"
        ])
        result = self.message(
            "选择第一个方案", "profile_choice",
            {"choice_index": 0, "profile_revision": state["revision"]},
            "profile-repair-skip-valid-0001",
        )
        self.assertEqual(result["message_public"]["kind"], "profile_review")
        self.assertEqual(result["message_public"]["module"], 2)
        self.assertEqual(result["workspace"]["profile_state"]["phase"], "review")
        self.assertEqual(len([
            call for call in self.profile_agent.calls if call[0] == "ask_question"
        ]), before_questions)

    def test_free_and_explicit_nonprofile_turns_use_deepseek(self):
        before = len(self.profile_agent.calls)
        free = self.message("查看我的偏好", suffix="deepseek-all-free-0001")
        calls = self.profile_agent.calls[before:]
        self.assertEqual(free["message_public"]["kind"], "preferences")
        self.assertTrue(any(call[0] == "interpret_intent" for call in calls))
        self.assertTrue(any(call[0] == "compose_reply" for call in calls))

        before = len(self.profile_agent.calls)
        explicit = self.message(
            "查看偏好", "view_preferences", suffix="deepseek-all-explicit-0001",
        )
        calls = self.profile_agent.calls[before:]
        self.assertEqual(explicit["message_public"]["kind"], "preferences")
        self.assertFalse(any(call[0] == "interpret_intent" for call in calls))
        self.assertTrue(any(call[0] == "compose_reply" for call in calls))

    def test_chat_during_template_collection_does_not_create_batch(self):
        self.store.update_workspace(
            USER["username"], PROJECT_ID,
            platforms=["douyin"],
            flow={"mode": "template_collect", "platforms": ["douyin"], "topic": ""},
        )
        result = self.message(
            "我还没想好，先告诉我你能做什么",
            suffix="chat-template-collect-0001",
        )
        self.assertEqual(result["message_public"]["kind"], "assistant_reply")
        self.assertEqual(self.store.batches(USER["username"], PROJECT_ID), [])
        workspace = self.store.workspace(USER["username"], PROJECT_ID)
        self.assertEqual(workspace["flow"]["mode"], "template_collect")

    def test_chat_during_template_review_does_not_revise_batch(self):
        draft = self._draft_batch()
        before = self.store.batch(USER["username"], draft["id"], include_private=True)
        result = self.message(
            "为什么选择这个模板？先解释，不要修改",
            suffix="chat-template-review-0001",
        )
        after = self.store.batch(USER["username"], draft["id"], include_private=True)
        self.assertEqual(result["message_public"]["kind"], "assistant_reply")
        self.assertEqual(after["revision"], before["revision"])
        self.assertEqual(after["plan_hash"], before["plan_hash"])

    def test_profile_answer_fails_closed_without_deepseek_configuration(self):
        self.store.update_workspace(
            USER["username"], PROJECT_ID, profile_state=initial_state(),
            profile={}, flow={"mode": "profile_interview"},
        )
        self.service.profile_agent = DeepSeekProfileAgent("")
        with self.assertRaises(APIError) as raised:
            self.message("我是企业AI顾问", suffix="no-model-0001")
        self.assertEqual(raised.exception.code, "creator_model_unavailable")
        self.assertFalse(any(
            message["role"] == "user" and "企业AI顾问" in message["content"]
            for message in self.store.messages(USER["username"], PROJECT_ID)
        ))
        self.service.profile_agent = self.profile_agent
        retried = self.message("我是企业AI顾问", suffix="no-model-0001")
        self.assertEqual(retried["message_public"]["kind"], "profile_question")

    def test_full_independent_profile_journey_persists_four_modules(self):
        self.store.update_workspace(
            USER["username"], PROJECT_ID, profile_state=initial_state(),
            profile={}, deliverables={}, flow={"mode": "profile_interview"},
        )
        turn = 0
        while True:
            workspace = self.store.workspace(USER["username"], PROJECT_ID)
            state = workspace["profile_state"]
            if state.get("profile_ready"):
                break
            turn += 1
            self.assertLess(turn, 80, "profile journey did not converge")
            if state.get("phase") == "review":
                self.message(
                    "选择第一个方案", "profile_choice", {
                        "choice_index": 0, "profile_revision": state["revision"],
                    },
                    "profile-flow-%04d" % turn,
                )
            else:
                key = current_question(state)["key"]
                self.message(
                    PROFILE_ANSWERS[key],
                    "profile_answer", {"profile_revision": state["revision"]},
                    "profile-flow-%04d" % turn,
                )
        workspace = self.store.workspace(USER["username"], PROJECT_ID)
        self.assertTrue(workspace["profile_state"]["profile_ready"])
        self.assertEqual(workspace["profile_state"]["completed_modules"], [1, 2, 3, 4])
        self.assertEqual(set(workspace["profile"]["modules"]), {"1", "2", "3", "4"})
        self.assertIn("personal_profile", workspace["deliverables"])
        self.assertIn("background_profile_pdf", workspace["deliverables"])
        self.assertEqual(
            workspace["deliverables"]["background_profile_pdf"]["profile_schema_version"],
            2,
        )
        pdf_path = self.service._profile_pdf_path(USER["username"], PROJECT_ID)
        self.assertTrue(pdf_path.is_file())
        self.assertGreater(pdf_path.stat().st_size, 1024)
        self.assertEqual(pdf_path.read_bytes()[:5], b"%PDF-")
        snapshot = self.bootstrap()
        self.assertEqual(
            snapshot["project"]["foundation_pdf_url"],
            "/api/creator-agent/projects/%s/background.pdf" % PROJECT_ID,
        )
        self.assertEqual(snapshot["project"]["foundation_pdf_status"], "ready")
        self.assertEqual(snapshot["project"]["foundation_pdf_retry_url"], "")
        self.assertNotIn("background_profile_pdf", snapshot["project"]["deliverables"])
        self.assertFalse(any(
            message.get("public", {}).get("source") == "ip12"
            for message in self.store.messages(USER["username"], PROJECT_ID)
        ))

    def test_profile_completion_survives_background_pdf_failure(self):
        state = initial_state()
        state.update({
            "current_module": 4,
            "question_index": len(MODULES[4]["questions"]),
            "phase": "review",
            "completed_modules": [1, 2, 3],
            "answers": {
                str(module): {
                    question["key"]: PROFILE_ANSWERS[question["key"]]
                    for question in MODULES[module]["questions"]
                }
                for module in range(1, 5)
            },
            "selected_profiles": {
                str(index): {"title": "模块%d方案" % index}
                for index in range(1, 4)
            },
            "module_reviews": {
                "4": self.profile_agent.build_module_review(state, 4),
            },
        })
        self.store.update_workspace(
            USER["username"], PROJECT_ID, profile_state=state,
            profile={}, deliverables={}, flow={"mode": "profile_interview"},
        )
        with mock.patch(
            "creator_agent.service.render_profile_pdf",
            side_effect=ProfilePDFError("forced PDF failure"),
        ):
            result = self.message(
                "选择第一个方案", "profile_choice",
                {"choice_index": 0, "profile_revision": state["revision"]},
                "profile-pdf-failure-0001",
            )
        workspace = self.store.workspace(USER["username"], PROJECT_ID)
        self.assertEqual(result["message_public"]["kind"], "profile_completed")
        self.assertTrue(workspace["profile_state"]["profile_ready"])
        self.assertEqual(
            workspace["deliverables"]["background_profile_pdf"]["status"],
            "failed",
        )
        self.assertEqual(
            workspace["deliverables"]["background_profile_pdf"]["error_code"],
            "profile_pdf_failed",
        )
        self.assertEqual(result["project"]["foundation_pdf_status"], "failed")
        self.assertEqual(result["project"]["foundation_pdf_url"], "")
        self.assertEqual(
            result["project"]["foundation_pdf_retry_url"],
            "/api/creator-agent/projects/%s/background.pdf" % PROJECT_ID,
        )
        self.assertEqual(
            result["project"]["foundation_pdf_error_code"],
            "profile_pdf_failed",
        )
        self.assertNotIn(
            "background_profile_pdf", result["project"]["deliverables"],
        )

    def test_stale_ready_pdf_is_not_exposed_for_new_profile_revision(self):
        workspace = self.store.workspace(USER["username"], PROJECT_ID)
        revision = int(workspace["profile_state"]["revision"])
        self.store.update_workspace(
            USER["username"], PROJECT_ID,
            deliverables={
                "background_profile_pdf": {
                    "title": "IP人设定位背景档案",
                    "url": "/api/creator-agent/projects/%s/background.pdf" % PROJECT_ID,
                    "status": "ready",
                    "profile_revision": revision - 1,
                },
            },
        )
        project = self.bootstrap()["project"]
        self.assertEqual(project["foundation_pdf_status"], "pending")
        self.assertEqual(project["foundation_pdf_url"], "")
        path = self.service.background_pdf(USER, PROJECT_ID)
        self.assertTrue(path.is_file())
        refreshed = self.store.workspace(USER["username"], PROJECT_ID)
        self.assertEqual(
            refreshed["deliverables"]["background_profile_pdf"]["profile_schema_version"],
            2,
        )
        self.assertEqual(
            project["foundation_pdf_retry_url"],
            "/api/creator-agent/projects/%s/background.pdf" % PROJECT_ID,
        )

    def test_old_pdf_schema_requires_regeneration(self):
        workspace = self.store.workspace(USER["username"], PROJECT_ID)
        revision = int(workspace["profile_state"]["revision"])
        self.store.update_workspace(
            USER["username"], PROJECT_ID,
            deliverables={
                "background_profile_pdf": {
                    "title": "IP人设定位背景档案",
                    "url": "/api/creator-agent/projects/%s/background.pdf" % PROJECT_ID,
                    "status": "ready",
                    "profile_revision": revision,
                    "profile_schema_version": 1,
                },
            },
        )
        project = self.bootstrap()["project"]
        self.assertEqual(project["foundation_pdf_status"], "pending")
        self.assertEqual(project["foundation_pdf_url"], "")

    def test_stale_profile_action_cannot_overwrite_current_step(self):
        self.store.update_workspace(
            USER["username"], PROJECT_ID, profile_state=initial_state(),
            profile={}, flow={"mode": "profile_interview"},
        )
        with self.assertRaises(APIError) as raised:
            self.message(
                "专业可靠", "profile_answer",
                {"answer": "专业可靠", "profile_revision": 0},
                "stale-profile-0001",
            )
        self.assertEqual(raised.exception.code, "profile_state_conflict")
        self.assertFalse([call for call in self.profile_agent.calls if call[0] == "capture"])

    def test_profile_state_compare_and_swap_rejects_second_writer(self):
        state = initial_state()
        self.store.update_workspace(
            USER["username"], PROJECT_ID, profile_state=state,
            profile={}, flow={"mode": "profile_interview"},
        )
        first = copy.deepcopy(state)
        first.update({"revision": 2, "question_index": 1})
        self.store.update_profile_state(
            USER["username"], PROJECT_ID, first, 1,
        )
        second = copy.deepcopy(state)
        second.update({"revision": 2, "question_index": 1})
        with self.assertRaises(StateConflict):
            self.store.update_profile_state(
                USER["username"], PROJECT_ID, second, 1,
            )

    def test_profile_turn_crash_boundaries_replay_once(self):
        for index, boundary in enumerate((
            "before_state", "after_state", "after_assistant", "before_commit", "after_commit",
        ), 1):
            with self.subTest(boundary=boundary):
                state = initial_state()
                self.store.update_workspace(
                    USER["username"], PROJECT_ID, profile_state=state,
                    profile={}, flow={"mode": "profile_interview"},
                )
                request_id = "profile-crash-%02d" % index
                body = {
                    "message": "我是企业AI顾问%d" % index,
                    "request_id": request_id, "project_id": PROJECT_ID,
                }
                fired = {"value": False}

                def transaction_fault(stage):
                    if boundary == stage and not fired["value"]:
                        fired["value"] = True
                        raise SystemExit(stage)

                def after_commit_fault():
                    if boundary == "after_commit" and not fired["value"]:
                        fired["value"] = True
                        raise SystemExit("after_commit")

                def before_commit_fault():
                    if boundary == "before_state" and not fired["value"]:
                        fired["value"] = True
                        raise SystemExit("before_state")

                self.service._before_profile_commit_hook = before_commit_fault
                self.service._profile_turn_fault_hook = transaction_fault
                self.service._after_profile_commit_hook = after_commit_fault
                calls_before = len([
                    call for call in self.profile_agent.calls if call[0] == "capture"
                ])
                with self.assertRaises(SystemExit):
                    self.service.message(USER, {}, body)
                self.service._before_profile_commit_hook = None
                self.service._profile_turn_fault_hook = None
                self.service._after_profile_commit_hook = None
                replay = self.service.message(USER, {}, body)
                current = self.store.workspace(USER["username"], PROJECT_ID)["profile_state"]
                self.assertEqual((current["revision"], current["question_index"]), (2, 1))
                self.assertIn("当前职业", replay["reply"])
                with closing(self.store.db()) as connection:
                    user_row = connection.execute(
                        "SELECT id,public_json FROM creator_messages "
                        "WHERE username=? AND project_id=? AND request_id=?",
                        (USER["username"], PROJECT_ID, request_id),
                    ).fetchone()
                    user_public = json.loads(user_row["public_json"])
                    assistants = connection.execute(
                        "SELECT COUNT(*) FROM creator_messages WHERE username=? "
                        "AND project_id=? AND source_key=?",
                        (USER["username"], PROJECT_ID,
                         "profile-turn:%d" % int(user_row["id"])),
                    ).fetchone()[0]
                self.assertIn("turn", user_public)
                self.assertIn("response", user_public)
                self.assertEqual(assistants, 1)
                calls_after = len([
                    call for call in self.profile_agent.calls if call[0] == "capture"
                ])
                self.assertEqual(
                    calls_after - calls_before,
                    1 if boundary == "after_commit" else 2,
                )

    def test_model_rate_limit_rejects_before_second_deepseek_call(self):
        self.store.update_workspace(
            USER["username"], PROJECT_ID, profile_state=initial_state(),
            profile={}, flow={"mode": "profile_interview"},
        )
        self.service.usage_guard = ModelUsageGuard(
            self.store.db, user_window_requests=2, ip_window_requests=10,
            user_concurrency=1, global_concurrency=2, clock=self.clock,
        )
        headers = {"X-Forwarded-For": "1.2.3.4"}
        first = {
            "message": "我是企业AI顾问", "request_id": "model-limit-0001",
            "project_id": PROJECT_ID,
        }
        self.service.message(USER, headers, first)
        with self.assertRaises(APIError) as raised:
            self.service.message(USER, headers, {
                "message": "我经历过一次创业转型", "request_id": "model-limit-0002",
                "project_id": PROJECT_ID,
            })
        self.assertEqual(raised.exception.status, 429)
        self.assertEqual(raised.exception.code, "model_user_rate_limited")
        self.assertEqual(len([call for call in self.profile_agent.calls if call[0] == "capture"]), 1)

    def test_client_ip_ignores_spoofed_forwarded_prefix(self):
        self.assertEqual(
            self.service._client_ip({
                "X-Real-IP": "203.0.113.9",
                "X-Forwarded-For": "198.51.100.77, 203.0.113.9",
            }),
            "203.0.113.9",
        )
        self.assertEqual(
            self.service._client_ip({
                "X-Forwarded-For": "198.51.100.77, 203.0.113.10",
            }),
            "203.0.113.10",
        )

    def test_model_cost_budget_rejects_before_deepseek_call(self):
        self.store.update_workspace(
            USER["username"], PROJECT_ID, profile_state=initial_state(),
            profile={}, flow={"mode": "profile_interview"},
        )
        self.service.usage_guard = ModelUsageGuard(
            self.store.db, user_daily_cost_micro_usd=1,
            global_daily_cost_micro_usd=100_000, clock=self.clock,
        )
        with self.assertRaises(APIError) as raised:
            self.service.message(USER, {"X-Forwarded-For": "1.2.3.4"}, {
                "message": "我是企业AI顾问", "request_id": "model-budget-0001",
                "project_id": PROJECT_ID,
            })
        self.assertEqual(raised.exception.status, 429)
        self.assertEqual(raised.exception.code, "model_user_daily_budget")
        self.assertEqual(
            len([call for call in self.profile_agent.calls if call[0] == "capture"]),
            0,
        )

    def test_video_plan_is_per_platform_and_does_not_quote_automatically(self):
        result = self.message(
            "制作视频", "start_video",
            {"topic": "企业做 Agent 前先梳理流程", "platforms": ["douyin", "xiaohongshu"]},
            "2001",
        )
        batch = result["latest_batch"]
        self.assertEqual(batch["status"], "draft")
        self.assertEqual({item["platform"] for item in batch["plans"]}, {"douyin", "xiaohongshu"})
        public_json = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("source_message_id", public_json)
        self.assertNotIn("last_mutation_message_id", public_json)
        self.assertEqual(len(batch["jobs"]), 2)
        quotes = [call for call in self.bridge.calls if call[0] == "action" and call[1] == "matrix-template-generate"]
        self.assertEqual(quotes, [])

    def test_video_start_shows_adjustable_default_platforms(self):
        self.bootstrap()
        self.store.update_workspace(
            USER["username"], PROJECT_ID, platforms=["douyin", "wechat_channels"],
        )
        result = self.message("开始制作视频", "start_video", {}, "platform-0001")
        self.assertEqual(result["message_public"]["kind"], "platform_picker")
        self.assertEqual(result["message_public"]["selected"], ["douyin", "wechat_channels"])
        self.assertEqual(result["workspace"]["flow"]["mode"], "template_platforms")

    def test_platform_adjustment_before_quote_replaces_draft_without_version_bump(self):
        first = self.message(
            "制作视频", "start_video",
            {"topic": "企业内容获客", "platforms": ["douyin", "xiaohongshu"]},
            "platform-1001",
        )["latest_batch"]
        picker = self.message(
            "调整平台", "adjust_video_platforms", {
                "batch_id": first["id"], "expected_revision": first["revision"],
            },
            "platform-1002",
        )
        self.assertEqual(picker["message_public"]["selected"], ["douyin", "xiaohongshu"])
        changed = self.message(
            "已选择：小红书", "set_platforms", {"platforms": ["xiaohongshu"]},
            "platform-1003",
        )["latest_batch"]
        self.assertNotEqual(changed["id"], first["id"])
        self.assertEqual([item["platform"] for item in changed["plans"]], ["xiaohongshu"])
        self.assertEqual(changed["jobs"][0]["version"], 1)

    def test_pre_generation_copy_edit_stays_free_and_invalidates_quote(self):
        draft = self.message(
            "制作视频", "start_video",
            {"topic": "企业内容获客", "platforms": ["douyin", "xiaohongshu"]},
            "edit-0001",
        )["latest_batch"]
        edited = self.message(
            "小红书标题改成：企业获客先做对这三步",
            suffix="edit-0002",
        )["latest_batch"]
        self.assertEqual(edited["id"], draft["id"])
        self.assertEqual(edited["status"], "ready")
        self.service.quote_batch(USER, draft["id"], edited["revision"])
        edited_again = self.message(
            "小红书标题改成：企业获客先梳理流程",
            suffix="edit-0003",
        )["latest_batch"]
        self.assertEqual(edited_again["id"], draft["id"])
        self.assertEqual(edited_again["status"], "ready")
        self.assertEqual(edited_again["quote"], {})
        private = self.store.batch(USER["username"], draft["id"], include_private=True)
        self.assertTrue(all(not job["quote_token"] for job in private["jobs"]))

    def test_free_text_can_choose_platforms_and_topic(self):
        result = self.message(
            "制作抖音和视频号视频：企业先梳理流程再做 Agent",
            suffix="2002",
        )
        batch = result["latest_batch"]
        self.assertEqual({item["platform"] for item in batch["plans"]}, {"douyin", "wechat_channels"})
        self.assertIn("企业先梳理流程", batch["topic"])

    def test_topic_plan_requires_platforms_then_uses_independent_profile_agent(self):
        first = self.message("生成选题计划", "topic_plan", {}, "topic-0001")
        self.assertEqual(first["message_public"]["kind"], "platform_picker")
        self.assertFalse([call for call in self.profile_agent.calls if call[0] == "topic_plan"])
        second = self.message(
            "已选择：抖音、小红书", "set_platforms",
            {"platforms": ["douyin", "xiaohongshu"]}, "topic-0002",
        )
        self.assertEqual(second["workspace"]["flow"]["mode"], "idle")
        self.assertEqual(second["message_public"]["kind"], "topic_plan")
        self.assertEqual(len([call for call in self.profile_agent.calls if call[0] == "topic_plan"]), 1)
        paid = [call for call in self.bridge.calls if call[0] == "action" and call[1] == "matrix-template-generate"]
        self.assertEqual(paid, [])

    def test_copy_revision_uses_independent_agent_without_video_quote(self):
        result = self.message(
            "修改第一篇文案的开头，让它更直接",
            suffix="copy-revise-0001",
        )
        self.assertEqual(result["message_public"]["kind"], "topic_plan")
        self.assertEqual(len([call for call in self.profile_agent.calls if call[0] == "topic_plan"]), 1)
        paid = [call for call in self.bridge.calls if call[0] == "action" and call[1] == "matrix-template-generate"]
        self.assertEqual(paid, [])

    def test_replayed_unfinished_turn_does_not_repeat_side_effects(self):
        self.bootstrap()
        self.store.add_message(
            USER["username"], PROJECT_ID, "user", "制作视频",
            request_id="creator-turn-stuck-0001",
            request_hash=self.service._message_request_hash(
                PROJECT_ID, "制作视频", "start_video",
                {"topic": "测试", "platforms": ["douyin"]},
            ),
        )
        result = self.service.message(USER, self.headers, {
            "message": "制作视频", "request_id": "creator-turn-stuck-0001",
            "intent": "start_video", "payload": {"topic": "测试", "platforms": ["douyin"]},
        })
        self.assertEqual(result["message_public"]["kind"], "video_plan")
        self.assertEqual(len(self.store.batches(USER["username"], PROJECT_ID)), 1)

    def test_nonprofile_turn_crash_boundaries_replay_once(self):
        for boundary in ("after_assistant", "before_commit", "after_commit"):
            with self.subTest(boundary=boundary):
                request_id = "generic-crash-%s" % boundary
                body = {
                    "message": "查看偏好", "request_id": request_id,
                    "intent": "view_preferences", "project_id": PROJECT_ID,
                }
                fired = {"value": False}

                def transaction_fault(stage):
                    if boundary == stage and not fired["value"]:
                        fired["value"] = True
                        raise SystemExit(stage)

                def after_commit_fault():
                    if boundary == "after_commit" and not fired["value"]:
                        fired["value"] = True
                        raise SystemExit("after_commit")

                self.service._message_turn_fault_hook = transaction_fault
                self.service._after_message_commit_hook = after_commit_fault
                with self.assertRaises(SystemExit):
                    self.service.message(USER, self.headers, body)
                self.service._message_turn_fault_hook = None
                self.service._after_message_commit_hook = None
                replay = self.service.message(USER, self.headers, body)
                self.assertIn("偏好", replay["reply"])
                with closing(self.store.db()) as connection:
                    user_row = connection.execute(
                        "SELECT id,public_json FROM creator_messages "
                        "WHERE username=? AND project_id=? AND request_id=?",
                        (USER["username"], PROJECT_ID, request_id),
                    ).fetchone()
                    public = json.loads(user_row["public_json"])
                    assistants = connection.execute(
                        "SELECT COUNT(*) FROM creator_messages "
                        "WHERE username=? AND project_id=? AND source_key=?",
                        (USER["username"], PROJECT_ID,
                         "message-turn:%d" % int(user_row["id"])),
                    ).fetchone()[0]
                self.assertIn("turn", public)
                self.assertIn("response", public)
                self.assertEqual(assistants, 1)

    def test_video_side_effect_crash_recovers_one_batch(self):
        body = {
            "message": "制作视频", "request_id": "video-side-effect-crash-0001",
            "intent": "start_video", "project_id": PROJECT_ID,
            "payload": {"topic": "企业获客", "platforms": ["douyin"]},
        }
        fired = {"value": False}

        def crash_once(stage):
            if stage == "after_batch_create" and not fired["value"]:
                fired["value"] = True
                raise SystemExit(stage)

        self.service._message_side_effect_fault_hook = crash_once
        with self.assertRaises(SystemExit):
            self.service.message(USER, self.headers, body)
        self.service._message_side_effect_fault_hook = None
        batches = self.store.batches(USER["username"], PROJECT_ID)
        self.assertEqual(len(batches), 1)
        replay = self.service.message(USER, self.headers, body)
        self.assertEqual(replay["latest_batch"]["id"], batches[0]["id"])
        self.assertEqual(len(self.store.batches(USER["username"], PROJECT_ID)), 1)
        self.assertEqual(replay["workspace"]["flow"]["mode"], "template_review")

    def test_topic_side_effect_crash_reuses_saved_result(self):
        body = {
            "message": "生成抖音选题", "request_id": "topic-side-effect-crash-0001",
            "intent": "topic_plan", "project_id": PROJECT_ID,
            "payload": {"platforms": ["douyin"]},
        }
        self.service._before_message_commit_hook = lambda: (_ for _ in ()).throw(
            SystemExit("before_message_commit")
        )
        with self.assertRaises(SystemExit):
            self.service.message(USER, self.headers, body)
        self.service._before_message_commit_hook = None
        calls = [call for call in self.profile_agent.calls if call[0] == "topic_plan"]
        self.assertEqual(len(calls), 1)
        replay = self.service.message(USER, self.headers, body)
        self.assertEqual(replay["message_public"]["kind"], "topic_plan")
        calls = [call for call in self.profile_agent.calls if call[0] == "topic_plan"]
        self.assertEqual(len(calls), 1)
        deliverables = self.store.workspace(USER["username"], PROJECT_ID)["deliverables"]
        self.assertEqual(
            len([key for key in deliverables if key.startswith("topic_plan_request_")]),
            1,
        )

    def test_plan_revision_side_effect_crash_advances_once(self):
        draft = self._draft_batch()
        body = {
            "message": "标题改成更直接", "request_id": "revision-side-effect-crash-0001",
            "project_id": PROJECT_ID,
        }
        self.service._message_side_effect_fault_hook = lambda stage: (
            (_ for _ in ()).throw(SystemExit(stage))
            if stage == "after_batch_mutation" else None
        )
        with self.assertRaises(SystemExit):
            self.service.message(USER, self.headers, body)
        self.service._message_side_effect_fault_hook = None
        changed = self.store.batch(USER["username"], draft["id"], include_private=True)
        self.assertEqual(changed["revision"], draft["revision"] + 1)
        replay = self.service.message(USER, self.headers, body)
        self.assertEqual(replay["latest_batch"]["revision"], changed["revision"])
        current = self.store.batch(USER["username"], draft["id"], include_private=True)
        self.assertEqual(current["revision"], changed["revision"])

    def test_quote_side_effect_crash_reuses_same_quote(self):
        draft = self._draft_batch()
        body = {
            "message": "确认方案", "request_id": "quote-side-effect-crash-0001",
            "intent": "confirm_plan", "project_id": PROJECT_ID,
            "payload": {
                "batch_id": draft["id"],
                "expected_revision": draft["revision"],
            },
        }
        self.service._before_message_commit_hook = lambda: (_ for _ in ()).throw(
            SystemExit("before_message_commit")
        )
        with self.assertRaises(SystemExit):
            self.service.message(USER, self.headers, body)
        self.service._before_message_commit_hook = None
        quote_calls = self.bridge.quote_count
        quoted = self.store.batch(USER["username"], draft["id"])
        self.assertEqual(quoted["status"], "quoted")
        replay = self.service.message(USER, self.headers, body)
        self.assertEqual(replay["message_public"]["kind"], "video_quote")
        self.assertEqual(self.bridge.quote_count, quote_calls)

    def test_confirmation_side_effect_crash_does_not_resubmit(self):
        draft = self._draft_batch()
        quoted = self.service.quote_batch(USER, draft["id"], draft["revision"])
        body = {
            "message": "确认扣点并开始生成",
            "request_id": "confirm-side-effect-crash-0001",
            "intent": "confirm_payment", "project_id": PROJECT_ID,
            "payload": {
                "batch_id": draft["id"],
                "confirmation_id": "confirm-side-effect-crash",
                "expected_revision": quoted["revision"],
                "expected_quote_expires_at": quoted["quote_expires_at"],
            },
        }
        self.service._before_message_commit_hook = lambda: (_ for _ in ()).throw(
            SystemExit("before_message_commit")
        )
        with self.assertRaises(SystemExit):
            self.service.message(USER, self.headers, body)
        self.service._before_message_commit_hook = None
        confirm_calls = self.bridge.confirm_count
        replay = self.service.message(USER, self.headers, body)
        self.assertEqual(replay["message_public"]["kind"], "video_submitted")
        self.assertEqual(self.bridge.confirm_count, confirm_calls)

    def test_same_request_id_replays_and_changed_payload_conflicts(self):
        body = {
            "message": "制作视频", "request_id": "creator-turn-hash-0001",
            "intent": "start_video",
            "payload": {"topic": "企业获客", "platforms": ["douyin"]},
        }
        first = self.service.message(USER, self.headers, body)
        second = self.service.message(USER, self.headers, copy.deepcopy(body))
        self.assertEqual(first, second)
        for item in second["messages"]:
            if item["role"] == "user":
                self.assertNotIn("turn", item["public"])
                self.assertNotIn("response", item["public"])
        self.assertEqual(len(self.store.batches(USER["username"], PROJECT_ID)), 1)
        changed = copy.deepcopy(body)
        changed["payload"]["topic"] = "另一个主题"
        with self.assertRaises(APIError) as raised:
            self.service.message(USER, self.headers, changed)
        self.assertEqual(raised.exception.code, "idempotency_conflict")

    def test_creator_service_has_no_ip12_runtime_client(self):
        self.assertFalse(hasattr(self.service, "ip12"))

    def _draft_batch(self):
        result = self.message(
            "制作视频", "start_video",
            {"topic": "企业内容获客", "platforms": ["douyin", "wechat_channels"]},
            "3001",
        )
        return result["latest_batch"]

    def test_creator_multiplatform_hyperframes_rejects_before_child_quotes(self):
        draft = self._draft_batch()
        plans = copy.deepcopy(draft["plans"])
        for plan in plans:
            plan["input"]["template_id"] = "ref-01-chengdu-green-brush"
        updated = self.store.replace_batch_plans(
            USER["username"], draft["id"], plans, draft["revision"]
        )
        self.bridge.templates.append({
            "id": "ref-01-chengdu-green-brush",
            "name": "成都绿描边手写",
            "engine": "hyperframes",
            "font_selectable": False,
        })

        with self.assertRaises(APIError) as raised:
            self.service.quote_batch(USER, updated["id"], updated["revision"])
        self.assertEqual("matrix_template_single_only", raised.exception.code)
        self.assertEqual(0, self.bridge.quote_count)
        self.assertEqual([], [
            call for call in self.bridge.calls
            if call[0] == "action" and call[1] == "matrix-template-generate"
        ])

    def test_creator_single_platform_hyperframes_still_quotes(self):
        draft = self.message(
            "制作视频", "start_video",
            {"topic": "企业内容获客", "platforms": ["douyin"]},
            "single-hf-3001",
        )["latest_batch"]
        plans = copy.deepcopy(draft["plans"])
        plans[0]["input"]["template_id"] = "ref-01-chengdu-green-brush"
        updated = self.store.replace_batch_plans(
            USER["username"], draft["id"], plans, draft["revision"]
        )
        self.bridge.templates.append({
            "id": "ref-01-chengdu-green-brush",
            "name": "成都绿描边手写",
            "engine": "hyperframes",
            "font_selectable": False,
        })

        quoted = self.service.quote_batch(
            USER, updated["id"], updated["revision"]
        )
        self.assertEqual("quoted", quoted["status"])
        self.assertEqual(1, self.bridge.quote_count)

    def test_unified_quote_keeps_private_tokens_server_side(self):
        draft = self._draft_batch()
        quoted = self.service.quote_batch(USER, draft["id"], draft["revision"])
        self.assertEqual(quoted["status"], "quoted")
        self.assertEqual(quoted["quote"]["total_cost"], 10)
        raw = json.dumps(quoted, ensure_ascii=False)
        self.assertNotIn("private-quote", raw)
        self.assertNotIn("quote_token", raw)
        private = self.store.batch(USER["username"], draft["id"], include_private=True)
        self.assertTrue(all(item["quote_token"] for item in private["jobs"]))
        self.assertEqual(quoted["quote_expires_at"], quoted["quote"]["expires_at"])
        self.assertTrue(all(item["quote_expires_at"] == quoted["quote_expires_at"] for item in private["jobs"]))
        self.assertTrue(all(item["quote_cost"] == 5 for item in private["jobs"]))

    def test_unexpired_quote_is_reused_idempotently(self):
        draft = self._draft_batch()
        first = self.service.quote_batch(USER, draft["id"], draft["revision"])
        quote_calls = self.bridge.quote_count
        self.clock.advance(60)
        second = self.service.quote_batch(USER, draft["id"], draft["revision"])
        self.assertEqual(second, first)
        self.assertEqual(self.bridge.quote_count, quote_calls)

    def test_expired_quote_is_atomically_requoted(self):
        draft = self._draft_batch()
        first = self.service.quote_batch(USER, draft["id"], draft["revision"])
        first_expiry = first["quote_expires_at"]
        self.clock.advance(301)
        second = self.service.quote_batch(USER, draft["id"], draft["revision"])
        self.assertEqual(second["status"], "quoted")
        self.assertGreater(second["quote_expires_at"], first_expiry)
        self.assertEqual(self.bridge.quote_count, 4)
        private = self.store.batch(USER["username"], draft["id"], include_private=True)
        self.assertTrue(all(job["quote_expires_at"] == second["quote_expires_at"] for job in private["jobs"]))

    def test_confirmation_for_old_quote_cannot_submit_a_requoted_batch(self):
        draft = self._draft_batch()
        first = self.service.quote_batch(USER, draft["id"], draft["revision"])
        self.clock.advance(301)
        second = self.service.quote_batch(USER, draft["id"], draft["revision"])
        before = self.store.batch(USER["username"], draft["id"], include_private=True)
        with self.assertRaises(APIError) as raised:
            self.service.confirm_batch(
                USER, draft["id"], "creator-confirm-old-quote",
                second["revision"], first["quote_expires_at"],
            )
        self.assertEqual(raised.exception.code, "quote_expired")
        self.assertEqual(
            self.store.batch(USER["username"], draft["id"], include_private=True),
            before,
        )
        self.assertEqual(self.bridge.confirm_count, 0)

    def test_near_expiry_confirmation_is_rejected_without_state_change_or_provider_call(self):
        draft = self._draft_batch()
        quoted = self.service.quote_batch(USER, draft["id"], draft["revision"])
        margin = self.service._quote_safety_margin(len(quoted["jobs"]))
        self.clock.advance(300 - margin + 1)
        before = self.store.batch(USER["username"], draft["id"], include_private=True)
        with self.assertRaises(APIError) as raised:
            self.service.confirm_batch(
                USER, draft["id"], "creator-confirm-near-expiry", quoted["revision"],
                quoted["quote_expires_at"],
            )
        self.assertEqual(raised.exception.code, "quote_expired")
        after = self.store.batch(USER["username"], draft["id"], include_private=True)
        self.assertEqual(after, before)
        self.assertEqual(self.bridge.confirm_count, 0)

    def test_three_platform_sequential_submit_stays_inside_quote_window(self):
        result = self.message(
            "制作视频", "start_video", {
                "topic": "企业内容获客",
                "platforms": ["douyin", "xiaohongshu", "wechat_channels"],
            }, "three-platform-quote",
        )
        draft = result["latest_batch"]
        quoted = self.service.quote_batch(USER, draft["id"], draft["revision"])
        margin = self.service._quote_safety_margin(3)
        self.clock.advance(300 - margin - 1)
        self.bridge.confirm_advance_seconds = 35
        submitted = self.service.confirm_batch(
            USER, draft["id"], "creator-confirm-three-platform", quoted["revision"],
            quoted["quote_expires_at"],
        )
        self.assertEqual(self.bridge.confirm_count, 3)
        self.assertEqual(submitted["status"], "running")
        self.assertLess(int(self.clock()), quoted["quote_expires_at"])

    def test_three_platform_stale_claim_recovery_stays_inside_quote_window(self):
        result = self.message(
            "制作视频", "start_video", {
                "topic": "企业内容获客",
                "platforms": ["douyin", "xiaohongshu", "wechat_channels"],
            }, "three-platform-recovery",
        )
        draft = result["latest_batch"]
        quoted = self.service.quote_batch(USER, draft["id"], draft["revision"])
        margin = self.service._quote_safety_margin(3)
        self.clock.advance(300 - margin - 1)
        self.store.claim_confirmation(
            USER["username"], draft["id"], "creator-confirm-three-recovery",
            quoted["revision"], quoted["quote_expires_at"],
            now=int(self.clock()), safety_margin_seconds=margin,
        )
        self.clock.advance(STALE_CLAIM_SECONDS)
        self.bridge.confirm_advance_seconds = 35
        recovered = self.service.refresh_batch(USER, draft["id"])
        self.assertEqual(self.bridge.confirm_count, 3)
        self.assertEqual(recovered["status"], "running")
        self.assertLess(int(self.clock()), quoted["quote_expires_at"])

    def test_expired_message_confirmation_requotes_instead_of_failing_generation(self):
        draft = self._draft_batch()
        quoted = self.service.quote_batch(USER, draft["id"], draft["revision"])
        self.clock.advance(301)
        result = self.message(
            "确认扣点并开始生成", "confirm_payment", {
                "batch_id": draft["id"],
                "expected_revision": quoted["revision"],
                "expected_quote_expires_at": quoted["quote_expires_at"],
                "confirmation_id": "creator-confirm-expired-message",
            }, "expired-message-confirm",
        )
        self.assertEqual(result["message_public"]["kind"], "video_quote")
        self.assertIn("自动重新报价", result["reply"])
        self.assertEqual(self.bridge.confirm_count, 0)
        self.assertEqual(self.bridge.quote_count, 4)

    def test_one_confirmation_submits_each_platform_once(self):
        draft = self._draft_batch()
        quoted = self.service.quote_batch(USER, draft["id"], draft["revision"])
        first = self.service.confirm_batch(
            USER, draft["id"], "creator-confirm-0001", quoted["revision"],
            quoted["quote_expires_at"],
        )
        second = self.service.confirm_batch(
            USER, draft["id"], "creator-confirm-0001", quoted["revision"],
            quoted["quote_expires_at"],
        )
        self.assertEqual(first, second)
        self.assertEqual(self.bridge.confirm_count, 2)
        self.assertNotIn("job_id", json.dumps(first, ensure_ascii=False))

    def test_edit_vs_confirm_uses_frozen_submission_snapshot(self):
        draft = self._draft_batch()
        quoted = self.service.quote_batch(USER, draft["id"], draft["revision"])
        original = self.bridge.action
        entered, release = threading.Event(), threading.Event()
        submitted_inputs, errors = [], []

        def slow(account_id, action, tool_input, **options):
            if action == "matrix-template-generate" and options.get("confirm"):
                submitted_inputs.append(copy.deepcopy(tool_input))
                entered.set(); release.wait(5)
            return original(account_id, action, tool_input, **options)

        self.bridge.action = slow
        thread = threading.Thread(target=lambda: self._capture_error(
            errors, lambda: self.service.confirm_batch(
                USER, draft["id"], "creator-confirm-race-edit", quoted["revision"],
                quoted["quote_expires_at"],
            ),
        ))
        thread.start(); self.assertTrue(entered.wait(3))
        current = self.store.batch(USER["username"], draft["id"], include_private=True)
        with self.assertRaises(StateConflict):
            self.service._revise_video_plan(
                USER, self.store.workspace(USER["username"], PROJECT_ID), current,
                "抖音标题改成：不应提交的新标题",
            )
        release.set(); thread.join(5)
        self.assertFalse(errors)
        final = self.store.batch(USER["username"], draft["id"], include_private=True)
        self.assertEqual(len(submitted_inputs), 2)
        self.assertTrue(all(job["input"] == job["submit_input"] for job in final["jobs"]))
        self.assertNotIn("不应提交", json.dumps(submitted_inputs, ensure_ascii=False))

    def test_double_confirm_claims_each_provider_job_once(self):
        draft = self._draft_batch()
        quoted = self.service.quote_batch(USER, draft["id"], draft["revision"])
        original = self.bridge.action
        entered, release = threading.Event(), threading.Event()
        errors = []

        def slow(account_id, action, tool_input, **options):
            if action == "matrix-template-generate" and options.get("confirm"):
                entered.set(); release.wait(5)
            return original(account_id, action, tool_input, **options)

        self.bridge.action = slow
        call = lambda: self.service.confirm_batch(
            USER, draft["id"], "creator-confirm-race-double", quoted["revision"],
            quoted["quote_expires_at"],
        )
        first = threading.Thread(target=lambda: self._capture_error(errors, call))
        first.start(); self.assertTrue(entered.wait(3))
        second = threading.Thread(target=lambda: self._capture_error(errors, call))
        second.start(); second.join(3); release.set(); first.join(5)
        self.assertFalse(errors)
        self.assertEqual(self.bridge.confirm_count, 2)

    def test_quote_vs_edit_is_serialized_by_batch_claim(self):
        draft = self._draft_batch()
        original = self.bridge.action
        entered, release = threading.Event(), threading.Event()
        errors = []

        def slow(account_id, action, tool_input, **options):
            if action == "matrix-template-generate" and not options.get("confirm"):
                entered.set(); release.wait(5)
            return original(account_id, action, tool_input, **options)

        self.bridge.action = slow
        thread = threading.Thread(target=lambda: self._capture_error(
            errors, lambda: self.service.quote_batch(USER, draft["id"], draft["revision"]),
        ))
        thread.start(); self.assertTrue(entered.wait(3))
        current = self.store.batch(USER["username"], draft["id"], include_private=True)
        with self.assertRaises(StateConflict):
            self.service._revise_video_plan(
                USER, self.store.workspace(USER["username"], PROJECT_ID), current,
                "抖音标题改成：报价期间不允许编辑",
            )
        release.set(); thread.join(5)
        self.assertFalse(errors)
        self.assertEqual(self.store.batch(USER["username"], draft["id"])["status"], "quoted")

    def test_refresh_vs_confirm_never_resubmits_claimed_jobs(self):
        draft = self._draft_batch()
        quoted = self.service.quote_batch(USER, draft["id"], draft["revision"])
        original = self.bridge.action
        entered, release = threading.Event(), threading.Event()
        errors = []

        def slow(account_id, action, tool_input, **options):
            if action == "matrix-template-generate" and options.get("confirm"):
                entered.set(); release.wait(5)
            return original(account_id, action, tool_input, **options)

        self.bridge.action = slow
        thread = threading.Thread(target=lambda: self._capture_error(
            errors, lambda: self.service.confirm_batch(
                USER, draft["id"], "creator-confirm-race-refresh", quoted["revision"],
                quoted["quote_expires_at"],
            ),
        ))
        thread.start(); self.assertTrue(entered.wait(3))
        during = self.service.refresh_batch(USER, draft["id"])
        self.assertEqual(during["status"], "running")
        release.set(); thread.join(5)
        self.assertFalse(errors)
        self.assertEqual(self.bridge.confirm_count, 2)

    def test_stale_quote_claim_can_resume_after_restart(self):
        draft = self._draft_batch()
        self.store.claim_quote(USER["username"], draft["id"], draft["revision"])
        with closing(self.store.db()) as connection:
            connection.execute(
                "UPDATE creator_batches SET updated_at=0 WHERE id=?", (draft["id"],),
            )
            connection.commit()
        quoted = self.service.quote_batch(USER, draft["id"], draft["revision"])
        self.assertEqual(quoted["status"], "quoted")

    def test_stale_submit_claim_recovers_frozen_jobs_after_restart(self):
        draft = self._draft_batch()
        quoted = self.service.quote_batch(USER, draft["id"], draft["revision"])
        self.store.claim_confirmation(
            USER["username"], draft["id"], "creator-confirm-stale",
            quoted["revision"], quoted["quote_expires_at"],
        )
        with closing(self.store.db()) as connection:
            connection.execute(
                "UPDATE creator_jobs SET updated_at=0 WHERE batch_id=?", (draft["id"],),
            )
            connection.commit()
        recovered = self.service.refresh_batch(USER, draft["id"])
        self.assertEqual(recovered["status"], "running")
        self.assertEqual(self.bridge.confirm_count, 2)

    @staticmethod
    def _capture_error(errors, function):
        try:
            function()
        except Exception as exc:
            errors.append(exc)

    def test_uncertain_submission_reuses_original_child_idempotency_key(self):
        draft = self._draft_batch()
        quoted = self.service.quote_batch(USER, draft["id"], draft["revision"])
        original = self.bridge.action
        failed_once = {"value": False}

        def flaky(account_id, action, tool_input, **options):
            if action == "matrix-template-generate" and options.get("confirm") and not failed_once["value"]:
                failed_once["value"] = True
                raise APIError(503, "response lost", "bridge_unavailable")
            return original(account_id, action, tool_input, **options)

        self.bridge.action = flaky
        first = self.service.confirm_batch(
            USER, draft["id"], "creator-confirm-uncertain", quoted["revision"],
            quoted["quote_expires_at"],
        )
        unknown = next(item for item in first["jobs"] if item["status"] == "submission_unknown")
        private_before = self.store.batch(USER["username"], draft["id"], include_private=True)
        original_key = next(item for item in private_before["jobs"] if item["id"] == unknown["id"])["idempotency_key"]
        recovered = self.service.refresh_batch(USER, draft["id"])
        private_after = self.store.batch(USER["username"], draft["id"], include_private=True)
        recovered_job = next(item for item in private_after["jobs"] if item["id"] == unknown["id"])
        self.assertEqual(recovered_job["idempotency_key"], original_key)
        self.assertEqual(recovered_job["status"], "running")
        self.assertEqual(self.bridge.confirm_count, 2)
        self.assertEqual(recovered["status"], "running")

    def test_accepted_lost_response_recovers_same_job_after_quote_ttl(self):
        draft = self._draft_batch()
        quoted = self.service.quote_batch(USER, draft["id"], draft["revision"])
        original = self.bridge.action
        lost_once = {"value": False}

        def accepted_then_lost(account_id, action, tool_input, **options):
            if (
                action == "matrix-template-generate"
                and options.get("confirm")
                and not lost_once["value"]
            ):
                lost_once["value"] = True
                original(account_id, action, tool_input, **options)
                raise APIError(503, "response lost after acceptance", "bridge_unavailable")
            return original(account_id, action, tool_input, **options)

        self.bridge.action = accepted_then_lost
        first = self.service.confirm_batch(
            USER, draft["id"], "creator-confirm-accepted-loss",
            quoted["revision"], quoted["quote_expires_at"],
        )
        unknown = next(job for job in first["jobs"] if job["status"] == "submission_unknown")
        private = self.store.batch(USER["username"], draft["id"], include_private=True)
        frozen = next(job for job in private["jobs"] if job["id"] == unknown["id"])
        accepted_job_id = self.bridge.accepted_submissions[
            frozen["submit_idempotency_key"]
        ]["job_id"]
        self.clock.advance(301)
        recovered = self.service.refresh_batch(USER, draft["id"])
        recovered_job = next(job for job in recovered["jobs"] if job["id"] == unknown["id"])
        recovered_private = self.store.batch(
            USER["username"], draft["id"], include_private=True,
        )
        recovered_private_job = next(
            job for job in recovered_private["jobs"] if job["id"] == unknown["id"]
        )
        self.assertEqual(recovered_job["status"], "running")
        self.assertEqual(recovered_private_job["job_id"], accepted_job_id)
        self.assertEqual(self.bridge.confirm_count, 2)
        self.assertEqual(self.bridge.reconcile_count, 1)
        self.assertNotIn("failed_submission", {job["status"] for job in recovered["jobs"]})

    def test_expired_missing_reconcile_never_becomes_a_first_submission(self):
        draft = self._draft_batch()
        quoted = self.service.quote_batch(USER, draft["id"], draft["revision"])
        self.store.claim_confirmation(
            USER["username"], draft["id"], "creator-confirm-never-accepted",
            quoted["revision"], quoted["quote_expires_at"],
            now=int(self.clock()),
        )
        self.clock.advance(301)
        recovered = self.service.refresh_batch(USER, draft["id"])
        self.assertEqual(recovered["status"], "failed")
        self.assertTrue(all(
            job["status"] == "failed_submission" for job in recovered["jobs"]
        ))
        self.assertEqual(self.bridge.confirm_count, 0)
        self.assertEqual(self.bridge.reconcile_count, 2)

    def test_reconciled_refund_is_a_terminal_failed_job(self):
        result = self.service._submission_result({
            "status": "failed", "error": "任务创建失败，点数已退回",
            "refund_status": "refunded", "reconciled": True,
        })
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["refund_status"], "refunded")
        self.assertTrue(result["result"]["reconciled"])

    def test_submission_result_requires_a_positive_content_job_id(self):
        accepted = self.service._submission_result({
            "status": "running", "job_id": "7161",
        })
        self.assertEqual(accepted["job_id"], "7161")
        for invalid in (True, 0, -1, "0", "not-a-job", 9_223_372_036_854_775_808):
            with self.subTest(invalid=invalid), self.assertRaises(APIError) as raised:
                self.service._submission_result({
                    "status": "running", "job_id": invalid,
                })
            self.assertEqual(raised.exception.code, "submit_result_unknown")

    def test_corrupt_stored_job_id_becomes_terminal_instead_of_polling_forever(self):
        draft = self._draft_batch()
        quoted = self.service.quote_batch(USER, draft["id"], draft["revision"])
        submitted = self.service.confirm_batch(
            USER, draft["id"], "creator-confirm-corrupt-job-id",
            quoted["revision"], quoted["quote_expires_at"],
        )
        corrupted = submitted["jobs"][0]
        self.store.update_job(
            USER["username"], corrupted["id"], job_id="provider-id-is-not-numeric",
        )
        refreshed = self.service.refresh_batch(USER, draft["id"])
        current = next(item for item in refreshed["jobs"] if item["id"] == corrupted["id"])
        self.assertEqual(current["status"], "failed")
        self.assertIn("任务编号无效", current["error"])
        self.assertFalse(any(
            call[0] == "action" and call[1] == "task"
            and call[2].get("job_id") == "provider-id-is-not-numeric"
            for call in self.bridge.calls
        ))

    def test_one_failed_platform_does_not_erase_other_success(self):
        draft = self._draft_batch()
        quoted = self.service.quote_batch(USER, draft["id"], draft["revision"])
        self.service.confirm_batch(
            USER, draft["id"], "creator-confirm-0002", quoted["revision"],
            quoted["quote_expires_at"],
        )
        self.bridge.task_results = {
            "7001": {"status": "done", "result": {"video_url": "/media/one.mp4"}},
            "7002": {"status": "failed", "error": "render failed", "refund_status": "refunded"},
        }
        refreshed = self.service.refresh_batch(USER, draft["id"])
        self.assertEqual(refreshed["status"], "partial")
        task_calls = [
            call for call in self.bridge.calls
            if call[0] == "action" and call[1] == "task"
        ]
        self.assertTrue(task_calls)
        self.assertTrue(all(
            isinstance(call[2]["job_id"], int) for call in task_calls
        ))
        statuses = {item["platform"]: item["status"] for item in refreshed["jobs"]}
        self.assertEqual(set(statuses.values()), {"done", "failed"})
        failed = next(item for item in refreshed["jobs"] if item["status"] == "failed")
        self.assertEqual(failed["refund_status"], "refunded")

    def test_completed_video_revision_creates_new_unquoted_version(self):
        draft = self.message(
            "制作视频", "start_video",
            {"topic": "企业内容获客", "platforms": ["douyin", "xiaohongshu"]},
            "version-0001",
        )["latest_batch"]
        quoted = self.service.quote_batch(USER, draft["id"], draft["revision"])
        self.service.confirm_batch(
            USER, draft["id"], "creator-confirm-version", quoted["revision"],
            quoted["quote_expires_at"],
        )
        self.bridge.task_results = {
            "7001": {"status": "done", "result": {"video_url": "/media/douyin.mp4"}},
            "7002": {"status": "done", "result": {"video_url": "/media/xhs.mp4"}},
        }
        self.service.refresh_batch(USER, draft["id"])
        before_quotes = len([
            call for call in self.bridge.calls
            if call[0] == "action" and call[1] == "matrix-template-generate" and not call[3].get("confirm")
        ])
        result = self.message(
            "小红书标题改成：企业做 Agent 前先看这份清单",
            suffix="version-0002",
        )
        latest = result["latest_batch"]
        self.assertNotEqual(latest["id"], draft["id"])
        self.assertEqual(latest["status"], "draft")
        xhs = next(item for item in latest["plans"] if item["platform"] == "xiaohongshu")
        self.assertEqual(xhs["top_text"], "企业做 Agent 前先看这份清单")
        self.assertEqual([job["platform"] for job in latest["jobs"]], ["xiaohongshu"])
        self.assertEqual(latest["jobs"][0]["version"], 2)
        after_quotes = len([
            call for call in self.bridge.calls
            if call[0] == "action" and call[1] == "matrix-template-generate" and not call[3].get("confirm")
        ])
        self.assertEqual(before_quotes, after_quotes)

    def test_long_term_preference_is_global_or_platform_specific(self):
        value, changed = remember_preference({}, "以后标题不要夸张")
        self.assertTrue(changed)
        self.assertIn("以后标题不要夸张", value["global"])
        value, changed = remember_preference(value, "小红书以后更像真实经验分享")
        self.assertTrue(changed)
        self.assertIn("小红书以后更像真实经验分享", value["platforms"]["xiaohongshu"])
        value, changed = remember_preference(value, "这条视频用正式语气")
        self.assertFalse(changed)
        value, changed = remember_preference(value, "抖音和视频号以后都使用直接标题")
        self.assertTrue(changed)
        self.assertIn("抖音和视频号以后都使用直接标题", value["platforms"]["douyin"])
        self.assertIn("抖音和视频号以后都使用直接标题", value["platforms"]["wechat_channels"])

    def test_confirmed_profile_modification_is_saved_in_own_profile(self):
        self.message("修改我的画像", "modify_profile", {}, "profile-0001")
        result = self.message(
            "目标客户改为准备数字化转型的制造企业",
            suffix="profile-0002",
        )
        overrides = result["workspace"]["profile_overrides"]
        self.assertIn("目标客户改为", overrides["general"][-1]["content"])
        self.assertIn("独立画像", result["reply"])
        self.assertTrue(any(call[0] == "reply" for call in self.profile_agent.calls))
        self.assertIn("overrides", result["workspace"]["profile"])

    def test_collecting_profile_is_not_treated_as_ready(self):
        project = {"profile_state": initial_state()}
        self.assertFalse(self.service._foundation_ready(project))

    def test_matrix_creator_contract_quotes_registered_generation(self):
        catalog = hq_cli_api.action_catalog({"matrix_template_video": True})
        action = next(item for item in catalog["actions"] if item["action"] == "matrix-template-generate")
        self.assertEqual(action["billing"], "quote_then_confirm")
        plan = hq_cli_api.action_plan("matrix-template-generate", {
            "top_text": "企业别急着做 Agent",
            "bottom_text": "关注获取完整流程",
            "template_id": "native-bold",
        })
        self.assertEqual(plan["generation_kind"], "matrix_template_video")
        self.assertEqual(plan["endpoint"], "/api/gen/matrix-template")

    def test_auth_bridge_exposes_only_creator_actions_and_task_status(self):
        import auth_server

        handler = auth_server.H.__new__(auth_server.H)
        handler._cli_send = lambda status, value: (status, value)
        handler._creator_agent_row = lambda account_id: {
            "username": USER["username"], "account_id": account_id,
        }
        handler._execute_cli_action = mock.Mock(return_value=(200, {"ok": True}))
        with mock.patch.object(auth_server.feature_flags, "is_enabled", return_value=True), \
             mock.patch.object(
                 auth_server.hq_cli_api, "proxy_json",
                 return_value=(200, {"ok": True, "ready": True}),
             ):
            status, catalog = handler._internal_creator_agent_catalog({"account_id": USER["account_id"]})
            self.assertEqual(status, 200)
            self.assertEqual(
                {item["action"] for item in catalog["actions"]},
                {"matrix-template-capability", "matrix-template-templates", "matrix-template-generate"},
            )
            result = handler._internal_creator_agent_action({
                "account_id": USER["account_id"], "action": "matrix-template-generate",
                "input": {"top_text": "标题内容", "bottom_text": "关注查看更多", "template_id": "native-bold"},
                "confirm": False,
            })
            rejected = handler._internal_creator_agent_action({
                "account_id": USER["account_id"], "action": "image-generate",
                "input": {"prompt": "not allowed"}, "confirm": False,
            })
            health_status, health = handler._internal_creator_agent_health()
        self.assertEqual(result, (200, {"ok": True}))
        self.assertEqual(rejected[0], 404)
        self.assertEqual(health_status, 200)
        self.assertTrue(health["ready"])
        self.assertIn("matrix-template-generate", health["actions"])
        self.assertIn("matrix-template-reconcile", health["actions"])
        self.assertTrue(handler._execute_cli_action.call_args.kwargs["trusted_internal"])

    def test_auth_creator_reconcile_is_internal_read_only_and_quote_independent(self):
        import auth_server

        handler = auth_server.H.__new__(auth_server.H)
        handler._cli_send = lambda status, value: (status, value)
        handler._creator_agent_row = lambda account_id: {
            "username": USER["username"], "account_id": account_id,
        }
        handler._cli_proxy = mock.Mock(return_value=(200, {
            "job_id": 8123, "cost": 5, "reconciled": True,
        }))
        body = {
            "account_id": USER["account_id"],
            "input": {
                "top_text": "标题内容", "bottom_text": "关注查看更多",
                "template_id": "native-bold",
            },
            "idempotency_key": "creator-reconcile-0001",
        }
        with mock.patch.object(auth_server.feature_flags, "is_enabled", return_value=True):
            status, result = handler._internal_creator_agent_reconcile(body)
        self.assertEqual((status, result["job_id"]), (200, 8123))
        plan = handler._cli_proxy.call_args.args[0]
        self.assertEqual(plan["path"], "/api/gen/internal/submission-reconcile")
        self.assertEqual(plan["body"]["endpoint"], "/api/gen/matrix-template")
        self.assertEqual(plan["body"]["idempotency_key"], "creator-reconcile-0001")
        self.assertNotIn("quote_token", plan["body"])
        handler._cli_proxy.return_value = (404, {"detail": "not found"})
        with mock.patch.object(auth_server.feature_flags, "is_enabled", return_value=True):
            unavailable_status, unavailable = handler._internal_creator_agent_reconcile(body)
        self.assertEqual((unavailable_status, unavailable["code"]), (
            503, "reconcile_unavailable",
        ))
        handler._cli_proxy.return_value = (500, {
            "detail": "任务创建失败，点数已退回",
            "code": "job_create_failed", "operation_terminal": True,
        })
        with mock.patch.object(auth_server.feature_flags, "is_enabled", return_value=True):
            terminal_status, terminal = handler._internal_creator_agent_reconcile(body)
        self.assertEqual(terminal_status, 200)
        self.assertEqual((terminal["status"], terminal["refund_status"]), (
            "failed", "refunded",
        ))

    def test_auth_creator_health_fails_closed_without_content_reconcile(self):
        import auth_server

        handler = auth_server.H.__new__(auth_server.H)
        handler._cli_send = lambda status, value: (status, value)
        with mock.patch.object(auth_server.feature_flags, "is_enabled", return_value=True), \
             mock.patch.object(
                 auth_server.hq_cli_api, "proxy_json",
                 return_value=(404, {"detail": "not found"}),
             ):
            status, health = handler._internal_creator_agent_health()
        self.assertEqual(status, 200)
        self.assertFalse(health["ready"])
        self.assertFalse(health["checks"]["submission_reconcile"])
        self.assertNotIn("matrix-template-reconcile", health["actions"])

    def test_http_bootstrap_returns_single_project_workspace(self):
        CreatorAgentHandler.service = self.service
        server = ThreadingHTTPServer(("127.0.0.1", 0), CreatorAgentHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = "http://127.0.0.1:%d" % server.server_address[1]
            value = json.load(urllib.request.urlopen(base + "/bootstrap", timeout=3))
            self.assertEqual(value["project"]["id"], PROJECT_ID)
            self.assertEqual(len(value["projects"]), 1)
            self.assertNotIn("quote_token", json.dumps(value, ensure_ascii=False))
            health = json.load(urllib.request.urlopen(base + "/health", timeout=3))
            self.assertTrue(health["ready"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_http_messages_trailing_slash_is_not_routed(self):
        CreatorAgentHandler.service = self.service
        server = ThreadingHTTPServer(("127.0.0.1", 0), CreatorAgentHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = urllib.request.Request(
                "http://127.0.0.1:%d/messages/" % server.server_address[1],
                data=json.dumps({
                    "message": "制作视频",
                    "request_id": "trailing-slash-must-fail-0001",
                }).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request, timeout=3)
            self.assertEqual(raised.exception.code, 404)
            body = json.loads(raised.exception.read())
            self.assertEqual(body["code"], "not_found")
            self.assertFalse(any(
                item["content"] == "制作视频"
                for item in self.store.messages(USER["username"], PROJECT_ID)
            ))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_http_background_pdf_is_private_and_rendered(self):
        CreatorAgentHandler.service = self.service
        server = ThreadingHTTPServer(("127.0.0.1", 0), CreatorAgentHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = (
                "http://127.0.0.1:%d/projects/%s/background.pdf" %
                (server.server_address[1], PROJECT_ID)
            )
            with urllib.request.urlopen(url, timeout=5) as response:
                payload = response.read()
                self.assertEqual(response.headers.get_content_type(), "application/pdf")
                self.assertEqual(response.headers.get("Cache-Control"), "private, no-store")
            self.assertEqual(payload[:5], b"%PDF-")
            self.assertGreater(len(payload), 1024)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_http_batch_routes_use_revisioned_state_machine(self):
        draft = self._draft_batch()
        CreatorAgentHandler.service = self.service
        server = ThreadingHTTPServer(("127.0.0.1", 0), CreatorAgentHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = "http://127.0.0.1:%d" % server.server_address[1]

            def post(path, body):
                request = urllib.request.Request(
                    base + path,
                    data=json.dumps(body).encode("utf-8"),
                    headers={"Content-Type": "application/json"}, method="POST",
                )
                return json.load(urllib.request.urlopen(request, timeout=3))

            quoted = post(
                "/batches/%s/quote" % draft["id"],
                {"expected_revision": draft["revision"]},
            )["batch"]
            submitted = post(
                "/batches/%s/confirm" % draft["id"],
                {"expected_revision": quoted["revision"],
                 "expected_quote_expires_at": quoted["quote_expires_at"],
                 "confirmation_id": "creator-http-confirm-0001"},
            )["batch"]
            refreshed = post("/batches/%s/refresh" % draft["id"], {})["batch"]
            self.assertEqual(submitted["status"], "running")
            self.assertEqual(refreshed["status"], "running")
            self.assertEqual(self.bridge.confirm_count, 2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
