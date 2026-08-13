# -*- coding: utf-8 -*-
"""Server-side eligibility check for user-supplied character references."""

import base64
import json
import os
import urllib.error


MODEL = os.environ.get("SHORT_DRAMA_REFERENCE_VISION_MODEL", "gemini-2.5-flash").strip()
ACCEPTED_EXTENTS = {"half_body", "three_quarter", "full_body"}
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["has_real_person", "visible_extent"],
    "properties": {
        "has_real_person": {"type": "boolean"},
        "visible_extent": {
            "type": "string",
            "enum": [
                "head_only", "shoulders_only", "upper_body", "half_body",
                "three_quarter", "full_body", "unknown",
            ],
        },
    },
}
INSTRUCTION = (
    "判断图片中是否至少有一名清晰可辨的写实人类形象，并判断其中画面范围最大的一名人物的可见范围。"
    "合格人物可以来自真实摄影、写实影视画面或写实AI生成人物图；"
    "不包括动漫、插画、3D卡通、玩偶、雕像、人体模型，也不能只是单独合成的一张脸。"
    "三视图、角色设定板或同一人物的多视角拼图，只要至少一个视角清晰展示合格人物，就按该视角判断。"
    "visible_extent 必须严格选择：head_only=只有头部；shoulders_only=头肩照；"
    "upper_body=只到胸部；half_body=至少从头到腰/髋部；three_quarter=至少从头到膝部；"
    "full_body=从头到脚；unknown=无法可靠判断。不要依据服装、背景或文字猜测。"
)


def _candidate_payload(response):
    candidates = response.get("candidates") if isinstance(response, dict) else None
    parts = (
        candidates[0].get("content", {}).get("parts", [])
        if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict)
        else []
    )
    text = "".join(
        part.get("text", "") for part in parts if isinstance(part, dict)
    ).strip()
    if not text:
        raise ValueError("empty response")
    result = json.loads(text)
    if not isinstance(result, dict) or set(result) != {"has_real_person", "visible_extent"}:
        raise ValueError("invalid response shape")
    if type(result["has_real_person"]) is not bool:
        raise ValueError("invalid person result")
    if result["visible_extent"] not in SCHEMA["properties"]["visible_extent"]["enum"]:
        raise ValueError("invalid extent result")
    return result


def validate_character_reference(raw, mime_type):
    """Raise the exact user-facing error when the supplied image is ineligible."""
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key or not MODEL:
        raise ValueError("人物图片检测暂时不可用，请稍后重试")
    if not isinstance(raw, (bytes, bytearray)) or not raw:
        raise ValueError("请上传人物图")
    if mime_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise ValueError("请上传人物图")

    body = {
        "contents": [{"parts": [
            {"inlineData": {
                "mimeType": mime_type,
                "data": base64.b64encode(bytes(raw)).decode("ascii"),
            }},
            {"text": INSTRUCTION},
        ]}],
        "generationConfig": {
            "responseModalities": ["TEXT"],
            "temperature": 0,
            "maxOutputTokens": 120,
            "thinkingConfig": {"thinkingBudget": 0},
            "responseMimeType": "application/json",
            "responseJsonSchema": SCHEMA,
        },
    }
    official_base = os.environ.get(
        "GEMINI_OFFICIAL_BASE", "https://generativelanguage.googleapis.com"
    ).rstrip("/")
    fallback_base = os.environ.get(
        "GEMINI_BASE", "https://generativelanguage.googleapis.com"
    ).rstrip("/")
    try:
        from . import egress

        response = egress.post_json_idempotent(
            official_base,
            fallback_base,
            "/v1beta/models/%s:generateContent" % MODEL,
            json.dumps(body, ensure_ascii=False).encode("utf-8"),
            {"Content-Type": "application/json", "x-goog-api-key": api_key},
            max_attempts=4,
            retry_delays=(2, 5, 10),
        )
        result = _candidate_payload(response)
    except urllib.error.HTTPError as error:
        code = int(getattr(error, "code", 0) or 0)
        print(
            "[short-drama] character reference validation HTTP %s" % code,
            flush=True,
        )
        if code == 429:
            raise ValueError("人物检测服务繁忙，请稍后重新检测")
        if code in {401, 403}:
            raise ValueError("人物检测服务配置异常，请联系管理员")
        raise ValueError("人物图片检测暂时不可用，请稍后重新检测")
    except Exception as error:
        print(
            "[short-drama] character reference validation failed: %s"
            % type(error).__name__,
            flush=True,
        )
        raise ValueError("人物图片检测暂时不可用，请稍后重新检测")

    if not result["has_real_person"]:
        raise ValueError("请上传人物图")
    if result["visible_extent"] not in ACCEPTED_EXTENTS:
        raise ValueError("请上传至少包含半身的人物图")
    return result
