#!/usr/bin/env python3
"""
video_replica.py — 新闻播报类视频复刻引擎
复刻逻辑：文案分段 → 提取关键词 → Pexels搜素材 → seedance兜底 → 叠字幕 → 配音 → 合成
"""
import os, re, json, uuid, time, shutil, subprocess
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from artifact_store import finalize_file, video_path, video_work_dir

PEXELS_KEY = os.environ.get("PEXELS_API_KEY", "")
SEEDANCE_KEY = os.environ.get("ARK_API_KEY") or os.environ.get("SEEDANCE_API_KEY", "")
SEEDANCE_API = "https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks"

# ── Keyword extractor ──
def extract_keyword(text):
    """Smart keyword: use AI to pick English search term for Pexels"""
    try:
        from video_pipeline import ai_chat
        kw = ai_chat(f"这句话的视觉搜索关键词，用英文一个词：{text}").strip().strip('"')
        if kw and len(kw) < 20:
            return kw
    except:
        pass
    words = re.findall(r'[\u4e00-\u9fff]{2,4}', text)
    return words[-1] if words else text[:6]

# ── Pexels search + download ──
def search_pexels(keyword):
    """Search Pexels for video clips matching keyword"""
    import requests as req
    if not PEXELS_KEY:
        return None
    try:
        r = req.get("https://api.pexels.com/videos/search",
            headers={"Authorization": PEXELS_KEY},
            params={"query": keyword, "per_page": 5, "orientation": "portrait"},
            timeout=10)
        if r.status_code == 200:
            hits = r.json().get("videos", [])
            for v in hits:
                files = sorted(v.get("video_files", []), key=lambda x: x.get("width", 0) or 0)
                for f in files:
                    if f.get("width", 0) >= 720:
                        return {"url": f["link"], "width": f["width"], "height": f["height"],
                                "duration": v.get("duration", 10), "source": "pexels"}
    except Exception as e:
        print(f"Pexels error: {e}")
    return None

def download_clip(url, output_path, target_dur=4):
    """Download and trim clip to target duration"""
    import requests as req
    try:
        data = req.get(url, timeout=30).content
        tmp = str(output_path) + ".tmp.mp4"
        with open(tmp, "wb") as f:
            f.write(data)
        # Trim + resize to 1080x1920
        subprocess.run([
            "ffmpeg", "-y", "-i", tmp, "-t", str(target_dur),
            "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920",
            "-c:v", "libx264", "-preset", "ultrafast", "-an", str(output_path)
        ], capture_output=True, timeout=20)
        os.remove(tmp)
        return str(output_path) if os.path.exists(output_path) else ""
    except Exception as e:
        print(f"Download error: {e}")
        return ""

# ── Seedance fallback ──
def generate_seedance(prompt, output_path, dur=4):
    """Generate video via seedance when Pexels fails"""
    import requests as req
    try:
        r = req.post(SEEDANCE_API,
            headers={"Authorization": f"Bearer {SEEDANCE_KEY}", "Content-Type": "application/json"},
            json={
                "model": "doubao-seedance-1-0-pro-fast-251015",
                "content": [{"type": "text", "text": f"9:16竖屏，{prompt}"}],
                "ratio": "9:16", "duration": dur, "watermark": False
            }, timeout=15)
        task_id = r.json().get("id", "")
        if task_id:
            for _ in range(20):
                time.sleep(4)
                poll = req.get(f"{SEEDANCE_API}/{task_id}",
                    headers={"Authorization": f"Bearer {SEEDANCE_KEY}"}, timeout=10)
                pd = poll.json()
                if pd.get("status") == "succeeded":
                    video_url = pd.get("content", {}).get("video_url", "")
                    if video_url:
                        data = req.get(video_url, timeout=60).content
                        with open(output_path, "wb") as f:
                            f.write(data)
                        return str(output_path)
                    break
    except Exception as e:
        print(f"Seedance error: {e}")
    return ""

def get_clip(keyword, i, work_dir):
    """Get clip: try Pexels first, then seedance"""
    clip_path = work_dir / f"clip_{i:02d}.mp4"
    dur = 4

    # Try Pexels
    pex = search_pexels(keyword)
    if pex:
        result = download_clip(pex["url"], clip_path, dur)
        if result:
            print(f"Clip [{i}] PEXELS: {keyword}")
            return result

    # Fallback: seedance
    result = generate_seedance(keyword, clip_path, dur)
    if result:
        print(f"Clip [{i}] SEEDANCE: {keyword}")
        return result

    print(f"Clip [{i}] FAILED: {keyword}")
    return ""

# ── Subtitle overlay ──
def add_subtitle(clip_path, text, output_path):
    """Overlay yellow subtitle at bottom, white shadow"""
    subprocess.run([
        "ffmpeg", "-y", "-i", clip_path,
        "-vf", f"drawtext=text='{text}':fontfile=/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc:"
               f"fontsize=28:fontcolor=yellow:shadowcolor=black:shadowx=2:shadowy=2:"
               f"x=(w-text_w)/2:y=h-text_h-60",
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "copy", output_path
    ], capture_output=True, timeout=15)
    return output_path if os.path.exists(output_path) else clip_path

# ── Main Replica Pipeline ──
def replicate(news_script_segments, work_dir):
    """
    news_script_segments: list of {"time": "0-3s", "text": "口播文案"}
    Returns: list of final clip paths with subtitles
    """
    clips = [""] * len(news_script_segments)

    # Step 1: Get clips in parallel
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {}
        for i, seg in enumerate(news_script_segments):
            kw = extract_keyword(seg["text"])
            futures[pool.submit(get_clip, kw, i, work_dir)] = i

        for future in as_completed(futures):
            i = futures[future]
            clips[i] = future.result()

    # Step 2: Add subtitles
    final_clips = []
    for i, clip in enumerate(clips):
        if not clip or not os.path.exists(clip):
            continue
        text = news_script_segments[i]["text"][:30]
        out = work_dir / f"final_{i:02d}.mp4"
        final_clips.append(add_subtitle(clip, text, out))

    return final_clips

def compose_final(clips, nar_text, work_dir):
    """Concat clips + add narration audio"""
    # TTS
    audio_path = work_dir / "narration.mp3"
    subprocess.run(["edge-tts", "--voice", "zh-CN-XiaoxiaoNeural", "--text", nar_text,
        "--write-media", str(audio_path)], capture_output=True, timeout=60)

    if not audio_path.exists():
        return ""

    # Concat
    concat_file = work_dir / "concat.txt"
    valid = [str(c) for c in clips if c and os.path.exists(str(c)) and str(c).endswith('.mp4')]
    if not valid:
        return ""

    with open(concat_file, "w") as f:
        for c in valid:
            f.write(f"file '{c}'\n")

    output = work_dir / "replica.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file),
        "-i", str(audio_path), "-c:v", "libx264", "-preset", "fast",
        "-c:a", "aac", "-shortest", str(output)
    ], capture_output=True, timeout=120)

    return str(output) if output.exists() else ""

# ── API endpoint ──
def register_replica(app):
    from flask import request, jsonify

    @app.route("/api/replica", methods=["POST"])
    def api_replica():
        from security import current_username
        data = request.get_json() or {}
        topic = data.get("topic", "").strip()
        segments = data.get("segments", [])

        if not segments:
            return jsonify(ok=False, error="请提供分段文案"), 400

        username = current_username()
        job_id, work_dir = video_work_dir(username)

        try:
            clips = replicate(segments, work_dir)
            nar_text = " ".join(s.get("text", "") for s in segments)
            final = compose_final(clips, nar_text, work_dir)

            if not final:
                return jsonify(ok=False, error="Compose failed"), 500

            final_name = f"{job_id}.mp4"
            final_path = video_path(username, final_name)
            finalize_file(final, final_path)
            shutil.rmtree(work_dir, ignore_errors=True)

            return jsonify({
                "ok": True,
                "video_url": f"/api/video-file/{final_name}",
                "clips": len(clips),
                "topic": topic
            })
        except Exception as e:
            import traceback
            traceback.print_exc()
            return jsonify(ok=False, error=str(e)), 500
