#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build the local, authorized one-click-video showcase fixture."""

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
    "/Users/xlzj/Desktop/一键成片功能制作/da261c918b6907a0ab0fb7eed4ac4c71_raw.mp4"
)
OUTPUT_DIR = pathlib.Path("/tmp/hq-one-click-showcase/output")

CUES = [
    {"text": "20多岁不靠父母", "start_ms": 140, "end_ms": 1020, "confidence": 0.96},
    {"text": "不靠别人 也不靠同事", "start_ms": 1020, "end_ms": 2480, "confidence": 0.96},
    {"text": "就靠我自己", "start_ms": 2480, "end_ms": 4000, "confidence": 0.97},
    {"text": "全款拿下", "start_ms": 4070, "end_ms": 5840, "confidence": 0.95},
    {"text": "一碗猪脚饭", "start_ms": 5840, "end_ms": 7000, "confidence": 0.98},
    {"text": "如果你跟我一样", "start_ms": 7000, "end_ms": 8000, "confidence": 0.96},
    {"text": "做 AI 你也可以", "start_ms": 8000, "end_ms": 9220, "confidence": 0.97},
]


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    source = media.probe_media(SOURCE)
    detected = analysis.detect_candidates(source["duration_ms"], CUES)
    decisions = {
        item["id"]: ("remove" if item["default_selected"] else "keep")
        for item in detected["candidates"]
    }
    edl = analysis.build_edl(source["duration_ms"], detected["candidates"], decisions)
    clean_master = OUTPUT_DIR / "clean-master.mp4"
    clean_info = media.build_clean_master(SOURCE, edl, clean_master)
    remapped = media.remap_cues(CUES, edl)
    fixture = {
        "source": str(SOURCE),
        "source_media": source,
        "candidates": detected["candidates"],
        "decisions": decisions,
        "edl": edl,
        "clean_master": str(clean_master),
        "clean_media": clean_info,
        "cues": remapped,
        "template": {
            "id": "viral-talking-head-v1",
            "version": "1.0.0-demo",
            "hook_line_1": "20多岁 全靠自己",
            "hook_line_2": "全款拿下猪脚饭",
            "brand": "黄雀 AI · 一键成片",
            "cta": "让每条口播都有记忆点",
        },
    }
    (OUTPUT_DIR / "showcase.json").write_text(
        json.dumps(fixture, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({
        "ok": True,
        "fixture": str(OUTPUT_DIR / "showcase.json"),
        "clean_master": str(clean_master),
        "source_duration_ms": source["duration_ms"],
        "output_duration_ms": edl["output_duration_ms"],
        "candidate_count": len(detected["candidates"]),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
