"""Local-only Codex CLI transport backed by the signed-in ChatGPT account."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


class CodexTransportError(RuntimeError):
    pass


class CodexResponse:
    status_code = 200
    text = ""

    def __init__(self, content):
        self.content = str(content or "")
        self.text = self.content

    def json(self):
        return {"choices": [{"message": {"content": self.content}}]}


def _clean_env(source=None):
    env = dict(source or os.environ)
    for key in list(env):
        upper = key.upper()
        if upper.endswith(("_API_KEY", "_ACCESS_TOKEN", "_SECRET")):
            env.pop(key, None)
    return env


def require_chatgpt_login(*, codex_bin=None, runner=subprocess.run, env=None):
    codex_bin = codex_bin or shutil.which("codex")
    if not codex_bin:
        raise CodexTransportError("本机没有安装 Codex CLI")
    try:
        result = runner(
            [codex_bin, "login", "status"],
            text=True,
            capture_output=True,
            timeout=15,
            env=_clean_env(env),
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CodexTransportError("无法确认 Codex 登录状态") from exc
    status = (str(result.stdout or "") + "\n" + str(result.stderr or "")).strip()
    if result.returncode != 0 or "Logged in using ChatGPT" not in status:
        raise CodexTransportError("本地模式只允许使用 ChatGPT 登录的 Codex 订阅额度")
    return status


def complete(messages, *, schema=None, timeout=180, workdir=None, codex_bin=None,
             runner=subprocess.run, env=None):
    codex_bin = codex_bin or shutil.which("codex")
    if not codex_bin:
        raise CodexTransportError("本机没有安装 Codex CLI")
    model = str((env or os.environ).get("HERMES_CODEX_MODEL") or "gpt-5.6-luna")
    reasoning = str((env or os.environ).get("HERMES_CODEX_REASONING") or "low")
    transcript = json.dumps(list(messages or []), ensure_ascii=False)
    prompt = (
        "你是黄雀 IP12 本地模型传输。不得调用工具、运行命令或修改文件。"
        "严格按 messages 中的 role 层级处理下面的对话，只输出最终 assistant 内容。\n\n"
        "messages:\n" + transcript
    )
    with tempfile.TemporaryDirectory(prefix="ip12-codex-") as directory:
        directory = Path(directory)
        output_path = directory / "answer.txt"
        command = [
            codex_bin, "exec", "-C", str(directory), "-s", "read-only",
            "--ephemeral", "--skip-git-repo-check", "--color", "never",
            "--ignore-user-config", "-m", model,
            "-c", 'model_reasoning_effort="%s"' % reasoning,
            "-o", str(output_path),
        ]
        if schema:
            schema_path = directory / "schema.json"
            schema_path.write_text(
                json.dumps(schema, ensure_ascii=False), encoding="utf-8"
            )
            command.extend(["--output-schema", str(schema_path)])
            prompt += "\n\n只输出符合所给 JSON Schema 的完整 JSON 对象，不要 Markdown。"
        command.append(prompt)
        try:
            result = runner(
                command,
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=max(1, float(timeout)),
                env=_clean_env(env),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise CodexTransportError("Codex 本地调用超时") from exc
        except OSError as exc:
            raise CodexTransportError("Codex 本地调用失败") from exc
        if result.returncode != 0:
            raise CodexTransportError("Codex 本地调用失败")
        try:
            content = output_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise CodexTransportError("Codex 没有返回结果") from exc
        if not content:
            raise CodexTransportError("Codex 没有返回结果")
        if schema:
            try:
                json.loads(content)
            except ValueError as exc:
                raise CodexTransportError("Codex 没有返回有效 JSON") from exc
        return content
