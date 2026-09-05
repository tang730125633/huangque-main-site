# -*- coding: utf-8 -*-
import hashlib
import json
import sqlite3
import sys
import unittest
import uuid
from contextlib import closing
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVER_DIR = str(ROOT / "server")
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from content_domains import video_agent_tools


class VideoAgentToolTests(unittest.TestCase):
    def setUp(self):
        self.db_path = ROOT / (".video-agent-tools-test-%s.sqlite3" % uuid.uuid4().hex)

        def db_factory():
            conn = sqlite3.connect(self.db_path, timeout=5)
            conn.row_factory = sqlite3.Row
            return conn

        self.db = db_factory
        self.calls = []
        self.claims_calls = []

        def cli_execute(capability, input_body, **kwargs):
            self.calls.append((capability, dict(input_body), dict(kwargs)))
            if kwargs.get("confirm"):
                return {"job_id": 321, "status": "pending"}
            if capability.endswith("generate"):
                # 模拟鉴权服务的标准化：补默认值，回传被签名的 payload。
                payload = dict(input_body)
                if capability == "cinematic-open-generate":
                    payload.setdefault("resolution", "720p")
                    payload.setdefault("ratio", "9:16")
                    payload.setdefault("duration", 10)
                    payload.setdefault("enhance_prompt", False)
                if capability == "video-generate":
                    payload.setdefault("channel", "grok")
                    payload.setdefault("model", "grok-imagine-video")
                    payload.setdefault("ratio", "16:9")
                    payload.setdefault("duration", 10)
                    payload.setdefault("resolution", "720p")
                payload_hash = hashlib.sha256(
                    json.dumps(payload, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":")).encode("utf-8")
                ).hexdigest()
                return {
                    "quote_token": "server-only-quote-token",
                    "fingerprint": "cinematic:" + payload_hash,
                    "kind": "cinematic", "cost": 90, "points": 1000,
                    "expires_in": 120, "confirmation_required": True,
                    "payload": payload,
                }
            return {"items": [{"display_name": "公共音色 1"}], "total": 1}

        self.cli_execute = cli_execute

        def quote_claims(quote_token):
            self.claims_calls.append(quote_token)
            return {
                "nonce": "f" * 32, "kind": "cinematic", "cost": 90,
                "expires_at": 2000, "username": "alice",
                "payload_hash": "a" * 64,
            }

        self.quote_claims = quote_claims

    def tearDown(self):
        self.db_path.unlink(missing_ok=True)

    def test_tool_catalog_has_only_curated_read_and_quote_functions(self):
        names = {item["name"] for item in video_agent_tools.TOOL_DEFINITIONS}
        self.assertIn("hq_get_account", names)
        self.assertIn("hq_quote_story_video", names)
        self.assertIn("hq_quote_tryon_fast_video", names)
        self.assertIn("hq_quote_tryon_classic_video", names)
        self.assertNotIn("hq_run", names)
        self.assertNotIn("shell", names)
        self.assertNotIn("confirm", names)
        self.assertTrue(all(item["type"] == "function" for item in video_agent_tools.TOOL_DEFINITIONS))

    def test_tryon_quote_tools_are_strict_and_quote_only(self):
        runtime = video_agent_tools.VideoAgentToolRuntime(
            username="alice", web_token="web-token", db_factory=self.db,
            cli_execute=self.cli_execute, now=lambda: 1000,
        )
        image_id = "img_" + "a" * 32
        video_id = "vid_" + "b" * 32
        fast = runtime.run("hq_quote_tryon_fast_video", json.dumps({
            "person_image_upload_id": image_id,
            "clothes_upload_id": "img_" + "c" * 32,
            "seconds": 8,
        }))
        classic = runtime.run("hq_quote_tryon_classic_video", json.dumps({
            "person_video_upload_id": video_id,
            "background_upload_id": "img_" + "d" * 32,
            "seconds": 4,
        }))
        self.assertEqual(fast["pending_action"]["capability"], "tryon-fast-generate")
        self.assertEqual(classic["pending_action"]["capability"], "tryon-classic-generate")
        self.assertTrue(all(not call[2].get("confirm", False) for call in self.calls))
        with self.assertRaises(video_agent_tools.ToolError):
            runtime.run("hq_quote_tryon_classic_video", json.dumps({
                "person_video_upload_id": video_id,
            }))

    def test_quote_tool_only_creates_server_pending_action(self):
        runtime = video_agent_tools.VideoAgentToolRuntime(
            username="alice", web_token="web-token", db_factory=self.db,
            cli_execute=self.cli_execute, now=lambda: 1000,
        )
        result = runtime.run("hq_quote_story_video", json.dumps({
            "avatar_id": 7, "prompt": "雨夜重逢", "duration": 8, "ratio": "9:16",
        }, ensure_ascii=False))
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("server-only-quote-token", serialized)
        self.assertEqual(result["pending_action"]["status"], "awaiting_confirmation")
        self.assertEqual(result["pending_action"]["cost"], 90)
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(self.calls[0][2].get("confirm", False))
        with closing(self.db()) as conn:
            row = conn.execute("SELECT quote_token,status FROM video_agent_pending_actions").fetchone()
        self.assertEqual(row["quote_token"], "server-only-quote-token")
        self.assertEqual(row["status"], "awaiting_confirmation")

    def test_talking_quote_accepts_verified_private_image_upload_route(self):
        runtime = video_agent_tools.VideoAgentToolRuntime(
            username="alice", web_token="web-token", db_factory=self.db,
            cli_execute=self.cli_execute, now=lambda: 1000,
        )
        image_upload_id = "img_" + "a" * 32

        result = runtime.run("hq_quote_talking_video", json.dumps({
            "image_upload_id": image_upload_id,
            "text": "介绍新品",
            "voice": "voice-1",
        }, ensure_ascii=False))

        self.assertEqual(result["pending_action"]["status"], "awaiting_confirmation")
        self.assertEqual(self.calls[0][0], "digital-ip-text-generate")
        self.assertEqual(self.calls[0][1]["image_upload_id"], image_upload_id)
        self.assertNotIn("avatar_id", self.calls[0][1])
        for invalid_materials in ({}, {"avatar_id": 7, "image_upload_id": image_upload_id}):
            with self.subTest(invalid_materials=invalid_materials):
                with self.assertRaises(video_agent_tools.ToolError):
                    runtime.run("hq_quote_talking_video", json.dumps({
                        **invalid_materials,
                        "text": "介绍新品",
                        "voice": "voice-1",
                    }, ensure_ascii=False))

    def test_quote_without_normalized_fingerprint_fails_closed(self):
        def unsafe_quote(capability, input_body, **kwargs):
            return {
                "quote_token": "server-only-quote-token", "cost": 90,
                "expires_in": 120, "confirmation_required": True,
            }

        runtime = video_agent_tools.VideoAgentToolRuntime(
            username="alice", web_token="web-token", db_factory=self.db,
            cli_execute=unsafe_quote, now=lambda: 1000,
        )
        with self.assertRaises(video_agent_tools.ToolError) as error:
            runtime.run("hq_quote_story_video", json.dumps({
                "avatar_id": 7, "prompt": "雨夜重逢",
            }, ensure_ascii=False))
        self.assertEqual(error.exception.code, "quote_response_invalid")
        with closing(self.db()) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM video_agent_pending_actions"
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_quote_without_signed_payload_fails_closed(self):
        def bare_quote(capability, input_body, **kwargs):
            payload = dict(input_body)
            payload_hash = hashlib.sha256(
                json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            return {
                "quote_token": "server-only-quote-token", "cost": 90,
                "fingerprint": "cinematic:" + payload_hash,
                "expires_in": 120, "confirmation_required": True,
            }

        runtime = video_agent_tools.VideoAgentToolRuntime(
            username="alice", web_token="web-token", db_factory=self.db,
            cli_execute=bare_quote, now=lambda: 1000,
        )
        with self.assertRaises(video_agent_tools.ToolError) as error:
            runtime.run("hq_quote_story_video", json.dumps({
                "avatar_id": 7, "prompt": "雨夜重逢",
            }, ensure_ascii=False))
        self.assertEqual(error.exception.code, "quote_response_invalid")

    def test_quote_with_payload_mismatching_fingerprint_fails_closed(self):
        def mismatched_quote(capability, input_body, **kwargs):
            return {
                "quote_token": "server-only-quote-token", "cost": 90,
                "fingerprint": "cinematic:" + "b" * 64,
                "expires_in": 120, "confirmation_required": True,
                "payload": dict(input_body),
            }

        runtime = video_agent_tools.VideoAgentToolRuntime(
            username="alice", web_token="web-token", db_factory=self.db,
            cli_execute=mismatched_quote, now=lambda: 1000,
        )
        with self.assertRaises(video_agent_tools.ToolError) as error:
            runtime.run("hq_quote_story_video", json.dumps({
                "avatar_id": 7, "prompt": "雨夜重逢",
            }, ensure_ascii=False))
        self.assertEqual(error.exception.code, "quote_response_invalid")

    def test_confirmation_is_explicit_idempotent_and_reuses_identical_input(self):
        runtime = video_agent_tools.VideoAgentToolRuntime(
            username="alice", web_token="web-token", db_factory=self.db,
            cli_execute=self.cli_execute, now=lambda: 1000,
        )
        quote = runtime.run("hq_quote_story_video", json.dumps({
            "avatar_id": 7, "prompt": "雨夜重逢", "duration": 8, "ratio": "9:16",
        }, ensure_ascii=False))
        pending_id = quote["pending_action"]["id"]
        first = video_agent_tools.confirm_pending_action(
            pending_id, "request-12345678", username="alice", web_token="web-token",
            db_factory=self.db, cli_execute=self.cli_execute, now=lambda: 1001,
            quote_claims=self.quote_claims,
        )
        second = video_agent_tools.confirm_pending_action(
            pending_id, "request-12345678", username="alice", web_token="web-token",
            db_factory=self.db, cli_execute=self.cli_execute, now=lambda: 1002,
            quote_claims=self.quote_claims,
        )
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "submitted")
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(self.calls[1][2]["confirm"])
        self.assertEqual(self.calls[0][1], self.calls[1][1])
        self.assertEqual(self.calls[1][2]["quote_token"], "server-only-quote-token")
        with closing(self.db()) as conn:
            stored_token = conn.execute(
                "SELECT quote_token FROM video_agent_pending_actions WHERE id=?",
                (pending_id,),
            ).fetchone()[0]
        self.assertEqual(stored_token, "")

    def test_confirmation_result_exposes_only_safe_task_fields(self):
        def exposed_confirm(capability, input_body, **kwargs):
            if kwargs.get("confirm"):
                return {
                    "job_id": 321,
                    "cost": 8,
                    "points_left": 992,
                    "video_url": "https://signed.example/private.mp4",
                    "access_token": "secret-token",
                    "provider": {"task_id": "upstream-private"},
                }
            return self.cli_execute(capability, input_body, **kwargs)

        runtime = video_agent_tools.VideoAgentToolRuntime(
            username="alice", web_token="web-token", db_factory=self.db,
            cli_execute=exposed_confirm, now=lambda: 1000,
        )
        quote = runtime.run("hq_quote_talking_video", json.dumps({
            "avatar_id": 7, "text": "欢迎来到直播间", "voice": "voice-1",
        }, ensure_ascii=False))
        confirmed = video_agent_tools.confirm_pending_action(
            quote["pending_action"]["id"], "request-12345678",
            username="alice", web_token="web-token", db_factory=self.db,
            cli_execute=exposed_confirm, now=lambda: 1001,
            quote_claims=self.quote_claims,
        )
        self.assertEqual(confirmed["result"], {
            "job_id": 321, "cost": 8, "points_left": 992,
        })
        serialized = json.dumps(confirmed, ensure_ascii=False)
        self.assertNotIn("signed.example", serialized)
        self.assertNotIn("secret-token", serialized)
        self.assertNotIn("upstream-private", serialized)

    def test_unknown_or_extra_arguments_fail_before_cli(self):
        runtime = video_agent_tools.VideoAgentToolRuntime(
            username="alice", web_token="web-token", db_factory=self.db,
            cli_execute=self.cli_execute,
        )
        with self.assertRaises(video_agent_tools.ToolError) as error:
            runtime.run("hq_quote_story_video", '{"prompt":"ok","command":"rm"}')
        self.assertEqual(error.exception.code, "tool_arguments_invalid")
        self.assertEqual(self.calls, [])
        with self.assertRaises(video_agent_tools.ToolError):
            runtime.run("hq_run", "{}")

    def test_read_tool_returns_bounded_sanitized_result(self):
        runtime = video_agent_tools.VideoAgentToolRuntime(
            username="alice", web_token="web-token", db_factory=self.db,
            cli_execute=self.cli_execute,
        )
        result = runtime.run("hq_list_voices", "{}")
        self.assertTrue(result["ok"])
        self.assertIn("公共音色", json.dumps(result, ensure_ascii=False))
        self.assertEqual(self.calls[0][0], "voices")

    def test_local_read_fallback_handles_missing_delegate_endpoint(self):
        calls = []

        def missing_delegate(*_args, **_kwargs):
            raise video_agent_tools.hq_cli_executor.CLIExecutionError(
                "cli_auth_failed", "无法为当前账号取得临时 CLI 授权", 404,
            )

        def local_voices(arguments):
            calls.append(arguments)
            return {
                "items": [{
                    "id": 7, "scope": "public", "voice_key": "voice-1",
                    "display_name": "本地公共音色", "provider_voice": "secret",
                }],
                "total": 1,
            }

        runtime = video_agent_tools.VideoAgentToolRuntime(
            username="alice", web_token="web-token", db_factory=self.db,
            cli_execute=missing_delegate,
            read_fallbacks={"hq_list_voices": local_voices},
        )
        result = runtime.run("hq_list_voices", "{}")
        self.assertEqual(calls, [{}])
        self.assertTrue(result["ok"])
        self.assertIn("本地公共音色", json.dumps(result, ensure_ascii=False))
        self.assertNotIn("secret", json.dumps(result, ensure_ascii=False))
        self.assertEqual(runtime.activity[-1]["fallback"], "local_read")

    def test_local_read_fallback_never_handles_quote_or_non_404_failure(self):
        fallback_calls = []

        def unavailable(*_args, **_kwargs):
            raise video_agent_tools.hq_cli_executor.CLIExecutionError(
                "cli_auth_unavailable", "CLI 鉴权服务暂时不可用", 503,
            )

        runtime = video_agent_tools.VideoAgentToolRuntime(
            username="alice", web_token="web-token", db_factory=self.db,
            cli_execute=unavailable,
            read_fallbacks={
                "hq_list_voices": lambda _arguments: fallback_calls.append("voices"),
                "hq_quote_talking_video": lambda _arguments: fallback_calls.append("quote"),
            },
        )
        with self.assertRaises(video_agent_tools.ToolError) as read_error:
            runtime.run("hq_list_voices", "{}")
        self.assertEqual(read_error.exception.code, "cli_auth_unavailable")
        with self.assertRaises(video_agent_tools.ToolError) as quote_error:
            runtime.run("hq_quote_talking_video", json.dumps({
                "avatar_id": 7, "text": "测试文案", "voice": "voice-1",
            }, ensure_ascii=False))
        self.assertEqual(quote_error.exception.code, "cli_auth_unavailable")
        self.assertEqual(fallback_calls, [])

    def test_read_tool_uses_capability_allowlist_before_model_boundary(self):
        def exposed_cli(capability, input_body, **kwargs):
            return {
                "items": [{
                    "id": 7, "name": "可用素材", "status": "ready",
                    "prompt": "private prompt", "url": "https://signed.example/secret",
                    "file": "C:/private/input.png", "provider_asset_id": "upstream-1",
                    "access_token": "secret-token",
                }],
                "total": 1,
                "debug": {"request": "internal"},
            }

        runtime = video_agent_tools.VideoAgentToolRuntime(
            username="alice", web_token="web-token", db_factory=self.db,
            cli_execute=exposed_cli,
        )
        result = runtime.run("hq_list_assets", '{"kind":"image"}')
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertIn("可用素材", serialized)
        for forbidden in (
                "private prompt", "signed.example", "C:/private", "upstream-1",
                "secret-token", "debug"):
            self.assertNotIn(forbidden, serialized)

    def test_identical_live_quote_is_reused_without_second_cli_call(self):
        runtime = video_agent_tools.VideoAgentToolRuntime(
            username="alice", web_token="web-token", db_factory=self.db,
            cli_execute=self.cli_execute, now=lambda: 1000,
        )
        arguments = json.dumps({
            "avatar_id": 7, "prompt": "雨夜重逢", "duration": 8, "ratio": "9:16",
        }, ensure_ascii=False)
        first = runtime.run("hq_quote_story_video", arguments)
        second = runtime.run("hq_quote_story_video", arguments)
        self.assertEqual(first["pending_action"]["id"], second["pending_action"]["id"])
        self.assertTrue(second["reused"])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(runtime.pending_actions), 1)
        with closing(self.db()) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM video_agent_pending_actions"
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_tool_timeout_is_bounded_and_forwarded_to_cli(self):
        runtime = video_agent_tools.VideoAgentToolRuntime(
            username="alice", web_token="web-token", db_factory=self.db,
            cli_execute=self.cli_execute,
        )
        runtime.run("hq_list_voices", "{}", timeout_seconds=90)
        self.assertEqual(self.calls[0][2]["timeout"], 35)

    def test_pending_action_is_bound_to_owner(self):
        runtime = video_agent_tools.VideoAgentToolRuntime(
            username="alice", web_token="web-token", db_factory=self.db,
            cli_execute=self.cli_execute, now=lambda: 1000,
        )
        quote = runtime.run("hq_quote_story_video", json.dumps({
            "avatar_id": 7, "prompt": "雨夜重逢",
        }, ensure_ascii=False))
        with self.assertRaises(video_agent_tools.ToolError) as error:
            video_agent_tools.confirm_pending_action(
                quote["pending_action"]["id"], "request-12345678",
                username="bob", web_token="web-token", db_factory=self.db,
                cli_execute=self.cli_execute, now=lambda: 1001,
                quote_claims=self.quote_claims,
            )
        self.assertEqual(error.exception.code, "pending_action_not_found")
        self.assertEqual(len(self.calls), 1)

    def test_expired_quote_is_terminal_and_token_is_erased(self):
        runtime = video_agent_tools.VideoAgentToolRuntime(
            username="alice", web_token="web-token", db_factory=self.db,
            cli_execute=self.cli_execute, now=lambda: 1000,
        )
        quote = runtime.run("hq_quote_story_video", json.dumps({
            "avatar_id": 7, "prompt": "雨夜重逢",
        }, ensure_ascii=False))
        pending_id = quote["pending_action"]["id"]
        with self.assertRaises(video_agent_tools.ToolError) as error:
            video_agent_tools.confirm_pending_action(
                pending_id, "request-12345678", username="alice", web_token="web-token",
                db_factory=self.db, cli_execute=self.cli_execute, now=lambda: 1201,
                quote_claims=self.quote_claims,
            )
        self.assertEqual(error.exception.code, "pending_action_expired")
        with closing(self.db()) as conn:
            row = conn.execute(
                "SELECT status,quote_token FROM video_agent_pending_actions WHERE id=?",
                (pending_id,),
            ).fetchone()
        self.assertEqual(row["status"], "expired")
        self.assertEqual(row["quote_token"], "")

    def test_unknown_confirmation_outcome_is_terminal_and_not_retried(self):
        runtime = video_agent_tools.VideoAgentToolRuntime(
            username="alice", web_token="web-token", db_factory=self.db,
            cli_execute=self.cli_execute, now=lambda: 1000,
        )
        arguments = json.dumps({
            "avatar_id": 7, "prompt": "雨夜重逢",
        }, ensure_ascii=False)
        quote = runtime.run("hq_quote_story_video", arguments)
        pending_id = quote["pending_action"]["id"]
        attempts = []

        def unknown(*args, **kwargs):
            attempts.append(1)
            raise video_agent_tools.hq_cli_executor.CLIExecutionError(
                "cli_timeout", "结果未知", 504, unknown_outcome=True,
            )

        with self.assertRaises(video_agent_tools.ToolError) as first:
            video_agent_tools.confirm_pending_action(
                pending_id, "request-12345678", username="alice", web_token="web-token",
                db_factory=self.db, cli_execute=unknown, now=lambda: 1001,
                quote_claims=self.quote_claims,
            )
        self.assertTrue(first.exception.unknown_outcome)
        self.assertEqual(first.exception.pending_action["status"], "result_unknown")
        with closing(self.db()) as conn:
            stored_token = conn.execute(
                "SELECT quote_token FROM video_agent_pending_actions WHERE id=?",
                (pending_id,),
            ).fetchone()[0]
        self.assertEqual(stored_token, "")
        with self.assertRaises(video_agent_tools.ToolError) as second:
            video_agent_tools.confirm_pending_action(
                pending_id, "request-12345678", username="alice", web_token="web-token",
                db_factory=self.db, cli_execute=unknown, now=lambda: 1002,
                quote_claims=self.quote_claims,
            )
        self.assertEqual(second.exception.code, "pending_action_unavailable")
        self.assertEqual(len(attempts), 1)
        # A new conversation with the same input must not mint a fresh quote:
        # the earlier submit may already have succeeded upstream.
        fresh_runtime = video_agent_tools.VideoAgentToolRuntime(
            username="alice", web_token="web-token", db_factory=self.db,
            cli_execute=self.cli_execute, now=lambda: 1003,
        )
        blocked = fresh_runtime.run("hq_quote_story_video", arguments)
        self.assertTrue(blocked["result_unknown"])
        self.assertFalse(blocked["confirmation_required"])
        self.assertEqual(blocked["pending_action"]["id"], pending_id)
        self.assertEqual(len(self.calls), 1)
        semantic_variant = json.dumps({
            "avatar_ids": [7], "prompt": "雨夜重逢", "ratio": "9:16",
            "duration": 10, "enhance_prompt": False,
        }, ensure_ascii=False)
        normalized_fresh = fresh_runtime.run(
            "hq_quote_story_video", semantic_variant
        )
        # 语义变体是另一份标准化输入（payload 哈希不同）：允许重新报价。
        # 内容侧幂等按 payload 绑定，不会与结果未知的那次提交冲突。
        self.assertTrue(normalized_fresh["confirmation_required"])
        self.assertNotEqual(
            normalized_fresh["pending_action"]["id"], pending_id
        )
        self.assertEqual(len(self.calls), 2)

    def test_startup_recovery_never_reopens_interrupted_confirmation(self):
        runtime = video_agent_tools.VideoAgentToolRuntime(
            username="alice", web_token="web-token", db_factory=self.db,
            cli_execute=self.cli_execute, now=lambda: 1000,
        )
        quote = runtime.run("hq_quote_story_video", json.dumps({
            "avatar_id": 7, "prompt": "雨夜重逢",
        }, ensure_ascii=False))
        pending_id = quote["pending_action"]["id"]
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE video_agent_pending_actions SET status='confirming' WHERE id=?",
                (pending_id,),
            )
            conn.commit()
        video_agent_tools.ensure_tables(self.db, recover_confirming=True)
        with closing(self.db()) as conn:
            row = conn.execute(
                "SELECT status,error_code,quote_token "
                "FROM video_agent_pending_actions WHERE id=?",
                (pending_id,),
            ).fetchone()
        self.assertEqual(row["status"], "result_unknown")
        self.assertEqual(row["error_code"], "interrupted_confirmation")
        self.assertEqual(row["quote_token"], "")

    def test_old_live_index_migrates_result_unknown_into_duplicate_barrier(self):
        runtime = video_agent_tools.VideoAgentToolRuntime(
            username="alice", web_token="web-token", db_factory=self.db,
            cli_execute=self.cli_execute, now=lambda: 1000,
        )
        quote = runtime.run("hq_quote_story_video", json.dumps({
            "avatar_id": 7, "prompt": "雨夜重逢",
        }, ensure_ascii=False))
        pending_id = quote["pending_action"]["id"]
        duplicate_id = "vpa_" + "f" * 32
        with closing(self.db()) as conn:
            conn.execute("DROP INDEX idx_video_agent_pending_live_input")
            conn.execute(
                "CREATE UNIQUE INDEX idx_video_agent_pending_live_input "
                "ON video_agent_pending_actions(username,capability,input_hash) "
                "WHERE status IN ('awaiting_confirmation','confirming')"
            )
            conn.execute(
                "UPDATE video_agent_pending_actions "
                "SET status='result_unknown',error_code='old_unknown' WHERE id=?",
                (pending_id,),
            )
            conn.execute(
                "INSERT INTO video_agent_pending_actions "
                "SELECT ?,username,tool_name,capability,input_json,input_hash,quote_token,"
                "cost,points,'awaiting_confirmation',created_at+1,expires_at,updated_at+1,"
                "NULL,NULL,NULL,NULL,NULL FROM video_agent_pending_actions WHERE id=?",
                (duplicate_id, pending_id),
            )
            conn.commit()
        video_agent_tools.ensure_tables(self.db)
        with closing(self.db()) as conn:
            rows = conn.execute(
                "SELECT id,status,quote_token FROM video_agent_pending_actions ORDER BY id"
            ).fetchall()
            index_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' "
                "AND name='idx_video_agent_pending_live_input'"
            ).fetchone()[0]
        by_id = {row["id"]: row for row in rows}
        self.assertEqual(by_id[pending_id]["status"], "result_unknown")
        self.assertEqual(by_id[pending_id]["quote_token"], "")
        self.assertEqual(by_id[duplicate_id]["status"], "cancelled")
        self.assertEqual(by_id[duplicate_id]["quote_token"], "")
        self.assertIn("result_unknown", index_sql)


    def test_video_generate_enforces_real_cli_channel_rules(self):
        runtime = video_agent_tools.VideoAgentToolRuntime(
            username="alice", web_token="web-token", db_factory=self.db,
            cli_execute=self.cli_execute, now=lambda: 1000,
        )

        def quote(payload):
            return runtime.run(
                "hq_quote_video_generate", json.dumps(payload, ensure_ascii=False)
            )

        # 真实 CLI 接受：minimax 只收 2k、支持 21:9/adaptive；micro 支持
        # adaptive 与 generate_audio。
        self.assertEqual(
            quote({"prompt": "蓝色产品视频", "channel": "minimax",
                   "ratio": "21:9", "resolution": "2k", "duration": 5})
            ["pending_action"]["status"],
            "awaiting_confirmation",
        )
        self.assertEqual(
            quote({"prompt": "蓝色产品视频", "channel": "micro",
                   "ratio": "adaptive", "generate_audio": True, "duration": 8})
            ["pending_action"]["status"],
            "awaiting_confirmation",
        )
        self.assertEqual(
            quote({"prompt": "蓝色产品视频", "channel": "sora",
                   "seconds": 4, "model": "sora-2-pro", "resolution": "1080p"})
            ["pending_action"]["status"],
            "awaiting_confirmation",
        )
        # 真实 CLI 按“字段存在”判定：显式 false 也只允许 micro。
        self.assertEqual(
            quote({"prompt": "蓝色产品视频", "channel": "micro",
                   "ratio": "adaptive", "generate_audio": False, "duration": 8})
            ["pending_action"]["status"],
            "awaiting_confirmation",
        )
        # 真实 CLI 必拒的组合必须在这里就被拦截，而不是 403/报错到上游。
        invalid = [
            {"prompt": "p", "channel": "minimax", "resolution": "720p"},
            {"prompt": "p", "channel": "grok", "ratio": "21:9"},
            {"prompt": "p", "resolution": "768p"},
            {"prompt": "p", "channel": "sora", "duration": 5},
            {"prompt": "p", "channel": "grok", "generate_audio": True},
            {"prompt": "p", "channel": "grok", "generate_audio": False},
            {"prompt": "p", "channel": "omni", "generate_audio": False},
            {"prompt": "p", "channel": "sora", "generate_audio": False},
            {"prompt": "p", "channel": "omni", "model": "sora-2"},
            {"prompt": "p", "channel": "sora", "seconds": 4, "model": "sora-2",
             "resolution": "1080p"},
            {"prompt": "p", "channel": "grok", "ratio": "16:9",
             "resolution": "480p",
             "reference_upload_ids": ["img_" + "a" * 32]},
        ]
        for payload in invalid:
            with self.assertRaises(video_agent_tools.ToolError) as error:
                quote(payload)
            self.assertEqual(
                error.exception.code, "tool_arguments_invalid", payload
            )

    def test_pending_card_input_summary_is_server_derived_and_fingerprint_bound(self):
        runtime = video_agent_tools.VideoAgentToolRuntime(
            username="alice", web_token="web-token", db_factory=self.db,
            cli_execute=self.cli_execute, now=lambda: 1000,
        )
        result = runtime.run("hq_quote_story_video", json.dumps({
            "avatar_id": 7, "prompt": "只生成蓝色产品视频", "duration": 8,
            "ratio": "9:16",
        }, ensure_ascii=False))
        card = result["pending_action"]
        # 摘要来自鉴权服务签名的标准化 payload（含默认值），指纹即其哈希。
        signed = dict({
            "avatar_id": 7, "prompt": "只生成蓝色产品视频", "duration": 8,
            "ratio": "9:16", "resolution": "720p", "enhance_prompt": False,
        })
        expected_digest = hashlib.sha256(
            json.dumps(signed, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        self.assertEqual(card["input"]["fingerprint"], "cinematic:" + expected_digest)
        self.assertEqual(card["input"]["source"], "signed_payload")
        by_key = {item["key"]: item for item in card["input"]["summary"]}
        self.assertEqual(by_key["prompt"]["value"], "只生成蓝色产品视频")
        self.assertEqual(by_key["prompt"]["label"], "画面描述")
        self.assertEqual(by_key["duration"]["value"], 8)
        # 服务端默认值也必须完整展示：确认卡所见即所提交。
        self.assertEqual(by_key["resolution"]["value"], "720p")
        self.assertEqual(by_key["ratio"]["value"], "9:16")
        self.assertEqual(by_key["enhance_prompt"]["value"], "否")
        self.assertNotIn(
            "quote_token", {item["key"] for item in card["input"]["summary"]}
        )
        # 确认后的卡片依然只展示落库 payload 生成的摘要（与报价同源），
        # 而不是任何模型自由文本。
        confirmed = video_agent_tools.confirm_pending_action(
            card["id"], "request-12345678", username="alice", web_token="web-token",
            db_factory=self.db, cli_execute=self.cli_execute, now=lambda: 1001,
            quote_claims=self.quote_claims,
        )
        self.assertEqual(confirmed["input"]["summary"], card["input"]["summary"])
        self.assertEqual(confirmed["input"]["fingerprint"], card["input"]["fingerprint"])

    def test_confirmation_reverts_when_quote_claims_fails_before_execution(self):
        runtime = video_agent_tools.VideoAgentToolRuntime(
            username="alice", web_token="web-token", db_factory=self.db,
            cli_execute=self.cli_execute, now=lambda: 1000,
        )
        quote = runtime.run("hq_quote_story_video", json.dumps({
            "avatar_id": 7, "prompt": "雨夜重逢",
        }, ensure_ascii=False))
        pending_id = quote["pending_action"]["id"]

        def broken_claims(_token):
            raise video_agent_tools.hq_cli_executor.CLIExecutionError(
                "cli_auth_unavailable", "CLI 鉴权服务暂时不可用", 503,
            )

        with self.assertRaises(video_agent_tools.ToolError) as error:
            video_agent_tools.confirm_pending_action(
                pending_id, "request-12345678", username="alice", web_token="web-token",
                db_factory=self.db, cli_execute=self.cli_execute, now=lambda: 1001,
                quote_claims=broken_claims,
            )
        self.assertEqual(error.exception.code, "cli_auth_unavailable")
        with closing(self.db()) as conn:
            row = conn.execute(
                "SELECT status,idempotency_key,submission_key,quote_token "
                "FROM video_agent_pending_actions WHERE id=?",
                (pending_id,),
            ).fetchone()
        # 新时序先核验后领取：核验失败时卡片从未被领取。
        self.assertEqual(row["status"], "awaiting_confirmation")
        self.assertIsNone(row["idempotency_key"])
        self.assertIsNone(row["submission_key"])
        self.assertEqual(row["quote_token"], "server-only-quote-token")
        self.assertEqual(len(self.calls), 1)  # 只有报价，从未执行付费提交

    def _make_unknown_card(self):
        runtime = video_agent_tools.VideoAgentToolRuntime(
            username="alice", web_token="web-token", db_factory=self.db,
            cli_execute=self.cli_execute, now=lambda: 1000,
        )
        quote = runtime.run("hq_quote_story_video", json.dumps({
            "avatar_id": 7, "prompt": "雨夜重逢",
        }, ensure_ascii=False))
        pending_id = quote["pending_action"]["id"]

        def unknown(*args, **kwargs):
            raise video_agent_tools.hq_cli_executor.CLIExecutionError(
                "cli_timeout", "结果未知", 504, unknown_outcome=True,
            )

        with self.assertRaises(video_agent_tools.ToolError):
            video_agent_tools.confirm_pending_action(
                pending_id, "request-12345678", username="alice", web_token="web-token",
                db_factory=self.db, cli_execute=unknown, now=lambda: 1001,
                quote_claims=self.quote_claims,
            )
        with closing(self.db()) as conn:
            return conn.execute(
                "SELECT submission_key FROM video_agent_pending_actions WHERE id=?",
                (pending_id,),
            ).fetchone()[0]

    def test_reconcile_converges_never_submitted_card_to_failed(self):
        submission_key = self._make_unknown_card()
        self.assertEqual(submission_key, "hqcli-" + "f" * 32)
        with closing(self.db()) as conn:
            row = conn.execute(
                "SELECT id FROM video_agent_pending_actions WHERE submission_key=?",
                (submission_key,),
            ).fetchone()
        card_id = row["id"]
        reconciled = video_agent_tools.reconcile_pending_action(
            card_id, username="alice", db_factory=self.db, now=lambda: 2000,
        )
        self.assertEqual(reconciled["status"], "failed")
        with closing(self.db()) as conn:
            stored = conn.execute(
                "SELECT status,error_code,quote_token "
                "FROM video_agent_pending_actions WHERE id=?", (card_id,)
            ).fetchone()
        self.assertEqual(stored["status"], "failed")
        self.assertEqual(stored["error_code"], "reconciled_never_submitted")
        self.assertEqual(stored["quote_token"], "")
        # 收敛后同输入可以重新报价，不再被未知指纹永久阻断。
        runtime = video_agent_tools.VideoAgentToolRuntime(
            username="alice", web_token="web-token", db_factory=self.db,
            cli_execute=self.cli_execute, now=lambda: 2001,
        )
        fresh = runtime.run("hq_quote_story_video", json.dumps({
            "avatar_id": 7, "prompt": "雨夜重逢",
        }, ensure_ascii=False))
        self.assertTrue(fresh["confirmation_required"])
        self.assertEqual(fresh["pending_action"]["status"], "awaiting_confirmation")

    def test_reconcile_converges_recorded_submission_to_submitted(self):
        submission_key = self._make_unknown_card()
        with closing(self.db()) as conn:
            row = conn.execute(
                "SELECT id FROM video_agent_pending_actions WHERE submission_key=?",
                (submission_key,),
            ).fetchone()
            from content_domains import submission_idempotency
            submission_idempotency.ensure_table(conn)
            conn.execute("""CREATE TABLE IF NOT EXISTS jobs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT, username TEXT, cost INTEGER,
                status TEXT DEFAULT 'pending', payload TEXT, result TEXT, error TEXT,
                created_at INTEGER, updated_at INTEGER, submission_key TEXT)""")
            conn.execute(
                "INSERT INTO submission_idempotency"
                "(username,endpoint,idem_key,request_hash,response_json,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                ("alice", "/api/gen/cinematic", submission_key, "x" * 64,
                 json.dumps({"job_id": 999, "points_left": 120}),
                 1001, 1001),
            )
            job_id = conn.execute(
                "INSERT INTO jobs(kind,username,status,created_at,updated_at,submission_key) "
                "VALUES(?,?,?,?,?,?)",
                ("cinematic", "alice", "pending", 1002, 1002, submission_key),
            ).lastrowid
            conn.commit()
        card_id = row["id"]
        reconciled = video_agent_tools.reconcile_pending_action(
            card_id, username="alice", db_factory=self.db, now=lambda: 2000,
        )
        self.assertEqual(reconciled["status"], "submitted")
        self.assertEqual(reconciled["result"]["job_id"], job_id)

    def test_reconcile_recorded_response_requires_exact_endpoint_and_job_identity(self):
        cases = (
            ("wrong-endpoint", "/api/gen/video", "cinematic"),
            ("wrong-kind", "/api/gen/cinematic", "video"),
            ("missing-job", "/api/gen/cinematic", None),
        )
        for name, endpoint, kind in cases:
            with self.subTest(case=name):
                submission_key = self._make_unknown_card()
                with closing(self.db()) as conn:
                    row = conn.execute(
                        "SELECT id FROM video_agent_pending_actions WHERE submission_key=?",
                        (submission_key,),
                    ).fetchone()
                    from content_domains import submission_idempotency
                    submission_idempotency.ensure_table(conn)
                    conn.execute("""CREATE TABLE IF NOT EXISTS jobs(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        kind TEXT, username TEXT, cost INTEGER,
                        status TEXT DEFAULT 'pending', payload TEXT, result TEXT, error TEXT,
                        created_at INTEGER, updated_at INTEGER, submission_key TEXT)""")
                    conn.execute("DELETE FROM submission_idempotency")
                    conn.execute("DELETE FROM jobs")
                    conn.execute(
                        "INSERT INTO submission_idempotency"
                        "(username,endpoint,idem_key,request_hash,response_json,created_at,updated_at) "
                        "VALUES(?,?,?,?,?,?,?)",
                        ("alice", endpoint, submission_key, "x" * 64,
                         json.dumps({"job_id": 999}), 1001, 1001),
                    )
                    if kind:
                        conn.execute(
                            "INSERT INTO jobs(kind,username,status,created_at,updated_at,submission_key) "
                            "VALUES(?,?,?,?,?,?)",
                            (kind, "alice", "pending", 1002, 1002, submission_key),
                        )
                    conn.commit()
                reconciled = video_agent_tools.reconcile_pending_action(
                    row["id"], username="alice", db_factory=self.db,
                    now=lambda: 2000,
                )
                self.assertEqual(reconciled["status"], "failed")
                self.assertNotIn("result", reconciled)

    def test_reconcile_stays_unknown_while_submission_has_no_response(self):
        submission_key = self._make_unknown_card()
        with closing(self.db()) as conn:
            row = conn.execute(
                "SELECT id FROM video_agent_pending_actions WHERE submission_key=?",
                (submission_key,),
            ).fetchone()
            from content_domains import submission_idempotency
            submission_idempotency.ensure_table(conn)
            conn.execute(
                "INSERT INTO submission_idempotency"
                "(username,endpoint,idem_key,request_hash,response_json,created_at,updated_at) "
                "VALUES(?,?,?,?,NULL,?,?)",
                ("alice", "/api/gen/video", submission_key, "x" * 64, 1001, 1001),
            )
            conn.commit()
        card_id = row["id"]
        # 安全期内：既不能收敛，也不能放行重报。
        with self.assertRaises(video_agent_tools.ToolError) as error:
            video_agent_tools.reconcile_pending_action(
                card_id, username="alice", db_factory=self.db, now=lambda: 1050,
            )
        self.assertEqual(error.exception.code, "pending_reconcile_in_flight")
        self.assertEqual(
            error.exception.pending_action["status"], "result_unknown",
        )

    def test_reconcile_waits_out_safety_window_when_no_record(self):
        submission_key = self._make_unknown_card()
        with closing(self.db()) as conn:
            row = conn.execute(
                "SELECT id FROM video_agent_pending_actions WHERE submission_key=?",
                (submission_key,),
            ).fetchone()
        card_id = row["id"]
        # 刚超时后立即对账：被杀掉的 CLI 背后的认证代理请求可能仍在途，
        # 不能立即判 failed，也不能放行重新报价。
        with self.assertRaises(video_agent_tools.ToolError) as error:
            video_agent_tools.reconcile_pending_action(
                card_id, username="alice", db_factory=self.db, now=lambda: 1050,
            )
        self.assertEqual(error.exception.code, "pending_reconcile_in_flight")
        with closing(self.db()) as conn:
            status = conn.execute(
                "SELECT status FROM video_agent_pending_actions WHERE id=?",
                (card_id,),
            ).fetchone()[0]
        self.assertEqual(status, "result_unknown")

    def test_reconcile_stale_processing_converges_via_jobs_ledger(self):
        submission_key = self._make_unknown_card()
        with closing(self.db()) as conn:
            row = conn.execute(
                "SELECT id,cost FROM video_agent_pending_actions WHERE submission_key=?",
                (submission_key,),
            ).fetchone()
            conn.execute("""CREATE TABLE IF NOT EXISTS jobs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT, username TEXT, cost INTEGER,
                status TEXT DEFAULT 'pending', payload TEXT, result TEXT, error TEXT,
                created_at INTEGER, updated_at INTEGER, submission_key TEXT)""")
            from content_domains import submission_idempotency
            submission_idempotency.ensure_table(conn)
            conn.execute(
                "INSERT INTO submission_idempotency"
                "(username,endpoint,idem_key,request_hash,response_json,created_at,updated_at) "
                "VALUES(?,?,?,?,NULL,?,?)",
                ("alice", "/api/gen/cinematic", submission_key, "x" * 64, 1001, 1001),
            )
            # 受理中断但任务已经创建：账本必须收敛为 submitted。
            conn.execute(
                "INSERT INTO jobs(kind,username,cost,status,created_at,updated_at,submission_key) "
                "VALUES(?,?,?,?,?,?,?)",
                ("cinematic", "alice", int(row["cost"]), "pending", 1002, 1002,
                 submission_key),
            )
            conn.commit()
        card_id = row["id"]
        reconciled = video_agent_tools.reconcile_pending_action(
            card_id, username="alice", db_factory=self.db, now=lambda: 2000,
        )
        self.assertEqual(reconciled["status"], "submitted")
        self.assertGreaterEqual(reconciled["result"]["job_id"], 1)

    def test_reconcile_never_claims_unrelated_same_price_same_kind_job(self):
        submission_key = self._make_unknown_card()
        with closing(self.db()) as conn:
            row = conn.execute(
                "SELECT id,cost FROM video_agent_pending_actions WHERE submission_key=?",
                (submission_key,),
            ).fetchone()
            conn.execute("""CREATE TABLE IF NOT EXISTS jobs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT, username TEXT, cost INTEGER,
                status TEXT DEFAULT 'pending', payload TEXT, result TEXT, error TEXT,
                created_at INTEGER, updated_at INTEGER, submission_key TEXT)""")
            from content_domains import submission_idempotency
            submission_idempotency.ensure_table(conn)
            conn.execute(
                "INSERT INTO submission_idempotency"
                "(username,endpoint,idem_key,request_hash,response_json,created_at,updated_at) "
                "VALUES(?,?,?,?,NULL,?,?)",
                ("alice", "/api/gen/cinematic", submission_key, "x" * 64, 1001, 1001),
            )
            # 与未知卡同账号、同价格、同类型、同时间窗，但属于另一次请求。
            conn.execute(
                "INSERT INTO jobs(kind,username,cost,status,created_at,updated_at,submission_key) "
                "VALUES(?,?,?,?,?,?,?)",
                ("cinematic", "alice", int(row["cost"]), "pending", 1002, 1002,
                 "hqcli-" + "e" * 32),
            )
            conn.commit()
        reconciled = video_agent_tools.reconcile_pending_action(
            row["id"], username="alice", db_factory=self.db, now=lambda: 2000,
        )
        self.assertEqual("failed", reconciled["status"])
        self.assertNotIn("result", reconciled)

    def test_reconcile_stale_processing_without_job_converges_failed(self):
        submission_key = self._make_unknown_card()
        with closing(self.db()) as conn:
            row = conn.execute(
                "SELECT id FROM video_agent_pending_actions WHERE submission_key=?",
                (submission_key,),
            ).fetchone()
            conn.execute("""CREATE TABLE IF NOT EXISTS jobs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT, username TEXT, cost INTEGER,
                status TEXT DEFAULT 'pending', payload TEXT, result TEXT, error TEXT,
                created_at INTEGER, updated_at INTEGER, submission_key TEXT)""")
            from content_domains import submission_idempotency
            submission_idempotency.ensure_table(conn)
            conn.execute(
                "INSERT INTO submission_idempotency"
                "(username,endpoint,idem_key,request_hash,response_json,created_at,updated_at) "
                "VALUES(?,?,?,?,NULL,?,?)",
                ("alice", "/api/gen/cinematic", submission_key, "x" * 64, 1001, 1001),
            )
            conn.commit()
        card_id = row["id"]
        reconciled = video_agent_tools.reconcile_pending_action(
            card_id, username="alice", db_factory=self.db, now=lambda: 2000,
        )
        self.assertEqual(reconciled["status"], "failed")
        with closing(self.db()) as conn:
            stored = conn.execute(
                "SELECT status,error_code FROM video_agent_pending_actions WHERE id=?",
                (card_id,),
            ).fetchone()
        self.assertEqual(stored["status"], "failed")
        self.assertEqual(stored["error_code"], "reconciled_stale_processing")

    def test_reconcile_rejects_legacy_cards_without_submission_key(self):
        submission_key = self._make_unknown_card()
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE video_agent_pending_actions SET submission_key=NULL "
                "WHERE submission_key=?", (submission_key,)
            )
            row = conn.execute(
                "SELECT id FROM video_agent_pending_actions"
            ).fetchone()
            conn.commit()
        with self.assertRaises(video_agent_tools.ToolError) as error:
            video_agent_tools.reconcile_pending_action(
                row["id"], username="alice", db_factory=self.db, now=lambda: 2000,
            )
        self.assertEqual(error.exception.code, "pending_reconcile_unavailable")
        self.assertEqual(
            error.exception.pending_action["status"], "result_unknown",
        )


if __name__ == "__main__":
    unittest.main()
