import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from content_domains import points, video


class XiaolePointsTests(unittest.TestCase):
    """果肉视频按模型、分辨率和时长计价；参考图不额外收点。"""

    def test_generation_price_matrix(self):
        cases = [
            ("grok-imagine-video", "480p", 10),
            ("grok-imagine-video", "720p", 12),
            ("grok-imagine-video-1.5", "480p", 15),
            ("grok-imagine-video-1.5", "720p", 25),
            ("grok-imagine-video-1.5", "1080p", 44),
        ]
        for model, resolution, rate in cases:
            self.assertEqual(points.cost_of("xiaole_video", {
                "model": model, "resolution": resolution, "duration": 5,
            }), rate * 5)

    def test_edit_is_under_maintenance(self):
        with self.assertRaisesRegex(ValueError, "编辑维护中"):
            points.cost_of("xiaole_video", {"operation": "edit", "source_duration": 3.0})

    def test_reference_images_do_not_change_price(self):
        body = {"model": "grok-imagine-video-1.5", "resolution": "1080p", "duration": 10}
        base = points.cost_of("xiaole_video", body)
        self.assertEqual(points.cost_of("xiaole_video", dict(body, reference_images=["data:image/jpeg;base64,x"])), base)

    def test_duration_is_capped_at_15(self):
        self.assertEqual(points.cost_of("xiaole_video", {"duration": 999}), 15 * 12)

    def test_default_duration_when_missing(self):
        self.assertEqual(points.cost_of("xiaole_video", {}), 10 * 12)

    def test_submit_path_and_validator_stay_wired_together(self):
        core_src = (Path(video.__file__).with_name("core.py")).read_text(encoding="utf-8")
        self.assertIn('elif kind == "xiaole_video":', core_src)
        self.assertIn('validate_xiaole_video_payload(body, user["username"])', core_src)
        self.assertTrue(callable(video.validate_xiaole_video_payload))


if __name__ == "__main__":
    unittest.main()
