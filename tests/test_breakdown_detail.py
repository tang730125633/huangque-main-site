# -*- coding: utf-8 -*-
import importlib
import json
import pathlib
import sys
import unittest
from unittest import mock


SERVER = pathlib.Path(__file__).resolve().parents[1] / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

breakdown = importlib.import_module("content_domains.breakdown")


class BreakdownDetailPromptTests(unittest.TestCase):
    def _run_breakdown(self):
        """驱动一次非反推的 _do_breakdown，捕获发给模型的 sysmsg/usermsg。"""
        captured = {}
        fake_tikhub = mock.Mock()
        fake_tikhub.detail.return_value = {
            "play_url": "https://cdn.example/video.mp4",
            "duration": 30,
            "title": "detail prompt regression",
        }
        fake_tikhub.download_to_file.return_value = None
        fake_tikhub.transcript.return_value = []

        def fake_request(sysmsg, usermsg, context, frames):
            captured.update(sysmsg=sysmsg, usermsg=usermsg)
            return {"rhythm": [], "scenes": [], "viral_logic": "", "template": ""}

        with mock.patch.dict(sys.modules, {"tikhub": fake_tikhub}), \
             mock.patch.object(breakdown, "_probe_duration", return_value=30.0), \
             mock.patch.object(
                 breakdown, "_extract_frames", return_value=(None, ["frame.jpg"])
             ), \
             mock.patch.object(
                 breakdown, "_request_breakdown_result", side_effect=fake_request
             ):
            result = breakdown._do_breakdown(
                {"_job_id": 7},
                {"platform": "douyin", "id": "123", "note_type": "video"},
                "https://www.douyin.com/video/123",
            )
        self.assertEqual("breakdown", result["type"])
        return captured

    def test_usermsg_requests_lighting_audio_transition_fields(self):
        captured = self._run_breakdown()
        usermsg = captured["usermsg"]
        for field in ('"lighting"', '"audio"', '"transition"'):
            self.assertIn(field, usermsg)

    def test_usermsg_requires_200_300_chars_and_replayable_detail(self):
        captured = self._run_breakdown()
        usermsg = captured["usermsg"]
        self.assertIn("200-300 字", usermsg)
        self.assertIn("1:1 复拍", usermsg)
        self.assertNotIn("80-140 字", usermsg)

    def test_usermsg_details_rhythm_strategy_and_viral_logic(self):
        captured = self._run_breakdown()
        usermsg = captured["usermsg"]
        self.assertIn("strategy 写 2-3 句", usermsg)
        self.assertIn("钩子", usermsg)
        self.assertIn("留存", usermsg)
        self.assertIn("转化", usermsg)

    def test_sysmsg_targets_replayable_storyboard(self):
        captured = self._run_breakdown()
        self.assertEqual(
            "你是黄雀传媒资深短视频编导。分析视频关键帧和口播，拆解为可直接复拍的完整分镜脚本，"
            "详细程度以让拍摄团队无需再追问原作为准。只输出 JSON，不要多余内容。",
            captured["sysmsg"],
        )


if __name__ == "__main__":
    unittest.main()
