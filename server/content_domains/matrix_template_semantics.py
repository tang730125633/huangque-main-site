"""AI semantic-boundary hints for server-owned matrix template layout."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request


MODEL = os.environ.get(
    "MATRIX_TEMPLATE_SEMANTIC_MODEL", "gpt-4.1-mini"
).strip() or "gpt-4.1-mini"
OPENAI_BASE = os.environ.get("OPENAI_BASE", "https://api.openai.com").rstrip("/")
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
VERSION = 1
CACHE_SECONDS = 10 * 60
CACHE_LIMIT = 512
_CACHE: dict[str, tuple[float, dict]] = {}
_LOCK = threading.Lock()
_KEY_LOCKS = tuple(threading.Lock() for _ in range(32))
_OBVIOUS_BOUNDARY = frozenset("，。！？；：、,.!?;:|｜ \t")
_NUMERIC_PHRASE_RE = re.compile(
    r"(?:(?<![0-9])(?:"
    r"[0-9]{1,3}(?:[,，][0-9]{3})+(?:[.．][0-9]+)?"
    r"|[0-9]+(?:[.．][0-9]+)?"
    r")(?![0-9])|[零〇一二三四五六七八九十百千万亿两几]+)"
    r"\s*[十百千万亿个家人位名条款套种项台年月日天次岁]{0,2}"
)


def _chat_url() -> str:
    return (
        OPENAI_BASE + "/chat/completions"
        if OPENAI_BASE.endswith("/v1")
        else OPENAI_BASE + "/v1/chat/completions"
    )


def _source_sha256(top: str, bottom: str) -> str:
    return hashlib.sha256((top + "\0" + bottom).encode("utf-8")).hexdigest()


def _indexed(value: str) -> str:
    return " | ".join(f"{index}:{char!r}" for index, char in enumerate(value))


def _obvious_breaks(value: str) -> set[int]:
    return {
        index for index, char in enumerate(value[:-1])
        if char in _OBVIOUS_BOUNDARY
    }


def _break_splits_number_phrase(value: str, index: int) -> bool:
    boundary = index + 1
    return any(
        match.start() < boundary < match.end()
        for match in _NUMERIC_PHRASE_RE.finditer(value)
    )


def _normalize_breaks(raw, value: str) -> list[int]:
    if not isinstance(raw, list) or len(raw) > 64:
        raise RuntimeError("AI 语义断点格式无效")
    if any(isinstance(item, bool) or not isinstance(item, int) for item in raw):
        raise RuntimeError("AI 语义断点格式无效")
    values = {
        item for item in raw if 0 <= item < len(value) - 1
    }
    values.update(_obvious_breaks(value))
    return sorted(
        item for item in values
        if not _break_splits_number_phrase(value, item)
    )


def _nearest_safe_top1_end(raw, breaks: list[int], top: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise RuntimeError("AI top1 语义边界无效")
    if raw >= len(top) - 1:
        return len(top) - 1
    while raw + 1 < len(top) - 1 and top[raw + 1] in _OBVIOUS_BOUNDARY:
        raw += 1
    candidates = [item for item in breaks if item >= max(0, raw)]
    if not candidates:
        raise RuntimeError("AI top1 语义边界无效")
    return candidates[0]


def _prompt(top: str, bottom: str, contract: dict) -> str:
    layers = contract.get("layers") or {}
    top1 = layers.get("top1") or {}
    top2 = layers.get("top2") or {}
    top3 = layers.get("top3") or {}
    bottom2 = layers.get("bottom2") or {}
    top3_line = (
        f"\n- top3: {int(top3.get('font_size_px'))}px，"
        f"可用宽 {int(top3.get('max_width_px') or contract.get('max_width_px') or 996)}px，最多 "
        f"{int(top3.get('max_lines'))} 行，用于更小的补充说明。"
        if top3 else ""
    )
    return f"""
你是中文短视频的语义边界标注器。你不能重写文案，只能返回字符索引。

模板文字区宽 {int(contract.get('max_width_px') or 996)}px：
- top1: {int(top1.get('font_size_px') or 86)}px，可用宽 {int(top1.get('max_width_px') or contract.get('max_width_px') or 996)}px，最多 {int(top1.get('max_lines') or 2)} 行，用于开场钩子。
- top2: {int(top2.get('font_size_px') or 62)}px，可用宽 {int(top2.get('max_width_px') or contract.get('max_width_px') or 996)}px，最多 {int(top2.get('max_lines') or 4)} 行，用于具体说明。{top3_line}
- bottom2: {int(bottom2.get('font_size_px') or 78)}px，可用宽 {int(bottom2.get('max_width_px') or contract.get('max_width_px') or 996)}px，最多 {int(bottom2.get('max_lines') or 2)} 行。

请返回：
1. top1_end：top1 最后一个字符的索引（包含）；其余顶部文案由生成端按完整分句分配到 top2，有 top3 时也可分配到 top3。
2. top_break_after：顶部文案中所有可安全换行的索引。
3. bottom_break_after：底部文案中所有可安全换行的索引。

只标记完整语义边界，不要过度细分。禁止在地名、行业名、多层名词短语、动宾短语、数字组合、列举项和短 CTA 内部标记。

顶部原文索引：{_indexed(top)}
底部原文索引：{_indexed(bottom)}

只输出 JSON：
{{"top1_end":0,"top_break_after":[0],"bottom_break_after":[0]}}
""".strip()


def _request(top: str, bottom: str, contract: dict, *, previous=None, feedback="") -> dict:
    if not OPENAI_KEY:
        raise RuntimeError("AI 语义排版模型密钥未配置")
    messages = [
        {"role": "system", "content": _prompt(top, bottom, contract)},
        {"role": "user", "content": "请标注语义边界。"},
    ]
    if previous is not None and feedback:
        messages.extend([
            {"role": "assistant", "content": json.dumps(previous, ensure_ascii=False)},
            {"role": "user", "content": "上一结果未通过真实字体排版校验：" + str(feedback)[:300] + "。请修正索引，不要重写文案。"},
        ])
    body = json.dumps({
        "model": MODEL,
        "messages": messages,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "max_tokens": 300,
    }, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        _chat_url(), data=body,
        headers={
            "Authorization": "Bearer " + OPENAI_KEY,
            "Content-Type": "application/json",
        }, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.load(response)
        return json.loads(payload["choices"][0]["message"]["content"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("AI 语义排版返回无效") from exc
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"AI 语义排版请求失败（HTTP {exc.code}）") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError("AI 语义排版服务连接失败") from exc


def generate(top: str, bottom: str, contract: dict, *, previous=None, feedback="") -> dict:
    raw = _request(top, bottom, contract, previous=previous, feedback=feedback)
    top_breaks = _normalize_breaks(raw.get("top_break_after"), top)
    bottom_breaks = _normalize_breaks(raw.get("bottom_break_after"), bottom)
    top1_end = _nearest_safe_top1_end(raw.get("top1_end"), top_breaks, top)
    return {
        "version": VERSION,
        "model": MODEL,
        "source_sha256": _source_sha256(top, bottom),
        "top1_end": top1_end,
        "top_break_after": top_breaks,
        "bottom_break_after": bottom_breaks,
    }


def cache_key(top: str, bottom: str, template_id: str, contract: dict) -> str:
    payload = json.dumps(
        [VERSION, MODEL, template_id, top, bottom, contract],
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _remember(key: str, value: dict) -> None:
    with _LOCK:
        if len(_CACHE) >= CACHE_LIMIT:
            oldest = min(_CACHE, key=lambda item: _CACHE[item][0])
            _CACHE.pop(oldest, None)
        _CACHE[key] = (time.monotonic(), dict(value))


def resolve(top: str, bottom: str, template_id: str, contract: dict,
            validator) -> tuple[dict, object | None]:
    key = cache_key(top, bottom, template_id, contract)
    key_lock = _KEY_LOCKS[int(key[:8], 16) % len(_KEY_LOCKS)]
    with key_lock:
        cached_value = None
        with _LOCK:
            now = time.monotonic()
            item = _CACHE.get(key)
            if item is not None and now - item[0] <= CACHE_SECONDS:
                cached_value = dict(item[1])
            _CACHE.pop(key, None)
        if cached_value is not None:
            accepted, result = validator(cached_value)
            if accepted:
                _remember(key, cached_value)
                return cached_value, result
            previous, feedback, attempts = cached_value, str(result), 2
        else:
            previous, feedback, attempts = None, "", 3
        for _attempt in range(attempts):
            value = generate(
                top, bottom, contract,
                previous=previous, feedback=feedback,
            )
            accepted, result = validator(value)
            if accepted:
                _remember(key, value)
                return value, result
            previous, feedback = value, str(result or "语义排版校验失败")
        raise RuntimeError("AI 语义排版经两次修复后仍未通过真实字体校验")
