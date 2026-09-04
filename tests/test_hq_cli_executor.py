# -*- coding: utf-8 -*-
import io
import json
import os
import shutil
import subprocess
import sys
import unittest
import urllib.request
import uuid
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SERVER_DIR = str(ROOT / "server")
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from content_domains import hq_cli_executor


class HTTPResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit=-1):
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")


class ProcessResult:
    def __init__(self, stdout, returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


class HQCLIExecutorTests(unittest.TestCase):
    def test_cli_auth_base_env_takes_precedence_over_remote_auth_base(self):
        requests = []

        def http_open(request, timeout=0):
            requests.append(request.full_url)
            if request.full_url.endswith("/api/auth/internal/cli/delegate"):
                return HTTPResponse({
                    "access_token": "ephemeral-cli-token-1234567890",
                    "expires_at": 9999999999,
                    "scopes": ["profile:read"],
                })
            return HTTPResponse({"ok": True})

        def run_process(_argv, **_kwargs):
            return ProcessResult(json.dumps({
                "schema": "hq.run/v1", "capability": "account",
                "result": {"user": {"username": "alice"}},
            }))

        with patch.dict(os.environ, {
            "AUTH_BASE": "https://linnn.huangquechuanmei.com",
            "HQ_CLI_AUTH_BASE": "http://127.0.0.1:8104",
        }, clear=False):
            result = hq_cli_executor.execute(
                "account", {}, username="alice", web_token="web-session-token",
                scopes=["profile:read"], internal_token="internal-service-token",
                http_open=http_open, run_process=run_process,
            )

        self.assertEqual("alice", result["user"]["username"])
        self.assertEqual(
            "http://127.0.0.1:8104/api/auth/internal/cli/delegate",
            requests[0],
        )

    def test_cli_token_is_ephemeral_env_only_and_revoked(self):
        requests = []
        process = {}

        def http_open(request, timeout=0):
            requests.append((request, timeout))
            if request.full_url.endswith("/api/auth/internal/cli/delegate"):
                return HTTPResponse({
                    "access_token": "ephemeral-cli-token-1234567890",
                    "expires_at": 9999999999,
                    "scopes": ["profile:read"],
                })
            self.assertTrue(request.full_url.endswith("/api/auth/cli/logout"))
            return HTTPResponse({"ok": True})

        def run_process(argv, **kwargs):
            process["argv"] = argv
            process["kwargs"] = kwargs
            return ProcessResult(json.dumps({
                "schema": "hq.run/v1", "capability": "account",
                "result": {"user": {"username": "alice"}},
            }))

        result = hq_cli_executor.execute(
            "account", {}, username="alice", web_token="web-session-token",
            scopes=["profile:read"], auth_base="http://127.0.0.1:8095",
            internal_token="internal-service-token", http_open=http_open,
            run_process=run_process,
        )
        self.assertEqual(result["user"]["username"], "alice")
        self.assertNotIn("ephemeral-cli-token", " ".join(process["argv"]))
        self.assertNotIn("web-session-token", " ".join(process["argv"]))
        self.assertEqual(process["kwargs"]["env"]["HQ_CLI_ACCESS_TOKEN"], "ephemeral-cli-token-1234567890")
        self.assertEqual(process["kwargs"]["env"]["HQ_CLI_API_BASE"], "http://127.0.0.1:8095")
        self.assertNotIn("HQ_CLI_QUOTE_TOKEN", process["kwargs"]["env"])
        self.assertEqual(len(requests), 2)
        delegate = requests[0][0]
        self.assertEqual(delegate.get_header("Authorization"), "Bearer web-session-token")
        self.assertEqual(delegate.get_header("X-hq-internal-token"), "internal-service-token")

    def test_confirm_requires_quote_and_uses_fixed_cli_arguments(self):
        with self.assertRaises(hq_cli_executor.CLIExecutionError) as error:
            hq_cli_executor.execute(
                "cinematic-open-generate", {"prompt": "test"}, username="alice",
                web_token="web-session-token", scopes=["generation:quote", "generation:submit"],
                confirm=True, quote_token="", auth_base="http://127.0.0.1:8095",
                internal_token="internal-service-token",
            )
        self.assertEqual(error.exception.code, "quote_required")

    def test_confirm_passes_quote_only_in_child_environment(self):
        process = {}
        quote_token = "e" * 48 + "." + "a" * 64

        def http_open(request, timeout=0):
            if request.full_url.endswith("/api/auth/internal/cli/delegate"):
                return HTTPResponse({
                    "access_token": "ephemeral-cli-token-1234567890",
                    "expires_at": 9999999999,
                    "scopes": ["generation:quote", "generation:submit"],
                })
            return HTTPResponse({"ok": True})

        def run_process(argv, **kwargs):
            process["argv"] = list(argv)
            process["kwargs"] = kwargs
            return ProcessResult(json.dumps({
                "schema": "hq.run/v1", "capability": "video-generate",
                "result": {"job_id": 321, "status": "pending"},
            }))

        result = hq_cli_executor.execute(
            "video-generate", {"prompt": "test"}, username="alice",
            web_token="web-session-token",
            scopes=["generation:quote", "generation:submit"], confirm=True,
            quote_token=quote_token, auth_base="http://127.0.0.1:8095",
            internal_token="internal-service-token", http_open=http_open,
            run_process=run_process,
        )
        self.assertEqual(321, result["job_id"])
        self.assertIn("--confirm", process["argv"])
        self.assertNotIn("--quote-token", process["argv"])
        self.assertNotIn(quote_token, " ".join(process["argv"]))
        self.assertEqual(
            quote_token, process["kwargs"]["env"]["HQ_CLI_QUOTE_TOKEN"],
        )

    def test_invalid_quote_fails_before_delegation_or_process(self):
        called = {"http": 0, "process": 0}

        def http_open(*_args, **_kwargs):
            called["http"] += 1

        def run_process(*_args, **_kwargs):
            called["process"] += 1

        with self.assertRaises(hq_cli_executor.CLIExecutionError) as error:
            hq_cli_executor.execute(
                "video-generate", {"prompt": "test"}, username="alice",
                web_token="web-session-token",
                scopes=["generation:quote", "generation:submit"], confirm=True,
                quote_token="not-a-server-quote",
                auth_base="http://127.0.0.1:8095",
                internal_token="internal-service-token", http_open=http_open,
                run_process=run_process,
            )
        self.assertEqual("quote_invalid", error.exception.code)
        self.assertEqual({"http": 0, "process": 0}, called)

    def test_internal_http_opener_disables_proxies_and_redirects(self):
        opener = hq_cli_executor._SAFE_HTTP_OPEN.__self__
        self.assertFalse(any(
            isinstance(handler, urllib.request.ProxyHandler)
            for handler in opener.handlers
        ))
        redirect = next(
            handler for handler in opener.handlers
            if isinstance(handler, hq_cli_executor._NoRedirect)
        )
        self.assertIsNone(redirect.redirect_request(
            None, None, 302, "Found", {}, "https://evil.example/leak",
        ))

    def test_auth_origin_rejects_ssrf_and_nonstandard_official_ports(self):
        accepted = (
            "https://huangquechuanmei.com",
            "https://huangquechuanmei.com:443",
            "http://127.0.0.1:8095",
            "http://localhost:8095",
            "http://[::1]:8095",
        )
        for origin in accepted:
            with self.subTest(origin=origin):
                self.assertEqual(origin, hq_cli_executor._origin(origin))
        rejected = (
            "https://huangquechuanmei.com:444",
            "https://www.huangquechuanmei.com",
            "http://huangquechuanmei.com",
            "https://127.0.0.1:8095",
            "http://0.0.0.0:8095",
            "http://169.254.169.254",
            "http://127.0.0.1:8095/path",
            "http://user@127.0.0.1:8095",
            "http://127.0.0.1:8095?query=1",
            " http://127.0.0.1:8095",
            "http://127.0.0.1:\n8095",
        )
        for origin in rejected:
            with self.subTest(origin=origin), self.assertRaises(
                    hq_cli_executor.CLIExecutionError):
                hq_cli_executor._origin(origin)

    def test_explicit_auth_base_takes_precedence_over_environment(self):
        with patch.dict(os.environ, {
            "AUTH_BASE": "http://127.0.0.1:8095",
            "HQ_CLI_AUTH_BASE": "http://127.0.0.1:8104",
        }, clear=False):
            self.assertEqual(
                "http://127.0.0.1:8123",
                hq_cli_executor._configured_auth_base("http://127.0.0.1:8123"),
            )

    def test_configured_deployment_module_path_passes_subprocess_smoke(self):
        deploy_root = ROOT / (".hq-cli-deploy-test-" + uuid.uuid4().hex)
        deploy_root.mkdir()
        try:
            shutil.copytree(ROOT / "tools" / "hq-cli" / "src" / "hq_cli",
                            deploy_root / "hq_cli")
            with patch.dict(os.environ, {
                "HQ_CLI_MODULE_PATH": str(deploy_root),
            }):
                env = hq_cli_executor._minimal_child_env(
                    "d" * 43, "http://127.0.0.1:8095",
                    deploy_root / ".ephemeral-config",
                )
            completed = subprocess.run(
                [sys.executable, "-m", "hq_cli", "version", "--json"],
                cwd=str(deploy_root), env=env, text=True, encoding="utf-8",
                errors="replace", capture_output=True, timeout=10, shell=False,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual("hq.version/v1", json.loads(completed.stdout)["schema"])
        finally:
            shutil.rmtree(deploy_root, ignore_errors=True)

    def test_production_deploy_entrypoints_sync_and_preflight_cli_package(self):
        deploy = (ROOT / "scripts" / "deploy_site.sh").read_text(encoding="utf-8")
        ship = (ROOT / "ship").read_text(encoding="utf-8")
        sentinel = (ROOT / "scripts" / "drift_sentinel.py").read_text(
            encoding="utf-8"
        )
        for script in (deploy, ship):
            self.assertIn("tools/hq-cli/src/hq_cli/", script)
            self.assertIn("-m hq_cli version --json", script)
        # deploy_site.sh 通过 CONTENT_DIR 变量表达同一生产路径，
        # 契约仍必须是 /home/ubuntu/content-api/hq_cli/。
        self.assertIn('CONTENT_DIR="/home/ubuntu/content-api"', deploy)
        self.assertIn("$HOST:$CONTENT_DIR/hq_cli/", deploy)
        self.assertIn("$ROOT/tools/hq-cli/src/hq_cli/", deploy)
        # 鉴权端必须与内容服务同一次部署，先 auth 后 content。
        self.assertIn('AUTH_DIR="/home/ubuntu/auth-service"', deploy)
        self.assertIn("server/auth_server.py", deploy)
        self.assertIn("server/hq_cli_api.py", deploy)
        self.assertIn("restart huangque-auth", deploy)
        self.assertIn("quote-claims", deploy)
        self.assertIn("tools/hq-cli/src/hq_cli/", sentinel)
        self.assertIn("/home/ubuntu/content-api/hq_cli", sentinel)

    def test_cli_error_envelope_preserves_safe_machine_error(self):
        completed = ProcessResult(
            "", returncode=11,
            stderr=json.dumps({
                "schema": "hq.error/v1", "error": "invalid_quote_token",
                "message": "must not be returned by the bridge", "exit_code": 11,
            }),
        )
        with self.assertRaises(hq_cli_executor.CLIExecutionError) as error:
            hq_cli_executor._parse_cli_result(
                completed, "video-generate", confirm=True,
            )
        self.assertEqual("invalid_quote_token", error.exception.code)
        self.assertTrue(error.exception.unknown_outcome)
        self.assertNotIn("must not", str(error.exception))

    def test_single_deadline_budgets_delegate_process_and_revoke(self):
        requests = []
        process = {}

        def http_open(request, timeout=0):
            requests.append((request.full_url, timeout))
            return HTTPResponse({
                "access_token": "ephemeral-cli-token-1234567890",
                "scopes": ["profile:read"],
            })

        def run_process(argv, **kwargs):
            process["timeout"] = kwargs["timeout"]
            return ProcessResult(json.dumps({
                "schema": "hq.run/v1", "capability": "account",
                "result": {"ok": True},
            }))

        with patch.object(
                hq_cli_executor.time, "monotonic",
                side_effect=[100.0, 100.5, 103.0, 105.1]):
            result = hq_cli_executor.execute(
                "account", {}, username="alice", web_token="web-session-token",
                scopes=["profile:read"], auth_base="http://127.0.0.1:8095",
                internal_token="internal-service-token", timeout=5,
                http_open=http_open, run_process=run_process,
            )
        self.assertTrue(result["ok"])
        self.assertEqual(2.0, process["timeout"])
        self.assertEqual(1, len(requests))
        self.assertLessEqual(requests[0][1], 5.0)

    def test_deadline_before_paid_process_is_known_not_submitted(self):
        process_calls = []
        quote_token = "e" * 48 + "." + "a" * 64

        def http_open(request, timeout=0):
            return HTTPResponse({
                "access_token": "ephemeral-cli-token-1234567890",
                "scopes": ["generation:quote", "generation:submit"],
            })

        with patch.object(
                hq_cli_executor.time, "monotonic",
                side_effect=[100.0, 100.1, 105.2, 105.3]), self.assertRaises(
                    hq_cli_executor.CLIExecutionError) as error:
            hq_cli_executor.execute(
                "video-generate", {"prompt": "test"}, username="alice",
                web_token="web-session-token",
                scopes=["generation:quote", "generation:submit"], confirm=True,
                quote_token=quote_token, auth_base="http://127.0.0.1:8095",
                internal_token="internal-service-token", timeout=5,
                http_open=http_open,
                run_process=lambda *args, **kwargs: process_calls.append(1),
            )
        self.assertEqual("cli_timeout", error.exception.code)
        self.assertFalse(error.exception.unknown_outcome)
        self.assertEqual([], process_calls)

    def test_unknown_capability_fails_before_external_calls(self):
        with self.assertRaises(hq_cli_executor.CLIExecutionError) as error:
            hq_cli_executor.execute(
                "shell", {"command": "whoami"}, username="alice", web_token="token",
                scopes=["profile:read"], auth_base="http://127.0.0.1:8095",
                internal_token="internal-service-token",
            )
        self.assertEqual(error.exception.code, "capability_not_allowed")


if __name__ == "__main__":
    unittest.main()
