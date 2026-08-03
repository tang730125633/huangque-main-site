#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build the one-click-video showcase from the true unedited source clip."""

import hashlib
import json
import pathlib
import sys


ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from content_domains import video_compose_analysis as analysis
from content_domains import video_compose_media as media


SOURCE = pathlib.Path(
    "/Users/xlzj/Desktop/一键成片功能制作/bc5493e17dd2ed4e76b2a3dcd87e8273_raw.mp4"
)
OUTPUT_DIR = pathlib.Path("/tmp/hq-one-click-raw-showcase/output")

CANDIDATE_SPECS = [
    (0, 329, "leading_silence", "开头环境底噪与准备动作 0.329 秒", 0.98, True),
    (3648, 3830, "breath_pause", "短气口 0.182 秒，删除后开场更紧凑", 0.86, True),
    (5369, 6229, "silence", "句中停顿 0.860 秒", 0.99, True),
    (8987, 10381, "silence", "转折前停顿 1.394 秒", 0.99, True),
    (12440, 14400, "restart", "正片结束后的补充口误“那我可以了”", 0.97, True),
]

SOURCE_CUES = [
    {"text": "20多岁", "start_ms": 329, "end_ms": 2800},
    {"text": "不靠父母", "start_ms": 2800, "end_ms": 3640},
    {"text": "不靠别人", "start_ms": 3830, "end_ms": 4440},
    {"text": "也不靠同事", "start_ms": 4440, "end_ms": 5369},
    {"text": "就靠我自己", "start_ms": 6229, "end_ms": 6900},
    {"text": "全款拿下一碗猪脚饭", "start_ms": 6900, "end_ms": 8987},
    {"text": "如果你跟我一样", "start_ms": 10381, "end_ms": 11180},
    {"text": "做 AI", "start_ms": 11180, "end_ms": 11700},
    {"text": "你也可以", "start_ms": 11700, "end_ms": 12440},
]


def candidate_id(kind, start_ms, end_ms):
    raw = "%s:%d:%d" % (kind, start_ms, end_ms)
    return "candidate_" + hashlib.sha256(raw.encode()).hexdigest()[:16]


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    source = media.probe_media(SOURCE)
    candidates = []
    for start_ms, end_ms, kind, reason, confidence, default_selected in CANDIDATE_SPECS:
        candidates.append({
            "id": candidate_id(kind, start_ms, end_ms),
            "start_ms": start_ms,
            "end_ms": end_ms,
            "type": kind,
            "text": "那我可以了" if kind == "restart" else "",
            "reason": reason,
            "confidence": confidence,
            "default_selected": default_selected,
            "user_decision": "pending",
        })
    decisions = {item["id"]: "remove" for item in candidates}
    edl = analysis.build_edl(source["duration_ms"], candidates, decisions)
    clean_master = OUTPUT_DIR / "clean-master-raw.mp4"
    clean_info = media.build_clean_master(SOURCE, edl, clean_master)
    remapped = media.remap_cues(SOURCE_CUES, edl)
    fixture = {
        "source": str(SOURCE),
        "source_media": source,
        "transcript": "20多岁不靠父母不靠别人也不靠同事就靠我自己全款拿下一碗猪脚饭如果你跟我一样做AI你也可以，那我可以了",
        "candidates": candidates,
        "decisions": decisions,
        "edl": edl,
        "clean_master": str(clean_master),
        "clean_media": clean_info,
        "cues": remapped,
        "template": {"id": "viral-talking-head-v1", "version": "1.0.0-demo"},
    }
    fixture_path = OUTPUT_DIR / "raw-showcase.json"
    fixture_path.write_text(json.dumps(fixture, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "ok": True,
        "fixture": str(fixture_path),
        "clean_master": str(clean_master),
        "source_duration_ms": source["duration_ms"],
        "output_duration_ms": edl["output_duration_ms"],
        "removed_ms": source["duration_ms"] - edl["output_duration_ms"],
        "candidate_count": len(candidates),
        "cues": remapped,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
