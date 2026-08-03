"""Deterministic, injection-safe ASS subtitle generation for D-2."""

import os
import re
import subprocess
from pathlib import Path


FONT_NAME = "Noto Sans CJK SC"
DEFAULT_FONT_PATH = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
REQUIRED_CJK_GLYPHS = "黄雀字幕测试"
MAX_SUBTITLE_LENGTH = 4000
MAX_SUBTITLE_EVENTS = 500
ASS_RESOLUTIONS = {
    "9:16": (1080, 1920),
    "16:9": (1920, 1080),
}


class SubtitleError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def ass_time(milliseconds, end=False):
    if type(milliseconds) is not int or milliseconds < 0:
        raise SubtitleError("subtitle_timeline_invalid", "字幕时间无效")
    centiseconds = (
        (milliseconds + 9) // 10 if end else milliseconds // 10
    )
    hours, remainder = divmod(centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    seconds, fraction = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{fraction:02d}"


def escape_ass_text(value):
    text = str(value or "").replace("\x00", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Full-width replacements preserve readability while making override blocks
    # and backslash commands impossible for libass to interpret.
    text = text.replace("\\", "＼").replace("{", "｛").replace("}", "｝")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.replace("\n", "\\N")


def _style(position, width, height):
    alignment = 8 if position == "top" else 2
    margin_v = max(60, round(height * 0.07))
    font_size = max(42, round(height * 0.038))
    outline = max(2, round(height / 640))
    return (
        "Style: Default,"
        f"{FONT_NAME},{font_size},&H00FFFFFF,&H000000FF,&H00000000,"
        "&H64000000,-1,0,0,0,100,100,0,0,1,"
        f"{outline},1,{alignment},80,80,{margin_v},1"
    )


def _events(media_plan):
    duration = media_plan.get("project_duration_ms")
    if type(duration) is not int or duration <= 0:
        raise SubtitleError("subtitle_timeline_invalid", "项目字幕时长无效")
    events = []
    for shot in media_plan.get("shots") or []:
        shot_start = shot.get("start_ms")
        shot_end = shot.get("end_ms")
        if (
            type(shot_start) is not int
            or type(shot_end) is not int
            or shot_start < 0
            or shot_end <= shot_start
            or shot_end > duration
        ):
            raise SubtitleError("subtitle_timeline_invalid", "镜头字幕区间无效")
        audio = shot.get("audio")
        lines = audio.get("lines") if isinstance(audio, dict) else []
        for line in lines or []:
            if not line.get("subtitle_visible"):
                continue
            text = str(line.get("subtitle_text") or "").strip()
            if not text or len(text) > MAX_SUBTITLE_LENGTH:
                raise SubtitleError("subtitle_text_invalid", "字幕文本无效")
            start_ms = (
                line.get("subtitle_start_ms")
                if line.get("subtitle_start_ms") is not None
                else line.get("start_ms")
            )
            end_ms = (
                line.get("subtitle_end_ms")
                if line.get("subtitle_end_ms") is not None
                else line.get("end_ms")
            )
            if (
                type(start_ms) is not int
                or type(end_ms) is not int
                or start_ms < 0
                or end_ms <= start_ms
                or shot_start + end_ms > shot_end
            ):
                raise SubtitleError(
                    "subtitle_timeline_invalid", "字幕超出镜头范围"
                )
            events.append({
                "id": str(line.get("id") or ""),
                "start_ms": shot_start + start_ms,
                "end_ms": shot_start + end_ms,
                "text": escape_ass_text(text),
            })
    if len(events) > MAX_SUBTITLE_EVENTS:
        raise SubtitleError("subtitle_timeline_invalid", "字幕数量超过限制")
    return sorted(
        events,
        key=lambda item: (item["start_ms"], item["end_ms"], item["id"]),
    )


def generate_ass(ratio, position, media_plan):
    if ratio not in ASS_RESOLUTIONS or position not in {"top", "bottom"}:
        raise SubtitleError("subtitle_timeline_invalid", "字幕画幅或位置无效")
    if not isinstance(media_plan, dict):
        raise SubtitleError("subtitle_timeline_invalid", "字幕媒体计划无效")
    width, height = ASS_RESOLUTIONS[ratio]
    header = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "",
        "[V4+ Styles]",
        (
            "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,"
            "OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,"
            "ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,"
            "Alignment,MarginL,MarginR,MarginV,Encoding"
        ),
        _style(position, width, height),
        "",
        "[Events]",
        (
            "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,"
            "Effect,Text"
        ),
    ]
    for event in _events(media_plan):
        header.append(
            "Dialogue: 0,"
            f"{ass_time(event['start_ms'])},{ass_time(event['end_ms'], end=True)},"
            f"Default,,0,0,0,,{event['text']}"
        )
    return "\n".join(header) + "\n"


def _charset_contains(charset, codepoint):
    for token in str(charset or "").split():
        try:
            if "-" in token:
                start, end = token.split("-", 1)
                if int(start, 16) <= codepoint <= int(end, 16):
                    return True
            elif int(token, 16) == codepoint:
                return True
        except ValueError:
            continue
    return False


def inspect_font(font_path=None, runner=subprocess.run):
    configured = Path(
        font_path
        or os.environ.get("SHORT_DRAMA_SUBTITLE_FONT")
        or DEFAULT_FONT_PATH
    ).resolve()
    if not configured.is_file():
        raise SubtitleError(
            "subtitle_font_unavailable", "指定的中文字幕字体文件不存在"
        )
    try:
        match = runner(
            ["fc-match", "--format=%{family}\n%{file}", FONT_NAME],
            capture_output=True,
            text=True,
            timeout=10,
        )
        query = runner(
            ["fc-query", "--format=%{charset}", str(configured)],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as error:
        raise SubtitleError(
            "subtitle_font_unavailable", "字幕字体预检工具不可用"
        ) from error
    lines = str(match.stdout or "").splitlines()
    family = lines[0].strip() if lines else ""
    matched_file = Path(lines[1].strip()).resolve() if len(lines) > 1 else None
    if (
        match.returncode != 0
        or FONT_NAME.casefold() not in family.casefold()
        or matched_file != configured
    ):
        raise SubtitleError(
            "subtitle_font_unavailable",
            "fontconfig 未匹配到指定的 Noto CJK 字体",
        )
    charset = str(query.stdout or "").strip()
    if (
        query.returncode != 0
        or not all(
            _charset_contains(charset, ord(character))
            for character in REQUIRED_CJK_GLYPHS
        )
    ):
        raise SubtitleError(
            "subtitle_font_unavailable", "指定字体不包含所需的中文字符"
        )
    return {
        "family": family[:200],
        "file": str(configured),
        "font_dir": str(configured.parent),
    }
