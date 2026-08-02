#!/usr/bin/env python3
"""Seedance 动作模仿隔离 POC；默认只打印请求，不产生费用。"""
import argparse
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from content_domains import video_seedance  # noqa: E402


PROMPTS = {
    "rgb": (
        "以[Image 1]作为唯一人物身份、面部、发型、体型和服装参考。"
        "严格参考[Video 1]的动作顺序、节奏、身体姿态、空间位置和镜头运动；"
        "不要继承[Video 1]中原人物的身份、服装和背景。保持单人、全身、连续稳定。"
    ),
    "depth": (
        "以[Image 1]作为唯一人物身份、面部、发型、体型和服装参考。"
        "[Video 1]仅作为深度结构、动作顺序、节奏、身体姿态和空间位置参考；"
        "不要继承其黑白深度图风格、原人物外观或背景。保持单人、全身、连续稳定。"
    ),
    "depth_scene": (
        "以[Image 1]作为唯一人物身份、面部、发型、体型和服装参考，以[Image 2]作为场景参考。"
        "[Video 1]仅作为深度结构、动作顺序、节奏、身体姿态和空间位置参考；"
        "不要继承其黑白深度图风格或原人物外观。保持单人、全身、连续稳定。"
    ),
}


def build_payload(mode, identity_image, motion_video, scene_image=None, **options):
    if not str(identity_image).startswith("asset://asset-"):
        raise ValueError("人物参考必须使用本人授权的 Seedance asset:// 素材")
    if mode == "depth_scene" and not scene_image:
        raise ValueError("depth_scene 模式必须提供场景参考图")
    refs = [identity_image] + ([scene_image] if scene_image else [])
    prompt = PROMPTS[mode]
    if options.get("prompt"):
        prompt += "\n补充要求：" + str(options["prompt"]).strip()
    return video_seedance._build_payload(
        options.get("model") or video_seedance.SEEDANCE_MODEL,
        prompt,
        options.get("duration", 5),
        options.get("ratio", "adaptive"),
        options.get("resolution", "480p"),
        options.get("generate_audio", False),
        refs,
        [motion_video],
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=PROMPTS, default="rgb")
    parser.add_argument("--identity-image", required=True,
                        help="已获本人授权的 asset://asset-... 人物素材")
    parser.add_argument("--motion-video", required=True,
                        help="公网 HTTPS 动作视频或深度视频 URL")
    parser.add_argument("--scene-image", help="depth_scene 模式的场景图片 URL")
    parser.add_argument("--prompt", default="")
    parser.add_argument("--model", default=video_seedance.SEEDANCE_MODEL,
                        choices=sorted(video_seedance.MODELS))
    parser.add_argument("--duration", type=int, default=5)
    parser.add_argument("--ratio", default="adaptive",
                        choices=sorted(video_seedance.RATIOS))
    parser.add_argument("--resolution", default="480p",
                        choices=("480p", "720p", "1080p"))
    parser.add_argument("--audio", action="store_true")
    parser.add_argument("--submit", action="store_true",
                        help="提交一条真实付费任务；默认仅预览请求")
    args = parser.parse_args(argv)

    payload = build_payload(
        args.mode, args.identity_image, args.motion_video, args.scene_image,
        prompt=args.prompt, model=args.model, duration=args.duration,
        ratio=args.ratio, resolution=args.resolution,
        generate_audio=args.audio,
    )
    if not args.submit:
        print(video_seedance._safe_text(
            json.dumps(payload, ensure_ascii=False, indent=2), limit=20000
        ))
        return 0
    if os.environ.get("SEEDANCE_MOTION_POC_ALLOW_PAID") != "1":
        parser.error("付费提交前必须设置 SEEDANCE_MOTION_POC_ALLOW_PAID=1")
    result = video_seedance.generate(
        model=args.model,
        prompt=payload["content"][0]["text"],
        duration=args.duration,
        ratio=args.ratio,
        resolution=args.resolution,
        generate_audio=args.audio,
        reference_images=[item["image_url"]["url"] for item in payload["content"]
                          if item["type"] == "image_url"],
        reference_videos=[args.motion_video],
    )
    print(video_seedance._safe_text(
        json.dumps(result, ensure_ascii=False, indent=2), limit=20000
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
