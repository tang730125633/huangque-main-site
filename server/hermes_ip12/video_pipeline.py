#!/usr/bin/env python3
"""video_pipeline.py — 抖音视频分析+仿写 6步流水线 v3 (素材库集成)"""
import os, re, json, uuid, time, shutil, subprocess
from pathlib import Path
from artifact_store import find_upload, finalize_file, video_path, video_work_dir

# ── 素材库集成 ──
from media_library import MediaLibrary, KnowledgeBase, get_best_image

# ── AI helper (call_ai returns requests.Response, extract text) ──
def ai_chat(prompt):
    """Call AI and return text content string."""
    from model_router import call_ai
    resp = call_ai([{"role": "user", "content": prompt}])
    try:
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except:
        return resp.text  # fallback

# ── Step 1: Transcribe ──
def transcribe_video(video_path):
    """faster-whisper → full transcript + segments"""
    from faster_whisper import WhisperModel
    model = WhisperModel("tiny", device="cpu", compute_type="int8")
    segments, info = model.transcribe(str(video_path), language="zh", beam_size=5)
    full_text = ""
    segs = []
    for seg in segments:
        full_text += seg.text
        segs.append({"start": round(seg.start, 2), "end": round(seg.end, 2), "text": seg.text.strip()})
    return {"full_text": full_text, "segments": segs}

# ── Step 2: Deconstruct ──
def deconstruct(transcript):
    prompt = f"""你是顶级短视频编导。分析以下抖音视频逐字稿，拆解结构。

逐字稿：{transcript[:3000]}

输出JSON（只输出JSON）：
{{"hook":"开头钩子","hook_type":"疑问/反常识/痛点","structure":["段落1","段落2"],"emotional_curve":["开头:好奇","中段:焦虑"],"key_phrases":["金句1"],"cta":"结尾号召","pacing":"快/慢/交替","scene_count":6}}"""
    resp = ai_chat(prompt)
    m = re.search(r'\{.*\}', resp, re.DOTALL)
    return json.loads(m.group()) if m else {"error": "parse failed", "raw": resp[:200]}

# ── Step 3: Optimize ──
def optimize(analysis, topic, niche, scene_count=6, visual_report=""):
    visual_hint = ""
    if visual_report:
        visual_hint = f"""
原视频画面风格（参考此风格设计画面）：
- 主视觉风格：{visual_report.get('dominant_style','未知')}
- 场景类型：{', '.join(visual_report.get('scene_types',[]))}
- {'有' if visual_report.get('has_person') else '无'}真人出镜
- 色调：中性/科技感深色背景
"""

    prompt = f"""你是抖音爆款写手。参考对标视频分析，写一条全新原创脚本。

对标分析：{analysis}
新主题：{topic}
赛道：{niche}
场景数：{scene_count}
{visual_hint}
输出JSON（visual字段要求具体画面：人物动作+场景+道具+光线，50字以上）：
{{"title":"标题","hook":"3秒钩子文案","scenes":[{{"text":"口播文案","visual":"具体画面描述(50字+):人物+场景+道具+光线","emotion":"情绪","duration_seconds":5}}],"cta":"结尾号召","narration_full":"完整口播文案"}}"""
    resp = ai_chat(prompt)
    m = re.search(r'\{.*\}', resp, re.DOTALL)
    return json.loads(m.group()) if m else {"error": "parse failed", "raw": resp[:200]}

# ── Step 4: Generate Videos via Seedance (parallel) ──
SEEDANCE_KEY = os.environ.get("ARK_API_KEY") or os.environ.get("SEEDANCE_API_KEY", "")
SEEDANCE_API = "https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks"

def _gen_one_video(scene, i, work_dir, owner_username):
    """Generate one video segment via seedance 1.0-pro-fast"""
    import requests as req

    visual = scene.get('visual', '')
    dur = scene.get('duration_seconds', 5) or 5
    dur = max(2, min(12, int(dur)))

    prompt = f"竖屏9:16短视频，电影级打光，写实风格。{visual}"

    try:
        # Submit
        r = req.post(SEEDANCE_API,
            headers={"Authorization": f"Bearer {SEEDANCE_KEY}", "Content-Type": "application/json"},
            json={
                "model": "doubao-seedance-1-0-pro-fast-251015",
                "content": [{"type": "text", "text": prompt}],
                "ratio": "9:16",
                "duration": dur,
                "watermark": False
            }, timeout=15)

        task_id = r.json().get("id", "")
        if not task_id:
            raise Exception(f"No task ID: {r.text[:100]}")

        # Poll
        for _ in range(20):
            time.sleep(4)
            poll = req.get(f"{SEEDANCE_API}/{task_id}",
                headers={"Authorization": f"Bearer {SEEDANCE_KEY}"}, timeout=10)
            pd = poll.json()
            status = pd.get("status", "")
            if status == "succeeded":
                video_url = pd.get("content", {}).get("video_url", "")
                if video_url:
                    vp = work_dir / f"scene_{i:02d}.mp4"
                    vp.write_bytes(req.get(video_url, timeout=60).content)
                    print(f"Video [{i}] SEEDANCE OK: {vp.stat().st_size} bytes ({dur}s)")
                    # Auto-save to library
                    try:
                        kw = visual[:30] if visual else f"scene_{i}"
                        MediaLibrary.add(
                            kw, str(vp), source="seedance",
                            tags=["seedance", "ai_generated"],
                            owner_username=owner_username,
                        )
                    except Exception as le:
                        print(f"Library save: {le}")
                    return str(vp)
                break
            elif status == "failed":
                print(f"Video [{i}] SEEDANCE failed: {pd.get('error','')}")
                break

    except Exception as e:
        print(f"Video [{i}] SEEDANCE error: {e}")

    # Fallback: generate text placeholder image
    try:
        from PIL import Image, ImageDraw
        img_path = work_dir / f"scene_{i:02d}.png"
        colors = [(28,28,40), (20,30,50), (40,20,30)]
        img = Image.new("RGB", (720, 1280), colors[i % 3])
        draw = ImageDraw.Draw(img)
        for j, line in enumerate([visual[i:i+25] for i in range(0, min(len(visual), 100), 25)]):
            draw.text((30, 100 + j*35), line, fill=(200,200,220))
        img.save(str(img_path))
        print(f"Video [{i}] FALLBACK image")
        return str(img_path)
    except:
        return ""

def generate_videos(scenes, work_dir, owner_username):
    """Generate all scene videos in parallel (skip cached)"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    videos = [""] * len(scenes)
    to_generate = [(i, s) for i, s in enumerate(scenes) if not s.get("_cached")]

    # Mark cached as already done
    for i, s in enumerate(scenes):
        if s.get("_cached") and os.path.exists(work_dir / f"scene_{i:02d}.mp4"):
            videos[i] = str(work_dir / f"scene_{i:02d}.mp4")

    if to_generate:
        with ThreadPoolExecutor(max_workers=len(to_generate)) as pool:
            futures = {
                pool.submit(_gen_one_video, s, i, work_dir, owner_username): i
                for i, s in to_generate
            }
            for future in as_completed(futures):
                i = futures[future]
                try: videos[i] = future.result()
                except Exception as e: print(f"Video [{i}] crashed: {e}")

    ok = sum(1 for v in videos if v)
    lib_hits = sum(1 for s in scenes if s.get("_cached"))
    print(f"Videos: {ok}/{len(scenes)} ({lib_hits} from library)")
    return videos

# ── Step 5: TTS ──
def generate_tts(text, work_dir):
    audio_path = work_dir / "narration.mp3"
    subprocess.run(["edge-tts", "--voice", "zh-CN-XiaoxiaoNeural", "--text", text, "--write-media", str(audio_path)],
        capture_output=True, timeout=120)
    return str(audio_path) if audio_path.exists() else ""

# ── Step 6: Compose (concat videos + audio) ──
def compose_video(video_paths, audio_path, work_dir):
    """FFmpeg: concat video segments + overlay audio"""
    output_path = work_dir / "output.mp4"
    valid = [p for p in video_paths if p and os.path.exists(p)]
    if not valid: return ""

    if len(valid) == 1 and valid[0].endswith('.png'):
        # Single image fallback - use old Ken Burns
        per_dur = 10
        probe = subprocess.run(["ffprobe","-v","quiet","-show_entries","format=duration",
            "-of","default=noprint_wrappers=1:nokey=1", audio_path], capture_output=True, text=True)
        dur = float(probe.stdout.strip() or 30)
        cmd = ["ffmpeg","-y","-loop","1","-i",valid[0],"-i",audio_path,
            "-vf",f"scale=1080:1920,zoompan=z='min(zoom+0.001,1.3)':d={int(dur*25)}:s=1080x1920",
            "-c:v","libx264","-preset","fast","-c:a","aac","-shortest",str(output_path)]
    else:
        # Concat multiple videos + overlay audio
        # Write concat file
        concat_file = work_dir / "concat.txt"
        with open(concat_file, "w") as f:
            for v in valid:
                if v.endswith('.mp4'):
                    f.write(f"file '{v}'\n")
        if concat_file.stat().st_size == 0: return ""

        cmd = ["ffmpeg","-y","-f","concat","-safe","0","-i",str(concat_file),
            "-i",audio_path,"-c:v","libx264","-preset","fast","-c:a","aac","-shortest",
            "-vf","scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920",
            str(output_path)]

    subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    return str(output_path) if output_path.exists() else ""
# ── API ──
def register_pipeline(app):

    @app.route("/api/pipeline", methods=["POST"])
    def api_pipeline():
        from flask import request, jsonify
        from security import current_username

        data = request.get_json() or {}
        upload_id = str(data.get("upload_id") or "").strip()
        topic = data.get("topic", "").strip()
        niche = data.get("niche", "美业").strip()

        username = current_username()
        try:
            input_video_path = find_upload(username, upload_id)
        except FileNotFoundError:
            return jsonify(ok=False, error="视频不存在或不属于上传目录"), 400
        if input_video_path.suffix.lower() not in {".mp4", ".mov", ".webm", ".mkv", ".avi"}:
            return jsonify(ok=False, error="视频类型不支持"), 400

        job_id, work_dir = video_work_dir(username)

        try:
            steps = {}

            # 1. Transcribe
            t = transcribe_video(str(input_video_path))
            steps["transcribe"] = "OK"

            # 2. Deconstruct
            if topic:
                analysis = {"topic": topic, "scene_count": 6}
            else:
                analysis = deconstruct(t["full_text"])
            steps["deconstruct"] = "OK"

            # 3. Optimize (with visual analysis)
            sc = analysis.get("scene_count", 6) or 6

            # Run visual analysis on the video
            from video_vision import analyze_video_visual
            visual_report = analyze_video_visual(str(input_video_path), num_frames=6)
            steps["vision"] = f"{visual_report.get('frames_analyzed',0)} frames"

            script = optimize(
                json.dumps(analysis, ensure_ascii=False),
                topic or "短视频仿写", niche, sc,
                visual_report=visual_report
            )
            steps["optimize"] = "OK"

            if "error" in script:
                return jsonify(ok=False, error=script["error"]), 500

            # 4. Videos (seedance) — with library lookup
            scenes = script.get("scenes", [])

            # Check library for cached videos
            lib_hits = 0
            for i, scene in enumerate(scenes):
                kw = scene.get("visual", "")[:30] or scene.get("text", "")[:20]
                cached = MediaLibrary.search(kw, owner_username=username)
                if cached:
                    cached_path = cached[0]["file_path"]
                    if os.path.exists(cached_path) and cached_path.endswith('.mp4'):
                        # Reuse cached video
                        dest = work_dir / f"scene_{i:02d}.mp4"
                        shutil.copy2(cached_path, dest)
                        scenes[i]["_cached"] = True
                        lib_hits += 1
            if lib_hits:
                steps["library_hits"] = f"{lib_hits}/{len(scenes)} reused"

            video_paths = generate_videos(scenes, work_dir, username)
            steps["videos"] = f"{sum(1 for v in video_paths if v)}/{len(scenes)}"

            # 5. TTS
            narration = script.get("narration_full", " ".join(s.get("text","") for s in scenes))
            audio = generate_tts(narration, work_dir)
            steps["tts"] = "OK"

            if not audio:
                return jsonify(ok=False, error="TTS failed"), 500

            # 6. Compose
            video = compose_video(video_paths, audio, work_dir)
            steps["render"] = "OK"

            if not video:
                return jsonify(ok=False, error="Render failed"), 500

            final_name = f"{job_id}.mp4"
            final_path = video_path(username, final_name)
            finalize_file(video, final_path)
            shutil.rmtree(work_dir, ignore_errors=True)

            # Save visual formula to knowledge base
            try:
                KnowledgeBase.add_formula(
                    script.get("title", job_id),
                    {
                        "visual_style": visual_report.get("dominant_style", "auto"),
                        "scene_count": len(scenes),
                        "pacing": analysis.get("pacing", "medium"),
                        "hook_type": analysis.get("hook_type", ""),
                        "visuals": [s.get("visual", "")[:80] for s in scenes]
                    }
                )
            except Exception as ke:
                print(f"Knowledge save: {ke}")

            return jsonify({
                "ok": True,
                "video_url": f"/api/video-file/{final_name}",
                "video_id": job_id,
                "title": script.get("title", ""),
                "scenes": scenes,
                "script": script,
                "steps": steps
            })
        except Exception as e:
            import traceback
            traceback.print_exc()
            return jsonify(ok=False, error=str(e)), 500
