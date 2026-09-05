"""视觉描述通道：把用户上传的图片用视觉模型转成文字描述，注入主 Agent 上下文。

- 凭证走 config（VISION_* 显式覆盖，回落主 LLM 端点 + DeepSeek 视觉模型）；
- 任何失败返回空串，绝不影响主链路（图片本地路径仍会传给主 Agent 供黄雀上传用）。
"""
from __future__ import annotations

import base64
import mimetypes
import os

from .. import config

try:
    from openai import OpenAI
    _OPENAI_AVAILABLE = True
except ImportError:  # pragma: no cover
    _OPENAI_AVAILABLE = False

_MAX_BYTES = 5 * 1024 * 1024   # 视觉 API 常见 5MB 上限
_MAX_TEXT = 800                # 注入上下文的描述长度上限

_PROMPT = (
    "请用中文详细描述这张图片：画面主体、场景、人物、构图、风格，"
    "以及图中出现的所有文字（尽量原样转录）。300 字以内。"
)


def describe_image(path: str) -> str:
    """读取本地图片并用视觉模型生成中文描述；失败/不可用时返回空串。"""
    if not _OPENAI_AVAILABLE or not config.VISION_API_KEY:
        return ""
    if not path or not os.path.exists(path):
        return ""
    try:
        if os.path.getsize(path) > _MAX_BYTES:
            return ""
        mime = mimetypes.guess_type(path)[0] or "image/jpeg"
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        kwargs = {"api_key": config.VISION_API_KEY, "timeout": config.LLM_TIMEOUT}
        if config.VISION_BASE_URL:
            kwargs["base_url"] = config.VISION_BASE_URL
        if config.MAIN_LLM_PROXY:
            try:
                import httpx
                kwargs["http_client"] = httpx.Client(
                    proxy=config.MAIN_LLM_PROXY, trust_env=False, timeout=config.LLM_TIMEOUT)
            except Exception:
                pass
        resp = OpenAI(**kwargs).chat.completions.create(
            model=config.VISION_MODEL,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": _PROMPT},
                    {"type": "image_url",
                     "image_url": {"url": "data:%s;base64,%s" % (mime, b64)}},
                ],
            }],
            temperature=0.3,
        )
        text = (resp.choices[0].message.content or "").strip()
        return text[:_MAX_TEXT]
    except Exception:
        return ""
