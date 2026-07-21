import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


SERVER_DIR = str(Path(__file__).resolve().parents[1] / "server")
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from content_domains import short_drama, text


def valid_raw_plan():
    return {
        "title": "雨夜来客",
        "logline": "侦探发现来客来自未来",
        "characters": [{
            "key": "detective", "name": "林默", "identity": "侦探",
            "personality": "冷静", "appearance_prompt": "黑发青年",
            "wardrobe_prompt": "深色风衣",
        }],
        "script": {
            "hook": "敲门声响起", "conflict": "女孩预言命案", "turn": "凶手是未来的自己",
            "ending": "门再次响起",
            "dialogue_lines": [{"id": "line-1", "character_key": "detective", "text": "谁在那里？"}],
        },
        "shots": [{
            "key": "s%d" % i, "duration": 5, "scene_description": "雨夜室内",
            "camera_description": "中景缓推", "character_keys": ["detective"],
            "dialogue_line_ids": ["line-1"] if i == 1 else [], "image_prompt": "电影感雨夜",
            "video_prompt": "人物警觉转身",
        } for i in range(1, 7)],
    }


class ShortDramaPlanningTests(unittest.TestCase):
    def test_normalize_plan_requires_six_to_ten_timed_shots(self):
        settings = {"target_duration": 30, "ratio": "9:16", "shot_count": 6}
        raw = valid_raw_plan()
        raw["script"]["dialogue_lines"] = []
        raw["shots"][0]["dialogue_line_ids"] = []

        plan = short_drama.normalize_plan(raw, settings)

        self.assertEqual(len(plan["shots"]), 6)
        self.assertEqual(sum(x["duration"] for x in plan["shots"]), 30)
        self.assertEqual(plan["characters"][0]["source_type"], "ai_character")
        self.assertEqual(plan["characters"][0]["identity_text"], "侦探")
        self.assertEqual(plan["script"]["conflict_text"], "女孩预言命案")

    def test_normalize_plan_rejects_unknown_character_references(self):
        raw = valid_raw_plan()
        raw["shots"][0]["character_keys"] = ["missing"]

        with self.assertRaisesRegex(ValueError, "不存在的角色"):
            short_drama.normalize_plan(raw, {"target_duration": 30, "ratio": "9:16", "shot_count": 6})

    def test_normalize_plan_rejects_unknown_dialogue_references(self):
        raw = valid_raw_plan()
        raw["shots"][0]["dialogue_line_ids"] = ["missing"]

        with self.assertRaisesRegex(ValueError, "不存在的台词"):
            short_drama.normalize_plan(raw, {"target_duration": 30, "ratio": "9:16", "shot_count": 6})

    def test_parse_and_normalize_plan_requires_json_object(self):
        with self.assertRaisesRegex(ValueError, "JSON"):
            short_drama.parse_and_normalize_plan("```json\n{}\n```", {
                "target_duration": 30, "ratio": "9:16", "shot_count": 6,
            })

    def test_build_plan_prompt_requires_json_keys(self):
        prompt = short_drama.build_plan_prompt({
            "prompt": "悬疑反转", "target_duration": 30, "ratio": "9:16", "shot_count": 6,
            "style": "电影写实", "platform": "抖音",
        })

        self.assertIn("JSON", prompt)
        for key in ("title", "logline", "characters", "script", "shots", "dialogue_line_ids"):
            self.assertIn(key, prompt)

    def test_validate_planning_payload_normalizes_request_values(self):
        settings = short_drama.validate_planning_payload({
            "prompt": " 雨夜来客 ", "dur": "30s", "ratio": "9:16", "shot_count": "6",
            "style": " 电影写实 ", "platform": " 抖音 ",
        })

        self.assertEqual(settings, {
            "prompt": "雨夜来客", "target_duration": 30, "ratio": "9:16", "shot_count": 6,
            "style": "电影写实", "platform": "抖音",
        })

    def test_gen_copy_short_drama_returns_normalized_plan(self):
        raw = valid_raw_plan()
        with patch.object(text, "_chat", return_value=json.dumps(raw, ensure_ascii=False)):
            result = text.gen_copy({
                "format": "short_drama", "prompt": "雨夜来客", "dur": "30s", "ratio": "9:16",
                "shot_count": 6, "style": "电影写实", "platform": "抖音",
            })

        self.assertEqual(result["type"], "copy")
        self.assertEqual(result["mode"], "short_drama")
        self.assertEqual(result["dur"], "30s")
        self.assertEqual(result["plan"]["characters"][0]["source_type"], "ai_character")


if __name__ == "__main__":
    unittest.main()
