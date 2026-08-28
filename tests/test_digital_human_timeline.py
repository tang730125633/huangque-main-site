# -*- coding: utf-8 -*-
import math
import unittest
from unittest import mock

from server.content_domains import digital_human_timeline as timeline


class DigitalHumanTimelineTests(unittest.TestCase):
    def test_authorized_portrait_is_not_expanded_into_gesture_jobs(self):
        script = "这是一个面向普通人的人工智能讲解。" * 42
        result = timeline.plan_text(script)
        self.assertGreater(result["segment_count"], 2)
        self.assertNotIn("gesture_count", result)
        self.assertNotIn("gestures", result)
        self.assertTrue(all("gesture_index" not in item for item in result["segments"]))

    def test_forty_second_plan_has_open_middle_and_end_presenter(self):
        # Keep the expected duration near 40 seconds without relying on a
        # client supplied duration.
        script = "人工智能正在改变普通人的工作方式，我们先看清问题，再选择真正有用的工具。" * 5
        result = timeline.plan_text(script)
        self.assertGreaterEqual(result["expected_duration"], 35)
        self.assertLessEqual(result["expected_duration"], 50)
        windows = result["presenter_windows"]
        self.assertGreaterEqual(len(windows), 3)
        self.assertEqual(windows[0][0], 0.0)
        self.assertLessEqual(windows[0][1], 3.0)
        self.assertAlmostEqual(windows[-1][1], result["expected_duration"], places=3)
        self.assertAlmostEqual(windows[-1][1] - windows[-1][0], 3.0, delta=0.05)
        self.assertTrue(any(20 <= start <= 30 for start, _end in windows[1:-1]))

    def test_presenter_intervals_are_twenty_to_thirty_seconds(self):
        script = "今天我们用简单的方法讲清楚一个人工智能概念，并给出可以立刻执行的步骤。" * 14
        result = timeline.plan_text(script)
        starts = [window[0] for window in result["presenter_windows"][:-1]]
        for previous, current in zip(starts, starts[1:]):
            self.assertGreaterEqual(current - previous, timeline.MIN_APPEARANCE_INTERVAL)
            self.assertLessEqual(current - previous, timeline.MAX_APPEARANCE_INTERVAL)

    def test_thirty_one_to_thirty_nine_second_plan_does_not_insert_early_presenter(self):
        durations = timeline._normalize_durations(["前半段", "后半段"], 35.0)
        windows = timeline.presenter_windows(durations, 35.0)
        self.assertGreaterEqual(windows[1][0], timeline.MIN_APPEARANCE_INTERVAL)
        self.assertLessEqual(windows[1][0], timeline.MAX_APPEARANCE_INTERVAL)
        self.assertEqual(windows[-1], [32.0, 35.0])

    def test_material_count_is_duration_driven_and_infographics_are_limited(self):
        short = timeline.plan_text("这个功能会先理解内容，再自动寻找相关画面，最后合成一条完整视频。")
        long = timeline.plan_text("这个功能会先理解内容，再自动寻找相关画面，最后合成一条完整视频。" * 12)
        self.assertGreater(long["material_count"], short["material_count"])
        infographics = [item for item in long["materials"] if item["scene_type"] == "infographic"]
        self.assertLessEqual(len(infographics), 2)
        self.assertEqual(
            long["source_priority"],
            ["customer_reference", "feishu", "public_web", "ai"],
        )

    def test_material_slots_cover_only_non_presenter_intervals(self):
        result = timeline.plan_text("普通人学习人工智能不用先背很多术语，先从一个真实问题开始就可以。" * 8)
        for slot in result["materials"]:
            self.assertGreater(slot["duration"], 0)
            for window_start, window_end in result["presenter_windows"]:
                overlap = min(slot["end"], window_end) - max(slot["start"], window_start)
                self.assertLessEqual(overlap, 0.001)

    def test_zero_material_count_is_valid_only_for_full_presenter_timeline(self):
        for duration in (6.0, 6.05):
            with self.subTest(duration=duration):
                windows = timeline.presenter_windows([duration], duration)
                self.assertEqual(timeline.material_slots(windows, duration), [])
                self.assertEqual(timeline.material_slots(windows, duration, 0), [])
                for invalid_count in (False, 1):
                    with self.assertRaises(timeline.TimelinePlanError):
                        timeline.material_slots(windows, duration, invalid_count)

        duration = 6.06
        windows = timeline.presenter_windows([duration], duration)
        self.assertEqual(len(timeline.material_slots(windows, duration)), 1)
        with self.assertRaises(timeline.TimelinePlanError):
            timeline.material_slots(windows, duration, 0)

    def test_short_text_boundaries_keep_material_contract(self):
        script = "这是一段合法的最短测试文案。"
        for duration, expected_count in ((6.0, 0), (6.05, 0), (6.06, 1)):
            with self.subTest(duration=duration), \
                    mock.patch.object(timeline, "estimate_duration", return_value=duration):
                result = timeline.plan_text(script)
                self.assertEqual(result["expected_duration"], duration)
                self.assertEqual(result["material_count"], expected_count)

    def test_plan_digest_covers_schedule_and_source_priority(self):
        one = timeline.plan_text("先把问题讲清楚，再决定使用什么人工智能工具，往往会更高效。" * 4)
        two = timeline.plan_text("先把问题讲清楚，再决定使用什么人工智能工具，往往会更高效。" * 5)
        self.assertNotEqual(one["plan_digest"], two["plan_digest"])
        self.assertEqual(len(one["plan_digest"]), 64)

    def test_removed_gesture_field_and_excessive_duration_fail_closed(self):
        with self.assertRaises(timeline.TimelinePlanError):
            timeline.plan_response({
                "script": "这是一段长度足够的测试文案。",
                "gesture_count": 1,
            })
        with self.assertRaises(timeline.TimelinePlanError) as caught:
            timeline.plan_text("这是超长内容。" * 1000)
        self.assertIn(caught.exception.code, {
            "digital_human_duration_exceeded", "invalid_digital_human_plan",
        })


if __name__ == "__main__":
    unittest.main()
