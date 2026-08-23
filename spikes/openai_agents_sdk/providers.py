from __future__ import annotations

import os
from typing import Any


def build_b1_openai_responses() -> tuple[Any | None, dict[str, Any]]:
    key = os.environ.get("OPENAI_API_KEY")
    meta = {"provider": "openai", "api": "responses", "model": "gpt-5.6-terra"}
    if not key:
        return None, {**meta, "status": "provider_blocked"}
    from agents import AsyncOpenAI, OpenAIResponsesModel

    client = AsyncOpenAI(api_key=key)
    return OpenAIResponsesModel(model=meta["model"], openai_client=client), {
        **meta, "status": "ready",
    }


def build_b2_dashscope_chat_completions() -> tuple[Any | None, dict[str, Any]]:
    key = os.environ.get("DASHSCOPE_API_KEY")
    meta = {
        "provider": "dashscope", "api": "chat_completions", "model": "qwen-plus",
        "limitations": [
            "no OpenAI Responses continuation semantics",
            "OpenAI trace export disabled without an OpenAI key",
            "provider-specific streaming/structured output behavior requires live verification",
        ],
    }
    if not key:
        return None, {**meta, "status": "provider_blocked"}
    from agents import AsyncOpenAI, OpenAIChatCompletionsModel
    client = AsyncOpenAI(
        api_key=key,
        base_url=os.environ.get(
            "DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
        ),
    )
    return OpenAIChatCompletionsModel(model=meta["model"], openai_client=client), {
        **meta, "status": "ready",
    }
