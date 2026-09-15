#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""成片封面补截：给历史 done 视频任务批量生成封面。

流程：扫 jobs 里 status='done' 的视频类任务（无 cover_url 且有可用视频输入）
→ ffmpeg 截第 1 秒帧 → 上传 COS（video-covers/<job_id>.jpg）→ cover_url 写回 result。

视频输入按三级兜底（2026-09-16 补截实战：老任务 COS 签名链接已过期，直接下载 403）：
  1. 直链截帧：result 的 video_url/url 或本地 OUT_DIR 文件（jobs_store 同口径）；
  2. COS SDK 直取：链接指向本桶（COS/CDN 域名）时，按 URL 路径还原对象键，
     用服务端密钥下载视频本体再截帧——绕过过期签名；
  3. 平台缩略图兜底：视频本体彻底不可达时，取 result 里的 image_url/thumbnail_url
     等图片，转成 JPG 当封面（仍是该视频的画面，不是新做海报）。

用法（服务器 /home/ubuntu/content-api 目录下）：
    python3 scripts/backfill_video_covers.py --limit 20 --dry-run     # 先试 20 条，只看不写
    python3 scripts/backfill_video_covers.py --limit 0               # 0=全部
    python3 scripts/backfill_video_covers.py --limit 0 --workers 6

幂等：已有 cover_url 的跳过；中断重跑接着补。只写 result 的 cover_url 字段，
不动 status/refunded/其他字段（CAS 只认 done 行）。
"""
import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from content_domains import jobs_store  # noqa: E402  顶层只依赖标准库，import 无副作用
from content_domains import cos  # noqa: E402

JOB_DB = os.environ.get("CONTENT_JOBS_DB") or os.path.join(ROOT, "content_jobs.db")
FFMPEG_TIMEOUT = 90
IMAGE_MAX_BYTES = 20 * 1024 * 1024  # 缩略图兜底单张上限
IMAGE_FETCH_TIMEOUT = 30


def db():
    return sqlite3.connect(JOB_DB, timeout=30)


def collect_candidates(limit):
    with closing(db()) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT id,result FROM jobs "
            "WHERE status='done' AND COALESCE(deleted,0)=0 AND result IS NOT NULL "
            "ORDER BY id DESC" + (" LIMIT ?" if limit else ""),
            (int(limit),) if limit else (),
        ).fetchall()
    candidates = []
    for row in rows:
        try:
            result = json.loads(row["result"] or "{}")
        except Exception:
            continue
        if not isinstance(result, dict) or result.get("cover_url"):
            continue
        # 只收真视频任务：直链必须是视频文件形态（mp4/mov/webm），本地文件
        # 则是 OUT_DIR 里的视频本体。图片任务/页面链接（采集任务的来源页）
        # 不算成片视频，不给它们编封面。
        try:
            src, kind = jobs_store._video_cover_input(result)
        except Exception:
            src = ""
        if not src:
            continue
        if kind == "url" and not _looks_like_video_url(src):
            continue
        candidates.append((int(row["id"]), result))
    return candidates


def ffmpeg_frame(ffmpeg, src, out_jpg):
    """截第 1 秒帧缩到 854 宽 JPG。

    ffmpeg 对单帧图片输入会「0 帧、退出码 0、不写文件」——产出为空时去掉
    -ss 再截一次（取首帧），两次都空才算失败。
    """
    for seek in (["-ss", "1"], []):
        cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        ] + seek + [
            "-i", src,
            "-frames:v", "1", "-vf", "scale=854:-2:flags=lanczos",
            "-q:v", "4", out_jpg,
        ]
        subprocess.run(cmd, capture_output=True, timeout=FFMPEG_TIMEOUT, check=True)
        if os.path.isfile(out_jpg) and os.path.getsize(out_jpg) > 0:
            return
    raise RuntimeError("ffmpeg 未产出封面文件")


def _looks_like_video_url(url):
    """URL 本身是不是视频文件形态（.mp4/.mov/.webm/.m4v）。"""
    try:
        path = urllib.parse.urlparse(url).path.lower()
    except Exception:
        return False
    return path.endswith((".mp4", ".mov", ".webm", ".m4v"))


def _cos_key_from_url(url):
    """链接若指向本桶（COS 域名或 COS_DOMAIN），还原对象键；否则返回空串。"""
    if not cos.enabled():
        return ""
    try:
        parts = urllib.parse.urlparse(url)
        host = (parts.hostname or "").lower()
        key = urllib.parse.unquote(parts.path or "").lstrip("/")
    except Exception:
        return ""
    if not host or not key:
        return ""
    bucket_host = "%s.cos.%s.myqcloud.com" % (cos._BUCKET.lower(), cos._REGION.lower())
    domain_host = ""
    if cos._DOMAIN:
        try:
            domain_host = (urllib.parse.urlparse(cos._DOMAIN).hostname or "").lower()
        except Exception:
            pass
    if host != bucket_host and (not domain_host or host != domain_host):
        return ""
    return key


def _image_urls_in(result):
    """result 里所有图片形态的公网链接（image_url/thumbnail_url/cover 等），
    缩略图兜底按 image* 优先、其余按出现顺序。"""
    found = []

    def walk(obj):
        if isinstance(obj, dict):
            for key, value in obj.items():
                if isinstance(value, str) and value.startswith(("http://", "https://")):
                    if any(t in key.lower() for t in ("image", "cover", "thumb", "poster", "frame")):
                        found.append(value)
                else:
                    walk(value)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(result)
    return found


def _fetch(url, dest):
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "hq-cover-backfill/1.0"})
    with urllib.request.urlopen(req, timeout=IMAGE_FETCH_TIMEOUT) as resp:
        size = 0
        with open(dest, "wb") as fp:
            while True:
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                size += len(chunk)
                if size > IMAGE_MAX_BYTES:
                    raise RuntimeError("图片超过 20MB 上限")
                fp.write(chunk)


def _image_to_jpg(td, result, ffmpeg):
    """缩略图兜底：下载 result 里的图片，转成 JPG 返回路径；全部失败返回 ""。"""
    for url in _image_urls_in(result)[:3]:
        raw = os.path.join(td, "raw_img")
        try:
            _fetch(url, raw)
        except Exception:
            continue
        if not os.path.isfile(raw) or os.path.getsize(raw) <= 0:
            continue
        try:
            with open(raw, "rb") as fp:
                magic = fp.read(12)
            out = os.path.join(td, "cover.jpg")
            if magic.startswith(b"\xff\xd8"):  # JPEG 直接用
                os.replace(raw, out)
            elif magic.startswith((b"\x89PNG", b"GIF8", b"RIFF", b"\x00\x00\x01\x00", b"BM")):
                # PNG/GIF/WebP(RIFF)/ICO/BMP → ffmpeg 转 JPG
                cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                       "-i", raw, "-frames:v", "1", "-vf", "scale=854:-2:flags=lanczos",
                       "-q:v", "4", out]
                subprocess.run(cmd, capture_output=True, timeout=FFMPEG_TIMEOUT, check=True)
            else:
                continue
            if os.path.isfile(out) and os.path.getsize(out) > 0:
                return out
        except Exception:
            continue
    return ""


def generate_cover(job_id, result, dry_run):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg 不在 PATH")
    src, _kind = jobs_store._video_cover_input(result)
    if not src:
        raise RuntimeError("无可用视频输入")
    with tempfile.TemporaryDirectory(prefix="hq-cover-") as td:
        out_jpg = os.path.join(td, "cover.jpg")
        last_err = None
        # ① 直链/本地文件截帧（新任务主路径）
        try:
            ffmpeg_frame(ffmpeg, src, out_jpg)
        except Exception as exc:
            last_err = exc
            # ② COS SDK 直取：老签名链接过期/CDN 403 都绕过
            key = _cos_key_from_url(src)
            if key:
                local_video = os.path.join(td, "video.mp4")
                try:
                    cos.download(key, local_video)
                    ffmpeg_frame(ffmpeg, local_video, out_jpg)
                    last_err = None
                except Exception as exc2:
                    last_err = exc2
        # ③ 视频本体彻底不可达：平台缩略图兜底（仍是该视频画面）
        if last_err is not None or not os.path.isfile(out_jpg):
            fallback = _image_to_jpg(td, result, ffmpeg)
            if fallback:
                out_jpg = fallback
            else:
                raise RuntimeError("视频与缩略图均不可达：%s" % last_err)
        if not os.path.isfile(out_jpg) or os.path.getsize(out_jpg) <= 0:
            raise RuntimeError("未产出封面文件")
        if dry_run:
            return "dry-run-ok"
        cover_url = cos.upload(out_jpg, "video-covers/%d.jpg" % job_id, content_type="image/jpeg")
    if not cover_url or not cover_url.startswith("http"):
        raise RuntimeError("COS 上传未返回直链")
    if not dry_run:
        jobs_store._write_cover_url(db, job_id, cover_url)
    return cover_url


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="只处理最近 N 条（0=全部）")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true", help="只统计候选不截帧")
    args = parser.parse_args()

    if not cos.enabled() and not args.dry_run:
        print("COS 未配置（缺 COS_SECRET_ID/KEY/REGION/BUCKET），无法上传封面", file=sys.stderr)
        return 2

    candidates = collect_candidates(args.limit)
    print("候选任务 %d 条（limit=%s, dry_run=%s, db=%s）"
          % (len(candidates), args.limit or "全部", args.dry_run, JOB_DB), flush=True)
    if args.dry_run:
        for jid, result in candidates[:20]:
            src, kind = jobs_store._video_cover_input(result)
            print("  job %d <- %s" % (jid, (src or "")[:80]), flush=True)
        return 0
    if not candidates:
        return 0

    ok = fail = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(generate_cover, jid, result, args.dry_run): jid
                   for jid, result in candidates}
        for future in as_completed(futures):
            jid = futures[future]
            try:
                detail = future.result()
                ok += 1
                print("job %d OK %s" % (jid, detail), flush=True)
            except Exception as exc:
                fail += 1
                print("job %d FAIL %s: %s" % (jid, type(exc).__name__, str(exc)[:160]), flush=True)

    print("完成：成功 %d / 失败 %d" % (ok, fail), flush=True)
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
