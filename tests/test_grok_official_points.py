import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from content_domains import points, video


class XiaolePointsTests(unittest.TestCase):
    """果肉视频统一 30 点/秒 × 时长（kongli 2026-07-15）。

    此前是按 xAI 官方成本×汇率×缓冲动态算（model/resolution/reference_images 都影响价），
    现在改成扁平 30 点/秒 —— 这些因素不再进价格。生成走 duration(上限 15s)，编辑走
    source_duration(上限 8.7s)，缺失兜底 10s。
    """

    def test_generation_is_thirty_per_second(self):
        self.assertEqual(points.cost_of("xiaole_video", {"duration": 5}), 150)    # 5 × 30
        self.assertEqual(points.cost_of("xiaole_video", {"duration": 10}), 300)   # 10 × 30

    def test_edit_uses_source_duration_ceil(self):
        # 编辑走 source_duration，上限 8.7s，向上取整：8.7 → 9 → 270
        self.assertEqual(points.cost_of("xiaole_video", {"operation": "edit", "source_duration": 8.7}), 270)
        self.assertEqual(points.cost_of("xiaole_video", {"operation": "edit", "source_duration": 3.0}), 90)

    def test_model_resolution_images_no_longer_matter(self):
        base = points.cost_of("xiaole_video", {"duration": 10})
        for extra in ({"model": "grok-imagine-video-1.5", "resolution": "1080p"},
                      {"reference_images": ["data:image/jpeg;base64,x"]},
                      {"channel": "micro"}):
            self.assertEqual(points.cost_of("xiaole_video", dict(extra, duration=10)), base,
                             "%s 不该再影响果肉价" % list(extra))

    def test_duration_is_capped_at_15(self):
        self.assertEqual(points.cost_of("xiaole_video", {"duration": 999}), 15 * 30)

    def test_default_duration_when_missing(self):
        self.assertEqual(points.cost_of("xiaole_video", {}), 10 * 30)   # 缺 duration 兜底 10s

    def test_submit_path_and_validator_stay_wired_together(self):
        core_src = (Path(video.__file__).with_name("core.py")).read_text(encoding="utf-8")
        self.assertIn('elif kind == "xiaole_video":', core_src)
        self.assertIn("validate_xiaole_video_payload(body)", core_src)
        self.assertTrue(callable(video.validate_xiaole_video_payload))


if __name__ == "__main__":
    unittest.main()
