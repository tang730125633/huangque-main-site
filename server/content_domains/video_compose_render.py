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
TEMPLATE_VARIANTS = {
    "viral-talking-head-v1": {
        "variant": "high", "label": "HIGH CUT · 01",
    },
    "professional-explainer-v1": {
        "variant": "professional", "label": "PRO EXPLAIN · 02",
    },
    "clean-talking-v1": {
        "variant": "clean", "label": "CLEAN TALK · 03",
    },
}
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


def normalize_template_id(value):
    template_id = str(value or TEMPLATE_ID).strip()
    if template_id not in TEMPLATE_VARIANTS:
        raise RenderError("不支持的剪辑模板")
    return template_id


def normalize_input(payload):
    if not isinstance(payload, dict):
        raise RenderError("模板输入格式无效")
    try:
        duration_ms = int(payload.get("duration_ms"))
    except (TypeError, ValueError):
        raise RenderError("模板时长格式无效")
    if not 800 <= duration_ms <= 300000:
        raise RenderError("模板时长超出首版范围")
    template_id = normalize_template_id(payload.get("template_id"))
    template = TEMPLATE_VARIANTS[template_id]
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
        "template_id": template_id, "variant": template["variant"],
        "mode_label": template["label"],
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
    if data["variant"] == "high":
        lines = [
            '    const tl = gsap.timeline({paused:true});',
            '    tl.from("#brand",{opacity:0,x:-36,duration:.42,ease:"power3.out"},0)',
            '      .from("#mode-chip",{opacity:0,x:32,duration:.38,ease:"power3.out"},.08)',
            '      .from("#hook-one",{opacity:0,y:-54,scale:.86,duration:.46,ease:"back.out(1.7)"},.05)',
            '      .from("#hook-two",{opacity:0,y:52,scale:.76,rotation:-4,duration:.48,ease:"back.out(1.9)"},.24)',
            '      .to("#hook-rule",{width:460,duration:.42,ease:"power3.out"},.48)',
            '      .to("#hook",{opacity:0,y:-18,duration:.24,ease:"power2.in"},%.3f)' % min(1.92, max(.9, duration * .24)),
        ]
    else:
        smooth = "power2.out" if data["variant"] == "professional" else "power1.out"
        lines = [
            '    const tl = gsap.timeline({paused:true});',
            '    tl.from("#brand",{opacity:0,y:-14,duration:.5,ease:"%s"},0)' % smooth,
            '      .from("#mode-chip",{opacity:0,y:-12,duration:.46,ease:"%s"},.08)' % smooth,
            '      .from("#hook-one",{opacity:0,y:24,scale:.98,duration:.52,ease:"%s"},.08)' % smooth,
            '      .from("#hook-two",{opacity:0,y:22,scale:.98,duration:.54,ease:"%s"},.2)' % smooth,
            '      .to("#hook-rule",{width:%d,duration:.5,ease:"%s"},.42)' % (
                320 if data["variant"] == "professional" else 210, smooth),
            '      .to("#hook",{opacity:0,y:-10,duration:.34,ease:"power1.in"},%.3f)' % min(2.2, max(1.15, duration * .24)),
        ]
    for index, item in enumerate(data["headlines"], 1):
        if data["variant"] != "high":
            props = '{opacity:0,y:24,scale:.97,duration:.36,ease:"power2.out"}'
        elif item["text"].upper() == "AI":
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
    caption_motion = ('{opacity:0,y:30,scale:.94,duration:.18,ease:"back.out(1.5)"}'
                      if data["variant"] == "high" else
                      '{opacity:0,y:16,duration:.28,ease:"power2.out"}')
    lines.append('    [%s].forEach(([selector,start])=>tl.from(selector,%s,start));' %
                 (rows, caption_motion))
    lines.append('    window.__timelines.main = tl;')
    return "\n".join(lines)


def _variant_css(data):
    if data["variant"] == "professional":
        return """
    body.variant-professional .wash{background:linear-gradient(180deg,rgba(4,12,20,.22),transparent 30%,rgba(3,12,20,.82) 100%)}
    body.variant-professional .brand{border-radius:7px;background:rgba(5,18,28,.84);border-color:rgba(77,208,194,.32)}
    body.variant-professional .brand-dot,body.variant-professional .brand-rule,body.variant-professional .progress-fill,body.variant-professional .hook-underline{background:#4dd0c2;box-shadow:none}
    body.variant-professional .mode-chip{border-radius:7px;border-color:rgba(77,208,194,.36);color:#a9eee7;background:rgba(5,18,28,.7)}
    body.variant-professional .hook{top:190px;text-align:left}
    body.variant-professional .hook-line{font-size:62px;transform:none;border-left:8px solid #4dd0c2;background:rgba(4,16,26,.84)}
    body.variant-professional .hook-line.two{font-size:68px;transform:none;box-shadow:none;color:#eaf7f5;background:rgba(4,16,26,.84)}
    body.variant-professional .hook-underline{margin-left:0;height:5px}
    body.variant-professional .caption{left:56px;bottom:330px;min-width:0;max-width:900px;transform:none;text-align:left;border-radius:8px;border-left:7px solid #4dd0c2;background:rgba(4,16,26,.9);font-size:43px}
    body.variant-professional .caption .key{color:#72dfd4}
    body.variant-professional .giant{text-align:left;top:220px}
    body.variant-professional .giant-word,body.variant-professional .giant-word.yellow{width:auto;height:auto;transform:none;border-radius:10px;border-color:#4dd0c2;background:rgba(4,16,26,.94);color:#fff;font-size:112px;text-shadow:none}
    body.variant-professional .cta{top:220px;border-color:#4dd0c2;border-radius:10px;background:rgba(4,16,26,.94)}
    body.variant-professional .cta b{color:#72dfd4}
    """
    if data["variant"] == "clean":
        return """
    body.variant-clean .wash{background:linear-gradient(180deg,rgba(0,0,0,.12),transparent 42%,rgba(0,0,0,.58) 100%)}
    body.variant-clean .caption-shield{opacity:.34}
    body.variant-clean .brand{top:46px;background:transparent;border:0;padding:0;box-shadow:none}
    body.variant-clean .brand-dot{width:9px;height:9px;box-shadow:none}
    body.variant-clean .brand-rule{display:none}
    body.variant-clean .mode-chip{top:43px;border:0;background:transparent;color:rgba(255,255,255,.68);letter-spacing:2px}
    body.variant-clean .hook{top:215px;filter:none}
    body.variant-clean .hook-line{padding:0;background:transparent;font-size:66px;transform:none;text-shadow:0 3px 22px rgba(0,0,0,.72)}
    body.variant-clean .hook-line.two{margin-top:10px;padding:0;color:#f4ca3b;background:transparent;font-size:74px;transform:none;box-shadow:none}
    body.variant-clean .hook-underline{height:3px;box-shadow:none}
    body.variant-clean .caption{bottom:300px;min-width:0;max-width:920px;padding:8px 18px;background:transparent;border:0;box-shadow:none;font-size:45px;text-shadow:0 3px 14px #000}
    body.variant-clean .caption small{display:none}
    body.variant-clean .giant{top:250px}
    body.variant-clean .giant-word,body.variant-clean .giant-word.yellow{width:auto;height:auto;padding:0;background:transparent;border:0;color:#f4ca3b;font-size:118px;transform:none;text-shadow:0 4px 24px #000}
    body.variant-clean .cta{top:255px;padding:18px;background:transparent;border:0;box-shadow:none;text-shadow:0 3px 18px #000}
    body.variant-clean .cta b{font-size:62px}
    body.variant-clean .progress{height:3px;background:rgba(255,255,255,.22)}
    body.variant-clean .footer{opacity:.58}
    """
    return ""


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
            "brand": data["brand_name"], "template": data["template_id"],
            "version": TEMPLATE_VERSION,
        }, ensure_ascii=False, separators=(",", ":")), quote=True),
        "__BRAND__": html.escape(data["brand_name"]),
        "VIRAL TALK · 01": html.escape(data["mode_label"]),
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
    markup = markup.replace("<body>", '<body class="variant-%s">' % data["variant"])
    markup = markup.replace("</head>", "<style>%s</style>\n</head>" % _variant_css(data))
    if any(marker in markup for marker in replacements if marker.startswith("__")):
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
                command, check=True, timeout=timeout or max(900, int(data["duration_ms"] / 1000 * 20)),
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
    return {"template_id": data["template_id"], "template_version": TEMPLATE_VERSION,
            "output": report, "file": str(output_path), "render_log": "ok"}
