"""Authenticated OpenAI-compatible model stream for Huangque CLI."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from .model_usage import ModelUsageError


ROUTE = "/model/v1/chat/completions"
PUBLIC_MODEL = "huangque-agent"
MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_OUTPUT_TOKENS = 8192


class ModelProxyError(RuntimeError):
    def __init__(self, status, detail, code):
        super().__init__(detail)
        self.status = int(status)
        self.detail = str(detail)
        self.code = str(code)


def _contains_image(value):
    if isinstance(value, dict):
        if value.get("type") in {"image", "image_url", "input_image"}:
            return True
        return any(_contains_image(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_image(item) for item in value)
    return False


class AccountModelProxy:
    def __init__(self, profile_agent, usage_guard):
        self.profile_agent = profile_agent
        self.usage_guard = usage_guard

    def _payload(self, body):
        if not isinstance(body, dict):
            raise ModelProxyError(400, "请求体必须是对象", "invalid_request")
        if body.get("model") != PUBLIC_MODEL or body.get("stream") is not True:
            raise ModelProxyError(400, "模型或流式参数无效", "invalid_request")
        messages = body.get("messages")
        if not isinstance(messages, list) or not 1 <= len(messages) <= 200:
            raise ModelProxyError(400, "消息格式无效", "invalid_messages")
        if _contains_image(messages):
            raise ModelProxyError(400, "黄雀账户模型暂不支持图片输入", "image_input_unsupported")
        tools = body.get("tools")
        if tools is not None and (not isinstance(tools, list) or len(tools) > 200):
            raise ModelProxyError(400, "工具列表无效", "invalid_tools")
        payload = dict(body)
        payload["model"] = self.profile_agent.model
        if isinstance(payload.get("max_tokens"), bool):
            raise ModelProxyError(400, "max_tokens 无效", "invalid_request")
        payload["max_tokens"] = min(
            MAX_OUTPUT_TOKENS,
            max(1, int(payload.get("max_tokens") or MAX_OUTPUT_TOKENS)),
        )
        payload.setdefault("thinking", {"type": "disabled"})
        return payload

    def stream(self, handler, user, client_ip, body):
        if not self.profile_agent.configured:
            raise ModelProxyError(503, "黄雀账户模型暂不可用", "model_unavailable")
        try:
            payload = self._payload(body)
        except (TypeError, ValueError) as exc:
            raise ModelProxyError(400, "max_tokens 无效", "invalid_request") from exc
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_REQUEST_BYTES:
            raise ModelProxyError(413, "模型请求过大", "request_too_large")
        try:
            lease = self.usage_guard.acquire(
                user["username"], client_ip, "huangque_cli_agent",
                len(encoded), payload["max_tokens"],
            )
        except ModelUsageError as exc:
            raise ModelProxyError(429, exc.detail, exc.code) from exc

        request = urllib.request.Request(
            self.profile_agent.base_url + "/chat/completions",
            data=encoded,
            headers={
                "Authorization": "Bearer " + self.profile_agent.api_key,
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
            method="POST",
        )
        success = False
        response_started = False
        try:
            with self.profile_agent.opener.open(request, timeout=180) as response:
                handler.send_response(response.getcode())
                handler.send_header(
                    "Content-Type",
                    response.headers.get("Content-Type") or "text/event-stream; charset=utf-8",
                )
                handler.send_header("Cache-Control", "no-store")
                handler.send_header("X-Accel-Buffering", "no")
                handler.send_header("Connection", "close")
                handler.end_headers()
                response_started = True
                while chunk := response.read(64 * 1024):
                    handler.wfile.write(chunk)
                    handler.wfile.flush()
                handler.close_connection = True
                success = True
        except urllib.error.HTTPError as exc:
            status = 429 if exc.code == 429 else 503
            raise ModelProxyError(status, "黄雀账户模型暂不可用", "model_upstream_error") from exc
        except (OSError, TimeoutError, urllib.error.URLError) as exc:
            if response_started:
                handler.close_connection = True
                return
            raise ModelProxyError(503, "黄雀账户模型暂不可用", "model_upstream_error") from exc
        finally:
            lease.finish(success)
