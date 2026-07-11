import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from content_domains import points, video


class GrokOfficialPointsTests(unittest.TestCase):
    def test_edit_price_includes_video_input_and_720p_output(self):
        with patch.dict(os.environ, {"GROK_VIDEO_PROVIDER": "xai", "XAI_USD_CNY": "7.3", "XAI_PRICE_BUFFER": "1.2"}):
            self.assertEqual(points.cost_of("xiaole_video", {"channel": "grok", "operation": "edit", "source_duration": 8.7}), 61)

    def test_standard_price_scales_with_duration_and_resolution(self):
        env = {"GROK_VIDEO_PROVIDER": "xai", "XAI_USD_CNY": "7.3", "XAI_PRICE_BUFFER": "1.2"}
        with patch.dict(os.environ, env, clear=False):
            five_480 = points.cost_of("xiaole_video", {
                "channel": "grok", "model": "grok-imagine-video", "duration": 5, "resolution": "480p",
            })
            ten_720 = points.cost_of("xiaole_video", {
                "channel": "grok", "model": "grok-imagine-video", "duration": 10, "resolution": "720p",
            })
        self.assertEqual(five_480, 22)
        self.assertEqual(ten_720, 62)

    def test_video_15_image_input_and_resolution_cost_more(self):
        env = {"GROK_VIDEO_PROVIDER": "xai", "XAI_USD_CNY": "7.3", "XAI_PRICE_BUFFER": "1.2"}
        with patch.dict(os.environ, env, clear=False):
            cost = points.cost_of("xiaole_video", {
                "channel": "grok", "model": "grok-imagine-video-1.5", "duration": 10,
                "resolution": "720p", "reference_images": ["data:image/jpeg;base64,x"],
            })
        self.assertEqual(cost, 124)

    def test_submit_path_and_validator_stay_wired_together(self):
        core_src = (Path(video.__file__).with_name("core.py")).read_text(encoding="utf-8")
        self.assertIn('elif kind == "xiaole_video":', core_src)
        self.assertIn("validate_xiaole_video_payload(body)", core_src)
        self.assertTrue(callable(video.validate_xiaole_video_payload))


if __name__ == "__main__":
    unittest.main()
