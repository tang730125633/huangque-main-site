# -*- coding: utf-8 -*-
"""Fail-closed HQ CLI bridge for the customer-guide Director Agent.

Capability discovery is secret-free.  Customer-confirmed script production is
the only account-bound action: it uses a two-minute user-scoped delegation,
an isolated credentials directory, an exact trusted origin, and the real CLI
quote/confirm contract.  No token is sent to the model or persisted in jobs.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request


MAX_DISCOVERY_OUTPUT_BYTES = 1024 * 1024
MAX_CLI_OUTPUT_BYTES = 256 * 1024
CLI_TIMEOUT_SECONDS = max(
    1, min(10, int(os.environ.get("DIRECTOR_AGENT_CLI_TIMEOUT_SECONDS", "5") or 5))
)
PRODUCTION_CLI_TIMEOUT_SECONDS = max(
    5, min(120, int(os.environ.get("DIRECTOR_AGENT_PRODUCTION_CLI_TIMEOUT_SECONDS", "30") or 30))
)
AUTH_BASE = os.environ.get("AUTH_BASE", "http://127.0.0.1:8095").strip().rstrip("/")
INTERNAL_TOKEN = os.environ.get("HQ_INTERNAL_TOKEN", "")
PRODUCTION_ORIGIN = os.environ.get("DIRECTOR_AGENT_CLI_ORIGIN", "").strip().rstrip("/")
TRUSTED_PRODUCTION_ORIGINS = frozenset({
    "https://huangquechuanmei.com",
    "https://yuelei.huangquechuanmei.com",
})
_LOCAL_CLI_ROOT = Path(__file__).resolve().parents[2] / "tools" / "hq-cli"
_RUNTIME_CLI_ROOT = Path("/opt/huangque-repository/tools/hq-cli")
CLI_ROOT = Path(os.environ.get(
    "DIRECTOR_AGENT_CLI_ROOT",
    str(_LOCAL_CLI_ROOT if _LOCAL_CLI_ROOT.is_dir() else _RUNTIME_CLI_ROOT),
)).expanduser()

PAGE_CAPABILITY = {
    "script": "script",
    "digital_human_oneclick": "digital-presenter-capability",
    "private_domain_video": "assets-page",
}
_REQUIRED_MODULE_FILES = (
    "__init__.py", "__main__.py", "catalog.py", "cli.py", "client.py",
)
_RUN_MODULE = (
    "import runpy,sys;"
    "sys.path.insert(0,sys.argv.pop(1));"
    "runpy.run_module('hq_cli',run_name='__main__')"
)


class DirectorCLIError(ValueError):
    """Public-safe failure raised when the local CLI contract is unusable."""

    def __init__(self, detail, code="director_cli_error", status=502, retryable=True):
        super().__init__(str(detail))
        self.code = str(code or "director_cli_error")[:80]
        self.status = int(status)
        self.retryable = bool(retryable)


def _cli_paths(root=None):
    candidate = Path(root or CLI_ROOT)
    if not candidate.is_absolute() or candidate.is_symlink():
        raise DirectorCLIError("编导 CLI 路径无效")
    try:
        resolved_root = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        raise DirectorCLIError("编导 CLI 尚未安装")
    source = resolved_root / "src"
    package = source / "hq_cli"
    for name in _REQUIRED_MODULE_FILES:
        item = package / name
        try:
            resolved = item.resolve(strict=True)
            resolved.relative_to(resolved_root)
        except (OSError, RuntimeError, ValueError):
            raise DirectorCLIError("编导 CLI 文件不完整")
        if item.is_symlink() or not resolved.is_file():
            raise DirectorCLIError("编导 CLI 文件不安全")
    return resolved_root, source


def is_available(root=None):
    try:
        _cli_paths(root)
        return True
    except DirectorCLIError:
        return False


def production_is_available(root=None):
    if (not is_available(root) or not INTERNAL_TOKEN
            or PRODUCTION_ORIGIN not in TRUSTED_PRODUCTION_ORIGINS):
        return False
    parsed = urllib.parse.urlsplit(AUTH_BASE)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        return False
    try:
        health = _local_json(
            "/api/auth/internal/director-agent/health", {},
            {"X-HQ-Internal-Token": INTERNAL_TOKEN}, timeout=2,
        )
    except DirectorCLIError:
        return False
    return bool(
        health.get("ready") is True
        and health.get("contract") == "director-agent-action/v1"
        and health.get("stable_idempotency_required") is True
        and health.get("actions") == ["director-script-generate"]
    )


def _subprocess_env():
    """Build a minimal environment that deliberately excludes all secrets."""
    result = {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONIOENCODING": "utf-8",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }
    # CPython on Windows needs these process settings; none are credentials.
    for name in ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP"):
        if os.environ.get(name):
            result[name] = os.environ[name]
    return result


def _run_json(arguments, root=None, runner=subprocess.run):
    if not arguments or arguments[0] not in {"capabilities", "describe"}:
        raise DirectorCLIError("编导 CLI 命令不在允许范围")
    if arguments[0] == "capabilities" and len(arguments) != 1:
        raise DirectorCLIError("编导 CLI 参数无效")
    if arguments[0] == "describe" and (
            len(arguments) != 2 or arguments[1] not in PAGE_CAPABILITY.values()):
        raise DirectorCLIError("编导 CLI 能力不在允许范围")
    cli_root, source = _cli_paths(root)
    command = [
        sys.executable, "-I", "-X", "utf8", "-c", _RUN_MODULE, str(source),
        *arguments, "--json",
    ]
    try:
        completed = runner(
            command, cwd=str(cli_root), env=_subprocess_env(),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8",
            errors="replace", timeout=CLI_TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise DirectorCLIError("编导 CLI 暂时不可用")
    stdout = str(completed.stdout or "")
    stderr = str(completed.stderr or "")
    if (len(stdout.encode("utf-8")) > MAX_DISCOVERY_OUTPUT_BYTES
            or len(stderr.encode("utf-8")) > MAX_DISCOVERY_OUTPUT_BYTES):
        raise DirectorCLIError("编导 CLI 返回内容过大")
    if int(completed.returncode) != 0:
        raise DirectorCLIError("编导 CLI 执行失败")
    try:
        payload = json.loads(stdout)
    except (TypeError, ValueError):
        raise DirectorCLIError("编导 CLI 返回格式无效")
    if not isinstance(payload, dict):
        raise DirectorCLIError("编导 CLI 返回格式无效")
    return payload


def _local_json(path, body, headers=None, timeout=10):
    parsed = urllib.parse.urlsplit(AUTH_BASE)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise DirectorCLIError("编导 CLI 内部鉴权目标不安全")
    data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request_headers = {"Content-Type": "application/json", "Accept": "application/json"}
    request_headers.update(headers or {})
    request = urllib.request.Request(
        AUTH_BASE + path, data=data, headers=request_headers, method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            raw, status = response.read(MAX_CLI_OUTPUT_BYTES + 1), response.getcode()
    except urllib.error.HTTPError as error:
        raw, status = error.read(MAX_CLI_OUTPUT_BYTES + 1), error.code
    except (urllib.error.URLError, OSError):
        raise DirectorCLIError("编导 CLI 内部鉴权暂时不可用")
    if len(raw) > MAX_CLI_OUTPUT_BYTES:
        raise DirectorCLIError("编导 CLI 内部鉴权响应过大")
    try:
        payload = json.loads(raw or b"{}")
    except (UnicodeDecodeError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    if not 200 <= int(status) < 300:
        code = str(payload.get("code") or "director_cli_error")[:80]
        detail = str(payload.get("detail") or "编导 CLI 内部调用失败")[:220]
        raise DirectorCLIError(
            detail, code=code, status=int(status),
            retryable=int(status) >= 500 or int(status) in {408, 429},
        )
    return payload


_SCRIPT_STYLE = {"口播": "spoken", "剧情": "story", "种草": "recommend"}
_SCRIPT_DURATION = {"15s": 15, "30s": 30, "60s": 60}
_SCRIPT_PLATFORM = {"抖音": "douyin", "小红书": "xiaohongshu", "视频号": "channels"}


def _canonical_script_input(cli_input):
    required = {
        "request_id", "topic", "selling_points", "style", "duration", "platform",
    }
    if not isinstance(cli_input, dict) or set(cli_input) != required:
        raise DirectorCLIError("编导 CLI 脚本输入格式无效", retryable=False)
    request_id = str(cli_input.get("request_id") or "")
    topic = str(cli_input.get("topic") or "").strip()
    selling_points = str(cli_input.get("selling_points") or "").strip()
    if not 8 <= len(request_id) <= 128 or not topic:
        raise DirectorCLIError("编导 CLI 脚本输入格式无效", retryable=False)
    try:
        style = _SCRIPT_STYLE[cli_input["style"]]
        duration = _SCRIPT_DURATION[cli_input["duration"]]
        platform = _SCRIPT_PLATFORM[cli_input["platform"]]
    except (KeyError, TypeError):
        raise DirectorCLIError("编导 CLI 脚本选项无效", retryable=False)
    prompt = topic
    if selling_points:
        prompt += "；核心卖点：" + selling_points
    if len(prompt) > 20000:
        raise DirectorCLIError("编导 CLI 脚本内容过长", retryable=False)
    return request_id, {
        "prompt": prompt,
        "style": style,
        "duration": duration,
        "platform": platform,
    }


def _director_script_action(username, cli_input, confirm=False, quote_token=""):
    if not isinstance(username, str) or not username.strip() or len(username.strip()) > 160:
        raise DirectorCLIError("编导 CLI 缺少认证账号", retryable=False)
    request_id, canonical_input = _canonical_script_input(cli_input)
    body = {
        "username": username.strip(),
        "action": "director-script-generate",
        "input": canonical_input,
        "confirm": bool(confirm),
    }
    if confirm:
        body["quote_token"] = quote_token
        body["idempotency_key"] = request_id
    return _local_json(
        "/api/auth/internal/director-agent/action", body,
        {"X-HQ-Internal-Token": INTERNAL_TOKEN},
        timeout=PRODUCTION_CLI_TIMEOUT_SECONDS,
    )


def _delegation(username, purpose="director_agent_script"):
    if not production_is_available():
        raise DirectorCLIError("编导 CLI 生产能力未安全配置")
    result = _local_json(
        "/api/auth/internal/cli/delegation",
        {"username": username, "purpose": purpose},
        {"X-HQ-Internal-Token": INTERNAL_TOKEN},
    )
    token = str(result.get("access_token") or "")
    origin = str(result.get("origin") or "").rstrip("/")
    scopes = result.get("scopes") or []
    expires_at = int(result.get("expires_at") or 0)
    if (len(token) < 20 or origin != PRODUCTION_ORIGIN
            or origin not in TRUSTED_PRODUCTION_ORIGINS
            or set(scopes) != {"generation:quote", "generation:submit", "tasks:read"}
            or expires_at <= int(time.time())):
        raise DirectorCLIError("编导 CLI 内部委托格式无效")
    return token, expires_at, scopes, origin


def _revoke_delegation(token):
    try:
        _local_json(
            "/api/auth/cli/logout", {}, {"Authorization": "Bearer " + token},
        )
    except DirectorCLIError:
        pass


def _run_production(arguments, cli_input, username, root=None, runner=subprocess.run):
    capability = arguments[1] if len(arguments) >= 2 else ""
    if (not arguments or arguments[0] != "run"
            or capability not in {"script-generate", "breakdown-reverse"}
            or "--open-browser" in arguments or "--file" in arguments):
        raise DirectorCLIError("编导 CLI 生产命令不在允许范围")
    cli_root, source = _cli_paths(root)
    purpose = "director_agent_reverse" if capability == "breakdown-reverse" else "director_agent_script"
    token, expires_at, scopes, origin = _delegation(username, purpose)
    command = [
        sys.executable, "-I", "-X", "utf8", "-c", _RUN_MODULE, str(source),
        *arguments, "--input", "@-", "--json",
    ]
    try:
        with tempfile.TemporaryDirectory(prefix="hq-director-cli-") as config_dir:
            os.chmod(config_dir, 0o700)
            credentials = Path(config_dir) / "credentials.json"
            credentials.write_text(json.dumps({
                "access_token": token, "expires_at": expires_at, "scopes": scopes,
            }, sort_keys=True, separators=(",", ":")), encoding="utf-8")
            os.chmod(credentials, 0o600)
            env = _subprocess_env()
            env["HQ_CLI_CONFIG_DIR"] = config_dir
            env["HQ_CLI_API_BASE"] = origin
            try:
                completed = runner(
                    command, cwd=str(cli_root), env=env,
                    input=json.dumps(cli_input, ensure_ascii=False, separators=(",", ":")),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    encoding="utf-8", errors="replace",
                    timeout=PRODUCTION_CLI_TIMEOUT_SECONDS, check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                raise DirectorCLIError("编导 CLI 生产调用暂时不可用")
    finally:
        _revoke_delegation(token)
    stdout, stderr = str(completed.stdout or ""), str(completed.stderr or "")
    if (len(stdout.encode("utf-8")) > MAX_CLI_OUTPUT_BYTES
            or len(stderr.encode("utf-8")) > MAX_CLI_OUTPUT_BYTES):
        raise DirectorCLIError("编导 CLI 生产响应过大")
    if int(completed.returncode) != 0:
        try:
            error_payload = json.loads(stderr)
        except (TypeError, ValueError):
            error_payload = {}
        if (isinstance(error_payload, dict)
                and error_payload.get("schema") == "hq.error/v1"):
            code = str(error_payload.get("error") or "director_cli_error")[:80]
            detail = str(error_payload.get("message") or "编导 CLI 生产执行失败")[:220]
            details = error_payload.get("details")
            http_status = details.get("http_status") if isinstance(details, dict) else None
            if isinstance(http_status, bool) or not isinstance(http_status, int):
                http_status = 502
            if http_status == 402 or code == "insufficient_points":
                raise DirectorCLIError(
                    "点数不足，请充值后重新确认生产",
                    code="insufficient_points", status=402, retryable=False,
                )
            if code == "quote_expired":
                raise DirectorCLIError(
                    "报价已过期，正在重新报价",
                    code=code, status=409, retryable=True,
                )
            retryable = http_status >= 500 or http_status in {408, 429}
            raise DirectorCLIError(
                detail, code=code, status=http_status, retryable=retryable,
            )
        raise DirectorCLIError("编导 CLI 生产执行失败")
    try:
        payload = json.loads(stdout)
    except (TypeError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        raise DirectorCLIError("编导 CLI 生产响应格式无效")
    if payload.get("schema") != "hq.run/v1" or payload.get("capability") != capability:
        raise DirectorCLIError("编导 CLI 生产响应版本无效")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise DirectorCLIError("编导 CLI 生产响应格式无效")
    return result


def quote_script(username, cli_input, root=None, runner=subprocess.run):
    if not production_is_available(root):
        raise DirectorCLIError("编导 CLI 生产能力未安全配置")
    result = _director_script_action(username, cli_input)
    cost = result.get("cost")
    expires_in = result.get("expires_in")
    quote_token = result.get("quote_token")
    if (not isinstance(quote_token, str) or not 20 <= len(quote_token) <= 4096
            or isinstance(cost, bool) or not isinstance(cost, int) or not 1 <= cost <= 10000
            or isinstance(expires_in, bool) or not isinstance(expires_in, int)
            or not 1 <= expires_in <= 3600):
        raise DirectorCLIError("编导 CLI 报价响应无效")
    return result


def confirm_script(username, cli_input, quote_token, root=None, runner=subprocess.run):
    if not isinstance(quote_token, str) or not 20 <= len(quote_token) <= 4096:
        raise DirectorCLIError("编导 CLI 报价凭证无效")
    if not production_is_available(root):
        raise DirectorCLIError("编导 CLI 生产能力未安全配置")
    result = _director_script_action(
        username, cli_input, confirm=True, quote_token=quote_token,
    )
    job_id = result.get("job_id")
    if isinstance(job_id, bool) or not isinstance(job_id, int) or job_id <= 0:
        raise DirectorCLIError("编导 CLI 没有返回有效任务号")
    return result


def quote_reverse(username, cli_input, root=None, runner=subprocess.run):
    result = _run_production(
        ["run", "breakdown-reverse"], cli_input, username,
        root=root, runner=runner,
    )
    if (not isinstance(result.get("quote_token"), str)
            or not isinstance(result.get("cost"), int)
            or not isinstance(result.get("expires_in"), int)):
        raise DirectorCLIError("视频反推 CLI 报价响应无效")
    return result


def confirm_reverse(username, cli_input, quote_token, root=None, runner=subprocess.run):
    if not isinstance(quote_token, str) or not 20 <= len(quote_token) <= 4096:
        raise DirectorCLIError("视频反推 CLI 报价凭证无效")
    result = _run_production(
        ["run", "breakdown-reverse", "--confirm", "--quote-token", quote_token],
        cli_input, username, root=root, runner=runner,
    )
    job_id = result.get("job_id")
    if isinstance(job_id, bool) or not isinstance(job_id, int) or job_id <= 0:
        raise DirectorCLIError("视频反推 CLI 没有返回有效任务号")
    return result


def page_guide(page, root=None, runner=subprocess.run):
    """Discover and describe one page capability through the real HQ CLI."""
    capability_id = PAGE_CAPABILITY.get(str(page or ""))
    if not capability_id:
        raise DirectorCLIError("当前页面没有编导 CLI 能力")
    catalog = _run_json(["capabilities"], root=root, runner=runner)
    if catalog.get("schema") != "hq.capabilities/v1":
        raise DirectorCLIError("编导 CLI 能力目录版本无效")
    matches = [
        item for item in (catalog.get("capabilities") or [])
        if isinstance(item, dict) and item.get("id") == capability_id
    ]
    if len(matches) != 1:
        raise DirectorCLIError("编导 CLI 缺少页面能力")
    described = _run_json(
        ["describe", capability_id], root=root, runner=runner,
    )
    capability = described.get("capability")
    if (described.get("schema") != "hq.describe/v1"
            or not isinstance(capability, dict)
            or capability.get("id") != capability_id):
        raise DirectorCLIError("编导 CLI 能力说明无效")
    allowed_fields = {
        "id", "name", "kind", "description", "input_schema",
        "requires_auth", "required_scope", "target_auth", "side_effect",
        "confirmation_required", "cost", "deep_link", "next_actions",
    }
    safe_capability = {
        key: value for key, value in capability.items() if key in allowed_fields
    }
    return {
        "schema": "hq.director-page-guide/v1",
        "cli_version": str(described.get("cli_version") or ""),
        "page": page,
        "capability": safe_capability,
        "execution_policy": {
            "discovery_only": True,
            "customer_confirmation_required_for_generation": True,
            "chat_confirmed_script_generation": production_is_available(root),
        },
    }
