# -*- coding: utf-8 -*-
"""Shared, auditable response contract for every leads service entry point."""
import hashlib
import re
import time


def leads_cost(count, pages):
    count = max(1, min(30, int(count or 12)))
    pages = max(1, min(3, int(pages or 1)))
    return 6 + (count * pages) // 4


def validate_compliance(payload):
    accepted_at = int((payload or {}).get("compliance_accepted_at") or 0)
    now = int(time.time())
    if (payload or {}).get("compliance_version") != "leads-v1" or not accepted_at:
        raise ValueError("请先确认获客数据合规要求")
    if accepted_at < now - 600 or accepted_at > now + 300:
        raise ValueError("合规确认已过期，请重新确认")
    return True


def _text_key(text):
    return re.sub(r"\s+", "", re.sub(r"\[[^\]]+\]", "", text or "")).strip().lower()


def _identity(comment):
    source_comment_id = str(comment.get("comment_id") or "").strip()
    if source_comment_id:
        return "%s:comment:%s" % (comment.get("platform") or "", source_comment_id)
    return "%s:%s:%s:%s" % (
        comment.get("platform") or "",
        comment.get("source_id") or comment.get("video_url") or "",
        comment.get("user_id") or comment.get("nickname") or "",
        _text_key(comment.get("content")),
    )


def _legacy_identity(comment):
    return "%s:%s:%s" % (
        comment.get("platform") or "",
        comment.get("user_id") or comment.get("nickname") or "",
        _text_key(comment.get("content")),
    )


def _lead_id(comment):
    return hashlib.sha1(_identity(comment).encode("utf-8")).hexdigest()[:16]


def _timestamp(value):
    try:
        value = int(value or 0)
        if value > 10_000_000_000:
            value //= 1000
        return value if value > 0 else None
    except (TypeError, ValueError):
        return None


def classify_comments(raw, is_spam, is_high, intent_profile):
    """Classify comments and preserve a complete accounting equation.

    total = leads_count + spam + chat + deduped + empty
    """
    leads, spam, chat, deduped, empty, seen = [], 0, 0, 0, 0, set()
    collected_at = int(time.time())
    for comment in raw:
        text = (comment.get("content") or "").strip()
        if not text:
            empty += 1
            continue
        if is_spam(text):
            spam += 1
            continue
        if len(_text_key(text)) < 2:
            chat += 1
            continue
        if not is_high(text):
            chat += 1
            continue
        identity = _identity(comment)
        if identity in seen:
            deduped += 1
            continue
        seen.add(identity)
        profile = intent_profile(text) or {}
        item = dict(comment)
        item.update(profile)
        item.update({
            "lead_id": _lead_id(comment),
            "legacy_lead_id": hashlib.sha1(_legacy_identity(comment).encode("utf-8")).hexdigest()[:16],
            "source_comment_id": comment.get("comment_id"),
            "comment_time": _timestamp(comment.get("time")),
            "collected_at": collected_at,
            "follow_status": "待跟进",
            "follow_note": "",
        })
        leads.append(item)
    leads.sort(key=lambda item: (
        item.get("comment_time") or 0,
        len(item.get("content") or ""),
        item.get("like_count") or 0,
    ), reverse=True)
    return {
        "leads": leads,
        "leads_count": len(leads),
        "spam": spam,
        "chat": chat,
        "deduped": deduped,
        "empty": empty,
        "total": len(raw),
        "collected_at": collected_at,
    }
