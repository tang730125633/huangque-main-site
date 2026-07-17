import importlib
import sys
import unittest
from pathlib import Path


class BreakdownTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server_dir = str(Path(__file__).resolve().parents[1] / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)
        cls.breakdown = importlib.import_module("content_domains.breakdown")

    def setUp(self):
        self.orig_heartbeat = self.breakdown._heartbeat
        self.orig_extract_frames = self.breakdown._extract_frames
        self.orig_chat_multimodal = self.breakdown._chat_multimodal
        self.orig_tempfile = self.breakdown.tempfile.NamedTemporaryFile
        self.orig_tikhub = sys.modules.get("tikhub")

    def tearDown(self):
        self.breakdown._heartbeat = self.orig_heartbeat
        self.breakdown._extract_frames = self.orig_extract_frames
        self.breakdown._chat_multimodal = self.orig_chat_multimodal
        self.breakdown.tempfile.NamedTemporaryFile = self.orig_tempfile
        if self.orig_tikhub is None:
            sys.modules.pop("tikhub", None)
        else:
            sys.modules["tikhub"] = self.orig_tikhub

    def _install_fake_env(self, raw_json, transcript=None):
        calls = {}

        class FakeTikHub:
            @staticmethod
            def detail(platform, item_id, note_type=None):
                calls["detail"] = (platform, item_id, note_type)
                return {
                    "play_url": "https://example.test/demo.mp4",
                    "duration": 18,
                    "title": "团购探店案例",
                }

            @staticmethod
            def download_to_file(play_url, deadline, filename):
                calls["download"] = (play_url, filename)

            @staticmethod
            def transcript(det, video_path=None):
                calls["transcript"] = (det.get("title"), video_path)
                return transcript if transcript is not None else [{"start": 0, "end": 3, "text": "先看门头"}]

        self.breakdown._heartbeat = lambda job_id, phase: calls.setdefault("phases", []).append(phase)
        self.breakdown._extract_frames = lambda video_path, count, duration: (
            "fake-frame-dir",
            ["frame_1.jpg", "frame_2.jpg"],
        )

        def fake_chat_multimodal(sysmsg, usermsg, frames, temp=0.7):
            calls["sysmsg"] = sysmsg
            calls["usermsg"] = usermsg
            calls["frames"] = list(frames)
            return raw_json

        self.breakdown._chat_multimodal = fake_chat_multimodal
        self.breakdown.tempfile.NamedTemporaryFile = lambda suffix="", delete=False: type("Tmp", (), {"name": "fake-video.mp4"})()
        sys.modules["tikhub"] = FakeTikHub
        return calls

    def test_do_breakdown_returns_analysis_and_requests_it_in_prompt(self):
        calls = self._install_fake_env(
            '{"scenes":[{"dur":"3s","scene":"门店门头","line":"今天带你看一家店"}],"analysis":"这是一条团购探店口播视频"}'
        )

        result = self.breakdown._do_breakdown(
            {"_job_id": 11},
            {"platform": "douyin", "id": "abc123"},
            "https://example.test/post/1",
        )

        self.assertEqual(result["type"], "breakdown")
        self.assertEqual(result["source_platform"], "douyin")
        self.assertEqual(result["analysis"], "这是一条团购探店口播视频")
        self.assertEqual(result["scenes"][0]["scene"], "门店门头")
        self.assertIn('"analysis"', calls["usermsg"])
        self.assertIn("同时输出一份视频内容综合分析", calls["sysmsg"])
        self.assertIn("每个 scene 一句话说清画面", calls["usermsg"])
        self.assertEqual(calls["frames"], ["frame_1.jpg", "frame_2.jpg"])
        self.assertEqual(calls["phases"], ["downloading", "extracting_frames", "transcribing", "analyzing"])

    def test_do_breakdown_defaults_analysis_to_empty_string(self):
        self._install_fake_env(
            '{"scenes":[{"dur":"4s","scene":"产品特写","line":"重点看这个细节"}]}'
        )

        result = self.breakdown._do_breakdown(
            {"_job_id": 12},
            {"platform": "xiaohongshu", "id": "note-9", "note_type": "video"},
            "https://example.test/post/2",
        )

        self.assertEqual(result["analysis"], "")
        self.assertEqual(result["source_title"], "团购探店案例")
        self.assertEqual(result["duration"], 18)
        self.assertEqual(len(result["scenes"]), 1)

    def test_parse_breakdown_json_accepts_fenced_json(self):
        result = self.breakdown._parse_breakdown_json(
            '```json\n{"scenes":[{"dur":"3s","scene":"门头","line":"欢迎来到门店"}],"analysis":"先钩子再转化"}\n```'
        )

        self.assertEqual(result["analysis"], "先钩子再转化")
        self.assertEqual(result["scenes"][0]["scene"], "门头")

    def test_parse_breakdown_json_accepts_wrapped_prose(self):
        result = self.breakdown._parse_breakdown_json(
            '下面是拆解结果，请直接取 JSON：\n```json\n{"scenes":[{"dur":"5s","scene":"产品特写","line":"先看成分"}],"analysis":"中段突出卖点"}\n```\n请查收。'
        )

        self.assertEqual(result["analysis"], "中段突出卖点")
        self.assertEqual(result["scenes"][0]["line"], "先看成分")

    def test_parse_breakdown_json_ignores_trailing_braces_in_prose(self):
        result = self.breakdown._parse_breakdown_json(
            '{"scenes":[{"dur":"4s","scene":"护理镜头","line":"重点看手法"}],"analysis":"结尾给行动指令"}\n备注：字段 {analysis} 已生成。'
        )

        self.assertEqual(result["analysis"], "结尾给行动指令")
        self.assertEqual(result["scenes"][0]["dur"], "4s")

    def test_parse_breakdown_json_raises_same_error_for_invalid_output(self):
        with self.assertRaisesRegex(ValueError, "拆解结果解析失败，请重试"):
            self.breakdown._parse_breakdown_json("not json at all")


if __name__ == "__main__":
    unittest.main()
