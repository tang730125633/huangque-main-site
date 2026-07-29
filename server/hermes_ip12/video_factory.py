"""
视频工厂 V3 — 重写出图+TTS核心
改进：Pollinations flux模型+增强提示词、专业配音、毛玻璃转场、Ken Burns效果
"""
import os, re, json, uuid, time, tempfile, subprocess, shutil, urllib.parse
import requests as http_requests
from pathlib import Path
from flask import request, jsonify
from artifact_store import finalize_file, video_path as owned_video_path, video_work_dir

def register_video_factory(app):

    @app.route("/api/generate-video", methods=["POST"])
    def api_generate_video():
        from model_router import call_ai
        from security import current_username

        body = request.get_json()
        topic = body.get("topic", "").strip()
        niche = body.get("niche", "美业").strip()
        style = body.get("style", "story").strip()

        if not topic:
            return jsonify({"ok": False, "error": "请输入话题"}), 400

        username = current_username()
        video_id, work_dir = video_work_dir(username)

        try:
            script = generate_script(call_ai, topic, niche, style)
            scenes = generate_all_images(scenes=script["scenes"], work_dir=work_dir)
            audio_path = generate_tts_pro(script["narration_full"], work_dir)
            subtitle_path = generate_subtitles(script["scenes"], work_dir)
            video_path = compose_video_pro(scenes, audio_path, subtitle_path, work_dir)

            final_name = f"{video_id}.mp4"
            final_path = owned_video_path(username, final_name)
            finalize_file(video_path, final_path)
            shutil.rmtree(work_dir, ignore_errors=True)

            return jsonify({
                "ok": True,
                "video_url": f"/api/video-file/{final_name}",
                "video_id": video_id,
                "title": script["title"],
                "scenes": [{"narration": s["narration"][:80], "image_url": s.get("image_url", "")} for s in scenes],
                "script": script["narration_full"]
            })
        except Exception as e:
            shutil.rmtree(work_dir, ignore_errors=True)
            import traceback
            traceback.print_exc()
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.route("/api/video-file/<filename>")
    def api_video_file(filename):
        from flask import send_file
        from security import current_username
        try:
            path = owned_video_path(current_username(), filename)
        except FileNotFoundError:
            return jsonify({"error": "not found"}), 404
        if not path.exists():
            return jsonify({"error": "not found"}), 404
        return send_file(str(path), mimetype="video/mp4")


# ═══════════════════════════════════════════
# 1. SCRIPT — 更强的 AI 脚本生成
# ═══════════════════════════════════════════

def generate_script(call_ai, topic, niche, style):
    prompt = f"""你是一位顶级短视频导演。为一个{niche}行业视频生成完整脚本。

话题：{topic}

━━━━━━━━━━━━━━━━━━━━━━━━
⚠️ 铁律：每个场景的visual描述必须精准对应本场景的narration内容。不要说"美容院"这么泛——要说"美容师俯身在客户耳边轻声询问需求，客户犹豫的表情，暖黄灯光"这种具体画面。

输出严格JSON：
{{
  "title": "视频标题（<12字，必须有吸引力）",
  "narration_full": "完整旁白（必须像真人聊天。用短句。用'姐''说实话''你仔细想''我跟你讲'这些词。每句不超过20字。不要书面语。）",
  "scenes": [
    {{
      "narration": "本场景配音（15-25字，必须口语化）",
      "visual": "英文图片描述。关键：描述的画面必须是本段narration正在说的那个具体场景。不要模糊。不要万能描述。不少于25个英文单词。",
      "duration": 6,
      "subtitle": "字幕（<15字）"
    }}
  ]
}}
3-4个场景。不要写废话。"""

    resp = call_ai(
        [{"role": "system", "content": "你是顶级短视频导演。输出严格JSON，visual描述必须极致详细。"},
         {"role": "user", "content": prompt}],
        stream=False, temperature=0.85
    )
    text = resp.json()["choices"][0]["message"]["content"]
    json_match = re.search(r'\{.*\}', text, re.DOTALL)
    if not json_match:
        raise Exception(f"AI返回无效: {text[:300]}")
    return json.loads(json_match.group())


# ═══════════════════════════════════════════
# 2. IMAGES — Pollinations flux模型 + 重试 + 高质量prompt
# ═══════════════════════════════════════════

QUALITY_SUFFIX = ", cinematic lighting, hyperrealistic, sharp focus, professional photography, 8K, masterpiece"

def generate_all_images(scenes, work_dir):
    """通义万相 出图 — 阿里 DashScope，高质量，国内直连"""
    DASHSCOPE_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
    API = "https://dashscope.aliyuncs.com/api/v1/services/aigc/text2image/image-synthesis"
    TASK_API = "https://dashscope.aliyuncs.com/api/v1/tasks"

    headers = {"Authorization": f"Bearer {DASHSCOPE_KEY}", "Content-Type": "application/json",
               "X-DashScope-Async": "enable"}

    # Step 1+2: Submit and poll each scene sequentially
    for i, scene in enumerate(scenes):
        prompt = scene["visual"].strip()
        img_path = work_dir / f"scene_{i:02d}.jpg"

        try:
            payload = {"model": "wanx-v1", "input": {"prompt": prompt[:500]},
                       "parameters": {"size": "1024*1024", "n": 1}}
            resp = http_requests.post(API, headers=headers, json=payload, timeout=30)
            if resp.status_code != 200:
                raise Exception(f"Submit failed: {resp.status_code}")

            tid = resp.json()["output"]["task_id"]

            # Poll this task
            for _ in range(40):  # Up to 80 seconds
                time.sleep(2)
                r = http_requests.get(f"{TASK_API}/{tid}", headers={
                    "Authorization": f"Bearer {DASHSCOPE_KEY}"}, timeout=15)
                if r.status_code != 200:
                    continue
                data = r.json()
                status = data.get("output", {}).get("task_status", "")
                if status == "SUCCEEDED":
                    img_url = data["output"]["results"][0]["url"]
                    ir = http_requests.get(img_url, timeout=30)
                    if ir.status_code == 200:
                        with open(img_path, "wb") as f:
                            f.write(ir.content)
                        scene["image_path"] = str(img_path)
                        scene["image_url"] = img_url
                    break
                elif status == "FAILED":
                    break
        except Exception:
            pass

        if "image_path" not in scene or not os.path.exists(str(img_path)):
            colors = [("20,25,50","40,20,55"),("15,35,55","50,20,45"),("25,20,55","35,40,50")]
            c1, c2 = colors[i % 3]
            try:
                subprocess.run(["ffmpeg","-y","-f","lavfi","-i",
                    f"gradients=s=1024x1024:c0=rgb({c1}):c1=rgb({c2})",
                    "-frames:v","1",str(img_path)],capture_output=True,timeout=8)
            except: pass
            scene["image_path"] = str(img_path)
            scene["image_url"] = ""

    return scenes


# ═══════════════════════════════════════════
# 3. TTS — 专业配音，停顿自然
# ═══════════════════════════════════════════

def generate_tts_pro(narration_text, work_dir):
    """专业配音：预先格式化文本让语音更自然"""
    audio_path = work_dir / "narration.mp3"
    text_file = work_dir / "narration.txt"

    # Format text: add natural pauses via punctuation
    formatted = narration_text.strip()
    # Ensure sentences end with punctuation for natural pauses
    if not formatted.endswith(('.','!','?','。','！','？')):
        formatted += '。'

    with open(text_file, "w", encoding="utf-8") as f:
        f.write(formatted)

    # zh-CN-YunyangNeural = professional male, news/documentary style
    # rate=-3% for slightly slower, more deliberate delivery
    result = subprocess.run(
        ["edge-tts", "--voice", "zh-CN-YunyangNeural",
         "--rate=-3%",
         "-f", str(text_file),
         "--write-media", str(audio_path)],
        capture_output=True, text=True, timeout=90
    )
    if result.returncode != 0:
        raise Exception(f"TTS: {result.stderr[:200]}")

    return str(audio_path)


# ═══════════════════════════════════════════
# 4. SUBTITLES
# ═══════════════════════════════════════════

def generate_subtitles(scenes, work_dir):
    srt_path = work_dir / "subtitles.srt"
    t = 0.0
    with open(srt_path, "w", encoding="utf-8") as f:
        for i, scene in enumerate(scenes):
            dur = scene.get("duration", 5)
            start, end = t, t + dur
            t = end
            def fmt(x):
                h, m = divmod(int(x), 3600)
                m, s = divmod(m, 60)
                return f"{h:02d}:{m:02d}:{int(s):02d},{int((x%1)*1000):03d}"
            f.write(f"{i+1}\n{fmt(start)} --> {fmt(end)}\n{scene.get('subtitle', scene['narration'])}\n\n")
    return str(srt_path)


# ═══════════════════════════════════════════
# 5. VIDEO COMPOSITION — Ken Burns + fade transitions
# ═══════════════════════════════════════════

def compose_video_pro(scenes, audio_path, subtitle_path, work_dir):
    video_path = work_dir / "output.mp4"

    # Build filter complex for Ken Burns + crossfade
    # Each image: zoompan for slow zoom, then fade out
    filter_parts = []
    seg_count = len(scenes)

    for i, scene in enumerate(scenes):
        dimg = scene.get("image_path", "")
        if not dimg or not os.path.exists(dimg):
            dimg = str(work_dir / f"emergency_{i}.jpg")
            subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=0x1a1a2e:s=1024x1792:d=1",
                          "-frames:v", "1", dimg], capture_output=True, timeout=5)

        dur = scene.get("duration", 5)
        fps = 25
        total_frames = int(dur * fps)

        # Ken Burns: slow zoom in (1.0 → 1.08 over the clip duration)
        zoompan = (
            f"zoompan=z='min(zoom+0.0004,1.08)':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1080x1920:fps={fps}"
        )

        filter_parts.append(f"[{i}:v]{zoompan},format=yuv420p,setpts=PTS-STARTPTS[v{i}]")

    # Build the full filter
    inputs = ""
    for i in range(seg_count):
        inputs += f"-loop 1 -t {scenes[i].get('duration',5)} -i \"{scenes[i].get('image_path','')}\" "

    # Concat all video segments
    concat_inputs = "".join([f"[v{i}]" for i in range(seg_count)])
    filter_complex = ";".join(filter_parts) + f";{concat_inputs}concat=n={seg_count}:v=1:a=0[outv]"

    # Actually, let's use the simpler segment approach that works
    # Generate each segment separately with Ken Burns, then concat
    seg_files = []
    for i, scene in enumerate(scenes):
        seg = work_dir / f"seg_{i:02d}.mp4"
        img = scene.get("image_path", "")
        dur = scene.get("duration", 5)

        if not os.path.exists(img):
            img = str(work_dir / f"e_{i}.jpg")
            subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=0x1a1a2e:s=1024x1792",
                          "-frames:v", "1", img], capture_output=True, timeout=5)

        # Ken Burns effect on single image
        subprocess.run([
            "ffmpeg", "-y",
            "-loop", "1", "-i", img,
            "-t", str(dur),
            "-vf", (
                "zoompan=z='min(zoom+0.0005,1.08)':d=1:"
                "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
                "s=1080x1920:fps=25,"
                "fade=t=in:st=0:d=0.5,fade=t=out:st={0}:d=0.5".format(dur - 0.5)
            ),
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-preset", "fast", "-crf", "23",
            str(seg)
        ], capture_output=True, check=True, timeout=30)
        seg_files.append(str(seg))

    # Concat
    concat_txt = work_dir / "concat.txt"
    with open(concat_txt, "w") as f:
        for sf in seg_files:
            f.write(f"file '{sf}'\n")

    temp_video = work_dir / "temp_v.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(concat_txt), "-c", "copy", str(temp_video)
    ], capture_output=True, check=True, timeout=30)

    # Add audio + subtitles + background music
    # Generate subtle ambient background music
    ambient_path = work_dir / "ambient.mp3"
    audio_dur = float(subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", audio_path],
        text=True).strip())

    subprocess.run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"sine=frequency=220:duration={audio_dur}",
        "-f", "lavfi", "-i", f"sine=frequency=330:duration={audio_dur}",
        "-filter_complex", "[0:a][1:a]amix=inputs=2:duration=first:weights=0.08 0.06,volume=0.15",
        "-c:a", "libmp3lame", "-q:a", "2",
        str(ambient_path)
    ], capture_output=True, timeout=15)
    subprocess.run([
        "ffmpeg", "-y",
        "-i", str(temp_video),
        "-i", audio_path,
        "-i", str(ambient_path),
        "-filter_complex", (
            "[1:a][2:a]amix=inputs=2:duration=first:weights=1.0 0.5[amix];"
            f"[0:v]subtitles={subtitle_path}:"
            "force_style='FontName=WenQuanYi Zen Hei,FontSize=36,"
            "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,Outline=3,"
            "Alignment=2,Bold=1,MarginV=60'[outv]"
        ),
        "-map", "[outv]", "-map", "[amix]",
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-shortest", "-movflags", "+faststart",
        str(video_path)
    ], capture_output=True, check=True, timeout=90)

    return str(video_path)
