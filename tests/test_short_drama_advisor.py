import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


SERVER_DIR = str(Path(__file__).resolve().parents[1] / "server")
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from content_domains import short_drama, short_drama_advisor


class Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")


class ShortDramaAdvisorTests(unittest.TestCase):
    def setUp(self):
        short_drama_advisor._reset_usage_for_tests()
        self.tempdir = tempfile.TemporaryDirectory(prefix="hq-advisor-")
        self.db_path = Path(self.tempdir.name) / "content.db"

        def db_factory():
            return sqlite3.connect(self.db_path, timeout=5)

        self.db = db_factory
        short_drama_advisor.init_db(self.db)
        self.claim_patch = mock.patch.object(
            short_drama_advisor.provider_keys, "claim_candidate",
            return_value={"id": "xai-key-1", "secret": "pool-secret"},
        )
        self.health_patch = mock.patch.object(
            short_drama_advisor.provider_keys, "set_health"
        )
        self.claim_candidate = self.claim_patch.start()
        self.set_health = self.health_patch.start()

    def tearDown(self):
        self.health_patch.stop()
        self.claim_patch.stop()
        self.tempdir.cleanup()

    def test_recommendation_context_is_cleaned_and_selection_is_canonical(self):
        cleaned = short_drama_advisor._clean_body({
            "messages": [],
            "understanding": {},
            "expected_field": "conflict",
            "recommendation_context": {
                "field": "conflict",
                "options": ["必须隐瞒真相", "关系即将破裂", "时间只剩一天", "不应保留"],
                "selected_index": "3",
                "selected_value": "伪造值",
            },
            "user_message": "我选择方向 3：时间只剩一天。",
        })
        self.assertEqual(cleaned["recommendation_context"], {
            "field": "conflict",
            "options": ["必须隐瞒真相", "关系即将破裂", "时间只剩一天"],
            "selected_index": 3,
            "selected_value": "时间只剩一天",
        })

    @staticmethod
    def _success_opener(counter=None):
        def opener(_request, timeout=0):
            if counter is not None:
                counter.append(timeout)
            content = {
                "intent": "answer", "reply": "收到", "confidence": .9,
                "field_updates": [], "missing_fields": [],
            }
            return Response({
                "choices": [{"message": {"content": json.dumps(content)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            })
        return opener

    def test_question_does_not_extract_business_fields(self):
        captured = {}

        def opener(request, timeout=0):
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            captured["timeout"] = timeout
            captured["authorization"] = request.get_header("Authorization")
            content = {
                "intent": "ask_recommendation",
                "reply": "可以，我给你三个冲突方向。",
                "extracted_fields": {},
                "missing_fields": ["conflict"],
                "confidence": 0.98,
                "quick_replies": ["关系即将破裂", "时间只剩一天"],
            }
            return Response({"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}]})

        with mock.patch.dict(os.environ, {
            "SHORT_DRAMA_ADVISOR_API_BASE": "https://advisor.example/v1",
        }, clear=False):
            result = short_drama_advisor.advise({
                "messages": ["青春期学生", "你觉得呢"],
                "understanding": {"protagonist": "青春期学生"},
                "expected_field": "conflict",
                "user_message": "你觉得呢",
            }, opener=opener)

        self.assertEqual(result["intent"], "ask_recommendation")
        self.assertEqual(result["extracted_fields"], {})
        self.assertEqual(result["missing_fields"], ["conflict"])
        self.assertEqual(captured["timeout"], 45)
        self.assertEqual(captured["payload"]["model"], "grok-3-mini")
        self.assertEqual(captured["payload"]["max_tokens"], 1200)
        self.assertEqual("Bearer pool-secret", captured["authorization"])

    def test_response_is_normalized_to_public_contract(self):
        result = short_drama_advisor._normalize({
            "intent": "ANSWER",
            "reply": "收到",
            "extracted_fields": {"conflict": "时间只剩一天", "admin": "secret"},
            "missing_fields": ["ending", "admin"],
            "confidence": 8,
            "quick_replies": ["一", "二", "三", "四", "五"],
        })
        self.assertEqual(result["intent"], "answer")
        self.assertEqual(result["extracted_fields"], {"conflict": "时间只剩一天"})
        self.assertEqual(result["missing_fields"], ["ending"])
        self.assertEqual(result["confidence"], 1.0)
        self.assertEqual(len(result["quick_replies"]), 4)
        self.assertEqual(result["field_updates"][0]["operation"], "set")
        self.assertEqual(result["field_updates"][0]["status"], "confirmed")
        self.assertEqual(result["next_action"], "continue")
        self.assertEqual(result["mode"], "ai")
        self.assertFalse(result["degraded"])

    def test_negation_and_clear_operation_are_preserved(self):
        result = short_drama_advisor._normalize({
            "intent": "negate",
            "reply": "已取消悬疑风格",
            "recap": "风格已清空",
            "field_updates": [{
                "field": "style", "operation": "clear", "value": "",
                "confidence": 0.92, "evidence": "不要悬疑",
            }],
            "confidence": 0.92,
        })
        self.assertEqual(result["intent"], "negate")
        self.assertEqual(result["field_updates"], [{
            "field": "style", "operation": "clear", "value": "",
            "confidence": 0.92, "evidence": "不要悬疑",
            "status": "removed",
        }])
        self.assertEqual(result["recap"], "风格已清空")

    def test_multiple_fields_evidence_and_low_confidence_status_are_preserved(self):
        result = short_drama_advisor._normalize({
            "intent": "answer",
            "reply": "我理解了大部分设定。",
            "field_updates": [
                {"field": "topic", "operation": "set", "value": "雨夜便利店", "confidence": .96, "evidence": "雨夜便利店的故事"},
                {"field": "protagonist", "operation": "set", "value": "刚失业的女性", "confidence": .93, "evidence": "女主刚失业"},
                {"field": "ending", "operation": "set", "value": "温暖", "confidence": .72, "evidence": "最后想温暖一点"},
            ],
            "focus_field": "conflict",
            "next_action": "ask",
            "confidence": .9,
        })
        self.assertEqual(len(result["field_updates"]), 3)
        self.assertEqual(result["field_updates"][0]["status"], "confirmed")
        self.assertEqual(result["field_updates"][2]["status"], "inferred")
        self.assertEqual(result["field_updates"][1]["evidence"], "女主刚失业")
        self.assertEqual(result["focus_field"], "conflict")

    def test_ambiguous_conflict_is_normalized_without_overwriting_reasoning(self):
        result = short_drama_advisor._normalize({
            "intent": "modify",
            "reply": "你希望保留哪一种情绪？",
            "field_updates": [{"field": "emotion", "operation": "set", "value": "温暖", "confidence": .7, "evidence": "也可以温暖"}],
            "conflicts": [{"field": "emotion", "proposed_value": "温暖", "reason": "没有明确表示替换", "requires_confirmation": True}],
            "next_action": "clarify",
        }, {"emotion": "紧张悬疑"})
        self.assertEqual(result["field_updates"][0]["status"], "conflicted")
        self.assertEqual(result["conflicts"][0]["existing_value"], "紧张悬疑")
        self.assertEqual(result["next_action"], "clarify")

    def test_missing_provider_is_explicit_and_route_is_allowlisted(self):
        with mock.patch.dict(os.environ, {
            "SHORT_DRAMA_ADVISOR_API_BASE": "",
            "XAI_API_BASE": "",
        }, clear=False):
            with self.assertRaises(short_drama_advisor.AdvisorError) as raised:
                short_drama_advisor.advise({"user_message": "你觉得呢"})
        self.assertEqual(raised.exception.status, 503)
        self.assertEqual(raised.exception.code, "advisor_provider_not_configured")
        self.assertIn("/api/gen/short-drama/advisor", short_drama._HTTP_ROUTES["POST"])

    def test_free_window_exhaustion_rejects_before_provider_call(self):
        calls = []
        with mock.patch.dict(os.environ, {
            "SHORT_DRAMA_ADVISOR_API_BASE": "https://advisor.example/v1",
            "SHORT_DRAMA_ADVISOR_REQUESTS_PER_WINDOW": "2",
        }, clear=False):
            for _index in range(2):
                short_drama_advisor.advise(
                    {"user_message": "继续完善故事"},
                    opener=self._success_opener(calls), username="alice",
                    db_factory=self.db,
                )
            with self.assertRaises(short_drama_advisor.AdvisorError) as raised:
                short_drama_advisor.advise(
                    {"user_message": "再次调用"},
                    opener=self._success_opener(calls), username="alice",
                    db_factory=self.db,
                )
        self.assertEqual(429, raised.exception.status)
        self.assertEqual("advisor_rate_limited", raised.exception.code)
        self.assertEqual(2, len(calls))

    def test_user_and_global_concurrency_reject_before_provider_call(self):
        started = threading.Event()
        release = threading.Event()
        provider_calls = []

        def blocking_opener(_request, timeout=0):
            provider_calls.append("alice")
            started.set()
            release.wait(5)
            return self._success_opener()(_request, timeout)

        errors = []
        with mock.patch.dict(os.environ, {
            "SHORT_DRAMA_ADVISOR_API_BASE": "https://advisor.example/v1",
            "SHORT_DRAMA_ADVISOR_REQUESTS_PER_WINDOW": "20",
            "SHORT_DRAMA_ADVISOR_USER_CONCURRENCY": "1",
            "SHORT_DRAMA_ADVISOR_GLOBAL_CONCURRENCY": "1",
        }, clear=False):
            thread = threading.Thread(target=lambda: short_drama_advisor.advise(
                {"user_message": "阻塞请求"}, opener=blocking_opener,
                username="alice", db_factory=self.db,
            ))
            thread.start()
            self.assertTrue(started.wait(2))
            with self.assertRaises(short_drama_advisor.AdvisorError) as same_user:
                short_drama_advisor.advise(
                    {"user_message": "并发请求"}, opener=self._success_opener(errors),
                    username="alice", db_factory=self.db,
                )
            with self.assertRaises(short_drama_advisor.AdvisorError) as global_limit:
                short_drama_advisor.advise(
                    {"user_message": "另一账号请求"}, opener=self._success_opener(errors),
                    username="bob", db_factory=self.db,
                )
            release.set()
            thread.join(5)
            result = short_drama_advisor.advise(
                {"user_message": "槽位释放后继续"}, opener=self._success_opener(errors),
                username="bob", db_factory=self.db,
            )
        self.assertEqual("advisor_user_busy", same_user.exception.code)
        self.assertEqual("advisor_capacity_reached", global_limit.exception.code)
        self.assertEqual("answer", result["intent"])
        self.assertEqual(["alice"], provider_calls)
        self.assertEqual(1, len(errors))

    def test_provider_failure_releases_concurrency_slot(self):
        calls = []

        def failed_opener(_request, timeout=0):
            calls.append("failed")
            raise OSError("provider unavailable")

        with mock.patch.dict(os.environ, {
            "SHORT_DRAMA_ADVISOR_API_BASE": "https://advisor.example/v1",
            "SHORT_DRAMA_ADVISOR_REQUESTS_PER_WINDOW": "20",
            "SHORT_DRAMA_ADVISOR_USER_CONCURRENCY": "1",
            "SHORT_DRAMA_ADVISOR_GLOBAL_CONCURRENCY": "1",
        }, clear=False):
            with self.assertRaises(short_drama_advisor.AdvisorError):
                short_drama_advisor.advise(
                    {"user_message": "第一次失败"}, opener=failed_opener,
                    username="alice", db_factory=self.db,
                )
            result = short_drama_advisor.advise(
                {"user_message": "失败后重试"}, opener=self._success_opener(calls),
                username="alice", db_factory=self.db,
            )
        self.assertEqual("answer", result["intent"])
        self.assertEqual(2, len(calls))

    def test_pool_key_works_when_legacy_environment_keys_are_empty(self):
        with mock.patch.dict(os.environ, {
            "SHORT_DRAMA_ADVISOR_API_BASE": "https://advisor.example/v1",
            "SHORT_DRAMA_ADVISOR_API_KEY": "",
            "XAI_API_KEY": "",
        }, clear=False):
            result = short_drama_advisor.advise(
                {"user_message": "继续完善故事"},
                opener=self._success_opener(), username="alice",
                db_factory=self.db,
            )
        self.assertEqual("answer", result["intent"])
        self.claim_candidate.assert_called_with("xai")

    def test_auth_failure_marks_key_unhealthy_and_switches_candidate(self):
        self.claim_candidate.side_effect = [
            {"id": "xai-key-1", "secret": "expired-secret"},
            {"id": "xai-key-2", "secret": "healthy-secret"},
        ]
        authorizations = []

        def opener(request, timeout=0):
            authorizations.append(request.get_header("Authorization"))
            if len(authorizations) == 1:
                raise urllib.error.HTTPError(
                    request.full_url, 401, "Unauthorized", {}, io.BytesIO(b"{}")
                )
            return self._success_opener()(request, timeout)

        with mock.patch.dict(os.environ, {
            "SHORT_DRAMA_ADVISOR_API_BASE": "https://advisor.example/v1",
        }, clear=False):
            result = short_drama_advisor.advise(
                {"user_message": "给我三个方向"}, opener=opener,
                username="alice", db_factory=self.db,
            )

        self.assertEqual("answer", result["intent"])
        self.assertEqual(
            ["Bearer expired-secret", "Bearer healthy-secret"], authorizations
        )
        self.assertEqual(2, self.claim_candidate.call_count)
        self.assertEqual("xai-key-1", self.set_health.call_args_list[0].args[0])
        self.assertFalse(self.set_health.call_args_list[0].args[1])
        self.assertEqual("xai-key-2", self.set_health.call_args_list[1].args[0])
        self.assertTrue(self.set_health.call_args_list[1].args[1])

    def test_ambiguous_network_failure_does_not_switch_key(self):
        def opener(_request, timeout=0):
            raise OSError("connection reset after request")

        with mock.patch.dict(os.environ, {
            "SHORT_DRAMA_ADVISOR_API_BASE": "https://advisor.example/v1",
        }, clear=False):
            with self.assertRaises(short_drama_advisor.AdvisorError):
                short_drama_advisor.advise(
                    {"user_message": "继续"}, opener=opener,
                    username="alice", db_factory=self.db,
                )
        self.assertEqual(1, self.claim_candidate.call_count)
        self.set_health.assert_not_called()

    def test_egress_opener_covers_primary_fallback_and_no_proxy(self):
        for proxy in (
            "http://127.0.0.1:10809",
            "http://127.0.0.1:7897",
            "",
        ):
            built = mock.Mock()
            with self.subTest(proxy=proxy or "direct"), \
                    mock.patch.object(
                        short_drama_advisor.egress, "preferred_proxy",
                        return_value=proxy,
                    ), mock.patch.object(
                        short_drama_advisor.urllib.request, "build_opener",
                        return_value=built,
                    ) as build_opener:
                request_open = short_drama_advisor._provider_opener()
                self.assertIs(request_open, built.open)
                if proxy:
                    handler = build_opener.call_args.args[0]
                    self.assertEqual(proxy, handler.proxies["https"])
                else:
                    build_opener.assert_called_once_with()

    def test_persisted_window_survives_runtime_reset(self):
        with mock.patch.dict(os.environ, {
            "SHORT_DRAMA_ADVISOR_API_BASE": "https://advisor.example/v1",
            "SHORT_DRAMA_ADVISOR_REQUESTS_PER_WINDOW": "1",
        }, clear=False):
            short_drama_advisor.advise(
                {"user_message": "第一次"}, opener=self._success_opener(),
                username="alice", db_factory=self.db,
            )
            short_drama_advisor._reset_usage_for_tests()
            with self.assertRaises(short_drama_advisor.AdvisorError) as raised:
                short_drama_advisor.advise(
                    {"user_message": "重启后第二次"}, opener=self._success_opener(),
                    username="alice", db_factory=self.db,
                )
        self.assertEqual("advisor_rate_limited", raised.exception.code)

    def test_global_budget_exhaustion_blocks_before_provider_call(self):
        calls = []
        with mock.patch.dict(os.environ, {
            "SHORT_DRAMA_ADVISOR_API_BASE": "https://advisor.example/v1",
            "SHORT_DRAMA_ADVISOR_GLOBAL_DAILY_BUDGET_MICROUSD": "5400",
        }, clear=False):
            short_drama_advisor.advise(
                {"user_message": "第一次"}, opener=self._success_opener(calls),
                username="alice", db_factory=self.db,
            )
            with self.assertRaises(short_drama_advisor.AdvisorError) as raised:
                short_drama_advisor.advise(
                    {"user_message": "另一账号"}, opener=self._success_opener(calls),
                    username="bob", db_factory=self.db,
                )
        self.assertEqual("advisor_global_budget_exhausted", raised.exception.code)
        self.assertEqual(1, len(calls))

    def test_daily_user_quota_survives_runtime_reset(self):
        with mock.patch.dict(os.environ, {
            "SHORT_DRAMA_ADVISOR_API_BASE": "https://advisor.example/v1",
            "SHORT_DRAMA_ADVISOR_REQUESTS_PER_WINDOW": "20",
            "SHORT_DRAMA_ADVISOR_USER_DAILY_REQUESTS": "1",
        }, clear=False):
            short_drama_advisor.advise(
                {"user_message": "第一次"}, opener=self._success_opener(),
                username="alice", db_factory=self.db,
            )
            short_drama_advisor._reset_usage_for_tests()
            with self.assertRaises(short_drama_advisor.AdvisorError) as raised:
                short_drama_advisor.advise(
                    {"user_message": "今天再次调用"}, opener=self._success_opener(),
                    username="alice", db_factory=self.db,
                )
        self.assertEqual("advisor_daily_quota_exhausted", raised.exception.code)

    def test_global_budget_reservation_is_atomic_across_threads(self):
        start = threading.Barrier(3)
        results = []
        provider_calls = []

        def run(username):
            start.wait(3)
            try:
                short_drama_advisor.advise(
                    {"user_message": username},
                    opener=self._success_opener(provider_calls),
                    username=username, db_factory=self.db,
                )
                results.append("ok")
            except short_drama_advisor.AdvisorError as error:
                results.append(error.code)

        with mock.patch.dict(os.environ, {
            "SHORT_DRAMA_ADVISOR_API_BASE": "https://advisor.example/v1",
            "SHORT_DRAMA_ADVISOR_GLOBAL_DAILY_BUDGET_MICROUSD": "5400",
            "SHORT_DRAMA_ADVISOR_GLOBAL_CONCURRENCY": "2",
        }, clear=False), mock.patch.object(
            short_drama_advisor, "_finalize_usage"
        ):
            threads = [
                threading.Thread(target=run, args=(username,))
                for username in ("alice", "bob")
            ]
            for thread in threads:
                thread.start()
            start.wait(3)
            for thread in threads:
                thread.join(5)

        self.assertCountEqual(
            ["ok", "advisor_global_budget_exhausted"], results
        )
        self.assertEqual(1, len(provider_calls))

    def test_provider_limits_are_clamped_and_reserve_covers_maximum(self):
        with mock.patch.dict(os.environ, {
            "SHORT_DRAMA_ADVISOR_API_BASE": "https://advisor.example/v1",
            "SHORT_DRAMA_ADVISOR_MAX_TOKENS": "999999",
        }, clear=False):
            prepared = short_drama_advisor._prepare_provider_request({
                "user_message": "继续",
            })
        payload = json.loads(prepared["payload"].decode("utf-8"))
        maximum = short_drama_advisor._token_cost(
            "grok-3-mini",
            short_drama_advisor._MAX_INPUT_TOKENS,
            short_drama_advisor._MAX_OUTPUT_TOKENS,
        )
        self.assertEqual(1200, payload["max_tokens"])
        self.assertEqual(5400, prepared["reserve_microusd"])
        self.assertEqual(maximum, prepared["reserve_microusd"])

    def test_successful_request_settles_to_actual_model_cost(self):
        with mock.patch.dict(os.environ, {
            "SHORT_DRAMA_ADVISOR_API_BASE": "https://advisor.example/v1",
        }, clear=False):
            short_drama_advisor.advise(
                {"user_message": "继续"}, opener=self._success_opener(),
                username="alice", db_factory=self.db,
            )
        conn = self.db()
        try:
            row = conn.execute(
                "SELECT reserved_microusd,prompt_tokens,completion_tokens,status "
                "FROM short_drama_advisor_usage"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual((40, 100, 20, "succeeded"), row)

    def test_missing_provider_usage_keeps_conservative_reserve(self):
        def opener(_request, timeout=0):
            content = {
                "intent": "answer", "reply": "收到", "confidence": .9,
                "field_updates": [], "missing_fields": [],
            }
            return Response({
                "choices": [{"message": {"content": json.dumps(content)}}],
            })

        with mock.patch.dict(os.environ, {
            "SHORT_DRAMA_ADVISOR_API_BASE": "https://advisor.example/v1",
        }, clear=False):
            short_drama_advisor.advise(
                {"user_message": "继续"}, opener=opener,
                username="alice", db_factory=self.db,
            )
        conn = self.db()
        try:
            row = conn.execute(
                "SELECT reserved_microusd,status FROM short_drama_advisor_usage"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual((5400, "succeeded"), row)

    def test_daily_quota_resets_at_shanghai_midnight(self):
        before = datetime(2026, 8, 5, 15, 59, 59, tzinfo=timezone.utc).timestamp()
        after = datetime(2026, 8, 5, 16, 0, 0, tzinfo=timezone.utc).timestamp()
        with mock.patch.dict(os.environ, {
            "SHORT_DRAMA_ADVISOR_USER_DAILY_REQUESTS": "1",
            "SHORT_DRAMA_ADVISOR_REQUESTS_PER_WINDOW": "20",
        }, clear=False):
            first = short_drama_advisor._acquire_usage(
                "alice", self.db, "before", 5400, "grok-3-mini", now=before
            )
            short_drama_advisor._finalize_usage(
                first, "succeeded", {"prompt_tokens": 1, "completion_tokens": 1}
            )
            short_drama_advisor._release_usage(first)
            second = short_drama_advisor._acquire_usage(
                "alice", self.db, "after", 5400, "grok-3-mini", now=after
            )
            short_drama_advisor._release_usage(second)

    def test_oversized_input_is_rejected_before_provider_claim(self):
        with mock.patch.dict(os.environ, {
            "SHORT_DRAMA_ADVISOR_API_BASE": "https://advisor.example/v1",
        }, clear=False), self.assertRaises(short_drama_advisor.AdvisorError) as raised:
            short_drama_advisor.advise({
                "messages": ["剧" * 600 for _ in range(20)],
                "user_message": "继续",
            }, opener=self._success_opener(), username="alice", db_factory=self.db)
        self.assertEqual("advisor_input_too_large", raised.exception.code)
        self.claim_candidate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
