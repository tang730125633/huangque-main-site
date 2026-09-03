# -*- coding: utf-8 -*-
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

        def cli_execute(capability, input_body, **kwargs):
            self.calls.append((capability, dict(input_body), dict(kwargs)))
            if kwargs.get("confirm"):
                return {"job_id": 321, "status": "pending"}
            if capability.endswith("generate"):
                return {
                    "quote_token": "server-only-quote-token",
                    "fingerprint": "cinematic:" + "a" * 64,
                    "kind": "cinematic", "cost": 90, "points": 1000,
                    "expires_in": 120, "confirmation_required": True,
                }
            return {"items": [{"display_name": "公共音色 1"}], "total": 1}

        self.cli_execute = cli_execute

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
        )
        second = video_agent_tools.confirm_pending_action(
            pending_id, "request-12345678", username="alice", web_token="web-token",
            db_factory=self.db, cli_execute=self.cli_execute, now=lambda: 1002,
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
        normalized_blocked = fresh_runtime.run(
            "hq_quote_story_video", semantic_variant
        )
        self.assertTrue(normalized_blocked["result_unknown"])
        self.assertFalse(normalized_blocked["confirmation_required"])
        self.assertEqual(normalized_blocked["pending_action"]["id"], pending_id)
        # A free normalized quote is allowed, but no new confirmable card is
        # created and therefore no second paid submit can be clicked.
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
                "NULL,NULL,NULL FROM video_agent_pending_actions WHERE id=?",
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


if __name__ == "__main__":
    unittest.main()
