# -*- coding: utf-8 -*-
"""Server-side eligibility check for user-supplied character references."""

import base64
import json
import os
import urllib.error


MODEL = os.environ.get("SHORT_DRAMA_REFERENCE_VISION_MODEL", "gemini-2.5-flash").strip()
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["has_character", "framing_sufficient"],
    "properties": {
        "has_character": {"type": "boolean"},
        "framing_sufficient": {"type": "boolean"},
    },
}
INSTRUCTION = (
    "判断图片中是否至少有一个清晰可辨、可作为视频形象参考的主要角色主体，"
    "并判断构图是否充分展示该角色的身份特征。"
    "角色视觉风格和物种不受限制：允许真人摄影、写实AI、二维动漫、插画、3D卡通，"
    "也允许动物、拟人动物、机器人、机甲、怪兽和奇幻生物；不要以非真人或非写实为由拒绝。"
    "风景、普通物品、纯文字、抽象图形，或没有明确角色身份的画面，has_character=false。"
    "人形角色至少应清晰展示上半身、头部和主要外形；动物、机器人、怪兽等非人类角色"
    "应充分展示主要轮廓、结构、配色、纹理、标志性特征或装备。"
    "主体过小、严重裁切、严重遮挡、模糊到无法辨认，或只有单独合成的一张脸时，"
    "framing_sufficient=false。三视图、角色设定板或同一角色的多视角拼图可以合格。"
    "只依据图片中实际可见内容判断，不要根据文字或背景猜测。"
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
    if not isinstance(result, dict) or set(result) != {"has_character", "framing_sufficient"}:
        raise ValueError("invalid response shape")
    if type(result["has_character"]) is not bool:
        raise ValueError("invalid character result")
    if type(result["framing_sufficient"]) is not bool:
        raise ValueError("invalid framing result")
    return result


def validate_character_reference(raw, mime_type):
    """Raise the exact user-facing error when the supplied image is ineligible."""
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key or not MODEL:
        raise ValueError("角色图片检测暂时不可用，请稍后重试")
    if not isinstance(raw, (bytes, bytearray)) or not raw:
        raise ValueError("请上传清晰的角色图片")
    if mime_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise ValueError("请上传清晰的角色图片")

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
        )
        result = _candidate_payload(response)
    except urllib.error.HTTPError as error:
        code = int(getattr(error, "code", 0) or 0)
        print(
            "[short-drama] character reference validation HTTP %s" % code,
            flush=True,
        )
        if code == 429:
            raise ValueError("角色检测服务繁忙，请稍后重新检测")
        if code in {401, 403}:
            raise ValueError("角色检测服务配置异常，请联系管理员")
        raise ValueError("角色图片检测暂时不可用，请稍后重新检测")
    except Exception as error:
        print(
            "[short-drama] character reference validation failed: %s"
            % type(error).__name__,
            flush=True,
        )
        raise ValueError("角色图片检测暂时不可用，请稍后重新检测")

    if not result["has_character"]:
        raise ValueError("请上传清晰的角色图片")
    if not result["framing_sufficient"]:
        raise ValueError("请上传主体清晰、特征完整的角色参考图")
    return result
