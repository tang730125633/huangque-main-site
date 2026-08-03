# -*- coding: utf-8 -*-
"""Frozen HyperFrames template preparation and rendering for one-click video."""

import html
import json
import os
import pathlib
import shlex
import shutil
import subprocess
import tempfile

from . import video_compose_media as media


BASE = pathlib.Path(__file__).resolve().parents[1]
_LOCAL_TEMPLATE_ROOT = BASE.parent / "site" / "assets" / "one-click" / "templates"
_DEPLOYED_TEMPLATE_ROOT = pathlib.Path("/var/www/huangquechuanmei/assets/one-click/templates")
TEMPLATE_ROOT = pathlib.Path(os.environ.get(
    "VIDEO_COMPOSE_TEMPLATE_ROOT",
    str(_LOCAL_TEMPLATE_ROOT if _LOCAL_TEMPLATE_ROOT.is_dir() else _DEPLOYED_TEMPLATE_ROOT),
))
_DEFAULT_NPX = "/home/ubuntu/.local/hq-node/bin/npx" if pathlib.Path(
    "/home/ubuntu/.local/hq-node/bin/npx").is_file() else "npx"
HYPERFRAMES_COMMAND = os.environ.get(
    "VIDEO_COMPOSE_HYPERFRAMES_CMD", _DEFAULT_NPX + " --yes hyperframes@0.7.90"
).strip()
TEMPLATE_ID = "viral-talking-head-v1"
TEMPLATE_VERSION = "1.0.0"
MAX_CUES = 300
MAX_HEADLINES = 3


class RenderError(ValueError):
    pass


def _text(value, limit, fallback=""):
    value = str(value or "").strip()
    if len(value) > limit:
        value = value[:limit]
    return value or fallback


def _seconds(value_ms):
    return "%.3f" % (max(0, int(value_ms)) / 1000.0)


def _highlight(text, keywords):
    escaped = html.escape(text)
    for keyword in keywords or []:
        keyword = _text(keyword, 20)
        if keyword and keyword in text:
            escaped_keyword = html.escape(keyword)
            return escaped.replace(escaped_keyword, '<span class="key">%s</span>' % escaped_keyword, 1)
    return escaped


def normalize_input(payload):
    if not isinstance(payload, dict):
        raise RenderError("模板输入格式无效")
    try:
        duration_ms = int(payload.get("duration_ms"))
    except (TypeError, ValueError):
        raise RenderError("模板时长格式无效")
    if not 800 <= duration_ms <= 180000:
        raise RenderError("模板时长超出首版范围")
    cues = []
    for index, item in enumerate(payload.get("cues") or []):
        if not isinstance(item, dict):
            continue
        try:
            start_ms = int(item.get("start_ms")); end_ms = int(item.get("end_ms"))
        except (TypeError, ValueError):
            continue
        text = _text(item.get("text"), 80)
        if not text or start_ms < 0 or end_ms <= start_ms or end_ms > duration_ms + 100:
            continue
        cues.append({
            "text": text, "start_ms": start_ms, "end_ms": min(duration_ms, end_ms),
            "keywords": [_text(value, 20) for value in (item.get("keywords") or [])[:4] if _text(value, 20)],
        })
    cues.sort(key=lambda item: (item["start_ms"], item["end_ms"]))
    if not cues or len(cues) > MAX_CUES:
        raise RenderError("模板字幕数量无效")
    previous_end = 0
    for cue in cues:
        if cue["start_ms"] < previous_end - 30:
            raise RenderError("模板字幕时间重叠")
        previous_end = cue["end_ms"]
    hook = payload.get("hook") if isinstance(payload.get("hook"), dict) else {}
    hook_one = _text(hook.get("line_1"), 28, cues[0]["text"])
    hook_two = _text(hook.get("line_2"), 28, max(cues, key=lambda item: len(item["text"]))["text"])
    headlines = []
    for item in (payload.get("headlines") or [])[:MAX_HEADLINES]:
        if not isinstance(item, dict):
            continue
        try:
            start_ms = int(item.get("start_ms")); end_ms = int(item.get("end_ms"))
        except (TypeError, ValueError):
            continue
        text = _text(item.get("text"), 12)
        if text and 0 <= start_ms < end_ms <= duration_ms:
            headlines.append({"text": text, "start_ms": start_ms, "end_ms": end_ms})
    brand = payload.get("brand") if isinstance(payload.get("brand"), dict) else {}
    cta_title = _text(brand.get("cta_title"), 18, cues[-1]["text"])
    cta_subtitle = _text(brand.get("cta"), 36, "让每条口播都有记忆点")
    cta_start_ms = max(cues[-1]["start_ms"], duration_ms - 1400)
    cuts = []
    for value in payload.get("cut_points_ms") or []:
        try: point = int(value)
        except (TypeError, ValueError): continue
        if 250 <= point <= duration_ms - 250:
            cuts.append(point)
    return {
        "duration_ms": duration_ms, "cues": cues, "hook_one": hook_one,
        "hook_two": hook_two, "headlines": headlines,
        "brand_name": _text(brand.get("name"), 24, "黄雀 AI"),
        "cta_title": cta_title, "cta_subtitle": cta_subtitle,
        "cta_start_ms": cta_start_ms, "cut_points_ms": sorted(set(cuts))[:20],
    }


def _caption_html(cues):
    rows = []
    for index, cue in enumerate(cues, 1):
        rows.append(
            '    <div id="caption-%d" class="clip caption" data-start="%s" data-duration="%s" '
            'data-track-index="10">%s<small>PHRASE %02d</small></div>' % (
                index, _seconds(cue["start_ms"]),
                _seconds(cue["end_ms"] - cue["start_ms"]),
                _highlight(cue["text"], cue["keywords"]), index,
            )
        )
    return "\n".join(rows)


def _headline_html(headlines):
    rows = []
    for index, item in enumerate(headlines, 1):
        css = "giant-word yellow" if item["text"].upper() == "AI" else "giant-word"
        rows.append(
            '    <div id="headline-%d" class="clip giant" data-start="%s" data-duration="%s" '
            'data-track-index="%d"><div class="%s">%s</div></div>' % (
                index, _seconds(item["start_ms"]),
                _seconds(item["end_ms"] - item["start_ms"]), 4 + index,
                css, html.escape(item["text"]),
            )
        )
    return "\n".join(rows)


def _timeline_script(data):
    duration = data["duration_ms"] / 1000.0
    lines = [
        '    const tl = gsap.timeline({paused:true});',
        '    tl.from("#brand",{opacity:0,x:-36,duration:.42,ease:"power3.out"},0)',
        '      .from("#mode-chip",{opacity:0,x:32,duration:.38,ease:"power3.out"},.08)',
        '      .from("#hook-one",{opacity:0,y:-54,scale:.86,duration:.46,ease:"back.out(1.7)"},.05)',
        '      .from("#hook-two",{opacity:0,y:52,scale:.76,rotation:-4,duration:.48,ease:"back.out(1.9)"},.24)',
        '      .to("#hook-rule",{width:460,duration:.42,ease:"power3.out"},.48)',
        '      .to("#hook",{opacity:0,y:-18,duration:.24,ease:"power2.in"},%.3f)' % min(1.92, max(.9, duration * .24)),
    ]
    for index, item in enumerate(data["headlines"], 1):
        if item["text"].upper() == "AI":
            props = '{opacity:0,scale:1.08,rotation:-5,y:-22,duration:.28,ease:"back.out(1.6)"}'
        else:
            props = '{opacity:0,scale:1.75,rotation:7,y:-36,duration:.30,ease:"back.out(1.45)"}'
        lines.append('      .from("#headline-%d",%s,%s)' % (index, props, _seconds(item["start_ms"])))
    lines.extend([
        '      .from("#cta",{opacity:0,y:-58,scale:.86,duration:.38,ease:"back.out(1.55)"},%s)' % _seconds(data["cta_start_ms"]),
        '      .fromTo("#progress-fill",{scaleX:0},{scaleX:1,duration:%.3f,ease:"none"},0);' % duration,
    ])
    for point in data["cut_points_ms"]:
        at = point / 1000.0
        lines.append('    tl.to("#a-roll",{scale:1.04,duration:.14,ease:"power2.out"},%.3f)' % max(0, at - .04))
        lines.append('      .to("#a-roll",{scale:1,duration:.24,ease:"power2.inOut"},%.3f);' % (at + .10))
    rows = ",".join('["#caption-%d",%s]' % (index, _seconds(cue["start_ms"]))
                    for index, cue in enumerate(data["cues"], 1))
    lines.append('    [%s].forEach(([selector,start])=>tl.from(selector,{opacity:0,y:30,scale:.94,duration:.18,ease:"back.out(1.5)"},start));' % rows)
    lines.append('    window.__timelines.main = tl;')
    return "\n".join(lines)


def prepare_workspace(clean_video, payload, workspace):
    data = normalize_input(payload)
    template = TEMPLATE_ROOT / TEMPLATE_ID
    source = template / "index.template.txt"
    if not source.is_file():
        raise RenderError("一键成片模板不存在")
    workspace = pathlib.Path(workspace)
    shutil.copytree(template, workspace, dirs_exist_ok=True)
    shutil.copy2(clean_video, workspace / "clean-master.mp4")
    markup = source.read_text(encoding="utf-8")
    duration = data["duration_ms"] / 1000.0
    replacements = {
        "__DURATION__": "%.3f" % duration,
        "__VARIABLES__": html.escape(json.dumps({
            "brand": data["brand_name"], "template": TEMPLATE_ID,
            "version": TEMPLATE_VERSION,
        }, ensure_ascii=False, separators=(",", ":")), quote=True),
        "__BRAND__": html.escape(data["brand_name"]),
        "__HOOK_ONE__": html.escape(data["hook_one"]),
        "__HOOK_TWO__": html.escape(data["hook_two"]),
        "__CAPTIONS__": _caption_html(data["cues"]),
        "__HEADLINES__": _headline_html(data["headlines"]),
        "__CTA_START__": _seconds(data["cta_start_ms"]),
        "__CTA_DURATION__": _seconds(data["duration_ms"] - data["cta_start_ms"]),
        "__CTA_TITLE__": html.escape(data["cta_title"]),
        "__CTA_SUBTITLE__": html.escape(data["cta_subtitle"]),
        "__DURATION_LABEL__": "%02d:%02d" % (int(duration) // 60, int(round(duration)) % 60),
        "__TIMELINE_SCRIPT__": _timeline_script(data),
    }
    for marker, value in replacements.items():
        markup = markup.replace(marker, value)
    if any(marker in markup for marker in replacements):
        raise RenderError("模板变量没有完全填充")
    (workspace / "index.html").write_text(markup, encoding="utf-8")
    try:
        (workspace / "index.template.txt").unlink()
    except FileNotFoundError:
        pass
    return data


def render(clean_video, payload, output_path, timeout=None):
    output_path = pathlib.Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="hq-compose-render-") as directory:
        workspace = pathlib.Path(directory) / "project"
        data = prepare_workspace(clean_video, payload, workspace)
        command = shlex.split(HYPERFRAMES_COMMAND) + [
            "render", str(workspace), "--output", str(output_path), "--fps", "30",
            "--quality", "high", "--workers", "1", "--low-memory-mode", "--strict", "--quiet",
        ]
        environment = dict(os.environ)
        environment.update({"HYPERFRAMES_SKIP_SKILLS": "1", "PRODUCER_LOW_MEMORY_MODE": "1"})
        if "HYPERFRAMES_BROWSER_PATH" not in environment and pathlib.Path(
                "/usr/bin/chromium-browser").is_file():
            environment["HYPERFRAMES_BROWSER_PATH"] = "/usr/bin/chromium-browser"
        try:
            completed = subprocess.run(
                command, check=True, timeout=timeout or max(300, int(data["duration_ms"] / 1000 * 20)),
                cwd=str(workspace), env=environment,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
        except FileNotFoundError as error:
            raise RenderError("一键成片渲染运行时尚未安装") from error
        except subprocess.TimeoutExpired as error:
            raise RenderError("模板渲染超时") from error
        except subprocess.CalledProcessError as error:
            detail = ((error.stderr or b"") + (error.stdout or b"")).decode("utf-8", "replace")[-500:]
            raise RenderError("模板渲染失败" + ("：" + detail if detail else "")) from error
    report = media.probe_media(output_path)
    if report["video_codec"] != "h264" or not report["has_audio"]:
        raise RenderError("模板输出编码不符合交付要求")
    return {"template_id": TEMPLATE_ID, "template_version": TEMPLATE_VERSION,
            "output": report, "file": str(output_path), "render_log": "ok"}
