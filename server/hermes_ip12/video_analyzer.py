"""
视频对标分析器 — 下载 → 转文字 → AI拆解结构 → 喂给视频工厂
"""
import ipaddress, os, re, json, socket, uuid, time, subprocess, shutil, tempfile, urllib.parse
import requests as http_requests
from pathlib import Path
from flask import request, jsonify
from artifact_store import (
    StorageQuotaExceeded,
    analysis_dir,
    atomic_write_bytes,
    finalize_file,
    new_asset_id,
    reserve_capacity,
    video_path as owned_video_path,
    video_work_dir,
)
ANALYSIS_MAX_DOWNLOAD_MB = max(
    1, int(os.environ.get("HERMES_ANALYSIS_MAX_DOWNLOAD_MB", "200"))
)
ANALYSIS_MAX_DOWNLOAD_BYTES = ANALYSIS_MAX_DOWNLOAD_MB * 1024 * 1024
ANALYSIS_MAX_DOWNLOAD_ARG = f"{ANALYSIS_MAX_DOWNLOAD_MB}M"
VIDEO_HOSTS = (
    "douyin.com", "iesdouyin.com", "tiktok.com", "tiktokv.com",
    "xiaohongshu.com", "xhslink.com", "bilibili.com", "b23.tv",
    "kuaishou.com", "gifshow.com", "youtube.com", "youtu.be",
    "weibo.com", "weibo.cn", "ixigua.com", "toutiao.com",
)


def is_public_video_url(url):
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return False
        host = parsed.hostname.rstrip(".").lower()
        if not any(host == allowed or host.endswith(f".{allowed}") for allowed in VIDEO_HOSTS):
            return False
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(
                host,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        }
        return bool(addresses) and all(ipaddress.ip_address(address).is_global for address in addresses)
    except (OSError, ValueError):
        return False


def register_video_analyzer(app):

    @app.route("/api/analyze-video", methods=["POST"])
    def api_analyze_video():
        """下载对标视频 → 转文字 → AI拆解"""
        from model_router import call_ai
        from security import current_username

        body = request.get_json() or {}
        url = body.get("url", "").strip()

        if not url:
            return jsonify({"ok": False, "error": "请输入视频链接"}), 400
        if not is_public_video_url(url):
            return jsonify({"ok": False, "error": "仅支持公网 HTTP/HTTPS 视频链接"}), 400

        username = current_username()
        analysis_id = new_asset_id()
        final_dir = None

        try:
            with reserve_capacity(ANALYSIS_MAX_DOWNLOAD_BYTES) as reservation:
                with tempfile.TemporaryDirectory(prefix="hermes-analysis-") as temp_dir:
                    work_dir = Path(temp_dir)

                    # Download and transcription are staged outside the final
                    # owner directory while their worst-case size is reserved.
                    video_path = download_video(url, work_dir)
                    if not video_path:
                        return jsonify({"ok": False, "error": "视频下载失败，请检查链接是否有效"}), 400
                    source_video = Path(video_path).resolve()
                    if source_video.stat().st_size > ANALYSIS_MAX_DOWNLOAD_BYTES:
                        raise StorageQuotaExceeded("analysis download exceeds size limit")

                    transcript = transcribe_video(str(source_video), work_dir)
                    if not transcript:
                        return jsonify({"ok": False, "error": "语音转文字失败"}), 500
                    analysis = analyze_transcript(call_ai, transcript, url)

                    _, final_dir = analysis_dir(username, analysis_id)
                    final_video = final_dir / f"source{source_video.suffix.lower()}"
                    try:
                        finalize_file(
                            source_video, final_video, reservation=reservation
                        )
                        result = {
                            "analysis_id": analysis_id,
                            "owner_username": username,
                            "url": url,
                            "transcript": transcript,
                            "analysis": analysis,
                            "video_path": str(final_video),
                        }
                        atomic_write_bytes(
                            final_dir / "result.json",
                            json.dumps(
                                result, ensure_ascii=False, indent=2
                            ).encode("utf-8"),
                            reservation=reservation,
                        )
                    except Exception:
                        shutil.rmtree(final_dir, ignore_errors=True)
                        raise

            return jsonify({
                "ok": True,
                "analysis_id": analysis_id,
                "transcript": transcript[:500],
                "analysis": analysis
            })

        except StorageQuotaExceeded as e:
            if final_dir is not None:
                shutil.rmtree(final_dir, ignore_errors=True)
            return jsonify({"ok": False, "error": str(e)}), 507
        except Exception as e:
            if final_dir is not None:
                shutil.rmtree(final_dir, ignore_errors=True)
            import traceback
            traceback.print_exc()
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.route("/api/generate-from-analysis", methods=["POST"])
    def api_generate_from_analysis():
        """基于分析结果生成对标视频"""
        from model_router import call_ai
        from security import current_username

        body = request.get_json()
        analysis_id = body.get("analysis_id", "").strip()
        topic = body.get("topic", "").strip()
        niche = body.get("niche", "美业").strip()

        if not re.fullmatch(r"[0-9a-f]{10}", analysis_id) or not topic:
            return jsonify({"ok": False, "error": "缺少参数"}), 400

        username = current_username()
        try:
            _, work_dir = analysis_dir(username, analysis_id, create=False)
        except FileNotFoundError:
            return jsonify({"ok": False, "error": "分析结果不存在"}), 404
        result_file = work_dir / "result.json"

        if not result_file.exists():
            return jsonify({"ok": False, "error": "分析结果不存在"}), 404

        with open(result_file, "r", encoding="utf-8") as f:
            result = json.load(f)

        analysis = result.get("analysis", "")
        transcript = result.get("transcript", "")

        # Generate script with reference
        from video_factory import generate_all_images, generate_tts_pro, generate_subtitles, compose_video_pro

        video_id, vwork_dir = video_work_dir(username)

        try:
            script = generate_script_with_reference(call_ai, topic, niche, analysis, transcript)
            scenes = generate_all_images(scenes=script["scenes"], work_dir=vwork_dir)
            audio_path = generate_tts_pro(script["narration_full"], vwork_dir)
            subtitle_path = generate_subtitles(script["scenes"], vwork_dir)
            video_path = compose_video_pro(scenes, audio_path, subtitle_path, vwork_dir)

            final_name = f"{video_id}.mp4"
            final_path = owned_video_path(username, final_name)
            finalize_file(video_path, final_path)
            shutil.rmtree(vwork_dir, ignore_errors=True)

            return jsonify({
                "ok": True,
                "video_url": f"/api/video-file/{final_name}",
                "video_id": video_id,
                "title": script["title"],
                "scenes": [{"narration": s["narration"][:80], "image_url": s.get("image_url", "")} for s in scenes],
                "script": script["narration_full"]
            })
        except Exception as e:
            shutil.rmtree(vwork_dir, ignore_errors=True)
            import traceback
            traceback.print_exc()
            return jsonify({"ok": False, "error": str(e)}), 500


# ═══════════════════════════════════════════
# DOWNLOAD
# ═══════════════════════════════════════════

def download_video(url, work_dir):
    """yt-dlp 下载视频"""
    output = work_dir / "%(id)s.%(ext)s"

    result = subprocess.run([
        "yt-dlp",
        "-f", "best[height<=1080]",
        "-o", str(output),
        "--no-playlist",
        "--max-filesize", ANALYSIS_MAX_DOWNLOAD_ARG,
        "--socket-timeout", "30",
        url
    ], capture_output=True, text=True, timeout=120, cwd=str(work_dir))

    # Find the downloaded file
    video_files = list(work_dir.glob("*.mp4")) + list(work_dir.glob("*.webm")) + list(work_dir.glob("*.mkv"))
    if video_files:
        return str(video_files[0])

    # Try with different format
    result2 = subprocess.run([
        "yt-dlp",
        "-f", "worst[ext=mp4]",
        "-o", str(output),
        "--no-playlist",
        "--max-filesize", ANALYSIS_MAX_DOWNLOAD_ARG,
        "--socket-timeout", "30",
        url
    ], capture_output=True, text=True, timeout=120, cwd=str(work_dir))

    video_files = list(work_dir.glob("*.mp4"))
    return str(video_files[0]) if video_files else None


# ═══════════════════════════════════════════
# TRANSCRIBE
# ═══════════════════════════════════════════

def transcribe_video(video_path, work_dir):
    """faster-whisper 语音转文字"""
    from faster_whisper import WhisperModel

    # Extract audio
    audio_path = work_dir / "audio.mp3"
    subprocess.run([
        "ffmpeg", "-y", "-i", video_path,
        "-vn", "-acodec", "libmp3lame", "-ar", "16000", "-ac", "1",
        str(audio_path)
    ], capture_output=True, check=True, timeout=60)

    # Transcribe with tiny model (fast, low memory)
    model = WhisperModel("tiny", device="cpu", compute_type="int8")
    segments, _ = model.transcribe(str(audio_path), language="zh", beam_size=5)

    transcript = " ".join([seg.text.strip() for seg in segments])
    return transcript


# ═══════════════════════════════════════════
# ANALYZE
# ═══════════════════════════════════════════

def analyze_transcript(call_ai, transcript, url):
    """AI 拆解对标视频结构"""
    prompt = f"""你是一位顶级短视频分析师。请分析这段对标视频的字幕文本，拆解它的创作公式。

━━━━━━━━━━━━━━━━━━━━━━━━
📹 视频来源：{url}

📝 字幕文本：
{transcript[:3000]}

━━━━━━━━━━━━━━━━━━━━━━━━
请从以下维度拆解（输出纯文本，不要JSON）：

1. 🎣 开场钩子（前3秒说了什么？用了什么技巧？痛点/反常识/悬念/数字？）

2. 📖 内容结构（分几段？每段在讲什么？节奏怎么推进的？）

3. 😭 情绪曲线（有没有制造焦虑→给希望→证明→号召的过程？）

4. 🎬 画面暗示（从文案推测画面是什么风格：口播/剧情/混剪？人物出镜还是纯图文？）

5. 💰 转化设计（有没有引导点赞/关注/私信/成交？怎么做的不让人反感？）

6. 🧬 可复制公式（用一句话总结这个视频的创作模板）

分析要具体，引用原文中的句子作为证据。"""

    resp = call_ai(
        [{"role": "system", "content": "你是顶级短视频分析师。输出结构清晰，具体引用原文。"},
         {"role": "user", "content": prompt}],
        stream=False, temperature=0.5
    )
    return resp.json()["choices"][0]["message"]["content"]


# ═══════════════════════════════════════════
# GENERATE WITH REFERENCE
# ═══════════════════════════════════════════

def generate_script_with_reference(call_ai, topic, niche, analysis, transcript):
    """基于对标分析生成模仿脚本"""
    prompt = f"""你是一位顶级短视频导演。现在你要为一个{niche}行业账号创作一条视频。

━━━━━━━━━━━━━━━━━━━━━━━━
📌 你的话题：{topic}

📋 参考对标视频的分析：
{analysis[:2000]}

📝 参考视频原文案（节选）：
{transcript[:1000]}

━━━━━━━━━━━━━━━━━━━━━━━━
⚠️ 关键要求：
1. 模仿参考视频的「结构框架」和「节奏」，但内容完全替换为你的话题
2. 参考怎么开头的，你就怎么开头。参考怎么转折的，你就怎么转折
3. 把参考视频里的话题/案例换成{niche}行业相关的内容
4. 保留参考视频的情绪曲线和说服逻辑

输出严格JSON：
{{
  "title": "标题",
  "narration_full": "完整旁白",
  "scenes": [
    {{"narration":"配音","visual":"英文图片描述(必须精准对应本段内容)","duration":6,"subtitle":"字幕"}}
  ]
}}
3-5个场景。口语化，用"姐""说实话"这些词。"""

    resp = call_ai(
        [{"role": "system", "content": "你是顶级短视频导演。输出严格JSON。"},
         {"role": "user", "content": prompt}],
        stream=False, temperature=0.85
    )
    text = resp.json()["choices"][0]["message"]["content"]
    json_match = re.search(r'\{.*\}', text, re.DOTALL)
    if not json_match:
        raise Exception(f"AI未返回JSON: {text[:300]}")
    return json.loads(json_match.group())
