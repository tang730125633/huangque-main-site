# -*- coding: utf-8 -*-
"""Stable prompt references shared by image and video APIs."""
import re


IMAGE_MENTION_RE = re.compile(r"@(?:图片|图)(\d+)")


def validate_image_mentions(prompt, reference_count):
    prompt = str(prompt or "")
    count = max(0, int(reference_count or 0))
    for match in IMAGE_MENTION_RE.finditer(prompt):
        index = int(match.group(1))
        if index < 1:
            raise ValueError("参考图编号从1开始，请使用 @图片1")
        if index > count:
            raise ValueError(
                "提示词引用了 @图片%d，但当前只有 %d 张参考图" % (index, count)
            )
    return prompt


def resolve_image_mentions(prompt, reference_count, style="generic"):
    prompt = validate_image_mentions(prompt, reference_count)
    if style == "xai":
        return IMAGE_MENTION_RE.sub(
            lambda match: "<IMAGE_%s>" % match.group(1), prompt
        )
    if style == "omni":
        return IMAGE_MENTION_RE.sub(
            lambda match: "<IMAGE_REF_%d>" % (int(match.group(1)) - 1), prompt
        )
    return IMAGE_MENTION_RE.sub(
        lambda match: "第%s张参考图" % match.group(1), prompt
    )
