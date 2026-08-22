"""Small browser-free PDF fallback for the isolated Agent preview."""

from __future__ import annotations

import math
import re
from pathlib import Path


def _plain_lines(markdown):
    lines = []
    for raw in str(markdown or "").splitlines():
        text = raw.strip().strip("|")
        if not text or re.fullmatch(r"[:|\- ]+", text):
            continue
        text = re.sub(r"^#{1,4}\s*", "", text)
        text = re.sub(r"^(?:[-*]|\d+[.)])\s+", "", text)
        text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
        text = re.sub(r"`([^`]+)`", r"\1", text)
        lines.append(text)
    return lines or ["暂无已确认内容。"]


def _wrap(text, font_name, font_size, max_width, pdfmetrics):
    result, current = [], ""
    for character in str(text):
        candidate = current + character
        if current and pdfmetrics.stringWidth(candidate, font_name, font_size) > max_width:
            result.append(current)
            current = character
        else:
            current = candidate
    if current:
        result.append(current)
    return result or [""]


def render_foundation_pdf_fallback(markdown, target):
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    font_name = "STSong-Light"
    try:
        pdfmetrics.getFont(font_name)
    except KeyError:
        pdfmetrics.registerFont(UnicodeCIDFont(font_name))

    width, height = A4
    margin_x, margin_top, margin_bottom = 48, 58, 44
    font_size, line_height = 9.2, 13.5
    wrapped = []
    for line in _plain_lines(markdown):
        wrapped.extend(_wrap(line, font_name, font_size, width - margin_x * 2, pdfmetrics))

    page_count = 8
    lines_per_page = max(1, math.ceil(len(wrapped) / page_count))
    maximum_lines = max(1, int((height - margin_top - margin_bottom - 24) / line_height))
    if lines_per_page > maximum_lines:
        raise RuntimeError("PDF fallback content is too long")

    document = canvas.Canvas(str(target), pagesize=A4, pageCompression=0)
    document.setTitle("IP 人设定位｜模块 1-4 初稿")
    for page_index in range(page_count):
        document.setFont(font_name, 12)
        document.drawString(margin_x, height - 36, "IP 人设定位｜模块 1-4 初稿")
        document.setFont(font_name, 8)
        document.drawRightString(width - margin_x, 24, "%d / %d" % (page_index + 1, page_count))
        document.setFont(font_name, font_size)
        y = height - margin_top
        start = page_index * lines_per_page
        for line in wrapped[start:start + lines_per_page]:
            document.drawString(margin_x, y, line)
            y -= line_height
        document.showPage()
    document.save()
    return target
