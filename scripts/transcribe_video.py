#!/usr/bin/env python3
"""
视频转口播文案 — 从 MediaCrawler 抓取的视频数据中下载视频 → 提取音频 → faster-whisper 转文字。
支持搜索(search)和创作者(creator)两种模式的 jsonl 数据。

前置依赖（run_local 环境已自带）：
  pip install faster-whisper imageio-ffmpeg

模型首次运行会自动下载(~484MB)；国内网络可设 HF_ENDPOINT=https://hf-mirror.com 加速，
或手动下载放到 models/faster-whisper-small/ 目录。

用法：
  # 单个视频(指定 aweme_id)
  python scripts/transcribe_video.py data/douyin/jsonl/search_contents_*.jsonl --id 7578832165515281595

  # 批量：取点赞最高的 top N 个视频
  python scripts/transcribe_video.py data/douyin/jsonl/creator_contents_*.jsonl --top 10

  # 批量全部(所有有下载链接的视频)
  python scripts/transcribe_video.py data/douyin/jsonl/search_contents_*.jsonl --all

  # 输出：每视频一个 .txt + 汇总 summary.jsonl
"""
import sys, os, io, json, subprocess, glob, argparse, hashlib
import httpx

# 模型可选：tiny(74MB)/base(141MB)/small(484MB)/medium(1.5GB)/large(2.9GB)
# small 在中文上性价比最好
MODEL_SIZE = os.environ.get("WHISPER_MODEL", "small")
MODEL_PATH = os.path.join(os.path.dirname(__file__), "..", "models", f"faster-whisper-{MODEL_SIZE}")

try:
    import imageio_ffmpeg
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    FFMPEG = "ffmpeg"


def load_works(pattern):
    """加载 jsonl，返回有 video_download_url 的作品列表。"""
    works = []
    seen = set()
    for p in glob.glob(pattern):
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                aid = d.get("aweme_id")
                url = d.get("video_download_url") or ""
                if url and aid and aid not in seen:
                    seen.add(aid)
                    works.append(d)
    return works


def download_video(url, out_path, timeout=120, retries=4):
    """下载视频，已存在且完整则跳过；带重试。"""
    if os.path.exists(out_path) and os.path.getsize(out_path) > 100_000:
        print(f"     视频已存在 ({os.path.getsize(out_path)//1024} KB)")
        return True
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/125.0 Safari/537.36",
        "Referer": "https://www.douyin.com/",
    }
    for attempt in range(retries):
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True, trust_env=False) as c:
                r = c.get(url, headers=headers)
                r.raise_for_status()
                with open(out_path, "wb") as f:
                    f.write(r.content)
            print(f"     下载完成 ({os.path.getsize(out_path)//1024} KB)")
            return True
        except Exception as e:
            print(f"     下载失败 (重试 {attempt+1}/{retries}): {e}")
    return False


def extract_audio(mp4, wav, sample_rate=16000):
    """ffmpeg 提取单声道 16k wav，已存在则跳过。"""
    if os.path.exists(wav) and os.path.getsize(wav) > 10_000:
        print(f"     音频已存在 ({os.path.getsize(wav)//1024} KB)")
        return True
    try:
        subprocess.run(
            [FFMPEG, "-y", "-i", mp4, "-ar", str(sample_rate), "-ac", "1", "-vn", wav],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"     音频提取完成 ({os.path.getsize(wav)//1024} KB)")
        return True
    except subprocess.CalledProcessError as e:
        print(f"     音频提取失败: {e}")
        return False


def transcribe(wav, model_path=MODEL_PATH, model_size=MODEL_SIZE, language="zh"):
    """faster-whisper 转文字，返回完整文案文本。"""
    from faster_whisper import WhisperModel
    # 优先加载本地路径，否则走 huggingface 下载
    if os.path.isdir(model_path) and os.path.exists(os.path.join(model_path, "config.json")):
        model_id = model_path
    else:
        model_id = model_size
    model = WhisperModel(model_id, device="cpu", compute_type="int8")
    segments, info = model.transcribe(wav, language=language, vad_filter=True)
    text = "".join(seg.text for seg in segments).strip()
    return text, info


def main():
    ap = argparse.ArgumentParser(description="视频转口播文案 — MediaCrawler 数据 → 文字稿")
    ap.add_argument("jsonl", help="jsonl 路径或通配(如 creator_contents_*.jsonl)")
    ap.add_argument("--id", default="", help="指定 aweme_id(单个视频)")
    ap.add_argument("--top", type=int, default=0, help="批量:点赞最高的top N个")
    ap.add_argument("--all", action="store_true", help="批量:所有有下载链接的视频")
    ap.add_argument("--out", default="", help="输出目录(默认 transcribe_out)")
    ap.add_argument("--skip-download", action="store_true", help="跳过下载,只用已有 mp4/wav")
    args = ap.parse_args()

    works = load_works(args.jsonl)
    print(f"加载 {len(works)} 个有下载链接的视频", file=sys.stderr)

    # 筛选
    if args.id:
        works = [w for w in works if w["aweme_id"] == args.id]
        if not works:
            print(f"未找到 aweme_id={args.id}", file=sys.stderr); sys.exit(1)
    elif args.top > 0:
        works.sort(key=lambda w: int(w.get("liked_count", "0").replace("万", "0000") or 0), reverse=True)
        works = works[:args.top]
    elif not args.all:
        print("请指定 --id / --top / --all", file=sys.stderr); sys.exit(1)

    out_dir = args.out or os.path.join(os.path.dirname(__file__), "..", "transcribe_out")
    os.makedirs(out_dir, exist_ok=True)

    summary = []
    success, fail = 0, 0

    for i, w in enumerate(works, 1):
        aid = w["aweme_id"]
        title = (w.get("title") or w.get("desc") or "")[:50]
        url = w["video_download_url"]
        print(f"\n[{i}/{len(works)}] {title}", file=sys.stderr)
        print(f"  aweme_id: {aid}", file=sys.stderr)

        mp4 = os.path.join(out_dir, f"{aid}.mp4")
        wav = os.path.join(out_dir, f"{aid}.wav")
        txt = os.path.join(out_dir, f"{aid}.txt")

        # 文案已存在则跳过
        if os.path.exists(txt) and os.path.getsize(txt) > 10:
            with open(txt, encoding="utf-8") as f:
                text = f.read().strip()
            print(f"     文案已存在 ({len(text)} 字)", file=sys.stderr)
        else:
            if not args.skip_download:
                print("     ① 下载视频…", file=sys.stderr)
                if not download_video(url, mp4):
                    fail += 1; continue
                print("     ② 提取音频…", file=sys.stderr)
                if not extract_audio(mp4, wav):
                    fail += 1; continue
            print("     ③ 语音识别…", file=sys.stderr)
            try:
                text, info = transcribe(wav)
                print(f"     ④ 识别完成: {info.language} (置信 {info.language_probability:.2f})"
                      f" / {len(text)} 字", file=sys.stderr)
                with open(txt, "w", encoding="utf-8") as f:
                    f.write(text)
            except Exception as e:
                print(f"     识别失败: {e}", file=sys.stderr)
                fail += 1; continue

        success += 1
        summary.append({
            "aweme_id": aid, "title": title,
            "nickname": w.get("nickname", ""),
            "liked_count": w.get("liked_count", ""),
            "comment_count": w.get("comment_count", ""),
            "char_count": len(text),
            "txt_path": txt,
            "text": text,
        })

    # 汇总
    summary_path = os.path.join(out_dir, "summary.jsonl")
    with open(summary_path, "w", encoding="utf-8") as f:
        for s in summary:
            # 写入不含 text 的元信息行 + text 的完整行（双线，text 不进索引避免膨胀）
            meta = {k: v for k, v in s.items() if k != "text"}
            f.write(json.dumps(meta, ensure_ascii=False) + "\n")
    print(f"\n✅ 完成: 成功 {success} / 失败 {fail}", file=sys.stderr)

    # 输出汇总到 stdout（可管道给其他工具）
    print(json.dumps({"success": success, "fail": fail, "summary": summary_path},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
