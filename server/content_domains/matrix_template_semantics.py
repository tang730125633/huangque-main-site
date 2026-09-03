"""AI semantic-boundary hints for server-owned matrix template layout."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.request


MODEL = os.environ.get(
    "MATRIX_TEMPLATE_SEMANTIC_MODEL", "gpt-4.1-mini"
).strip() or "gpt-4.1-mini"
REPAIR_MODEL = os.environ.get(
    "MATRIX_TEMPLATE_SEMANTIC_REPAIR_MODEL", "gpt-4.1"
).strip() or "gpt-4.1"
OPENAI_BASE = os.environ.get("OPENAI_BASE", "https://api.openai.com").rstrip("/")
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
VERSION = 1
CACHE_SECONDS = 10 * 60
CACHE_LIMIT = 512
MAX_TOP1_REBALANCE_CANDIDATES = 3
CONNECTION_RETRY_DELAYS_SECONDS = (1, 2)
_RETRYABLE_TRANSPORT_ERRORS = (
    urllib.error.URLError,
    TimeoutError,
    ConnectionError,
    http.client.IncompleteRead,
    ssl.SSLEOFError,
)
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
    candidates = list(breaks)
    if not candidates:
        raise RuntimeError("AI top1 语义边界无效")
    return min(candidates, key=lambda item: (abs(item - raw), item > raw, item))


def _earlier_top1_candidates(value: dict, top: str) -> list[dict]:
    current = value.get("top1_end")
    breaks = value.get("top_break_after")
    if (
        isinstance(current, bool) or not isinstance(current, int)
        or not isinstance(breaks, list)
    ):
        return []
    candidates = []
    for boundary in reversed([
        item for item in breaks
        if (
            not isinstance(item, bool) and isinstance(item, int)
            and 0 <= item < current - 1
            and not _break_splits_number_phrase(top, item)
        )
    ]):
        candidate = dict(value)
        candidate["top1_end"] = boundary
        candidate["top_break_after"] = list(breaks)
        candidate["bottom_break_after"] = list(
            value.get("bottom_break_after") or []
        )
        candidates.append(candidate)
        if len(candidates) >= MAX_TOP1_REBALANCE_CANDIDATES:
            break
    return candidates


def _longest_segment(value: dict, text: str, key: str) -> tuple[int, int]:
    breaks = value.get(key)
    if not isinstance(breaks, list):
        return (0, max(0, len(text) - 1))
    boundaries = [-1] + sorted({
        item for item in breaks
        if (
            not isinstance(item, bool) and isinstance(item, int)
            and 0 <= item < len(text) - 1
        )
    }) + [len(text) - 1]
    return max(
        (
            (boundaries[index - 1] + 1, boundaries[index])
            for index in range(1, len(boundaries))
        ),
        key=lambda span: span[1] - span[0],
    )


def _repair_feedback(value: dict, feedback: str, top: str, bottom: str) -> str:
    top_span = _longest_segment(value, top, "top_break_after")
    bottom_span = _longest_segment(value, bottom, "bottom_break_after")
    top_text = top[top_span[0]:top_span[1] + 1]
    bottom_text = bottom[bottom_span[0]:bottom_span[1] + 1]
    return (
        f"{feedback}。上一结果已失败，不得原样重复。"
        f"顶部最长块为索引 {top_span[0]}-{top_span[1]}，"
        f"原文为 {top_text!r}；底部最长块为索引 "
        f"{bottom_span[0]}-{bottom_span[1]}，原文为 {bottom_text!r}。"
        "若 top1 已足够短但 top2/top3 仍放不下，"
        "必须在过长语义块内新增完整短语边界。"
        "严禁拆开任何双字词、地名、行业词、动宾短语或复合名词；"
        "只能在语法成分或完整短语之间断开。"
    )


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
1. top1_end：top1 最后一个字符的索引（包含）；其余顶部文案由生成端按完整分句分配到 top2，有 top3 时也可分配到 top3。top1 必须能在自己的字号、宽度和行数内独立排下；不确定时宁可选更早的完整语义边界。
2. top_break_after：顶部文案中所有可安全换行的索引。
3. bottom_break_after：底部文案中所有可安全换行的索引。

只标记完整语义边界，不要过度细分。禁止在地名、行业名、多层名词短语、动宾短语、数字组合、列举项和短 CTA 内部标记。

顶部原文索引：{_indexed(top)}
底部原文索引：{_indexed(bottom)}

只输出 JSON：
{{"top1_end":0,"top_break_after":[0],"bottom_break_after":[0]}}
""".strip()


def _repair_prompt(top: str, bottom: str, contract: dict,
                   previous: dict, feedback: str) -> str:
    layers = contract.get("layers") or {}
    layer_lines = []
    for name in ("top1", "top2", "top3", "bottom2"):
        item = layers.get(name)
        if not isinstance(item, dict):
            continue
        layer_lines.append(
            f"- {name}: {int(item.get('font_size_px') or 0)}px，"
            f"可用宽 {int(item.get('max_width_px') or contract.get('max_width_px') or 996)}px，"
            f"最多 {int(item.get('max_lines') or 1)} 行。"
        )
    return f"""
你是中文短视频语义分块器。上一个索引方案未通过真实字体排版，现在改为先拆完整短语，再由程序换算索引。

模板区域：
{chr(10).join(layer_lines)}

硬规则：
1. 不得增删、替换或调序任何非空白字符；数组连接后必须逐字等于原文。
2. 每块必须是完整词组或语法成分，严禁拆开双字词、地名、行业词、动宾短语、数字组合、英文词和复合名词。
3. 顶部每块原则上 2-8 个可见字符；任何可继续按语法成分拆分的长块都不得超过 10 个字符。
4. 每个 `|`、`｜` 或句末标点后必须结束当前块，分隔符必须保留。
5. 先拆主语、地点状语、谓语、宾语和并列项，不要把整句当成一个块。
6. top1_chunk_count 表示前几个 top_chunks 放入 top1，其余由排版器分配到 top2/top3。
7. 上一结果不得原样重复。

示例：
原文“老周在深圳组建了人工智能创业团队|每天交流项目”
应拆为 ["老周","在深圳","组建了","人工智能","创业团队|","每天","交流项目"]。

顶部原文：{top}
底部原文：{bottom}
上一结果：{json.dumps(previous, ensure_ascii=False)}
校验反馈：{feedback[:500]}

只输出 JSON：
{{"top_chunks":["原文片段"],"bottom_chunks":["原文片段"],"top1_chunk_count":1}}
""".strip()


def _align_chunks(raw, source: str, label: str) -> list[str]:
    if not isinstance(raw, list) or not 1 <= len(raw) <= 64:
        raise RuntimeError(f"AI {label}语义块格式无效")
    if any(not isinstance(item, str) or not item for item in raw):
        raise RuntimeError(f"AI {label}语义块格式无效")
    aligned = []
    cursor = 0
    for raw_chunk in raw:
        chunk = raw_chunk
        if source.startswith(chunk, cursor):
            aligned.append(chunk)
            cursor += len(chunk)
            continue
        gap_end = cursor
        while gap_end < len(source) and source[gap_end].isspace():
            gap_end += 1
        if gap_end == cursor or not source.startswith(chunk, gap_end):
            raise RuntimeError(f"AI {label}语义块改写了原文")
        gap = source[cursor:gap_end]
        if aligned:
            aligned[-1] += gap
            aligned.append(chunk)
        else:
            aligned.append(gap + chunk)
        cursor = gap_end + len(chunk)
    trailing = source[cursor:]
    if trailing:
        if not trailing.isspace():
            raise RuntimeError(f"AI {label}语义块未覆盖完整原文")
        aligned[-1] += trailing
    if "".join(aligned) != source:
        raise RuntimeError(f"AI {label}语义块与原文不一致")
    return aligned


def _chunk_layout(raw: dict, top: str, bottom: str, model: str) -> dict:
    if not isinstance(raw, dict):
        raise RuntimeError("AI 语义分块返回无效")
    top_chunks = _align_chunks(raw.get("top_chunks"), top, "顶部")
    bottom_chunks = _align_chunks(raw.get("bottom_chunks"), bottom, "底部")
    top1_count = raw.get("top1_chunk_count")
    if (
        isinstance(top1_count, bool) or not isinstance(top1_count, int)
        or not 1 <= top1_count <= len(top_chunks)
    ):
        raise RuntimeError("AI top1 语义块数无效")

    def break_after(chunks):
        cursor = 0
        result = []
        for chunk in chunks[:-1]:
            cursor += len(chunk)
            result.append(cursor - 1)
        return result

    top_breaks = _normalize_breaks(break_after(top_chunks), top)
    bottom_breaks = _normalize_breaks(break_after(bottom_chunks), bottom)
    requested_top1_end = sum(len(item) for item in top_chunks[:top1_count]) - 1
    top1_end = _nearest_safe_top1_end(requested_top1_end, top_breaks, top)
    return {
        "version": VERSION,
        "model": model,
        "source_sha256": _source_sha256(top, bottom),
        "top1_end": top1_end,
        "top_break_after": top_breaks,
        "bottom_break_after": bottom_breaks,
    }


def _request(top: str, bottom: str, contract: dict, *, previous=None,
             feedback="", model=None, repair=False) -> dict:
    if not OPENAI_KEY:
        raise RuntimeError("AI 语义排版模型密钥未配置")
    if repair:
        messages = [
            {"role": "system", "content": _repair_prompt(
                top, bottom, contract, previous or {}, str(feedback or ""),
            )},
            {"role": "user", "content": "请按原文分成完整语义短语块。"},
        ]
    else:
        messages = [
            {"role": "system", "content": _prompt(top, bottom, contract)},
            {"role": "user", "content": "请标注语义边界。"},
        ]
    selected_model = str(model or MODEL).strip() or MODEL
    body = json.dumps({
        "model": selected_model,
        "messages": messages,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "max_tokens": 500 if repair else 300,
    }, ensure_ascii=False).encode("utf-8")
    attempts = len(CONNECTION_RETRY_DELAYS_SECONDS) + 1
    for attempt in range(attempts):
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
            raise RuntimeError(
                f"AI 语义排版请求失败（HTTP {exc.code}）"
            ) from exc
        except _RETRYABLE_TRANSPORT_ERRORS as exc:
            if attempt >= len(CONNECTION_RETRY_DELAYS_SECONDS):
                raise RuntimeError("AI 语义排版服务连接失败") from exc
            delay = CONNECTION_RETRY_DELAYS_SECONDS[attempt]
            print(
                "[matrix-template-semantic-retry] "
                f"attempt={attempt + 2}/{attempts} delay={delay}s",
                flush=True,
            )
            time.sleep(delay)
    raise RuntimeError("AI 语义排版服务连接失败")


def generate(top: str, bottom: str, contract: dict, *, previous=None,
             feedback="", model=None, repair=False) -> dict:
    selected_model = str(model or MODEL).strip() or MODEL
    raw = _request(
        top, bottom, contract, previous=previous, feedback=feedback,
        model=selected_model, repair=repair,
    )
    if repair:
        return _chunk_layout(raw, top, bottom, selected_model)
    top_breaks = _normalize_breaks(raw.get("top_break_after"), top)
    bottom_breaks = _normalize_breaks(raw.get("bottom_break_after"), bottom)
    top1_end = _nearest_safe_top1_end(raw.get("top1_end"), top_breaks, top)
    return {
        "version": VERSION,
        "model": selected_model,
        "source_sha256": _source_sha256(top, bottom),
        "top1_end": top1_end,
        "top_break_after": top_breaks,
        "bottom_break_after": bottom_breaks,
    }


def cache_key(top: str, bottom: str, template_id: str, contract: dict) -> str:
    payload = json.dumps(
        [VERSION, MODEL, REPAIR_MODEL, template_id, top, bottom, contract],
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
        tested = set()

        def validate_with_rebalance(value, prior_feedback=""):
            last_feedback = str(prior_feedback or "语义排版校验失败")
            for candidate in [value, *_earlier_top1_candidates(value, top)]:
                signature = json.dumps(
                    candidate, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"),
                )
                if signature in tested:
                    continue
                tested.add(signature)
                accepted, result = validator(candidate)
                if accepted:
                    return candidate, result, ""
                last_feedback = str(result or last_feedback)
            return None, None, last_feedback

        if cached_value is not None:
            accepted_value, result, feedback = validate_with_rebalance(
                cached_value
            )
            if accepted_value is not None:
                _remember(key, accepted_value)
                return accepted_value, result
            previous, attempts = cached_value, 2
        else:
            previous, feedback, attempts = None, "", 3
        for _attempt in range(attempts):
            selected_model = MODEL if previous is None else REPAIR_MODEL
            value = generate(
                top, bottom, contract,
                previous=previous, feedback=feedback,
                model=selected_model,
                repair=previous is not None,
            )
            accepted_value, result, feedback = validate_with_rebalance(
                value, feedback
            )
            if accepted_value is not None:
                _remember(key, accepted_value)
                return accepted_value, result
            previous = value
            feedback = _repair_feedback(value, feedback, top, bottom)
        raise RuntimeError("AI 语义排版经两次修复后仍未通过真实字体校验")
