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

    def test_normalize_plan_preserves_optional_character_voice_config(self):
        raw = valid_raw_plan()
        raw["characters"][0].update({"voice_key": "narrator", "voice_settings": {"speed": 1.1}})

        plan = short_drama.normalize_plan(raw, {"target_duration": 30, "ratio": "9:16", "shot_count": 6})

        self.assertEqual(plan["characters"][0]["voice_key"], "narrator")
        self.assertEqual(plan["characters"][0]["voice_settings"], {"speed": 1.1})
        self.assertIn("voice_key", short_drama.build_plan_prompt({
            "prompt": "雨夜来客", "target_duration": 30, "ratio": "9:16", "shot_count": 6,
            "style": "电影写实", "platform": "抖音",
        }))

    def test_normalize_plan_defaults_and_validates_character_voice_config(self):
        plan = short_drama.normalize_plan(valid_raw_plan(), {
            "target_duration": 30, "ratio": "9:16", "shot_count": 6,
        })
        self.assertIsNone(plan["characters"][0]["voice_key"])
        self.assertEqual(plan["characters"][0]["voice_settings"], {})

        for invalid in ([], "voice-settings"):
            raw = valid_raw_plan()
            raw["characters"][0]["voice_settings"] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                short_drama.normalize_plan(raw, {"target_duration": 30, "ratio": "9:16", "shot_count": 6})

    def test_validate_planning_payload_rejects_impossible_duration_shot_count_pairs(self):
        for duration, shot_count in ((30, 7), (30, 8), (30, 9), (30, 10), (45, 10)):
            with self.subTest(duration=duration, shot_count=shot_count), self.assertRaises(ValueError):
                short_drama.validate_planning_payload({
                    "prompt": "雨夜来客", "dur": "%ss" % duration, "ratio": "9:16", "shot_count": shot_count,
                })

    def test_validate_planning_payload_accepts_feasible_duration_shot_count_pairs(self):
        for duration, shot_count in ((30, 6), (45, 6), (45, 9), (60, 6), (60, 10)):
            with self.subTest(duration=duration, shot_count=shot_count):
                settings = short_drama.validate_planning_payload({
                    "prompt": "雨夜来客", "dur": "%ss" % duration, "ratio": "9:16", "shot_count": shot_count,
                })
                self.assertEqual((settings["target_duration"], settings["shot_count"]), (duration, shot_count))

    def test_normalize_plan_rejects_non_string_textual_fields_and_keys(self):
        mutations = (
            lambda raw: raw.update({"title": 1}),
            lambda raw: raw["characters"][0].update({"key": 1}),
            lambda raw: raw["script"].update({"hook": []}),
            lambda raw: raw["shots"][0].update({"scene_description": {}}),
            lambda raw: raw["shots"][0].update({"character_keys": [1]}),
        )
        for mutate in mutations:
            raw = valid_raw_plan()
            mutate(raw)
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                short_drama.normalize_plan(raw, {"target_duration": 30, "ratio": "9:16", "shot_count": 6})

    def test_normalize_plan_rejects_non_integer_durations_and_invalid_settings(self):
        for duration in (True, 5.9, "5"):
            raw = valid_raw_plan()
            raw["shots"][0]["duration"] = duration
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                short_drama.normalize_plan(raw, {"target_duration": 30, "ratio": "9:16", "shot_count": 6})
        with self.assertRaises(ValueError):
            short_drama.normalize_plan(valid_raw_plan(), {
                "target_duration": 30, "ratio": "1:1", "shot_count": 6,
            })

    def test_normalize_plan_rejects_duplicate_shot_references(self):
        for field, values in (("character_keys", ["detective", "detective"]),
                              ("dialogue_line_ids", ["line-1", "line-1"])):
            raw = valid_raw_plan()
            raw["shots"][0][field] = values
            with self.subTest(field=field), self.assertRaises(ValueError):
                short_drama.normalize_plan(raw, {"target_duration": 30, "ratio": "9:16", "shot_count": 6})

    def test_validate_planning_payload_requires_integer_duration_and_shot_count(self):
        for field, values in (("dur", (True, 30.9, "30", [], {})),
                              ("shot_count", (True, 6.0, 6.9, "6", [], {}))):
            for value in values:
                payload = {"prompt": "雨夜来客", "dur": 30, "ratio": "9:16", "shot_count": 6}
                payload[field] = value
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    short_drama.validate_planning_payload(payload)

    def test_normalize_plan_requires_integer_direct_settings(self):
        for field, values in (("target_duration", (True, 30.0, 30.9, "30", [], {})),
                              ("shot_count", (True, 6.0, 6.9, "6", [], {}))):
            for value in values:
                settings = {"target_duration": 30, "ratio": "9:16", "shot_count": 6}
                settings[field] = value
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    short_drama.normalize_plan(valid_raw_plan(), settings)

    def test_normalize_plan_rejects_non_string_source_type_cleanly(self):
        for source_type in ([], {}):
            raw = valid_raw_plan()
            raw["characters"][0]["source_type"] = source_type
            with self.subTest(source_type=source_type), self.assertRaises(ValueError):
                short_drama.normalize_plan(raw, {"target_duration": 30, "ratio": "9:16", "shot_count": 6})

    def test_validate_planning_payload_accepts_16_by_9_and_rejects_unsupported_values(self):
        settings = short_drama.validate_planning_payload({
            "prompt": "雨夜来客", "dur": 30, "ratio": "16:9", "shot_count": 6,
        })
        self.assertEqual(settings["ratio"], "16:9")
        for patch in ({"dur": 20}, {"ratio": "1:1"}):
            payload = {"prompt": "雨夜来客", "dur": 30, "ratio": "9:16", "shot_count": 6}
            payload.update(patch)
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                short_drama.validate_planning_payload(payload)

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
            "prompt": " 雨夜来客 ", "dur": "30s", "ratio": "9:16", "shot_count": 6,
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
