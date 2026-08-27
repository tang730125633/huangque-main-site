"""Small browser-free PDF fallback for the isolated Agent preview."""

from __future__ import annotations

import math
import os
import re
from pathlib import Path


def _styled_lines(markdown):
    lines = []
    for raw in str(markdown or "").splitlines():
        text = raw.strip()
        if not text or re.fullmatch(r"[:|\- ]+", text):
            continue
        kind = "body"
        if text.startswith("## "):
            kind, text = "section", text[3:]
        elif text.startswith("#### "):
            kind, text = "card_title", text[5:]
            if any(label in text for label in (
                "情绪曲线", "开场钩子", "可拆选题", "使用边界", "表达边界",
                "适用场景", "金句", "内容支柱", "具体动作", "建议产出", "验证方式",
            )):
                kind = "card_detail"
        elif text.startswith("### "):
            kind, text = "subsection", text[4:]
            if "AI包装建议" in text or "执行优先级" in text:
                kind = "advice"
        elif text.startswith("> "):
            kind, text = "quote", text[2:]
        elif text.startswith("|") and text.endswith("|"):
            kind = "table"
            text = "  ｜  ".join(cell.strip() for cell in text.strip("|").split("|"))
        elif re.match(r"^(?:[-*]|\d+[.)])\s+", text):
            kind = "list"
            text = "• " + re.sub(r"^(?:[-*]|\d+[.)])\s+", "", text)
        else:
            text = re.sub(r"^#{1,4}\s*", "", text)
        text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
        text = re.sub(r"`([^`]+)`", r"\1", text)
        if kind == "body" and re.match(r"(?:🥇|🥈|🥉|P[012]\b)", text):
            kind = "priority"
        lines.append((kind, text))
    return lines or [("body", "暂无已确认内容。")]


def _wrap(text, font_name, font_size, max_width, pdfmetrics):
    result, current = [], ""
    for character in str(text):
        candidate = current + character
        if current and pdfmetrics.stringWidth(candidate, font_name, font_size) > max_width:
            result.append(current)
            if character in "，。；：、）】”’！？" and result:
                result[-1] += character
                current = ""
            else:
                current = character
        else:
            current = candidate
    if current:
        result.append(current)
    return result or [""]


def render_foundation_pdf_fallback(markdown, target):
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas

    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    font_name = "HermesFoundationCJK"
    header_font_name = "HermesFoundationHeaderCJK"
    font_candidates = [
        os.getenv("HERMES_PDF_FONT_PATH", ""),
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    ]
    for font_path in font_candidates:
        if not font_path or not Path(font_path).is_file():
            continue
        try:
            pdfmetrics.registerFont(TTFont(font_name, font_path))
            pdfmetrics.registerFont(TTFont(header_font_name, font_path))
            break
        except Exception:
            continue
    else:
        raise RuntimeError("PDF fallback requires an embeddable CJK font")

    width, height = A4
    margin_x, margin_top, margin_bottom = 48, 76, 46
    font_size, line_height = 10.5, 17.4
    wrapped = []
    for kind, line in _styled_lines(markdown):
        line_size = (
            10.2 if kind == "section" else
            10.0 if kind in {"card_title", "advice"} else
            10.5 if kind == "card_detail" else
            10.6 if kind == "subsection" else
            9.4 if kind == "table" else font_size
        )
        for segment in _wrap(line, font_name, line_size, width - margin_x * 2 - (10 if kind in {"quote", "list"} else 0), pdfmetrics):
            wrapped.append((kind, segment))

    maximum_lines = max(1, int((height - margin_top - margin_bottom - 24) / line_height))
    # Keep the concise editorial report dense enough to read like a proposal,
    # while still leaving room for the appendix and final confirmation page.
    page_count = max(6, math.ceil(len(wrapped) / maximum_lines))
    if page_count > 10:
        raise RuntimeError("PDF fallback content is too long")
    lines_per_page = max(1, math.ceil(len(wrapped) / page_count))

    document = canvas.Canvas(str(target), pagesize=A4, pageCompression=1)
    document.setTitle("IP 人设定位｜模块 1-4 初稿")
    for page_index in range(page_count):
        document.setFillColorRGB(0.965, 0.975, 0.99)
        document.rect(0, 0, width, height, stroke=0, fill=1)
        document.setFillColorRGB(0.09, 0.24, 0.47)
        document.rect(0, height - 52, width, 52, stroke=0, fill=1)
        document.setFillColorRGB(1, 1, 1)
        document.setFont(header_font_name, 12)
        document.drawString(margin_x, height - 33, "IP 人设定位 | 模块 1-4 初稿")
        document.setStrokeColorRGB(0.78, 0.83, 0.9)
        document.line(margin_x, 36, width - margin_x, 36)
        document.setFillColorRGB(0.35, 0.41, 0.49)
        document.setFont("Helvetica", 8)
        document.drawRightString(width - margin_x, 22, "%d / %d" % (page_index + 1, page_count))
        y = height - margin_top
        start = page_index * lines_per_page
        for kind, line in wrapped[start:start + lines_per_page]:
            if kind == "section":
                document.setFillColorRGB(0.89, 0.93, 0.98)
                document.roundRect(margin_x - 5, y - 3, width - margin_x * 2 + 10, line_height, 3, stroke=0, fill=1)
                document.setFillColorRGB(0.09, 0.24, 0.47)
                document.setFont(font_name, 10.2)
            elif kind == "card_title":
                document.setFillColorRGB(0.14, 0.31, 0.56)
                document.roundRect(margin_x - 5, y - 3, width - margin_x * 2 + 10, line_height, 3, stroke=0, fill=1)
                document.setFillColorRGB(1, 1, 1)
                document.setFont(font_name, 10.0)
            elif kind == "card_detail":
                document.setFillColorRGB(0.16, 0.29, 0.46)
                document.setFont(font_name, 10.5)
            elif kind == "advice":
                document.setFillColorRGB(0.99, 0.95, 0.82)
                document.roundRect(margin_x - 5, y - 3, width - margin_x * 2 + 10, line_height, 3, stroke=0, fill=1)
                document.setFillColorRGB(0.51, 0.35, 0.05)
                document.setFont(font_name, 10.0)
            elif kind == "subsection":
                document.setFillColorRGB(0.16, 0.29, 0.46)
                document.setFont(font_name, 10.6)
            elif kind == "table":
                document.setFillColorRGB(0.94, 0.96, 0.985)
                document.rect(margin_x - 3, y - 3, width - margin_x * 2 + 6, line_height, stroke=0, fill=1)
                document.setFillColorRGB(0.2, 0.27, 0.35)
                document.setFont(font_name, 9.4)
            elif kind == "quote":
                document.setFillColorRGB(0.33, 0.4, 0.49)
                document.setFont(font_name, font_size)
            elif kind == "priority":
                document.setFillColorRGB(0.51, 0.35, 0.05)
                document.setFont(font_name, font_size)
            else:
                document.setFillColorRGB(0.15, 0.19, 0.24)
                document.setFont(font_name, font_size)
            document.drawString(margin_x, y, line)
            y -= line_height
        document.showPage()
    document.save()
    return target
