"""Render the confirmed Creator profile into a private Chinese PDF."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import html
import os
import pathlib

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import (
    CondPageBreak, HRFlowable, PageBreak, Paragraph, SimpleDocTemplate, Spacer,
    Table, TableStyle,
)

from .profile_agent import MODULES, profile_answer_value


PROFILE_PDF_SCHEMA_VERSION = 2


class ProfilePDFError(RuntimeError):
    pass


def profile_pdf_path(root, username, project_id):
    owner = hashlib.sha256(str(username).encode("utf-8")).hexdigest()[:16]
    return pathlib.Path(root) / ("%s-%s.pdf" % (owner, project_id))


def _text(value, maximum=12000):
    return str(value or "").strip()[:maximum]


def _paragraph(value, style):
    escaped = html.escape(_text(value)).replace("\n", "<br/>")
    return Paragraph(escaped or "未填写", style)


def _styles():
    try:
        pdfmetrics.getFont("STSong-Light")
    except KeyError:
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "ProfileTitle", parent=base["Title"], fontName="STSong-Light",
            fontSize=25, leading=34, textColor=colors.HexColor("#12233A"),
            alignment=TA_CENTER, spaceAfter=8 * mm,
        ),
        "subtitle": ParagraphStyle(
            "ProfileSubtitle", parent=base["Normal"], fontName="STSong-Light",
            fontSize=10, leading=17, textColor=colors.HexColor("#65758B"),
            alignment=TA_CENTER,
        ),
        "h1": ParagraphStyle(
            "ProfileH1", parent=base["Heading1"], fontName="STSong-Light",
            fontSize=18, leading=25, textColor=colors.HexColor("#17375E"),
            spaceBefore=3 * mm, spaceAfter=5 * mm,
        ),
        "h2": ParagraphStyle(
            "ProfileH2", parent=base["Heading2"], fontName="STSong-Light",
            fontSize=12, leading=18, textColor=colors.HexColor("#805D18"),
            spaceBefore=3 * mm, spaceAfter=2 * mm,
        ),
        "body": ParagraphStyle(
            "ProfileBody", parent=base["BodyText"], fontName="STSong-Light",
            fontSize=9.5, leading=16, textColor=colors.HexColor("#29384B"),
            spaceAfter=2.5 * mm,
        ),
        "small": ParagraphStyle(
            "ProfileSmall", parent=base["BodyText"], fontName="STSong-Light",
            fontSize=8, leading=13, textColor=colors.HexColor("#738196"),
        ),
        "label": ParagraphStyle(
            "ProfileLabel", parent=base["BodyText"], fontName="STSong-Light",
            fontSize=9, leading=15, textColor=colors.HexColor("#805D18"),
            spaceBefore=1.5 * mm, spaceAfter=1 * mm,
        ),
    }


def _footer(canvas, document):
    canvas.saveState()
    canvas.setStrokeColor(colors.HexColor("#D5DDE8"))
    canvas.line(18 * mm, 13 * mm, A4[0] - 18 * mm, 13 * mm)
    canvas.setFont("STSong-Light", 8)
    canvas.setFillColor(colors.HexColor("#718096"))
    canvas.drawString(18 * mm, 8 * mm, "黄雀 AI · 独立个人画像档案")
    canvas.drawRightString(A4[0] - 18 * mm, 8 * mm, str(document.page))
    canvas.restoreState()


def _option_items(selected):
    if not isinstance(selected, dict):
        return []
    items = []
    for label, key in (
        ("方案", "title"), ("一句话定位", "one_liner"),
        ("优势", "strengths"), ("风险", "risks"),
    ):
        value = selected.get(key)
        if isinstance(value, list):
            value = "；".join(_text(item) for item in value)
        items.append((label, value))
    return items


def _divider():
    return HRFlowable(
        width="100%", thickness=0.35, color=colors.HexColor("#DDE3EB"),
        spaceBefore=1.5 * mm, spaceAfter=2 * mm,
    )


def render_profile_pdf(output_path, display_name, profile, state):
    output = pathlib.Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".tmp.pdf")
    styles = _styles()
    selected = profile.get("modules") if isinstance(profile, dict) else {}
    answers = state.get("answers") if isinstance(state, dict) else {}
    reviews = state.get("module_reviews") if isinstance(state, dict) else {}
    skipped = set(state.get("skipped_questions") or []) if isinstance(state, dict) else set()
    generated_at = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")

    story = [
        Spacer(1, 18 * mm),
        Paragraph("黄雀 IP 人设定位档案", styles["title"]),
        Paragraph(html.escape(_text(display_name, 120) or "个人画像"), styles["subtitle"]),
        Spacer(1, 8 * mm),
        Table([
            [_paragraph("档案范围", styles["small"]), _paragraph("模块1-4 · 定位、人设、价值、故事", styles["body"])],
            [_paragraph("生成方式", styles["small"]), _paragraph("DeepSeek 对话采集与结构化分析", styles["body"])],
            [_paragraph("生成时间", styles["small"]), _paragraph(generated_at, styles["body"])],
        ], colWidths=[35 * mm, 120 * mm], style=TableStyle([
            ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#EDF3FA")),
            ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#D5DDE8")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 7),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ])),
        Spacer(1, 12 * mm),
        _paragraph(
            "本档案依据用户在黄雀 AI 创作助手中的真实回答生成。跳过或未确认的信息不会被推测补全，后续可通过对话继续更新。",
            styles["body"],
        ),
    ]
    overrides = profile.get("overrides") if isinstance(profile, dict) else []
    if isinstance(overrides, list) and overrides:
        story.append(Spacer(1, 5 * mm))
        story.append(Paragraph("画像补充与修订", styles["h2"]))
        for item in overrides[-20:]:
            content = item.get("content") if isinstance(item, dict) else item
            story.append(_paragraph(content, styles["body"]))

    for module in range(1, 5):
        story.append(PageBreak() if module == 1 else CondPageBreak(75 * mm))
        module_name = MODULES[module]["name"]
        story.append(Paragraph("模块%d｜%s" % (module, module_name), styles["h1"]))
        review = (reviews or {}).get(str(module)) or {}
        if review.get("summary"):
            story.append(Paragraph("DeepSeek 模块总结", styles["h2"]))
            story.append(_paragraph(review["summary"], styles["body"]))
        module_selected = (selected or {}).get(str(module)) or {}
        option_items = _option_items(module_selected)
        if option_items:
            story.append(CondPageBreak(25 * mm))
            story.append(Paragraph("已确认方案", styles["h2"]))
            for label, value in option_items:
                story.append(CondPageBreak(18 * mm))
                story.append(Paragraph(html.escape(label), styles["label"]))
                story.append(_paragraph(value, styles["body"]))
                story.append(_divider())
        story.append(CondPageBreak(25 * mm))
        story.append(Paragraph("背景信息", styles["h2"]))
        for question in MODULES[module]["questions"]:
            key = question["key"]
            value = profile_answer_value(answers, module, key)
            if value in (None, "") and "%d:%s" % (module, key) in skipped:
                value = "已跳过"
            story.append(CondPageBreak(18 * mm))
            story.append(_paragraph(question["question"], styles["label"]))
            story.append(_paragraph(value, styles["body"]))
            story.append(_divider())

    document = SimpleDocTemplate(
        str(temporary), pagesize=A4, rightMargin=18 * mm, leftMargin=18 * mm,
        topMargin=18 * mm, bottomMargin=18 * mm,
        title="黄雀IP人设定位档案", author="黄雀 AI",
    )
    try:
        document.build(story, onFirstPage=_footer, onLaterPages=_footer)
        if not temporary.exists() or temporary.stat().st_size < 1024:
            raise ProfilePDFError("profile PDF output is empty")
        os.replace(temporary, output)
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        if isinstance(exc, ProfilePDFError):
            raise
        raise ProfilePDFError("profile PDF generation failed") from exc
    return {
        "path": str(output),
        "size": int(output.stat().st_size),
        "generated_at": generated_at,
    }
