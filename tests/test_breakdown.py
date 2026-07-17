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
        self.assertFalse(result["asr_failed"])
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

    def test_do_breakdown_reverse_prompt_returns_prompt_and_keeps_asr_flag(self):
        import os, tempfile
        thumb = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        thumb.write(bytes.fromhex(
            "ffd8ffe000104a46494600010101006000600000ffdb004300080606070605080707070909080a0c"
            "140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20242e2720222c231c1c2837292c3031343434"
            "1f27393d38323c2e333432ffdb0043010909090c0b0c180d0d1832211c21323232323232323232"
            "323232323232323232323232323232323232323232323232323232323232323232323232323232"
            "ffc00011080001000103012200021101031101ffc4001400010000000000000000000000000000"
            "0008ffc40014100100000000000000000000000000000000ffda0008010100013f10c9b0a3c4ff"
            "d9"
        ))
        thumb.close()
        calls = self._install_fake_env(
            '```\n轻奢美容院场景，主角手持精华产品，暖金柔光，近景推镜，突出肌肤通透感与活动钩子\n```',
            transcript=None,
        )
        self.breakdown._extract_frames = lambda video_path, count, duration: (
            "fake-frame-dir",
            [thumb.name, thumb.name],
        )

        try:
            result = self.breakdown._do_breakdown(
                {"_job_id": 14, "mode": "reverse_prompt"},
                {"platform": "douyin", "id": "rev-1"},
                "https://example.test/post/reverse",
                "reverse_prompt",
            )
        finally:
            os.unlink(thumb.name)

        self.assertEqual(result["type"], "breakdown_reverse")
        self.assertEqual(result["source_platform"], "douyin")
        self.assertIn("轻奢美容院场景", result["prompt"])
        self.assertEqual(result["frame_count"], 2)
        self.assertEqual(len(result["frame_thumbnails"]), 2)
        self.assertTrue(result["frame_thumbnails"][0].startswith("data:image/jpeg;base64,"))
        self.assertFalse(result["asr_failed"])
        self.assertIn("反推出一条适合后续作图/创作的中文提示词", calls["usermsg"])
        self.assertIn("只输出提示词本身", calls["sysmsg"])
        self.assertEqual(calls["phases"], ["downloading", "extracting_frames", "transcribing", "analyzing"])

    def test_breakdown_scenes_retries_once_when_parse_fails(self):
        calls = []
        original_parse = self.breakdown._parse_breakdown_json
        try:
            def fake_chat_multimodal(sysmsg, usermsg, frames, temp=0.7):
                calls.append((sysmsg, usermsg, list(frames), temp))
                return 'first' if len(calls) == 1 else '{"scenes":[],"analysis":"ok"}'

            seen = {"count": 0}
            def fake_parse(raw):
                seen["count"] += 1
                if seen["count"] == 1:
                    raise ValueError("拆解结果解析失败，请重试")
                return original_parse(raw)

            self.breakdown._chat_multimodal = fake_chat_multimodal
            self.breakdown._parse_breakdown_json = fake_parse
            result = self.breakdown._breakdown_scenes_from_frames("标题", 18, "douyin", "文案", ["f1.jpg"])
        finally:
            self.breakdown._parse_breakdown_json = original_parse

        self.assertEqual(result["analysis"], "ok")
        self.assertEqual(len(calls), 2)

    def test_breakdown_reverse_prompt_retries_once_when_clean_fails(self):
        calls = []
        original_clean = self.breakdown._clean_reverse_prompt
        try:
            def fake_chat_multimodal(sysmsg, usermsg, frames, temp=0.7):
                calls.append((sysmsg, usermsg, list(frames), temp))
                return 'first' if len(calls) == 1 else '轻奢美容院场景，主角手持精华产品，暖金柔光'

            seen = {"count": 0}
            def fake_clean(raw):
                seen["count"] += 1
                if seen["count"] == 1:
                    raise ValueError("反推结果解析失败，请重试")
                return original_clean(raw)

            self.breakdown._chat_multimodal = fake_chat_multimodal
            self.breakdown._clean_reverse_prompt = fake_clean
            result = self.breakdown._reverse_prompt_from_frames("标题", 18, "douyin", "文案", ["f1.jpg"])
        finally:
            self.breakdown._clean_reverse_prompt = original_clean

        self.assertIn("轻奢美容院场景", result)
        self.assertEqual(len(calls), 2)

    def test_gen_breakdown_rejects_unknown_mode(self):
        with self.assertRaisesRegex(ValueError, "mode 仅支持 scenes / reverse_prompt"):
            self.breakdown.gen_breakdown({"url": "https://example.test/v/1", "mode": "mystery"})

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

    def test_parse_breakdown_json_raises_same_error_for_invalid_output(self):
        with self.assertRaisesRegex(ValueError, "拆解结果解析失败，请重试"):
            self.breakdown._parse_breakdown_json("not json at all")

    def test_do_breakdown_records_asr_failure(self):
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
                raise RuntimeError("ASR service unavailable")

        self.breakdown._heartbeat = lambda job_id, phase: calls.setdefault("phases", []).append(phase)
        self.breakdown._extract_frames = lambda video_path, count, duration: (
            "fake-frame-dir",
            ["frame_1.jpg", "frame_2.jpg"],
        )

        def fake_chat_multimodal(sysmsg, usermsg, frames, temp=0.7):
            calls["sysmsg"] = sysmsg
            calls["usermsg"] = usermsg
            calls["frames"] = list(frames)
            return '{"scenes":[{"dur":"3s","scene":"门头","line":"欢迎光临"}],"analysis":"探店视频"}'

        self.breakdown._chat_multimodal = fake_chat_multimodal
        self.breakdown.tempfile.NamedTemporaryFile = lambda suffix="", delete=False: type("Tmp", (), {"name": "fake-video.mp4"})()
        sys.modules["tikhub"] = FakeTikHub

        result = self.breakdown._do_breakdown(
            {"_job_id": 13},
            {"platform": "douyin", "id": "abc456"},
            "https://example.test/post/3",
        )

        self.assertTrue(result["asr_failed"])
        self.assertIn("ASR 转录失败", calls["usermsg"])
        self.assertEqual(calls["phases"], ["downloading", "extracting_frames", "transcribing", "analyzing"])

    def test_heartbeat_uses_prefixed_key_to_avoid_collision(self):
        import inspect
        src = inspect.getsource(self.breakdown._heartbeat)
        self.assertIn('"_hb_phase"', src)
        self.assertNotIn('"phase"', src)

    def test_iter_json_objects_skips_oversized_input(self):
        big = "x" * 50001
        result = list(self.breakdown._iter_json_objects(big))
        self.assertEqual(result, [])

    def test_iter_json_objects_handles_normal_input(self):
        result = list(self.breakdown._iter_json_objects('{"a":1} extra {"b":2}'))
        self.assertEqual(len(result), 2)
        self.assertIn('{"a":1}', result)
        self.assertIn('{"b":2}', result)

    def test_extract_frames_clamps_count_to_range(self):
        import inspect
        src = inspect.getsource(self.breakdown._extract_frames)
        self.assertIn("max(2, min(count, 12))", src)

    def test_gen_breakdown_single_url_still_works(self):
        calls = self._install_fake_env(
            '{"scenes":[{"dur":"3s","scene":"门头","line":"欢迎"}],"analysis":"ok"}'
        )
        sys.modules["tikhub"].parse_link = lambda url: {"platform": "douyin", "id": "abc123"}

        result = self.breakdown.gen_breakdown({"url": "https://example.test/v/1", "_job_id": 20})

        self.assertEqual(result["type"], "breakdown")
        self.assertEqual(result["source_platform"], "douyin")

    def test_gen_breakdown_batch_urls_returns_combined_results(self):
        calls = {}

        class FakeTikHub:
            @staticmethod
            def parse_link(url):
                return {"platform": "douyin", "id": "abc" + url[-1]}
            @staticmethod
            def detail(platform, item_id, note_type=None):
                return {
                    "play_url": "https://example.test/demo.mp4",
                    "duration": 18,
                    "title": "测试视频",
                }
            @staticmethod
            def download_to_file(play_url, deadline, filename):
                pass
            @staticmethod
            def transcript(det, video_path=None):
                return [{"start": 0, "end": 3, "text": "测试文案"}]

        self.breakdown._heartbeat = lambda job_id, phase: None
        self.breakdown._extract_frames = lambda video_path, count, duration: ("d", ["f1.jpg", "f2.jpg"])
        self.breakdown._chat_multimodal = lambda sysmsg, usermsg, frames, temp=0.7: '{"scenes":[{"dur":"3s","scene":"画面","line":"口播"}],"analysis":"分析"}'
        self.breakdown.tempfile.NamedTemporaryFile = lambda suffix="", delete=False: type("Tmp", (), {"name": "f.mp4"})()
        sys.modules["tikhub"] = FakeTikHub

        result = self.breakdown.gen_breakdown({"urls": ["https://example.test/v/1", "https://example.test/v/2"], "_job_id": 21})

        self.assertEqual(result["type"], "breakdown_batch")
        self.assertEqual(result["total"], 2)
        self.assertEqual(len(result["results"]), 2)
        self.assertEqual(len(result["errors"]), 0)

    def test_gen_breakdown_batch_rejects_more_than_5(self):
        with self.assertRaisesRegex(ValueError, "最多 5 条"):
            self.breakdown.gen_breakdown({"urls": ["http://a.test/1"] * 6, "_job_id": 22})

    def test_do_breakdown_includes_frame_thumbnails(self):
        import tempfile
        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        tmp.write(bytes.fromhex(
            "ffd8ffe000104a46494600010101006000600000ffdb004300080606070605080707070909080a0c"
            "140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20242e2720222c231c1c2837292c3031343434"
            "1f27393d38323c2e333432ffdb0043010909090c0b0c180d0d1832211c21323232323232323232"
            "323232323232323232323232323232323232323232323232323232323232323232323232323232"
            "ffc00011080001000103012200021101031101ffc4001400010000000000000000000000000000"
            "0008ffc40014100100000000000000000000000000000000ffda0008010100013f10c9b0a3c4ff"
            "d9"
        ))
        tmp.close()

        calls = self._install_fake_env(
            '{"scenes":[{"dur":"3s","scene":"门头","line":"欢迎"}],"analysis":"ok"}'
        )
        self.breakdown._extract_frames = lambda video_path, count, duration: (
            "fake-frame-dir",
            [tmp.name],
        )

        result = self.breakdown._do_breakdown(
            {"_job_id": 30},
            {"platform": "douyin", "id": "thumb-test"},
            "https://example.test/post/thumb",
        )

        self.assertIn("frame_thumbnails", result)
        self.assertEqual(len(result["frame_thumbnails"]), 1)
        self.assertTrue(result["frame_thumbnails"][0].startswith("data:image/jpeg;base64,"))
        import os; os.unlink(tmp.name)

    def test_do_breakdown_retries_once_on_parse_failure(self):
        self._install_fake_env('{"scenes":[]}')
        responses = [
            "这不是 JSON，完全无法解析",
            '{"scenes":[{"dur":"3s","scene":"门头","line":"欢迎光临"}],"analysis":"ok"}',
        ]
        calls = {"n": 0}

        def flaky_chat(sysmsg, usermsg, frames, temp=0.7):
            r = responses[calls["n"]]
            calls["n"] += 1
            return r

        self.breakdown._chat_multimodal = flaky_chat

        result = self.breakdown._do_breakdown(
            {"_job_id": 40},
            {"platform": "douyin", "id": "retry-ok"},
            "https://example.test/post/retry",
        )

        self.assertEqual(calls["n"], 2)
        self.assertEqual(result["scenes"][0]["scene"], "门头")

    def test_do_breakdown_raises_after_two_parse_failures(self):
        self._install_fake_env('{"scenes":[]}')
        self.breakdown._chat_multimodal = lambda sysmsg, usermsg, frames, temp=0.7: "not json at all"

        with self.assertRaisesRegex(ValueError, "拆解结果解析失败，请重试"):
            self.breakdown._do_breakdown(
                {"_job_id": 41},
                {"platform": "douyin", "id": "retry-fail"},
                "https://example.test/post/retry-fail",
            )

    def test_do_breakdown_normalizes_millisecond_duration(self):
        """tikhub 返回毫秒时长（18320），结果必须统一成秒（18）"""
        self._install_fake_env(
            '{"scenes":[{"dur":"3s","scene":"门头","line":"欢迎"}],"analysis":"ok"}'
        )
        sys.modules["tikhub"].detail = lambda platform, item_id, note_type=None: {
            "play_url": "https://example.test/demo.mp4",
            "duration": 18320,
            "title": "毫秒时长视频",
        }

        result = self.breakdown._do_breakdown(
            {"_job_id": 50},
            {"platform": "douyin", "id": "ms-duration"},
            "https://example.test/post/ms",
        )

        self.assertEqual(result["duration"], 18)

if __name__ == "__main__":
    unittest.main()
