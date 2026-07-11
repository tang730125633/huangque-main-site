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


class WorkerCountTests(unittest.TestCase):
    def test_motion_workers_stay_conservative_until_wavespeed_is_measured(self):
        """动作模仿的 worker 数不能拿 HeyGen 的实测当依据。

        我一度把它提到 10，理由是「HeyGen 10 路无 429、生成不降速」——但动作模仿的实际
        供应商是 WaveSpeed，而 WaveSpeed 的并发能力从没测过。HeyGen 的那个 10 应该留给
        走 HeyGen 的功能，不该外溢到这里。在 WaveSpeed 压测出来之前，保守留 3。
        """
        self.assertEqual(core.MOTION_JOB_WORKERS, 3)


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

    def test_shrink_happens_right_after_the_reference_lands_on_disk(self):
        # 防回归：改了等于没改的经典形态——函数写了，但没人调
        self.assertIn("reference_video_file = _shrink_motion_reference(reference_video_file)", self.src)

    def test_shrink_is_before_both_providers(self):
        """必须早于两个供应商的调用点，两条路才都吃到压缩后的文件。

        我第一版把它接在 HeyGen 的上传处——而动作模仿走的是 WaveSpeed，
        等于压缩完全没作用在正路上。
        （锚点不能用 `if line == "2":`——换装那边也有一处同样的字样，会先匹配到。）
        """
        shrink_at = self.src.index("_shrink_motion_reference(reference_video_file)")
        for callee in ("wavespeed.generate_motion(", "generate_heygen_motion_video(image_file"):
            self.assertLess(shrink_at, self.src.index(callee),
                            "压缩晚于 %s → 这条路拿到的还是原片" % callee)

    def test_heygen_path_does_not_shrink_twice(self):
        # 分发前已经压过，HeyGen 上传处不该再压一遍（白烧一次 ffmpeg）
        self.assertNotIn("_heygen_upload_asset(_shrink_reference_video(", self.src)


if __name__ == "__main__":
    unittest.main()
