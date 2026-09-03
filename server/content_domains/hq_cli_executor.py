"""Constrained HQ CLI subprocess bridge for the video Agent.

The browser session token is exchanged for a short-lived, minimum-scope CLI
grant.  That grant is passed only through the child environment and is revoked
after every invocation.  Callers can select only the capabilities below.
"""

import importlib.util
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


ALLOWED_CAPABILITIES = frozenset({
    "account", "channels", "pricing", "video-avatars", "voices", "assets",
    "tasks", "task", "video-generate", "digital-ip-text-generate",
    "cinematic-open-generate", "cinematic-motion-generate",
})
MAX_HTTP_BYTES = 64 * 1024
MAX_CLI_OUTPUT_BYTES = 128 * 1024
DEFAULT_TIMEOUT = 35
DEFAULT_TOKEN_TTL = 180
QUOTE_TOKEN_ENV = "HQ_CLI_QUOTE_TOKEN"
CLI_MODULE_PATH_ENV = "HQ_CLI_MODULE_PATH"
CLI_AUTH_BASE_ENV = "HQ_CLI_AUTH_BASE"
_QUOTE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,4031}\.[A-Fa-f0-9]{64}$")


class CLIExecutionError(RuntimeError):
    def __init__(self, code, message, status=502, *, unknown_outcome=False):
        super().__init__(message)
        self.code = str(code or "cli_failed")
        self.status = int(status)
        self.unknown_outcome = bool(unknown_outcome)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# Delegation carries both the user's web session and HQ_INTERNAL_TOKEN.  Never
# let environment proxies or a cross-origin redirect observe either header.
_SAFE_HTTP_OPEN = urllib.request.build_opener(
    urllib.request.ProxyHandler({}), _NoRedirect()
).open


def _origin(value):
    configured = str(value or "")
    if (
        not configured
        or configured != configured.strip()
        or any(ord(character) <= 32 or ord(character) == 127 for character in configured)
    ):
        raise CLIExecutionError("auth_base_invalid", "CLI 鉴权服务地址配置无效", 503)
    raw = configured[:-1] if configured.endswith("/") else configured
    try:
        parsed = urllib.parse.urlsplit(raw)
        port = parsed.port
    except ValueError as error:
        raise CLIExecutionError(
            "auth_base_invalid", "CLI 鉴权服务地址配置无效", 503
        ) from error
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise CLIExecutionError("auth_base_invalid", "CLI 鉴权服务地址配置无效", 503)
    host = parsed.hostname.lower()
    is_loopback = (
        parsed.scheme == "http"
        and host in {"127.0.0.1", "localhost", "::1"}
        and (port is None or 1 <= port <= 65535)
    )
    is_official = (
        parsed.scheme == "https"
        and host == "huangquechuanmei.com"
        and port in {None, 443}
    )
    if not (is_loopback or is_official):
        raise CLIExecutionError("auth_base_invalid", "CLI 鉴权服务地址不在允许范围", 503)
    return raw


def _configured_auth_base(explicit=None):
    """Resolve the CLI bridge origin without widening the accepted origins."""
    return _origin(
        explicit
        or os.getenv(CLI_AUTH_BASE_ENV)
        or os.getenv("AUTH_BASE")
        or "http://127.0.0.1:8095"
    )


def _read_json_response(response):
    raw = response.read(MAX_HTTP_BYTES + 1)
    if len(raw) > MAX_HTTP_BYTES:
        raise CLIExecutionError("auth_response_too_large", "CLI 鉴权响应过大", 502)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CLIExecutionError("auth_response_invalid", "CLI 鉴权响应格式无效", 502) from error
    if not isinstance(value, dict):
        raise CLIExecutionError("auth_response_invalid", "CLI 鉴权响应格式无效", 502)
    return value


def _post_json(url, payload, headers, *, timeout, http_open):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with http_open(request, timeout=timeout) as response:
            return _read_json_response(response)
    except CLIExecutionError:
        raise
    except urllib.error.HTTPError as error:
        status = int(getattr(error, "code", 502) or 502)
        raise CLIExecutionError(
            "cli_delegation_denied" if status in {401, 403} else "cli_auth_failed",
            "无法为当前账号取得临时 CLI 授权", status,
        ) from error
    except (OSError, TimeoutError) as error:
        raise CLIExecutionError("cli_auth_unavailable", "CLI 鉴权服务暂时不可用", 503) from error


def _delegate_token(*, auth_base, internal_token, username, web_token, scopes,
                    ttl_seconds, timeout, http_open):
    if not str(internal_token or "").strip():
        raise CLIExecutionError("internal_auth_not_configured", "内部 CLI 授权尚未配置", 503)
    if not str(web_token or "").strip() or not str(username or "").strip():
        raise CLIExecutionError("identity_required", "无法确认当前操作账号", 401)
    unique_scopes = sorted({str(item).strip() for item in scopes if str(item).strip()})
    if not unique_scopes:
        raise CLIExecutionError("scope_required", "CLI 最小权限不能为空", 500)
    result = _post_json(
        auth_base + "/api/auth/internal/cli/delegate",
        {"username": username, "scopes": unique_scopes, "ttl_seconds": int(ttl_seconds)},
        {
            "Authorization": "Bearer " + str(web_token).strip(),
            "X-HQ-Internal-Token": str(internal_token).strip(),
        },
        timeout=timeout, http_open=http_open,
    )
    token = str(result.get("access_token") or "").strip()
    if not 20 <= len(token) <= 512:
        raise CLIExecutionError("cli_delegation_invalid", "临时 CLI 授权响应无效", 502)
    granted = result.get("scopes") or []
    if not isinstance(granted, list) or not set(unique_scopes).issubset(set(granted)):
        raise CLIExecutionError("cli_delegation_invalid", "临时 CLI 授权范围不足", 502)
    return token


def _revoke_token(auth_base, token, *, timeout, http_open):
    if not token:
        return
    try:
        _post_json(
            auth_base + "/api/auth/cli/logout", {},
            {"Authorization": "Bearer " + token},
            timeout=timeout, http_open=http_open,
        )
    except Exception:
        # The grant has a short TTL.  Revocation is best-effort and its failure
        # must never replace the business result or expose the token.
        return


def _cli_module_root():
    """Locate the import root containing ``hq_cli`` in dev and production."""
    configured = str(os.getenv(CLI_MODULE_PATH_ENV, "") or "").strip()
    if configured:
        candidate = Path(configured).expanduser()
        if not candidate.is_absolute():
            raise CLIExecutionError(
                "cli_module_path_invalid", "HQ CLI 模块路径必须是绝对路径", 503
            )
        candidate = candidate.resolve()
        if not (candidate / "hq_cli" / "__main__.py").is_file():
            raise CLIExecutionError(
                "cli_module_path_invalid", "HQ CLI 模块路径不可用", 503
            )
        return candidate

    try:
        spec = importlib.util.find_spec("hq_cli")
    except (ImportError, ModuleNotFoundError, ValueError):
        spec = None
    if spec and spec.submodule_search_locations:
        for location in spec.submodule_search_locations:
            package = Path(location).resolve()
            if (package / "__main__.py").is_file():
                return package.parent

    current = Path(__file__).resolve()
    candidates = (
        # Production deploy copies hq_cli beside content_domains.
        current.parents[1],
        # Repository checkout keeps it under tools/hq-cli/src.
        current.parents[2] / "tools" / "hq-cli" / "src",
    )
    for candidate in candidates:
        if (candidate / "hq_cli" / "__main__.py").is_file():
            return candidate
    raise CLIExecutionError("cli_not_installed", "黄雀 CLI 运行模块未部署", 503)


def _minimal_child_env(token, auth_base, config_dir, quote_token="", module_root=None):
    env = {}
    for key in (
        "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "LANG",
        "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
    ):
        if os.environ.get(key):
            env[key] = os.environ[key]
    env.update({
        "PYTHONPATH": str(module_root or _cli_module_root()),
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "HQ_CLI_ACCESS_TOKEN": token,
        "HQ_CLI_API_BASE": auth_base,
        "HQ_CLI_CONFIG_DIR": str(config_dir),
    })
    if quote_token:
        env[QUOTE_TOKEN_ENV] = quote_token
    return env


def _parse_cli_result(completed, capability, *, confirm):
    stdout = str(getattr(completed, "stdout", "") or "")
    stderr = str(getattr(completed, "stderr", "") or "")
    if len(stdout.encode("utf-8")) > MAX_CLI_OUTPUT_BYTES:
        raise CLIExecutionError("cli_output_too_large", "CLI 返回内容过大", 502,
                                unknown_outcome=confirm)
    if int(getattr(completed, "returncode", 1) or 0) != 0:
        code = "cli_command_failed"
        try:
            error_body = json.loads(stdout or stderr)
            if isinstance(error_body, dict):
                candidate = str(error_body.get("error") or "")
                if re.fullmatch(r"[a-z][a-z0-9_]{0,79}", candidate):
                    code = candidate
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
        raise CLIExecutionError(code, "黄雀 CLI 调用失败", 502,
                                unknown_outcome=confirm)
    try:
        envelope = json.loads(stdout)
    except (TypeError, json.JSONDecodeError) as error:
        raise CLIExecutionError("cli_response_invalid", "CLI 返回格式无效", 502,
                                unknown_outcome=confirm) from error
    if (
        not isinstance(envelope, dict)
        or envelope.get("schema") != "hq.run/v1"
        or envelope.get("capability") != capability
        or not isinstance(envelope.get("result"), dict)
    ):
        raise CLIExecutionError("cli_response_invalid", "CLI 返回格式无效", 502,
                                unknown_outcome=confirm)
    return envelope["result"]


def execute(capability, input_body, *, username, web_token, scopes, confirm=False,
            quote_token="", auth_base=None, internal_token=None,
            ttl_seconds=DEFAULT_TOKEN_TTL, timeout=DEFAULT_TIMEOUT,
            http_open=None, run_process=None):
    """Run one allowlisted capability as the currently authenticated user."""
    capability = str(capability or "").strip()
    if capability not in ALLOWED_CAPABILITIES:
        raise CLIExecutionError("capability_not_allowed", "该 CLI 能力未向视频助手开放", 403)
    if not isinstance(input_body, dict):
        raise CLIExecutionError("input_invalid", "CLI 输入必须是 JSON 对象", 400)
    if confirm and not str(quote_token or "").strip():
        raise CLIExecutionError("quote_required", "确认生成需要有效报价", 409)
    if not confirm and quote_token:
        raise CLIExecutionError("quote_not_allowed", "报价令牌只能用于显式确认", 400)
    clean_quote_token = str(quote_token or "").strip()
    if confirm and not _QUOTE_TOKEN_RE.fullmatch(clean_quote_token):
        raise CLIExecutionError("quote_invalid", "确认生成的报价令牌无效", 400)
    try:
        timeout_budget = max(1.0, min(60.0, float(timeout)))
    except (TypeError, ValueError) as error:
        raise CLIExecutionError("cli_timeout_invalid", "黄雀 CLI 超时配置无效", 500) from error
    deadline = time.monotonic() + timeout_budget

    def remaining(cap, *, unknown_outcome=False):
        value = min(float(cap), deadline - time.monotonic())
        if value <= 0:
            raise CLIExecutionError(
                "cli_timeout",
                "黄雀 CLI 调用超时，结果状态未知" if unknown_outcome else "黄雀 CLI 调用超时",
                504, unknown_outcome=unknown_outcome,
            )
        return value

    base = _configured_auth_base(auth_base)
    # Resolve the module before minting a delegated credential.  Deployment
    # mistakes therefore fail without creating even a short-lived grant.
    module_root = _cli_module_root()
    internal = internal_token if internal_token is not None else os.getenv("HQ_INTERNAL_TOKEN", "")
    request_open = http_open or _SAFE_HTTP_OPEN
    runner = run_process or subprocess.run
    token = _delegate_token(
        auth_base=base, internal_token=internal, username=username,
        web_token=web_token, scopes=scopes,
        ttl_seconds=max(60, min(300, int(ttl_seconds))),
        timeout=remaining(15),
        http_open=request_open,
    )
    try:
        # The environment-token branch must never touch the credentials file.
        # Point it at a unique non-existent path so an accidental disk fallback
        # fails closed instead of reading a developer's shared CLI login.
        config_dir = Path(__file__).resolve().parents[2] / (
            ".hq-cli-ephemeral-" + secrets.token_hex(12)
        )
        try:
            argv = [
                sys.executable, "-m", "hq_cli", "run", capability,
                "--input", "@-", "--json",
            ]
            if confirm:
                argv.append("--confirm")
            try:
                process_timeout = remaining(timeout_budget)
                completed = runner(
                    argv,
                    input=json.dumps(input_body, ensure_ascii=False, separators=(",", ":")),
                    text=True, encoding="utf-8", errors="replace",
                    capture_output=True, timeout=process_timeout,
                    env=_minimal_child_env(
                        token, base, config_dir,
                        quote_token=clean_quote_token if confirm else "",
                        module_root=module_root,
                    ),
                    cwd=str(module_root),
                    shell=False,
                )
            except subprocess.TimeoutExpired as error:
                raise CLIExecutionError(
                    "cli_timeout", "黄雀 CLI 调用超时，结果状态未知" if confirm else "黄雀 CLI 调用超时",
                    504, unknown_outcome=confirm,
                ) from error
            except OSError as error:
                raise CLIExecutionError("cli_unavailable", "黄雀 CLI 暂时不可用", 503) from error
            return _parse_cli_result(completed, capability, confirm=confirm)
        finally:
            # Normally the path never exists.  Remove it only if it is still an
            # empty directory; never recurse through unexpected CLI output.
            try:
                config_dir.rmdir()
            except (FileNotFoundError, OSError):
                pass
    finally:
        revoke_budget = min(10.0, deadline - time.monotonic())
        if revoke_budget > 0:
            _revoke_token(
                base, token, timeout=revoke_budget, http_open=request_open
            )
