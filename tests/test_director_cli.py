import json
import io
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from content_domains import director_cli


def catalog_payload():
    return {
        "schema": "hq.capabilities/v1", "cli_version": "0.6.0",
        "capabilities": [
            {"id": item, "kind": "api"}
            for item in director_cli.PAGE_CAPABILITY.values()
        ],
    }


def description_payload(identifier):
    return {
        "schema": "hq.describe/v1", "cli_version": "0.6.0",
        "capability": {
            "id": identifier, "name": "能力", "kind": "api",
            "description": "只读能力说明", "input_schema": {
                "type": "object", "properties": {}, "required": [],
                "additionalProperties": False,
            },
            "requires_auth": True, "required_scope": "profile:read",
            "target_auth": "hq_device_authorization", "side_effect": "read",
            "confirmation_required": False, "cost": {"kind": "none"},
            "deep_link": None, "next_actions": ["由顾客确认下一步"],
            "untrusted_extra": "must not escape the bridge",
        },
    }


class DirectorCLITests(unittest.TestCase):
    def test_submission_recovery_codes_remain_retryable(self):
        for code, status in (
                ("idempotency_in_progress", 409),
                ("reconcile_pending", 503)):
            with self.subTest(code=code):
                error = urllib.error.HTTPError(
                    "http://127.0.0.1/internal", status, code, {},
                    io.BytesIO(json.dumps({
                        "detail": "original submission is recovering",
                        "code": code,
                    }).encode("utf-8")),
                )
                opener = mock.Mock()
                opener.open.side_effect = error
                with mock.patch.object(
                    director_cli.urllib.request, "build_opener",
                    return_value=opener,
                ), mock.patch.object(
                    director_cli, "AUTH_BASE", "http://127.0.0.1:8011",
                ), self.assertRaises(director_cli.DirectorCLIError) as raised:
                    director_cli._local_json("/action", {})
                self.assertEqual(code, raised.exception.code)
                self.assertTrue(raised.exception.retryable)

    def test_script_bridge_uses_canonical_internal_action_and_stable_idempotency(self):
        cli_input = {
            "request_id": "director-production-1234567890abcdef",
            "topic": "东鹏特饮",
            "selling_points": "买三送一",
            "style": "种草",
            "duration": "30s",
            "platform": "抖音",
        }
        calls = []

        def local_json(path, body, headers=None, timeout=10):
            calls.append((path, body, headers, timeout))
            if path.endswith("/health"):
                return {
                    "ready": True,
                    "contract": "director-agent-action/v1",
                    "actions": ["director-script-generate"],
                    "stable_idempotency_required": True,
                }
            if body["confirm"]:
                return {"job_id": 77, "points_left": 96}
            return {
                "quote_token": "q" * 24, "cost": 3,
                "points": 99, "expires_in": 60,
            }

        with mock.patch.object(director_cli, "INTERNAL_TOKEN", "internal-secret"), \
             mock.patch.object(
                 director_cli, "PRODUCTION_ORIGIN", "https://huangquechuanmei.com",
             ), mock.patch.object(director_cli, "_local_json", side_effect=local_json):
            quote = director_cli.quote_script("alice", cli_input)
            result = director_cli.confirm_script(
                "alice", cli_input, quote["quote_token"],
            )

        self.assertEqual((quote["cost"], result["job_id"]), (3, 77))
        actions = [item for item in calls if item[0].endswith("/action")]
        self.assertEqual(len(actions), 2)
        quoted, confirmed = actions[0][1], actions[1][1]
        self.assertEqual(quoted["action"], "director-script-generate")
        self.assertEqual(quoted["input"], {
            "prompt": "东鹏特饮；核心卖点：买三送一",
            "style": "recommend", "duration": 30, "platform": "douyin",
        })
        self.assertNotIn("idempotency_key", quoted)
        self.assertEqual(
            confirmed["idempotency_key"], cli_input["request_id"],
        )
        self.assertEqual(confirmed["input"], quoted["input"])

    def test_production_health_and_quote_validation_fail_closed(self):
        with mock.patch.object(director_cli, "INTERNAL_TOKEN", "internal-secret"), \
             mock.patch.object(
                 director_cli, "PRODUCTION_ORIGIN", "https://huangquechuanmei.com",
             ), mock.patch.object(
                 director_cli, "_local_json", return_value={
                     "ready": True, "contract": "wrong-contract",
                     "actions": ["director-script-generate"],
                     "stable_idempotency_required": True,
                 },
             ):
            self.assertFalse(director_cli.production_is_available())

        cli_input = {
            "request_id": "director-production-1234567890abcdef",
            "topic": "东鹏特饮", "selling_points": "买三送一",
            "style": "种草", "duration": "30s", "platform": "抖音",
        }
        responses = iter([
            {
                "ready": True, "contract": "director-agent-action/v1",
                "actions": ["director-script-generate"],
                "stable_idempotency_required": True,
            },
            {"quote_token": "q" * 24, "cost": True, "expires_in": 60},
        ])
        with mock.patch.object(director_cli, "INTERNAL_TOKEN", "internal-secret"), \
             mock.patch.object(
                 director_cli, "PRODUCTION_ORIGIN", "https://huangquechuanmei.com",
             ), mock.patch.object(
                 director_cli, "_local_json", side_effect=lambda *_args, **_kwargs: next(responses),
             ), self.assertRaisesRegex(
                 director_cli.DirectorCLIError, "报价响应无效",
             ):
            director_cli.quote_script("alice", cli_input)

    def test_real_repository_cli_guides_all_agent_pages(self):
        expected = {
            "script": "script",
            "digital_human_oneclick": "digital-presenter-capability",
        }
        for page, identifier in expected.items():
            with self.subTest(page=page):
                result = director_cli.page_guide(page)
                self.assertEqual(result["schema"], "hq.director-page-guide/v1")
                self.assertEqual(result["capability"]["id"], identifier)
                self.assertTrue(result["execution_policy"]["discovery_only"])
                self.assertTrue(result["execution_policy"][
                    "customer_confirmation_required_for_generation"
                ])
        with self.assertRaises(director_cli.DirectorCLIError):
            director_cli.page_guide("private_domain_video")

    def test_bridge_runs_only_capabilities_and_describe_without_secrets(self):
        captured = []

        def runner(command, **kwargs):
            captured.append((command, kwargs))
            if "capabilities" in command:
                payload = catalog_payload()
            else:
                payload = description_payload("script")
            return subprocess.CompletedProcess(
                command, 0, stdout=json.dumps(payload, ensure_ascii=False), stderr="",
            )

        with mock.patch.dict(os.environ, {
            "DIRECTOR_AGENT_API_KEY": "agent-secret",
            "OPENAI_API_KEY": "global-secret",
            "OPENROUTER_API_KEY": "router-secret",
        }):
            result = director_cli.page_guide("script", runner=runner)
        self.assertEqual(result["capability"]["id"], "script")
        self.assertNotIn("untrusted_extra", result["capability"])
        self.assertEqual(len(captured), 2)
        self.assertIn("capabilities", captured[0][0])
        self.assertIn("describe", captured[1][0])
        self.assertNotIn("run", captured[0][0] + captured[1][0])
        self.assertNotIn("--confirm", captured[0][0] + captured[1][0])
        self.assertIs(captured[0][1]["stdin"], subprocess.DEVNULL)
        for _, kwargs in captured:
            environment = kwargs["env"]
            self.assertNotIn("DIRECTOR_AGENT_API_KEY", environment)
            self.assertNotIn("OPENAI_API_KEY", environment)
            self.assertNotIn("OPENROUTER_API_KEY", environment)
            self.assertNotIn("HOME", environment)

    def test_command_and_page_allowlists_fail_closed(self):
        with self.assertRaisesRegex(director_cli.DirectorCLIError, "允许范围"):
            director_cli._run_json(["run", "script"])
        with self.assertRaisesRegex(director_cli.DirectorCLIError, "能力不在"):
            director_cli._run_json(["describe", "image-generate"])
        with self.assertRaisesRegex(director_cli.DirectorCLIError, "当前页面"):
            director_cli.page_guide("admin")

    def test_timeout_nonzero_and_malformed_output_are_public_safe(self):
        def timeout(*_args, **_kwargs):
            raise subprocess.TimeoutExpired(["hq"], 5)

        with self.assertRaisesRegex(director_cli.DirectorCLIError, "暂时不可用"):
            director_cli._run_json(["capabilities"], runner=timeout)

        def failed(command, **_kwargs):
            return subprocess.CompletedProcess(
                command, 9, stdout="", stderr="credential=/private/value",
            )

        with self.assertRaisesRegex(director_cli.DirectorCLIError, "执行失败") as caught:
            director_cli._run_json(["capabilities"], runner=failed)
        self.assertNotIn("private", str(caught.exception))

        def malformed(command, **_kwargs):
            return subprocess.CompletedProcess(
                command, 0, stdout="not-json", stderr="",
            )

        with self.assertRaisesRegex(director_cli.DirectorCLIError, "返回格式"):
            director_cli._run_json(["capabilities"], runner=malformed)

    def test_missing_or_symlinked_cli_fails_closed(self):
        with tempfile.TemporaryDirectory() as raw:
            missing = pathlib.Path(raw) / "missing"
            self.assertFalse(director_cli.is_available(missing))
            if hasattr(os, "symlink"):
                link = pathlib.Path(raw) / "link"
                try:
                    link.symlink_to(ROOT / "tools" / "hq-cli", target_is_directory=True)
                except OSError:
                    return
                self.assertFalse(director_cli.is_available(link))

    def test_catalog_and_description_must_bind_the_same_capability(self):
        responses = iter([
            {"schema": "hq.capabilities/v1", "capabilities": []},
        ])

        def missing(command, **_kwargs):
            return subprocess.CompletedProcess(
                command, 0, stdout=json.dumps(next(responses)), stderr="",
            )

        with self.assertRaisesRegex(director_cli.DirectorCLIError, "缺少页面能力"):
            director_cli.page_guide("script", runner=missing)

        def mismatched(command, **_kwargs):
            payload = (catalog_payload() if "capabilities" in command
                       else description_payload("video"))
            return subprocess.CompletedProcess(
                command, 0, stdout=json.dumps(payload), stderr="",
            )

        with self.assertRaisesRegex(director_cli.DirectorCLIError, "能力说明无效"):
            director_cli.page_guide("script", runner=mismatched)


if __name__ == "__main__":
    unittest.main()
