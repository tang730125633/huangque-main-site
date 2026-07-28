# -*- coding: utf-8 -*-
import base64
import http.client
import io
import json
import pathlib
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server"))
from content_domains import breakdown
try:
    from PIL import Image
except ImportError:
    Image = None


class BreakdownFollowupTests(unittest.TestCase):
    def test_tikhub_millisecond_duration_is_normalized_to_seconds(self):
        self.assertAlmostEqual(
            breakdown._normalize_duration_seconds(10034), 10.034, places=3
        )
        self.assertEqual(breakdown._normalize_duration_seconds(23), 23)
        self.assertEqual(
            breakdown._normalize_duration_seconds(None, fallback=30), 30
        )

    def test_link_reverse_uses_downloaded_media_duration_for_frames(self):
        fake_tikhub = mock.Mock()
        fake_tikhub.detail.return_value = {
            "play_url": "https://cdn.example/video.mp4",
            "duration": 10034,
            "title": "duration unit regression",
        }
        fake_tikhub.download_to_file.return_value = None
        fake_tikhub.transcript.return_value = []
        with mock.patch.dict(sys.modules, {"tikhub": fake_tikhub}), \
             mock.patch.object(breakdown, "_probe_duration", return_value=10.034), \
             mock.patch.object(
                 breakdown, "_extract_frames", return_value=(None, ["frame.jpg"])
             ) as extracted, \
             mock.patch.object(
                 breakdown, "_reverse_from_frames",
                 return_value={"type": "breakdown_reverse"},
             ) as reversed_from_frames:
            result = breakdown._do_breakdown(
                {"mode": "reverse_prompt", "_job_id": 7},
                {"platform": "douyin", "id": "123", "note_type": "video"},
                "https://www.douyin.com/video/123",
            )

        self.assertEqual(result["type"], "breakdown_reverse")
        self.assertEqual(extracted.call_args.args[1:], (4, 10.034))
        self.assertEqual(reversed_from_frames.call_args.args[-1], 10.034)
        self.assertEqual(
            reversed_from_frames.call_args.kwargs["script_text"],
            "",
        )

    def test_reverse_prompt_uses_program_generated_timeline_sections(self):
        captured = {}
        responses = [
            "人物从画面左侧进入室内，身体朝向房间中央，前景桌面与后景窗户"
            "形成清晰层次；平视中景机位向右跟随，暖色侧光照亮人物轮廓，"
            "人物在桌边停止后镜头同步减速。",
            "同一人物来到桌边伸手拿起杯子，视线跟随手部移动，桌面和杯子"
            "位于前景，窗户保持在人物后方；平视中景机位轻微前推，柔和侧光"
            "在杯壁形成随手腕变化的反光。",
            "人物放下杯子后自然抬头，肩部放松并位于画面中央，桌面仍在"
            "前景且后方窗户轻微虚化；画面由中景收束到平视近景，侧前方"
            "主光保持柔和，人物面部亮度略高于背景。",
        ]

        def fake_chat(
            system_message, user_message, frames, temp=0.7, max_tokens=None,
        ):
            captured.update(
                system=system_message,
                user=user_message,
                frames=frames,
                max_tokens=max_tokens,
            )
            return responses.pop(0)

        with mock.patch.object(
            breakdown, "_chat_multimodal", side_effect=fake_chat
        ):
            result = breakdown._reverse_from_frames(
                {},
                ["frame-1.jpg", "frame-2.jpg"],
                title="测试视频",
                platform="douyin",
                duration=7,
                script_text="[0s-3s] 开场口播",
            )

        self.assertIn("[00:00-00:02.333]", result["prompt"])
        self.assertTrue(result["prompt"].splitlines()[-1].startswith(
            "[00:04.667-00:07]"
        ))
        self.assertEqual(captured["max_tokens"], 700)
        for expected in (
            "唯一关键帧",
            "写60-120字",
            "景别视角构图",
            "不要JSON",
        ):
            self.assertIn(expected, captured["user"])
        self.assertIn("直接可见的事实", captured["system"])

    def test_reverse_timeline_accepts_fractional_and_subsecond_duration(self):
        segments = breakdown._validate_reverse_timeline(
            (
                "[00:00-00:00.25] 快速开场。\n"
                "[00:00.25-00:00.75] 主体完成动作。"
            ),
            0.75,
        )
        self.assertEqual(segments, [(0.0, 0.25), (0.25, 0.75)])

        segments = breakdown._validate_reverse_timeline(
            "[00:00-00:01.25] 开场。\n[00:01.25-00:02.5] 收束。",
            2.5,
        )
        self.assertEqual(segments[-1][1], 2.5)

        self.assertEqual(
            breakdown._validate_reverse_timeline(
                "[00:00-00:02] 按画面帧率取整后的结尾。", 2.08,
            ),
            [(0.0, 2.0)],
        )
        self.assertEqual(
            breakdown._validate_reverse_timeline(
                "[00:00-00:02.08] 按画面帧率取整后的结尾。", 2.0,
            ),
            [(0.0, 2.08)],
        )

        self.assertEqual(
            breakdown._validate_reverse_timeline(
                (
                    "视频复刻提示词：\n"
                    "1. [00:00-00:01] 主体从画面左侧进入。\n"
                    "补充说明主体保持平视。\n"
                    "- [00:01-00:02] 镜头跟随主体并自然收束。"
                ),
                2,
            ),
            [(0.0, 1.0), (1.0, 2.0)],
        )

    def test_reverse_timeline_rejects_invalid_boundaries(self):
        invalid = (
            (
                "[00:00.1-00:01] 内容。",
                1,
                "从00:00开始",
            ),
            (
                "[00:00-00:01] 内容。\n[00:01.1-00:02] 内容。",
                2,
                "空档",
            ),
            (
                "[00:00-00:01.1] 内容。\n[00:01-00:02] 内容。",
                2,
                "重叠",
            ),
            (
                (
                    "[00:00-00:00.5] 内容。\n"
                    "[00:00.5-00:01] 内容。\n"
                    "[00:00.4-00:02] 内容。"
                ),
                2,
                "乱序",
            ),
            (
                "[00:00-00:01.3] 内容。",
                1,
                "超出",
            ),
            (
                "[00:00-00:00.7] 内容。",
                1,
                "未对齐",
            ),
            (
                "标题\n[错误时间] 内容。",
                1,
                "未找到合法",
            ),
            (
                "[00:00-00:01] 无",
                1,
                "缺少画面描述",
            ),
        )
        for prompt, duration, message in invalid:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    breakdown._validate_reverse_timeline(prompt, duration)

    def test_reverse_segment_content_validation(self):
        valid = [
            "主体从画面左侧进入室内空间，镜头保持平视中景并缓慢向右跟随，暖色侧光勾勒人物轮廓，前景桌面与后景窗户形成明确层次。人物步伐平稳，身体朝向与视线都指向画面中央，环境音保持安静，镜头移动在人物停步时同步减速。",
            "人物走到桌边拿起杯子并转向镜头，动作从伸手开始到握稳结束，视线跟随杯子移动，机位轻微前推，环境光保持柔和。杯壁反光随手腕转动发生变化，中景构图逐渐突出上半身，前后景位置与上一段连续，不增加新的道具或人物。",
            "人物放下杯子后自然抬头微笑，镜头从中景收束到近景，背景虚化程度逐渐增加，动作、光线与空间关系均延续上一镜头。主光仍从侧前方照射，人物肩部放松并保持面向镜头，运镜平稳停止，画面在安静环境音中自然结束。",
        ]
        self.assertEqual(
            breakdown._validate_reverse_segment_contents(valid, 3),
            valid,
        )
        cases = (
            (["内容", valid[1], valid[2]], "占位"),
            ([valid[0], valid[1]], "分段数量"),
            (["这段太短", valid[1], valid[2]], "内容过短"),
            ([valid[0], valid[0], valid[2]], "完全重复"),
        )
        for contents, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    breakdown._validate_reverse_segment_contents(contents, 3)

    def test_reverse_segment_objects_are_flattened_with_required_fields(self):
        item = {
            "subject": "人物位于画面中央并面向镜头，灰色上衣轮廓清晰",
            "action": "人物从低头阅读开始，缓慢抬头并在动作结束时保持平视",
            "scene": "室内桌面位于前景，白色墙面和条形天花板构成后景层次",
            "camera": "平视中景固定机位，构图以人物上半身为中心并保持稳定",
            "light": "柔和自然光从画面左侧进入，整体色温中性且材质反光克制",
            "audio": "没有可确认环境声，画面文字保持原样，不新增口播或字幕",
        }
        raw = json.dumps({"segments": [item, item, dict(
            item, action="人物保持平视后放松肩部，动作自然停止并在画面中央收束",
        )]}, ensure_ascii=False)
        with self.assertRaisesRegex(ValueError, "完全重复"):
            breakdown._coerce_reverse_segments(raw, 3)
        parsed = breakdown._coerce_reverse_segments(
            json.dumps({"segments": [
                item,
                dict(item, action="人物抬头后转向左侧，动作连续且在转身结束时停下"),
                dict(item, action="人物回到正面并放松肩部，镜头保持稳定直至自然结束"),
            ]}, ensure_ascii=False),
            3,
        )
        self.assertEqual(len(parsed), 3)
        self.assertIn("主体与位置：", parsed[0])
        self.assertIn("声音字幕：", parsed[0])

        with self.assertRaisesRegex(ValueError, "缺少字段"):
            breakdown._coerce_reverse_segments(
                json.dumps({"segments": [dict(item, audio="")]}, ensure_ascii=False),
                1,
            )

    def test_reverse_segment_objects_reject_visual_placeholder_fields(self):
        real = {
            "subject": "女子位于画面中央",
            "action": "女子背对镜头站立",
            "scene": "夜晚街道铺有落叶",
            "camera": "平视固定中景",
            "light": "柔和环境光从左侧照入",
            "audio": "无",
        }
        cases = (
            {field: "无" for field in real},
            *(dict(real, **{field: "未确认"}) for field in (
                "subject", "action", "scene", "camera", "light",
            )),
        )
        for item in cases:
            with self.subTest(item=item):
                raw = json.dumps({"segments": [item]}, ensure_ascii=False)
                with self.assertRaisesRegex(ValueError, "视觉字段包含占位内容"):
                    breakdown._coerce_reverse_segments(
                        raw, 1, allow_duplicates=True, allow_short=True,
                    )

    def test_reverse_evidence_retries_after_invalid_first_result(self):
        invalid = json.dumps({"segments": ["未确认"] * 3}, ensure_ascii=False)
        valid = [
            "画面中央是一名坐在草地上的女子，人物背对镜头，平视中景中"
            "背景树木和远山形成层次，自然光均匀照亮人物与草地，整体色调偏暖。"
            "人物双手位于膝前，轮廓与较暗的远景之间保持清楚分离。",
            "同一女子缓慢放下双手，身体仍位于中央，草地处于前景且树木"
            "位于后方；固定中景构图保持稳定，环境光柔和且明暗对比清楚。"
            "人物肩部和手臂均处于画面中部区域。",
            "女子再次抬起双手并保持背对镜头，前景草地、中景树木与远山"
            "构成纵深；平视中景中主体轮廓清楚，自然光亮度和暖色色调保持连续。"
            "人物位置和背景层次与前一张图片相互衔接。",
        ]
        with mock.patch.object(
            breakdown, "_chat_multimodal", side_effect=[invalid] + valid,
        ) as chat:
            result = breakdown._reverse_from_frames(
                {}, ["frame.jpg"], duration=6,
            )

        self.assertEqual(chat.call_count, 4)
        self.assertEqual(len(result["prompt"].splitlines()), 3)

    def test_reverse_visual_fields_allow_uncertainty_after_visible_facts(self):
        item = {
            "subject": "画面中央可见一名背对镜头的女子，具体身份无法确认",
            "action": "女子保持坐姿并抬起双手，未观察到明显位移，动作在抬手后停止",
            "scene": "前景可见草地，中后景为树木和山体，具体地点无法确认",
            "camera": "平视中景固定机位，主体位于中央，无明显运镜",
            "light": "自然光均匀照亮主体与环境，具体光源位置无法确认",
            "audio": "未识别到明确声音或字幕",
        }
        parsed = breakdown._coerce_reverse_segments(
            json.dumps({"segments": [item]}, ensure_ascii=False),
            1,
            allow_duplicates=True,
            allow_short=True,
        )
        self.assertEqual(len(parsed), 1)
        self.assertIn("具体身份无法确认", parsed[0])
        self.assertIn("具体地点无法确认", parsed[0])

    def test_reverse_audio_rejects_long_screen_text_copy(self):
        item = {
            "subject": "画面中央可见一名背对镜头的女子，人物轮廓清晰",
            "action": "女子保持坐姿并抬起双手，动作在抬手后稳定停止",
            "scene": "前景可见草地，中后景为树木和远山，空间层次清楚",
            "camera": "平视中景固定机位，主体位于中央，无明显运镜",
            "light": "自然光均匀照亮主体与环境，整体色调偏暖",
            "audio": "这是一段被模型从屏幕上逐字复制出来的很长文字内容"
                     "而且还会继续重复并显著挤占其他视觉字段的输出空间",
        }
        with self.assertRaisesRegex(ValueError, "声音字幕字段过长"):
            breakdown._coerce_reverse_segments(
                json.dumps({"segments": [item]}, ensure_ascii=False),
                1,
                allow_duplicates=True,
                allow_short=True,
            )

    def test_reverse_segment_objects_allow_real_short_visuals_and_empty_audio(self):
        item = {
            "subject": "一名女子位于画面中央并背对镜头",
            "action": "女子保持站立姿态，双手自然垂在身体两侧",
            "scene": "夜晚街道铺有落叶，路面延伸至画面后方",
            "camera": "平视中景固定机位，主体位于中央",
            "light": "柔和环境光从画面左侧照入",
            "audio": "无",
        }
        raw = json.dumps({"segments": [item]}, ensure_ascii=False)
        parsed = breakdown._coerce_reverse_segments(
            raw, 1, allow_duplicates=True, allow_short=True,
        )
        self.assertEqual(len(parsed), 1)
        self.assertIn("主体与位置：一名女子位于画面中央", parsed[0])
        self.assertIn("声音字幕：无", parsed[0])

    def test_reverse_timeline_rejects_placeholder_visuals_on_all_attempts(self):
        placeholder_item = {
            "subject": "无",
            "action": "没有",
            "scene": "未确认",
            "camera": "略",
            "light": "待补充",
            "audio": "占位",
        }
        raw = json.dumps(
            {"segments": [placeholder_item, placeholder_item, placeholder_item]},
            ensure_ascii=False,
        )
        with mock.patch.object(
            breakdown, "_chat_multimodal", return_value=raw,
        ) as chat:
            with self.assertRaisesRegex(ValueError, "不能是JSON"):
                breakdown._reverse_from_frames(
                    {}, ["frame.jpg"], duration=6,
                )

        self.assertEqual(chat.call_count, 2)

    def test_reverse_visual_fields_reject_generic_success_content(self):
        generic = {
            "subject": "未观察到明显动作变化",
            "action": "未观察到明显动作变化",
            "scene": "未观察到明显动作变化",
            "camera": "未观察到明显运镜",
            "light": "未观察到明显光线方向",
            "audio": "未观察到明显声音",
        }
        raw = json.dumps({"segments": [generic] * 3}, ensure_ascii=False)
        with self.assertRaisesRegex(ValueError, "缺少可验证画面事实"):
            breakdown._coerce_reverse_segments(
                raw, 3, allow_duplicates=True, allow_short=True,
            )

    def test_reverse_visual_fields_require_camera_and_light_evidence(self):
        item = {
            "subject": "一名女子位于画面中央并背对镜头",
            "action": "女子保持站立姿态，双手自然垂在身体两侧",
            "scene": "夜晚街道铺有落叶，路面延伸至画面后方",
            "camera": "画面保持稳定且没有发生变化",
            "light": "整体画面状态保持稳定",
            "audio": "无",
        }
        raw = json.dumps({"segments": [item]}, ensure_ascii=False)
        with self.assertRaisesRegex(ValueError, "camera缺少"):
            breakdown._coerce_reverse_segments(
                raw, 1, allow_duplicates=True, allow_short=True,
            )
        item["camera"] = "平视中景固定机位，主体位于中央"
        with self.assertRaisesRegex(ValueError, "light缺少"):
            breakdown._coerce_reverse_segments(
                json.dumps({"segments": [item]}, ensure_ascii=False),
                1,
                allow_duplicates=True,
                allow_short=True,
            )

    def test_repeated_frame_evidence_reviews_only_later_duplicates(self):
        establishing = (
            "夜晚古镇的拱桥横跨水面，前景水面倒映暖黄色灯笼，街道和古建筑"
            "向画面深处延伸；画面采用俯视全景构图，深蓝天空与暖色灯光形成对比。"
        )
        repeated = (
            "穿粉色连帽上衣的女子位于画面中央，背景为虚化的灯笼和古建筑；"
            "人物处于平视中景，暖黄色环境光照亮面部和衣袖，整体色调柔和，"
            "画面两侧的栏杆和灯笼共同形成纵深层次，主体轮廓与背景分离清楚。"
        )
        raw = (
            "图片1：" + establishing
            + "图片2：" + repeated
            + "图片3：" + repeated
            + "图片4：" + repeated
        )
        reviews = [
            "女性身体和脸朝向画面左侧，左侧手臂较低，右侧手臂向右上方伸展，粉色衣袖横过前景。",
            "女性以侧面朝向画面左侧，左臂抬起向左延伸，另一侧手臂下垂，粉色布料在左侧形成弧线。",
        ]
        with mock.patch.object(
            breakdown, "_chat_multimodal", side_effect=reviews,
        ) as chat:
            contents = breakdown._coerce_reverse_frame_evidence(
                raw,
                ["frame-1.jpg", "frame-2.jpg", "frame-3.jpg", "frame-4.jpg"],
                4,
            )

        self.assertEqual(chat.call_count, 2)
        self.assertNotIn("动作差异复核", contents[1])
        self.assertIn("右侧手臂向右上方伸展", contents[2])
        self.assertIn("左臂抬起向左延伸", contents[3])
        breakdown._validate_reverse_segment_contents(contents, 4)

    def test_reverse_evidence_removes_speculative_sentences(self):
        content, removed = breakdown._remove_reverse_speculation(
            "女子穿粉色上衣站在画面中央。"
            "她似乎在享受夜晚的微风。"
            "背景灯笼发出暖黄色光线。"
            "桥下形成美丽的倒影，古色古香的建筑位于后景。"
            "衣袖向右上方延伸，增加了画面的动态感。"
            "画面给人一种温馨氛围。"
        )

        self.assertTrue(removed)
        self.assertIn("女子穿粉色上衣站在画面中央", content)
        self.assertIn("背景灯笼发出暖黄色光线", content)
        self.assertIn("桥下形成倒影，传统样式的建筑位于后景", content)
        self.assertIn("衣袖向右上方延伸", content)
        self.assertNotIn("似乎", content)
        self.assertNotIn("美丽", content)
        self.assertNotIn("动态感", content)
        self.assertNotIn("氛围", content)

    def test_person_evidence_rechecks_action_after_speculation_removed(self):
        raw = (
            "图片1：一名穿粉色连帽上衣的女性位于画面中央，棕色长发披在肩后，"
            "夜晚灯笼和建筑在背景中虚化，暖黄色环境光照亮人物轮廓。"
            "她抬起手臂，似乎在感受夜晚微风。"
        )
        review = (
            "女性身体朝向左侧，左臂位于肩部高度并向画面左侧伸展，"
            "右臂自然下垂，粉色衣袖在前景形成弧线。"
        )
        with mock.patch.object(
            breakdown, "_chat_multimodal", return_value=review,
        ) as chat:
            contents = breakdown._coerce_reverse_frame_evidence(
                raw, ["frame.jpg"], 1,
            )

        self.assertEqual(chat.call_count, 1)
        self.assertIn("动作差异复核", contents[0])
        self.assertIn("左臂位于肩部高度", contents[0])
        self.assertNotIn("似乎", contents[0])

    def test_stationary_person_without_visible_action_needs_review(self):
        self.assertTrue(
            breakdown._reverse_person_content_needs_action_review(
                "一名穿粉色上衣的女性站在画面中央，背景灯笼发出暖黄色光线。"
            )
        )
        self.assertFalse(
            breakdown._reverse_person_content_needs_action_review(
                "一名穿粉色上衣的女性位于画面中央，手臂抬起，衣袖形成弧线。"
            )
        )
        self.assertTrue(
            breakdown._reverse_person_content_needs_action_review(
                "画面中主体穿着粉色连帽衫，位于中央偏左，背景灯光模糊。"
            )
        )

    def test_repeated_reverse_segments_reference_matching_previous_segment(self):
        contents = [
            "主体A保持静止",
            "主体B向右移动",
            "主体A保持静止",
        ]
        annotated = breakdown._annotate_repeated_reverse_segments(contents)
        self.assertNotIn("连续性：", annotated[0])
        self.assertNotIn("连续性：", annotated[1])
        self.assertIn("与第1段保持同一主体", annotated[2])
        self.assertNotIn("与第2段保持同一主体", annotated[2])

    def test_reverse_timeline_is_program_generated_and_retries_once(self):
        first = json.dumps(
            {"segments": ["内容过短", "内容过短", "内容过短"]},
            ensure_ascii=False,
        )
        contents = [
            {
                "subject": "人物位于画面左侧并朝向室内中央，外观轮廓清晰",
                "action": "人物从左侧进入，步伐平稳，最后在桌边停止",
                "scene": "桌面位于前景，窗户和墙面构成中后景空间层次",
                "camera": "平视中景机位缓慢向右跟随，主体逐渐移向中央",
                "light": "暖色侧光勾勒人物轮廓，室内明暗对比柔和",
                "audio": "环境音安静，无明确字幕",
            },
            {
                "subject": "同一人物位于桌边，右手靠近桌面上的杯子",
                "action": "人物伸手拿起杯子并转向镜头，动作在握稳后结束",
                "scene": "桌面和杯子位于前景，窗户保持在人物后方",
                "camera": "平视中景机位轻微前推，构图突出人物上半身",
                "light": "柔和侧光照亮人物和杯壁，反光随手腕变化",
                "audio": "环境音保持安静",
            },
            {
                "subject": "同一人物位于画面中央，杯子已经放回桌面",
                "action": "人物放下杯子后自然抬头，肩部放松并保持正面姿态",
                "scene": "桌面仍处于前景，背景窗户轻微虚化且位置连续",
                "camera": "平视近景机位平稳停止，主体面部位于构图中心",
                "light": "侧前方主光保持柔和，人物面部亮度略高于背景",
                "audio": "环境音安静，无新增字幕",
            },
        ]
        responses = [
            first,
            "人物从画面左侧进入室内，身体朝向房间中央，前景桌面与后景窗户"
            "形成清晰层次；平视中景机位向右跟随，暖色侧光照亮人物轮廓，"
            "人物在桌边停止后镜头同步减速，视线始终指向画面中央。",
            "同一人物来到桌边伸手拿起杯子，视线跟随手部移动，桌面和杯子"
            "位于前景，窗户保持在人物后方；平视中景机位轻微前推，柔和侧光"
            "在杯壁形成随手腕变化的反光。",
            "人物放下杯子后自然抬头，肩部放松并位于画面中央，桌面仍在"
            "前景且后方窗户轻微虚化；画面由中景收束到平视近景，侧前方主光"
            "保持柔和，人物面部亮度略高于背景。",
        ]
        messages = []
        token_limits = []

        def fake_chat(
            system_message, user_message, frames, temp=0.7, max_tokens=None,
        ):
            messages.append(user_message)
            token_limits.append(max_tokens)
            return responses.pop(0)

        with mock.patch.object(
            breakdown, "_chat_multimodal", side_effect=fake_chat,
        ) as chat:
            result = breakdown._reverse_from_frames(
                {}, ["frame.jpg"], duration=6,
            )

        self.assertEqual(chat.call_count, 4)
        self.assertEqual(token_limits, [700, 700, 700, 700])
        self.assertEqual(
            [call.kwargs.get("temp") for call in chat.call_args_list],
            [0.1, 0.1, 0.1, 0.1],
        )
        self.assertIn("唯一关键帧", messages[1])
        self.assertEqual(
            [line.split("]", 1)[0] + "]" for line in result["prompt"].splitlines()],
            breakdown._fixed_reverse_ranges(6),
        )
        breakdown._validate_reverse_timeline(result["prompt"], 6)

    def test_reverse_timeline_reviews_repeated_segments(self):
        content = (
            "穿粉色连帽上衣的女子站在画面中央，背景为虚化的灯笼和古建筑；"
            "人物处于平视中景，暖黄色环境光照亮面部和衣袖，画面两侧栏杆与"
            "灯笼形成纵深层次，主体轮廓与背景分离清楚，整体色调柔和。"
        )
        with mock.patch.object(
            breakdown, "_chat_multimodal", side_effect=[content] * 3,
        ) as chat:
            result = breakdown._reverse_from_frames(
                {}, ["frame.jpg"], duration=6,
            )

        self.assertEqual(chat.call_count, 3)
        lines = result["prompt"].splitlines()
        self.assertNotIn("连续性：", lines[0])
        self.assertIn("与第1段保持同一主体", lines[1])
        self.assertIn("与第2段保持同一主体", lines[2])
        breakdown._validate_reverse_timeline(result["prompt"], 6)

    def test_reverse_timeline_never_pads_short_draft_with_boilerplate(self):
        short_item = {
            "subject": "女子位于街道",
            "action": "背对镜头站立",
            "scene": "夜晚街道有落叶",
            "camera": "固定中景",
            "light": "柔和环境光",
            "audio": "无",
        }
        short = json.dumps(
            {"segments": [short_item, short_item, short_item]},
            ensure_ascii=False,
        )
        with mock.patch.object(
            breakdown, "_chat_multimodal", return_value=short,
        ) as chat:
            with self.assertRaisesRegex(
                ValueError, "不能是JSON",
            ):
                breakdown._reverse_from_frames(
                    {}, ["frame.jpg"], duration=6,
                )

        self.assertEqual(chat.call_count, 2)

    def test_reverse_timeline_succeeds_on_second_evidence_attempt(self):
        invalid = json.dumps({"segments": ["始终错误。"]}, ensure_ascii=False)
        valid = [
            "一名女子位于夜晚街道中央并背对镜头，双手自然垂在身体两侧，"
            "前景路面铺有落叶且向后延伸；平视中景固定构图中，柔和侧光从左侧"
            "照亮人物轮廓，背景亮度较低。",
            "同一女子保持站立后轻微向左转头，身体仍在画面中央，街道与"
            "落叶位置连续；平视中景机位稳定，左侧环境光照亮肩部并与暗背景"
            "形成清楚对比。",
            "女子恢复面向前方的站姿，双手仍自然垂落，路面和街道背景"
            "保持原有纵深；固定中景构图没有改变，柔和侧光继续勾勒人物边缘，"
            "整体色调保持偏暖。",
        ]
        responses = [invalid] + valid
        messages = []

        def fake_chat(
            system_message, user_message, frames, temp=0.7, max_tokens=None,
        ):
            messages.append(user_message)
            return responses.pop(0)

        with mock.patch.object(
            breakdown, "_chat_multimodal", side_effect=fake_chat,
        ) as chat:
            result = breakdown._reverse_from_frames(
                {}, ["frame.jpg"], duration=6,
            )

        self.assertEqual(chat.call_count, 4)
        self.assertIn("唯一关键帧", messages[1])
        self.assertEqual(len(result["prompt"].splitlines()), 3)
        breakdown._validate_reverse_timeline(result["prompt"], 6)

    def test_reverse_timeline_validation_fails_after_bounded_retry(self):
        invalid = json.dumps({"segments": ["始终错误。"]}, ensure_ascii=False)
        with mock.patch.object(
            breakdown, "_chat_multimodal", return_value=invalid,
        ) as chat:
            with self.assertRaisesRegex(ValueError, "逐帧证据失败"):
                breakdown._reverse_from_frames(
                    {}, ["frame.jpg"], duration=2,
                )
        self.assertEqual(chat.call_count, 2)

    def test_reverse_prompt_accepts_array_and_recovers_missing_commas(self):
        array_raw = json.dumps({
            "prompt": [
                "[00:00-00:01] 开场。",
                "[00:01-00:02] 收束。",
            ],
        }, ensure_ascii=False)
        self.assertEqual(
            breakdown._coerce_reverse_prompt(array_raw),
            "[00:00-00:01] 开场。\n[00:01-00:02] 收束。",
        )
        malformed = (
            '```json\n{"prompt": ['
            '"[00:00-00:01] 开场。" '
            '"[00:01-00:02] 收束。"]}\n```'
        )
        self.assertEqual(
            breakdown._coerce_reverse_prompt(malformed),
            "[00:00-00:01] 开场。\n[00:01-00:02] 收束。",
        )

    def test_retry_ranges_are_bounded_by_real_duration(self):
        ranges = breakdown._fixed_reverse_ranges(10.034)
        self.assertEqual(len(ranges), 4)
        self.assertEqual(ranges[0].split("-", 1)[0], "[00:00")
        self.assertTrue(ranges[-1].endswith("00:10.034]"))
        short_ranges = breakdown._fixed_reverse_ranges(2)
        self.assertEqual(len(short_ranges), 3)
        self.assertTrue(short_ranges[-1].endswith("00:02]"))

    def test_timeline_labels_carry_rounded_milliseconds_into_minutes(self):
        expected_labels = {
            59.9996: "01:00",
            60: "01:00",
            119.9996: "02:00",
            120: "02:00",
        }
        for duration, expected in expected_labels.items():
            with self.subTest(duration=duration):
                self.assertEqual(
                    breakdown._timeline_label(duration), expected,
                )

        expected_ranges = {
            59.9996: [
                "[00:00-00:15]",
                "[00:15-00:30]",
                "[00:30-00:45]",
                "[00:45-01:00]",
            ],
            60: [
                "[00:00-00:15]",
                "[00:15-00:30]",
                "[00:30-00:45]",
                "[00:45-01:00]",
            ],
            119.9996: [
                "[00:00-00:30]",
                "[00:30-01:00]",
                "[01:00-01:30]",
                "[01:30-02:00]",
            ],
            120: [
                "[00:00-00:30]",
                "[00:30-01:00]",
                "[01:00-01:30]",
                "[01:30-02:00]",
            ],
        }
        for duration, expected in expected_ranges.items():
            with self.subTest(duration=duration):
                ranges = breakdown._fixed_reverse_ranges(duration)
                self.assertEqual(ranges, expected)
                self.assertNotIn(":60", "".join(ranges))
                prompt = "\n".join(
                    "%s 该段包含可执行的主体、动作、场景、镜头与光线描述。"
                    % item
                    for item in ranges
                )
                segments = breakdown._validate_reverse_timeline(
                    prompt, duration,
                )
                self.assertEqual(len(segments), 4)
                self.assertAlmostEqual(
                    segments[-1][1], round(duration, 3), places=3,
                )

    def test_static_image_reverse_does_not_require_a_zero_length_timeline(self):
        captured = {}

        def fake_chat(system_message, user_message, frames, temp=0.7):
            captured["user"] = user_message
            return json.dumps(
                {"prompt": "正面平视构图，人物位于画面中央，柔和侧光。"},
                ensure_ascii=False,
            )

        with mock.patch.object(
            breakdown, "_chat_multimodal", side_effect=fake_chat,
        ) as chat:
            result = breakdown._reverse_from_frames(
                {}, ["image.jpg"], title="portrait.png", duration=0,
            )

        self.assertEqual(chat.call_count, 1)
        self.assertEqual(result["duration"], 0)
        self.assertIn("素材类型：静态图片", captured["user"])
        self.assertIn("不要编造时间轴", captured["user"])
        self.assertNotIn("末段结束时间", captured["user"])

    def test_download_timeout_refreshes_detail_and_retries_once(self):
        fake_tikhub = mock.Mock()
        stale = {
            "play_url": "https://stale.example/video.mp4",
            "duration": 10034,
        }
        fresh = {
            "play_url": "https://fresh.example/video.mp4",
            "duration": 10,
        }
        fake_tikhub.download_to_file.side_effect = [
            TimeoutError("下载超过预算（已下载 1.0MB）"),
            None,
        ]
        fake_tikhub.detail.return_value = fresh

        result = breakdown._download_breakdown_video(
            fake_tikhub,
            {"platform": "douyin", "id": "123", "note_type": "video"},
            stale,
            "target.mp4",
        )

        self.assertIs(result, fresh)
        self.assertEqual(fake_tikhub.download_to_file.call_count, 2)
        self.assertEqual(
            fake_tikhub.download_to_file.call_args_list[0].args[0],
            stale["play_url"],
        )
        self.assertEqual(
            fake_tikhub.download_to_file.call_args_list[1].args[0],
            fresh["play_url"],
        )
        fake_tikhub.detail.assert_called_once_with(
            "douyin", "123", "video", fresh=True
        )

    def test_missing_cached_play_url_refreshes_detail_before_download(self):
        fake_tikhub = mock.Mock()
        stale = {"duration": 10034}
        fresh = {
            "play_url": "https://fresh.example/video.mp4",
            "duration": 10,
        }
        fake_tikhub.detail.return_value = fresh

        result = breakdown._download_breakdown_video(
            fake_tikhub,
            {"platform": "douyin", "id": "123", "note_type": "video"},
            stale,
            "target.mp4",
        )

        self.assertIs(result, fresh)
        fake_tikhub.detail.assert_called_once_with(
            "douyin", "123", "video", fresh=True
        )
        fake_tikhub.download_to_file.assert_called_once()
        self.assertEqual(
            fake_tikhub.download_to_file.call_args.args[0],
            fresh["play_url"],
        )

    def test_image_post_without_play_url_is_rejected_without_refresh(self):
        fake_tikhub = mock.Mock()

        with self.assertRaises(ValueError):
            breakdown._download_breakdown_video(
                fake_tikhub,
                {"platform": "xiaohongshu", "id": "123", "note_type": "image"},
                {"images": ["https://cdn.example/image.jpg"]},
                "target.mp4",
            )

        fake_tikhub.detail.assert_not_called()
        fake_tikhub.download_to_file.assert_not_called()

    def test_incomplete_read_refreshes_detail_and_retries_once(self):
        fake_tikhub = mock.Mock()
        stale = {"play_url": "https://stale.example/video.mp4"}
        fresh = {"play_url": "https://fresh.example/video.mp4"}
        fake_tikhub.detail.return_value = fresh
        fake_tikhub.download_to_file.side_effect = [
            http.client.IncompleteRead(b"partial", 100),
            None,
        ]

        result = breakdown._download_breakdown_video(
            fake_tikhub,
            {"platform": "douyin", "id": "123", "note_type": "video"},
            stale,
            "target.mp4",
        )

        self.assertIs(result, fresh)
        self.assertEqual(fake_tikhub.download_to_file.call_count, 2)
        fake_tikhub.detail.assert_called_once_with(
            "douyin", "123", "video", fresh=True
        )

    def test_download_size_error_is_not_retried(self):
        fake_tikhub = mock.Mock()
        fake_tikhub.download_to_file.side_effect = ValueError(
            "file exceeds 26MB limit"
        )

        with self.assertRaisesRegex(ValueError, "26MB"):
            breakdown._download_breakdown_video(
                fake_tikhub,
                {"platform": "douyin", "id": "123", "note_type": "video"},
                {"play_url": "https://cdn.example/video.mp4"},
                "target.mp4",
            )

        fake_tikhub.detail.assert_not_called()
        fake_tikhub.download_to_file.assert_called_once()

    def test_download_retry_stays_bounded_and_returns_clear_error(self):
        fake_tikhub = mock.Mock()
        detail = {"play_url": "https://cdn.example/video.mp4"}
        fake_tikhub.detail.return_value = detail
        fake_tikhub.download_to_file.side_effect = TimeoutError(
            "下载超过预算（已下载 1.0MB）"
        )

        with self.assertRaisesRegex(TimeoutError, "刷新地址后重试仍失败"):
            breakdown._download_breakdown_video(
                fake_tikhub,
                {"platform": "douyin", "id": "123", "note_type": "video"},
                detail,
                "target.mp4",
            )

        self.assertEqual(fake_tikhub.download_to_file.call_count, 2)
        fake_tikhub.detail.assert_called_once_with(
            "douyin", "123", "video", fresh=True
        )

    def test_extract_frames_rejects_empty_visual_input(self):
        first = tempfile.mkdtemp()
        second = tempfile.mkdtemp()
        with mock.patch.object(
            breakdown.tempfile, "mkdtemp", side_effect=[first, second]
        ), mock.patch.object(
            breakdown.subprocess, "run", return_value=mock.Mock()
        ):
            with self.assertRaisesRegex(ValueError, "关键帧"):
                breakdown._extract_frames("video.mp4", count=4, duration=10)

    def test_parser_recovers_json_from_unclosed_code_fence(self):
        parsed = breakdown._parse_breakdown_json(
            '```json\n{"scenes":[{"scene":"完整镜头","line":""}]}'
        )
        self.assertEqual(parsed["scenes"][0]["scene"], "完整镜头")

    def test_validation_rejects_empty_and_placeholder_scenes(self):
        with self.assertRaisesRegex(ValueError, "为空"):
            breakdown._validate_scene_breakdown({"scenes": []})
        with self.assertRaisesRegex(ValueError, "占位"):
            breakdown._validate_scene_breakdown(
                {"scenes": [{"scene": "具体画面", "line": ""}]}
            )

    def test_validation_normalizes_common_alternative_scene_schema(self):
        result = breakdown._validate_scene_breakdown({
            "scenes": [{
                "duration": "4s",
                "description": "人物从竞技场入口走向中央，火焰位于后景。",
                "mood": "紧张",
                "composition": "广角跟拍",
                "lighting": "暖色侧光",
                "dialogue": "准备开始。",
            }],
        })
        scene = result["scenes"][0]
        self.assertIn("人物从竞技场入口", scene["scene"])
        self.assertEqual("准备开始。", scene["line"])
        self.assertEqual("4s", scene["dur"])
        self.assertEqual("广角跟拍", scene["camera"])

    def test_breakdown_retries_malformed_model_output(self):
        valid = json.dumps(
            {"scenes": [{"scene": "人物从桌边起身走向窗前，镜头缓慢跟随", "line": ""}]},
            ensure_ascii=False,
        )
        with mock.patch.object(
            breakdown, "_chat_multimodal", side_effect=["not json", valid]
        ) as chat:
            result = breakdown._request_breakdown_result("system", "user", "context", [])
        self.assertEqual(len(result["scenes"]), 1)
        self.assertEqual(chat.call_count, 2)

    def test_multimodal_timeout_is_localized_after_safe_retries(self):
        with mock.patch.object(breakdown, "ZHIPU_API_KEY", "secret-test-key"), \
             mock.patch.object(
                 breakdown.egress, "post_json_idempotent",
                 side_effect=TimeoutError("The read operation timed out"),
             ):
            with self.assertRaisesRegex(RuntimeError, "AI 分析响应超时"):
                breakdown._chat_multimodal("system", "user", [])

    def test_multimodal_http_error_is_not_mislabeled_as_timeout(self):
        error = urllib.error.HTTPError(
            "https://open.bigmodel.cn/api/paas/v4/chat/completions",
            413,
            "Payload Too Large",
            {},
            io.BytesIO(b'{"error":"too large"}'),
        )
        with mock.patch.object(breakdown, "ZHIPU_API_KEY", "secret-test-key"), \
             mock.patch.object(
                 breakdown.egress, "post_json_idempotent", side_effect=error,
             ), mock.patch("builtins.print") as logged:
            with self.assertRaisesRegex(RuntimeError, "素材数据过大"):
                breakdown._chat_multimodal("system", "user", [])

        message = " ".join(str(arg) for arg in logged.call_args.args)
        self.assertIn("http=413", message)
        self.assertIn("request_bytes=", message)
        self.assertNotIn("secret-test-key", message)

    def test_multimodal_connection_reset_is_reported_as_interrupted(self):
        with mock.patch.object(breakdown, "ZHIPU_API_KEY", "secret-test-key"), \
             mock.patch.object(
                 breakdown.egress,
                 "post_json_idempotent",
                 side_effect=ConnectionResetError("connection reset by peer"),
             ):
            with self.assertRaisesRegex(RuntimeError, "AI 分析连接中断"):
                breakdown._chat_multimodal("system", "user", [])

    def test_multimodal_frames_respect_provider_count_and_byte_budgets(self):
        budgets = []

        def fake_frame(path, max_bytes):
            budgets.append((path, max_bytes))
            return b"x" * max_bytes, "image/jpeg"

        with mock.patch.object(breakdown, "ZHIPU_API_KEY", "secret-test-key"), \
             mock.patch.object(
                 breakdown, "_bounded_ai_frame", side_effect=fake_frame,
             ), mock.patch.object(
                 breakdown.egress,
                 "post_json_idempotent",
                 return_value={"choices": [{"message": {"content": "ok"}}]},
             ) as posted:
            result = breakdown._chat_multimodal(
                "system", "user", ["frame-%d.jpg" % index for index in range(10)]
            )

        self.assertEqual(result, "ok")
        self.assertEqual(len(budgets), breakdown._AI_MAX_FRAMES)
        self.assertEqual(
            [item[0] for item in budgets],
            ["frame-0.jpg", "frame-3.jpg", "frame-6.jpg", "frame-9.jpg"],
        )
        self.assertLessEqual(
            sum(item[1] for item in budgets),
            breakdown._AI_FRAMES_TOTAL_MAX_BYTES,
        )
        body = json.loads(posted.call_args.args[3])
        self.assertEqual(
            len(body["messages"][1]["content"]) - 1,
            breakdown._AI_MAX_FRAMES,
        )

    def test_multimodal_optional_max_tokens_is_sent_to_provider(self):
        with mock.patch.object(breakdown, "ZHIPU_API_KEY", "secret-test-key"), \
             mock.patch.object(
                 breakdown.egress,
                 "post_json_idempotent",
                 return_value={"choices": [{"message": {"content": "ok"}}]},
             ) as posted:
            breakdown._chat_multimodal(
                "system", "user", [], max_tokens=1800,
            )
        body = json.loads(posted.call_args.args[3])
        self.assertEqual(body["max_tokens"], 1800)

    def test_ai_frame_fallback_without_pillow_keeps_a_hard_byte_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            small = pathlib.Path(directory) / "frame.png"
            small.write_bytes(b"small-png")
            with mock.patch.object(
                breakdown,
                "_bounded_thumbnail",
                side_effect=ModuleNotFoundError("No module named PIL"),
            ):
                frame, media_type = breakdown._bounded_ai_frame(str(small), 32)
                self.assertEqual(frame, b"small-png")
                self.assertEqual(media_type, "image/png")

                small.write_bytes(b"x" * 33)
                with self.assertRaisesRegex(ValueError, "数据过大"):
                    breakdown._bounded_ai_frame(str(small), 32)

    def test_frame_thumbnails_are_embedded_before_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            image = pathlib.Path(directory) / "frame.jpg"
            image.write_bytes(b"\xff\xd8\xff\xd9")
            thumbs = breakdown._frame_thumbnails([str(image)])
        self.assertEqual(thumbs, [])

    @unittest.skipIf(Image is None, "Pillow is not installed")
    def test_large_source_is_resized_and_bounded_before_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            source = pathlib.Path(directory) / "large.png"
            Image.new("RGB", (3000, 2000), (35, 120, 210)).save(source, "PNG")
            original_size = source.stat().st_size
            thumbs = breakdown._frame_thumbnails([str(source)])

        self.assertEqual(len(thumbs), 1)
        self.assertTrue(thumbs[0].startswith("data:image/jpeg;base64,"))
        raw = base64.b64decode(thumbs[0].split(",", 1)[1])
        self.assertLessEqual(len(raw), breakdown._THUMBNAIL_MAX_BYTES)
        self.assertNotEqual(len(raw), original_size)
        with Image.open(io.BytesIO(raw)) as thumbnail:
            self.assertLessEqual(max(thumbnail.size), breakdown._THUMBNAIL_MAX_EDGE)


if __name__ == "__main__":
    unittest.main()
