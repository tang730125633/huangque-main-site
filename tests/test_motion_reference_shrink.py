# -*- coding: utf-8 -*-
"""参考视频上传前压到 720p/2Mbps —— 解开 motion 的并发天花板。

2026-07-11 实测，把「motion 并发 >3 会撞墙」这个旧结论彻底证伪：

  * HeyGen 侧 10 路并发：建视频 10/10 成功、零 429、生成不降速(404~511s vs 单条 460s)
  * 唯一挂掉的那条，是我们自己的上传撞了 240s 硬超时
  * 瓶颈是出境隧道上行 ~1.1 MB/s，而每条 motion 要推 23MB 的手机原片上去
        23MB × 10 路 / 1.1 MB/s ≈ 210s  →  贴着 240s 超时线，实测挂 1/10
        3MB  × 10 路 / 1.1 MB/s ≈  30s  →  离超时线十万八千里

而这 23MB 是纯浪费：原片 1920×1080 / 15.4 Mbps，HeyGen 的成片只有 720p / 5~7MB ——
推上去的码率是拿回来的 3.5 倍。参考视频只用来提取动作（HeyGen 提示词写死了
「Do NOT copy the reference video person's appearance」），样貌全部来自 avatar 图。

同 avatar、同素材做过原片/压缩片对比生成：姿态、身份、画质无差异。
"""
import importlib
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SERVER = str(Path(__file__).resolve().parents[1] / "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

video = importlib.import_module("content_domains.video")
core = importlib.import_module("content_domains.core")


class ShrinkTests(unittest.TestCase):
    def _fake_stat(self, size):
        class _S:
            st_size = size
        return _S()

    def test_small_file_is_not_touched(self):
        # 已经够小的参考视频不该白跑一次 ffmpeg
        p = Path("/tmp/ref.mp4")
        with patch.object(Path, "stat", lambda self: self._fake_stat(1024) if False else type("S", (), {"st_size": 1024})()), \
             patch.object(subprocess, "run", side_effect=AssertionError("小文件不该调 ffmpeg")):
            self.assertEqual(video._shrink_reference_video(p), p)

    def test_ffmpeg_failure_falls_back_to_original(self):
        """压缩失败必须回退原片 —— 压缩是优化，不是正确性前提。

        若这里改成抛异常，就把一个「省钱的优化」变成了「新的故障源」：
        服务器没装 ffmpeg / 转码失败 / 磁盘满，动作模仿就会整体不可用。
        """
        p = Path("/tmp/big.mp4")
        big = type("S", (), {"st_size": 24 * 1024 * 1024})()
        with patch.object(Path, "stat", lambda self: big), \
             patch.object(subprocess, "run", side_effect=FileNotFoundError("ffmpeg not found")):
            self.assertEqual(video._shrink_reference_video(p), p)

    def test_empty_output_falls_back_to_original(self):
        p = Path("/tmp/big.mp4")
        sizes = {"/tmp/big.mp4": 24 * 1024 * 1024}

        def fake_stat(self):
            return type("S", (), {"st_size": sizes.get(str(self), 0)})()

        with patch.object(Path, "stat", fake_stat), \
             patch.object(Path, "exists", lambda self: True), \
             patch.object(subprocess, "run", return_value=None):
            # 产物 size=0 → 视为失败 → 回退原片
            self.assertEqual(video._shrink_reference_video(p), p)

    def test_ffmpeg_command_targets_720p_and_drops_audio(self):
        p = Path("/tmp/big.mp4")
        captured = {}

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            return None

        big = type("S", (), {"st_size": 24 * 1024 * 1024})()
        with patch.object(Path, "stat", lambda self: big), \
             patch.object(Path, "exists", lambda self: True), \
             patch.object(subprocess, "run", fake_run):
            out = video._shrink_reference_video(p)

        cmd = captured["cmd"]
        joined = " ".join(cmd)
        self.assertIn("scale=w=1280:h=1280:force_original_aspect_ratio=decrease", joined)  # 长边 1280
        self.assertIn("2000k", joined)          # 2 Mbps
        self.assertIn("-an", cmd)               # 动作参考用不到音轨
        self.assertNotEqual(out, p)             # 压缩成功 → 返回新文件


class DispatchPathTests(unittest.TestCase):
    """压缩必须接在【线路分发之前】，否则只有一条路受益。"""

    def setUp(self):
        self.src = Path(video.__file__).read_text(encoding="utf-8")

    def test_cinematic_no_longer_shrinks(self):
        """⚠️ 反过来了：电影化身【不再压缩】参考视频（kongli 2026-07-14 的决定）。

        压缩是【重编码】（libx264 转 720p/2Mbps），画质有损 —— 而动作模仿的成片质量
        直接取决于参考视频。换了新出境节点（~1.5 MB/s）、上传超时也放宽到 600s 之后，
        压缩省的那点时间不值得拿画质去换。

        改成【只剥音轨】：-c:v copy 只重封装，画面一帧不动，实测省 58% 的上传量
        （HeyGen 的 cinematic_avatar 只看画面，根本不用参考视频的声音）。
        详见 test_motion_audio.py。
        """
        gen = self.src.split("def gen_cinematic")[1].split(chr(10) + "def ")[0]
        code = chr(10).join(ln for ln in gen.splitlines() if not ln.lstrip().startswith("#"))
        self.assertNotIn("_shrink_motion_reference(", code, "还在压缩 —— 画质有损")
        self.assertIn("_strip_audio(", code)

    def test_heygen_path_does_not_shrink_twice(self):
        # 分发前已经压过，HeyGen 上传处不该再压一遍（白烧一次 ffmpeg）
        self.assertNotIn("_heygen_upload_asset(_shrink_reference_video(", self.src)


if __name__ == "__main__":
    unittest.main()
