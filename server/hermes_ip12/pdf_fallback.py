"""Small browser-free PDF fallback for the isolated Agent preview."""

from __future__ import annotations

import math
import os
import re
from html import escape
from pathlib import Path


def _register_cjk_font(pdfmetrics, TTFont):
    font_name = "HermesFoundationCJK"
    for font_path in (
        os.getenv("HERMES_PDF_FONT_PATH", ""),
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    ):
        if not font_path or not Path(font_path).is_file():
            continue
        try:
            pdfmetrics.registerFont(TTFont(font_name, font_path))
            return font_name
        except Exception:
            continue
    raise RuntimeError("PDF fallback requires an embeddable CJK font")


def _has_sparse_tail(path, threshold=180):
    from pypdf import PdfReader
    reader = PdfReader(path)
    return len(reader.pages) > 1 and len((reader.pages[-1].extract_text() or "").strip()) < threshold


def render_foundation_consulting_pdf(markdown, target, title="IP 人设定位｜模块 1-4 初稿", _compact=0):
    """A restrained consulting-report layout for the customer-facing PDF."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import (
        KeepTogether, ListFlowable, ListItem, PageBreak, Paragraph, SimpleDocTemplate,
        Spacer, Table, TableStyle,
    )

    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    font_name = _register_cjk_font(pdfmetrics, TTFont)
    width, height = A4
    navy, ink, muted, line, pale = (
        colors.HexColor("#102f62"), colors.HexColor("#24272d"), colors.HexColor("#68717d"),
        colors.HexColor("#d9dde3"), colors.HexColor("#edf3ff"),
    )
    styles = getSampleStyleSheet()
    body_sizes = ((10.0, 16, 6), (9.6, 15, 5), (9.2, 14, 4))
    body_size, body_leading, body_after = body_sizes[min(int(_compact), 2)]
    body = ParagraphStyle("consulting-body", parent=styles["BodyText"], fontName=font_name,
                          fontSize=body_size, leading=body_leading,
                          textColor=ink, spaceAfter=body_after, wordWrap="CJK")
    module = ParagraphStyle("consulting-module", parent=body, fontSize=17, leading=23,
                            spaceBefore=0, spaceAfter=20, textColor=ink, fontName=font_name)
    section = ParagraphStyle("consulting-section", parent=body, fontSize=12.5, leading=18,
                             spaceBefore=13, spaceAfter=9, textColor=ink, fontName=font_name)
    card = ParagraphStyle("consulting-card", parent=body, fontSize=11.2, leading=16,
                          spaceBefore=9, spaceAfter=6, textColor=ink, fontName=font_name)
    small = ParagraphStyle("consulting-small", parent=body, fontSize=9.4, leading=14,
                           spaceAfter=0, textColor=muted, fontName=font_name)
    cover_title = ParagraphStyle("consulting-cover-title", parent=body, fontSize=23, leading=30,
                                 spaceAfter=18, textColor=ink, fontName=font_name)
    table_text = ParagraphStyle("consulting-table", parent=body, fontSize=9.2, leading=14,
                                textColor=ink, fontName=font_name, spaceAfter=0)
    list_text = ParagraphStyle("consulting-list", parent=body, fontSize=9.6, leading=14,
                               textColor=ink, fontName=font_name, spaceAfter=1)

    def plain(value):
        value = escape(str(value or ""))
        value = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", value)
        return value.replace("&lt;br/&gt;", "<br/>")

    def paragraph(value, style=body):
        return Paragraph(plain(value), style)

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setStrokeColor(navy)
        canvas.setLineWidth(1.4)
        canvas.line(22 * mm, 16 * mm, width - 22 * mm, 16 * mm)
        canvas.setFillColor(muted)
        canvas.setFont(font_name, 8)
        canvas.drawRightString(width - 22 * mm, 10 * mm, "第 %d 页" % doc.page)
        canvas.restoreState()

    def consulting_table(raw_rows):
        cells = [[cell.strip() for cell in row.strip().strip("|").split("|")] for row in raw_rows]
        header, *rows = cells
        if rows and all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in rows[0]):
            rows = rows[1:]
        data = [[paragraph(cell, table_text) for cell in header]]
        data += [[paragraph(cell, table_text) for cell in row] for row in rows]
        table = Table(data, colWidths=[(width - 44 * mm) / max(1, len(header))] * len(header), repeatRows=1)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), pale), ("TEXTCOLOR", (0, 0), (-1, 0), ink),
            ("FONTNAME", (0, 0), (-1, -1), font_name), ("GRID", (0, 0), (-1, -1), 0.45, colors.HexColor("#cfdbef")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 9),
            ("RIGHTPADDING", (0, 0), (-1, -1), 9), ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ]))
        return table

    lines = str(markdown or "").splitlines()
    story, index = [], 0
    if lines and lines[0].strip().startswith("## 首页"):
        index = 1
        cards, label, values = [], "", []
        while index < len(lines) and not lines[index].strip().startswith("## "):
            value = lines[index].strip()
            if value.startswith("#### "):
                if label:
                    cards.append([paragraph(label, table_text), paragraph("<br/>".join(values), table_text)])
                label, values = value[5:], []
            elif value:
                values.append(value)
            index += 1
        if label:
            cards.append([paragraph(label, table_text), paragraph("<br/>".join(values), table_text)])
        story += [Spacer(1, 20 * mm), paragraph(title.replace("｜", " | "), cover_title)]
        story += [paragraph("整理日期：基于本次对话", small), paragraph("报告框架：IP 十二模块｜模块 1-4", small), Spacer(1, 11 * mm)]
        cover = Table(cards, colWidths=[38 * mm, width - 82 * mm], repeatRows=0)
        cover.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (0, -1), pale), ("GRID", (0, 0), (-1, -1), 0.45, colors.HexColor("#cfdbef")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 10),
            ("RIGHTPADDING", (0, 0), (-1, -1), 10), ("TOPPADDING", (0, 0), (-1, -1), 11),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 11),
        ]))
        story += [cover, Spacer(1, 12 * mm), paragraph("本报告将本人确认事实与传播建议分开呈现；传播建议不代表已发生结果。", small), PageBreak()]

    table_rows, bullet_rows = [], []
    def flush_table():
        nonlocal table_rows
        if table_rows:
            story.extend([consulting_table(table_rows), Spacer(1, 9)])
            table_rows = []
    def flush_bullets():
        nonlocal bullet_rows
        if bullet_rows:
            story.append(ListFlowable([ListItem(paragraph(item, list_text), leftIndent=0) for item in bullet_rows], bulletType="bullet", leftIndent=14))
            story.append(Spacer(1, 3))
            bullet_rows = []

    while index < len(lines):
        raw = lines[index].strip()
        if raw.startswith("|") and raw.endswith("|"):
            flush_bullets(); table_rows.append(raw); index += 1; continue
        flush_table()
        if raw.startswith(("- ", "* ")):
            bullet_rows.append(raw[2:].strip()); index += 1; continue
        flush_bullets()
        if not raw:
            index += 1; continue
        if raw.startswith("## "):
            # 紧凑档位不再强制模块分页（页数质检 6-8 页；内容密度不够时靠压缩分页收敛）
            if not _compact and story and not isinstance(story[-1], PageBreak):
                story.append(PageBreak())
            story.append(paragraph(raw[3:], module))
        elif raw.startswith("### "):
            story.append(KeepTogether([paragraph(raw[4:], section), Spacer(1, 1)]))
        elif raw.startswith("#### "):
            story.append(KeepTogether([paragraph(raw[5:], card), Spacer(1, 1)]))
        elif raw.startswith("> "):
            story.append(Paragraph("<font color='#68717d'>%s</font>" % plain(raw[2:]), body))
        else:
            story.append(paragraph(raw))
        index += 1
    flush_table(); flush_bullets()
    margin = 22 * mm if not _compact else 16 * mm
    doc = SimpleDocTemplate(str(target), pagesize=A4, leftMargin=margin, rightMargin=margin,
                            topMargin=margin, bottomMargin=18 * mm, title=title)
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    if _compact < 2 and _has_sparse_tail(target):
        return render_foundation_consulting_pdf(markdown, target, title=title, _compact=_compact + 1)
    return target


def _styled_lines(markdown):
    lines = []
    homepage = False
    summary_card = False
    for raw in str(markdown or "").splitlines():
        text = raw.strip()
        if not text or re.fullmatch(r"[:|\- ]+", text):
            continue
        kind = "body"
        if text.startswith("## "):
            kind, text = "section", text[3:]
            homepage = text.startswith("首页")
            summary_card = False
        elif text.startswith("#### "):
            kind, text = "card_title", text[5:]
            summary_card = homepage
            if homepage:
                kind = "summary_card_title"
            if any(label in text for label in (
                "情绪曲线", "开场钩子", "可拆选题", "使用边界", "表达边界",
                "适用场景", "金句", "内容支柱", "具体动作", "建议产出", "验证方式",
            )):
                kind = "card_detail"
        elif text.startswith("### "):
            kind, text = "subsection", text[4:]
            summary_card = False
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
            if homepage and summary_card:
                kind = "summary_card_body"
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


def render_foundation_pdf_fallback(markdown, target, title="IP 人设定位｜模块 1-4 初稿"):
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
            10.8 if kind == "summary_card_title" else
            10.5 if kind == "summary_card_body" else
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
    first_module = next((index for index, (kind, text) in enumerate(wrapped)
                         if kind == "section" and text.startswith("模块一")), 0)
    if first_module:
        pages = [wrapped[:first_module]]
        remaining = wrapped[first_module:]
        for pages_left in range(page_count - 1, 0, -1):
            take = max(1, math.ceil(len(remaining) / pages_left))
            while take > 1 and take < len(remaining) and remaining[take - 1][0] in {
                "section", "subsection", "card_title", "advice"
            }:
                take -= 1
            pages.append(remaining[:take])
            remaining = remaining[take:]
    else:
        lines_per_page = max(1, math.ceil(len(wrapped) / page_count))
        pages = [wrapped[index:index + lines_per_page] for index in range(0, len(wrapped), lines_per_page)]
    page_count = len(pages)

    document = canvas.Canvas(str(target), pagesize=A4, pageCompression=1)
    document.setTitle(title)
    for page_index in range(page_count):
        document.setFillColorRGB(0.965, 0.975, 0.99)
        document.rect(0, 0, width, height, stroke=0, fill=1)
        document.setFillColorRGB(0.09, 0.24, 0.47)
        document.rect(0, height - 52, width, 52, stroke=0, fill=1)
        document.setFillColorRGB(1, 1, 1)
        document.setFont(header_font_name, 12)
        document.drawString(margin_x, height - 33, title.replace("｜", " | "))
        document.setStrokeColorRGB(0.78, 0.83, 0.9)
        document.line(margin_x, 36, width - margin_x, 36)
        document.setFillColorRGB(0.35, 0.41, 0.49)
        document.setFont("Helvetica", 8)
        document.drawRightString(width - margin_x, 22, "%d / %d" % (page_index + 1, page_count))
        y = height - margin_top
        for kind, line in pages[page_index]:
            if kind == "summary_card_title":
                document.setFillColorRGB(0.14, 0.31, 0.56)
                document.roundRect(margin_x - 5, y - 3, width - margin_x * 2 + 10, line_height + 2, 3, stroke=0, fill=1)
                document.setFillColorRGB(1, 1, 1)
                document.setFont(font_name, 10.8)
            elif kind == "summary_card_body":
                document.setFillColorRGB(0.94, 0.96, 0.985)
                document.roundRect(margin_x - 5, y - 3, width - margin_x * 2 + 10, line_height + 2, 3, stroke=0, fill=1)
                document.setFillColorRGB(0.15, 0.19, 0.24)
                document.setFont(font_name, 10.5)
            elif kind == "section":
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
            y -= line_height + (18 if kind == "summary_card_body" else 0)
        document.showPage()
    document.save()
    return target
