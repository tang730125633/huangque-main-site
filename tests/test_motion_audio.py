# -*- coding: utf-8 -*-
"""动作模仿：上传前剥音轨，出片后合回原声。不再压缩。

## 为什么剥音轨

HeyGen 的 cinematic_avatar 【只看画面】—— 它不会用参考视频的声音。音轨对它是纯浪费，
却要经过我们那条 ~1.5 MB/s 的出境隧道推上去。

实测：一段 6 秒的参考视频，剥掉音轨后 0.101 MB → 0.042 MB，**少传 58%**。

## 为什么要把原声合回去

HeyGen 的成片【本身没有声音】。用户上传的参考视频是有声的 —— 成片配回原声，观感上才是
「同一条片子，只是换了个人演」。

## ⚠️ 不再压缩（kongli 的决定，2026-07-14）

原来会把 >6MB 的参考视频转码成 720p/2Mbps —— 那是【重编码】，画质有损。而动作模仿的
成片质量直接取决于参考视频。换了新出境节点（~1.5 MB/s）、上传超时也放宽到 600s 之后，
压缩省的那点时间不值得拿画质去换。

**剥音轨不是压缩**：`-c:v copy` 只重封装，画面一帧不动。

## 三条红线

1. **顺序**：先抽原声，再剥音轨 —— 剥完就抽不出来了。
2. **一律回退，绝不失败**：剥不动就原样上传；合不回去就保留无声成片。
   这些都是优化，不是正确性前提 —— 绝不能把一个省带宽的优化变成新的故障源。
3. **参考视频本来就没音轨**是常态，不是错误。
"""
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SERVER = str(ROOT / "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

os.environ.setdefault("CONTENT_BASE", tempfile.mkdtemp())
video = importlib.import_module("content_domains.video")
SRC = (ROOT / "server/content_domains/video.py").read_text(encoding="utf-8")
GEN = SRC.split("def gen_cinematic")[1].split("\ndef ")[0]


class TheOrderMattersTests(unittest.TestCase):
    def test_the_audio_is_extracted_before_it_is_stripped(self):
        """⚠️ 剥完就抽不出来了。顺序反了，用户永远拿不到原声。"""
        i_extract = GEN.index("_extract_reference_audio")
        i_strip = GEN.index("_strip_audio")
        self.assertLess(i_extract, i_strip, "先抽原声，再剥音轨")

    def test_the_audio_comes_from_the_first_reference(self):
        """第一个参考视频同时也是决定成片时长的那个（_cinematic_duration）——
        取它的声音，时长才对得上。"""
        self.assertIn("_extract_reference_audio(video_files[0])", GEN)


class NoMoreTranscodingTests(unittest.TestCase):
    """⚠️ 不再压缩（kongli 2026-07-14）。动作模仿的成片质量直接取决于参考视频，
    转码成 720p/2Mbps 是拿画质换带宽 —— 换了新节点之后不值得。"""

    def test_the_cinematic_path_no_longer_shrinks(self):
        self.assertNotIn("_shrink_motion_reference", GEN,
                         "还在压缩 —— 那是重编码，画质有损")

    def test_stripping_audio_does_not_re_encode(self):
        """剥音轨【不是】压缩：-c:v copy 只重封装，画面一帧不动。"""
        block = SRC.split("def _strip_audio")[1].split("\ndef ")[0]
        self.assertIn('"-c:v", "copy"', block)
        self.assertNotIn("libx264", block, "剥个音轨还重编码，等于偷偷压缩")


class EverythingFallsBackTests(unittest.TestCase):
    """这些都是优化，不是正确性前提。绝不能把一个省带宽的优化变成新的故障源。"""

    def test_a_failed_strip_uploads_the_original(self):
        with patch.object(video.subprocess, "run", side_effect=RuntimeError("ffmpeg 没了")):
            got = video._strip_audio("/tmp/whatever.mp4")
        self.assertEqual(str(got), str(Path("/tmp/whatever.mp4")), "剥不动就原样上传")

    def test_a_reference_without_audio_is_not_an_error(self):
        """参考视频本来就没音轨 —— 很常见，不是错误。成片保持无声，任务照常成功。"""
        with patch.object(video, "_resolve_out_file", return_value=None):
            self.assertIsNone(video._extract_reference_audio("video/x.mp4"))

    def test_a_failed_mux_keeps_the_silent_video(self):
        """⚠️ 宁可无声，也不能因为配音失败就把成片丢了 —— 那可是花了 $7 的片子。"""
        with patch.object(video, "_resolve_out_file", side_effect=lambda f: None):
            got = video._mux_original_audio("video/out.mp4", "audio/src.m4a")
        self.assertEqual(got, "video/out.mp4")

    def test_the_mux_is_skipped_when_there_is_no_audio(self):
        self.assertIn("if source_audio:", GEN)


class TheMuxIsWiredCorrectlyTests(unittest.TestCase):
    def test_the_cover_is_taken_from_the_final_video(self):
        """封面得从【合了声音之后】的成片抽 —— 顺序反了，封面对应的是被丢弃的中间产物。"""
        i_mux = GEN.index("_mux_original_audio")
        i_cover = GEN.index("_extract_first_frame_cover")
        self.assertLess(i_mux, i_cover)

    def test_the_video_stream_is_copied_not_re_encoded(self):
        """合声音不该动画面 —— HeyGen 出的片子是花了钱的，重编码一遍纯属糟蹋。"""
        block = SRC.split("def _mux_original_audio")[1].split("\ndef ")[0]
        self.assertIn('"-c:v", "copy"', block)

    def test_mismatched_lengths_use_shortest(self):
        """成片 4~15 秒（自适应向上取整），原声是参考视频的实际长度 —— 对不齐是常态。
        -shortest：宁可音频末尾少一点，也不要视频尾巴上挂一段黑屏。"""
        block = SRC.split("def _mux_original_audio")[1].split("\ndef ")[0]
        self.assertIn('"-shortest"', block)


if __name__ == "__main__":
    unittest.main()
