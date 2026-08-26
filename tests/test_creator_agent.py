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
import urllib.request


ROOT = pathlib.Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
sys.path.insert(0, str(SERVER))

from creator_agent.planner import CreatorPlanner, GuidedPlanner, remember_preference
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


def ready_project():
    return {
        "id": PROJECT_ID,
        "title": "我的个人画像",
        "updated": "2026-08-26 12:00",
        "messages": [{"role": "assistant", "content": "画像已准备好。", "message_id": "welcome"}],
        "coach_state": {
            "revision": 9,
            "current_module": 5,
            "module_step": 0,
            "completed_modules": [1, 2, 3, 4],
            "foundation_report": {"status": "confirmed", "report_id": "report-1"},
            "ip_profile": {
                "confirmed_outputs": {
                    "module_1": {"content": "AI 企业服务，目标客户是互联网企业"},
                    "module_2": {"content": "轻松专业"},
                    "module_3": {"content": "用 AI 降低企业复杂工作成本"},
                    "module_4": {"content": "真实项目实践"},
                },
            },
        },
        "harness_actions": [],
        "reports": {},
        "deliverables": {},
        "artifacts": [],
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


class FakeIP12:
    def __init__(self, project=None):
        self.base_url = "http://127.0.0.1:3102"
        self.value = copy.deepcopy(project) if project else None
        self.calls = []

    def health(self):
        return {"ok": True}

    def projects(self, headers):
        return [] if self.value is None else [{
            "id": self.value["id"], "title": self.value["title"],
            "updated": self.value.get("updated", ""),
        }]

    def create(self, headers, title="我的个人画像"):
        self.calls.append(("create", title))
        self.value = {
            "id": PROJECT_ID, "title": title, "updated": "2026-08-26 12:00",
            "messages": [{"role": "assistant", "content": "先告诉我希望怎么称呼你。", "message_id": "first"}],
            "coach_state": {
                "revision": 1, "current_module": 1, "module_step": 0,
                "completed_modules": [], "foundation_report": {"status": "missing"},
                "ip_profile": {},
            },
            "harness_actions": [], "reports": {}, "deliverables": {}, "artifacts": [],
        }
        return {"id": PROJECT_ID, "title": title}

    def project(self, headers, project_id):
        assert project_id == PROJECT_ID
        return copy.deepcopy(self.value)

    def turn(self, headers, project_id, **kwargs):
        self.calls.append(("turn", kwargs))
        message = kwargs.get("message") or (kwargs.get("action") or {}).get("type") or "继续"
        self.value["messages"].append({"role": "user", "content": message, "message_id": "u-%d" % len(self.value["messages"])})
        assistant = "已记录，我会继续完成当前 IP12 步骤。"
        self.value["messages"].append({"role": "assistant", "content": assistant, "message_id": "a-%d" % len(self.value["messages"])})
        self.value["coach_state"]["revision"] += 1
        return {"assistant": assistant, "actions": [], "new_completed": []}

    def generate_foundation(self, headers, project_id):
        self.calls.append(("generate_foundation", project_id))
        self.value["coach_state"]["foundation_report"] = {
            "status": "awaiting_confirmation", "report_id": "report-2",
        }
        self.value["coach_state"]["revision"] += 1
        return {"ok": True}

    def confirm_foundation(self, headers, project_id, revision, report_id):
        self.calls.append(("confirm_foundation", project_id, revision, report_id))
        self.value["coach_state"]["foundation_report"]["status"] = "confirmed"
        self.value["coach_state"]["revision"] += 1
        return {"ok": True}


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
        self.ip12 = FakeIP12(ready_project())
        self.clock = MutableClock()
        self.bridge = FakeBridge(self.clock)
        self.service = CreatorAgentService(
            self.store, CreatorPlanner(provider=None, fallback=GuidedPlanner()),
            FakeAuth(), self.bridge, self.ip12, clock=self.clock,
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

    def test_bootstrap_creates_missing_profile_and_starts_ip12(self):
        ip12 = FakeIP12()
        service = CreatorAgentService(
            self.store, CreatorPlanner(provider=None, fallback=GuidedPlanner()),
            FakeAuth(), self.bridge, ip12,
        )
        result = service.bootstrap(USER, self.headers)
        self.assertEqual(ip12.calls[0], ("create", "我的个人画像"))
        self.assertEqual(result["project"]["id"], PROJECT_ID)
        self.assertFalse(result["project"]["progress"]["profile_complete"])
        self.assertIn("先告诉我", result["messages"][0]["content"])

    def test_health_requires_real_bridge_ip12_and_writable_database(self):
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
            "CREATOR_AGENT_IP12_URL": "http://127.0.0.1:3102",
            "HQ_INTERNAL_TOKEN": "production-shared-token",
        })
        self.assertEqual(service.bridge.internal_token, "production-shared-token")

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

    def test_incomplete_profile_routes_message_to_ip12_without_paid_action(self):
        self.ip12 = FakeIP12()
        self.ip12.create({}, "我的个人画像")
        self.service.ip12 = self.ip12
        result = self.message("大家叫我小黄", suffix="1001")
        self.assertIn("继续完成", result["reply"])
        self.assertEqual(len([call for call in self.ip12.calls if call[0] == "turn"]), 1)
        paid = [call for call in self.bridge.calls if call[0] == "action" and call[1] == "matrix-template-generate"]
        self.assertEqual(paid, [])

    def test_video_plan_is_per_platform_and_does_not_quote_automatically(self):
        result = self.message(
            "制作视频", "start_video",
            {"topic": "企业做 Agent 前先梳理流程", "platforms": ["douyin", "xiaohongshu"]},
            "2001",
        )
        batch = result["latest_batch"]
        self.assertEqual(batch["status"], "draft")
        self.assertEqual({item["platform"] for item in batch["plans"]}, {"douyin", "xiaohongshu"})
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

    def test_topic_plan_requires_platforms_then_stays_in_ip12_modules(self):
        first = self.message("生成选题计划", "topic_plan", {}, "topic-0001")
        self.assertEqual(first["message_public"]["kind"], "platform_picker")
        self.assertEqual(len([call for call in self.ip12.calls if call[0] == "turn"]), 0)
        second = self.message(
            "已选择：抖音、小红书", "set_platforms",
            {"platforms": ["douyin", "xiaohongshu"]}, "topic-0002",
        )
        self.assertEqual(second["workspace"]["flow"]["mode"], "topic_plan")
        self.assertEqual(second["message_public"]["kind"], "topic_plan")
        self.assertEqual(len([call for call in self.ip12.calls if call[0] == "turn"]), 1)
        paid = [call for call in self.bridge.calls if call[0] == "action" and call[1] == "matrix-template-generate"]
        self.assertEqual(paid, [])

    def test_copy_revision_returns_to_ip12_without_video_quote(self):
        self.ip12.value["coach_state"]["completed_modules"] = [1, 2, 3, 4, 5, 6]
        result = self.message(
            "修改第一篇文案的开头，让它更直接",
            suffix="copy-revise-0001",
        )
        self.assertEqual(result["message_public"]["kind"], "topic_plan")
        self.assertEqual(len([call for call in self.ip12.calls if call[0] == "turn"]), 1)
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
        with self.assertRaises(APIError) as raised:
            self.service.message(USER, self.headers, {
                "message": "制作视频", "request_id": "creator-turn-stuck-0001",
                "intent": "start_video", "payload": {"topic": "测试", "platforms": ["douyin"]},
            })
        self.assertEqual(raised.exception.code, "idempotency_in_progress")
        self.assertEqual(self.store.batches(USER["username"], PROJECT_ID), [])

    def test_same_request_id_replays_and_changed_payload_conflicts(self):
        body = {
            "message": "制作视频", "request_id": "creator-turn-hash-0001",
            "intent": "start_video",
            "payload": {"topic": "企业获客", "platforms": ["douyin"]},
        }
        first = self.service.message(USER, self.headers, body)
        second = self.service.message(USER, self.headers, copy.deepcopy(body))
        self.assertEqual(first, second)
        self.assertEqual(len(self.store.batches(USER["username"], PROJECT_ID)), 1)
        changed = copy.deepcopy(body)
        changed["payload"]["topic"] = "另一个主题"
        with self.assertRaises(APIError) as raised:
            self.service.message(USER, self.headers, changed)
        self.assertEqual(raised.exception.code, "idempotency_conflict")

    def test_ip12_production_handoff_is_not_exposed(self):
        actions = self.service._safe_ip12_actions([
            {"type": "prepare_production", "label": "开始制作口播视频"},
            {"type": "confirm_checkpoint", "target_id": "checkpoint-1", "label": "确认本模块"},
        ])
        self.assertEqual([item["type"] for item in actions], ["confirm_checkpoint"])
        cleaned = self.service._clean_ip12_action({
            "type": "confirm_checkpoint", "target_id": "checkpoint-1",
            "label": "确认本模块", "primary": True,
        })
        self.assertEqual(cleaned, {"type": "confirm_checkpoint", "target_id": "checkpoint-1"})

    def _draft_batch(self):
        result = self.message(
            "制作视频", "start_video",
            {"topic": "企业内容获客", "platforms": ["douyin", "wechat_channels"]},
            "3001",
        )
        return result["latest_batch"]

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

    def test_confirmed_profile_modification_is_versioned_without_rewriting_ip12(self):
        self.message("修改我的画像", "modify_profile", {}, "profile-0001")
        before_turns = len([call for call in self.ip12.calls if call[0] == "turn"])
        result = self.message(
            "目标客户改为准备数字化转型的制造企业",
            suffix="profile-0002",
        )
        overrides = result["workspace"]["profile_overrides"]
        self.assertIn("目标客户改为", overrides["module_1"][-1]["content"])
        self.assertEqual(len([call for call in self.ip12.calls if call[0] == "turn"]), before_turns)
        self.assertIn("原定位 PDF", result["reply"])

    def test_dirty_foundation_report_is_not_treated_as_ready(self):
        project = ready_project()
        project["coach_state"]["foundation_report"]["review_status"] = "dirty"
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
