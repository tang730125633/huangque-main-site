#!/usr/bin/env python3
"""
video_vision.py — 视频画面分析模块
提取关键帧 → GPT-4o Vision 分析 → 输出视觉结构报告
"""
import os, base64, json, subprocess, tempfile
from pathlib import Path

VISION_API_KEY = os.environ.get("HERMES_VISION_API_KEY", "")

def extract_frames(video_path, num_frames=8):
    """Extract evenly spaced key frames from video"""
    import requests as req

    # Get video duration
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", video_path],
        capture_output=True, text=True
    )
    dur = float(probe.stdout.strip() or 60)

    frames_dir = Path(tempfile.mkdtemp(prefix="vf_"))
    interval = dur / (num_frames + 1)

    frame_paths = []
    for i in range(1, num_frames + 1):
        t = interval * i
        out_path = frames_dir / f"frame_{i:02d}.jpg"
        subprocess.run([
            "ffmpeg", "-y", "-ss", str(t), "-i", video_path,
            "-vframes", "1", "-q:v", "3", str(out_path)
        ], capture_output=True, timeout=15)
        if out_path.exists() and out_path.stat().st_size > 1000:
            frame_paths.append(str(out_path))

    return frame_paths, dur

def analyze_frame(img_path):
    """Send image to GPT-4o for analysis"""
    import requests as req

    with open(img_path, "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode()

    resp = req.post("https://api.gptsapi.net/v1/chat/completions",
        headers={"Authorization": f"Bearer {VISION_API_KEY}", "Content-Type": "application/json"},
        json={
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": (
                    "分析这张短视频截图。用JSON回复，只回复JSON："
                    '{"scene_type":"实拍/录屏/素材混剪/纯文字","has_person":"是/否","visual_style":"科技感/生活感/商务/娱乐","'
                    '"color_tone":"暖色/冷色/中性","text_on_screen":"有/否","text_content":"屏幕上的文字内容",'
                    '"composition":"画面构图描述(20字以内)","subject":"画面主体是什么"'
                    "}"
                )},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}}
            ]}],
            "max_tokens": 300
        }, timeout=30)

    if resp.status_code == 200:
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        # Try to parse JSON from response
        import re
        m = re.search(r'\{.*\}', content, re.DOTALL)
        if m:
            return json.loads(m.group())
        return {"raw": content}
    return {"error": f"HTTP {resp.status_code}"}

def analyze_video_visual(video_path, num_frames=8):
    """Full visual analysis pipeline"""
    frames, duration = extract_frames(video_path, num_frames)

    if not frames:
        return {"error": "No frames extracted", "frames": 0}

    analyses = []
    for fp in frames:
        result = analyze_frame(fp)
        result["timestamp"] = os.path.basename(fp)
        analyses.append(result)

    # Summarize into visual style report
    styles = [a.get("visual_style", "") for a in analyses if "visual_style" in a]
    has_person = any(a.get("has_person") == "是" for a in analyses)
    text_screens = sum(1 for a in analyses if a.get("text_on_screen") == "是")
    scene_types = [a.get("scene_type", "") for a in analyses if "scene_type" in a]

    # Most common style
    style_counts = {}
    for s in styles:
        style_counts[s] = style_counts.get(s, 0) + 1
    dominant_style = max(style_counts, key=style_counts.get) if style_counts else "未识别"

    return {
        "duration_seconds": duration,
        "frames_analyzed": len(analyses),
        "dominant_style": dominant_style,
        "has_person": has_person,
        "text_overlay_ratio": f"{text_screens}/{len(analyses)}",
        "scene_types": list(set(scene_types)),
        "frame_details": analyses,
        "summary": (
            f"视频总长{duration:.0f}秒，共分析{len(analyses)}帧。"
            f"主视觉风格：{dominant_style}。"
            f"{'有' if has_person else '无'}真人出镜。"
            f"{text_screens}/{len(analyses)}帧有文字叠加。"
            f"场景类型：{'、'.join(set(scene_types)) or '未识别'}。"
        )
    }

# ── Test ──
if __name__ == "__main__":
    import sys
    from runtime_paths import DATA_DIR
    path = sys.argv[1] if len(sys.argv) > 1 else str(DATA_DIR / "uploads" / "douyin_new.mp4")
    print(f"Analyzing: {path}")
    result = analyze_video_visual(path, num_frames=6)
    print(json.dumps(result, ensure_ascii=False, indent=2))
