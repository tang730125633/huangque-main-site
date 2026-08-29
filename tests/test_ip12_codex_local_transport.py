import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


HERMES = Path(__file__).parents[1] / "server" / "hermes_ip12"
sys.path.insert(0, str(HERMES))

import codex_local_transport as transport


class CodexLocalTransportTests(unittest.TestCase):
    def test_chatgpt_login_is_required(self):
        def ok(_command, **_kwargs):
            return SimpleNamespace(returncode=0, stdout="Logged in using ChatGPT\n", stderr="")

        self.assertIn("ChatGPT", transport.require_chatgpt_login(codex_bin="codex", runner=ok))

        def api(_command, **_kwargs):
            return SimpleNamespace(returncode=0, stdout="Logged in using an API key\n", stderr="")

        with self.assertRaisesRegex(transport.CodexTransportError, "订阅额度"):
            transport.require_chatgpt_login(codex_bin="codex", runner=api)

    def test_provider_keys_are_not_inherited(self):
        cleaned = transport._clean_env({
            "PATH": "/bin", "OPENAI_API_KEY": "secret", "DEEPSEEK_API_KEY": "secret",
            "CODEX_ACCESS_TOKEN": "secret", "CODEX_HOME": "/tmp/codex",
        })
        self.assertEqual(cleaned, {"PATH": "/bin", "CODEX_HOME": "/tmp/codex"})

    def test_structured_completion_uses_schema_and_existing_response_shape(self):
        captured = {}

        def runner(command, **kwargs):
            captured.update(command=command, kwargs=kwargs)
            output = Path(command[command.index("-o") + 1])
            output.write_text('{"ok":true}', encoding="utf-8")
            return SimpleNamespace(returncode=0, stderr="")

        with tempfile.TemporaryDirectory() as directory:
            content = transport.complete(
                [{"role": "user", "content": "测试"}],
                schema={"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
                workdir=directory,
                codex_bin="codex",
                runner=runner,
                env={"OPENAI_API_KEY": "secret", "PATH": "/bin"},
            )
        self.assertEqual(json.loads(content), {"ok": True})
        self.assertIn("--output-schema", captured["command"])
        self.assertIn("read-only", captured["command"])
        self.assertIn("--ignore-user-config", captured["command"])
        self.assertIn("gpt-5.6-luna", captured["command"])
        self.assertNotIn("OPENAI_API_KEY", captured["kwargs"]["env"])
        self.assertEqual(transport.CodexResponse(content).json()["choices"][0]["message"]["content"], content)

    def test_nonzero_and_timeout_fail_closed(self):
        def failed(_command, **_kwargs):
            return SimpleNamespace(returncode=2, stderr="private details")

        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(
            transport.CodexTransportError, "调用失败"
        ):
            transport.complete([], workdir=directory, codex_bin="codex", runner=failed)

        def timeout(_command, **_kwargs):
            raise subprocess.TimeoutExpired("codex", 1)

        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(
            transport.CodexTransportError, "超时"
        ):
            transport.complete([], workdir=directory, codex_bin="codex", runner=timeout)


if __name__ == "__main__":
    unittest.main()
